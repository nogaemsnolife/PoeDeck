# PoeDeck

Minimal price dashboard for Path of Exile, built for a small always-on secondary monitor.
Pulls currency and unique item prices from [poe.ninja](https://poe.ninja) and refreshes them
on a timer. Single Python file, standard library only, ~45 MB RAM, idle CPU near zero.

## Run

Requires Python 3.10+ with tkinter (included in the standard Windows installer).

```bat
run.bat
```

or

```bat
pythonw poedeck.py
```

## Features

- League dropdown; "Auto (current SC)" picks the current softcore challenge league.
- Pick any currency or unique item (weapons, armours, accessories, flasks, jewels, maps, relics, tinctures)
  from the settings window with search. Unique variants and 5L/6L are separate entries.
- Prices in chaos, or in divines above half a divine.
- Price change column with a selectable window: 24h (default), 6h, 1h from a local price history
  the app records on every refresh, or poe.ninja's own 7d figure. A `~` marks a change measured
  against a point whose age differs noticeably from the window (e.g. right after a restart).
- Item icons, cached in `icons/` after the first download.
- Layout switches to 2-3 columns when the window is wide enough.
- Configurable refresh interval, font size, always-on-top. Window size and position are remembered.
- Hotkeys: `F5` refresh, `Ctrl+,` settings.

## Files

| File | Purpose |
|---|---|
| `poedeck.py` | the application |
| `config.json` | created on first run; league, selected items, options |
| `icons/` | cached item icons |
| `history.json` | local price history for the 1h/6h/24h change windows (3 days retention) |

## Notes

poe.ninja has no documented public API; the endpoints used here are the ones the site itself calls
(`/poe1/api/economy/...`). If they change, the URL constants at the top of `poedeck.py` are the only
place to update.

poe.ninja recalculates the economy overview roughly once an hour (measured: consecutive updates
61 minutes apart), so a refresh interval below 10 minutes gains nothing.
