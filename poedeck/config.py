"""Paths, constants and the persisted user configuration."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

from . import __version__

SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _nuitka_info():
    """Nuitka defines __compiled__ in the main module (and compiled modules); None when running from source."""
    main = sys.modules.get("__main__")
    return getattr(main, "__compiled__", None) or globals().get("__compiled__")


def is_frozen() -> bool:
    """True when running as a packaged executable (PyInstaller sets sys.frozen, Nuitka defines __compiled__)."""
    return bool(getattr(sys, "frozen", False)) or _nuitka_info() is not None


def executable_dir() -> str | None:
    """Directory of the packaged executable, or None from source.

    Nuitka: sys.executable is the bundled python.exe (in %TEMP% for onefile) and
    __compiled__.containing_dir is the *parent* of the dist folder in standalone mode, so the
    reliable answer is __compiled__.original_argv0, the path the user launched (measured on
    Nuitka 4.2 in both modes). PyInstaller onefile: sys.executable is the bundle itself.
    """
    info = _nuitka_info()
    if info is not None:
        exe = getattr(info, "original_argv0", None) or (sys.argv[0] if sys.argv else "") or sys.executable
        return os.path.dirname(os.path.abspath(exe))
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return None


def _writable(path: str) -> bool:
    try:
        probe = os.path.join(path, ".poedeck-write-test")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def _resolve_app_dir() -> str:
    """Where user data lives: next to the executable (portable), or %LOCALAPPDATA%/PoeDeck if that is read-only.

    Keeping data beside the exe means unpacking a new version over the old one preserves
    config.json, history.json, icons/ and the log.
    """
    base = executable_dir() or SOURCE_ROOT
    if _writable(base):
        return base
    fallback = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PoeDeck")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def resource_path(*parts: str) -> str:
    """Path of a read-only file bundled with the app (PyInstaller unpacks to sys._MEIPASS,
    Nuitka onefile keeps package files next to this module's extracted copy)."""
    base = getattr(sys, "_MEIPASS", None) or SOURCE_ROOT
    return os.path.join(base, *parts)


APP_DIR = _resolve_app_dir()


def debug_paths() -> dict:
    """Everything that went into choosing APP_DIR; written to $POEDECK_PATHS_FILE at startup when set."""
    info = _nuitka_info()
    return {
        "is_frozen": is_frozen(),
        "executable_dir": executable_dir(),
        "app_dir": APP_DIR,
        "source_root": SOURCE_ROOT,
        "sys_executable": sys.executable,
        "sys_argv0": sys.argv[0] if sys.argv else "",
        "config_file": __file__,
        "sys_frozen": getattr(sys, "frozen", None),
        "nuitka": repr(info) if info is not None else None,
        "nuitka_env": {k: v for k, v in os.environ.items() if k.startswith("NUITKA_")},
        "localappdata": os.environ.get("LOCALAPPDATA"),
        "cwd": os.getcwd(),
    }


CONFIG_PATH = os.path.join(APP_DIR, "config.json")
ICON_DIR = os.path.join(APP_DIR, "icons")
HISTORY_PATH = os.path.join(APP_DIR, "history.json")
LOG_PATH = os.path.join(APP_DIR, "poedeck.log")
APP_ICON = resource_path("assets", "poedeck.ico")

USER_AGENT = f"PoeDeck/{__version__} (github.com/nogaemsnolife/PoeDeck)"
HTTP_TIMEOUT = 30

AUTO_LEAGUE = "auto"  # sentinel: pick the current softcore challenge league
CURRENCY = "Currency"

# (poe.ninja API type, human label). Order is used in the settings category dropdown.
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

# Price-change windows. "7d" is poe.ninja's own weekly figure; the others come from
# the local price history this app records on every refresh.
CHANGE_WINDOWS: list[tuple[str, str, int | None]] = [
    ("7d", "7d (poe.ninja)", None),
    ("24h", "24h", 24 * 3600),
    ("6h", "6h", 6 * 3600),
    ("1h", "1h", 3600),
]
CHANGE_LABEL = {code: label for code, label, _ in CHANGE_WINDOWS}
CHANGE_SECONDS = {code: secs for code, _, secs in CHANGE_WINDOWS}
HISTORY_KEEP_S = 3 * 24 * 3600   # retention of local price points
HISTORY_MIN_GAP_S = 4 * 60       # do not store points closer than this (manual refreshes)

MAX_LIST_ROWS = 200      # settings list cap; uniques categories have ~900 entries
MAX_LIVE_SEARCHES = 5    # simultaneous trade live searches (GGG limits these per account)
LIVE_MAX_HITS = 8        # listings kept in the live panel
LIVE_HIT_TTL_S = 10 * 60 # listings disappear from the panel after this long
LIVE_POPUP_SECONDS = 8


@dataclass
class Config:
    league: str = AUTO_LEAGUE
    selected: list[str] = field(default_factory=lambda: list(DEFAULT_SELECTED))  # "<Category>:<id>"
    interval_min: int = 10
    font_size: int = 14
    geometry: str = "460x360+100+100"
    always_on_top: bool = False
    show_icons: bool = True
    change_window: str = "24h"   # one of CHANGE_WINDOWS codes
    # official trade site live search
    poesessid: str = ""          # session cookie, provided by the user, stored only in this file
    live_searches: list[dict] = field(default_factory=list)  # {"league", "id", "label", "enabled"}
    live_sound: bool = True
    live_popup: bool = True

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
        if cfg.change_window not in CHANGE_SECONDS:
            cfg.change_window = "24h"
        # migrate v0.1 configs that stored bare currency ids
        cfg.selected = [k if ":" in k else f"{CURRENCY}:{k}" for k in cfg.selected]
        cfg.live_searches = [s for s in cfg.live_searches if isinstance(s, dict) and s.get("id") and s.get("league")]
        return cfg

    def save(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.__dict__, f, indent=2, ensure_ascii=False)
        except OSError:
            pass
