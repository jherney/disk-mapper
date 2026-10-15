#!/usr/bin/env python3
"""diskmap — see what's using your disk space.

Scans a folder and shows the result two ways:
  * a tree (left) sorted biggest-first, with size, share of parent and file count
  * a treemap (right) where each box's area is proportional to its size

Sizes are actual space used on disk (allocated blocks), hard links are only
counted once, and virtual / network / snap filesystems are skipped so scanning
`/` gives a meaningful answer.

Usage: diskmap.py [folder]      (default: your home folder)
"""

import colorsys
import os
import re
import stat
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Filesystems that don't take up real disk space (or aren't local disks).
SKIP_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs",
    "pstore", "bpf", "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs",
    "autofs", "binfmt_misc", "efivarfs", "selinuxfs", "rpc_pipefs", "nsfs", "overlay",
    "squashfs", "ramfs", "nfs", "nfs4", "cifs", "smb3", "sshfs",
}

BG, PANEL, FG, MUTED, ACCENT = "#1e1f26", "#2a2c36", "#e6e6eb", "#9a9cab", "#6c8cff"
# Distinct hues for top-level items in the treemap.
HUES = [0.62, 0.08, 0.33, 0.95, 0.52, 0.15, 0.75, 0.42, 0.02, 0.85]

TREE_CHILD_LIMIT = 300   # rows shown per folder in the tree before "… more"
MAP_DEPTH = 3            # folder levels drawn inside the treemap
MIN_BOX = 4              # don't draw boxes smaller than this (px)


# ---------------------------------------------------------------- data

class Node:
    __slots__ = ("name", "parent", "size", "is_dir", "children", "files", "errors")

    def __init__(self, name, parent, is_dir, size=0):
        self.name, self.parent, self.is_dir, self.size = name, parent, is_dir, size
        self.children = [] if is_dir else None
        self.files = 0 if is_dir else 1
        self.errors = 0

    @property
    def path(self):
        parts, n = [], self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return os.path.join(n.name, *reversed(parts))

    def ancestors(self):
        n, out = self.parent, []
        while n is not None:
            out.append(n)
            n = n.parent
        return out


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def skipped_mounts(root):
    """Mount points under `root` whose filesystem type we don't want to count."""
    out = set()
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                _dev, mnt, fstype, *_ = line.split()
                # /proc/mounts escapes spaces etc. as octal (\040)
                mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), mnt)
                if (fstype in SKIP_FSTYPES or fstype.startswith("fuse.")) and mnt != root:
                    out.add(mnt)
    except OSError:
        pass
    return out


class Scanner(threading.Thread):
    def __init__(self, root_path):
        super().__init__(daemon=True)
        self.root_path = os.path.abspath(root_path)
        self.cancelled = False
        self.files = 0
        self.bytes = 0
        self.current = ""
        self.result = None
        self.elapsed = 0

    def run(self):
        start = time.monotonic()
        skip = skipped_mounts(self.root_path)
        seen_inodes = set()
        root = Node(self.root_path, None, True)
        dirs = [root]          # every directory, parents always before their children
        stack = [(root, self.root_path)]
        while stack and not self.cancelled:
            node, path = stack.pop()
            self.current = path
            try:
                it = os.scandir(path)
            except OSError:
                node.errors += 1
                continue
            # Reading the listing itself can fail part-way (folder deleted mid-scan, I/O error);
            # keep what was read so the scan always finishes.
            try:
                with it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            node.errors += 1
                            continue
                        if st.st_nlink > 1 and not stat.S_ISDIR(st.st_mode):
                            key = (st.st_dev, st.st_ino)
                            if key in seen_inodes:
                                continue
                            seen_inodes.add(key)
                        used = st.st_blocks * 512
                        if stat.S_ISDIR(st.st_mode):
                            if entry.path in skip:
                                continue
                            child = Node(entry.name, node, True, used)
                            node.children.append(child)
                            dirs.append(child)
                            stack.append((child, entry.path))
                        else:
                            node.children.append(Node(entry.name, node, False, used))
                            node.size += used
                            node.files += 1
                            self.files += 1
                        self.bytes += used
            except OSError:
                node.errors += 1
        # Roll sizes up from the deepest folders to the root, then sort biggest-first.
        for d in reversed(dirs):
            d.children.sort(key=lambda n: n.size, reverse=True)
            if d.parent is not None:
                d.parent.size += d.size
                d.parent.files += d.files
                d.parent.errors += d.errors
        self.elapsed = time.monotonic() - start
        self.result = root


# ---------------------------------------------------------------- treemap layout

def squarify(sizes, x, y, w, h):
    """Squarified treemap (Bruls, Huizing & van Wijk). `sizes` must be sorted descending."""
    rects = []
    total = sum(sizes)
    if total <= 0 or w <= 0 or h <= 0:
        return rects
    scale = w * h / total
    areas = [s * scale for s in sizes]

    def worst(row, side):
        s = sum(row)
        return max(max(row) * side * side / (s * s), s * s / (side * side * min(row)))

    i = 0
    while i < len(areas):
        side = min(w, h)
        row = [areas[i]]
        i += 1
        while i < len(areas) and worst(row + [areas[i]], side) <= worst(row, side):
            row.append(areas[i])
            i += 1
        s = sum(row)
        if w >= h:
            rw = s / h
            yy = y
            for a in row:
                rects.append((x, yy, rw, a / rw))
                yy += a / rw
            x, w = x + rw, w - rw
        else:
            rh = s / w
            xx = x
            for a in row:
                rects.append((xx, y, a / rh, rh))
                xx += a / rh
            y, h = y + rh, h - rh
    return rects


def shade(hue, depth, is_dir):
    light = 0.30 + 0.09 * depth + (0 if is_dir else 0.08)
    sat = 0.45 if is_dir else 0.55
    r, g, b = colorsys.hls_to_rgb(hue, min(light, 0.75), sat)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


# ---------------------------------------------------------------- GUI

class App:
    def __init__(self, start_path):
        self.root = tk.Tk(className="diskmap")
        self.root.title("diskmap")
        self.root.geometry("1200x750")
        self.root.minsize(800, 500)
        self.style()
        self.data = None          # root Node of the scan
        self.view = None          # Node currently shown in the treemap
        self.history = []
        self.iid_node = {}        # treeview iid -> Node
        self.node_iid = {}        # id(Node) -> treeview iid
        self.boxes = []           # (x0, y0, x1, y1, node) drawn on the canvas, shallow -> deep
        self.scanner = None
        self.redraw_job = None
        self.build()
        self.scan(start_path)

    def style(self):
        self.root.configure(bg=BG)
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor=PANEL,
                    font=("Sans", 10))
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Big.TLabel", font=("Sans", 13, "bold"))
        s.configure("TButton", background=PANEL, foreground=FG, padding=(10, 5), borderwidth=0)
        s.map("TButton", background=[("active", "#3a3d4a"), ("disabled", BG)],
              foreground=[("disabled", "#55586a")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        s.map("Accent.TButton", background=[("active", "#5a7af0")])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=24,
                    borderwidth=0)
        s.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        s.configure("Treeview.Heading", background=BG, foreground=MUTED, borderwidth=0,
                    font=("Sans", 9, "bold"))
        s.map("Treeview.Heading", background=[("active", BG)])
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, borderwidth=0)

    def build(self):
        bar = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Open folder…", style="Accent.TButton", command=self.choose).pack(side="left")
        ttk.Button(bar, text="Home", command=lambda: self.scan(os.path.expanduser("~"))).pack(side="left", padx=4)
        ttk.Button(bar, text="Whole disk ( / )", command=lambda: self.scan("/")).pack(side="left")
        self.rescan_btn = ttk.Button(bar, text="↻ Rescan", command=lambda: self.scan(self.data.name))
        self.rescan_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel)
        self.up_btn = ttk.Button(bar, text="↑ Up", command=self.go_up)
        self.up_btn.pack(side="right")
        self.back_btn = ttk.Button(bar, text="← Back", command=self.go_back)
        self.back_btn.pack(side="right", padx=4)

        head = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        head.pack(fill="x")
        self.title_lbl = ttk.Label(head, text="", style="Big.TLabel")
        self.title_lbl.pack(side="left")
        self.summary_lbl = ttk.Label(head, text="", style="Muted.TLabel")
        self.summary_lbl.pack(side="right")

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)

        left = ttk.Frame(body)
        self.tree = ttk.Treeview(left, columns=("size", "pct", "files"), selectmode="browse")
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("pct", text="Share of parent", anchor="w")
        self.tree.heading("files", text="Files", anchor="e")
        self.tree.column("#0", width=230, anchor="w")
        self.tree.column("size", width=80, anchor="e", stretch=False)
        self.tree.column("pct", width=130, anchor="w", stretch=False)
        self.tree.column("files", width=70, anchor="e", stretch=False)
        self.tree.tag_configure("more", foreground=MUTED)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        body.add(left, weight=2)

        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        body.add(self.canvas, weight=3)

        foot = ttk.Frame(self.root, padding=(10, 6))
        foot.pack(fill="x")
        self.progress = ttk.Progressbar(foot, mode="indeterminate", length=160)
        self.status_lbl = ttk.Label(foot, text="", style="Muted.TLabel")
        self.status_lbl.pack(side="left", fill="x", expand=True)

        self.tree.bind("<<TreeviewOpen>>", self.on_tree_open)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Double-1>", self.on_tree_double)
        self.tree.bind("<Button-3>", self.on_tree_right)
        self.canvas.bind("<Configure>", lambda e: self.schedule_redraw())
        self.canvas.bind("<Motion>", self.on_map_motion)
        self.canvas.bind("<Leave>", lambda e: self.status(""))
        self.canvas.bind("<Button-1>", self.on_map_click)
        self.canvas.bind("<Button-3>", self.on_map_right)
        self.root.bind("<BackSpace>", lambda e: self.go_up())
        self.root.bind("<Alt-Left>", lambda e: self.go_back())
        self.root.bind("<Alt-Up>", lambda e: self.go_up())
        self.root.bind("<F5>", lambda e: self.data and self.scan(self.data.name))
        self.update_nav()

    def status(self, text):
        self.status_lbl.configure(text=text)

    # ---------------------------------------------------------- scanning

    def choose(self):
        path = filedialog.askdirectory(parent=self.root, title="Folder to scan",
                                       initialdir=self.data.name if self.data else os.path.expanduser("~"))
        if path:
            self.scan(path)

    def scan(self, path):
        if self.scanner and self.scanner.is_alive():
            self.scanner.cancelled = True
        self.scanner = Scanner(path)
        self.scanner.start()
        self.rescan_btn.pack_forget()
        self.cancel_btn.pack(side="left", padx=4)
        self.progress.pack(side="right")
        self.progress.start(12)
        self.title_lbl.configure(text=f"Scanning {self.scanner.root_path} …")
        self.poll(self.scanner)

    def cancel(self):
        if self.scanner:
            self.scanner.cancelled = True

    def poll(self, sc):
        if sc is not self.scanner:
            return  # superseded by a newer scan
        if sc.result is None:
            self.status(f"{sc.files:,} files · {human(sc.bytes)} · {sc.current}")
            self.root.after(100, self.poll, sc)
            return
        self.progress.stop()
        self.progress.pack_forget()
        self.cancel_btn.pack_forget()
        self.rescan_btn.pack(side="left", padx=4)
        self.data = sc.result
        self.history = []
        self.load_tree()
        self.show(self.data, remember=False)
        note = " (cancelled — partial results)" if sc.cancelled else ""
        errs = f" · {self.data.errors:,} items unreadable (permission denied)" if self.data.errors else ""
        self.status(f"Scanned {self.data.files:,} files in {sc.elapsed:.1f}s{note}{errs}")

    # ---------------------------------------------------------- tree

    def load_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.iid_node.clear()
        self.node_iid.clear()
        iid = self.insert_row("", self.data, self.data.size)
        self.populate(iid)
        self.tree.item(iid, open=True)

    def insert_row(self, parent_iid, node, parent_size):
        pct = node.size / parent_size * 100 if parent_size else 0
        bar = "█" * round(pct / 10) + "░" * (10 - round(pct / 10))
        name = node.name if node.parent is None else node.name + ("/" if node.is_dir else "")
        iid = self.tree.insert(parent_iid, "end", text=("📁 " if node.is_dir else "    ") + name,
                               values=(human(node.size), f"{bar} {pct:4.1f}%",
                                       f"{node.files:,}" if node.is_dir else ""))
        self.iid_node[iid] = node
        self.node_iid[id(node)] = iid
        if node.is_dir and node.children:
            self.tree.insert(iid, "end", text="…", tags=("placeholder",))
        return iid

    def populate(self, iid):
        node = self.iid_node.get(iid)
        kids = self.tree.get_children(iid)
        if not node or not kids or "placeholder" not in self.tree.item(kids[0], "tags"):
            return
        self.tree.delete(*kids)
        for child in node.children[:TREE_CHILD_LIMIT]:
            self.insert_row(iid, child, node.size)
        rest = node.children[TREE_CHILD_LIMIT:]
        if rest:
            self.tree.insert(iid, "end", text=f"    … {len(rest):,} more smaller items",
                             values=(human(sum(c.size for c in rest)), "", ""), tags=("more",))

    def on_tree_open(self, _e):
        self.populate(self.tree.focus())

    def reveal(self, node):
        """Expand the tree down to `node` and select it."""
        for anc in reversed(node.ancestors()):
            iid = self.node_iid.get(id(anc))
            if iid:
                self.populate(iid)
                self.tree.item(iid, open=True)
        iid = self.node_iid.get(id(node))
        if iid:
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def on_tree_select(self, _e):
        sel = self.tree.selection()
        node = self.iid_node.get(sel[0]) if sel else None
        # show() selects the viewed folder itself; don't let that wipe the scan/trash message.
        if node and node is not self.view:
            self.status(f"{node.path} — {human(node.size)}")
            self.highlight(node)

    def on_tree_double(self, _e):
        node = self.iid_node.get(self.tree.focus())
        if node and node.is_dir:
            self.show(node)

    def on_tree_right(self, e):
        iid = self.tree.identify_row(e.y)
        node = self.iid_node.get(iid)
        if node:
            self.tree.selection_set(iid)
            self.context_menu(node, e.x_root, e.y_root)

    # ---------------------------------------------------------- navigation

    def show(self, node, remember=True):
        if remember and self.view is not None and node is not self.view:
            self.history.append(self.view)
        self.view = node
        self.title_lbl.configure(text=node.path)
        self.summary_lbl.configure(text=f"{human(node.size)} · {node.files:,} files · "
                                        f"{len(node.children):,} items")
        self.reveal(node)
        self.update_nav()
        self.redraw()

    def go_up(self):
        if self.view and self.view.parent:
            self.show(self.view.parent)

    def go_back(self):
        if self.history:
            self.show(self.history.pop(), remember=False)

    def update_nav(self):
        self.up_btn.state(["!disabled"] if self.view and self.view.parent else ["disabled"])
        self.back_btn.state(["!disabled"] if self.history else ["disabled"])

    # ---------------------------------------------------------- treemap

    def schedule_redraw(self):
        if self.redraw_job:
            self.root.after_cancel(self.redraw_job)
        self.redraw_job = self.root.after(60, self.redraw)

    def redraw(self):
        self.redraw_job = None
        c = self.canvas
        c.delete("all")
        self.boxes = []
        if not self.view:
            return
        w, h = c.winfo_width(), c.winfo_height()
        if not self.view.size:
            c.create_text(w / 2, h / 2, text="Empty", fill=MUTED)
            return
        self.draw_children(self.view, 0, 0, w, h, depth=0, hue=None)

    def draw_children(self, node, x, y, w, h, depth, hue):
        kids = [k for k in node.children if k.size > 0]
        if not kids:
            return
        # Lay out everything (so proportions are right), but only draw boxes big enough to see.
        rects = squarify([k.size for k in kids], x, y, w, h)
        for i, (k, (rx, ry, rw, rh)) in enumerate(zip(kids, rects)):
            if rw < MIN_BOX or rh < MIN_BOX:
                continue
            kh = HUES[i % len(HUES)] if hue is None else hue
            self.draw_box(k, rx, ry, rw, rh, depth, kh)

    def draw_box(self, node, x, y, w, h, depth, hue):
        c = self.canvas
        fill = shade(hue, depth, node.is_dir)
        c.create_rectangle(x, y, x + w, y + h, fill=fill, outline=BG, width=1)
        self.boxes.append((x, y, x + w, y + h, node))
        label = f"{node.name}  {human(node.size)}"
        header = 16
        if node.is_dir and depth < MAP_DEPTH - 1 and w > 40 and h > header + 20:
            if w > 60:
                c.create_text(x + 5, y + 2, text=label, anchor="nw", fill="#ffffff",
                              font=("Sans", 8, "bold"), width=max(w - 10, 1))
            self.draw_children(node, x + 2, y + header, w - 4, h - header - 2, depth + 1, hue)
        elif w > 50 and h > 18:
            c.create_text(x + w / 2, y + h / 2, text=label if h > 34 else node.name,
                          fill="#ffffff", font=("Sans", 8), width=max(w - 8, 1))

    def box_at(self, x, y):
        for x0, y0, x1, y1, node in reversed(self.boxes):  # deepest first
            if x0 <= x < x1 and y0 <= y < y1:
                return node
        return None

    def on_map_motion(self, e):
        node = self.box_at(e.x, e.y)
        if node:
            share = node.size / self.view.size * 100 if self.view.size else 0
            kind = f"folder · {node.files:,} files" if node.is_dir else "file"
            self.status(f"{node.path} — {human(node.size)} ({share:.1f}% of this view) · {kind}")

    def on_map_click(self, e):
        node = self.box_at(e.x, e.y)
        if not node:
            return
        # Zoom into the top-level item under the cursor (or the folder itself).
        while node.parent is not None and node.parent is not self.view:
            node = node.parent
        if node.is_dir:
            self.show(node)
        else:
            self.reveal(node)

    def on_map_right(self, e):
        node = self.box_at(e.x, e.y)
        if node:
            self.context_menu(node, e.x_root, e.y_root)

    def highlight(self, node):
        self.canvas.delete("hl")
        for x0, y0, x1, y1, n in self.boxes:
            if n is node:
                self.canvas.create_rectangle(x0 + 1, y0 + 1, x1 - 1, y1 - 1, outline="#ffffff",
                                             width=3, tags="hl")
                return

    # ---------------------------------------------------------- actions

    def context_menu(self, node, x, y):
        m = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT, activeforeground="#fff")
        if node.is_dir:
            m.add_command(label="Zoom in here", command=lambda: self.show(node))
        m.add_command(label="Open in file manager", command=lambda: self.open_in_fm(node))
        m.add_command(label="Copy path", command=lambda: self.copy_path(node))
        m.add_separator()
        m.add_command(label="Move to Trash…", command=lambda: self.trash(node))
        m.tk_popup(x, y)

    def open_in_fm(self, node):
        target = node.path if node.is_dir else os.path.dirname(node.path)
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def copy_path(self, node):
        self.root.clipboard_clear()
        self.root.clipboard_append(node.path)
        self.status(f"Copied {node.path}")

    def trash(self, node):
        if node.parent is None:
            return messagebox.showinfo("Not allowed", "Can't trash the folder you scanned.", parent=self.root)
        what = f"the folder '{node.name}' and everything in it" if node.is_dir else f"'{node.name}'"
        if not messagebox.askyesno("Move to Trash",
                                   f"Move {what} ({human(node.size)}) to the Trash?\n\n{node.path}\n\n"
                                   "You can restore it from the Trash until you empty it.",
                                   icon="warning", parent=self.root):
            return
        r = subprocess.run(["gio", "trash", "--", node.path], capture_output=True, text=True)
        if r.returncode != 0:
            return messagebox.showerror("Couldn't move to Trash", r.stderr.strip() or "Unknown error",
                                        parent=self.root)
        # Update the in-memory tree instead of rescanning.
        node.parent.children.remove(node)
        for anc in node.ancestors():
            anc.size -= node.size
            anc.files -= node.files
            anc.children.sort(key=lambda n: n.size, reverse=True)
        if self.view is node or node in self.view.ancestors():
            self.view = node.parent
        self.load_tree()
        self.show(self.view, remember=False)
        self.history = [h for h in self.history if node not in h.ancestors() and h is not node]
        self.update_nav()
        self.status(f"Moved {node.path} to the Trash — freed {human(node.size)} "
                    "(once the Trash is emptied).")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~")
    if not os.path.isdir(start):
        sys.exit(f"Not a folder: {start}")
    App(start).run()
