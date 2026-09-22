"""tkinter user interface: main dashboard window, settings window, live-search panel and popups."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import ttk

from .config import (AUTO_LEAGUE, CATEGORIES, CATEGORY_LABEL, CHANGE_LABEL, CHANGE_SECONDS, CHANGE_WINDOWS,
                     CURRENCY, HISTORY_PATH, LIVE_HIT_TTL_S, LIVE_MAX_HITS, LIVE_POPUP_SECONDS, LOG_PATH,
                     MAX_LIST_ROWS, MAX_LIVE_SEARCHES, Config)
from .format import abbreviate, fmt_change, fmt_num, fmt_price
from .ninja import (History, Item, Snapshot, describe_error, download_icons, fetch_currency, fetch_leagues,
                    fetch_uniques, pick_softcore_league)
from .trade import HIDEOUT_TRAVEL_URL, Listing, LiveManager, LiveSearch, travel_to_hideout

# Dark palette
BG = "#15171c"
BG_ROW = "#1c1f26"
BG_HEAD = "#0f1115"
FG = "#e6e6e6"
FG_DIM = "#8a8f9a"
FG_ACCENT = "#d4a84b"
FG_UP = "#5fbf7a"
FG_DOWN = "#e06c6c"
FG_DIVINE = "#8fc1ff"
SCROLL = "#3a3f4b"

log = logging.getLogger("poedeck.ui")

LIVE_SETTINGS = "__live__"   # pseudo-category in the settings dropdown
LIVE_SETTINGS_LABEL = "Live searches (trade site)"

EDITABLE_CLASSES = ("Entry", "TEntry", "Spinbox", "TSpinbox")
# (virtual event, Latin letter, Windows keycode, Cyrillic keysyms, chars incl. control codes)
EDIT_SHORTCUTS = [
    ("<<Paste>>", "v", 86, ("Cyrillic_ve", "Cyrillic_VE"), ("v", "V", "", "м", "М")),
    ("<<Copy>>", "c", 67, ("Cyrillic_es", "Cyrillic_ES"), ("c", "C", "", "с", "С")),
    ("<<Cut>>", "x", 88, ("Cyrillic_che", "Cyrillic_CHE"), ("x", "X", "", "ч", "Ч")),
    ("<<SelectAll>>", "a", 65, ("Cyrillic_ef", "Cyrillic_EF"), ("a", "A", "", "ф", "Ф")),
]


def edit_action(keysym: str, keycode: int, char: str) -> str | None:
    """Map a Ctrl+key press to a virtual edit event regardless of keyboard layout.

    Tk binds editing shortcuts to Latin keysyms, so with a Cyrillic layout Ctrl+V arrives as
    Ctrl+Cyrillic_ve and does nothing. Returns None for Latin keysyms (Tk's own binding handles
    those, and generating the event again would paste twice) and for unrelated keys.
    """
    for action, latin, code, keysyms, chars in EDIT_SHORTCUTS:
        if keysym.lower() == latin:
            return None
        if keycode == code or keysym in keysyms or char in chars:
            return action
    return None


def play_alert() -> None:
    if sys.platform != "win32":
        return
    try:
        import winsound
        winsound.PlaySound("SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:  # noqa: BLE001 - sound is best effort
        pass


class App:
    AUTO_LABEL = "Auto (current SC)"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = Config.load()
        self.history = History(HISTORY_PATH)
        self.snapshot: Snapshot | None = None
        self.leagues: list[str] = []
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.timer_id: str | None = None
        self.fetching = False
        self.loading_categories: set[str] = set()
        self.icon_job_running = False
        self.settings_win: tk.Toplevel | None = None
        self.row_widgets: list[tk.Widget] = []
        self.icons: dict[tuple[str, int], tk.PhotoImage] = {}
        self.cols = 1
        self.resize_job: str | None = None
        # live search state
        self.live = LiveManager(self.q)
        self.live_status: dict[str, str] = {}
        self.live_hits: list[Listing] = []
        self.live_seen: "collections.deque[str]" = collections.deque(maxlen=1000)  # listing ids already shown
        self.live_widgets: list[tk.Widget] = []
        self.popup: tk.Toplevel | None = None
        self.last_alert_at = 0.0

        root.title("PoeDeck")
        root.configure(bg=BG)
        root.geometry(self.cfg.geometry)
        root.minsize(280, 160)
        root.attributes("-topmost", bool(self.cfg.always_on_top))
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._setup_style()
        self._build()
        self._setup_edit_shortcuts()
        root.bind("<F5>", lambda e: self.refresh_now())
        root.bind("<Control-comma>", lambda e: self.open_settings())

        self.root.after(100, self._poll_queue)
        self.root.after(30_000, self._purge_hits)
        self._start_fetch(initial=True)
        self._apply_live()

    # -- style -------------------------------------------------------------------
    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", fieldbackground=BG_ROW, background=BG_ROW, foreground=FG,
                        arrowcolor=FG, bordercolor=BG_HEAD, lightcolor=BG_ROW, darkcolor=BG_ROW,
                        selectbackground=BG_ROW, selectforeground=FG)
        style.map("TCombobox", fieldbackground=[("readonly", BG_ROW)], foreground=[("readonly", FG)],
                  selectbackground=[("readonly", BG_ROW)], selectforeground=[("readonly", FG)])
        self.root.option_add("*TCombobox*Listbox.background", BG_ROW)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", FG_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", BG)
        style.configure("Dark.TCheckbutton", background=BG, foreground=FG, focuscolor=BG,
                        indicatorbackground=BG_ROW, indicatorforeground=FG_ACCENT, indicatorcolor=BG_ROW)
        style.map("Dark.TCheckbutton", background=[("active", BG)], foreground=[("active", FG)],
                  indicatorbackground=[("selected", BG_ROW), ("active", BG_ROW)])
        style.configure("Dark.TEntry", fieldbackground=BG_ROW, foreground=FG, insertcolor=FG,
                        bordercolor=BG_HEAD, lightcolor=BG_ROW, darkcolor=BG_ROW)
        style.configure("Dark.TButton", background=BG_ROW, foreground=FG, bordercolor=BG_HEAD,
                        lightcolor=BG_ROW, darkcolor=BG_ROW, focuscolor=BG_ROW, padding=(8, 2))
        style.map("Dark.TButton", background=[("active", SCROLL)])
        style.configure("Vertical.TScrollbar", background=SCROLL, troughcolor=BG, arrowcolor=FG,
                        bordercolor=BG, lightcolor=SCROLL, darkcolor=SCROLL)
        style.map("Vertical.TScrollbar", background=[("active", "#4a5060")])

    def _setup_edit_shortcuts(self):
        """Layout-independent Ctrl+C/V/X/A and a right-click menu for every entry field (see edit_action)."""
        def on_ctrl_key(event):
            widget = event.widget
            if not hasattr(widget, "winfo_class") or widget.winfo_class() not in EDITABLE_CLASSES:
                return None
            action = edit_action(event.keysym, event.keycode, event.char)
            if action:
                widget.event_generate(action)
                return "break"
            return None

        menu = tk.Menu(self.root, tearoff=0, bg=BG_ROW, fg=FG, activebackground=FG_ACCENT, activeforeground=BG,
                       borderwidth=0)
        for label, action in (("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"), ("Paste", "<<Paste>>"),
                              ("Select all", "<<SelectAll>>")):
            menu.add_command(label=label, command=lambda a=action: self.root.focus_get() and
                             self.root.focus_get().event_generate(a))

        def on_right_click(event):
            if hasattr(event.widget, "winfo_class") and event.widget.winfo_class() in EDITABLE_CLASSES:
                event.widget.focus_set()
                menu.tk_popup(event.x_root, event.y_root)
                return "break"
            return None

        self.root.bind_all("<Control-KeyPress>", on_ctrl_key)
        self.root.bind_all("<Button-3>", on_right_click)

    def _font(self, delta: int = 0, bold: bool = False):
        return ("Segoe UI", self.cfg.font_size + delta, "bold" if bold else "normal")

    # -- main window layout --------------------------------------------------------
    def _build(self):
        head = tk.Frame(self.root, bg=BG_HEAD, padx=8, pady=6)
        head.pack(fill="x")

        self.league_var = tk.StringVar()
        self.league_box = ttk.Combobox(head, textvariable=self.league_var, state="readonly",
                                       width=18, font=self._font(-3))
        self.league_box.pack(side="left")
        self.league_box.bind("<<ComboboxSelected>>", self.on_league_changed)

        self._icon_button(head, "⚙", self.open_settings).pack(side="right", padx=(4, 0))
        self._icon_button(head, "↻", self.refresh_now).pack(side="right")

        self.status_var = tk.StringVar(value="loading…")
        tk.Label(head, textvariable=self.status_var, bg=BG_HEAD, fg=FG_DIM,
                 font=self._font(-4)).pack(side="right", padx=8)

        # live panel sits at the bottom; packed before the body so the body takes the rest
        self.live_panel = tk.Frame(self.root, bg=BG_HEAD, padx=8, pady=4)

        self.body = tk.Frame(self.root, bg=BG, padx=8, pady=6)
        self.body.pack(fill="both", expand=True)
        self.body.bind("<Configure>", self._on_body_resize)

    def _icon_button(self, parent, text, cmd):
        b = tk.Label(parent, text=text, bg=BG_HEAD, fg=FG_DIM, font=self._font(0), cursor="hand2", padx=4)
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(fg=FG))
        b.bind("<Leave>", lambda e: b.configure(fg=FG_DIM))
        return b

    # -- icons -----------------------------------------------------------------------
    def _icon_px(self) -> int:
        return 48 if self.cfg.font_size >= 22 else 24

    def _blank(self, px: int) -> tk.PhotoImage:
        """Transparent placeholder so the icon cell is sized in pixels even before the icon arrives."""
        key = ("__blank__", px)
        img = self.icons.get(key)
        if img is None:
            img = tk.PhotoImage(width=px, height=px)
            self.icons[key] = img
        return img

    def _icon(self, item: Item) -> tk.PhotoImage:
        px = self._icon_px()
        key = (item.key, px)
        img = self.icons.get(key)
        if img is not None:
            return img
        if not os.path.exists(item.icon_file):
            return self._blank(px)
        try:
            img = tk.PhotoImage(file=item.icon_file)
            longest = max(img.width(), img.height())
            if longest > px:
                img = img.subsample(math.ceil(longest / px))
        except tk.TclError:
            return self._blank(px)
        self.icons[key] = img
        return img

    def _ensure_icons(self):
        """Download icons for selected items that are not cached yet (background thread)."""
        if not self.cfg.show_icons or self.icon_job_running or self.snapshot is None:
            return
        need = [it for it in (self.snapshot.get(k) for k in self.cfg.selected)
                if it is not None and it.icon_url and not os.path.exists(it.icon_file)]
        if not need:
            return
        self.icon_job_running = True
        threading.Thread(target=lambda: self.q.put(("icons", download_icons(need))), daemon=True).start()

    # -- multi-column layout ---------------------------------------------------------
    def _column_min_width(self, items: list[Item]) -> int:
        f_name = tkfont.Font(font=self._font())
        f_sub = tkfont.Font(font=self._font(-3))
        f_price = tkfont.Font(font=self._font(bold=True))
        f_ch = tkfont.Font(font=self._font(-3))
        # paddings mirror _row(): name padx 6, sub padx 8+4, price padx 6+6, change padx 4+4
        widths = [f_name.measure(it.name) + 6 + (f_sub.measure(abbreviate(it.sub)) + 12 if it.sub else 0)
                  for it in items] or [f_name.measure("Mirror of Kalandra") + 6]
        w = max(widths)
        w += f_price.measure("9999 div") + 12
        w += max(f_ch.measure("-100.0%"), f_ch.measure("0" * 8)) + 8
        if self.cfg.show_icons:
            w += self._icon_px() + 8
        return w + 12 + 16  # inter-column gap + safety margin

    def _on_body_resize(self, _evt=None):
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(120, self._maybe_relayout)

    def _maybe_relayout(self, force: bool = False):
        self.resize_job = None
        if self.snapshot is None:
            if force:
                self._render()
            return
        present = [it for it in (self.snapshot.get(k) for k in self.cfg.selected) if it is not None]
        width = self.body.winfo_width() - 16
        cols = max(1, min(len(present) or 1, width // max(1, self._column_min_width(present))))
        if cols != self.cols or force:
            self.cols = cols
            self._render()

    # -- rendering -------------------------------------------------------------------
    def _render(self):
        for w in self.row_widgets:
            w.destroy()
        self.row_widgets.clear()
        for i in range(12):
            self.body.grid_columnconfigure(i, weight=0, uniform="")
        snap = self.snapshot
        if snap is None:
            return

        present = [it for it in (snap.get(k) for k in self.cfg.selected) if it is not None]
        pending = [k for k in self.cfg.selected if snap.get(k) is None]
        cols = max(1, min(self.cols, len(present) or 1))

        if snap.divine_rate:
            lbl = tk.Label(self.body, text=f"1 Divine = {fmt_num(snap.divine_rate)} c",
                           bg=BG, fg=FG_DIVINE, font=self._font(-2, bold=True), anchor="w")
            lbl.grid(row=0, column=0, columnspan=cols, sticky="w", pady=(0, 6))
            self.row_widgets.append(lbl)
        win_lbl = tk.Label(self.body, text=f"Δ {self.cfg.change_window}", bg=BG, fg=FG_DIM,
                           font=self._font(-4), anchor="e")
        win_lbl.grid(row=0, column=cols - 1, sticky="e", pady=(0, 6))
        self.row_widgets.append(win_lbl)

        per_col = math.ceil(len(present) / cols) if present else 0
        for c in range(cols):
            frame = tk.Frame(self.body, bg=BG)
            frame.grid(row=1, column=c, sticky="new", padx=(0 if c == 0 else 12, 0))
            frame.grid_columnconfigure(1, weight=1)
            self.body.grid_columnconfigure(c, weight=1, uniform="col")
            self.row_widgets.append(frame)
            for r, item in enumerate(present[c * per_col:(c + 1) * per_col]):
                self._row(frame, r, snap, item)

        if not self.cfg.selected:
            note = "Nothing selected yet.\nClick ⚙ to pick currency and unique items."
        elif pending:
            loading = self.fetching or any(k.split(":", 1)[0] in self.loading_categories for k in pending)
            note = "loading…" if loading else "no data for: " + ", ".join(k.split(":", 1)[1] for k in pending)
        else:
            note = ""
        if note:
            lbl = tk.Label(self.body, text=note, bg=BG, fg=FG_DIM, font=self._font(-4), anchor="w",
                           wraplength=420, justify="left")
            lbl.grid(row=2, column=0, columnspan=cols, sticky="w", pady=(8, 0))
            self.row_widgets.append(lbl)
        self._ensure_icons()

    def _row(self, frame: tk.Frame, r: int, snap: Snapshot, item: Item):
        bg = BG_ROW if r % 2 == 0 else BG
        if self.cfg.show_icons:
            px = self._icon_px()
            ic = tk.Label(frame, image=self._icon(item), bg=bg, width=px, height=px, padx=4)
            ic.grid(row=r, column=0, sticky="nsew")
            self.row_widgets.append(ic)

        cell = tk.Frame(frame, bg=bg)
        cell.grid(row=r, column=1, sticky="nsew")
        tk.Label(cell, text=item.name, bg=bg, fg=FG, font=self._font(), anchor="w").pack(side="left", padx=(6, 0))
        if item.sub:
            # variant / link count on the same line, dimmed, so every row has the same height
            tk.Label(cell, text=abbreviate(item.sub), bg=bg, fg=FG_DIM, font=self._font(-3),
                     anchor="s").pack(side="left", fill="y", padx=(8, 4))
        self.row_widgets.append(cell)

        price_txt, in_div = fmt_price(item.chaos, snap.divine_rate, item.key)
        price = tk.Label(frame, text=price_txt, bg=bg, fg=FG_DIVINE if in_div else FG_ACCENT,
                         font=self._font(bold=True), anchor="e", padx=6)
        price.grid(row=r, column=2, sticky="nsew")

        ch, approx = self._change(snap, item)
        ch_fg = FG_DIM if ch is None or abs(ch) < 0.05 else (FG_UP if ch > 0 else FG_DOWN)
        change = tk.Label(frame, text=fmt_change(ch, approx), bg=bg, fg=ch_fg, font=self._font(-3),
                          anchor="e", width=8, padx=4)
        change.grid(row=r, column=3, sticky="nsew")
        self.row_widgets.extend((price, change))

    def _change(self, snap: Snapshot, item: Item) -> tuple[float | None, bool]:
        window_s = CHANGE_SECONDS.get(self.cfg.change_window)
        if window_s is None:
            return item.change_7d, False
        return self.history.change(snap.league, item.key, item.chaos, window_s, snap.updated_at)

    # -- fetching ----------------------------------------------------------------------
    def _selected_categories(self) -> set[str]:
        return {k.split(":", 1)[0] for k in self.cfg.selected}

    def _start_fetch(self, initial: bool = False):
        """Full refresh: leagues (first time), currency, and every category that has selected items."""
        if self.fetching:
            return
        self.fetching = True
        self.status_var.set("updating…")
        need_leagues = initial or not self.leagues
        cfg_league = self.cfg.league
        known_leagues = list(self.leagues)
        categories = self._selected_categories() - {CURRENCY}

        def worker():
            try:
                leagues = fetch_leagues() if need_leagues else None
                league = cfg_league
                if league == AUTO_LEAGUE:
                    league = pick_softcore_league(leagues or known_leagues)
                snap = Snapshot(league=league)
                snap.items[CURRENCY], snap.divine_rate = fetch_currency(league)
                snap.fetched[CURRENCY] = time.time()
                for cat in sorted(categories):
                    snap.items[cat] = fetch_uniques(league, cat)
                    snap.fetched[cat] = time.time()
                snap.updated_at = time.time()
                self.q.put(("data", (leagues, snap)))
            except Exception as e:  # noqa: BLE001 - surfaced in the status bar
                self.q.put(("error", describe_error(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _load_category(self, category: str):
        """Fetch one category on demand (settings browsing) and merge it into the current snapshot."""
        if self.snapshot is None or category in self.loading_categories:
            return
        self.loading_categories.add(category)
        league = self.snapshot.league

        def worker():
            try:
                items = fetch_uniques(league, category)
                self.q.put(("category", (league, category, items)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("category_error", (category, describe_error(e))))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                handler = getattr(self, f"_on_{kind}", None)
                if handler:
                    handler(payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _on_data(self, payload):
        leagues, snap = payload
        if leagues:
            self.leagues = leagues
            self._refresh_league_box()
        # keep categories that were browsed but have no selection, so settings stay populated
        if self.snapshot and self.snapshot.league == snap.league:
            for cat, items in self.snapshot.items.items():
                if cat not in snap.items:
                    snap.items[cat] = items
                    snap.fetched[cat] = self.snapshot.fetched.get(cat, 0.0)
        self.snapshot = snap
        present = [it for it in (snap.get(k) for k in self.cfg.selected) if it is not None]
        self.history.record(snap.league, present, snap.updated_at)
        self.status_var.set(time.strftime("%H:%M", time.localtime(snap.updated_at)))
        self.fetching = False
        self._maybe_relayout(force=True)
        self._sync_settings_list()
        self._schedule_next()

    def _on_error(self, payload):
        stale = ""
        if self.snapshot:
            stale = " (showing " + time.strftime("%H:%M", time.localtime(self.snapshot.updated_at)) + ")"
        self.status_var.set(f"error: {payload}{stale}")
        self.fetching = False
        self._render()
        self._schedule_next()

    def _on_category(self, payload):
        league, cat, items = payload
        self.loading_categories.discard(cat)
        if self.snapshot and self.snapshot.league == league:
            self.snapshot.items[cat] = items
            self.snapshot.fetched[cat] = time.time()
            newly = [items[k] for k in self.cfg.selected if k in items]
            self.history.record(league, newly, self.snapshot.updated_at)
            self._maybe_relayout(force=True)
            self._sync_settings_list()

    def _on_category_error(self, payload):
        cat, msg = payload
        self.loading_categories.discard(cat)
        self.status_var.set(f"error: {msg}")
        self._sync_settings_list()

    def _on_icons(self, payload):
        self.icon_job_running = False
        if payload:
            self._render()

    def _schedule_next(self):
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
        self.timer_id = self.root.after(self.cfg.interval_min * 60 * 1000, self._start_fetch)

    def refresh_now(self):
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
        self._start_fetch()

    # -- league selector -----------------------------------------------------------------
    def _refresh_league_box(self):
        self.league_box["values"] = [self.AUTO_LABEL] + self.leagues
        self.league_var.set(self.AUTO_LABEL if self.cfg.league == AUTO_LEAGUE else self.cfg.league)

    def on_league_changed(self, _evt=None):
        choice = self.league_var.get()
        self.cfg.league = AUTO_LEAGUE if choice == self.AUTO_LABEL else choice
        self.cfg.save()
        self.snapshot = None  # prices of another league must not linger on screen
        self._render()
        self._sync_settings_list()
        self.refresh_now()

    # -- live searches: panel, popups ---------------------------------------------------
    def _live_searches(self) -> list[LiveSearch]:
        return [LiveSearch.from_dict(d) for d in self.cfg.live_searches]

    def _apply_live(self):
        searches = self._live_searches()
        self.live.apply(self.cfg.poesessid.strip(), searches)
        self._render_live()

    def _on_live_status(self, payload):
        key, text = payload
        self.live_status[key] = text
        self._render_live()
        self._sync_settings_list()

    def _on_live_listings(self, payload):
        listings: list[Listing] = payload
        known = set(self.live_seen)
        fresh = []
        for l in listings:
            if l.listing_id not in known:
                known.add(l.listing_id)
                self.live_seen.append(l.listing_id)
                fresh.append(l)
        log.info("ui: %d listings received, %d new", len(listings), len(fresh))
        if not fresh:
            return
        self.live_hits = (fresh[::-1] + self.live_hits)[:LIVE_MAX_HITS]
        self._render_live()
        now = time.time()
        if self.cfg.live_sound and now - self.last_alert_at > 2.0:
            play_alert()  # one sound per burst, the server delivers batches within a second
        self.last_alert_at = now
        if self.cfg.live_popup:
            self._show_popup(fresh[-1], extra=len(fresh) - 1)

    def _render_live(self):
        for w in self.live_widgets:
            w.destroy()
        self.live_widgets.clear()
        searches = [s for s in self._live_searches() if s.enabled]
        if not searches:
            self.live_panel.pack_forget()
            return
        if not self.live_panel.winfo_ismapped():
            self.body.pack_forget()
            self.live_panel.pack(side="bottom", fill="x")
            self.body.pack(fill="both", expand=True)

        head = tk.Frame(self.live_panel, bg=BG_HEAD)
        head.pack(fill="x")
        self.live_widgets.append(head)
        tk.Label(head, text="Live", bg=BG_HEAD, fg=FG_ACCENT, font=self._font(-3, bold=True)).pack(side="left")
        for s in searches:
            status = self.live_status.get(s.key, "…")
            ok = status == "connected"
            color = FG_UP if ok else (FG_DOWN if ("fail" in status or "not" in status) else FG_DIM)
            text = f"● {s.label}" if ok else f"● {s.label}: {status}"
            lbl = tk.Label(head, text=text, bg=BG_HEAD, fg=color, font=self._font(-4), cursor="hand2")
            lbl.pack(side="left", padx=(10, 0))
            lbl.bind("<Button-1>", lambda e, url=s.page_url: webbrowser.open(url))
        if self.live_hits:
            clear = tk.Label(head, text="clear", bg=BG_HEAD, fg=FG_DIM, font=self._font(-4), cursor="hand2")
            clear.pack(side="right")
            clear.bind("<Button-1>", lambda e: self._clear_hits())

        for i, hit in enumerate(self.live_hits):
            self._live_row(hit, i)

    def _live_row(self, hit: Listing, i: int):
        bg = BG_ROW if i % 2 == 0 else BG_HEAD
        row = tk.Frame(self.live_panel, bg=bg, cursor="hand2")
        row.pack(fill="x", pady=(1, 0))
        self.live_widgets.append(row)
        when = time.strftime("%H:%M", time.localtime(hit.received))
        parts = [
            (when, FG_DIM, -4), (hit.search_label, FG_DIM, -4), (hit.name, FG, -2),
            (hit.price, FG_ACCENT, -2, True), (hit.seller, FG_DIM, -4),
        ]
        if hit.hideout_token and not hit.whisper:
            parts.append(("NPC", FG_DIVINE, -4))  # sold by the seller's NPC; their online state is irrelevant
        else:
            parts.append((hit.online, FG_UP if hit.online == "online" else FG_DIM, -4))
        for text, fg, delta, *bold in parts:
            tk.Label(row, text=text, bg=bg, fg=fg, font=self._font(delta, bool(bold)), anchor="w",
                     padx=6).pack(side="left")
        close = tk.Label(row, text="\u2715", bg=bg, fg=FG_DIM, font=self._font(-3), cursor="hand2", padx=8)
        close.pack(side="right")
        close.bind("<Button-1>", lambda e, h=hit: self._dismiss_hit(h))
        if hit.hideout_token and HIDEOUT_TRAVEL_URL:
            btn = tk.Label(row, text="\u2302 Travel", bg=bg, fg=FG_ACCENT, font=self._font(-3, True),
                           cursor="hand2", padx=8)
            btn.pack(side="right")
            btn.bind("<Button-1>", lambda e, h=hit: self._travel(h))
        if hit.whisper:
            row.configure(cursor="hand2")
            row.bind("<Button-1>", lambda e, h=hit: self._copy_whisper(h))
            for child in row.winfo_children():
                if child.cget("text") not in ("\u2302 Travel", "\u2715"):
                    child.bind("<Button-1>", lambda e, h=hit: self._copy_whisper(h))
        else:
            row.configure(cursor="")

    def _dismiss_hit(self, hit: Listing):
        self.live_hits = [h for h in self.live_hits if h.listing_id != hit.listing_id]
        self._render_live()

    def _clear_hits(self):
        self.live_hits.clear()
        self._render_live()

    def _purge_hits(self):
        """Drop listings older than LIVE_HIT_TTL_S from the panel."""
        cutoff = time.time() - LIVE_HIT_TTL_S
        kept = [h for h in self.live_hits if h.received >= cutoff]
        if len(kept) != len(self.live_hits):
            self.live_hits = kept
            self._render_live()
        self.root.after(30_000, self._purge_hits)

    def _flash_status(self, text: str, seconds: int = 3):
        self.status_var.set(text)
        self.root.after(seconds * 1000, lambda: self.snapshot and self.status_var.set(
            time.strftime("%H:%M", time.localtime(self.snapshot.updated_at))))

    def _copy_whisper(self, hit: Listing):
        if hit.whisper:
            self.root.clipboard_clear()
            self.root.clipboard_append(hit.whisper)
            self._flash_status("whisper copied")
        else:
            self._flash_status("NPC listing: no whisper, use Travel")

    def _travel(self, hit: Listing):
        """Explicit user click only; never called automatically."""
        session_id = self.cfg.poesessid.strip()
        if not session_id:
            self._flash_status("POESESSID not set")
            return
        self._flash_status("travelling…")
        search = next((s for s in self._live_searches() if s.key == hit.search_key), None)
        threading.Thread(target=lambda: self.q.put(("live_flash", travel_to_hideout(hit.hideout_token, session_id,
                                                                                    search))), daemon=True).start()

    def _on_live_flash(self, payload):
        self._flash_status(str(payload), 5)

    def _show_popup(self, hit: Listing, extra: int = 0):
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()
        win = tk.Toplevel(self.root)
        self.popup = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=FG_ACCENT)
        inner = tk.Frame(win, bg=BG_HEAD, padx=12, pady=8)
        inner.pack(padx=1, pady=1)
        title = hit.search_label + (f"  (+{extra} more)" if extra else "")
        tk.Label(inner, text=title, bg=BG_HEAD, fg=FG_ACCENT, font=self._font(-3, bold=True), anchor="w").pack(fill="x")
        tk.Label(inner, text=hit.name, bg=BG_HEAD, fg=FG, font=self._font(0), anchor="w").pack(fill="x")
        seller = hit.seller if hit.hideout_token and not hit.whisper else f"{hit.seller} ({hit.online})"
        tk.Label(inner, text=f"{hit.price}   ·   {seller}", bg=BG_HEAD, fg=FG_DIM,
                 font=self._font(-3), anchor="w").pack(fill="x")
        hint = "click to copy whisper" if hit.whisper else ("NPC listing — use Travel in the Live panel"
                                                          if hit.hideout_token else "click to close")
        tk.Label(inner, text=hint, bg=BG_HEAD, fg=FG_DIM, font=self._font(-5), anchor="e").pack(fill="x")
        for w in (win, inner, *inner.winfo_children()):
            w.bind("<Button-1>", lambda e, h=hit: (hit.whisper and self._copy_whisper(h), win.destroy()))
        win.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{sw - w - 24}+{sh - h - 72}")
        win.after(LIVE_POPUP_SECONDS * 1000, lambda: win.winfo_exists() and win.destroy())

    # -- settings window -------------------------------------------------------------------
    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("PoeDeck — settings")
        win.configure(bg=BG)
        win.geometry("560x620")
        win.transient(self.root)

        top = tk.Frame(win, bg=BG, padx=8, pady=8)
        top.pack(fill="x")
        self.category_var = tk.StringVar(value=CATEGORY_LABEL[CURRENCY])
        cat_box = ttk.Combobox(top, textvariable=self.category_var, state="readonly", width=24,
                               values=[label for _, label in CATEGORIES] + [LIVE_SETTINGS_LABEL],
                               font=self._font(-3))
        cat_box.pack(side="left")
        cat_box.bind("<<ComboboxSelected>>", lambda e: self._on_category_changed())
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var, style="Dark.TEntry", font=self._font(-3))
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.search_entry.focus_set()
        self.search_var.trace_add("write", lambda *_: self._fill_settings_list())

        opts = tk.Frame(win, bg=BG, padx=8)
        opts.pack(fill="x")
        tk.Label(opts, text="Refresh, min:", bg=BG, fg=FG_DIM, font=self._font(-3)).pack(side="left")
        self.interval_var = tk.StringVar(value=str(self.cfg.interval_min))
        self._spin(opts, self.interval_var, 1, 120, 4).pack(side="left", padx=6)
        tk.Label(opts, text="Font:", bg=BG, fg=FG_DIM, font=self._font(-3)).pack(side="left", padx=(10, 0))
        self.font_var = tk.StringVar(value=str(self.cfg.font_size))
        self._spin(opts, self.font_var, 8, 40, 3).pack(side="left", padx=6)
        tk.Label(opts, text="Change:", bg=BG, fg=FG_DIM, font=self._font(-3)).pack(side="left", padx=(10, 0))
        self.change_var = tk.StringVar(value=CHANGE_LABEL[self.cfg.change_window])
        change_box = ttk.Combobox(opts, textvariable=self.change_var, state="readonly", width=13,
                                  values=[label for _, label, _ in CHANGE_WINDOWS], font=self._font(-3))
        change_box.pack(side="left", padx=6)
        change_box.bind("<<ComboboxSelected>>", lambda e: self._apply_options())

        opts2 = tk.Frame(win, bg=BG, padx=8, pady=4)
        opts2.pack(fill="x")
        self.topmost_var = tk.BooleanVar(value=self.cfg.always_on_top)
        ttk.Checkbutton(opts2, text="Always on top", variable=self.topmost_var, style="Dark.TCheckbutton",
                        command=self._apply_options).pack(side="left")
        self.icons_var = tk.BooleanVar(value=self.cfg.show_icons)
        ttk.Checkbutton(opts2, text="Icons", variable=self.icons_var, style="Dark.TCheckbutton",
                        command=self._apply_options).pack(side="left", padx=(12, 0))
        tk.Label(opts2, text="F5 — refresh", bg=BG, fg=FG_DIM, font=self._font(-4)).pack(side="right")

        wrap = tk.Frame(win, bg=BG)
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.list_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.list_window, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        win.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.check_vars: dict[str, tk.BooleanVar] = {}
        self._fill_settings_list()

    def _spin(self, parent, var, lo, hi, width):
        sp = tk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=width, bg=BG_ROW, fg=FG,
                        buttonbackground=BG_ROW, insertbackground=FG, relief="flat", font=self._font(-3),
                        command=self._apply_options)
        sp.bind("<FocusOut>", lambda e: self._apply_options())
        sp.bind("<Return>", lambda e: self._apply_options())
        return sp

    def _current_category(self) -> str:
        label = self.category_var.get()
        if label == LIVE_SETTINGS_LABEL:
            return LIVE_SETTINGS
        for cat, lbl in CATEGORIES:
            if lbl == label:
                return cat
        return CURRENCY

    def _on_category_changed(self):
        self.search_var.set("")  # also triggers _fill_settings_list via the trace
        cat = self._current_category()
        if cat != LIVE_SETTINGS and self.snapshot and cat not in self.snapshot.items:
            self._load_category(cat)
            self._fill_settings_list()

    def _apply_options(self):
        try:
            self.cfg.interval_min = max(1, int(self.interval_var.get()))
        except ValueError:
            pass
        try:
            new_font = max(8, int(self.font_var.get()))
        except ValueError:
            new_font = self.cfg.font_size
        self.cfg.always_on_top = bool(self.topmost_var.get())
        self.root.attributes("-topmost", self.cfg.always_on_top)
        new_icons = bool(self.icons_var.get())
        new_window = next((code for code, label, _ in CHANGE_WINDOWS if label == self.change_var.get()),
                          self.cfg.change_window)
        if (new_font != self.cfg.font_size or new_icons != self.cfg.show_icons
                or new_window != self.cfg.change_window):
            self.cfg.font_size = new_font
            self.cfg.show_icons = new_icons
            self.cfg.change_window = new_window
            self._maybe_relayout(force=True)
            self._render_live()
        self.cfg.save()
        if not self.fetching:
            self._schedule_next()

    def _sync_settings_list(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self._fill_settings_list()

    def _hint(self, text: str):
        tk.Label(self.list_frame, text=text, bg=BG, fg=FG_DIM, font=self._font(-3), wraplength=500,
                 justify="left").pack(anchor="w", pady=6)

    def _fill_settings_list(self):
        if not (self.settings_win and self.settings_win.winfo_exists()):
            return
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)
        cat = self._current_category()
        if cat == LIVE_SETTINGS:
            self._fill_live_settings()
            return
        snap = self.snapshot
        if snap is None:
            self._hint("The list appears after the first successful update.")
            return
        if cat not in snap.items:
            self._hint("Loading…" if cat in self.loading_categories else "Not loaded. Re-select the category to retry.")
            return

        query = self.search_var.get().strip().lower()
        selected = set(self.cfg.selected)
        items = list(snap.items[cat].values())
        if cat == CURRENCY:
            items.sort(key=lambda it: (it.key not in selected, it.group, it.name))
        else:
            items.sort(key=lambda it: (it.key not in selected, -it.chaos))
        if query:
            items = [it for it in items if query in it.name.lower() or query in it.sub.lower()
                     or query in it.group.lower()]

        shown = 0
        cur_group = None
        for it in items:
            if shown >= MAX_LIST_ROWS:
                self._hint(f"…{len(items) - shown} more. Type to narrow the list.")
                break
            group = "Selected" if it.key in selected else (it.group if cat == CURRENCY else "By price")
            if group != cur_group:
                cur_group = group
                tk.Label(self.list_frame, text=group, bg=BG, fg=FG_ACCENT,
                         font=self._font(-3, bold=True)).pack(anchor="w", pady=(8, 2))
            var = self.check_vars.get(it.key)
            if var is None:
                var = tk.BooleanVar(value=it.key in selected)
                self.check_vars[it.key] = var
            else:
                var.set(it.key in selected)
            price_txt, _ = fmt_price(it.chaos, snap.divine_rate, it.key)
            details = " · ".join(b for b in (it.group if cat != CURRENCY else "", it.sub) if b)
            text = f"{it.name}   ·   {price_txt}" + (f"   ({details})" if details else "")
            ttk.Checkbutton(self.list_frame, text=text, variable=var, style="Dark.TCheckbutton",
                            command=lambda k=it.key, v=var: self._toggle(k, v.get())).pack(anchor="w", padx=4)
            shown += 1
        if shown == 0:
            self._hint("No matches.")

    def _toggle(self, key: str, on: bool):
        if on and key not in self.cfg.selected:
            self.cfg.selected.append(key)
        elif not on and key in self.cfg.selected:
            self.cfg.selected.remove(key)
        self.cfg.save()
        self._maybe_relayout(force=True)

    # -- settings: live searches -----------------------------------------------------------
    def _fill_live_settings(self):
        lf = self.list_frame

        def section(text):
            tk.Label(lf, text=text, bg=BG, fg=FG_ACCENT, font=self._font(-3, bold=True)).pack(anchor="w", pady=(8, 2))

        section("Session")
        self._hint("POESESSID cookie from pathofexile.com (browser dev tools → Cookies). It stays in config.json "
                   "on this machine only and is sent to pathofexile.com exclusively.")
        row = tk.Frame(lf, bg=BG)
        row.pack(fill="x", padx=4)
        self.sess_var = tk.StringVar(value=self.cfg.poesessid)
        ent = ttk.Entry(row, textvariable=self.sess_var, style="Dark.TEntry", font=self._font(-3), show="•")
        ent.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Apply", style="Dark.TButton", command=self._apply_session).pack(side="left", padx=(6, 0))

        section(f"Searches ({sum(1 for s in self.cfg.live_searches if s.get('enabled', True))}/{MAX_LIVE_SEARCHES} active)")
        self._hint("Paste a trade search URL (with or without /live), give it a short name, Add.")
        add = tk.Frame(lf, bg=BG)
        add.pack(fill="x", padx=4)
        self.url_var = tk.StringVar()
        self.label_var = tk.StringVar()
        ttk.Entry(add, textvariable=self.url_var, style="Dark.TEntry", font=self._font(-3)).pack(side="left", fill="x", expand=True)
        name_ent = ttk.Entry(add, textvariable=self.label_var, style="Dark.TEntry", font=self._font(-3), width=14)
        name_ent.pack(side="left", padx=6)
        ttk.Button(add, text="Add", style="Dark.TButton", command=self._add_live_search).pack(side="left")
        self.live_error_var = tk.StringVar()
        tk.Label(lf, textvariable=self.live_error_var, bg=BG, fg=FG_DOWN, font=self._font(-4)).pack(anchor="w", padx=4)

        for i, d in enumerate(self.cfg.live_searches):
            s = LiveSearch.from_dict(d)
            r = tk.Frame(lf, bg=BG_ROW if i % 2 == 0 else BG)
            r.pack(fill="x", padx=4, pady=1)
            var = tk.BooleanVar(value=s.enabled)
            ttk.Checkbutton(r, variable=var, style="Dark.TCheckbutton",
                            command=lambda idx=i, v=var: self._toggle_live(idx, v.get())).pack(side="left")
            lbl = tk.Label(r, text=s.label, bg=r["bg"], fg=FG, font=self._font(-2), anchor="w", cursor="hand2")
            lbl.pack(side="left", padx=(2, 8))
            lbl.bind("<Button-1>", lambda e, url=s.page_url: webbrowser.open(url))
            tk.Label(r, text=s.league, bg=r["bg"], fg=FG_DIM, font=self._font(-4)).pack(side="left")
            status = self.live_status.get(s.key, "…" if s.enabled else "off")
            color = FG_UP if status == "connected" else (FG_DOWN if ("fail" in status or "not" in status) else FG_DIM)
            tk.Label(r, text=status, bg=r["bg"], fg=color, font=self._font(-4), anchor="w").pack(side="left", padx=8)
            rm = tk.Label(r, text="✕", bg=r["bg"], fg=FG_DIM, font=self._font(-2), cursor="hand2", padx=6)
            rm.pack(side="right")
            rm.bind("<Button-1>", lambda e, idx=i: self._remove_live(idx))
        if not self.cfg.live_searches:
            self._hint("No live searches yet.")

        section("Alerts")
        arow = tk.Frame(lf, bg=BG)
        arow.pack(fill="x", padx=4)
        self.sound_var = tk.BooleanVar(value=self.cfg.live_sound)
        self.popup_var = tk.BooleanVar(value=self.cfg.live_popup)
        ttk.Checkbutton(arow, text="Sound", variable=self.sound_var, style="Dark.TCheckbutton",
                        command=self._apply_live_options).pack(side="left")
        ttk.Checkbutton(arow, text="Popup", variable=self.popup_var, style="Dark.TCheckbutton",
                        command=self._apply_live_options).pack(side="left", padx=(12, 0))
        self._hint("Clicking a listing in the main window or the popup copies its whisper to the clipboard.")

    def _apply_session(self):
        self.cfg.poesessid = self.sess_var.get().strip()
        self.cfg.save()
        self._apply_live()
        self._sync_settings_list()

    def _add_live_search(self):
        search = LiveSearch.from_url(self.url_var.get(), self.label_var.get())
        if search is None:
            self.live_error_var.set("Not a trade search URL (expected .../trade/search/<league>/<id>).")
            return
        if any(d.get("id") == search.id and d.get("league") == search.league for d in self.cfg.live_searches):
            self.live_error_var.set("This search is already in the list.")
            return
        active = sum(1 for s in self.cfg.live_searches if s.get("enabled", True))
        search.enabled = active < MAX_LIVE_SEARCHES
        self.cfg.live_searches.append(search.to_dict())
        self.cfg.save()
        self.url_var.set("")
        self.label_var.set("")
        self.live_error_var.set("" if search.enabled else f"Added disabled: {MAX_LIVE_SEARCHES} searches already active.")
        self._apply_live()
        self._sync_settings_list()

    def _toggle_live(self, idx: int, on: bool):
        if idx >= len(self.cfg.live_searches):
            return
        active = sum(1 for i, s in enumerate(self.cfg.live_searches) if s.get("enabled", True) and i != idx)
        if on and active >= MAX_LIVE_SEARCHES:
            self.live_error_var.set(f"At most {MAX_LIVE_SEARCHES} searches can be active.")
            self._sync_settings_list()
            return
        self.cfg.live_searches[idx]["enabled"] = on
        self.cfg.save()
        self._apply_live()
        self._sync_settings_list()

    def _remove_live(self, idx: int):
        if idx < len(self.cfg.live_searches):
            key = LiveSearch.from_dict(self.cfg.live_searches[idx]).key
            del self.cfg.live_searches[idx]
            self.live_status.pop(key, None)
            self.cfg.save()
            self._apply_live()
            self._sync_settings_list()

    def _apply_live_options(self):
        self.cfg.live_sound = bool(self.sound_var.get())
        self.cfg.live_popup = bool(self.popup_var.get())
        self.cfg.save()

    # -- lifecycle --------------------------------------------------------------------------
    def on_close(self):
        self.cfg.geometry = self.root.geometry()
        self.cfg.save()
        self.live.shutdown()
        self.root.destroy()


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("poedeck").setLevel(logging.INFO)
    logging.getLogger("poedeck").addHandler(handler)


def main():
    setup_logging()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - older Windows without shcore
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
