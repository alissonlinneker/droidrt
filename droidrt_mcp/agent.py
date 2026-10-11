"""adb helpers and the client for the on-device agent (dev.droidrt.agent).

Protocol: docs/agent-protocol.md, section 1. The host forwards a free local
TCP port to the abstract socket `droidrt_agent` and exchanges newline-delimited
JSON. The client installs and enables the agent when needed and reconnects
when the connection drops.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("droidrt_mcp.agent")

AGENT_PACKAGE = "dev.droidrt.agent"
ACCESSIBILITY_SERVICE = f"{AGENT_PACKAGE}/.AgentService"
ACCESSIBILITY_SERVICE_LONG = f"{AGENT_PACKAGE}/{AGENT_PACKAGE}.AgentService"
NOTIFY_SERVICE = f"{AGENT_PACKAGE}/{AGENT_PACKAGE}.NotifyService"
SOCKET_NAME = "droidrt_agent"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Commands that can be sent again safely after a dropped connection.
IDEMPOTENT = {"ping", "displays", "tree", "find", "notifications", "wait_idle"}

REPO_ROOT = Path(__file__).resolve().parent.parent
KEY_PATH = Path.home() / "Library" / "Application Support" / "droidrt" / "agent.key"
KEY_RECEIVER = f"{AGENT_PACKAGE}/.KeyReceiver"
SET_KEY_ACTION = f"{AGENT_PACKAGE}.SET_KEY"
HEX_RE = re.compile(r"[0-9a-f]+")
PROTOCOL_VERSION = 2


class DeviceError(Exception):
    """adb or the agent failed."""


class AgentError(DeviceError):
    """The agent answered with ok=false."""


class AgentAuthError(DeviceError):
    """The peer on the socket failed the mutual authentication."""


class AgentOutdatedError(DeviceError):
    """The agent did not greet with the expected hello (old version or still starting)."""


class NoKeyError(DeviceError):
    """The agent has no key yet and needs it delivered."""


def load_or_create_key(path: Path = KEY_PATH) -> str:
    """The shared secret as 64 hex chars, created with mode 0600 when missing."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir leaves an existing directory alone; the key folder is private either way.
    path.parent.chmod(0o700)
    try:
        key = path.read_text().strip()
        if len(key) == 64 and HEX_RE.fullmatch(key):
            if path.stat().st_mode & 0o077:
                path.chmod(0o600)
            return key
        log.warning("ignoring malformed agent key in %s", path)
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    tmp = path.with_name(path.name + ".tmp")
    # A leftover temp file (or a planted symlink) is removed first, and O_EXCL
    # refuses to follow anything that appears in between.
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return key


def sign(key_hex: str, message: str) -> str:
    return hmac.new(bytes.fromhex(key_hex), message.encode("utf-8"), hashlib.sha256).hexdigest()


class Adb:
    """Runs adb with an explicit serial, argv lists and a timeout on every call."""

    def __init__(self, serial: str, adb_path: str | None = None):
        self.serial = serial
        self.adb_path = adb_path or shutil.which("adb") or "adb"

    def run(self, *args: str, timeout: float = 30, check: bool = True, binary: bool = False,
            input_data: bytes | None = None) -> str | bytes:
        argv = [self.adb_path, "-s", self.serial, *args]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout, input=input_data,
                                  stdin=None if input_data is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise DeviceError(f"adb {' '.join(args[:3])} timed out after {timeout:.0f}s") from None
        except OSError as e:
            raise DeviceError(f"cannot run adb: {e}") from None
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            raise DeviceError(f"adb {' '.join(args[:3])} failed: {err[:500]}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    def shell(self, *args: str, timeout: float = 30, check: bool = True) -> str:
        """Run a command on the device. adb joins shell arguments into one string
        that the device shell parses, so every argument is quoted here."""
        return self.run("shell", " ".join(shlex.quote(a) for a in args), timeout=timeout, check=check)  # type: ignore[return-value]

    def exec_out(self, *args: str, timeout: float = 30) -> bytes:
        return self.run("exec-out", " ".join(shlex.quote(a) for a in args), timeout=timeout, binary=True)  # type: ignore[return-value]

    def is_ready(self) -> bool:
        try:
            return self.shell("getprop", "sys.boot_completed", timeout=5, check=False).strip() == "1"
        except DeviceError:
            return False


class AgentClient:
    """Thread-safe client. One request at a time on a persistent connection."""

    def __init__(self, adb: Adb, apk_path: Path | None = None, build_script: Path | None = None,
                 connect_timeout: float = 20, key_path: Path | None = None):
        self.adb = adb
        self.key_path = key_path or KEY_PATH
        self._key: str | None = None
        self.apk_path = apk_path or REPO_ROOT / "agent-apk/build/droidrt-agent.apk"
        self.build_script = build_script or REPO_ROOT / "agent-apk/build.sh"
        self.connect_timeout = connect_timeout
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._file = None
        self._next_id = 0
        self._port: int | None = None
        self._prepared = False
        self._last_used = 0.0
        # Tests can point the client at a fake server and skip adb entirely.
        self.fixed_address: tuple[str, int] | None = None

    # ------------------------------------------------------------ setup

    def ensure_installed(self) -> None:
        out = self.adb.shell("pm", "path", AGENT_PACKAGE, timeout=15, check=False)
        if "package:" in out:
            return
        self._install()
        self.deliver_key()

    def _install(self) -> None:
        if not self.apk_path.exists():
            if not self.build_script.exists():
                raise DeviceError(f"agent APK missing and no build script at {self.build_script}")
            log.info("building the device agent with %s", self.build_script)
            try:
                proc = subprocess.run(["/bin/sh", str(self.build_script)], cwd=str(self.build_script.parent),
                                      capture_output=True, timeout=900, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise DeviceError("building the agent APK timed out") from None
            if proc.returncode != 0 or not self.apk_path.exists():
                tail = (proc.stderr or proc.stdout).decode("utf-8", "replace")[-800:]
                raise DeviceError(f"building the agent APK failed: {tail}")
        log.info("installing %s", self.apk_path)
        out = self.adb.run("install", "-r", "-g", str(self.apk_path), timeout=180)
        if "Success" not in out:
            raise DeviceError(f"installing the agent failed: {str(out).strip()[:500]}")

    def ensure_enabled(self) -> None:
        current = self.adb.shell("settings", "get", "secure", "enabled_accessibility_services",
                                 timeout=10, check=False).strip()
        services = [s for s in current.split(":") if s and s != "null"]
        if ACCESSIBILITY_SERVICE not in services and ACCESSIBILITY_SERVICE_LONG not in services:
            services.append(ACCESSIBILITY_SERVICE)
            self.adb.shell("settings", "put", "secure", "enabled_accessibility_services", ":".join(services),
                           timeout=10)
        if self.adb.shell("settings", "get", "secure", "accessibility_enabled", timeout=10,
                          check=False).strip() != "1":
            self.adb.shell("settings", "put", "secure", "accessibility_enabled", "1", timeout=10)
        self.adb.shell("cmd", "notification", "allow_listener", NOTIFY_SERVICE, timeout=10, check=False)
        self.deliver_key()

    @property
    def key(self) -> str:
        if self._key is None:
            self._key = load_or_create_key(self.key_path)
        return self._key

    def deliver_key(self) -> None:
        """Hand the shared key to the agent through its explicit, permission
        protected broadcast receiver."""
        out = self.adb.shell("am", "broadcast", "-a", SET_KEY_ACTION, "-n", KEY_RECEIVER, "--es", "key", self.key,
                             timeout=15, check=False)
        if "result=1" not in out:
            raise DeviceError(f"the agent did not accept the key: {out.strip()[-300:]}")

    def _forward(self) -> int:
        out = str(self.adb.run("forward", "tcp:0", f"localabstract:{SOCKET_NAME}", timeout=10)).strip()
        try:
            return int(out.splitlines()[-1])
        except (ValueError, IndexError):
            raise DeviceError(f"unexpected adb forward output: {out!r}") from None

    def _remove_forward(self) -> None:
        if self._port and not self.fixed_address:
            try:
                self.adb.run("forward", "--remove", f"tcp:{self._port}", timeout=5, check=False)
            except DeviceError:
                pass
        self._port = None

    # ------------------------------------------------------------ connection

    def _close(self) -> None:
        for obj in (self._file, self._sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self._file = self._sock = None

    def close(self) -> None:
        with self._lock:
            self._close()
            self._remove_forward()

    def _open(self) -> None:
        if self.fixed_address:
            addr = self.fixed_address
        else:
            if not self._prepared:
                self.ensure_installed()
                self.ensure_enabled()
                self._prepared = True
            if self._port is None:
                self._port = self._forward()
            addr = ("127.0.0.1", self._port)
        sock = socket.create_connection(addr, timeout=5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._file = sock.makefile("rwb")
        self._handshake()

    def _read_json(self, what: str) -> dict:
        assert self._file is not None
        raw = self._file.readline(4096)
        if not raw:
            raise ConnectionError(f"agent closed the connection before {what}")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            raise AgentAuthError(f"invalid {what} from the agent socket") from None
        if not isinstance(msg, dict):
            raise AgentAuthError(f"invalid {what} from the agent socket")
        return msg

    def _handshake(self) -> None:
        """Mutual HMAC-SHA256 challenge. The agent proves it knows the key too, so
        a process squatting the abstract socket cannot impersonate it."""
        assert self._sock is not None and self._file is not None
        self._sock.settimeout(5)
        try:
            hello = self._read_json("hello")
        except socket.timeout:
            raise AgentOutdatedError("the agent sent no hello (outdated agent or service not ready)") from None
        nonce = hello.get("nonce")
        if hello.get("hello") == "droidrt-agent" and hello.get("v") != PROTOCOL_VERSION:
            raise AgentOutdatedError(f"the agent speaks protocol version {hello.get('v')!r}, expected 2")
        if hello.get("hello") != "droidrt-agent" or not isinstance(nonce, str) or len(nonce) != 32 \
                or not HEX_RE.fullmatch(nonce):
            raise AgentAuthError("agent authentication failed (unexpected hello; another process may own the "
                                 "socket)")
        mine = secrets.token_hex(16)
        line = json.dumps({"auth": sign(self.key, "client:" + nonce), "nonce": mine}, separators=(",", ":"))
        self._file.write(line.encode("ascii") + b"\n")
        self._file.flush()
        reply = self._read_json("auth reply")
        if reply.get("ok") is not True:
            if reply.get("error") in ("no key", "auth failed"):
                # The agent has no key or another one (for example after a reinstall):
                # the caller delivers ours again and retries once.
                raise NoKeyError(f"the agent rejected the key ({reply.get('error')})")
            raise AgentAuthError(f"agent authentication failed: {str(reply.get('error'))[:200]}")
        proof = reply.get("proof")
        # compare_digest raises TypeError on non ASCII str, so the shape is checked first.
        if not (isinstance(proof, str) and proof.isascii() and len(proof) == 64
                and hmac.compare_digest(proof, sign(self.key, "agent:" + mine))):
            raise AgentAuthError("agent authentication failed (another process may own the socket)")

    def _connect(self) -> None:
        """Connect and confirm with ping. The accessibility service may need a
        moment to bind after being enabled, so retry until the deadline."""
        deadline = time.monotonic() + self.connect_timeout
        last: Exception | None = None
        attempt = 0
        key_sent = reinstalled = False
        while time.monotonic() < deadline:
            attempt += 1
            try:
                self._open()
                self._exchange("ping", {}, 5)
                return
            except AgentAuthError:
                self._close()
                raise
            except AgentOutdatedError as e:
                self._close()
                last = e
                if not reinstalled and not self.fixed_address and self.apk_path.exists():
                    # An agent from an older checkout: upgrade it from the local build once.
                    log.info("upgrading the device agent: %s", e)
                    self._install()
                    self.ensure_enabled()
                    reinstalled = True
                    deadline = max(deadline, time.monotonic() + 10)
                time.sleep(0.5)
                continue
            except NoKeyError as e:
                self._close()
                if key_sent:
                    raise AgentAuthError(f"agent authentication failed after delivering the key: {e}") from None
                self.deliver_key()
                key_sent = True
                last = e
                continue
            except (OSError, DeviceError) as e:
                last = e
                self._close()
                if attempt == 3 and not self.fixed_address:
                    # The forward may point at a dead server; renew it and re-check the setup.
                    self._remove_forward()
                    self._prepared = False
                time.sleep(0.5)
        raise DeviceError(f"cannot reach the device agent: {last}")

    def _exchange(self, cmd: str, args: dict, timeout: float):
        assert self._sock is not None and self._file is not None
        self._next_id += 1
        req_id = self._next_id
        line = json.dumps({"id": req_id, "cmd": cmd, "args": args}, ensure_ascii=False, separators=(",", ":"))
        self._sock.settimeout(timeout)
        self._file.write(line.encode("utf-8") + b"\n")
        self._file.flush()
        while True:
            raw = self._file.readline(MAX_RESPONSE_BYTES + 1)
            if not raw:
                raise ConnectionError("agent closed the connection")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DeviceError("agent response too large")
            try:
                resp = json.loads(raw)
            except json.JSONDecodeError:
                raise DeviceError("agent sent invalid JSON") from None
            if resp.get("id") != req_id:
                continue  # a late answer to an abandoned request
            if resp.get("ok"):
                return resp.get("result")
            raise AgentError(str(resp.get("error") or "unknown agent error"))

    def call(self, cmd: str, args: dict | None = None, timeout: float = 15):
        args = {k: v for k, v in (args or {}).items() if v is not None}
        with self._lock:
            if self._sock is not None and cmd not in IDEMPOTENT and time.monotonic() - self._last_used > 2:
                # A command with side effects is never resent, so make sure an idle
                # connection is still alive before sending it.
                try:
                    self._exchange("ping", {}, 5)
                except (OSError, ConnectionError, DeviceError):
                    self._close()
            for attempt in (1, 2):
                if self._sock is None:
                    self._connect()
                self._last_used = time.monotonic()
                try:
                    return self._exchange(cmd, args, timeout)
                except AgentError:
                    raise
                except socket.timeout:
                    self._close()
                    raise DeviceError(f"agent command {cmd} timed out after {timeout:.0f}s") from None
                except (OSError, ConnectionError) as e:
                    self._close()
                    if attempt == 2 or cmd not in IDEMPOTENT:
                        raise DeviceError(f"connection to the agent dropped during {cmd}: {e}") from None
                    log.info("agent connection dropped, reconnecting")
        raise DeviceError("unreachable")
