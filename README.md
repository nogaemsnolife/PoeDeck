# PoeDeck

Minimal price dashboard for Path of Exile, built for a small always-on secondary monitor.
Pulls currency and unique item prices from [poe.ninja](https://poe.ninja) and refreshes them
on a timer, and can watch official trade site live searches. Pure Python, standard library only,
~50 MB RAM, idle CPU near zero.

## Download

Prebuilt Windows builds are on the [Releases](https://github.com/nogaemsnolife/PoeDeck/releases) page.
Unpack the zip anywhere and run `PoeDeck.exe`; no installation and no Python needed. The app is
portable: `config.json`, `history.json`, `icons/` and the log are created next to the exe, so a new
version unpacked over the old one keeps your settings. If that folder is read-only (e.g. Program
Files), data goes to `%LOCALAPPDATA%\PoeDeck` instead.

The executable is not code-signed, so Windows SmartScreen warns on first launch: choose
"More info" → "Run anyway". Keep `PoeDeck.exe` together with the DLLs in its folder.

Antivirus note: builds are done by GitHub Actions from this repository (see
`.github/workflows/release.yml`) with Nuitka in standalone mode, i.e. no self-extracting
archive, which keeps machine-learning heuristics such as `Trojan:Win32/Wacatac.B!ml` from
misfiring. If your scanner still complains, compare the zip's SHA-256 with the `.sha256` file
on the release and, if in doubt, run from source.

## Run from source

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
  bottom with a sound and a popup. Listings sold through the NPC market (the current default) are
  tagged `NPC` and get a `Travel` button that asks the trade site to move your character to the
  seller's hideout, exactly like the site's own button; it is never triggered automatically.
  Listings with a whisper are copied to the clipboard on click. Requires your `POESESSID` cookie,
  entered in settings; it is stored only in your local `config.json` and sent only to pathofexile.com.

## Files

| File | Purpose |
|---|---|
| `main.py` | entry point (`pythonw main.py` or `python -m poedeck`) |
| `poedeck/ui.py` | tkinter windows: dashboard, settings, live panel |
| `poedeck/ninja.py` | poe.ninja data layer and local price history |
| `poedeck/trade.py` | trade site live search: WebSocket client, listing fetch, rate limiting |
| `poedeck/config.py`, `poedeck/format.py` | settings and constants, text formatting |
| `assets/poedeck.ico` | application icon |
| `.github/workflows/` | CI on every push; a `v*` tag builds the exe with Nuitka and publishes a release |
| `config.json` | created on first run; league, selected items, options, live searches, POESESSID |
| `icons/` | cached item icons |
| `history.json` | local price history for the 1h/6h/24h change windows (3 days retention) |
| `poedeck.log` | runtime log (live search messages, fetch results, errors); no cookie or seller names |

## Notes

poe.ninja has no documented public API; the endpoints used here are the ones the site itself calls
(`/poe1/api/economy/...`). If they change, the URL constants at the top of `poedeck.py` are the only
place to update.

### Making a release

Bump `__version__` in `poedeck/__init__.py`, commit, then tag and push: `git tag v0.4.0 && git push origin v0.4.0`.
GitHub Actions builds `PoeDeck.exe`, zips it with README and LICENSE and attaches it to the release
with generated notes. Running the workflow manually ("Run workflow" button) builds the same zip as a
downloadable artifact without publishing a release.

The trade API is rate limited per IP; the app reads the `X-Rate-Limit-*` headers and backs off.
Running many other trade tools at the same time can still trigger a temporary block.

poe.ninja recalculates the economy overview roughly once an hour (measured: consecutive updates
61 minutes apart), so a refresh interval below 10 minutes gains nothing.
