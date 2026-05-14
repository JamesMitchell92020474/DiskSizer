# DiskSizer

A lightweight, TreeSize-style disk space analyser for Windows built with Python and tkinter. No external dependencies — just Python 3.9+.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Scan any folder or drive** — type a path (e.g. `C:\`) or use the Browse button
- **Sorted by size** — largest items always appear first at every level
- **Drill down** — click the expand arrow on any folder to reveal its contents
- **Double-click** a folder to re-scan it as the new root
- **Green size bars** — proportional bars drawn inline in the tree, colour-coded by type
- **Live updates** — the app watches the scanned folder in real time using `ReadDirectoryChangesW`; deletions, additions, renames, and size changes are reflected automatically within ~250 ms
- **Delete from the app** — right-click any item and choose **Delete…** to permanently remove it (with confirmation); the tree updates instantly
- **Live progress** — background scanning with a folder counter and cancel button
- **Right-click menu** — open in Explorer, re-scan, or delete
- **Status bar** — total size, folder count, file count, and live-watch indicator after each scan

## Requirements

- Python 3.9 or later
- tkinter (included with all standard Python Windows installers)

## Running

Double-click `run.bat`, or from a terminal:

```
python disksizer.py
```

## Usage

1. Click **Browse…** to pick a folder, or type a path directly (e.g. `C:\Users\YourName`) and press **Enter** or **Scan**
2. Wait for the scan to finish — progress is shown in the toolbar
3. Click the **▶** arrow next to any folder to expand it and see its contents
4. **Double-click** a folder to zoom in and re-scan from there
5. **Right-click** any item for additional options (Explorer, re-scan, delete)
6. Changes made in Explorer are reflected automatically — no need to re-scan

## License

MIT
