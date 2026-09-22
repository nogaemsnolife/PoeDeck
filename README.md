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
- Prices in chaos, or in divines above half a divine. 7-day change in colour.
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

## Notes

poe.ninja has no documented public API; the endpoints used here are the ones the site itself calls
(`/poe1/api/economy/...`). If they change, the URL constants at the top of `poedeck.py` are the only
place to update.
