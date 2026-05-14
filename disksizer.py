#!/usr/bin/env python3
"""DiskSizer — folder-size analyser (TreeSize-style). No external dependencies."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_size(n: int) -> str:
    if n < 1_024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1_024:.1f} KB"
    if n < 1_073_741_824:
        return f"{n / 1_048_576:.1f} MB"
    if n < 1_099_511_627_776:
        return f"{n / 1_073_741_824:.2f} GB"
    return f"{n / 1_099_511_627_776:.2f} TB"


# ── Scanner ───────────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self, progress_cb=None):
        self.data: dict[str, tuple[int, list]] = {}
        self.cancelled = False
        self._progress_cb = progress_cb
        self._folder_count = 0

    def cancel(self) -> None:
        self.cancelled = True

    def scan(self, root: str) -> None:
        self._visit(root)

    def _visit(self, path: str) -> int:
        if self.cancelled:
            return 0
        total = 0
        entries: list = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    if self.cancelled:
                        break
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_file():
                            sz = e.stat().st_size
                            entries.append(("file", e.name, e.path, sz))
                            total += sz
                        elif e.is_dir():
                            sz = self._visit(e.path)
                            entries.append(("dir", e.name, e.path, sz))
                            total += sz
                    except OSError:
                        pass
        except OSError:
            pass
        entries.sort(key=lambda x: x[3], reverse=True)
        self.data[path] = (total, entries)
        self._folder_count += 1
        if self._progress_cb and self._folder_count % 100 == 0:
            self._progress_cb(self._folder_count, path)
        return total


# ── Application ───────────────────────────────────────────────────────────────

_PLACEHOLDER_TAG = "placeholder"
_COL_FOLDER = "#3a9e4f"   # dark green for folders
_COL_FILE   = "#71c883"   # lighter green for files
_COL_BAR_BG = "#f0faf2"   # canvas background


class DiskSizer:
    def __init__(self) -> None:
        self.win = tk.Tk()
        self.win.title("DiskSizer")
        self.win.geometry("980x660")
        self.win.minsize(640, 440)

        self._scanner: Scanner | None = None
        self._scan_thread: threading.Thread | None = None
        self._node_to_path: dict[str, str] = {}
        self._redraw_id: str | None = None

        self._build_ui()
        self.win.mainloop()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(2, weight=1)

        # Toolbar
        toolbar = tk.Frame(self.win, bg="#e8e8e8", pady=6, padx=8)
        toolbar.grid(row=0, column=0, sticky="ew")

        tk.Button(toolbar, text="Browse…", font=("Segoe UI", 9),
                  command=self._browse, padx=6).pack(side=tk.LEFT)

        self._path_var = tk.StringVar()
        path_entry = tk.Entry(toolbar, textvariable=self._path_var, font=("Segoe UI", 9))
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 4))
        path_entry.bind("<Return>", lambda _: self._start_scan(self._path_var.get()))

        self._scan_btn = tk.Button(toolbar, text="Scan", font=("Segoe UI", 9),
                                   command=self._on_scan_click, padx=10)
        self._scan_btn.pack(side=tk.LEFT)

        # Progress strip (hidden until scan starts)
        self._prog_frame = tk.Frame(self.win, bg="#e8e8e8", pady=3, padx=8)
        self._pbar = ttk.Progressbar(self._prog_frame, mode="indeterminate", length=260)
        self._pbar.pack(side=tk.LEFT)
        self._prog_lbl = tk.Label(self._prog_frame, text="", bg="#e8e8e8",
                                  font=("Segoe UI", 8), fg="#555")
        self._prog_lbl.pack(side=tk.LEFT, padx=6)

        # Tree frame
        tree_frame = tk.Frame(self.win)
        tree_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=3)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self._vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal")

        self._tree = ttk.Treeview(
            tree_frame,
            columns=("size", "raw_bytes", "frac", "kind"),
            yscrollcommand=self._on_tree_yscroll,
            xscrollcommand=hsb.set,
            selectmode="browse",
        )
        self._vsb.config(command=self._tree.yview)
        hsb.config(command=self._tree.xview)

        # Green bar canvas — sits between tree and scrollbar
        self._bar_canvas = tk.Canvas(
            tree_frame, width=165, bg=_COL_BAR_BG,
            highlightthickness=1, highlightbackground="#c8c8c8", bd=0,
        )

        self._tree.grid(row=0, column=0, sticky="nsew")
        self._bar_canvas.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        self._vsb.grid(row=0, column=2, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._tree.heading("#0",        text="Name", anchor="w")
        self._tree.heading("size",      text="Size", anchor="e")
        self._tree.heading("raw_bytes", text="",     anchor="w")
        self._tree.heading("frac",      text="",     anchor="w")
        self._tree.heading("kind",      text="Type", anchor="w")

        self._tree.column("#0",        width=380, minwidth=160, stretch=True)
        self._tree.column("size",      width=90,  anchor="e",  stretch=False)
        self._tree.column("raw_bytes", width=0,               stretch=False)
        self._tree.column("frac",      width=0,               stretch=False)
        self._tree.column("kind",      width=65,  anchor="w",  stretch=False)

        self._tree.bind("<<TreeviewOpen>>", self._on_expand)
        self._tree.bind("<Double-1>",        self._on_double_click)

        # Mousewheel over the bar canvas scrolls the tree
        self._bar_canvas.bind("<MouseWheel>", lambda e: self._tree.yview_scroll(
            int(-1 * (e.delta / 120)), "units"
        ))
        self._bar_canvas.bind("<Configure>", lambda _: self._schedule_redraw())

        ctx = tk.Menu(self.win, tearoff=False)
        ctx.add_command(label="Open in Explorer", command=self._open_in_explorer)
        ctx.add_command(label="Scan this folder", command=self._rescan_node)
        self._ctx = ctx
        self._tree.bind("<Button-3>", self._show_ctx_menu)

        self._status = tk.StringVar(value="Select a folder above and press Scan.")
        tk.Label(self.win, textvariable=self._status, anchor="w",
                 relief=tk.SUNKEN, font=("Segoe UI", 8),
                 padx=5, pady=2).grid(row=3, column=0, sticky="ew")

        s = ttk.Style()
        try:
            s.theme_use("vista")
        except tk.TclError:
            pass
        s.configure("Treeview", rowheight=22, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    # ── Scroll sync ───────────────────────────────────────────────────────────

    def _on_tree_yscroll(self, *args) -> None:
        self._vsb.set(*args)
        self._schedule_redraw()

    # ── Scan flow ─────────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = filedialog.askdirectory(title="Choose a folder to analyse")
        if path:
            path = os.path.normpath(path)
            self._path_var.set(path)
            self._start_scan(path)

    def _on_scan_click(self) -> None:
        self._start_scan(self._path_var.get().strip())

    def _start_scan(self, path: str) -> None:
        if not path:
            return
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            self._status.set(f"Not a valid directory: {path}")
            return

        if self._scanner:
            self._scanner.cancel()

        self._tree.delete(*self._tree.get_children())
        self._node_to_path.clear()
        self._bar_canvas.delete("all")

        self._prog_frame.grid(row=1, column=0, sticky="ew")
        self._pbar.start(12)
        self._prog_lbl.config(text="Starting…")
        self._scan_btn.config(text="Cancel", command=self._cancel_scan)
        self._status.set(f"Scanning {path} …")

        self._scanner = Scanner(progress_cb=self._on_progress)
        self._scan_thread = threading.Thread(
            target=self._thread_scan, args=(path,), daemon=True
        )
        self._scan_thread.start()

    def _thread_scan(self, path: str) -> None:
        self._scanner.scan(path)  # type: ignore[union-attr]
        self.win.after(0, self._scan_complete, path)

    def _on_progress(self, count: int, path: str) -> None:
        label = (os.path.basename(path) or path)[-50:]
        self.win.after(0, lambda: self._prog_lbl.config(
            text=f"{count:,} folders … {label}"
        ))

    def _cancel_scan(self) -> None:
        if self._scanner:
            self._scanner.cancel()
        self._status.set("Cancelling…")

    def _scan_complete(self, root_path: str) -> None:
        self._pbar.stop()
        self._prog_frame.grid_remove()
        self._scan_btn.config(text="Scan", command=self._on_scan_click)

        sc = self._scanner
        if sc is None or sc.cancelled:
            self._status.set("Scan cancelled.")
            return

        data = sc.data
        if root_path not in data:
            self._status.set("Scan returned no data.")
            return

        total, _ = data[root_path]
        n_folders = len(data)
        n_files = sum(
            sum(1 for c in v[1] if c[0] == "file") for v in data.values()
        )

        name = os.path.basename(root_path) or root_path
        root_id = self._tree.insert(
            "", "end",
            text=f"  {name}",
            values=(fmt_size(total), total, 1.0, "Folder"),
            open=True,
        )
        self._node_to_path[root_id] = root_path
        self._fill_node(root_id, root_path)
        self._schedule_redraw()

        self._status.set(
            f"Total: {fmt_size(total)}   "
            f"Folders: {n_folders:,}   Files: {n_files:,}   "
            f"Path: {root_path}"
        )

    # ── Tree population ───────────────────────────────────────────────────────

    def _fill_node(self, parent_id: str, path: str) -> None:
        if self._scanner is None:
            return
        data = self._scanner.data
        if path not in data:
            return

        _, children = data[path]
        max_sz = children[0][3] if children else 1

        for kind, name, cpath, size in children:
            frac = size / max_sz if max_sz > 0 else 0.0
            if kind == "dir":
                iid = self._tree.insert(
                    parent_id, "end",
                    text=f"  {name}",
                    values=(fmt_size(size), size, frac, "Folder"),
                )
                self._node_to_path[iid] = cpath
                _, sub = data.get(cpath, (0, []))
                if sub:
                    self._tree.insert(iid, "end", text="", tags=(_PLACEHOLDER_TAG,))
            else:
                self._tree.insert(
                    parent_id, "end",
                    text=f"      {name}",
                    values=(fmt_size(size), size, frac, "File"),
                )

    def _on_expand(self, _event) -> None:
        node = self._tree.focus()
        kids = self._tree.get_children(node)
        if not kids:
            return
        if _PLACEHOLDER_TAG in self._tree.item(kids[0], "tags"):
            self._tree.delete(kids[0])
            path = self._node_to_path.get(node)
            if path:
                self._fill_node(node, path)
                self._schedule_redraw()

    def _on_double_click(self, _event) -> None:
        node = self._tree.focus()
        path = self._node_to_path.get(node)
        if path and os.path.isdir(path):
            self._path_var.set(path)
            self._start_scan(path)

    # ── Green bar canvas ──────────────────────────────────────────────────────

    def _schedule_redraw(self) -> None:
        if self._redraw_id:
            self.win.after_cancel(self._redraw_id)
        self._redraw_id = self.win.after(30, self._redraw_bars)

    def _redraw_bars(self) -> None:
        c = self._bar_canvas
        c.delete("all")
        cw = c.winfo_width()
        if cw <= 1:
            return

        # Draw a header band matching the Treeview heading style
        hdr_h = self._heading_height()
        c.create_rectangle(0, 0, cw, hdr_h, fill="#d9d9d9", outline="#b0b0b0")
        c.create_text(cw // 2, hdr_h // 2,
                      text="Size", font=("Segoe UI", 9, "bold"), fill="#333")

        # Draw a green bar for each visible tree row
        pad = 3

        def walk(node: str) -> None:
            for item in self._tree.get_children(node):
                bbox = self._tree.bbox(item)
                if bbox:
                    _, iy, _, ih = bbox
                    try:
                        frac = float(self._tree.set(item, "frac"))
                    except (ValueError, tk.TclError):
                        frac = 0.0
                    kind = self._tree.set(item, "kind")
                    color = _COL_FOLDER if kind == "Folder" else _COL_FILE
                    bw = max(2, int(frac * (cw - pad * 2)))
                    c.create_rectangle(
                        pad, iy + pad,
                        pad + bw, iy + ih - pad,
                        fill=color, outline="",
                    )
                if self._tree.item(item, "open"):
                    walk(item)

        walk("")

    def _heading_height(self) -> int:
        """Return the y-offset of the first tree row (= heading bar height)."""
        for item in self._tree.get_children(""):
            bbox = self._tree.bbox(item)
            if bbox:
                return bbox[1]
        return 22

    # ── Context menu ──────────────────────────────────────────────────────────

    def _show_ctx_menu(self, event) -> None:
        row = self._tree.identify_row(event.y)
        if row:
            self._tree.selection_set(row)
            self._ctx.post(event.x_root, event.y_root)

    def _open_in_explorer(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        path = self._node_to_path.get(sel[0])
        if not path:
            return
        target = path if os.path.isdir(path) else os.path.dirname(path)
        os.startfile(target)

    def _rescan_node(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        path = self._node_to_path.get(sel[0])
        if path and os.path.isdir(path):
            self._path_var.set(path)
            self._start_scan(path)


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    DiskSizer()
