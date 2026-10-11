"""Entry point: wires droidrt, the device agent and the MCP protocol together.

    python3 -m droidrt_mcp
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .agent import Adb, AgentClient
from .apks import ApkCache
from .device import DeviceManager
from .protocol import McpServer, Resource, ResourceTemplate, Tool
from .tools import Tools, limit_notifications
from . import validate

log = logging.getLogger("droidrt_mcp")

REPO_ROOT = Path(__file__).resolve().parent.parent

INSTRUCTIONS = (
    "droidrt runs Android apps in a local VM. Typical loop: list_apps, open_app (headless by default), "
    "get_screen to read the UI and element ids, then tap, type_text, swipe, press_key or wait_for, and "
    "close_app when done. App specific tools are prefixed with the app name. Respect each app's terms of "
    "service and never act on accounts that are not yours."
)


def import_droidrt():
    """droidrt.py lives next to this package. Importing it sets PATH so adb and
    scrcpy are found even when the client starts us with a minimal environment."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import droidrt  # noqa: E402
    return droidrt


def import_semantic():
    try:
        import semantic  # type: ignore
    except ImportError:
        log.info("semantic package not found; only generic tools are available")
        return None
    return semantic


class SemanticReloader:
    """Reloads the semantic tools and publishes the new tool set.

    One reload runs at a time: the startup reload in the background thread and
    the one after record_flow would otherwise race, and the slower one, holding
    an older view of the flows folder, could publish last and drop the flow that
    was just recorded."""

    def __init__(self, known: Callable[[], list[str]], installed: Callable[[], list[str]],
                 load: Callable[[list[str]], Any], publish: Callable[[], None]):
        self.known = known
        self.installed = installed
        self.load = load
        self.publish = publish
        self.lock = threading.Lock()

    def __call__(self, extra: str | None = None) -> None:
        with self.lock:
            pkgs = set(self.known())
            try:
                pkgs |= set(self.installed())
            except Exception as e:
                log.info("cannot list installed apps for semantic tools: %s", e)
            if extra:
                pkgs.add(extra)
            self.load(sorted(pkgs))
            self.publish()


def build(serial: str | None = None) -> tuple[McpServer, DeviceManager, Tools]:
    droidrt = import_droidrt()
    serial = serial or os.environ.get("DROIDRT_SERIAL") or droidrt.SERIAL
    adb = Adb(serial, droidrt.tool("adb"))
    agent = AgentClient(adb)
    try:
        scrcpy = droidrt.tool("scrcpy")
    except droidrt.DroidError:
        scrcpy = "scrcpy"

    def labels() -> dict[str, str]:
        try:
            return {pkg: pl.get("CFBundleName") or pkg for pkg, (_, pl) in droidrt.launchers().items()}
        except Exception:
            return {}

    def launch_window(pkg: str) -> None:
        # `droidrt.py launch` blocks while the window is open, so it runs detached.
        droidrt.LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([sys.executable, str(REPO_ROOT / "droidrt.py"), "launch", pkg],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    device = DeviceManager(adb, agent, scrcpy=scrcpy, display_spec=droidrt.DISPLAY,
                           state_file=droidrt.STATE_DIR / "mcp-sessions.json", log_dir=droidrt.LOG_DIR,
                           launch_window=launch_window, ensure_vm=droidrt.start, labels=labels)
    semantic = import_semantic()

    def loader(pkg: str) -> list[dict]:
        return semantic.tools_for(pkg, apk_cache.base_apks(pkg))

    apk_cache = ApkCache(adb, Path.home() / "Library/Caches/droidrt/apks")
    tools = Tools(device, loader if semantic else None)
    server = McpServer("droidrt", __version__, INSTRUCTIONS)
    device_lock = threading.Lock()

    def guarded(tool: Tool) -> Tool:
        inner = tool.handler

        def handler(args: dict):
            with device_lock:
                return inner(args)
        tool.handler = handler
        return tool

    def publish() -> None:
        server.set_tools([guarded(t) for t in tools.all()])

    def known_packages() -> list[str]:
        pkgs = set(labels())
        flows_root = droidrt.STATE_DIR / "flows"
        if flows_root.is_dir():
            pkgs |= {p.name for p in flows_root.iterdir() if p.is_dir() and validate.PACKAGE_RE.fullmatch(p.name)}
        if semantic is not None:
            curated = Path(semantic.__file__).resolve().parent / "curated"
            pkgs |= {p.stem for p in curated.glob("*.json")}
        return sorted(pkgs)

    def installed() -> list[str]:
        return [a["package"] for a in device.list_apps()] if adb.is_ready() else []

    reload_semantic = SemanticReloader(known_packages, installed, tools.load_semantic, publish)

    tools.on_flows_changed = reload_semantic  # type: ignore[method-assign]
    publish()
    # Semantic tools are loaded at startup, before the first tools/list in most clients;
    # when it takes longer the client gets notifications/tools/list_changed.
    threading.Thread(target=reload_semantic, daemon=True, name="semantic").start()

    def read_notifications(pkg: str | None = None) -> str:
        if pkg is not None:
            validate.package({"package": pkg})
        with device_lock:
            items = agent.call("notifications", {"package": pkg}, timeout=10)
        items = [n for n in items if isinstance(n, dict) and validate.package_allowed(n.get("package"))
                 and (pkg is None or n.get("package") == pkg)] if isinstance(items, list) else []
        return json.dumps(limit_notifications(items), ensure_ascii=False)

    def read_apps() -> str:
        with device_lock:
            return json.dumps(device.list_apps(), ensure_ascii=False)

    server.resources = {
        "droidrt://notifications": Resource("droidrt://notifications", "notifications",
                                            "Active and recently posted Android notifications", read_notifications),
        "droidrt://apps": Resource("droidrt://apps", "apps",
                                   "Installed apps with a launcher and their window state", read_apps),
    }
    server.templates = [ResourceTemplate("droidrt://notifications/{package}", "notifications-by-package",
                                         "Notifications of one package", "droidrt://notifications/",
                                         read_notifications)]
    return server, device, tools


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("DROIDRT_MCP_LOG", "INFO").upper(),
                        format="droidrt-mcp %(levelname)s %(name)s: %(message)s")
    try:
        server, device, _ = build()
    except Exception as e:
        log.error("startup failed: %s", e)
        return 1

    def on_term(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    # A closed terminal sends SIGHUP, whose default action skips the cleanup
    # below and leaves the headless scrcpy sessions running.
    signal.signal(signal.SIGHUP, on_term)
    stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", errors="replace", newline="\n", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    # Anything printed by mistake must never corrupt the protocol stream.
    sys.stdout = sys.stderr
    try:
        server.serve(stdin, stdout)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        device.shutdown()
    return 0
