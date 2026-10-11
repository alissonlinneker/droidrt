"""Device operations used by the MCP tools: displays, headless sessions,
screenshots and UI actions through the on-device agent.

An app is either shown in a droidrt window (a scrcpy process started by
`droidrt.py launch`) or driven headless: a scrcpy process with `--no-window`
that keeps a virtual display alive and records its low rate video stream to
/dev/null (scrcpy refuses `--new-display` without video). Headless sessions
belong to this server: they are stopped with SIGTERM on close_app and on exit.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import validate
from .agent import Adb, AgentClient, AgentError, DeviceError
from .protocol import ToolInputError

log = logging.getLogger("droidrt_mcp.device")

NEW_DISPLAY_RE = re.compile(r"New display: \S+ \(id=(\d+)\)")
START_APP_RE = re.compile(r"--start-app=[+-]?([A-Za-z][\w.]*)")
HEADLESS_TAG = "droidrt-headless:"
HEADLESS_RE = re.compile(r"--window-title=droidrt-headless:([A-Za-z][\w.]*)")
SCREENSHOT_MAX = 1024
KEYCODES = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER"}


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise DeviceError("screencap did not return a PNG image")
    return struct.unpack(">II", data[16:24])


def parse_logical_displays(dumpsys: str) -> dict[int, str]:
    """Map logical display ids to the unique id of their display device."""
    out = {}
    for m in re.finditer(r"^\s*mDisplayId=(\d+)\s*\n\s*mPrimaryDisplayDevice=.*\(([^()\s]+)\)\s*$", dumpsys, re.M):
        out[int(m.group(1))] = m.group(2)
    return out


def parse_sf_displays(text: str) -> dict[str, str]:
    """Map display device unique ids to SurfaceFlinger display ids."""
    out = {}
    for m in re.finditer(r"^Display (\d+) \(([^)]*)\):(.*)$", text, re.M):
        sf_id, rest = m.group(1), m.group(3)
        uid = re.search(r'uniqueId="([^"]+)"', rest)
        out[uid.group(1) if uid else f"local:{sf_id}"] = sf_id
    return out


def parse_scrcpy_ps(ps_output: str, serial: str) -> list[dict]:
    """scrcpy processes for this device: [{pid, package, headless}]."""
    found = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        argv0 = cmd.split(None, 1)[0]
        if not argv0.endswith("scrcpy") or f"-s {serial}" not in cmd:
            continue
        tag = HEADLESS_RE.search(cmd)
        if tag and "--no-window" in cmd:
            found.append({"pid": pid, "package": tag.group(1), "headless": True})
            continue
        m = START_APP_RE.search(cmd)
        if m:
            found.append({"pid": pid, "package": m.group(1), "headless": "--no-window" in cmd})
    return found


def compact_tree(roots: list[dict], max_chars: int = 60000) -> str:
    """Readable outline of the agent tree. Nodes without text, description,
    resource id or interaction flags are folded into their children."""
    lines: list[str] = []

    def label(n: dict) -> str | None:
        flags = [f for f in ("clickable", "editable", "scrollable", "password", "focused", "selected")
                 if n.get(f)]
        if n.get("checked") is not None:
            flags.append("checked" if n["checked"] else "unchecked")
        if n.get("enabled") is False:
            flags.append("disabled")
        text, desc, rid = n.get("text"), n.get("desc"), n.get("rid")
        if not (text or desc or rid or flags):
            return None
        cls = (n.get("cls") or "").rsplit(".", 1)[-1]
        out = f"[{n.get('id')}] {cls}"
        if text:
            out += f' "{_clip(text)}"'
        if desc:
            out += f' desc="{_clip(desc)}"'
        if rid:
            out += f" id={rid.split(':id/', 1)[-1]}"
        if flags:
            out += " {" + ",".join(flags) + "}"
        b = n.get("bounds")
        if isinstance(b, list) and len(b) == 4:
            out += f" @{b[0]},{b[1]},{b[2]},{b[3]}"
        return out

    def walk(n: dict, depth: int) -> None:
        text = label(n)
        if text is not None:
            lines.append("  " * depth + text)
            depth += 1
        for c in n.get("children") or []:
            if isinstance(c, dict):
                walk(c, depth)

    for root in roots:
        if not isinstance(root, dict):
            continue
        b = root.get("bounds")
        lines.append(f"window {root.get('id')} pkg={root.get('pkg')} display={root.get('displayId')}"
                     + (f" @{b[0]},{b[1]},{b[2]},{b[3]}" if isinstance(b, list) and len(b) == 4 else ""))
        for c in root.get("children") or []:
            if isinstance(c, dict):
                walk(c, 1)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated, use find_elements or a smaller max_depth)"
    return text


def _clip(s: str, n: int = 120) -> str:
    s = s.replace("\n", " ").replace('"', "'")
    return s if len(s) <= n else s[: n - 3] + "..."


def iter_nodes(roots: list) -> Any:
    stack = list(reversed([r for r in roots if isinstance(r, dict)]))
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed([c for c in n.get("children") or [] if isinstance(c, dict)]))


def node_summary(n: dict) -> dict:
    keys = ("id", "cls", "pkg", "text", "desc", "rid", "bounds", "displayId", "clickable", "editable",
            "scrollable", "checked", "enabled", "focused", "selected")
    return {k: n.get(k) for k in keys if n.get(k) is not None}


class DeviceManager:
    def __init__(self, adb: Adb, agent: AgentClient, *, scrcpy: str = "scrcpy", display_spec: str = "720x1560/320",
                 state_file: Path | None = None, log_dir: Path | None = None,
                 launch_window: Callable[[str], None] | None = None, ensure_vm: Callable[[], None] | None = None,
                 labels: Callable[[], dict[str, str]] | None = None):
        self.adb = adb
        self.agent = agent
        self.scrcpy = scrcpy
        self.display_spec = display_spec
        self.state_file = state_file
        self.log_dir = log_dir or Path(tempfile.gettempdir())
        self.launch_window = launch_window
        self.ensure_vm = ensure_vm
        self.labels = labels or (lambda: {})
        self.sessions: dict[str, dict] = {}
        self.lock = threading.RLock()
        self._load_sessions()

    # ------------------------------------------------------------ VM

    def ready(self) -> None:
        if self.adb.is_ready():
            return
        if self.ensure_vm is None:
            raise DeviceError(f"device {self.adb.serial} is not available")
        log.info("starting the VM")
        self.ensure_vm()
        if not self.adb.is_ready():
            raise DeviceError(f"device {self.adb.serial} did not finish booting")

    # ------------------------------------------------------------ processes and sessions

    def scrcpy_processes(self) -> list[dict]:
        try:
            out = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return parse_scrcpy_ps(out, self.adb.serial)

    def _load_sessions(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        alive = {(p["pid"], p["package"]) for p in self.scrcpy_processes() if p["headless"]}
        for pkg, s in (data.items() if isinstance(data, dict) else []):
            if isinstance(s, dict) and (s.get("pid"), pkg) in alive and isinstance(s.get("displayId"), int):
                self.sessions[pkg] = {"pid": s["pid"], "displayId": s["displayId"], "adopted": True}
        self._save_sessions()

    def _save_sessions(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(
                {k: {"pid": v["pid"], "displayId": v["displayId"]} for k, v in self.sessions.items()}))
        except OSError as e:
            log.warning("cannot save session state: %s", e)

    def _session_alive(self, pkg: str) -> dict | None:
        s = self.sessions.get(pkg)
        if not s:
            return None
        proc = s.get("proc")
        if proc is not None and proc.poll() is not None:
            self.sessions.pop(pkg, None)
            self._save_sessions()
            return None
        if proc is None and not any(p["pid"] == s["pid"] for p in self.scrcpy_processes()):
            self.sessions.pop(pkg, None)
            self._save_sessions()
            return None
        return s

    def window_packages(self) -> set[str]:
        return {p["package"] for p in self.scrcpy_processes() if not p["headless"]}

    def displays(self) -> list[dict]:
        res = self.agent.call("displays", timeout=10)
        return res if isinstance(res, list) else []

    def display_for(self, pkg: str) -> int | None:
        """Display that shows `pkg`: its headless session, else a display where
        the agent sees its windows (a virtual display is preferred)."""
        s = self._session_alive(pkg)
        if s:
            return s["displayId"]
        ids = [d.get("displayId") for d in self.displays() if pkg in (d.get("packages") or [])]
        ids = [i for i in ids if isinstance(i, int)]
        if not ids:
            return None
        nonzero = [i for i in ids if i != 0]
        return nonzero[-1] if nonzero else ids[0]

    def is_installed(self, pkg: str) -> bool:
        return "package:" in self.adb.shell("pm", "path", pkg, timeout=15, check=False)

    def open_app(self, pkg: str, headless: bool = True) -> dict:
        with self.lock:
            self.ready()
            if not self.is_installed(pkg):
                raise ToolInputError(f"{pkg} is not installed in the VM")
            has_window = pkg in self.window_packages()
            if has_window or self._session_alive(pkg):
                display = self._wait_display(pkg, 10)
                if display is not None:
                    return {"package": pkg, "displayId": display, "window": has_window,
                            "headless": not has_window, "started": False}
            if not headless:
                if self.launch_window is None:
                    raise ToolInputError("opening a window is not available in this setup")
                self.launch_window(pkg)
                display = self._wait_display(pkg, 30)
                if display is None:
                    raise DeviceError(f"the window for {pkg} did not show up")
                return {"package": pkg, "displayId": display, "window": True, "headless": False, "started": True}
            display = self._start_headless(pkg)
            return {"package": pkg, "displayId": display, "window": False, "headless": True, "started": True}

    def _wait_display(self, pkg: str, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            d = self.display_for(pkg)
            if d is not None or time.monotonic() > deadline:
                return d
            time.sleep(0.5)

    def headless_argv(self, pkg: str) -> list[str]:
        """scrcpy only keeps the virtual display: no window, no control channel, a
        1 fps video stream recorded to /dev/null (scrcpy needs a video sink for
        --new-display). The app is started separately with `am start --display`,
        because scrcpy's own --start-app races with a slow display setup. The
        window title only tags the process so it can be found again."""
        return [self.scrcpy, "-s", self.adb.serial, "--no-window", "--no-audio", "--no-control",
                f"--new-display={self.display_spec}", "--no-vd-system-decorations", "--display-ime-policy=hide",
                "--video-codec=h264", "--video-encoder=c2.android.avc.encoder", "--max-fps=1",
                "--video-bit-rate=200K", "--record=/dev/null", "--record-format=mkv",
                f"--window-title={HEADLESS_TAG}{pkg}"]

    def launcher_component(self, pkg: str) -> str:
        out = self.adb.shell("cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", pkg, timeout=15, check=False)
        comps = [l.strip() for l in out.splitlines() if re.fullmatch(r"[A-Za-z][\w.]*/[\w.$]+", l.strip())]
        if not comps:
            raise ToolInputError(f"{pkg} has no launcher activity")
        return comps[-1]

    def launch_on(self, pkg: str, display: int) -> None:
        comp = self.launcher_component(pkg)
        out = self.adb.shell("am", "start", "-W", "--display", str(display), "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", "-n", comp, timeout=60, check=False)
        if re.search(r"^Error|Exception", out, re.M):
            raise DeviceError(f"cannot start {comp} on display {display}: {out.strip()[-300:]}")

    def _start_headless(self, pkg: str) -> int:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"mcp-{pkg}.log"
        last = ""
        for attempt in range(3):
            with open(log_path, "wb") as out:
                proc = subprocess.Popen(self.headless_argv(pkg), stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 40
            display = None
            while time.monotonic() < deadline and proc.poll() is None:
                last = log_path.read_text(errors="replace")
                m = NEW_DISPLAY_RE.search(last)
                if m:
                    display = int(m.group(1))
                    break
                time.sleep(0.25)
            if display is not None:
                self.sessions[pkg] = {"pid": proc.pid, "displayId": display, "proc": proc}
                self._save_sessions()
                try:
                    self.launch_on(pkg, display)
                except Exception:
                    self.sessions.pop(pkg, None)
                    self._save_sessions()
                    self._terminate(proc.pid, proc)
                    raise
                self._wait_app_on(pkg, display, 15)
                log.info("headless session for %s on display %d (pid %d)", pkg, display, proc.pid)
                return display
            self._terminate(proc.pid, proc)
            log.info("headless scrcpy failed to start (attempt %d)", attempt + 1)
            time.sleep(1)
        raise DeviceError(f"could not create a headless display for {pkg}: {last.strip()[-400:]}")

    def _wait_app_on(self, pkg: str, display: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                for d in self.displays():
                    if d.get("displayId") == display and pkg in (d.get("packages") or []):
                        return
            except DeviceError:
                pass
            time.sleep(0.5)

    @staticmethod
    def _terminate(pid: int, proc: subprocess.Popen | None = None, wait: float = 5) -> bool:
        """SIGTERM only: SIGINT is ignored by background scrcpy and SIGKILL can wedge
        the scrcpy server inside the VM."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc is not None:
                if proc.poll() is not None:
                    return True
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
            time.sleep(0.1)
        return False

    def close_app(self, pkg: str) -> dict:
        with self.lock:
            closed = []
            s = self.sessions.pop(pkg, None)
            if s:
                self._terminate(s["pid"], s.get("proc"))
                closed.append({"pid": s["pid"], "kind": "headless"})
                self._save_sessions()
            for p in self.scrcpy_processes():
                if p["package"] == pkg and not any(c["pid"] == p["pid"] for c in closed):
                    self._terminate(p["pid"])
                    closed.append({"pid": p["pid"], "kind": "headless" if p["headless"] else "window"})
            return {"package": pkg, "closed": closed}

    def shutdown(self) -> None:
        """Stop the headless sessions started by this server process."""
        with self.lock:
            for pkg, s in list(self.sessions.items()):
                if s.get("proc") is not None:
                    self._terminate(s["pid"], s["proc"])
                    self.sessions.pop(pkg, None)
            self._save_sessions()
        self.agent.close()

    # ------------------------------------------------------------ screen

    def sf_display_id(self, display: int) -> str | None:
        if display == 0:
            return None
        logical = parse_logical_displays(self.adb.shell("dumpsys", "display", timeout=15))
        uid = logical.get(display)
        if uid is None:
            raise DeviceError(f"display {display} does not exist")
        if uid.startswith("local:"):
            return uid.split(":", 1)[1]
        sf = parse_sf_displays(self.adb.shell("dumpsys", "SurfaceFlinger", "--display-id", timeout=15))
        if uid not in sf:
            raise DeviceError(f"no SurfaceFlinger display for display {display}")
        return sf[uid]

    def screenshot(self, display: int, max_side: int = SCREENSHOT_MAX) -> tuple[str, dict]:
        """PNG of a display, scaled down to max_side, as base64 plus size info."""
        sf = self.sf_display_id(display)
        args = ["screencap", "-p"] + (["-d", sf] if sf else [])
        data = self.adb.exec_out(*args, timeout=20)
        w, h = png_size(data)
        info = {"displayId": display, "width": w, "height": h, "imageWidth": w, "imageHeight": h, "scale": 1.0}
        if max(w, h) > max_side:
            with tempfile.TemporaryDirectory(prefix="droidrt-mcp-") as tmp:
                src, dst = Path(tmp) / "in.png", Path(tmp) / "out.png"
                src.write_bytes(data)
                try:
                    subprocess.run(["/usr/bin/sips", "-Z", str(max_side), str(src), "--out", str(dst)],
                                   capture_output=True, timeout=30, check=True, stdin=subprocess.DEVNULL)
                    data = dst.read_bytes()
                except (OSError, subprocess.SubprocessError) as e:
                    log.warning("sips failed, sending the full size image: %s", e)
            iw, ih = png_size(data)
            info.update(imageWidth=iw, imageHeight=ih, scale=round(w / iw, 4))
        return base64.b64encode(data).decode("ascii"), info

    def tree(self, pkg: str, max_depth: int | None = None) -> list[dict]:
        res = self.agent.call("tree", {"package": pkg, "maxDepth": max_depth}, timeout=20)
        if isinstance(res, dict):
            res = [res]
        return res if isinstance(res, list) else []

    def find(self, selector: dict) -> list[dict]:
        res = self.agent.call("find", selector, timeout=15)
        return res if isinstance(res, list) else []

    # ------------------------------------------------------------ actions

    def click_node(self, node_id: str) -> dict:
        res = self.agent.call("action", {"nodeId": node_id, "action": "click"}, timeout=10)
        self.idle()
        return res if isinstance(res, dict) else {"performed": bool(res)}

    def idle(self, timeout_ms: int = 3000) -> None:
        try:
            self.agent.call("wait_idle", {"timeoutMs": timeout_ms, "idleMs": 300}, timeout=timeout_ms / 1000 + 5)
        except DeviceError as e:
            log.debug("wait_idle failed: %s", e)

    def tap_point(self, display: int, x: int, y: int) -> dict:
        res = self.agent.call("tap", {"displayId": display, "x": x, "y": y}, timeout=10)
        self.idle()
        return res if isinstance(res, dict) else {"performed": bool(res)}

    def first_match(self, selectors: list[dict]) -> dict | None:
        for sel in selectors:
            nodes = [n for n in self.find(sel) if n.get("enabled", True)]
            if nodes:
                clickable = [n for n in nodes if n.get("clickable")]
                return (clickable or nodes)[0]
        return None

    def tap_selector(self, selector: dict) -> dict:
        for attempt in (1, 2):
            node = self.first_match([selector])
            if node is None:
                raise ToolInputError(f"no element matches {json.dumps(selector, ensure_ascii=False)}")
            try:
                res = self.click_node(node["id"])
            except AgentError as e:
                if "stale" in str(e) and attempt == 1:
                    continue
                raise
            if not res.get("performed") and isinstance(node.get("bounds"), list) and node.get("displayId") is not None:
                b = node["bounds"]
                res = self.tap_point(node["displayId"], (b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            return {"element": node.get("id"), **res}
        raise ToolInputError("element went stale twice")

    def editable_node(self, pkg: str) -> dict | None:
        nodes = [n for n in iter_nodes(self.tree(pkg)) if n.get("editable")]
        focused = [n for n in nodes if n.get("focused")]
        return (focused or nodes or [None])[0]

    def set_text(self, node_id: str, text: str, submit: bool = False) -> dict:
        res = self.agent.call("action", {"nodeId": node_id, "action": "set_text", "text": text}, timeout=10)
        out = {"element": node_id, "performed": bool(res.get("performed") if isinstance(res, dict) else res)}
        if submit:
            self.idle(1500)
            sub = self.agent.call("action", {"nodeId": node_id, "action": "ime_enter"}, timeout=10)
            out["submitted"] = bool(sub.get("performed") if isinstance(sub, dict) else sub)
        self.idle()
        return out

    def type_selector(self, selector: dict, text: str) -> dict:
        node = self.first_match([selector])
        if node is None:
            raise ToolInputError(f"no element matches {json.dumps(selector, ensure_ascii=False)}")
        if not node.get("editable"):
            pkg = selector.get("package") or node.get("pkg")
            self.click_node(node["id"])
            focused = self.editable_node(pkg) if pkg else None
            if focused is None:
                raise ToolInputError("the matched element is not editable")
            node = focused
        return self.set_text(node["id"], text)

    def display_size(self, display: int) -> tuple[int, int]:
        for d in self.displays():
            if d.get("displayId") == display and d.get("width") and d.get("height"):
                return int(d["width"]), int(d["height"])
        raise DeviceError(f"unknown size for display {display}")

    def require_display(self, pkg: str) -> int:
        d = self.display_for(pkg)
        if d is None:
            raise ToolInputError(f"{pkg} has no open window; call open_app first")
        return d

    def swipe(self, pkg: str, direction: str) -> dict:
        """`direction` is where the finger moves: 'up' scrolls the content down."""
        display = self.require_display(pkg)
        w, h = self.display_size(display)
        cx, cy = w // 2, h // 2
        dx, dy = int(w * 0.3), int(h * 0.3)
        x1, y1, x2, y2 = {
            "up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
            "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy),
        }[direction]
        res = self.agent.call("swipe", {"displayId": display, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                        "durationMs": 300}, timeout=10)
        self.idle()
        return {"displayId": display, **(res if isinstance(res, dict) else {"performed": bool(res)})}

    def press(self, pkg: str, key: str) -> dict:
        """Key events go to the app's display with `input -d`, so a key never lands on
        another app. The agent's global action is the fallback."""
        self.idle()
        display = self.display_for(pkg)
        if display is not None:
            out = self.adb.shell("input", "-d", str(display), "keyevent", KEYCODES[key], timeout=10, check=False)
            if "Error" not in out and "Exception" not in out:
                self.idle()
                return {"displayId": display, "key": key, "performed": True}
        if key == "enter":
            raise ToolInputError(f"{pkg} has no open window; call open_app first")
        res = self.agent.call("global", {"action": key}, timeout=10)
        self.idle()
        return {"key": key, **(res if isinstance(res, dict) else {"performed": bool(res)})}

    def wait_for(self, selector: dict, timeout_ms: int) -> dict:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            nodes = self.find(selector)
            if nodes:
                return nodes[0]
            if time.monotonic() >= deadline:
                raise ToolInputError(f"timed out after {timeout_ms} ms waiting for "
                                     f"{json.dumps(selector, ensure_ascii=False)}")
            time.sleep(0.3)

    def start_intent(self, pkg: str, spec: dict) -> str:
        # The last line of defense: an intent never leaves the app it was issued for.
        validate.check_target(spec, pkg)
        display = self.display_for(pkg)
        if display is None:
            display = self.open_app(pkg, True)["displayId"]
        argv = ["am", "start", "-W", "--display", str(display)]
        if spec.get("action"):
            argv += ["-a", spec["action"]]
        if spec.get("data"):
            argv += ["-d", spec["data"]]
        if spec.get("mimeType"):
            argv += ["-t", spec["mimeType"]]
        for c in spec.get("categories") or []:
            argv += ["-c", c]
        if spec.get("component"):
            argv += ["-n", spec["component"]]
        elif spec.get("package") or pkg:
            argv += ["-p", spec.get("package") or pkg]
        for k, v in (spec.get("extras") or {}).items():
            if isinstance(v, bool):
                argv += ["--ez", k, "true" if v else "false"]
            elif isinstance(v, int):
                argv += ["--ei" if -(2 ** 31) <= v < 2 ** 31 else "--el", k, str(v)]
            elif isinstance(v, float):
                argv += ["--ef", k, repr(v)]
            else:
                argv += ["--es", k, v]
        out = self.adb.shell(*argv, timeout=30, check=False).strip()
        if re.search(r"^(Error|Exception)|Error type|does not exist|unable to resolve", out, re.M | re.I):
            raise ToolInputError(f"am start failed: {out[-600:]}")
        self.idle()
        return f"display {display}: " + " ".join(l for l in out.splitlines() if l.startswith(("Status", "Activity", "Warning")))

    # ------------------------------------------------------------ apps

    def list_apps(self) -> list[dict]:
        self.ready()
        third = set(re.findall(r"^package:(\S+)", self.adb.shell("pm", "list", "packages", "-3", timeout=20), re.M))
        out = self.adb.shell("cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", timeout=20)
        launchable = {line.strip().split("/", 1)[0] for line in out.splitlines() if "/" in line}
        if "com.android.vending" in launchable:
            third.add("com.android.vending")
        windows = self.window_packages()
        labels = self.labels()
        apps = []
        for pkg in sorted(third & launchable):
            if not validate.package_allowed(pkg):
                continue
            s = self._session_alive(pkg)
            apps.append({"package": pkg, "label": labels.get(pkg, pkg), "window": pkg in windows,
                         "headless": bool(s), **({"displayId": s["displayId"]} if s else {})})
        return apps
