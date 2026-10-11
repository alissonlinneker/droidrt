"""Template rendering and the flow engine for semantic tools.

Templates (docs/agent-protocol.md, section 2) use `{arg}` placeholders with
optional filters, chained with `|`:

- `urlencode`: percent-encodes everything except unreserved characters.
- `digits`: keeps only the digits 0-9.

The engine only renders strings and passes them as separate argv items to the
device layer, so no value ever reaches a shell unquoted.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Protocol

from . import validate
from .protocol import ToolInputError

PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
ARG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
FILTERS = {
    "urlencode": lambda s: urllib.parse.quote(s, safe="-_.~"),
    "digits": lambda s: "".join(c for c in s if "0" <= c <= "9"),
}
MAX_STEPS = 200
MAX_SLEEP_MS = 120_000
MAX_WAIT_MS = 300_000
MAX_RENDERED = 8192
PRESS_KEYS = ("back", "enter", "home")
DIRECTIONS = ("up", "down", "left", "right")


def render(template: str, args: dict) -> str:
    """Render one template string. Missing optional args become empty strings."""
    if template.count("{") != template.count("}"):
        raise ToolInputError(f"unbalanced braces in template {template!r}")

    def sub(match: re.Match) -> str:
        name, *filters = match.group(1).split("|")
        name = name.strip()
        if not ARG_RE.fullmatch(name):
            raise ToolInputError(f"bad placeholder {match.group(0)!r}")
        value = args.get(name)
        if value is None:
            text = ""
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float, str)):
            text = str(value)
        else:
            raise ToolInputError(f"argument {name!r} must be a string, number or boolean")
        for f in filters:
            fn = FILTERS.get(f.strip())
            if fn is None:
                raise ToolInputError(f"unknown template filter {f!r}")
            text = fn(text)
        return text

    out = PLACEHOLDER_RE.sub(sub, template)
    if len(out) > MAX_RENDERED:
        raise ToolInputError("rendered value is too long")
    return out


def render_value(value: Any, args: dict) -> Any:
    """Render every string inside a JSON value (dict keys are left alone)."""
    if isinstance(value, str):
        return render(value, args)
    if isinstance(value, dict):
        return {k: render_value(v, args) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, args) for v in value]
    return value


def check_arguments(schema: dict, args: dict) -> dict:
    """Light JSON schema check for semantic tool arguments: required keys,
    unknown keys, primitive types and string length."""
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    missing = [k for k in required if args.get(k) in (None, "")]
    if missing:
        raise ToolInputError(f"missing required argument(s): {', '.join(missing)}")
    unknown = set(args) - set(props)
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    for k, v in args.items():
        want = (props.get(k) or {}).get("type")
        py = types.get(want)
        if py and (not isinstance(v, py) or (want in ("integer", "number") and isinstance(v, bool))):
            raise ToolInputError(f"argument {k!r} must be of type {want}")
        if isinstance(v, str) and (len(v) > validate.MAX_TEXT or "\x00" in v):
            raise ToolInputError(f"argument {k!r} is too long or has forbidden characters")
    return args


class Device(Protocol):
    """What the flow engine needs from the device layer."""

    def open_app(self, package: str, headless: bool = True) -> dict: ...
    def start_intent(self, package: str, spec: dict) -> str: ...
    def wait_for(self, selector: dict, timeout_ms: int) -> dict: ...
    def tap_selector(self, selector: dict) -> dict: ...
    def type_selector(self, selector: dict, text: str) -> dict: ...
    def press(self, package: str, key: str) -> dict: ...
    def swipe(self, package: str, direction: str) -> dict: ...


def run_intent_action(device: Device, package: str, action: dict, args: dict) -> str:
    if not isinstance(action, dict):
        raise ToolInputError("intent must be an object")
    spec = render_value({k: v for k, v in action.items() if k != "type"}, args)
    clean = validate.intent(spec, target=package)
    return device.start_intent(package, clean)


def run_flow(device: Device, package: str, steps: list, args: dict, sleep=time.sleep) -> list[str]:
    """Execute flow steps in order and return a short log line per step."""
    if not isinstance(steps, list) or not steps:
        raise ToolInputError("flow needs a non empty list of steps")
    if len(steps) > MAX_STEPS:
        raise ToolInputError(f"flow has more than {MAX_STEPS} steps")
    done: list[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or len(step) != 1:
            raise ToolInputError(f"step {i}: each step is an object with exactly one key")
        (kind, body), = step.items()
        try:
            done.append(f"{i}: {kind}: {_run_step(device, package, kind, body, args, sleep)}")
        except ToolInputError as e:
            done.append(f"{i}: {kind}: failed: {e}")
            raise ToolInputError("flow stopped\n" + "\n".join(done)) from None
    return done


def _with_package(sel: dict, package: str) -> dict:
    """Pin a step selector to the flow's own package. A selector can never
    reach another app or a display by id, whatever the flow file says."""
    out = {k: v for k, v in sel.items() if k not in ("package", "displayId")}
    out["package"] = package
    return out


def _run_step(device: Device, package: str, kind: str, body: Any, args: dict, sleep) -> str:
    if kind == "open_app":
        pkg = render(body, args) if isinstance(body, str) else body
        validate.package({"package": pkg})
        if pkg != package:
            raise ToolInputError(f"open_app may only open the flow's own package {package}")
        info = device.open_app(pkg, True)
        return f"display {info.get('displayId')}"
    if kind == "intent":
        return run_intent_action(device, package, body, args)
    if kind == "wait":
        if not isinstance(body, dict):
            raise ToolInputError("wait needs an object")
        timeout = body.get("timeoutMs", 10000)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 < timeout <= MAX_WAIT_MS:
            raise ToolInputError(f"timeoutMs must be an integer from 1 to {MAX_WAIT_MS}")
        sel = validate.selector(render_value({k: v for k, v in body.items() if k != "timeoutMs"}, args), "wait")
        node = device.wait_for(_with_package(sel, package), timeout)
        return f"found {node.get('id')}"
    if kind == "tap":
        sel = validate.selector(render_value(body, args), "tap")
        res = device.tap_selector(_with_package(sel, package))
        return json.dumps(res)
    if kind == "type":
        if not isinstance(body, dict) or set(body) != {"selector", "text"}:
            raise ToolInputError("type needs exactly selector and text")
        sel = validate.selector(render_value(body["selector"], args), "type selector")
        text = render(body["text"], args) if isinstance(body["text"], str) else None
        if text is None:
            raise ToolInputError("type text must be a string")
        res = device.type_selector(_with_package(sel, package), text)
        return json.dumps(res)
    if kind == "press":
        if body not in PRESS_KEYS:
            raise ToolInputError(f"press must be one of {', '.join(PRESS_KEYS)}")
        return json.dumps(device.press(package, body))
    if kind == "swipe":
        if not isinstance(body, dict) or body.get("direction") not in DIRECTIONS:
            raise ToolInputError(f"swipe needs a direction in {', '.join(DIRECTIONS)}")
        return json.dumps(device.swipe(package, body["direction"]))
    if kind == "sleep":
        if not isinstance(body, int) or isinstance(body, bool) or not 0 <= body <= MAX_SLEEP_MS:
            raise ToolInputError(f"sleep must be an integer from 0 to {MAX_SLEEP_MS}")
        sleep(body / 1000)
        return f"{body} ms"
    raise ToolInputError(f"unknown step {kind!r}")


# ---------------------------------------------------------------- recorded flows

def flows_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "droidrt" / "flows"


def template_names(value: Any) -> list[str]:
    """Placeholder names used anywhere in a JSON value, in order of appearance."""
    names: list[str] = []
    if isinstance(value, str):
        for m in PLACEHOLDER_RE.finditer(value):
            n = m.group(1).split("|")[0].strip()
            if ARG_RE.fullmatch(n) and n not in names:
                names.append(n)
    elif isinstance(value, dict):
        for v in value.values():
            names += [n for n in template_names(v) if n not in names]
    elif isinstance(value, list):
        for v in value:
            names += [n for n in template_names(v) if n not in names]
    return names


def check_flow_scope(package: str, steps: Any) -> None:
    """Reject steps that reach outside `package`: open_app of another app, an
    intent with a foreign package or component, or a selector that names a
    package or a display. Independent of the semantic package."""
    if not isinstance(steps, list):
        return
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or len(step) != 1:
            continue
        (kind, body), = step.items()
        if kind == "open_app" and body != package:
            raise ToolInputError(f"step {i}: open_app may only open {package}")
        if kind == "intent" and isinstance(body, dict):
            if body.get("package") is not None and body.get("package") != package:
                raise ToolInputError(f"step {i}: intent package must be {package}")
            comp = body.get("component")
            if comp is not None and (not isinstance(comp, str) or validate.component_package(comp) != package):
                raise ToolInputError(f"step {i}: intent component must belong to {package}")
        sels = []
        if kind in ("wait", "tap") and isinstance(body, dict):
            sels.append(body)
        elif kind == "type" and isinstance(body, dict) and isinstance(body.get("selector"), dict):
            sels.append(body["selector"])
        for sel in sels:
            bad = {"package", "displayId"} & set(sel)
            if bad:
                raise ToolInputError(f"step {i}: selectors may not set {', '.join(sorted(bad))}; "
                                     "flows always act on their own package")


def save_flow(package: str, name: str, description: str, steps: list, input_schema: dict | None = None,
              base: Path | None = None) -> Path:
    """Write a recorded flow in the short form understood by semantic.flows."""
    validate.package({"package": package})
    if not isinstance(name, str) or not validate.FLOW_NAME_RE.fullmatch(name):
        raise ToolInputError("name must match ^[a-z][a-z0-9_]{0,47}$")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ToolInputError("description must be a non empty string up to 1024 characters")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise ToolInputError(f"steps must be a list with 1 to {MAX_STEPS} entries")
    check_flow_scope(package, steps)
    if input_schema is None:
        input_schema = {
            "type": "object",
            "properties": {n: {"type": "string"} for n in template_names(steps)},
            "required": template_names(steps),
        }
    data = {"name": name, "description": description.strip(), "package": package,
            "inputSchema": input_schema, "steps": steps}
    _validate_flow_definition(package, name, data)
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    if len(raw) > 1_000_000:
        raise ToolInputError("flow definition is too large")
    folder = (base or flows_dir()) / package
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    if path.is_symlink():
        raise ToolInputError("refusing to overwrite a symlink")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(raw + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _validate_flow_definition(package: str, name: str, data: dict) -> None:
    """Validate with the semantic package when it is available, otherwise with
    the checks of this module."""
    try:
        from semantic import flows as sflows  # type: ignore
        from semantic.schema import ToolError, app_slug  # type: ignore
    except ImportError:
        schema = data["inputSchema"]
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ToolInputError("inputSchema must be a JSON schema of type object")
        props = set((schema.get("properties") or {}).keys())
        missing = [n for n in template_names(data["steps"]) if n not in props]
        if missing:
            raise ToolInputError(f"placeholders without an input property: {', '.join(missing)}")
        for i, step in enumerate(data["steps"]):
            if not isinstance(step, dict) or len(step) != 1:
                raise ToolInputError(f"step {i}: each step is an object with exactly one key")
            if next(iter(step)) not in ("open_app", "intent", "wait", "tap", "type", "press", "swipe", "sleep"):
                raise ToolInputError(f"step {i}: unknown step {next(iter(step))!r}")
        return
    try:
        sflows.flow_to_tool(data, package, app_slug(package), name)
    except ToolError as e:
        raise ToolInputError(f"invalid flow: {e}") from None
