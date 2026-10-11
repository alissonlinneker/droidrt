"""Tests for droidrt_mcp.

    python3 -m unittest -v test_mcp

The end to end tests drive the real VM and run only when DROIDRT_E2E=1. They
open com.whatsapp headless, read the screen and close it again; they never
accept terms or send anything. Set DROIDRT_E2E_OUT to keep the screenshot.
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import logging
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from droidrt_mcp import flows, validate
from droidrt_mcp.agent import Adb, AgentAuthError, AgentClient, AgentError, DeviceError, load_or_create_key, sign
from droidrt_mcp.device import (DeviceManager, compact_tree, parse_logical_displays, parse_scrcpy_ps,
                                parse_sf_displays, png_size)
from droidrt_mcp.protocol import (INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST, MAX_BATCH, MAX_PENDING,
                                  METHOD_NOT_FOUND, PARSE_ERROR, RESOURCE_NOT_FOUND, SERVER_BUSY, McpServer, Resource,
                                  RpcError, Tool, ToolInputError, image_content, redact)
from droidrt_mcp.tools import MAX_NOTIFICATION_FIELD, MAX_NOTIFICATIONS, Tools, limit_notifications

ROOT = Path(__file__).resolve().parent
logging.disable(logging.CRITICAL)
TEST_KEY = "ab" * 32
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def rpc(i, method, params=None):
    msg = {"jsonrpc": "2.0", "id": i, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def init_params(version="2025-06-18"):
    return {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}


# ---------------------------------------------------------------- fakes

class FakeAgentHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        nonce = secrets.token_hex(16)
        self.wfile.write((json.dumps({"hello": "droidrt-agent", "v": srv.hello_version, "nonce": nonce}) + "\n").encode())
        self.wfile.flush()
        auth = json.loads(self.rfile.readline())
        if srv.key is None:
            self.wfile.write(b'{"ok":false,"error":"no key"}\n')
            return
        if not hmac.compare_digest(auth.get("auth", ""), sign(srv.key, "client:" + nonce)):
            self.wfile.write(b'{"ok":false,"error":"auth failed"}\n')
            return
        proof = "0" * 64 if srv.bad_proof else sign(srv.key, "agent:" + auth["nonce"])
        self.wfile.write((json.dumps({"ok": True, "proof": proof}) + "\n").encode())
        self.wfile.flush()
        for raw in self.rfile:
            req = json.loads(raw)
            srv.requests.append(req)
            if srv.drop_next:
                srv.drop_next = False
                return
            try:
                result = srv.respond(req["cmd"], req.get("args") or {})
                resp = {"id": req["id"], "ok": True, "result": result}
            except AgentError as e:
                resp = {"id": req["id"], "ok": False, "error": str(e)}
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


class FakeAgent(socketserver.ThreadingTCPServer):
    """Speaks the device agent protocol with a tiny scripted UI."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeAgentHandler)
        self.requests: list[dict] = []
        self.drop_next = False
        self.key: str | None = TEST_KEY
        self.bad_proof = False
        self.hello_version = 2
        self.text = ""
        self.nodes = [
            {"id": "w5:0.0", "cls": "android.widget.EditText", "pkg": "com.example", "text": None,
             "rid": "com.example:id/input", "bounds": [0, 100, 720, 200], "displayId": 2, "clickable": True,
             "editable": True, "enabled": True, "focused": True},
            {"id": "w5:0.1", "cls": "android.widget.Button", "pkg": "com.example", "text": "Send",
             "desc": "Send", "rid": "com.example:id/send", "bounds": [600, 1400, 700, 1500], "displayId": 2,
             "clickable": True, "editable": False, "enabled": True},
        ]
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return self.server_address

    def respond(self, cmd, args):
        if cmd == "ping":
            return {"version": "1", "sdk": 37}
        if cmd == "displays":
            return [{"displayId": 0, "width": 1080, "height": 2400, "packages": ["com.android.systemui"]},
                    {"displayId": 2, "width": 720, "height": 1560, "packages": ["com.example"]}]
        if cmd == "tree":
            return [{"id": "w5:0", "cls": "android.widget.FrameLayout", "pkg": "com.example",
                     "bounds": [0, 0, 720, 1560], "displayId": 2, "children": self.nodes}]
        if cmd == "find":
            out = []
            for n in self.nodes:
                if "text" in args and n.get("text") != args["text"]:
                    continue
                if "textContains" in args and args["textContains"].lower() not in (n.get("text") or "").lower():
                    continue
                if "contentDesc" in args and n.get("desc") != args["contentDesc"]:
                    continue
                if "resourceId" in args and not (n["rid"] == args["resourceId"]
                                                 or n["rid"].endswith("/" + args["resourceId"])):
                    continue
                out.append(n)
            return out
        if cmd == "action":
            if args["nodeId"] == "w9:9":
                raise AgentError("stale node")
            if args["action"] == "set_text":
                self.text = args["text"]
            return {"performed": True}
        if cmd in ("tap", "swipe", "global"):
            return {"performed": True}
        if cmd == "wait_idle":
            return {"idle": True}
        if cmd == "notifications":
            return [{"key": "0|com.example|1", "package": "com.example", "postTime": 1, "title": "Ann",
                     "text": "hi", "subText": None,
                     "actions": [{"index": 0, "title": "Mark read", "remoteInput": False},
                                 {"index": 1, "title": "Reply", "remoteInput": True}]}]
        if cmd == "notification_action":
            return {"performed": True}
        raise AgentError(f"unknown cmd {cmd}")


class FakeAdb(Adb):
    """Records shell argv lists and answers a few known commands."""

    def __init__(self, agent: "FakeAgent | None" = None):
        super().__init__("emulator-0000", "/bin/false")
        self.agent = agent
        self.calls: list[tuple] = []
        self.settings = {"enabled_accessibility_services": "com.other/.Svc", "accessibility_enabled": "0"}

    def run(self, *args, timeout=30, check=True, binary=False, input_data=None):
        self.calls.append(args)
        return b"" if binary else ""

    def shell(self, *args, timeout=30, check=True):
        self.calls.append(("shell",) + args)
        if args[:2] == ("pm", "path"):
            return "package:/data/app/base.apk\n"
        if args[:3] == ("settings", "get", "secure"):
            return self.settings.get(args[3], "null") + "\n"
        if args[:3] == ("settings", "put", "secure"):
            self.settings[args[3]] = args[4]
            return ""
        if args[:2] == ("getprop", "sys.boot_completed"):
            return "1\n"
        if args[:2] == ("am", "broadcast"):
            if self.agent is not None:
                self.agent.key = args[args.index("key") + 1]
            return "Broadcasting: Intent { }\nBroadcast completed: result=1\n"
        if args[:2] == ("am", "start"):
            return "Starting: Intent { }\nStatus: ok\nActivity: com.example/.Main\n"
        return ""


def key_file(tmp: Path) -> Path:
    path = tmp / "agent.key"
    if not path.exists():
        path.write_text(TEST_KEY + "\n")
        path.chmod(0o600)
    return path


def make_device(agent_server: FakeAgent, tmp: Path) -> DeviceManager:
    adb = FakeAdb(agent_server)
    client = AgentClient(adb, connect_timeout=3, key_path=key_file(tmp))
    client.fixed_address = agent_server.address
    dev = DeviceManager(adb, client, state_file=tmp / "sessions.json", log_dir=tmp)
    dev.scrcpy_processes = lambda: []  # type: ignore[method-assign]
    return dev


class FlowDevice:
    """Records what the flow engine asks for."""

    def __init__(self):
        self.calls = []

    def open_app(self, package, headless=True):
        self.calls.append(("open_app", package))
        return {"displayId": 3}

    def start_intent(self, package, spec):
        self.calls.append(("intent", package, spec))
        return "ok"

    def wait_for(self, selector, timeout_ms):
        self.calls.append(("wait", selector, timeout_ms))
        return {"id": "w1:0"}

    def tap_selector(self, selector):
        self.calls.append(("tap", selector))
        return {"performed": True}

    def type_selector(self, selector, text):
        self.calls.append(("type", selector, text))
        return {"performed": True}

    def press(self, package, key):
        self.calls.append(("press", package, key))
        return {"performed": True}

    def swipe(self, package, direction):
        self.calls.append(("swipe", package, direction))
        return {"performed": True}


# ---------------------------------------------------------------- protocol

class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer("droidrt", "0.0.1", "hello")

        def boom(args):
            raise ToolInputError("bad input")

        def crash(args):
            raise RuntimeError("kaput")

        self.server.set_tools([
            Tool("echo", "Echo", {"type": "object", "properties": {"x": {"type": "string"}}},
                 lambda a: a.get("x", ""), title="Echo"),
            Tool("img", "Image", {"type": "object"}, lambda a: [image_content("AAAA")]),
            Tool("boom", "Fails", {"type": "object"}, boom),
            Tool("crash", "Crashes", {"type": "object"}, crash),
        ])
        self.server.resources = {"droidrt://apps": Resource("droidrt://apps", "apps", "Apps", lambda: "[]")}

    def init(self, version="2025-06-18"):
        return self.server.handle_message(rpc(1, "initialize", init_params(version)))

    def test_initialize_and_negotiation(self):
        r = self.init()
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(r["result"]["capabilities"]["tools"]["listChanged"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "droidrt")
        self.assertEqual(r["result"]["instructions"], "hello")
        for v in ("2025-03-26", "2024-11-05"):
            self.assertEqual(self.init(v)["result"]["protocolVersion"], v)
        self.assertEqual(self.init("1999-01-01")["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_requires_version(self):
        r = self.server.handle_message(rpc(1, "initialize", {}))
        self.assertEqual(r["error"]["code"], INVALID_PARAMS)

    def test_ping_and_not_initialized(self):
        self.assertEqual(self.server.handle_message(rpc(1, "ping"))["result"], {})
        r = self.server.handle_message(rpc(2, "tools/list"))
        self.assertEqual(r["error"]["code"], INVALID_REQUEST)

    def test_tools_list(self):
        self.init()
        tools = self.server.handle_message(rpc(2, "tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["echo", "img", "boom", "crash"])
        self.assertEqual(tools[0]["title"], "Echo")
        self.assertIn("inputSchema", tools[0])
        self.init("2024-11-05")
        old = self.server.handle_message(rpc(3, "tools/list"))["result"]["tools"]
        self.assertNotIn("title", old[0])

    def test_tool_results(self):
        self.init()
        r = self.server.handle_message(rpc(2, "tools/call", {"name": "echo", "arguments": {"x": "hi"}}))
        self.assertEqual(r["result"], {"content": [{"type": "text", "text": "hi"}], "isError": False})
        r = self.server.handle_message(rpc(3, "tools/call", {"name": "img"}))
        self.assertEqual(r["result"]["content"][0], {"type": "image", "data": "AAAA", "mimeType": "image/png"})
        r = self.server.handle_message(rpc(4, "tools/call", {"name": "boom", "arguments": {}}))
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["content"][0]["text"], "bad input")
        r = self.server.handle_message(rpc(5, "tools/call", {"name": "crash", "arguments": {}}))
        self.assertTrue(r["result"]["isError"])

    def test_errors(self):
        self.init()
        self.assertEqual(self.server.handle_line("{nope")[0]["error"]["code"], PARSE_ERROR)
        self.assertIsNone(self.server.handle_line("{nope")[0]["id"])
        self.assertEqual(self.server.handle_message({"id": 1, "method": "ping"})["error"]["code"], INVALID_REQUEST)
        self.assertEqual(self.server.handle_message(rpc(1, "nope/nope"))["error"]["code"], METHOD_NOT_FOUND)
        r = self.server.handle_message(rpc(2, "tools/call", {"name": "missing"}))
        self.assertEqual(r["error"]["code"], INVALID_PARAMS)
        r = self.server.handle_message(rpc(3, "tools/call", {"name": "echo", "arguments": [1]}))
        self.assertEqual(r["error"]["code"], INVALID_PARAMS)
        r = self.server.handle_message(rpc(4, "tools/list", [1, 2]))
        self.assertEqual(r["error"]["code"], INVALID_PARAMS)
        r = self.server.handle_message({"jsonrpc": "2.0", "id": None, "method": "ping"})
        self.assertEqual(r["error"]["code"], INVALID_REQUEST)
        self.assertEqual(self.server.handle_line("[]")[0]["error"]["code"], INVALID_REQUEST)
        self.assertNotEqual(INTERNAL_ERROR, 0)

    def test_notifications_get_no_response(self):
        self.assertIsNone(self.server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                                      "params": {"requestId": 1}}))

    def test_resources(self):
        self.init()
        lst = self.server.handle_message(rpc(2, "resources/list"))["result"]["resources"]
        self.assertEqual(lst[0]["uri"], "droidrt://apps")
        r = self.server.handle_message(rpc(3, "resources/read", {"uri": "droidrt://apps"}))
        self.assertEqual(r["result"]["contents"][0], {"uri": "droidrt://apps", "mimeType": "application/json",
                                                       "text": "[]"})
        r = self.server.handle_message(rpc(4, "resources/read", {"uri": "droidrt://nothing"}))
        self.assertEqual(r["error"]["code"], RESOURCE_NOT_FOUND)
        self.assertEqual(self.server.handle_message(rpc(5, "resources/templates/list"))["result"],
                         {"resourceTemplates": []})

    def test_serve_stream_and_list_changed(self):
        lines = [rpc(1, "initialize", init_params()), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 rpc(2, "tools/call", {"name": "echo", "arguments": {"x": "a"}}), rpc(3, "ping")]
        out = io.StringIO()
        self.server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines) + "\n"), out)
        msgs = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(sorted(m["id"] for m in msgs), [1, 2, 3])
        for m in msgs:
            self.assertEqual(m["jsonrpc"], "2.0")
        # After initialized, changing the tool set notifies the client.
        self.server.set_tools([Tool("only", "Only", {"type": "object"}, lambda a: "x")])
        last = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(last, {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        before = out.getvalue()
        self.server.set_tools([Tool("only", "Only", {"type": "object"}, lambda a: "x")])
        self.assertEqual(out.getvalue(), before)  # same set, no notification


# ---------------------------------------------------------------- templates and validation

class TemplateTest(unittest.TestCase):
    def test_filters(self):
        args = {"phone": "+55 (11) 99999-0000", "text": "olá & bem?/x"}
        self.assertEqual(flows.render("https://wa.me/{phone|digits}?text={text|urlencode}", args),
                         "https://wa.me/5511999990000?text=ol%C3%A1%20%26%20bem%3F%2Fx")
        self.assertEqual(flows.render("{phone|digits|urlencode}", args), "5511999990000")
        self.assertEqual(flows.render("plain", args), "plain")

    def test_missing_and_types(self):
        self.assertEqual(flows.render("a{missing}b", {}), "ab")
        self.assertEqual(flows.render("{n}-{b}", {"n": 3, "b": True}), "3-true")
        with self.assertRaises(ToolInputError):
            flows.render("{x", {})
        with self.assertRaises(ToolInputError):
            flows.render("{x|shout}", {"x": "a"})
        with self.assertRaises(ToolInputError):
            flows.render("{x}", {"x": ["list"]})
        with self.assertRaises(ToolInputError):
            flows.render("{bad name}", {})

    def test_render_value_nested(self):
        v = flows.render_value({"a": ["{x}", {"b": "{x|digits}"}], "n": 1}, {"x": "a1b2"})
        self.assertEqual(v, {"a": ["a1b2", {"b": "12"}], "n": 1})

    def test_check_arguments(self):
        schema = {"type": "object", "properties": {"phone": {"type": "string"}, "n": {"type": "integer"}},
                  "required": ["phone"]}
        flows.check_arguments(schema, {"phone": "1"})
        for bad in ({}, {"phone": "1", "zzz": 1}, {"phone": 5}, {"phone": "1", "n": True}):
            with self.assertRaises(ToolInputError):
                flows.check_arguments(schema, bad)


class ValidateTest(unittest.TestCase):
    def test_package(self):
        self.assertEqual(validate.package({"package": "com.whatsapp"}), "com.whatsapp")
        for bad in ("", "1com", "com.x; rm -rf /", "com/x", "-p", "a" * 300, None, 5, "com.x\n"):
            with self.assertRaises(ToolInputError, msg=repr(bad)):
                validate.package({"package": bad})

    def test_intent(self):
        ok = validate.intent({"action": "android.intent.action.VIEW", "data": "https://x.y/?a=b c",
                              "extras": {"k.e_y": "v", "n": 3, "f": 1.5, "b": False}})
        self.assertEqual(ok["extras"]["n"], 3)
        bad_specs = [
            {"action": "x;reboot"}, {"data": "a\nb"}, {"action": "A", "extras": {"bad key": "v"}},
            {"action": "A", "extras": {"k": {"nested": 1}}}, {"component": "no-slash"}, {},
            {"action": "A", "mimeType": "text plain"}, {"action": "A", "categories": ["a b"]},
            {"action": "A", "package": "x y"}, {"action": "A", "extras": {"k": "x" * 5000}},
        ]
        for spec in bad_specs:
            with self.assertRaises(ToolInputError, msg=repr(spec)):
                validate.intent(spec)

    def test_selector_and_misc(self):
        validate.selector({"text": "a", "package": "com.a", "displayId": 2, "clickable": True})
        for bad in ({}, {"evil": 1}, {"text": ""}, {"displayId": "2"}, {"clickable": "yes"}, {"package": "x y"}):
            with self.assertRaises(ToolInputError):
                validate.selector(bad)
        with self.assertRaises(ToolInputError):
            validate.no_unknown({"a": 1, "b": 2}, {"a"})
        with self.assertRaises(ToolInputError):
            validate.integer({"n": 10**9}, "n", None, 0, 10)
        with self.assertRaises(ToolInputError):
            validate.node_id({"element_id": "w1:0; ls"})
        self.assertEqual(validate.node_id({"element_id": "w12:0.3.1"}), "w12:0.3.1")

    def test_adb_shell_quotes_every_argument(self):
        adb = Adb("emulator-5580", "/usr/bin/adb")
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            adb.shell("am", "start", "-d", "https://x/?a=1&b=$(reboot)")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["/usr/bin/adb", "-s", "emulator-5580", "shell"])
        self.assertEqual(argv[4], "am start -d 'https://x/?a=1&b=$(reboot)'")
        self.assertNotIn("shell", run.call_args[1])  # never shell=True
        self.assertIn("timeout", run.call_args[1])


# ---------------------------------------------------------------- parsers

class ParserTest(unittest.TestCase):
    DUMPSYS = """  Logical Displays: size=2
  Display 0:
    mDisplayId=0
    mPrimaryDisplayDevice=Built-in Screen(local:4619827259835644672)
    mIsEnabled=true
  Display 2:
    mDisplayId=2
    mPrimaryDisplayDevice=scrcpy(virtual:com.android.shell,2000,scrcpy,0)
"""
    SF = ('Display 4619827259835644672 (HWC display 0): port=0 pnpId=GGL displayName="EMU_display_0"\n'
          'Display 11529215049704637545 (Virtual display): displayName="scrcpy" '
          'uniqueId="virtual:com.android.shell,2000,scrcpy,0"\n')

    def test_display_mapping(self):
        logical = parse_logical_displays(self.DUMPSYS)
        self.assertEqual(logical, {0: "local:4619827259835644672", 2: "virtual:com.android.shell,2000,scrcpy,0"})
        sf = parse_sf_displays(self.SF)
        self.assertEqual(sf[logical[2]], "11529215049704637545")
        self.assertEqual(sf[logical[0]], "4619827259835644672")

    def test_scrcpy_ps(self):
        ps = ("  101 /opt/homebrew/bin/scrcpy -s emulator-5580 --new-display=720x1560/320 --start-app=com.whatsapp "
              "--window-title WhatsApp\n"
              "  102 /opt/homebrew/bin/scrcpy -s emulator-5580 --no-window --no-audio --start-app=com.a.b --x\n"
              "  103 /opt/homebrew/bin/scrcpy -s emulator-5554 --start-app=com.other\n"
              "  104 /usr/bin/grep scrcpy -s emulator-5580 --start-app=com.fake\n"
              "  105 /opt/homebrew/bin/scrcpy -s emulator-5580 --no-window --no-control --new-display=720x1560/320 "
              "--window-title=droidrt-headless:com.whatsapp\n")
        self.assertEqual(parse_scrcpy_ps(ps, "emulator-5580"), [
            {"pid": 101, "package": "com.whatsapp", "headless": False},
            {"pid": 102, "package": "com.a.b", "headless": True},
            {"pid": 105, "package": "com.whatsapp", "headless": True}])

    def test_compact_tree(self):
        agent = FakeAgent()
        try:
            text = compact_tree(agent.respond("tree", {}))
        finally:
            agent.shutdown()
            agent.server_close()
        self.assertIn("window w5:0 pkg=com.example display=2", text)
        self.assertIn('[w5:0.1] Button "Send" desc="Send" id=send {clickable} @600,1400,700,1500', text)
        self.assertIn("{clickable,editable,focused}", text)

    def test_png_size(self):
        self.assertEqual(png_size(PNG_1x1), (1, 1))
        with self.assertRaises(DeviceError):
            png_size(b"not a png")


# ---------------------------------------------------------------- agent client

class AgentClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeAgent()
        self.adb = FakeAdb(self.fake)
        self.client = AgentClient(self.adb, connect_timeout=3, key_path=key_file(Path(self.tmp.name)))
        self.client.fixed_address = self.fake.address

    def tearDown(self):
        self.client.close()
        self.fake.shutdown()
        self.fake.server_close()
        self.tmp.cleanup()

    def test_bad_proof_is_rejected(self):
        self.fake.bad_proof = True
        with self.assertRaises(AgentAuthError) as ctx:
            self.client.call("ping")
        self.assertIn("another process may own the socket", str(ctx.exception))
        self.assertEqual(self.fake.requests, [])  # nothing is sent to an unauthenticated peer

    def test_wrong_key_on_device_is_replaced(self):
        self.fake.key = "11" * 32
        self.assertEqual(self.client.call("ping")["sdk"], 37)
        self.assertEqual(self.fake.key, TEST_KEY)

    def test_key_rejected_twice_fails(self):
        self.fake.key = "11" * 32
        self.adb.agent = None  # the broadcast does not reach the fake agent
        with self.assertRaises(AgentAuthError):
            self.client.call("ping")

    def test_outdated_agent(self):
        self.fake.hello_version = 1
        self.client.connect_timeout = 1
        with self.assertRaises(DeviceError) as ctx:
            self.client.call("ping")
        self.assertIn("protocol version 1", str(ctx.exception))
        self.assertEqual(self.fake.requests, [])

    def test_missing_key_is_delivered_once(self):
        self.fake.key = None
        self.assertEqual(self.client.call("ping")["sdk"], 37)
        self.assertEqual(self.fake.key, TEST_KEY)
        broadcasts = [c for c in self.adb.calls if c[:3] == ("shell", "am", "broadcast")]
        self.assertEqual(broadcasts, [("shell", "am", "broadcast", "-a", "dev.droidrt.agent.SET_KEY", "-n",
                                       "dev.droidrt.agent/.KeyReceiver", "--es", "key", TEST_KEY)])

    def test_key_file_is_created_private(self):
        path = Path(self.tmp.name) / "sub" / "agent.key"
        key = load_or_create_key(path)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(load_or_create_key(path), key)

    def test_call_and_error(self):
        self.assertEqual(self.client.call("ping")["sdk"], 37)
        with self.assertRaises(AgentError):
            self.client.call("action", {"nodeId": "w9:9", "action": "click"})

    def test_reconnect_for_idempotent_commands(self):
        self.client.call("ping")
        self.fake.drop_next = True
        self.assertEqual(len(self.client.call("displays")), 2)

    def test_no_resend_for_side_effects(self):
        self.client.call("ping")
        self.fake.drop_next = True
        with self.assertRaises(DeviceError):
            self.client.call("tap", {"displayId": 2, "x": 1, "y": 1})
        taps = [r for r in self.fake.requests if r["cmd"] == "tap"]
        self.assertEqual(len(taps), 1)

    def test_ensure_enabled_appends_service(self):
        self.client.ensure_enabled()
        self.assertEqual(self.adb.settings["enabled_accessibility_services"],
                         "com.other/.Svc:dev.droidrt.agent/.AgentService")
        self.assertEqual(self.adb.settings["accessibility_enabled"], "1")
        puts = [c for c in self.adb.calls if c[:4] == ("shell", "settings", "put", "secure")]
        self.client.ensure_enabled()  # already enabled: nothing is written again
        self.assertEqual(puts, [c for c in self.adb.calls if c[:4] == ("shell", "settings", "put", "secure")])
        self.assertIn(("shell", "cmd", "notification", "allow_listener",
                       "dev.droidrt.agent/dev.droidrt.agent.NotifyService"), self.adb.calls)


class ApkCacheTest(unittest.TestCase):
    def test_pull_once_per_version(self):
        from droidrt_mcp.apks import ApkCache

        class PullAdb(FakeAdb):
            def shell(self, *args, timeout=30, check=True):
                self.calls.append(("shell",) + args)
                if args[:2] == ("dumpsys", "package"):
                    return "    versionCode=42 minSdk=21\n"
                if args[:2] == ("pm", "path"):
                    return "package:/data/app/x/base.apk\npackage:/data/app/x/split_config.arm64_v8a.apk\n"
                return "1\n"

            def run(self, *args, timeout=30, check=True, binary=False, input_data=None):
                self.calls.append(args)
                if args[0] == "pull":
                    Path(args[2]).write_bytes(b"PK")
                return ""

        with tempfile.TemporaryDirectory() as tmp:
            adb = PullAdb()
            stale = Path(tmp) / "com.a" / "41"
            stale.mkdir(parents=True)
            cache = ApkCache(adb, Path(tmp))
            first = cache.base_apks("com.a")
            self.assertEqual(first, [Path(tmp) / "com.a" / "42" / "base.apk"])
            self.assertFalse(stale.exists())
            self.assertEqual(cache.base_apks("com.a"), first)
            self.assertEqual(len([c for c in adb.calls if c[0] == "pull"]), 1)
            self.assertEqual([c for c in adb.calls if c[0] == "pull"][0][1], "/data/app/x/base.apk")
            self.assertIsNone(cache.base_apks("../etc"))


# ---------------------------------------------------------------- device, tools and flows

class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.fake = FakeAgent()
        self.device = make_device(self.fake, self.tmpdir)
        self.device.sessions["com.example"] = {"pid": 1, "displayId": 2, "proc": mock.Mock(poll=lambda: None)}
        self.tools = Tools(self.device, flows_base=self.tmpdir / "flows")

    def tearDown(self):
        self.device.agent.close()
        self.fake.shutdown()
        self.fake.server_close()
        self.tmp.cleanup()

    def cmds(self, name):
        return [r["args"] for r in self.fake.requests if r["cmd"] == name]

    def test_tap_variants(self):
        self.tools.tap({"package": "com.example", "text": "send"})  # falls back to textContains
        self.assertEqual(self.cmds("action")[-1], {"nodeId": "w5:0.1", "action": "click"})
        self.tools.tap({"package": "com.example", "resource_id": "send"})
        self.tools.tap({"package": "com.example", "x": 10, "y": 20})
        self.assertEqual(self.cmds("tap")[-1], {"displayId": 2, "x": 10, "y": 20})
        for bad in ({"package": "com.example"}, {"package": "com.example", "text": "a", "x": 1, "y": 2},
                    {"package": "com.example", "x": 1}, {"package": "com.example", "evil": 1}):
            with self.assertRaises(ToolInputError, msg=repr(bad)):
                self.tools.tap(bad)

    def test_type_text_uses_focused_field(self):
        res = self.tools.type_text({"package": "com.example", "text": "hello", "submit": True})
        self.assertEqual(self.fake.text, "hello")
        self.assertTrue(res["submitted"])
        self.assertIn({"nodeId": "w5:0.0", "action": "ime_enter"}, self.cmds("action"))

    def test_swipe_and_find_and_wait(self):
        self.tools.swipe({"package": "com.example", "direction": "up"})
        s = self.cmds("swipe")[-1]
        self.assertEqual((s["displayId"], s["x1"], s["x2"]), (2, 360, 360))
        self.assertGreater(s["y1"], s["y2"])
        found = self.tools.find_elements({"package": "com.example", "resource_id": "send"})
        self.assertEqual(found["count"], 1)
        self.assertEqual(self.tools.wait_for({"package": "com.example", "text": "SEND", "timeout_ms": 500})["id"],
                         "w5:0.1")
        with self.assertRaises(ToolInputError):
            self.tools.wait_for({"package": "com.example", "text": "nothing", "timeout_ms": 300})

    def test_press_key_targets_display(self):
        self.tools.press_key({"package": "com.example", "key": "back"})
        self.assertIn(("shell", "input", "-d", "2", "keyevent", "KEYCODE_BACK"), self.device.adb.calls)
        with self.assertRaises(ToolInputError):
            self.tools.press_key({"package": "com.example", "key": "power"})

    def test_reply_notification(self):
        res = self.tools.reply_notification({"key": "0|com.example|1", "text": "ok"})
        self.assertEqual(self.cmds("notification_action")[-1], {"key": "0|com.example|1", "index": 1, "text": "ok"})
        self.assertEqual(res["action"], "Reply")
        with self.assertRaises(ToolInputError):
            self.tools.reply_notification({"key": "missing", "text": "ok"})

    def test_run_intent_argv(self):
        self.tools.run_intent({"package": "com.example", "action": "android.intent.action.SEND",
                               "mime_type": "text/plain", "extras": {"android.intent.extra.TEXT": "a b; c",
                                                                     "n": 7, "flag": True}})
        call = next(c for c in self.device.adb.calls if c[:3] == ("shell", "am", "start"))
        self.assertEqual(list(call[1:]), [
            "am", "start", "-W", "--display", "2", "-a", "android.intent.action.SEND", "-t", "text/plain",
            "-p", "com.example", "--es", "android.intent.extra.TEXT", "a b; c", "--ei", "n", "7",
            "--ez", "flag", "true"])

    def test_get_screen_without_screenshot(self):
        content = self.tools.get_screen({"package": "com.example", "include_screenshot": False})
        self.assertEqual(len(content), 1)
        self.assertIn("[w5:0.1] Button", content[0]["text"])

    def test_get_screen_with_screenshot(self):
        with mock.patch.object(DeviceManager, "screenshot", return_value=("QUJD", {
                "displayId": 2, "width": 720, "height": 1560, "imageWidth": 473, "imageHeight": 1024,
                "scale": 1.5234})):
            content = self.tools.get_screen({"package": "com.example"})
        self.assertEqual(content[1], {"type": "image", "data": "QUJD", "mimeType": "image/png"})
        self.assertIn("multiply screenshot coordinates by 1.5234", content[0]["text"])

    def test_semantic_flow_against_fake_agent(self):
        definition = {
            "name": "example_send", "description": "Send", "package": "com.example",
            "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
            "action": {"type": "flow", "steps": [
                {"wait": {"resourceId": "input", "timeoutMs": 1000}},
                {"type": {"selector": {"resourceId": "input"}, "text": "{msg}"}},
                {"tap": {"contentDesc": "Send"}},
                {"press": "back"},
                {"sleep": 0},
            ]}}
        out = self.tools.run_semantic(definition, {"msg": "hi there"})
        self.assertIn("flow finished", out)
        self.assertEqual(self.fake.text, "hi there")
        self.assertIn({"package": "com.example", "contentDesc": "Send"}, self.cmds("find"))
        with self.assertRaises(ToolInputError):
            self.tools.run_semantic(definition, {})

    def test_flow_stops_on_failure(self):
        definition = {"name": "x_y", "description": "d", "package": "com.example",
                      "inputSchema": {"type": "object", "properties": {}},
                      "action": {"type": "flow", "steps": [{"tap": {"text": "Missing"}}, {"press": "back"}]}}
        with self.assertRaises(ToolInputError) as ctx:
            self.tools.run_semantic(definition, {})
        self.assertIn("0: tap: failed", str(ctx.exception))
        self.assertFalse([c for c in self.device.adb.calls if "keyevent" in c])


class FlowEngineTest(unittest.TestCase):
    def test_steps_and_rendering(self):
        dev = FlowDevice()
        slept = []
        steps = [
            {"open_app": "com.whatsapp"},
            {"intent": {"type": "intent", "action": "android.intent.action.VIEW",
                        "data": "https://wa.me/{phone|digits}?text={text|urlencode}", "package": "com.whatsapp",
                        "extras": {"k": "{text}"}, "categories": ["android.intent.category.BROWSABLE"]}},
            {"wait": {"contentDesc": "Send", "timeoutMs": 15000}},
            {"tap": {"contentDesc": "Send"}},
            {"type": {"selector": {"className": "EditText"}, "text": "{text}"}},
            {"press": "enter"}, {"swipe": {"direction": "down"}}, {"sleep": 250},
        ]
        log = flows.run_flow(dev, "com.whatsapp", steps, {"phone": "+1 (555) 010", "text": "a b"}, sleep=slept.append)
        self.assertEqual(len(log), len(steps))
        intent = dev.calls[1][2]
        self.assertEqual(intent["data"], "https://wa.me/1555010?text=a%20b")
        self.assertEqual(intent["extras"], {"k": "a b"})
        self.assertEqual(dev.calls[2], ("wait", {"contentDesc": "Send", "package": "com.whatsapp"}, 15000))
        self.assertEqual(dev.calls[4], ("type", {"className": "EditText", "package": "com.whatsapp"}, "a b"))
        self.assertEqual(slept, [0.25])

    def test_bad_steps(self):
        for steps in ([], [{"press": "power"}], [{"sleep": -1}], [{"rm": 1}], [{"a": 1, "b": 2}],
                      [{"wait": {"text": "x", "timeoutMs": 0}}], [{"open_app": "x;y"}],
                      [{"intent": {"action": "x;reboot"}}], [{"tap": {"evil": "x"}}]):
            with self.assertRaises(ToolInputError, msg=repr(steps)):
                flows.run_flow(FlowDevice(), "com.a", steps, {})

    def test_intent_action_injection_is_rejected(self):
        with self.assertRaises(ToolInputError):
            flows.run_intent_action(FlowDevice(), "com.a", {"type": "intent", "action": "{a}"}, {"a": "x y"})


class RecordFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_expose(self):
        steps = [{"open_app": "com.whatsapp"}, {"wait": {"textContains": "{who}", "timeoutMs": 5000}},
                 {"tap": {"textContains": "{who}"}}]
        path = flows.save_flow("com.whatsapp", "open_contact", "Open a contact", steps, base=self.base)
        self.assertEqual(path, self.base / "com.whatsapp" / "open_contact.json")
        data = json.loads(path.read_text())
        self.assertEqual(data["inputSchema"]["required"], ["who"])

        def loader(pkg):
            from semantic import flows as sflows
            return sflows.load_flows(pkg, "whatsapp", base=self.base)[0]

        tools = Tools(mock.Mock(), semantic_loader=loader, flows_base=self.base)
        tools.on_flows_changed = lambda pkg: tools.load_semantic([pkg])
        res = tools.record_flow({"package": "com.whatsapp", "name": "say_hi", "description": "Say hi",
                                 "steps": [{"press": "back"}]})
        self.assertIn("whatsapp_say_hi", res["tools"])
        self.assertIn("whatsapp_open_contact", res["tools"])
        self.assertIn("whatsapp_say_hi", [t.name for t in tools.all()])

    def test_rejects_bad_names_and_steps(self):
        for name in ("../evil", "Bad", "", "a" * 60, "has-dash", None):
            with self.assertRaises(ToolInputError, msg=repr(name)):
                flows.save_flow("com.whatsapp", name, "d", [{"press": "back"}], base=self.base)
        with self.assertRaises(ToolInputError):
            flows.save_flow("com.whatsapp", "ok", "d", [{"explode": 1}], base=self.base)
        with self.assertRaises(ToolInputError):
            flows.save_flow("../x", "ok", "d", [{"press": "back"}], base=self.base)
        self.assertFalse(any(self.base.rglob("*.json")))


# ---------------------------------------------------------------- security hardening

class PackagePolicyTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(validate.ALLOWLIST_ENV, None)

    def test_blocked_by_default(self):
        self.assertEqual(validate.BLOCKED_PACKAGES, {
            "com.android.settings", "com.google.android.packageinstaller", "com.android.packageinstaller",
            "com.android.vending", "com.android.shell", "com.android.systemui", "dev.droidrt.agent"})
        for pkg in validate.BLOCKED_PACKAGES:
            with self.assertRaises(ToolInputError, msg=pkg) as ctx:
                validate.package({"package": pkg})
            self.assertIn(validate.ALLOWLIST_ENV, str(ctx.exception))
        self.assertEqual(validate.package({"package": "com.whatsapp"}), "com.whatsapp")

    def test_allowlist(self):
        os.environ[validate.ALLOWLIST_ENV] = " com.whatsapp , com.android.settings,bad pkg, "
        self.assertEqual(validate.allowed_packages(), {"com.whatsapp", "com.android.settings"})
        self.assertEqual(validate.package({"package": "com.android.settings"}), "com.android.settings")
        self.assertEqual(validate.package({"package": "com.whatsapp"}), "com.whatsapp")
        for pkg in ("com.example", "com.android.vending"):
            with self.assertRaises(ToolInputError):
                validate.package({"package": pkg})
        os.environ[validate.ALLOWLIST_ENV] = "not valid,;"
        self.assertEqual(validate.allowed_packages(), frozenset())
        with self.assertRaises(ToolInputError):
            validate.package({"package": "com.whatsapp"})
        os.environ[validate.ALLOWLIST_ENV] = "  "
        self.assertIsNone(validate.allowed_packages())

    def test_package_regex_is_ascii_and_bounded(self):
        for bad in ("com.café", "com.١٢", "a" * 256, "com.a\n"):
            self.assertIsNone(validate.PACKAGE_RE.fullmatch(bad), repr(bad))
            with self.assertRaises(ToolInputError):
                validate.package({"package": bad})
        self.assertTrue(validate.PACKAGE_RE.fullmatch("a" * 255))

    def test_intent_target(self):
        ok = validate.intent({"component": "com.a/.Main"}, target="com.a")
        self.assertEqual(ok["component"], "com.a/.Main")
        validate.intent({"component": "com.a/com.lib.Activity"}, target="com.a")
        for spec in ({"component": "com.android.settings/.Settings"}, {"component": "com.ab/.Main"},
                     {"action": "a.b", "package": "com.b"}):
            with self.assertRaises(ToolInputError, msg=repr(spec)):
                validate.intent(spec, target="com.a")


class HardenedToolsTest(ToolsTest):
    """Reuses the ToolsTest fixtures; the inherited tests run again, which is cheap."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(validate.ALLOWLIST_ENV, None)

    def test_every_package_tool_applies_the_policy(self):
        calls = [
            (self.tools.open_app, {}), (self.tools.close_app, {}), (self.tools.get_screen, {}),
            (self.tools.find_elements, {"text": "a"}), (self.tools.tap, {"text": "a"}),
            (self.tools.type_text, {"text": "a"}), (self.tools.swipe, {"direction": "up"}),
            (self.tools.press_key, {"key": "back"}), (self.tools.wait_for, {"text": "a"}),
            (self.tools.run_intent, {"action": "android.intent.action.VIEW"}),
            (self.tools.get_notifications, {}),
            (self.tools.record_flow, {"name": "x", "description": "d", "steps": [{"press": "back"}]}),
        ]
        before = len(self.fake.requests), len(self.device.adb.calls)
        for fn, extra in calls:
            with self.assertRaises(ToolInputError, msg=fn.__name__):
                fn({"package": "com.android.settings", **extra})
        self.assertEqual((len(self.fake.requests), len(self.device.adb.calls)), before)
        os.environ[validate.ALLOWLIST_ENV] = "com.whatsapp"
        with self.assertRaises(ToolInputError):
            self.tools.swipe({"package": "com.example", "direction": "up"})

    def test_run_intent_component_must_match(self):
        for comp in ("com.android.settings/.Settings", "com.examplex/.Main"):
            with self.assertRaises(ToolInputError):
                self.tools.run_intent({"package": "com.example", "component": comp})
        self.tools.run_intent({"package": "com.example", "component": "com.example/.Main"})
        call = [c for c in self.device.adb.calls if c[:3] == ("shell", "am", "start")][-1]
        self.assertIn("com.example/.Main", call)

    def test_device_start_intent_checks_target(self):
        for spec in ({"component": "com.android.settings/.Settings"}, {"action": "a.b", "package": "com.other"}):
            with self.assertRaises(ToolInputError):
                self.device.start_intent("com.example", spec)
        self.assertFalse([c for c in self.device.adb.calls if c[:3] == ("shell", "am", "start")])

    def test_list_apps_hides_blocked_packages(self):
        adb = self.device.adb
        listing = "package:com.example\npackage:com.android.settings\npackage:dev.droidrt.agent\n"
        launch = ("com.example/.Main\ncom.android.settings/.Settings\ncom.android.vending/.Main\n"
                  "dev.droidrt.agent/.Main\n")
        orig = adb.shell

        def shell(*args, timeout=30, check=True):
            if args[:3] == ("pm", "list", "packages"):
                return listing
            if args[:2] == ("cmd", "package"):
                return launch
            return orig(*args, timeout=timeout, check=check)
        adb.shell = shell
        self.assertEqual([a["package"] for a in self.tools.list_apps({})], ["com.example"])
        os.environ[validate.ALLOWLIST_ENV] = "com.example,com.android.vending,com.android.settings"
        self.assertEqual([a["package"] for a in self.tools.list_apps({})],
                         ["com.android.settings", "com.android.vending", "com.example"])

    def test_notifications_are_filtered_and_capped(self):
        many = [{"key": f"k{i}", "package": "com.example", "title": "t", "text": "x" * 5000,
                 "actions": [{"index": 0, "title": "Reply", "remoteInput": True}]} for i in range(80)]
        many.insert(0, {"key": "sys", "package": "com.android.systemui", "title": "USB", "text": "",
                        "actions": [{"index": 0, "title": "Reply", "remoteInput": True}]})
        with mock.patch.object(self.device.agent, "call", return_value=many):
            res = self.tools.get_notifications({})
            self.assertNotIn("sys", [n["key"] for n in res["notifications"]])
            self.assertLessEqual(len(res["notifications"]), MAX_NOTIFICATIONS)
            self.assertTrue(res["truncated"])
            self.assertEqual(res["count"], 80)
            self.assertLessEqual(len(json.dumps(res)), 110_000)
            self.assertTrue(all(len(n["text"]) <= MAX_NOTIFICATION_FIELD + 3 for n in res["notifications"]))
            with self.assertRaises(ToolInputError):
                self.tools.reply_notification({"key": "sys", "text": "x"})
        small = limit_notifications([{"key": "a", "package": "com.example", "text": "hi"}])
        self.assertEqual(small, {"count": 1, "notifications": [{"key": "a", "package": "com.example",
                                                                 "text": "hi"}]})

    def test_annotations(self):
        tools = {t.name: t for t in self.tools.generic()}
        for name in ("list_apps", "get_screen", "find_elements", "wait_for", "get_notifications"):
            self.assertIs(tools[name].annotations["readOnlyHint"], True, name)
        for name in ("reply_notification", "run_intent", "record_flow", "type_text", "tap"):
            ann = tools[name].annotations
            self.assertIs(ann["readOnlyHint"], False, name)
            self.assertIs(ann["destructiveHint"], True, name)
        allowed = {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        for t in tools.values():
            self.assertLessEqual(set(t.annotations), allowed, t.name)
            self.assertEqual(t.annotations["title"], t.title)
            self.assertTrue(all(isinstance(v, bool) for k, v in t.annotations.items() if k != "title"))
        _, curated = __import__("semantic").load_curated("com.whatsapp")
        sem = {d["name"]: self.tools._semantic_tool(d).annotations for d in curated}
        self.assertTrue(sem["whatsapp_send_message"]["destructiveHint"])
        self.assertTrue(sem["whatsapp_share_text"]["destructiveHint"])
        self.assertFalse(sem["whatsapp_open_chat"]["destructiveHint"])
        server = McpServer("droidrt", "0")
        server.set_tools(tools.values())
        server.handle_message(rpc(1, "initialize", init_params()))
        listed = {t["name"]: t for t in server.handle_message(rpc(2, "tools/list"))["result"]["tools"]}
        self.assertTrue(listed["get_screen"]["annotations"]["readOnlyHint"])
        server.handle_message(rpc(3, "initialize", init_params("2024-11-05")))
        old = server.handle_message(rpc(4, "tools/list"))["result"]["tools"]
        self.assertNotIn("annotations", old[0])

    def test_semantic_tools_skip_blocked_and_foreign_packages(self):
        defs = {"com.android.settings": [{"name": "settings_x", "package": "com.android.settings"}],
                "com.example": [{"name": "example_ok", "package": "com.example"},
                                {"name": "example_bad", "package": "com.other"}]}
        self.tools.semantic_loader = lambda pkg: defs[pkg]
        names = [t.name for t in self.tools.load_semantic(["com.android.settings", "com.example"])]
        self.assertEqual(names, ["example_ok"])

    def test_semantic_flow_cannot_escape(self):
        base = {"name": "x_y", "description": "d", "package": "com.example",
                "inputSchema": {"type": "object", "properties": {}}}
        for steps in ([{"open_app": "com.android.settings"}],
                      [{"intent": {"type": "intent", "component": "com.android.settings/.Settings"}}],
                      [{"tap": {"text": "OK", "package": "com.android.settings"}}],
                      [{"tap": {"text": "OK", "displayId": 0}}]):
            with self.assertRaises(ToolInputError, msg=repr(steps)):
                self.tools.run_semantic({**base, "action": {"type": "flow", "steps": steps}}, {})
        with self.assertRaises(ToolInputError):
            self.tools.run_semantic({**base, "action": {"type": "intent", "action": "a.b",
                                                        "package": "com.android.settings"}}, {})
        with self.assertRaises(ToolInputError):
            self.tools.run_semantic({**base, "package": "com.android.settings",
                                     "action": {"type": "flow", "steps": [{"press": "back"}]}}, {})


class FlowScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_with_package_overrides(self):
        dev = FlowDevice()
        flows.run_flow(dev, "com.a", [{"tap": {"text": "x"}}], {})
        self.assertEqual(dev.calls[-1], ("tap", {"text": "x", "package": "com.a"}))
        self.assertEqual(flows._with_package({"text": "x", "package": "com.evil", "displayId": 0}, "com.a"),
                         {"text": "x", "package": "com.a"})

    def test_runtime_rejects_foreign_targets(self):
        for steps in ([{"open_app": "com.b"}], [{"intent": {"component": "com.b/.Main"}}],
                      [{"intent": {"action": "a.b", "package": "com.b"}}]):
            dev = FlowDevice()
            with self.assertRaises(ToolInputError, msg=repr(steps)):
                flows.run_flow(dev, "com.a", steps, {})
            self.assertEqual(dev.calls, [])
        with self.assertRaises(ToolInputError):
            flows.run_intent_action(FlowDevice(), "com.a", {"type": "intent", "component": "{c}"},
                                    {"c": "com.b/.Main"})

    def test_record_flow_rejects_escapes(self):
        bad = [
            [{"open_app": "com.android.settings"}],
            [{"intent": {"action": "a.b", "package": "com.android.settings"}}],
            [{"intent": {"component": "com.android.settings/.Settings"}}],
            [{"tap": {"text": "OK", "package": "com.whatsapp"}}],
            [{"tap": {"text": "OK", "displayId": 0}}],
            [{"wait": {"text": "OK", "package": "com.other"}}],
            [{"type": {"selector": {"text": "a", "displayId": 1}, "text": "x"}}],
        ]
        tools = Tools(mock.Mock(), flows_base=self.base)
        for steps in bad:
            with self.assertRaises(ToolInputError, msg=repr(steps)):
                tools.record_flow({"package": "com.whatsapp", "name": "x", "description": "d", "steps": steps})
        self.assertFalse(any(self.base.rglob("*.json")))

    def test_record_flow_returns_relative_path(self):
        tools = Tools(mock.Mock(), flows_base=self.base)
        res = tools.record_flow({"package": "com.whatsapp", "name": "say_hi", "description": "d",
                                 "steps": [{"press": "back"}]})
        self.assertEqual(res["saved"], "com.whatsapp/say_hi.json")
        self.assertTrue((self.base / "com.whatsapp" / "say_hi.json").is_file())


class RedactionTest(unittest.TestCase):
    def test_redact(self):
        home = str(Path.home())
        self.assertEqual(redact(f"cannot open {home}/Library/x"), "cannot open ~/Library/x")
        self.assertEqual(redact("plain"), "plain")

    def test_errors_sent_to_the_client_are_redacted(self):
        home = str(Path.home())
        server = McpServer("droidrt", "0")

        def input_error(a):
            raise ToolInputError(f"bad file {home}/flows/x.json")

        def device_error(a):
            raise DeviceError(f"agent APK missing and no build script at {home}/src/agent-apk/build.sh")

        def rpc_error(a):
            raise RpcError(INTERNAL_ERROR, f"boom in {home}/x")

        server.set_tools([Tool("a", "a", {"type": "object"}, input_error),
                          Tool("b", "b", {"type": "object"}, device_error),
                          Tool("c", "c", {"type": "object"}, rpc_error)])
        server.resources = {"droidrt://r": Resource("droidrt://r", "r", "r", lambda: input_error({}))}
        server.handle_message(rpc(1, "initialize", init_params()))
        for i, name in enumerate("abc", 2):
            out = json.dumps(server.handle_message(rpc(i, "tools/call", {"name": name, "arguments": {}})))
            self.assertNotIn(home, out, name)
            self.assertIn("~/", out, name)
        out = json.dumps(server.handle_message(rpc(9, "resources/read", {"uri": "droidrt://r"})))
        self.assertNotIn(home, out)


class ProtocolLimitsTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer("droidrt", "0")
        self.server.set_tools([Tool("echo", "e", {"type": "object"}, lambda a: "ok")])
        self.server.handle_message(rpc(1, "initialize", init_params("2025-03-26")))

    def test_batch_limit(self):
        ok = self.server.handle_line(json.dumps([rpc(i, "ping") for i in range(MAX_BATCH)]))
        self.assertEqual(len(ok[0]), MAX_BATCH)
        big = self.server.handle_line(json.dumps([rpc(i, "ping") for i in range(MAX_BATCH + 1)]))
        self.assertEqual(big[0]["error"]["code"], INVALID_REQUEST)
        self.assertIn("batch too large", big[0]["error"]["message"])

    def test_server_busy_when_the_queue_is_full(self):
        self.server._pending = MAX_PENDING
        out = io.StringIO()
        lines = [rpc(5, "tools/call", {"name": "echo"}), rpc(6, "ping")]
        self.server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out)
        msgs = {m["id"]: m for m in map(json.loads, out.getvalue().splitlines())}
        self.assertEqual(msgs[5]["error"], {"code": SERVER_BUSY, "message": "server busy"})
        self.assertEqual(msgs[6]["result"], {})

    def test_pending_counter_is_released(self):
        out = io.StringIO()
        lines = [rpc(i, "tools/call", {"name": "echo"}) for i in range(10, 20)]
        self.server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out)
        self.assertEqual(len(out.getvalue().splitlines()), 10)
        self.assertEqual(self.server._pending, 0)


class AgentHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeAgent()
        self.client = AgentClient(FakeAdb(self.fake), connect_timeout=3, key_path=key_file(Path(self.tmp.name)))
        self.client.fixed_address = self.fake.address

    def tearDown(self):
        self.client.close()
        self.fake.shutdown()
        self.fake.server_close()
        self.tmp.cleanup()

    def test_malformed_proofs_are_auth_errors(self):
        for proof in ("é" * 64, "a" * 63, "a" * 65, 12345, None):
            with self.subTest(proof=proof):
                with mock.patch.object(FakeAgentHandler, "handle", _handler_with_proof(proof)):
                    with self.assertRaises(AgentAuthError):
                        self.client.call("ping")
                self.client.close()

    def test_key_dir_is_tightened_and_write_is_atomic(self):
        folder = Path(self.tmp.name) / "keys"
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        path = folder / "agent.key"
        tmp = folder / "agent.key.tmp"
        tmp.write_text("stale")
        key = load_or_create_key(path)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(tmp.exists())
        self.assertEqual(path.read_text().strip(), key)

    def test_planted_tmp_symlink_is_not_followed(self):
        folder = Path(self.tmp.name) / "keys2"
        folder.mkdir()
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("keep")
        (folder / "agent.key.tmp").symlink_to(victim)
        load_or_create_key(folder / "agent.key")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((folder / "agent.key").is_symlink())


def _handler_with_proof(proof):
    def handle(self):
        nonce = secrets.token_hex(16)
        self.wfile.write((json.dumps({"hello": "droidrt-agent", "v": 2, "nonce": nonce}) + "\n").encode())
        self.wfile.flush()
        self.rfile.readline()
        self.wfile.write((json.dumps({"ok": True, "proof": proof}) + "\n").encode())
        self.wfile.flush()
        self.rfile.readline()
    return handle


# ---------------------------------------------------------------- end to end

class StdioClient:
    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "droidrt_mcp"], cwd=str(ROOT), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1)
        self.next_id = 0

    def request(self, method, params=None, timeout=120):
        self.next_id += 1
        msg = rpc(self.next_id, method, params)
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            resp = json.loads(line)
            if resp.get("id") == self.next_id:
                return resp
        raise TimeoutError(method)

    def call(self, name, **args):
        resp = self.request("tools/call", {"name": name, "arguments": args})
        if "error" in resp:
            raise AssertionError(resp["error"])
        result = resp["result"]
        if result.get("isError"):
            raise AssertionError(result["content"][0]["text"])
        return result["content"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=30)
        self.proc.stdout.close()


@unittest.skipUnless(os.environ.get("DROIDRT_E2E") == "1", "set DROIDRT_E2E=1 to drive the real VM")
class EndToEndTest(unittest.TestCase):
    PKG = "com.whatsapp"

    def test_whatsapp_headless(self):
        client = StdioClient()
        try:
            init = client.request("initialize", init_params())
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            client.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            client.proc.stdin.flush()
            names = [t["name"] for t in client.request("tools/list")["result"]["tools"]]
            self.assertIn("get_screen", names)

            apps = json.loads(client.call("list_apps")[0]["text"])
            self.assertIn(self.PKG, [a["package"] for a in apps])

            opened = json.loads(client.call("open_app", package=self.PKG, headless=True)[0]["text"])
            self.assertIsInstance(opened["displayId"], int)
            time.sleep(3)

            screen = client.call("get_screen", package=self.PKG, include_screenshot=True)
            self.assertEqual(screen[1]["type"], "image")
            png = base64.b64decode(screen[1]["data"])
            w, h = png_size(png)
            self.assertLessEqual(max(w, h), 1024)
            out_dir = Path(os.environ.get("DROIDRT_E2E_OUT") or tempfile.mkdtemp(prefix="droidrt-e2e-"))
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "whatsapp.png").write_bytes(png)
            (out_dir / "whatsapp.txt").write_text(screen[0]["text"])
            print(f"\nscreenshot saved to {out_dir / 'whatsapp.png'}", file=sys.stderr)

            found = json.loads(client.call("find_elements", package=self.PKG, clickable=True)[0]["text"])
            self.assertGreaterEqual(found["count"], 1)

            client.call("press_key", package=self.PKG, key="back")
            closed = json.loads(client.call("close_app", package=self.PKG)[0]["text"])
            self.assertTrue(closed["closed"])
            apps = json.loads(client.call("list_apps")[0]["text"])
            self.assertFalse(next(a for a in apps if a["package"] == self.PKG)["headless"])
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
