# Zendesk Tickets — local search

A small, self-contained tool for searching an offline Zendesk export. It builds
a full-text search index over your exported tickets, comments, users, and
organizations, then opens a browser-based search UI on your own machine. No
data leaves your computer.

## Requirements

- **Python 3.10 or newer** on the machine you'll run it from.
- Your Zendesk export files (NDJSON) — see [`data/README.txt`](data/README.txt)
  for the expected filenames.

That's the only dependency. The app is built from the Python standard library
alone — no `pip install` step.

## Install Python

### Windows
1. Go to <https://www.python.org/downloads/> and download the latest installer.
2. Run it. **Tick "Add Python to PATH"** at the bottom of the first screen,
   then click "Install Now".

### Mac
Either:
- Download the installer from <https://www.python.org/downloads/> and run it,
  **or**
- If you use Homebrew: `brew install python`.

### Linux
Most distros include Python 3 already. If not:
- Debian / Ubuntu: `sudo apt install python3`
- Fedora / RHEL: `sudo dnf install python3`

## Run the app

1. Put your Zendesk export files into the `data/` folder next to this README.
   See [`data/README.txt`](data/README.txt) for the list of expected files.
2. Double-click the launcher for your OS:

   | OS      | File to double-click |
   | ------- | -------------------- |
   | Windows | `Run on Windows.bat` |
   | Mac     | `Run on Mac.command` |
   | Linux   | `run-linux.sh` (or run `./run-linux.sh` from a terminal) |

3. The first launch builds a search index from your data files. This takes a
   few seconds to a few minutes depending on how much data you have. Subsequent
   launches start instantly.
4. Your default browser opens to <http://127.0.0.1:8765/>. Search away.

## Stopping the server

Close the terminal/cmd window that opened, or press **Ctrl+C** in it.

## Mac: first-run Gatekeeper warning

The first time you double-click `Run on Mac.command`, macOS may refuse to open
it with "cannot verify developer". To get past this:

1. **Right-click** (or Control-click) `Run on Mac.command`.
2. Choose **Open** from the context menu.
3. Click **Open** in the warning dialog.

After that, normal double-clicking works.

## Troubleshooting

- **"Python is not recognized" / not found** — Python isn't installed, or wasn't
  added to PATH on Windows. Reinstall and tick "Add Python to PATH".
- **"data directory not found"** — make sure your `*.ndjson` files are inside
  the `data/` folder next to the launcher script.
- **Port 8765 already in use** — another copy is already running, or another
  app is using that port. Close the other one, or run from a terminal with
  `python3 zdweb.py --port 8766`.
- **Force-rebuild the index** — delete `data/zdsearch.sqlite` and re-launch.
