#!/usr/bin/env python3
"""DiskSizer — folder-size analyser (TreeSize-style). No external dependencies."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import queue
import shutil
import struct
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


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


# ── File-system watcher (ReadDirectoryChangesW) ───────────────────────────────

_FILE_LIST_DIRECTORY   = 0x0001
_FILE_SHARE_READ       = 0x0001
_FILE_SHARE_WRITE      = 0x0002
_FILE_SHARE_DELETE     = 0x0004
_OPEN_EXISTING         = 3
_FILE_FLAG_BACKUP_SEMS = 0x02000000
_NOTIFY_FILTER         = 0x00000001 | 0x00000002 | 0x00000008  # file name | dir name | size

ACT_ADDED      = 1
ACT_REMOVED    = 2
ACT_MODIFIED   = 3
ACT_RENAME_OLD = 4
ACT_RENAME_NEW = 5


class FileWatcher:
    """Real-time directory watcher using Win32 ReadDirectoryChangesW.

    All events are posted to a queue.Queue so no tkinter calls are made
    from the background thread (avoids Win32 thread-safety issues).
    """

    def __init__(self, path: str, event_queue: queue.Queue):
        self._path    = path
        self._queue   = event_queue
        self._handle  = None   # raw int handle (c_void_p)
        self._running = False
        self._lock    = threading.Lock()

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            h, self._handle = self._handle, None
        if h is not None:
            # Closing the handle causes the blocking ReadDirectoryChangesW to return
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))

    def _run(self) -> None:
        k32 = ctypes.windll.kernel32

        # Must set restype to c_void_p — default c_int truncates to 32 bits on x64
        k32.CreateFileW.restype = ctypes.c_void_p

        handle = k32.CreateFileW(
            self._path,
            _FILE_LIST_DIRECTORY,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None, _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMS, None,
        )

        # INVALID_HANDLE_VALUE = (void*)-1; c_void_p returns it as a large uint or None
        if handle is None or handle in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            self._queue.put(("_err", "CreateFileW failed — cannot watch directory"))
            return

        with self._lock:
            if not self._running:           # stopped before we got here
                k32.CloseHandle(ctypes.c_void_p(handle))
                return
            self._handle = handle

        self._queue.put(("_ok", ""))        # signal that watcher started

        buf     = ctypes.create_string_buffer(65536)
        n_bytes = ctypes.wintypes.DWORD(0)

        while self._running:
            ok = k32.ReadDirectoryChangesW(
                ctypes.c_void_p(handle),
                ctypes.cast(buf, ctypes.c_void_p),
                ctypes.wintypes.DWORD(len(buf)),
                ctypes.wintypes.BOOL(True),
                ctypes.wintypes.DWORD(_NOTIFY_FILTER),
                ctypes.byref(n_bytes),
                None, None,
            )
            if not ok or n_bytes.value == 0:
                break

            offset = 0
            while True:
                try:
                    next_off, action, name_len = struct.unpack_from("III", buf, offset)
                    name = buf.raw[offset + 12: offset + 12 + name_len].decode("utf-16-le")
                    self._queue.put((action, name))
                except Exception:
                    break
                if next_off == 0:
                    break
                offset += next_off

        with self._lock:
            if self._handle == handle:
                k32.CloseHandle(ctypes.c_void_p(handle))
                self._handle = None


# ── Application ───────────────────────────────────────────────────────────────

_PLACEHOLDER_TAG = "placeholder"
_COL_FOLDER  = "#3a9e4f"
_COL_FILE    = "#71c883"
_COL_ROW_BG  = "#ffffff"


class DiskSizer:
    def __init__(self) -> None:
        self.win = tk.Tk()
        self.win.title("DiskSizer")
        self.win.geometry("980x660")
        self.win.minsize(640, 440)

        self._scanner:         Scanner | None     = None
        self._scan_thread:     threading.Thread | None = None
        self._watcher:         FileWatcher | None = None
        self._watched_root:    str                = ""
        self._rename_old_path: str                = ""

        self._node_to_path:    dict[str, str] = {}   # item_id  → abs_path
        self._path_to_node:    dict[str, str] = {}   # abs_path → item_id

        self._fs_queue:  queue.Queue = queue.Queue()
        self._redraw_id: str | None  = None
        self._header_h:  int         = 22

        self._build_ui()
        self.win.mainloop()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(2, weight=1)

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

        self._prog_frame = tk.Frame(self.win, bg="#e8e8e8", pady=3, padx=8)
        self._pbar = ttk.Progressbar(self._prog_frame, mode="indeterminate", length=260)
        self._pbar.pack(side=tk.LEFT)
        self._prog_lbl = tk.Label(self._prog_frame, text="", bg="#e8e8e8",
                                  font=("Segoe UI", 8), fg="#555")
        self._prog_lbl.pack(side=tk.LEFT, padx=6)

        tree_frame = tk.Frame(self.win)
        tree_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=3)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self._vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        hsb       = ttk.Scrollbar(tree_frame, orient="horizontal")

        self._tree = ttk.Treeview(
            tree_frame,
            columns=("size", "raw_bytes", "bar", "frac", "kind"),
            yscrollcommand=self._on_tree_yscroll,
            xscrollcommand=hsb.set,
            selectmode="browse",
        )
        self._vsb.config(command=self._tree.yview)
        hsb.config(command=self._tree.xview)

        self._tree.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._tree.heading("#0",        text="Name", anchor="w")
        self._tree.heading("size",      text="Size", anchor="e")
        self._tree.heading("raw_bytes", text="",     anchor="w")
        self._tree.heading("bar",       text="",     anchor="w")
        self._tree.heading("frac",      text="",     anchor="w")
        self._tree.heading("kind",      text="Type", anchor="w")

        self._tree.column("#0",        width=380, minwidth=160, stretch=True)
        self._tree.column("size",      width=90,  anchor="e",  stretch=False)
        self._tree.column("raw_bytes", width=0,               stretch=False)
        self._tree.column("bar",       width=150, anchor="w",  stretch=False, minwidth=40)
        self._tree.column("frac",      width=0,               stretch=False)
        self._tree.column("kind",      width=65,  anchor="w",  stretch=False)

        self._bar_canvas = tk.Canvas(tree_frame, bg=_COL_ROW_BG,
                                     highlightthickness=0, bd=0)
        self._bar_canvas.bind("<MouseWheel>", lambda e: self._tree.yview_scroll(
            int(-1 * e.delta / 120), "units"))

        self._tree.bind("<<TreeviewOpen>>", self._on_expand)
        self._tree.bind("<Double-1>",        self._on_double_click)
        self._tree.bind("<Configure>",       lambda _: self._schedule_reposition())

        ctx = tk.Menu(self.win, tearoff=False)
        ctx.add_command(label="Open in Explorer", command=self._open_in_explorer)
        ctx.add_command(label="Scan this folder", command=self._rescan_node)
        ctx.add_separator()
        ctx.add_command(label="Delete…",          command=self._delete_selected)
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

        # Start the event-queue drain loop (runs for the lifetime of the app)
        self._poll_fs_queue()

    # ── Scroll + canvas ───────────────────────────────────────────────────────

    def _on_tree_yscroll(self, *args) -> None:
        self._vsb.set(*args)
        self._schedule_redraw()

    def _schedule_reposition(self) -> None:
        if self._redraw_id:
            self.win.after_cancel(self._redraw_id)
        self._redraw_id = self.win.after(50, self._position_bar_canvas)

    def _schedule_redraw(self) -> None:
        if self._redraw_id:
            self.win.after_cancel(self._redraw_id)
        self._redraw_id = self.win.after(30, self._redraw_bars)

    def _position_bar_canvas(self) -> None:
        self.win.update_idletasks()
        roots = self._tree.get_children("")
        if not roots:
            return
        col_bbox  = self._tree.bbox(roots[0], "bar")
        item_bbox = self._tree.bbox(roots[0])
        if not col_bbox or not item_bbox:
            return
        col_x, _, col_w, _ = col_bbox
        self._header_h = item_bbox[1]
        tx = self._tree.winfo_x()
        ty = self._tree.winfo_y()
        th = self._tree.winfo_height()
        self._bar_canvas.place(
            x=tx + col_x, y=ty + self._header_h,
            width=col_w, height=max(1, th - self._header_h),
        )
        self._redraw_bars()

    def _redraw_bars(self) -> None:
        c = self._bar_canvas
        c.delete("all")
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw <= 1:
            return
        c.create_rectangle(0, 0, cw, ch, fill=_COL_ROW_BG, outline="")
        pad = 2

        def walk(node: str) -> None:
            for item in self._tree.get_children(node):
                col_bbox = self._tree.bbox(item, "bar")
                if col_bbox:
                    _, by, _, bh = col_bbox
                    cy = by - self._header_h
                    try:
                        frac = float(self._tree.set(item, "frac"))
                    except (ValueError, tk.TclError):
                        frac = 0.0
                    color = _COL_FOLDER if self._tree.set(item, "kind") == "Folder" else _COL_FILE
                    bw = max(2, int(frac * (cw - pad * 2)))
                    c.create_rectangle(pad, cy + pad, pad + bw, cy + bh - pad,
                                       fill=color, outline="")
                if self._tree.item(item, "open"):
                    walk(item)
        walk("")

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

        if self._watcher:
            self._watcher.stop()
            self._watcher = None
        if self._scanner:
            self._scanner.cancel()

        # Drain any stale watcher events from the previous scan
        while not self._fs_queue.empty():
            try:
                self._fs_queue.get_nowait()
            except queue.Empty:
                break

        self._tree.delete(*self._tree.get_children())
        self._node_to_path.clear()
        self._path_to_node.clear()
        self._bar_canvas.delete("all")

        self._prog_frame.grid(row=1, column=0, sticky="ew")
        self._pbar.start(12)
        self._prog_lbl.config(text="Starting…")
        self._scan_btn.config(text="Cancel", command=self._cancel_scan)
        self._status.set(f"Scanning {path} …")

        self._scanner = Scanner(progress_cb=self._on_progress)
        self._scan_thread = threading.Thread(
            target=self._thread_scan, args=(path,), daemon=True)
        self._scan_thread.start()

    def _thread_scan(self, path: str) -> None:
        self._scanner.scan(path)  # type: ignore[union-attr]
        self.win.after(0, self._scan_complete, path)

    def _on_progress(self, count: int, path: str) -> None:
        label = (os.path.basename(path) or path)[-50:]
        self.win.after(0, lambda: self._prog_lbl.config(
            text=f"{count:,} folders … {label}"))

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
        n_files   = sum(sum(1 for c in v[1] if c[0] == "file") for v in data.values())

        name    = os.path.basename(root_path) or root_path
        root_id = self._tree.insert(
            "", "end", text=f"  {name}",
            values=(fmt_size(total), total, "", 1.0, "Folder"),
            open=True,
        )
        self._node_to_path[root_id] = root_path
        self._path_to_node[root_path] = root_id
        self._fill_node(root_id, root_path)
        self.win.after(0, self._position_bar_canvas)

        # Start live watcher — posts events to _fs_queue, polled by _poll_fs_queue
        self._watched_root = root_path
        self._watcher = FileWatcher(root_path, self._fs_queue)
        self._watcher.start()

        self._status.set(
            f"Total: {fmt_size(total)}   Folders: {n_folders:,}   "
            f"Files: {n_files:,}   Path: {root_path}   [watching for changes]")

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
            frac   = size / max_sz if max_sz > 0 else 0.0
            is_dir = kind == "dir"
            iid    = self._tree.insert(
                parent_id, "end",
                text=f"  {name}" if is_dir else f"      {name}",
                values=(fmt_size(size), size, "", frac, "Folder" if is_dir else "File"),
            )
            self._node_to_path[iid] = cpath
            self._path_to_node[cpath] = iid
            if is_dir:
                _, sub = data.get(cpath, (0, []))
                if sub:
                    self._tree.insert(iid, "end", text="", tags=(_PLACEHOLDER_TAG,))

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

    # ── Live file-system updates ──────────────────────────────────────────────

    def _poll_fs_queue(self) -> None:
        """Drain the FS event queue on the main thread (runs every 250 ms)."""
        changed = False
        while True:
            try:
                item = self._fs_queue.get_nowait()
            except queue.Empty:
                break

            action = item[0]

            # Internal signals from the watcher thread
            if action == "_ok":
                cur = self._status.get()
                if "[watching for changes]" not in cur:
                    self._status.set(cur + "   [watching for changes]")
                continue
            if action == "_err":
                self._status.set(self._status.get() + f"   ⚠ {item[1]}")
                continue

            # Result posted back by _fs_add_bg after computing the new item's size
            if action == "_add":
                _, path, kind, sz = item
                self._fs_add_apply(path, kind, sz)
                changed = True
                continue

            # Real FS events (action is an int 1-5)
            if self._scanner is None or not self._watched_root:
                continue
            rel_path = item[1]
            full = os.path.normpath(os.path.join(self._watched_root, rel_path))

            if action == ACT_REMOVED:
                self._fs_remove(full)
                changed = True
            elif action == ACT_ADDED:
                threading.Thread(target=self._fs_add_bg,
                                 args=(full,), daemon=True).start()
            elif action == ACT_MODIFIED:
                self._fs_modify(full)
                changed = True
            elif action == ACT_RENAME_OLD:
                self._rename_old_path = full
            elif action == ACT_RENAME_NEW and self._rename_old_path:
                self._fs_rename(self._rename_old_path, full)
                self._rename_old_path = ""
                changed = True

        if changed:
            self._schedule_redraw()

        self.win.after(250, self._poll_fs_queue)

    def _fs_remove(self, path: str) -> None:
        if self._scanner is None:
            return
        data   = self._scanner.data
        parent = os.path.dirname(path)
        if parent not in data:
            return
        total, children = data[parent]
        size = next((c[3] for c in children if c[2] == path), None)
        if size is None:
            return

        # Grab tree node before pruning removes it from path_to_node
        node_id = self._path_to_node.get(path)

        # Update parent's children list (total updated by _propagate_delta)
        new_children = sorted([c for c in children if c[2] != path],
                               key=lambda x: x[3], reverse=True)
        data[parent] = (total, new_children)

        self._prune_data(path)
        self._propagate_delta(parent, -size)
        self._refresh_children_fracs(parent)

        if node_id and self._tree.exists(node_id):
            self._tree.delete(node_id)

    def _fs_add_bg(self, path: str) -> None:
        """Compute new item size off the main thread, post result to queue."""
        try:
            if os.path.isfile(path):
                sz, kind = os.path.getsize(path), "file"
            elif os.path.isdir(path):
                sz, kind = self._dir_size(path), "dir"
            else:
                return
        except OSError:
            return
        self._fs_queue.put(("_add", path, kind, sz))

    def _fs_add_apply(self, path: str, kind: str, sz: int) -> None:
        if self._scanner is None:
            return
        data   = self._scanner.data
        parent = os.path.dirname(path)
        name   = os.path.basename(path)

        if parent not in data:
            return
        total, children = data[parent]
        if any(c[2] == path for c in children):
            return   # already present

        new_children = sorted(children + [(kind, name, path, sz)],
                               key=lambda x: x[3], reverse=True)
        data[parent] = (total, new_children)
        if kind == "dir":
            data[path] = (sz, [])
        self._propagate_delta(parent, sz)
        self._refresh_children_fracs(parent)

        # Refresh the visible tree if the parent node is expanded
        parent_node = self._path_to_node.get(parent)
        if parent_node and self._tree.item(parent_node, "open"):
            self._refresh_node_children(parent_node, parent)
        elif parent_node:
            # Collapsed — ensure expand triangle exists
            if not self._tree.get_children(parent_node):
                self._tree.insert(parent_node, "end", text="",
                                  tags=(_PLACEHOLDER_TAG,))
        self._schedule_redraw()

    def _fs_modify(self, path: str) -> None:
        if self._scanner is None or not os.path.isfile(path):
            return
        data   = self._scanner.data
        parent = os.path.dirname(path)
        if parent not in data:
            return
        try:
            new_sz = os.path.getsize(path)
        except OSError:
            return
        total, children = data[parent]
        old_sz = next((c[3] for c in children if c[2] == path), None)
        if old_sz is None:
            return
        delta = new_sz - old_sz
        new_children = sorted(
            [(k, n, p, new_sz if p == path else s) for k, n, p, s in children],
            key=lambda x: x[3], reverse=True)
        data[parent] = (total, new_children)
        self._propagate_delta(parent, delta)
        self._refresh_children_fracs(parent)

        # Update the tree label for this file
        node_id = self._path_to_node.get(path)
        if node_id and self._tree.exists(node_id):
            vals       = list(self._tree.item(node_id, "values"))
            vals[0]    = fmt_size(new_sz)
            vals[1]    = new_sz
            self._tree.item(node_id, values=tuple(vals))

    def _fs_rename(self, old_path: str, new_path: str) -> None:
        if self._scanner is None:
            return
        data   = self._scanner.data
        parent = os.path.dirname(old_path)
        if parent not in data:
            return
        total, children = data[parent]
        new_name     = os.path.basename(new_path)
        new_children = []
        for entry in children:
            if entry[2] == old_path:
                new_children.append((entry[0], new_name, new_path, entry[3]))
                if old_path in data:
                    data[new_path] = data.pop(old_path)
                node_id = self._path_to_node.pop(old_path, None)
                if node_id:
                    self._path_to_node[new_path] = node_id
                    self._node_to_path[node_id]  = new_path
                    if self._tree.exists(node_id):
                        prefix = "  " if entry[0] == "dir" else "      "
                        self._tree.item(node_id, text=f"{prefix}{new_name}")
            else:
                new_children.append(entry)
        data[parent] = (total, new_children)

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _propagate_delta(self, start: str, delta: int) -> None:
        """Apply `delta` bytes to `start` and all ancestor totals."""
        if self._scanner is None:
            return
        data    = self._scanner.data
        root    = self._watched_root.lower()
        current = start

        while True:
            if current in data:
                total, children = data[current]
                data[current] = (total + delta, children)
                self._refresh_label(current)

            parent = os.path.dirname(current)
            if parent == current or not current.lower().startswith(root):
                break
            if current.lower() == root:
                break

            if parent in data:
                ptotal, pchildren = data[parent]
                new_pc = [(k, n, p, s + delta if p == current else s)
                          for k, n, p, s in pchildren]
                new_pc.sort(key=lambda x: x[3], reverse=True)
                data[parent] = (ptotal, new_pc)

            current = parent

    def _prune_data(self, path: str) -> None:
        """Remove path and all descendants from scan_data and path maps."""
        if self._scanner is None:
            return
        data   = self._scanner.data
        prefix = path.lower() + os.sep
        exact  = path.lower()

        for k in [k for k in data if k.lower() == exact or k.lower().startswith(prefix)]:
            del data[k]
        for p in [p for p in self._path_to_node
                  if p.lower() == exact or p.lower().startswith(prefix)]:
            nid = self._path_to_node.pop(p)
            self._node_to_path.pop(nid, None)

    def _refresh_label(self, path: str) -> None:
        """Refresh the size text for the tree node at path."""
        if self._scanner is None:
            return
        data = self._scanner.data
        if path not in data:
            return
        total = data[path][0]
        nid   = self._path_to_node.get(path)
        if nid and self._tree.exists(nid):
            vals    = list(self._tree.item(nid, "values"))
            vals[0] = fmt_size(total)
            vals[1] = total
            self._tree.item(nid, values=tuple(vals))

    def _refresh_children_fracs(self, parent_path: str) -> None:
        """Recalculate bar fractions for all children of parent_path."""
        if self._scanner is None:
            return
        data = self._scanner.data
        if parent_path not in data:
            return
        _, children = data[parent_path]
        max_sz = children[0][3] if children else 1
        for _, _, cpath, csize in children:
            nid = self._path_to_node.get(cpath)
            if nid and self._tree.exists(nid):
                frac    = csize / max_sz if max_sz > 0 else 0.0
                vals    = list(self._tree.item(nid, "values"))
                vals[3] = frac
                self._tree.item(nid, values=tuple(vals))

    def _refresh_node_children(self, parent_node: str, parent_path: str) -> None:
        """Re-populate an expanded node's children from scan_data."""
        for child_id in list(self._tree.get_children(parent_node)):
            cpath = self._node_to_path.pop(child_id, None)
            if cpath:
                self._path_to_node.pop(cpath, None)
            self._tree.delete(child_id)
        self._fill_node(parent_node, parent_path)

    @staticmethod
    def _dir_size(path: str) -> int:
        total = 0
        try:
            for e in os.scandir(path):
                try:
                    if e.is_symlink():
                        continue
                    total += e.stat().st_size if e.is_file() else DiskSizer._dir_size(e.path)
                except OSError:
                    pass
        except OSError:
            pass
        return total

    # ── Delete ────────────────────────────────────────────────────────────────

    def _delete_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        path = self._node_to_path.get(sel[0])
        if not path:
            return
        kind = "folder" if os.path.isdir(path) else "file"
        name = os.path.basename(path)
        if not messagebox.askyesno(
            "Confirm delete",
            f"Permanently delete {kind} '{name}'?\n\nThis cannot be undone.",
            icon="warning",
        ):
            return
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as exc:
            messagebox.showerror("Delete failed", str(exc))

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
        os.startfile(path if os.path.isdir(path) else os.path.dirname(path))

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
