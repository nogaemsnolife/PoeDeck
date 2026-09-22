"""Number and label formatting shared by the UI."""
from __future__ import annotations

import re

from .config import CURRENCY

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


def fmt_change(c: float | None, approx: bool = False) -> str:
    if c is None:
        return "—"
    sign = "+" if c > 0 else ""
    return f"{'~' if approx else ''}{sign}{c:.1f}%"


def fmt_price(chaos: float, divine_rate: float | None, key: str) -> tuple[str, bool]:
    """Return (text, in_divines). Prices above half a divine are shown in divines; Divine Orb itself in chaos."""
    if key != f"{CURRENCY}:divine" and divine_rate and chaos >= divine_rate * 0.5:
        return f"{fmt_num(chaos / divine_rate)} div", True
    return f"{fmt_num(chaos)} c", False


def abbreviate(text: str, limit: int = SUB_MAX_CHARS) -> str:
    """Shorten a variant label word by word; cut with an ellipsis if it is still too long."""
    if len(text) <= limit:
        return text
    short = re.sub(r"[A-Za-z]+", lambda m: ABBREVIATIONS.get(m.group(0), m.group(0)), text)
    if len(short) > limit:
        short = short[:limit - 1].rstrip(" ,") + "…"
    return short
