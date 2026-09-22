# PoeDeck

Minimal price dashboard for Path of Exile, built for a small always-on secondary monitor.
Pulls currency and unique item prices from [poe.ninja](https://poe.ninja) and refreshes them
on a timer, and can watch official trade site live searches. Pure Python, standard library only,
~50 MB RAM, idle CPU near zero.

## Run

Requires Python 3.10+ with tkinter (included in the standard Windows installer).

```bat
run.bat
```

or

```bat
pythonw main.py
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
- Trade site live searches (up to 5): paste a search URL, and new listings appear in a panel at the
  bottom with a sound and a popup. Clicking a listing copies the whisper. Requires your `POESESSID`
  cookie, entered in settings; it is stored only in your local `config.json` and sent only to
  pathofexile.com.

## Files

| File | Purpose |
|---|---|
| `main.py` | entry point (`pythonw main.py` or `python -m poedeck`) |
| `poedeck/ui.py` | tkinter windows: dashboard, settings, live panel |
| `poedeck/ninja.py` | poe.ninja data layer and local price history |
| `poedeck/trade.py` | trade site live search: WebSocket client, listing fetch, rate limiting |
| `poedeck/config.py`, `poedeck/format.py` | settings and constants, text formatting |
| `config.json` | created on first run; league, selected items, options, live searches, POESESSID |
| `icons/` | cached item icons |
| `history.json` | local price history for the 1h/6h/24h change windows (3 days retention) |

## Notes

poe.ninja has no documented public API; the endpoints used here are the ones the site itself calls
(`/poe1/api/economy/...`). If they change, the URL constants at the top of `poedeck.py` are the only
place to update.

The trade API is rate limited per IP; the app reads the `X-Rate-Limit-*` headers and backs off.
Running many other trade tools at the same time can still trigger a temporary block.

poe.ninja recalculates the economy overview roughly once an hour (measured: consecutive updates
61 minutes apart), so a refresh interval below 10 minutes gains nothing.
