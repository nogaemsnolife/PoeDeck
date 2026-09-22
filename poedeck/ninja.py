"""poe.ninja data layer: leagues, currency and unique prices, icons, local price history."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import CURRENCY, HISTORY_KEEP_S, HISTORY_MIN_GAP_S, HTTP_TIMEOUT, ICON_DIR, USER_AGENT

API_BASE = "https://poe.ninja/poe1/api/economy"
LEAGUES_URL = f"{API_BASE}/leagues"
CURRENCY_URL = f"{API_BASE}/exchange/current/overview?league={{league}}&type=Currency"
ITEM_URL = f"{API_BASE}/stash/current/item/overview?league={{league}}&type={{type}}"
IMAGE_HOST = "https://web.poecdn.com"  # currency image paths in the API are relative to this host


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


class History:
    """Local price history: league -> item key -> [[timestamp, chaos], ...] (oldest first).

    poe.ninja only exposes daily history, so short change windows are computed from the
    prices this app itself observed. The newest point survives restarts, so after a restart
    the change is measured against the last price seen in the previous session.
    """

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict[str, list[list[float]]]] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.data = raw
        except (OSError, json.JSONDecodeError):
            pass

    def record(self, league: str, items: list[Item], ts: float) -> None:
        per_league = self.data.setdefault(league, {})
        for it in items:
            pts = per_league.setdefault(it.key, [])
            if pts and ts - pts[-1][0] < HISTORY_MIN_GAP_S:
                continue
            pts.append([ts, it.chaos])
            # drop points older than the retention window, but always keep the newest of them
            # so a change can still be measured after a long pause or a restart
            cutoff = ts - HISTORY_KEEP_S
            while len(pts) > 1 and pts[1][0] < cutoff:
                pts.pop(0)
        self.save()

    def change(self, league: str, key: str, chaos_now: float, window_s: int, now: float) -> tuple[float | None, bool]:
        """Return (percent change, approximate) against the stored point closest to `now - window_s`.

        The point recorded in the current refresh is ignored. If the closest point's age differs
        from the window by more than 20 %, `approximate` is True (rendered with a "~").
        """
        pts = self.data.get(league, {}).get(key)
        if not pts:
            return None, False
        target = now - window_s
        candidates = [pt for pt in pts if pt[0] <= now - HISTORY_MIN_GAP_S]
        if not candidates:
            return None, False
        ref_ts, ref_chaos = min(candidates, key=lambda pt: abs(pt[0] - target))
        if ref_chaos <= 0:
            return None, False
        approx = abs((now - ref_ts) - window_s) > 0.2 * window_s
        return (chaos_now / ref_chaos - 1.0) * 100.0, approx

    def save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass


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


def describe_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"network: {getattr(e, 'reason', e)}"
    return f"{type(e).__name__}: {e}"
