"""Argument validation for every tool. All input from MCP clients is untrusted."""
from __future__ import annotations

import os
import re
from typing import Any

from .protocol import ToolInputError

PACKAGE_RE = re.compile(r"^[a-zA-Z][\w.]{0,254}$", re.ASCII)
INTENT_ACTION_RE = re.compile(r"^[A-Za-z][\w.]*$", re.ASCII)
EXTRA_KEY_RE = re.compile(r"^[A-Za-z_][\w.]*$", re.ASCII)
CATEGORY_RE = re.compile(r"^[A-Za-z][\w.]*$", re.ASCII)
COMPONENT_RE = re.compile(r"^[A-Za-z][\w.]*/[\w.$]+$", re.ASCII)
MIME_RE = re.compile(r"^[\w.+-]+/[\w.+*-]+$", re.ASCII)
NODE_ID_RE = re.compile(r"^[\w:.\-]{1,128}$", re.ASCII)
FLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
NOTIFICATION_KEY_MAX = 512

MAX_TEXT = 4096
MAX_PACKAGE = 255
MAX_EXTRAS = 32

# Optional allowlist of app packages the MCP tools may touch, comma separated.
ALLOWLIST_ENV = "DROIDRT_MCP_PACKAGES"
# System packages that are off limits unless listed in DROIDRT_MCP_PACKAGES: a
# prompt injected through app content must not be able to change settings,
# install apps, drive the shell or reconfigure the droidrt agent itself.
BLOCKED_PACKAGES = frozenset({
    "com.android.settings",
    "com.google.android.packageinstaller",
    "com.android.packageinstaller",
    "com.android.vending",
    "com.android.shell",
    "com.android.systemui",
    "dev.droidrt.agent",
})


def allowed_packages() -> frozenset[str] | None:
    """The allowlist from DROIDRT_MCP_PACKAGES, or None when it is unset or empty.
    Entries that are not package names are ignored, so a list with no valid
    entry allows nothing."""
    raw = os.environ.get(ALLOWLIST_ENV, "")
    if not raw.strip():
        return None
    return frozenset(p.strip() for p in raw.split(",")
                     if p.strip() and len(p.strip()) <= MAX_PACKAGE and PACKAGE_RE.fullmatch(p.strip()))


def package_allowed(pkg: Any) -> bool:
    if not isinstance(pkg, str):
        return False
    allow = allowed_packages()
    if allow is not None:
        return pkg in allow
    return pkg not in BLOCKED_PACKAGES


def _fail(msg: str) -> None:
    raise ToolInputError(msg)


def package(args: dict, key: str = "package", required: bool = True) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > MAX_PACKAGE or not PACKAGE_RE.fullmatch(value):
        _fail(f"'{key}' must be an Android package name such as com.example.app")
    if not package_allowed(value):
        _fail(f"package {value} is not allowed by this server (system packages are blocked by default; "
              f"the server owner can allow packages with {ALLOWLIST_ENV})")
    return value


def component_package(component: str) -> str:
    return component.split("/", 1)[0]


def check_target(spec: dict, target: str) -> None:
    """An intent may only address the target app: its package and the package
    part of its component must both be the target."""
    pkg = spec.get("package")
    if pkg is not None and pkg != target:
        _fail(f"intent package {pkg} does not match the target package {target}")
    comp = spec.get("component")
    if comp is not None and (not isinstance(comp, str) or component_package(comp) != target):
        _fail(f"intent component must belong to the target package {target}")


def string(args: dict, key: str, required: bool = True, max_len: int = MAX_TEXT, allow_empty: bool = False,
           single_line: bool = False) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        _fail(f"'{key}' must be a string")
    if not value and not allow_empty:
        _fail(f"'{key}' must not be empty")
    if len(value) > max_len:
        _fail(f"'{key}' is longer than {max_len} characters")
    if "\x00" in value or (single_line and ("\n" in value or "\r" in value)):
        _fail(f"'{key}' contains forbidden characters")
    return value


def boolean(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        _fail(f"'{key}' must be a boolean")
    return value


def integer(args: dict, key: str, default: int | None, lo: int, hi: int) -> int | None:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        _fail(f"'{key}' must be an integer from {lo} to {hi}")
    return value


def choice(args: dict, key: str, options: tuple[str, ...], default: str | None = None) -> str:
    value = args.get(key, default)
    if value not in options:
        _fail(f"'{key}' must be one of {', '.join(options)}")
    return value  # type: ignore[return-value]


def node_id(args: dict, key: str = "element_id", required: bool = False) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not NODE_ID_RE.fullmatch(value):
        _fail(f"'{key}' must be an element id returned by get_screen or find_elements")
    return value


def no_unknown(args: dict, allowed: set[str]) -> None:
    extra = set(args) - allowed
    if extra:
        _fail(f"unknown argument(s): {', '.join(sorted(extra))}")


def intent(spec: dict, target: str | None = None) -> dict:
    """Validate an intent description (already rendered) and return a clean copy.
    With `target`, the intent must stay inside that package (see check_target)."""
    if not isinstance(spec, dict):
        _fail("intent must be an object")
    out: dict[str, Any] = {}
    action = spec.get("action")
    if action is not None:
        if not isinstance(action, str) or len(action) > 255 or not INTENT_ACTION_RE.fullmatch(action):
            _fail("intent action must look like android.intent.action.VIEW")
        out["action"] = action
    data = spec.get("data")
    if data is not None:
        if not isinstance(data, str) or len(data) > MAX_TEXT or any(c in data for c in "\x00\n\r"):
            _fail("intent data must be a single line URI up to 4096 characters")
        out["data"] = data
    pkg = spec.get("package")
    if pkg is not None:
        if not isinstance(pkg, str) or not PACKAGE_RE.fullmatch(pkg) or len(pkg) > MAX_PACKAGE:
            _fail("intent package must be an Android package name")
        out["package"] = pkg
    comp = spec.get("component")
    if comp is not None:
        if not isinstance(comp, str) or len(comp) > 512 or not COMPONENT_RE.fullmatch(comp):
            _fail("intent component must look like com.example/.MainActivity")
        out["component"] = comp
    mime = spec.get("mimeType")
    if mime is not None:
        if not isinstance(mime, str) or len(mime) > 255 or not MIME_RE.fullmatch(mime):
            _fail("intent mimeType must look like text/plain")
        out["mimeType"] = mime
    cats = spec.get("categories") or []
    if not isinstance(cats, list) or len(cats) > 16 or not all(
            isinstance(c, str) and len(c) <= 255 and CATEGORY_RE.fullmatch(c) for c in cats):
        _fail("intent categories must be a list of category names")
    out["categories"] = list(cats)
    extras = spec.get("extras") or {}
    if not isinstance(extras, dict) or len(extras) > MAX_EXTRAS:
        _fail(f"intent extras must be an object with at most {MAX_EXTRAS} entries")
    clean = {}
    for k, v in extras.items():
        if not isinstance(k, str) or len(k) > 255 or not EXTRA_KEY_RE.fullmatch(k):
            _fail(f"bad intent extra key {k!r}")
        if isinstance(v, bool):
            pass
        elif isinstance(v, int):
            if not -(2 ** 63) <= v < 2 ** 63:
                _fail(f"intent extra {k!r} is out of range")
        elif isinstance(v, float):
            pass
        elif isinstance(v, str):
            if len(v) > MAX_TEXT or "\x00" in v:
                _fail(f"intent extra {k!r} is too long or has forbidden characters")
        else:
            _fail(f"intent extra {k!r} must be a string, number or boolean")
        clean[k] = v
    out["extras"] = clean
    if not (out.get("action") or out.get("data") or out.get("component")):
        _fail("intent needs at least one of action, data or component")
    if target is not None:
        check_target(out, target)
    return out


SELECTOR_KEYS = {"displayId", "package", "text", "textContains", "resourceId", "contentDesc", "className", "clickable"}


def selector(sel: Any, where: str = "selector") -> dict:
    if not isinstance(sel, dict) or not sel:
        _fail(f"{where} must be a non empty object")
    unknown = set(sel) - SELECTOR_KEYS
    if unknown:
        _fail(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    for k, v in sel.items():
        if k == "displayId":
            ok = isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2 ** 31
        elif k == "clickable":
            ok = isinstance(v, bool)
        elif k == "package":
            ok = isinstance(v, str) and bool(PACKAGE_RE.fullmatch(v)) and len(v) <= MAX_PACKAGE
        else:
            ok = isinstance(v, str) and 0 < len(v) <= 1024
        if not ok:
            _fail(f"{where} has a bad value for {k!r}")
    return dict(sel)
