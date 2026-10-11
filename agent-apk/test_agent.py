#!/opt/homebrew/bin/python3
"""End-to-end check of the droidrt device agent against a running VM.

It generates a fresh connection key, delivers it to the agent with the
SET_KEY broadcast, forwards a host port to the agent socket and performs the
mutual authentication handshake. It then checks that unauthenticated or
badly authenticated connections are dropped, opens WhatsApp on its welcome
screen and exercises every command of docs/agent-protocol.md. It never
accepts the WhatsApp terms: the only click goes to the language picker,
which is closed again with a global back.

Usage: ./test_agent.py [--serial emulator-5580] [--port 7912] [--no-forward]

The agent keeps the last key delivered, so after a run the host must deliver
its own key again before it can connect.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import threading
import time

WHATSAPP = "com.whatsapp"
NODE_KEYS = {"id", "cls", "pkg", "text", "desc", "rid", "bounds", "displayId", "clickable",
             "editable", "scrollable", "checked", "enabled", "focused", "selected", "password"}
NODE_ID = re.compile(r"^w-?\d+:0(\.\d+)*$")
SET_KEY = ["shell", "am", "broadcast", "-a", "dev.droidrt.agent.SET_KEY",
           "-n", "dev.droidrt.agent/.KeyReceiver", "--es", "key"]
HANDSHAKE_TIMEOUT_S = 10
MAX_PENDING_HANDSHAKES = 8

failures = []


def adb_path():
    sdk = os.environ.get("DROIDRT_SDK", "/opt/homebrew/share/android-commandlinetools")
    candidate = os.path.join(sdk, "platform-tools", "adb")
    return candidate if os.path.exists(candidate) else (shutil.which("adb") or "adb")


def mac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


class AuthError(Exception):
    pass


class Agent:
    """Client side of the agent protocol, including the mutual handshake."""

    def __init__(self, port, key, host="127.0.0.1"):
        self.sock = socket.create_connection((host, port), timeout=30)
        self.file = self.sock.makefile("rwb")
        self.next_id = 0
        try:
            self.handshake(key)
        except Exception:
            self.close()
            raise

    def handshake(self, key):
        hello = self.read()
        if hello.get("hello") != "droidrt-agent" or hello.get("v") != 2:
            raise AuthError(f"unexpected hello: {hello}")
        client_nonce = secrets.token_hex(16)
        self.send_raw(json.dumps({"auth": mac(key, "client:" + hello["nonce"]),
                                  "nonce": client_nonce}))
        resp = self.read()
        if resp.get("ok") is not True:
            raise AuthError(resp.get("error", "handshake refused"))
        if not hmac.compare_digest(str(resp.get("proof", "")), mac(key, "agent:" + client_nonce)):
            raise AuthError("agent proof mismatch")

    def send_raw(self, line):
        self.file.write(line.encode() + b"\n")
        self.file.flush()

    def read(self):
        line = self.file.readline()
        if not line:
            raise ConnectionError("agent closed the connection")
        return json.loads(line)

    def call(self, cmd, **args):
        self.next_id += 1
        self.send_raw(json.dumps({"id": self.next_id, "cmd": cmd, "args": args}))
        resp = self.read()
        if resp.get("id") != self.next_id:
            raise AssertionError(f"response id {resp.get('id')} != {self.next_id}")
        return resp

    def close(self):
        self.file.close()
        self.sock.close()


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f": {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def short(value, limit=160):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def walk(node):
    yield node
    for child in node.get("children", []):
        yield from walk(child)


def wait_for(agent, present=True, timeout=6.0, **selector):
    """Polls find until a WhatsApp node matching selector appears (or goes away)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = agent.call("find", package=WHATSAPP, **selector)
        if r["ok"] and bool(r["result"]) == present:
            return True
        time.sleep(0.25)
    return False


def raw_connection(port):
    """Opens a plain connection and returns (socket, file, hello line)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=HANDSHAKE_TIMEOUT_S + 5)
    f = sock.makefile("rwb")
    return sock, f, json.loads(f.readline())


def rest_of_stream(f):
    """Reads until the agent closes the connection; returns what it sent."""
    data = b""
    try:
        while True:
            chunk = f.read1(65536)
            if not chunk:
                return data
            data += chunk
    except (ConnectionResetError, BrokenPipeError):
        return data


def deliver_key(adb, key_hex):
    out = subprocess.run(adb + SET_KEY + [key_hex], capture_output=True, text=True).stdout
    return "result=1" in out, out.strip().splitlines()[-1] if out.strip() else ""


def fake_agent(proof):
    """Local server that speaks the agent hello and answers any auth with `proof`."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def run():
        conn, _ = server.accept()
        with conn, conn.makefile("rwb") as f:
            f.write(json.dumps({"hello": "droidrt-agent", "v": 2,
                                "nonce": secrets.token_hex(16)}).encode() + b"\n")
            f.flush()
            f.readline()
            f.write(json.dumps({"ok": True, "proof": proof}).encode() + b"\n")
            f.flush()
        server.close()

    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


def auth_checks(adb, port, key, key_hex):
    """Handshake, key delivery and rejection paths."""
    # Invalid keys are refused and do not replace the current one.
    ok, line = deliver_key(adb, "zz")
    check("SET_KEY rejects non-hex key", not ok and "result=0" in line, line)
    ok, line = deliver_key(adb, key_hex[:-2])
    check("SET_KEY rejects short key", not ok and "result=0" in line, line)

    # A correct handshake ends with a verified proof.
    try:
        a = Agent(port, key)
        r = a.call("ping")
        a.close()
        check("handshake and proof", r["ok"] and r["result"]["version"] == "2", short(r))
    except Exception as e:
        check("handshake and proof", False, repr(e))

    # A request sent without authenticating is never answered.
    sock, f, hello = raw_connection(port)
    check("hello line", hello.get("hello") == "droidrt-agent" and hello.get("v") == 2
          and re.fullmatch(r"[0-9a-f]{32}", hello.get("nonce", "")) is not None, short(hello))
    f.write(b'{"id":1,"cmd":"ping","args":{}}\n')
    f.flush()
    data = rest_of_stream(f)
    sock.close()
    check("request without auth is closed unanswered", data == b"", repr(data[:80]))

    # A browser style POST: the HTTP lines close the connection before the
    # body (a valid request) is read.
    body = b'{"id":1,"cmd":"global","args":{"action":"notifications"}}\n'
    sock, f, _ = raw_connection(port)
    f.write(b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    f.flush()
    data = rest_of_stream(f)
    sock.close()
    check("HTTP POST is closed without executing", data == b"", repr(data[:80]))

    # Wrong HMAC.
    sock, f, hello = raw_connection(port)
    f.write(json.dumps({"auth": mac(secrets.token_bytes(32), "client:" + hello["nonce"]),
                        "nonce": secrets.token_hex(16)}).encode() + b"\n")
    f.flush()
    data = rest_of_stream(f)
    sock.close()
    lines = data.splitlines()
    ok = len(lines) == 1 and json.loads(lines[0]) == {"ok": False, "error": "auth failed"}
    check("wrong HMAC gets 'auth failed' and is closed", ok, repr(data[:80]))

    # Replaying an old auth line against a new nonce fails the same way.
    sock, f, hello = raw_connection(port)
    stale = mac(key, "client:" + secrets.token_hex(16))
    f.write(json.dumps({"auth": stale, "nonce": secrets.token_hex(16)}).encode() + b"\n")
    f.flush()
    data = rest_of_stream(f)
    sock.close()
    check("auth for another nonce is refused", b"auth failed" in data, repr(data[:80]))

    # Silence: the handshake times out.
    sock, f, _ = raw_connection(port)
    start = time.monotonic()
    data = rest_of_stream(f)
    elapsed = time.monotonic() - start
    sock.close()
    check("silent client is closed by the handshake timeout",
          data == b"" and HANDSHAKE_TIMEOUT_S - 1 <= elapsed <= HANDSHAKE_TIMEOUT_S + 4,
          f"{elapsed:.1f}s")

    # A client dribbling one byte per second cannot stretch the handshake:
    # the deadline counts from the accept, not from the last byte.
    sock, f, _ = raw_connection(port)
    start = time.monotonic()
    closed = False
    while time.monotonic() - start < HANDSHAKE_TIMEOUT_S + 10:
        try:
            sock.sendall(b"{" if not closed and time.monotonic() - start < 0.5 else b" ")
        except OSError:
            closed = True
            break
        readable, _, _ = select.select([sock], [], [], 1.0)
        if readable:
            try:
                closed = sock.recv(4096) == b""
            except OSError:
                closed = True
            if closed:
                break
    elapsed = time.monotonic() - start
    sock.close()
    check("dribbling client is closed by the handshake deadline",
          closed and elapsed <= HANDSHAKE_TIMEOUT_S + 4, f"{elapsed:.1f}s")

    # An oversized first line is cut off long before the 1 MiB request limit.
    sock, f, _ = raw_connection(port)
    try:
        f.write(b"{" + b"a" * 8192)
        f.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    start = time.monotonic()
    data = rest_of_stream(f)
    elapsed = time.monotonic() - start
    sock.close()
    check("oversized auth line closes the connection at once",
          data == b"" and elapsed < HANDSHAKE_TIMEOUT_S - 2, f"{elapsed:.1f}s")

    # After auth, a request line over 1 MiB closes the connection.
    try:
        a = Agent(port, key)
        try:
            a.file.write(b"{" + b"a" * (1024 * 1024 + 16))
            a.file.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        data = rest_of_stream(a.file)
        a.close()
        check("request line over 1 MiB closes the connection", data == b"", repr(data[:80]))
    except Exception as e:
        check("request line over 1 MiB closes the connection", False, repr(e))

    # Unauthenticated connections are capped; the next one is dropped before
    # the hello, and slots come back once the idle sockets go away.
    idle = []
    try:
        for _ in range(MAX_PENDING_HANDSHAKES):
            idle.append(raw_connection(port))
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            extra = sock.recv(4096)
        except (ConnectionResetError, socket.timeout) as e:
            extra = repr(e).encode()
        sock.close()
        check("connections over the pending handshake cap are dropped", extra == b"", repr(extra[:80]))
    finally:
        for s, f, _ in idle:
            f.close()
            s.close()
    deadline = time.monotonic() + 5
    recovered = False
    while not recovered and time.monotonic() < deadline:
        try:
            Agent(port, key).close()
            recovered = True
        except (ConnectionError, ValueError, OSError, AuthError):
            time.sleep(0.2)
    check("handshake slots are released when idle clients leave", recovered)

    # The client detects an agent that answers with a wrong proof.
    try:
        Agent(fake_agent(secrets.token_hex(32)), key).close()
        check("client detects wrong proof", False, "handshake accepted a bad proof")
    except AuthError as e:
        check("client detects wrong proof", "proof" in str(e), str(e))

    # Rotation: a new key replaces the old one.
    new_hex = secrets.token_hex(32)
    ok, line = deliver_key(adb, new_hex)
    check("SET_KEY rotation", ok, line)
    try:
        Agent(port, key).close()
        check("old key refused after rotation", False, "old key still accepted")
    except AuthError as e:
        check("old key refused after rotation", str(e) == "auth failed", str(e))
    try:
        Agent(port, bytes.fromhex(new_hex)).close()
        check("new key accepted after rotation", True)
    except Exception as e:
        check("new key accepted after rotation", False, repr(e))
    return new_hex


def center(bounds):
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="emulator-5580")
    ap.add_argument("--port", type=int, default=7912)
    ap.add_argument("--no-forward", action="store_true")
    opts = ap.parse_args()
    adb = [adb_path(), "-s", opts.serial]

    if not opts.no_forward:
        subprocess.run(adb + ["forward", f"tcp:{opts.port}", "localabstract:droidrt_agent"],
                       check=True, capture_output=True)

    key_hex = secrets.token_hex(32)
    ok, line = deliver_key(adb, key_hex)
    check("SET_KEY delivers the key", ok, line)
    if not ok:
        print("cannot continue without a key")
        return 1
    key_hex = auth_checks(adb, opts.port, bytes.fromhex(key_hex), key_hex)
    key = bytes.fromhex(key_hex)
    agent = Agent(opts.port, key)

    # ping
    r = agent.call("ping")
    res = r.get("result") or {}
    check("ping", r["ok"] and res.get("version") == "2" and isinstance(res.get("sdk"), int), short(r))

    # Open WhatsApp and let it settle.
    subprocess.run(adb + ["shell", "monkey", "-p", WHATSAPP, "-c",
                          "android.intent.category.LAUNCHER", "1"], capture_output=True)
    r = agent.call("wait_idle", timeoutMs=8000, idleMs=700)
    check("wait_idle", r["ok"] and isinstance(r["result"].get("idle"), bool), short(r))

    # displays
    r = agent.call("displays")
    displays = r.get("result") or []
    ok = r["ok"] and displays and all(
        {"displayId", "width", "height", "packages"} <= set(d) for d in displays)
    check("displays", bool(ok), short(r, 400))
    wa_display = next((d["displayId"] for d in displays if WHATSAPP in d["packages"]), None)
    check("displays lists com.whatsapp", wa_display is not None, f"displayId={wa_display}")

    # tree of com.whatsapp
    r = agent.call("tree", package=WHATSAPP)
    roots = r.get("result") or []
    nodes = [n for root in roots for n in walk(root)]
    ok = (r["ok"] and roots and all(NODE_KEYS <= set(n) for n in nodes)
          and all(NODE_ID.match(n["id"]) for n in nodes)
          and all(n["pkg"] == WHATSAPP for n in roots)
          and all(n.get("children") != [] for n in nodes)
          and not any(n[k] == "" for n in nodes for k in ("text", "desc", "rid")))
    check("tree package=com.whatsapp", bool(ok), f"{len(roots)} window(s), {len(nodes)} nodes")
    r = agent.call("tree", package=WHATSAPP, maxDepth=1)
    shallow = r.get("result") or []
    ok = r["ok"] and all(all("children" not in c for c in root.get("children", [])) for root in shallow)
    check("tree maxDepth=1", bool(ok))
    if wa_display is not None:
        r = agent.call("tree", displayId=wa_display, maxDepth=0)
        check("tree displayId", r["ok"] and all(n["displayId"] == wa_display for n in r["result"]),
              f"{len(r.get('result') or [])} root(s) on display {wa_display}")

    # find "Agree and continue". WhatsApp renders the button in caps, so the
    # exact match may miss; textContains is case insensitive.
    r = agent.call("find", package=WHATSAPP, text="Agree and continue")
    exact = r.get("result") or []
    check("find text exact (protocol ok)", r["ok"], f"{len(exact)} match(es)")
    r = agent.call("find", package=WHATSAPP, textContains="agree and continue")
    found = [n for n in (r.get("result") or []) if n["cls"].endswith("Button")] or exact
    check("find textContains 'agree and continue'", r["ok"] and bool(found),
          short(found[0] if found else r))
    r = agent.call("find", package=WHATSAPP, resourceId="eula_accept")
    check("find resourceId (short form)", r["ok"] and len(r["result"]) == 1, short(r.get("result")))
    accept = found[0] if found else None

    # action focus on the accept button, never click.
    if accept:
        r = agent.call("action", nodeId=accept["id"], action="focus")
        check("action focus on 'Agree and continue'", r["ok"] and "performed" in r["result"], short(r))

    # stale and invalid node ids
    r = agent.call("action", nodeId="w987654:0", action="focus")
    check("stale node (unknown window)", not r["ok"] and r["error"] == "stale node", short(r))
    if roots:
        r = agent.call("action", nodeId=roots[0]["id"] + ".999", action="focus")
        check("stale node (bad path)", not r["ok"] and r["error"] == "stale node", short(r))
    r = agent.call("action", nodeId="garbage", action="click")
    check("invalid nodeId", not r["ok"], short(r))

    # click the language picker, then close it with a global back.
    r = agent.call("find", package=WHATSAPP, resourceId="com.whatsapp:id/language_picker")
    picker = (r.get("result") or [None])[0]
    if picker:
        r = agent.call("action", nodeId=picker["id"], action="click")
        check("action click language picker", r["ok"] and r["result"]["performed"] is True, short(r))
        opened = wait_for(agent, resourceId="com.whatsapp:id/closeButton")
        check("language sheet opened", opened)
        time.sleep(0.5)  # let the sheet take input focus before pressing back
        r = agent.call("global", action="back")
        check("global back", r["ok"] and r["result"]["performed"] is True, short(r))
        closed = wait_for(agent, resourceId="com.whatsapp:id/closeButton", present=False)
        check("back closed the language sheet", closed)
    else:
        check("language picker present", False, "skipped click and back")
        r = agent.call("global", action="back")
        check("global back", r["ok"] and r["result"]["performed"] is True, short(r))

    # tap and swipe on the welcome title, which is not interactive.
    wait_for(agent, resourceId="com.whatsapp:id/title")
    r = agent.call("find", package=WHATSAPP, resourceId="com.whatsapp:id/title")
    title = (r.get("result") or [None])[0]
    if title:
        x, y = center(title["bounds"])
        r = agent.call("tap", displayId=title["displayId"], x=x, y=y)
        check("tap", r["ok"] and r["result"]["performed"] is True, short(r))
        r = agent.call("swipe", displayId=title["displayId"], x1=x - 100, y1=y, x2=x + 100, y2=y,
                       durationMs=200)
        check("swipe", r["ok"] and r["result"]["performed"] is True, short(r))
    else:
        check("welcome title present", False, "skipped tap and swipe")

    # notifications
    r = agent.call("notifications")
    notes = r.get("result") or []
    keys = {"key", "package", "postTime", "title", "text", "subText", "actions"}
    check("notifications", r["ok"] and all(keys <= set(n) for n in notes), f"{len(notes)} item(s)")
    r = agent.call("notifications", package="no.such.package")
    check("notifications package filter", r["ok"] and r["result"] == [])
    r = agent.call("notifications", sinceMs=int(time.time() * 1000) + 3_600_000)
    check("notifications sinceMs filter", r["ok"] and r["result"] == [])
    r = agent.call("notification_action", key="0|no.such|1|null|0", index=0)
    check("notification_action unknown key", not r["ok"], short(r))
    with_actions = next((n for n in notes if n["actions"]), None)
    if with_actions is None and notes:
        r = agent.call("notification_action", key=notes[0]["key"], index=99)
        check("notification_action bad index", not r["ok"], short(r))

    # wait_idle with a timeout shorter than the idle window
    r = agent.call("wait_idle", timeoutMs=0, idleMs=600000)
    check("wait_idle timeout", r["ok"] and r["result"]["idle"] is False, short(r))

    # Error handling.
    r = agent.call("nope")
    check("unknown command", not r["ok"] and "unknown command" in r["error"], short(r))
    agent.send_raw("{not json")
    r = agent.read()
    check("invalid JSON", not r["ok"] and r["id"] is None, short(r))

    # Pipelining: three requests in one write, answered in order.
    batch = [{"id": 1000 + i, "cmd": c, "args": {}} for i, c in enumerate(["ping", "displays", "ping"])]
    agent.send_raw("\n".join(json.dumps(b) for b in batch))
    ids = [agent.read()["id"] for _ in batch]
    check("pipelining keeps order", ids == [1000, 1001, 1002], str(ids))

    # Several connections at once.
    other = Agent(opts.port, key)
    r1, r2 = other.call("ping"), agent.call("ping")
    other.close()
    check("multiple connections", r1["ok"] and r2["ok"])

    agent.close()
    print()
    print("all checks passed" if not failures else f"{len(failures)} failure(s): {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
