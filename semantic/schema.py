"""Validation and naming helpers shared by every tool source.

The rules follow section 2 of docs/agent-protocol.md. Everything that reaches
these helpers (curated JSON, recorded flows, values read from an APK) is
treated as untrusted input and checked before it becomes a tool.
"""

from __future__ import annotations

import re
import unicodedata

NAME_RE = re.compile(r"^[a-z0-9_]{1,64}\Z", re.ASCII)
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+\Z", re.ASCII)
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
ARG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
CLASS_PART = r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
COMPONENT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/\.?" + CLASS_PART + r"\Z", re.ASCII)
URI_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,31}):", re.ASCII)

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
MAX_PACKAGE = 255
MAX_STRING = 4096          # any template or selector string
MAX_EXTRAS = 32
MAX_CATEGORIES = 16
MAX_PROPERTIES = 32
SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "description", "$schema"})
PROPERTY_KEYS = frozenset({
    "type", "description", "enum", "default", "pattern", "minLength", "maxLength",
    "minimum", "maximum", "examples", "format",
})
PROPERTY_TYPES = frozenset({"string", "integer", "number", "boolean"})
MAX_SCHEMA_DEPTH = 6

# URI schemes an intent may never carry: local files, content grants, inline
# data and URL kinds that run code or start other components.
DANGEROUS_SCHEMES = frozenset({
    "file", "content", "data", "javascript", "vbscript", "about", "intent", "android-app",
    "android.resource", "jar", "blob", "filesystem", "view-source",
})


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
    # Control and format characters (category C) and the Unicode line, paragraph
    # and exotic space separators (category Z) all become a plain space.
    out = "".join(
        ch if ch in "\n\t " or unicodedata.category(ch)[0] not in "CZ" else " "
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


def package_ok(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_PACKAGE and bool(PACKAGE_RE.match(value))


def _check_string(value: str, where: str, what: str) -> None:
    if len(value) > MAX_STRING:
        raise ToolError(f"{where}: {what} longer than {MAX_STRING} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ToolError(f"{where}: {what} contains control characters")


def _check_data(data: str, where: str) -> None:
    """The scheme of an intent URI is fixed by the definition, never by an
    argument, and is never one of DANGEROUS_SCHEMES."""
    m = URI_SCHEME_RE.match(data)
    if m is None:
        if "{" in data:
            raise ToolError(f"{where}: data must start with a literal scheme before any placeholder")
        return
    if m.group(1).lower() in DANGEROUS_SCHEMES:
        raise ToolError(f"{where}: data scheme {m.group(1)!r} is not allowed")


def validate_intent(intent: object, props: set[str], where: str = "intent", package: str | None = None) -> None:
    """With `package`, the intent must stay inside that app: its package and
    the package part of its component must both be `package`.

    Placeholders are only allowed in `data` and in extra values: the action,
    package, component, MIME type and categories are fixed by the definition."""
    if not isinstance(intent, dict):
        raise ToolError(f"{where}: must be an object")
    unknown = set(intent) - INTENT_KEYS
    if unknown:
        raise ToolError(f"{where}: unknown keys {sorted(unknown)}")
    if intent.get("type", "intent") != "intent":
        raise ToolError(f"{where}: type must be 'intent'")
    for key in ("action", "data", "package", "component", "mimeType"):
        value = intent.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ToolError(f"{where}: {key} must be a string or null")
        _check_string(value, where, key)
        if key != "data" and ("{" in value or "}" in value):
            raise ToolError(f"{where}: {key} may not hold placeholders")
    if not (intent.get("action") or intent.get("data") or intent.get("component")):
        raise ToolError(f"{where}: needs at least one of action, data or component")
    pkg = intent.get("package")
    if pkg is not None and not package_ok(pkg):
        raise ToolError(f"{where}: bad package {pkg!r}")
    comp = intent.get("component")
    if comp is not None and not COMPONENT_RE.match(comp):
        raise ToolError(f"{where}: bad component {comp!r}")
    if package is not None:
        if pkg is not None and pkg != package:
            raise ToolError(f"{where}: package {pkg!r} is not the tool's package {package!r}")
        if comp is not None and comp.split("/", 1)[0] != package:
            raise ToolError(f"{where}: component {comp!r} does not belong to {package!r}")
    if intent.get("data"):
        _check_data(intent["data"], where)
    extras = intent.get("extras")
    if extras is not None:
        if not (isinstance(extras, dict) and len(extras) <= MAX_EXTRAS and all(
                isinstance(k, str) and isinstance(v, str) for k, v in extras.items())):
            raise ToolError(f"{where}: extras must map strings to strings (at most {MAX_EXTRAS})")
        for k, v in extras.items():
            _check_string(v, where, "extra value")
            if not k or len(k) > 255 or "{" in k or "}" in k:
                raise ToolError(f"{where}: bad extra key {k!r}")
    cats = intent.get("categories")
    if cats is not None:
        if not (isinstance(cats, list) and len(cats) <= MAX_CATEGORIES and all(
                isinstance(c, str) and 0 < len(c) <= 255 and "{" not in c and "}" not in c for c in cats)):
            raise ToolError(f"{where}: categories must be a list of at most {MAX_CATEGORIES} plain strings")
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
            ok = isinstance(value, str) and value != "" and len(value) <= MAX_STRING
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
            if not package_ok(body):
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
            if not isinstance(body["text"], str) or len(body["text"]) > MAX_STRING:
                raise ToolError(f"{where}: type text must be a string up to {MAX_STRING} characters")
            _check_templates(body, props, where)
        elif kind == "press":
            if not isinstance(body, str) or body not in PRESS_KEYS:
                raise ToolError(f"{where}: press must be one of {sorted(PRESS_KEYS)}")
        elif kind == "swipe":
            if (not isinstance(body, dict) or len(body) != 1 or not isinstance(body.get("direction"), str)
                    or body["direction"] not in SWIPE_DIRECTIONS):
                raise ToolError(f"{where}: swipe needs a direction in {sorted(SWIPE_DIRECTIONS)}")
        elif kind == "sleep":
            if not isinstance(body, int) or isinstance(body, bool) or not 0 <= body <= MAX_SLEEP_MS:
                raise ToolError(f"{where}: sleep must be an integer from 0 to {MAX_SLEEP_MS}")
        else:
            raise ToolError(f"{where}: unknown step {kind!r}")


def _depth(value: object, limit: int) -> bool:
    """True when `value` nests dicts and lists no deeper than `limit` (iterative)."""
    stack = [(value, 0)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, (dict, list)):
            if d >= limit:
                return False
            stack.extend((x, d + 1) for x in (v.values() if isinstance(v, dict) else v))
    return True


def _validate_input_schema(schema: object, name: str) -> dict:
    """A flat JSON schema of type object whose properties are primitives."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ToolError(f"{name}: inputSchema must be a JSON schema of type object")
    if not _depth(schema, MAX_SCHEMA_DEPTH):
        raise ToolError(f"{name}: inputSchema nests deeper than {MAX_SCHEMA_DEPTH} levels")
    unknown = set(schema) - SCHEMA_KEYS
    if unknown:
        raise ToolError(f"{name}: unknown inputSchema keys {sorted(unknown)}")
    props = schema.get("properties", {})
    if not isinstance(props, dict) or not all(isinstance(k, str) and ARG_RE.match(k) for k in props):
        raise ToolError(f"{name}: inputSchema.properties must map argument names to schemas")
    if len(props) > MAX_PROPERTIES:
        raise ToolError(f"{name}: more than {MAX_PROPERTIES} input properties")
    for key, prop in props.items():
        if not isinstance(prop, dict) or set(prop) - PROPERTY_KEYS:
            raise ToolError(f"{name}: property {key!r} must be an object with known schema keys")
        if "type" in prop and (not isinstance(prop["type"], str) or prop["type"] not in PROPERTY_TYPES):
            raise ToolError(f"{name}: property {key!r} has an unsupported type")
        desc = prop.get("description")
        if desc is not None and (not isinstance(desc, str) or len(desc) > MAX_DESCRIPTION):
            raise ToolError(f"{name}: property {key!r} description must be a string up to {MAX_DESCRIPTION} chars")
    required = schema.get("required", [])
    if (not isinstance(required, list) or not all(isinstance(r, str) for r in required)
            or len(set(required)) != len(required) or not set(required) <= set(props)):
        raise ToolError(f"{name}: inputSchema.required must list distinct input properties")
    return props


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
    if not package_ok(pkg):
        raise ToolError(f"{name}: bad package {pkg!r}")
    if package is not None and pkg != package:
        raise ToolError(f"{name}: package {pkg!r} does not match {package!r}")
    props = _validate_input_schema(tool.get("inputSchema"), name)
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
