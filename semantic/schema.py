"""Validation and naming helpers shared by every tool source.

The rules follow section 2 of docs/agent-protocol.md. Everything that reaches
these helpers (curated JSON, recorded flows, values read from an APK) is
treated as untrusted input and checked before it becomes a tool.
"""

from __future__ import annotations

import re
import unicodedata

NAME_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+\Z")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
ARG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")

FILTERS = frozenset({"urlencode", "digits"})
SELECTOR_KEYS = frozenset({
    "displayId", "package", "text", "textContains", "resourceId",
    "contentDesc", "className", "clickable",
})
WAIT_KEYS = frozenset({"text", "textContains", "resourceId", "contentDesc"})
PRESS_KEYS = frozenset({"back", "enter", "home"})
SWIPE_DIRECTIONS = frozenset({"up", "down", "left", "right"})
INTENT_KEYS = frozenset({
    "type", "action", "data", "package", "component", "extras", "mimeType", "categories",
})
MAX_SLEEP_MS = 120_000
MAX_TIMEOUT_MS = 300_000
MAX_STEPS = 200
MAX_DESCRIPTION = 1024


class ToolError(ValueError):
    """Raised when a tool definition does not follow the contract."""


def sanitize_name(raw: str, limit: int = 64, split_camel: bool = True) -> str:
    """Lowercase ASCII, runs of anything else collapsed to one underscore.

    With `split_camel`, "ChatListActivity" becomes "chat_list_activity".
    """
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    if split_camel:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:limit].rstrip("_")


def app_slug(package: str, label: str | None = None, limit: int = 20) -> str:
    """Short prefix for tool names, from the app label or else the package.

    "WhatsApp" gives "whatsapp", "Breezy Weather" gives "breezy_weather".
    Labels that do not reduce to ASCII fall back to the most specific
    package segment that is not a generic word.
    """
    if label:
        words = sanitize_name(label, split_camel=False).split("_")
        slug = ""
        for word in words:
            candidate = f"{slug}_{word}" if slug else word
            if len(candidate) > limit:
                break
            slug = candidate
        if not slug and words and words[0]:
            slug = words[0][:limit]
        if slug:
            return slug
    generic = {"android", "app", "apps", "mobile", "client", "com", "org", "net", "io", "dev"}
    parts = [p for p in package.split(".") if p]
    for part in reversed(parts):
        cleaned = sanitize_name(part, limit)
        if cleaned and cleaned not in generic:
            return cleaned
    return sanitize_name(package.replace(".", "_"), limit) or "app"


def prefixed_name(slug: str, *parts: str) -> str:
    """Join parts under the slug and keep the result within 64 characters."""
    body = sanitize_name("_".join(p for p in parts if p))
    name = f"{slug}_{body}" if body else slug
    return name[:64].rstrip("_")


def clean_text(value: object, limit: int = MAX_DESCRIPTION) -> str:
    """Strip control and format characters (bidi, zero width) and trim."""
    if not isinstance(value, str):
        return ""
    out = "".join(
        ch if ch in "\n\t" or unicodedata.category(ch)[0] != "C" else " "
        for ch in value
    )
    return re.sub(r"[ \t]+", " ", out).strip()[:limit]


def template_args(template: str) -> list[tuple[str, list[str]]]:
    """Parse `{arg}` and `{arg|filter}` placeholders, rejecting malformed ones."""
    if template.count("{") != template.count("}"):
        raise ToolError(f"unbalanced braces in template {template!r}")
    found = []
    for match in PLACEHOLDER_RE.finditer(template):
        arg, *filters = match.group(1).split("|")
        if not ARG_RE.match(arg):
            raise ToolError(f"bad placeholder {match.group(0)!r} in template {template!r}")
        for f in filters:
            if f not in FILTERS:
                raise ToolError(f"unknown filter {f!r} in template {template!r}")
        found.append((arg, filters))
    leftover = PLACEHOLDER_RE.sub("", template)
    if "{" in leftover or "}" in leftover:
        raise ToolError(f"nested or stray braces in template {template!r}")
    return found


def _check_templates(value: object, props: set[str], where: str) -> None:
    if isinstance(value, str):
        for arg, _ in template_args(value):
            if arg not in props:
                raise ToolError(f"{where}: placeholder {{{arg}}} is not an input property")
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ToolError(f"{where}: keys must be strings")
            _check_templates(v, props, where)
    elif isinstance(value, list):
        for v in value:
            _check_templates(v, props, where)


def validate_intent(intent: object, props: set[str], where: str = "intent", package: str | None = None) -> None:
    """With `package`, the intent must stay inside that app: its package and
    the package part of its component must both be `package`."""
    if not isinstance(intent, dict):
        raise ToolError(f"{where}: must be an object")
    unknown = set(intent) - INTENT_KEYS
    if unknown:
        raise ToolError(f"{where}: unknown keys {sorted(unknown)}")
    if intent.get("type", "intent") != "intent":
        raise ToolError(f"{where}: type must be 'intent'")
    for key in ("action", "data", "package", "component", "mimeType"):
        if intent.get(key) is not None and not isinstance(intent[key], str):
            raise ToolError(f"{where}: {key} must be a string or null")
    if not (intent.get("action") or intent.get("data") or intent.get("component")):
        raise ToolError(f"{where}: needs at least one of action, data or component")
    pkg = intent.get("package")
    if pkg is not None and not PACKAGE_RE.match(pkg):
        raise ToolError(f"{where}: bad package {pkg!r}")
    if package is not None:
        if pkg is not None and pkg != package:
            raise ToolError(f"{where}: package {pkg!r} is not the tool's package {package!r}")
        comp = intent.get("component")
        if comp is not None and comp.split("/", 1)[0] != package:
            raise ToolError(f"{where}: component {comp!r} does not belong to {package!r}")
    extras = intent.get("extras")
    if extras is not None and not (
        isinstance(extras, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in extras.items())
    ):
        raise ToolError(f"{where}: extras must map strings to strings")
    cats = intent.get("categories")
    if cats is not None and not (isinstance(cats, list) and all(isinstance(c, str) for c in cats)):
        raise ToolError(f"{where}: categories must be a list of strings")
    _check_templates(intent, props, where)


def validate_selector(sel: object, where: str) -> None:
    if not isinstance(sel, dict) or not sel:
        raise ToolError(f"{where}: selector must be a non empty object")
    unknown = set(sel) - SELECTOR_KEYS
    if unknown:
        raise ToolError(f"{where}: unknown selector keys {sorted(unknown)}")
    for key, value in sel.items():
        if key == "displayId":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif key == "clickable":
            ok = isinstance(value, bool)
        else:
            ok = isinstance(value, str) and value != ""
        if not ok:
            raise ToolError(f"{where}: bad value for {key!r}")


# Flow steps always act on the flow's own package, so their selectors may not
# name a package or a display (the engine pins the package itself).
STEP_SELECTOR_FORBIDDEN = frozenset({"package", "displayId"})


def _validate_step_selector(sel: object, where: str) -> None:
    validate_selector(sel, where)
    bad = STEP_SELECTOR_FORBIDDEN & set(sel)  # type: ignore[arg-type]
    if bad:
        raise ToolError(f"{where}: selectors in flow steps may not set {sorted(bad)}")


def validate_steps(steps: object, props: set[str], package: str | None = None) -> None:
    """Check flow steps against the step list in the contract. With `package`,
    open_app and intents must target that package."""
    if not isinstance(steps, list) or not steps:
        raise ToolError("flow: steps must be a non empty list")
    if len(steps) > MAX_STEPS:
        raise ToolError(f"flow: more than {MAX_STEPS} steps")
    for i, step in enumerate(steps):
        where = f"step {i}"
        if not isinstance(step, dict) or len(step) != 1:
            raise ToolError(f"{where}: each step is an object with exactly one key")
        (kind, body), = step.items()
        if kind == "open_app":
            if not isinstance(body, str) or not PACKAGE_RE.match(body):
                raise ToolError(f"{where}: open_app needs a package name")
            if package is not None and body != package:
                raise ToolError(f"{where}: open_app may only open {package!r}")
        elif kind == "intent":
            validate_intent(body, props, where, package)
        elif kind == "wait":
            if not isinstance(body, dict):
                raise ToolError(f"{where}: wait needs an object")
            timeout = body.get("timeoutMs", 10000)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 < timeout <= MAX_TIMEOUT_MS:
                raise ToolError(f"{where}: timeoutMs must be an integer in (0, {MAX_TIMEOUT_MS}]")
            sel = {k: v for k, v in body.items() if k != "timeoutMs"}
            if not set(sel) & WAIT_KEYS:
                raise ToolError(f"{where}: wait needs one of {sorted(WAIT_KEYS)}")
            _validate_step_selector(sel, where)
            _check_templates(sel, props, where)
        elif kind == "tap":
            _validate_step_selector(body, where)
            _check_templates(body, props, where)
        elif kind == "type":
            if not isinstance(body, dict) or set(body) != {"selector", "text"}:
                raise ToolError(f"{where}: type needs exactly selector and text")
            _validate_step_selector(body["selector"], where)
            if not isinstance(body["text"], str):
                raise ToolError(f"{where}: type text must be a string")
            _check_templates(body, props, where)
        elif kind == "press":
            if body not in PRESS_KEYS:
                raise ToolError(f"{where}: press must be one of {sorted(PRESS_KEYS)}")
        elif kind == "swipe":
            if not isinstance(body, dict) or body.get("direction") not in SWIPE_DIRECTIONS or len(body) != 1:
                raise ToolError(f"{where}: swipe needs a direction in {sorted(SWIPE_DIRECTIONS)}")
        elif kind == "sleep":
            if not isinstance(body, int) or isinstance(body, bool) or not 0 <= body <= MAX_SLEEP_MS:
                raise ToolError(f"{where}: sleep must be an integer from 0 to {MAX_SLEEP_MS}")
        else:
            raise ToolError(f"{where}: unknown step {kind!r}")


def validate_tool(tool: object, package: str | None = None) -> dict:
    """Validate one tool dict and return it unchanged. Raises ToolError."""
    if not isinstance(tool, dict):
        raise ToolError("tool must be an object")
    unknown = set(tool) - {"name", "description", "package", "inputSchema", "action"}
    if unknown:
        raise ToolError(f"unknown tool keys {sorted(unknown)}")
    name = tool.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ToolError(f"bad tool name {name!r}")
    desc = tool.get("description")
    if not isinstance(desc, str) or not desc.strip() or len(desc) > MAX_DESCRIPTION:
        raise ToolError(f"{name}: description must be a non empty string up to {MAX_DESCRIPTION} chars")
    pkg = tool.get("package")
    if not isinstance(pkg, str) or not PACKAGE_RE.match(pkg):
        raise ToolError(f"{name}: bad package {pkg!r}")
    if package is not None and pkg != package:
        raise ToolError(f"{name}: package {pkg!r} does not match {package!r}")
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ToolError(f"{name}: inputSchema must be a JSON schema of type object")
    props = schema.get("properties", {})
    if not isinstance(props, dict) or not all(isinstance(k, str) and ARG_RE.match(k) for k in props):
        raise ToolError(f"{name}: inputSchema.properties must map argument names to schemas")
    required = schema.get("required", [])
    if not isinstance(required, list) or not set(required) <= set(props):
        raise ToolError(f"{name}: inputSchema.required lists unknown properties")
    action = tool.get("action")
    if not isinstance(action, dict):
        raise ToolError(f"{name}: action must be an object")
    kind = action.get("type")
    try:
        if kind == "intent":
            validate_intent(action, set(props), "action", pkg)
        elif kind == "flow":
            if set(action) != {"type", "steps"}:
                raise ToolError("flow action takes only type and steps")
            validate_steps(action.get("steps"), set(props), pkg)
        else:
            raise ToolError(f"unknown action type {kind!r}")
    except ToolError as e:
        raise ToolError(f"{name}: {e}") from None
    return tool
