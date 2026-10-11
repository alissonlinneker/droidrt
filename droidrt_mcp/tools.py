"""MCP tool definitions: generic UI tools, semantic tools and record_flow."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import flows, validate
from .device import DeviceManager, compact_tree, node_summary
from .protocol import Tool, ToolInputError, image_content, text_content

log = logging.getLogger("droidrt_mcp.tools")

PKG = {"type": "string", "description": "Android package name, for example com.whatsapp", "pattern": r"^[a-zA-Z][\w.]*$",
       "maxLength": validate.MAX_PACKAGE}
MAX_FIND_RESULTS = 100
MAX_NOTIFICATIONS = 50
MAX_NOTIFICATION_FIELD = 2000
MAX_NOTIFICATIONS_TEXT = 100_000

# ToolAnnotations (MCP 2025-06-18). They are hints for the client, never a
# security boundary: read only tools only observe the VM; tools that can send,
# submit or overwrite something are marked destructive.
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
LOCAL_SESSION = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
UI_INPUT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
MAY_SEND = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}
SEND_WORDS = ("send", "share", "reply", "post", "delete", "remove", "pay", "buy", "call", "submit")


def annotations(title: str, hints: dict) -> dict:
    return {"title": title, **hints}


def semantic_annotations(d: dict) -> dict:
    """Hints for semantic tools. A flow can tap or type anything and an intent
    named after sending or sharing can reach other people, so those are
    destructive; the rest (open a screen, a deep link, a draft) only change
    what the app shows."""
    action = d.get("action") or {}
    name = str(d.get("name") or "")
    words = set(name.lower().split("_"))
    if action.get("type") == "flow" or words & set(SEND_WORDS) or str(action.get("action") or "").endswith(
            ("SEND", "SEND_MULTIPLE", "SENDTO")):
        hints = MAY_SEND
    else:
        hints = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
    return annotations(name, hints)


def _clip_text(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


def limit_notifications(items: Any) -> dict:
    """At most MAX_NOTIFICATIONS entries and about MAX_NOTIFICATIONS_TEXT
    characters, with long fields cut. Notification text is written by third
    parties and must not flood the model's context."""
    items = [n for n in items if isinstance(n, dict)] if isinstance(items, list) else []
    total = len(items)
    out: list[dict] = []
    size = 0
    truncated = False
    for n in items[:MAX_NOTIFICATIONS]:
        clean = {k: _clip_text(v, MAX_NOTIFICATION_FIELD) for k, v in n.items() if k != "actions"}
        acts = n.get("actions")
        if isinstance(acts, list):
            clean["actions"] = [{k: _clip_text(v, 200) for k, v in a.items()} for a in acts[:16]
                                if isinstance(a, dict)]
        cost = len(json.dumps(clean, ensure_ascii=False))
        if size + cost > MAX_NOTIFICATIONS_TEXT:
            truncated = True
            break
        size += cost
        out.append(clean)
    result: dict[str, Any] = {"count": total, "notifications": out}
    if truncated or len(out) < total:
        result["truncated"] = True
    return result


def schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


class Tools:
    def __init__(self, device: DeviceManager, semantic_loader: Callable[[str], list[dict]] | None = None,
                 flows_base: Path | None = None):
        self.device = device
        self.semantic_loader = semantic_loader
        self.flows_base = flows_base
        self.semantic: dict[str, dict] = {}

    # ------------------------------------------------------------ registry

    def generic(self) -> list[Tool]:
        a = annotations
        return [
            Tool("list_apps", "List the apps installed in the droidrt VM that have a launcher: package, label, "
                 "whether a droidrt window is open and whether a headless session is running.",
                 schema({}), self.list_apps, "List apps", a("List apps", READ_ONLY)),
            Tool("open_app", "Open an app so it can be driven. If the app already has a droidrt window its display "
                 "is reused; otherwise, with headless=true (default), a virtual display without a window is "
                 "created. headless=false opens a regular droidrt window. Returns the display id.",
                 schema({"package": PKG, "headless": {"type": "boolean", "default": True}}, ["package"]),
                 self.open_app, "Open app", a("Open app", LOCAL_SESSION)),
            Tool("close_app", "Close the headless session or the droidrt window of an app.",
                 schema({"package": PKG}, ["package"]), self.close_app, "Close app", a("Close app", LOCAL_SESSION)),
            Tool("get_screen", "Describe the current screen of an app: an outline of its accessibility tree with "
                 "element ids (usable with tap and type_text) and, by default, a PNG screenshot of its display "
                 "scaled to at most 1024 px. Coordinates in the outline are display pixels.",
                 schema({"package": PKG, "include_screenshot": {"type": "boolean", "default": True},
                         "max_depth": {"type": "integer", "minimum": 1, "maximum": 100}}, ["package"]),
                 self.get_screen, "Get screen", a("Get screen", READ_ONLY)),
            Tool("find_elements", "Find UI elements of an app. text is an exact, case sensitive match; "
                 "text_contains is case insensitive; resource_id accepts the short form (send) or the full one "
                 "(com.app:id/send).",
                 schema({"package": PKG, "text": {"type": "string"}, "text_contains": {"type": "string"},
                         "resource_id": {"type": "string"}, "content_desc": {"type": "string"},
                         "class_name": {"type": "string"}, "clickable": {"type": "boolean"},
                         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_FIND_RESULTS, "default": 30}},
                        ["package"]),
                 self.find_elements, "Find elements", a("Find elements", READ_ONLY)),
            Tool("tap", "Tap an element of an app, chosen by element_id (from get_screen or find_elements), by "
                 "text (exact match first, then case insensitive contains, then content description), by "
                 "resource_id, or tap the display coordinates x,y.",
                 schema({"package": PKG, "element_id": {"type": "string"}, "text": {"type": "string"},
                         "resource_id": {"type": "string"}, "x": {"type": "integer", "minimum": 0},
                         "y": {"type": "integer", "minimum": 0}}, ["package"]),
                 self.tap, "Tap", a("Tap", MAY_SEND)),
            Tool("type_text", "Replace the text of an editable field. Without element_id the focused field, or "
                 "the first editable one, is used. submit=true sends the IME action (enter) afterwards.",
                 schema({"package": PKG, "text": {"type": "string", "maxLength": validate.MAX_TEXT},
                         "element_id": {"type": "string"}, "submit": {"type": "boolean", "default": False}},
                        ["package", "text"]),
                 self.type_text, "Type text", a("Type text", MAY_SEND)),
            Tool("swipe", "Swipe on the app's display. direction is where the finger moves: up scrolls the "
                 "content down.",
                 schema({"package": PKG, "direction": {"type": "string", "enum": list(flows.DIRECTIONS)}},
                        ["package", "direction"]),
                 self.swipe, "Swipe", a("Swipe", UI_INPUT)),
            Tool("press_key", "Press back, home or enter on the app's display.",
                 schema({"package": PKG, "key": {"type": "string", "enum": list(flows.PRESS_KEYS)}},
                        ["package", "key"]),
                 self.press_key, "Press key", a("Press key", MAY_SEND)),
            Tool("wait_for", "Wait until an element with the given text (case insensitive contains) or "
                 "resource_id appears in the app.",
                 schema({"package": PKG, "text": {"type": "string"}, "resource_id": {"type": "string"},
                         "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 120000, "default": 10000}},
                        ["package"]),
                 self.wait_for, "Wait for element", a("Wait for element", READ_ONLY)),
            Tool("get_notifications", "List Android notifications (active ones plus recently posted), optionally "
                 "for one package. Each has a key, title, text and actions (remoteInput marks quick reply).",
                 schema({"package": PKG}), self.get_notifications, "Get notifications",
                 a("Get notifications", READ_ONLY)),
            Tool("reply_notification", "Reply to a notification through its quick reply (RemoteInput) action.",
                 schema({"key": {"type": "string", "maxLength": validate.NOTIFICATION_KEY_MAX},
                         "text": {"type": "string", "maxLength": validate.MAX_TEXT}}, ["key", "text"]),
                 self.reply_notification, "Reply to notification", a("Reply to notification", MAY_SEND)),
            Tool("run_intent", "Start an activity with an Android intent (am start) on the app's display, "
                 "opening the app headless first when needed. extras values may be strings, numbers or booleans.",
                 schema({"package": PKG,
                         "action": {"type": "string", "description": "for example android.intent.action.VIEW"},
                         "data": {"type": "string", "description": "URI"},
                         "extras": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
                         "mime_type": {"type": "string"}, "component": {"type": "string"},
                         "categories": {"type": "array", "items": {"type": "string"}}}, ["package"]),
                 self.run_intent, "Run intent", a("Run intent", MAY_SEND)),
            Tool("record_flow", "Save a reusable flow for an app and expose it as a new tool named "
                 "<app>_<name>. Steps follow the droidrt flow format: open_app, intent, wait, tap, type, press, "
                 "swipe, sleep; strings may use {arg} placeholders with the filters |urlencode and |digits, and "
                 "every placeholder becomes a string argument of the new tool. An existing flow with the same "
                 "name is only replaced with overwrite=true.",
                 schema({"package": PKG, "name": {"type": "string", "pattern": validate.FLOW_NAME_RE.pattern},
                         "description": {"type": "string", "maxLength": 1024},
                         "steps": {"type": "array", "items": {"type": "object"}, "minItems": 1,
                                   "maxItems": flows.MAX_STEPS},
                         "input_schema": {"type": "object"},
                         "overwrite": {"type": "boolean", "default": False}},
                        ["package", "name", "description", "steps"]),
                 self.record_flow, "Record flow",
                 a("Record flow", {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                                   "openWorldHint": False})),
        ]

    def load_semantic(self, packages: list[str]) -> list[Tool]:
        """Tools from semantic.tools_for for each package. Generic tool names win."""
        if self.semantic_loader is None:
            return []
        taken = {t.name for t in self.generic()}
        found: dict[str, dict] = {}
        for pkg in packages:
            if not validate.package_allowed(pkg):
                continue
            try:
                defs = self.semantic_loader(pkg)
            except Exception as e:
                log.warning("semantic tools for %s failed: %s", pkg, e)
                continue
            for d in defs:
                name = d.get("name")
                if d.get("package") != pkg:
                    continue
                if isinstance(name, str) and name not in taken and name not in found:
                    found[name] = d
        self.semantic = found
        return [self._semantic_tool(d) for d in found.values()]

    def _semantic_tool(self, d: dict) -> Tool:
        name = d["name"]
        return Tool(name, d.get("description") or name, d.get("inputSchema") or schema({}),
                    lambda args, d=d: self.run_semantic(d, args), annotations=semantic_annotations(d))

    def all(self) -> list[Tool]:
        return self.generic() + [self._semantic_tool(d) for d in self.semantic.values()]

    # ------------------------------------------------------------ handlers

    def list_apps(self, args: dict) -> Any:
        validate.no_unknown(args, set())
        return self.device.list_apps()

    def open_app(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "headless"})
        pkg = validate.package(args)
        return self.device.open_app(pkg, validate.boolean(args, "headless", True))

    def close_app(self, args: dict) -> Any:
        validate.no_unknown(args, {"package"})
        return self.device.close_app(validate.package(args))

    def get_screen(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "include_screenshot", "max_depth"})
        pkg = validate.package(args)
        shot = validate.boolean(args, "include_screenshot", True)
        depth = validate.integer(args, "max_depth", None, 1, 100)
        roots = self.device.tree(pkg, depth)
        if not roots:
            raise ToolInputError(f"{pkg} has no visible window; call open_app first")
        content = []
        header = f"package {pkg}"
        display = roots[0].get("displayId")
        if shot:
            if not isinstance(display, int):
                display = self.device.require_display(pkg)
            data, info = self.device.screenshot(display)
            header += (f", display {display} ({info['width']}x{info['height']}); screenshot "
                       f"{info['imageWidth']}x{info['imageHeight']}")
            if info["scale"] != 1.0:
                header += f", multiply screenshot coordinates by {info['scale']} to get display pixels"
            content.append(text_content(header + "\n" + compact_tree(roots)))
            content.append(image_content(data, "image/png"))
        else:
            content.append(text_content(header + f", display {display}\n" + compact_tree(roots)))
        return content

    def _selector(self, args: dict, pkg: str) -> dict:
        sel: dict[str, Any] = {"package": pkg}
        for src, dst in (("text", "text"), ("text_contains", "textContains"), ("resource_id", "resourceId"),
                         ("content_desc", "contentDesc"), ("class_name", "className")):
            v = validate.string(args, src, required=False, max_len=1024)
            if v is not None:
                sel[dst] = v
        if "clickable" in args:
            sel["clickable"] = validate.boolean(args, "clickable", False)
        return sel

    def find_elements(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "text", "text_contains", "resource_id", "content_desc",
                                   "class_name", "clickable", "limit"})
        pkg = validate.package(args)
        limit = validate.integer(args, "limit", 30, 1, MAX_FIND_RESULTS)
        sel = self._selector(args, pkg)
        if len(sel) == 1:
            raise ToolInputError("give at least one of text, text_contains, resource_id, content_desc, "
                                 "class_name or clickable")
        nodes = self.device.find(sel)
        return {"count": len(nodes), "elements": [node_summary(n) for n in nodes[:limit]]}

    def tap(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "element_id", "text", "resource_id", "x", "y"})
        pkg = validate.package(args)
        nid = validate.node_id(args)
        text = validate.string(args, "text", required=False, max_len=1024)
        rid = validate.string(args, "resource_id", required=False, max_len=512)
        has_xy = "x" in args or "y" in args
        chosen = sum(v is not None for v in (nid, text, rid)) + (1 if has_xy else 0)
        if chosen != 1:
            raise ToolInputError("give exactly one of element_id, text, resource_id or x and y")
        if nid:
            self.device.check_node_owner(pkg, nid)
            return {"element": nid, **self.device.click_node(nid)}
        if has_xy:
            x = validate.integer(args, "x", None, 0, 100000)
            y = validate.integer(args, "y", None, 0, 100000)
            if x is None or y is None:
                raise ToolInputError("x and y go together")
            return self.device.tap_point(self.device.require_display(pkg), x, y)
        if rid:
            return self.device.tap_selector({"package": pkg, "resourceId": rid})
        node = self.device.first_match([{"package": pkg, "text": text}, {"package": pkg, "textContains": text},
                                        {"package": pkg, "contentDesc": text}])
        if node is None:
            raise ToolInputError(f"no element with text {text!r}")
        return {"element": node["id"], **self.device.click_node(node["id"])}

    def type_text(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "text", "element_id", "submit"})
        pkg = validate.package(args)
        text = validate.string(args, "text", allow_empty=True)
        nid = validate.node_id(args)
        submit = validate.boolean(args, "submit", False)
        if nid is None:
            node = self.device.editable_node(pkg)
            if node is None:
                raise ToolInputError(f"{pkg} has no editable field on screen")
            nid = node["id"]
        else:
            self.device.check_node_owner(pkg, nid)
        return self.device.set_text(nid, text, submit)

    def swipe(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "direction"})
        return self.device.swipe(validate.package(args), validate.choice(args, "direction", flows.DIRECTIONS))

    def press_key(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "key"})
        return self.device.press(validate.package(args), validate.choice(args, "key", flows.PRESS_KEYS))

    def wait_for(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "text", "resource_id", "timeout_ms"})
        pkg = validate.package(args)
        text = validate.string(args, "text", required=False, max_len=1024)
        rid = validate.string(args, "resource_id", required=False, max_len=512)
        if (text is None) == (rid is None):
            raise ToolInputError("give exactly one of text or resource_id")
        timeout = validate.integer(args, "timeout_ms", 10000, 100, 120000)
        sel = {"package": pkg, **({"textContains": text} if text is not None else {"resourceId": rid})}
        return node_summary(self.device.wait_for(sel, timeout))

    def get_notifications(self, args: dict) -> Any:
        validate.no_unknown(args, {"package"})
        pkg = validate.package(args, required=False)
        items = self.device.agent.call("notifications", {"package": pkg}, timeout=10)
        items = [n for n in items if isinstance(n, dict) and validate.package_allowed(n.get("package"))
                 and (pkg is None or n.get("package") == pkg)] if isinstance(items, list) else []
        return limit_notifications(items)

    def reply_notification(self, args: dict) -> Any:
        validate.no_unknown(args, {"key", "text"})
        key = validate.string(args, "key", max_len=validate.NOTIFICATION_KEY_MAX, single_line=True)
        text = validate.string(args, "text")
        items = self.device.agent.call("notifications", {}, timeout=10)
        items = items if isinstance(items, list) else []
        note = next((n for n in items if isinstance(n, dict) and n.get("key") == key), None)
        if note is None or not validate.package_allowed(note.get("package")):
            raise ToolInputError("no notification with that key")
        action = next((a for a in note.get("actions") or [] if isinstance(a, dict) and a.get("remoteInput")
                       and isinstance(a.get("index"), int) and not isinstance(a.get("index"), bool)), None)
        if action is None:
            raise ToolInputError("that notification has no reply action")
        res = self.device.agent.call("notification_action", {"key": key, "index": action["index"], "text": text},
                                     timeout=10)
        return {"key": key, "action": action.get("title"), **(res if isinstance(res, dict) else {})}

    def run_intent(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "action", "data", "extras", "mime_type", "component", "categories"})
        pkg = validate.package(args)
        spec = validate.intent({"action": args.get("action"), "data": args.get("data"), "package": pkg,
                                "extras": args.get("extras"), "mimeType": args.get("mime_type"),
                                "component": args.get("component"), "categories": args.get("categories")},
                               target=pkg)
        return self.device.start_intent(pkg, spec)

    def record_flow(self, args: dict) -> Any:
        validate.no_unknown(args, {"package", "name", "description", "steps", "input_schema", "overwrite"})
        pkg = validate.package(args)
        overwrite = validate.boolean(args, "overwrite", False)
        input_schema = args.get("input_schema")
        if input_schema is not None and not isinstance(input_schema, dict):
            raise ToolInputError("input_schema must be an object")
        path = flows.save_flow(pkg, args.get("name"), args.get("description"), args.get("steps"),
                               input_schema, base=self.flows_base, overwrite=overwrite)
        self.on_flows_changed(pkg)
        names = [n for n, d in self.semantic.items() if d.get("package") == pkg]
        # Relative to the flows folder: the absolute path would expose the user's home.
        return {"saved": f"{pkg}/{path.stem}.json", "tools": names}

    # Replaced by the server so it can reload tools and notify the client.
    def on_flows_changed(self, package: str) -> None:
        pass

    def run_semantic(self, d: dict, args: dict) -> Any:
        flows.check_arguments(d.get("inputSchema") or {}, args)
        pkg = validate.package({"package": d.get("package")})
        action = d.get("action") or {}
        kind = action.get("type")
        if kind == "intent":
            return flows.run_intent_action(self.device, pkg, action, args)
        if kind == "flow":
            flows.check_flow_scope(pkg, action.get("steps"))
            done = flows.run_flow(self.device, pkg, action.get("steps"), args)
            return "flow finished\n" + "\n".join(done)
        raise ToolInputError(f"unsupported action type {kind!r}")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)
