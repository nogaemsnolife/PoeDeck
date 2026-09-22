"""PoeDeck - minimal poe.ninja price dashboard for a small secondary monitor.

Single-file tkinter app, standard library only. Shows selected currency and
unique item prices for a Path of Exile league with periodic auto-refresh.

Run without a console window:  pythonw poedeck.py
Hotkeys: F5 refresh, Ctrl+, open settings.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from tkinter import font as tkfont
from tkinter import ttk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
ICON_DIR = os.path.join(APP_DIR, "icons")

API_BASE = "https://poe.ninja/poe1/api/economy"
LEAGUES_URL = f"{API_BASE}/leagues"
CURRENCY_URL = f"{API_BASE}/exchange/current/overview?league={{league}}&type=Currency"
ITEM_URL = f"{API_BASE}/stash/current/item/overview?league={{league}}&type={{type}}"
IMAGE_HOST = "https://web.poecdn.com"  # currency image paths in the API are relative to this host
USER_AGENT = "PoeDeck/0.2 (personal dashboard; tkinter)"
HTTP_TIMEOUT = 30

AUTO_LEAGUE = "auto"  # sentinel: pick the current softcore challenge league
CURRENCY = "Currency"

# (API type, human label). Order is used in the settings category dropdown.
CATEGORIES: list[tuple[str, str]] = [
    (CURRENCY, "Currency"),
    ("UniqueWeapon", "Unique Weapons"),
    ("UniqueArmour", "Unique Armours"),
    ("UniqueAccessory", "Unique Accessories"),
    ("UniqueFlask", "Unique Flasks"),
    ("UniqueJewel", "Unique Jewels"),
    ("UniqueMap", "Unique Maps"),
    ("UniqueRelic", "Unique Relics"),
    ("UniqueTincture", "Unique Tinctures"),
]
CATEGORY_LABEL = dict(CATEGORIES)

DEFAULT_SELECTED = [
    f"{CURRENCY}:divine", f"{CURRENCY}:exalted", f"{CURRENCY}:mirror", f"{CURRENCY}:ancient-orb",
    f"{CURRENCY}:annul", f"{CURRENCY}:fracturing-orb", f"{CURRENCY}:sacred-orb",
]

MAX_LIST_ROWS = 200  # settings list cap; uniques categories have ~900 entries

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


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
@dataclass
class Config:
    league: str = AUTO_LEAGUE
    selected: list[str] = field(default_factory=lambda: list(DEFAULT_SELECTED))  # "<Category>:<id>"
    interval_min: int = 5
    font_size: int = 14
    geometry: str = "460x360+100+100"
    always_on_top: bool = False
    show_icons: bool = True

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except (OSError, json.JSONDecodeError):
            pass
        cfg.interval_min = max(1, int(cfg.interval_min))
        # migrate v0.1 configs that stored bare currency ids
        cfg.selected = [k if ":" in k else f"{CURRENCY}:{k}" for k in cfg.selected]
        return cfg

    def save(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.__dict__, f, indent=2, ensure_ascii=False)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# Data layer
# ----------------------------------------------------------------------------
@dataclass
class Item:
    key: str            # "<Category>:<id>", stable across refreshes
    category: str
    name: str
    sub: str            # secondary text: variant / link count
    group: str          # currency: API category; uniques: base type
    chaos: float
    change_7d: float | None
    icon_url: str

    @property
    def icon_file(self) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.key.replace(":", "_"))
        return os.path.join(ICON_DIR, safe + ".png")


@dataclass
class Snapshot:
    league: str
    divine_rate: float | None = None          # chaos per divine
    items: dict[str, dict[str, Item]] = field(default_factory=dict)  # category -> key -> Item
    fetched: dict[str, float] = field(default_factory=dict)          # category -> timestamp
    updated_at: float = 0.0

    def get(self, key: str) -> Item | None:
        cat = key.split(":", 1)[0]
        return self.items.get(cat, {}).get(key)


def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_leagues() -> list[str]:
    data = http_get_json(LEAGUES_URL)
    return [entry["name"] for entry in data if "name" in entry]


def pick_softcore_league(leagues: list[str]) -> str:
    """First league that is neither hardcore nor permanent = current softcore challenge league."""
    for name in leagues:
        if name.lower().startswith("hardcore") or name in ("Standard", "Hardcore"):
            continue
        return name
    return "Standard"


def fetch_currency(league: str) -> tuple[dict[str, Item], float | None]:
    data = http_get_json(CURRENCY_URL.format(league=urllib.parse.quote(league)))
    meta = {it["id"]: it for it in data.get("items", [])}
    core = data.get("core", {})
    rate = core.get("rates", {}).get("divine")
    divine_rate = 1.0 / float(rate) if rate else None

    items: dict[str, Item] = {}
    for raw in data.get("lines", []):
        cid = raw.get("id")
        if not cid:
            continue
        m = meta.get(cid, {})
        change = (raw.get("sparkline") or {}).get("totalChange")
        image = m.get("image", "")
        key = f"{CURRENCY}:{cid}"
        items[key] = Item(
            key=key, category=CURRENCY, name=m.get("name", cid), sub="", group=m.get("category", "Other"),
            chaos=float(raw.get("primaryValue") or 0.0),
            change_7d=float(change) if change is not None else None,
            icon_url=(image if image.startswith("http") else IMAGE_HOST + image) if image else "",
        )
    if divine_rate is None and f"{CURRENCY}:divine" in items:
        divine_rate = items[f"{CURRENCY}:divine"].chaos
    return items, divine_rate


def fetch_uniques(league: str, category: str) -> dict[str, Item]:
    data = http_get_json(ITEM_URL.format(league=urllib.parse.quote(league), type=category))
    items: dict[str, Item] = {}
    for raw in data.get("lines", []):
        did = raw.get("detailsId")
        if not did:
            continue
        bits = []
        if raw.get("variant"):
            bits.append(str(raw["variant"]))
        links = raw.get("links")
        if isinstance(links, int) and links >= 5:
            bits.append(f"{links}L")
        change = (raw.get("sparkLine") or {}).get("totalChange")
        key = f"{category}:{did}"
        items[key] = Item(
            key=key, category=category, name=raw.get("name", did), sub=" · ".join(bits),
            group=raw.get("baseType") or "", chaos=float(raw.get("chaosValue") or 0.0),
            change_7d=float(change) if change is not None else None, icon_url=raw.get("icon") or "",
        )
    return items


def download_icons(items: list[Item]) -> int:
    """Download icons that are not cached yet. Returns how many were fetched. Runs in a worker thread."""
    fetched = 0
    os.makedirs(ICON_DIR, exist_ok=True)
    for it in items:
        if not it.icon_url or os.path.exists(it.icon_file):
            continue
        try:
            req = urllib.request.Request(it.icon_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = resp.read()
            tmp = it.icon_file + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, it.icon_file)
            fetched += 1
        except (urllib.error.URLError, OSError):
            continue
    return fetched


def fmt_num(v: float) -> str:
    if v >= 10000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:.0f}"
    if v >= 10:
        return f"{v:.1f}"
    if v >= 1:
        return f"{v:.2f}"
    if v >= 0.01:
        return f"{v:.3f}"
    return f"{v:.4f}"


def fmt_change(c: float | None) -> str:
    if c is None:
        return ""
    sign = "+" if c > 0 else ""
    return f"{sign}{c:.1f}%"


def fmt_price(chaos: float, divine_rate: float | None, key: str) -> tuple[str, str]:
    """Return (text, color). Prices above half a divine are shown in divines; Divine Orb itself in chaos."""
    if key != f"{CURRENCY}:divine" and divine_rate and chaos >= divine_rate * 0.5:
        return f"{fmt_num(chaos / divine_rate)} div", FG_DIVINE
    return f"{fmt_num(chaos)} c", FG_ACCENT


# Word-level abbreviations for unique variant labels, so the hint fits on the item's row.
ABBREVIATIONS = {
    "Requirements": "Req", "Requirement": "Req", "Level": "Lvl", "Physical": "Phys", "Elemental": "Ele",
    "Lightning": "Light", "Projectiles": "Proj", "Projectile": "Proj", "Damage": "Dmg", "Resistances": "Res",
    "Resistance": "Res", "Resist": "Res", "Duration": "Dur", "Reduction": "Red", "Recovery": "Rec",
    "Multi": "Mult", "Chance": "Chc", "Maximum": "Max", "Minimum": "Min", "Intelligence": "Int",
    "Strength": "Str", "Dexterity": "Dex", "Accuracy": "Acc", "Endurance": "End", "Evasion": "Eva",
    "Suppressed": "Supp", "Suppress": "Supp", "Cooldown": "CD", "Movement": "Move", "Notables": "Not.",
}
SUB_MAX_CHARS = 22


def abbreviate(text: str, limit: int = SUB_MAX_CHARS) -> str:
    """Shorten a variant label word by word; cut with an ellipsis if it is still too long."""
    if len(text) <= limit:
        return text
    short = re.sub(r"[A-Za-z]+", lambda m: ABBREVIATIONS.get(m.group(0), m.group(0)), text)
    if len(short) > limit:
        short = short[:limit - 1].rstrip(" ,") + "…"
    return short


def describe_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"network: {getattr(e, 'reason', e)}"
    return f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
class App:
    AUTO_LABEL = "Auto (current SC)"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = Config.load()
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

        root.title("PoeDeck")
        root.configure(bg=BG)
        root.geometry(self.cfg.geometry)
        root.minsize(280, 160)
        root.attributes("-topmost", bool(self.cfg.always_on_top))
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._setup_style()
        self._build()
        root.bind("<F5>", lambda e: self.refresh_now())
        root.bind("<Control-comma>", lambda e: self.open_settings())

        self.root.after(100, self._poll_queue)
        self._start_fetch(initial=True)

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
        style.configure("Vertical.TScrollbar", background=SCROLL, troughcolor=BG, arrowcolor=FG,
                        bordercolor=BG, lightcolor=SCROLL, darkcolor=SCROLL)
        style.map("Vertical.TScrollbar", background=[("active", "#4a5060")])

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

        price_txt, price_fg = fmt_price(item.chaos, snap.divine_rate, item.key)
        price = tk.Label(frame, text=price_txt, bg=bg, fg=price_fg, font=self._font(bold=True),
                         anchor="e", padx=6)
        price.grid(row=r, column=2, sticky="nsew")

        ch = item.change_7d
        ch_fg = FG_DIM if ch is None or abs(ch) < 0.05 else (FG_UP if ch > 0 else FG_DOWN)
        change = tk.Label(frame, text=fmt_change(ch), bg=bg, fg=ch_fg, font=self._font(-3),
                          anchor="e", width=8, padx=4)
        change.grid(row=r, column=3, sticky="nsew")
        self.row_widgets.extend((price, change))

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
                if kind == "data":
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
                    self.status_var.set(time.strftime("%H:%M", time.localtime(snap.updated_at)))
                    self.fetching = False
                    self._maybe_relayout(force=True)
                    self._sync_settings_list()
                    self._schedule_next()
                elif kind == "error":
                    stale = ""
                    if self.snapshot:
                        stale = " (showing " + time.strftime("%H:%M", time.localtime(self.snapshot.updated_at)) + ")"
                    self.status_var.set(f"error: {payload}{stale}")
                    self.fetching = False
                    self._render()
                    self._schedule_next()
                elif kind == "category":
                    league, cat, items = payload
                    self.loading_categories.discard(cat)
                    if self.snapshot and self.snapshot.league == league:
                        self.snapshot.items[cat] = items
                        self.snapshot.fetched[cat] = time.time()
                        self._maybe_relayout(force=True)
                        self._sync_settings_list()
                elif kind == "category_error":
                    cat, msg = payload
                    self.loading_categories.discard(cat)
                    self.status_var.set(f"error: {msg}")
                    self._sync_settings_list()
                elif kind == "icons":
                    self.icon_job_running = False
                    if payload:
                        self._render()
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

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
        cat_box = ttk.Combobox(top, textvariable=self.category_var, state="readonly", width=18,
                               values=[label for _, label in CATEGORIES], font=self._font(-3))
        cat_box.pack(side="left")
        cat_box.bind("<<ComboboxSelected>>", lambda e: self._on_category_changed())
        self.search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.search_var, style="Dark.TEntry", font=self._font(-3))
        ent.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ent.focus_set()
        self.search_var.trace_add("write", lambda *_: self._fill_settings_list())

        opts = tk.Frame(win, bg=BG, padx=8)
        opts.pack(fill="x")
        tk.Label(opts, text="Refresh, min:", bg=BG, fg=FG_DIM, font=self._font(-3)).pack(side="left")
        self.interval_var = tk.StringVar(value=str(self.cfg.interval_min))
        self._spin(opts, self.interval_var, 1, 120, 4).pack(side="left", padx=6)
        tk.Label(opts, text="Font:", bg=BG, fg=FG_DIM, font=self._font(-3)).pack(side="left", padx=(10, 0))
        self.font_var = tk.StringVar(value=str(self.cfg.font_size))
        self._spin(opts, self.font_var, 8, 40, 3).pack(side="left", padx=6)

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
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
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
        for cat, lbl in CATEGORIES:
            if lbl == label:
                return cat
        return CURRENCY

    def _on_category_changed(self):
        self.search_var.set("")  # also triggers _fill_settings_list via the trace
        cat = self._current_category()
        if self.snapshot and cat not in self.snapshot.items:
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
        if new_font != self.cfg.font_size or new_icons != self.cfg.show_icons:
            self.cfg.font_size = new_font
            self.cfg.show_icons = new_icons
            self._maybe_relayout(force=True)
        self.cfg.save()
        if not self.fetching:
            self._schedule_next()

    def _sync_settings_list(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self._fill_settings_list()

    def _fill_settings_list(self):
        if not (self.settings_win and self.settings_win.winfo_exists()):
            return
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)
        snap = self.snapshot
        cat = self._current_category()

        def hint(text):
            tk.Label(self.list_frame, text=text, bg=BG, fg=FG_DIM, font=self._font(-3), wraplength=420,
                     justify="left").pack(anchor="w", pady=6)

        if snap is None:
            hint("The list appears after the first successful update.")
            return
        if cat not in snap.items:
            hint("Loading…" if cat in self.loading_categories else "Not loaded. Re-select the category to retry.")
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
                hint(f"…{len(items) - shown} more. Type to narrow the list.")
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
            hint("No matches.")

    def _toggle(self, key: str, on: bool):
        if on and key not in self.cfg.selected:
            self.cfg.selected.append(key)
        elif not on and key in self.cfg.selected:
            self.cfg.selected.remove(key)
        self.cfg.save()
        self._maybe_relayout(force=True)

    # -- lifecycle --------------------------------------------------------------------------
    def on_close(self):
        self.cfg.geometry = self.root.geometry()
        self.cfg.save()
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - older Windows without shcore
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
