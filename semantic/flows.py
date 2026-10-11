"""Recorded flows: load, validate and list.

Flows live in `~/Library/Application Support/droidrt/flows/<package>/<name>.json`.
This module only reads and checks them; the MCP server runs them.

A flow file is either a full tool definition (see docs/agent-protocol.md,
section 2) or the short form:

    {"description": "...", "inputSchema": {...}, "steps": [ ... ]}

When `name` is missing it comes from the file name, and names are always
prefixed with the app slug.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from .schema import NAME_RE, PACKAGE_RE, ToolError, clean_text, prefixed_name, sanitize_name, validate_tool

log = logging.getLogger(__name__)

FLOWS_DIR = Path.home() / "Library" / "Application Support" / "droidrt" / "flows"
MAX_FILE_BYTES = 1_000_000
MAX_FLOWS = 200


def flow_dir(package: str, base: Path | None = None) -> Path:
    if not PACKAGE_RE.match(package):
        raise ValueError(f"bad package name {package!r}")
    return (base or FLOWS_DIR) / package


def list_flows(package: str, base: Path | None = None) -> list[Path]:
    """Flow files for a package, sorted by name. Symlinks are skipped."""
    folder = flow_dir(package, base)
    if not folder.is_dir():
        return []
    files = [p for p in sorted(folder.glob("*.json")) if p.is_file() and not p.is_symlink()]
    return files[:MAX_FLOWS]


def flow_to_tool(data: object, package: str, slug: str, stem: str) -> dict:
    """Normalize a parsed flow file into a validated tool dict."""
    if not isinstance(data, dict):
        raise ToolError("flow file must hold a JSON object")
    if "action" in data:
        tool = dict(data)
    else:
        unknown = set(data) - {"name", "description", "inputSchema", "steps", "package"}
        if unknown:
            raise ToolError(f"unknown keys {sorted(unknown)}")
        tool = {k: data[k] for k in ("name", "description", "inputSchema", "package") if k in data}
        tool["action"] = {"type": "flow", "steps": data.get("steps")}
    name = tool.get("name") if isinstance(tool.get("name"), str) else stem
    name = sanitize_name(name)
    if not name.startswith(slug + "_"):
        name = prefixed_name(slug, name)
    if not NAME_RE.match(name):
        raise ToolError(f"cannot derive a tool name from {stem!r}")
    tool["name"] = name
    tool.setdefault("package", package)
    tool["description"] = clean_text(tool.get("description")) or f"Recorded flow {stem}."
    tool.setdefault("inputSchema", {"type": "object", "properties": {}, "required": []})
    return validate_tool(tool, package)


def _read_limited(path: Path) -> bytes:
    """Read a regular file of at most MAX_FILE_BYTES without following a
    symlink swapped in after the directory was listed."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ToolError(f"{path.name}: not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"{path.name}: larger than {MAX_FILE_BYTES} bytes")
    return data


def load_flow(path: Path, package: str, slug: str) -> dict:
    path = Path(path)
    raw = _read_limited(path)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise ToolError(f"{path.name}: invalid JSON ({e})") from None
    try:
        return flow_to_tool(data, package, slug, path.stem)
    except ToolError as e:
        raise ToolError(f"{path.name}: {e}") from None


def load_flows(package: str, slug: str, base: Path | None = None) -> tuple[list[dict], list[str]]:
    """Valid flow tools plus one error message per rejected file."""
    tools, errors = [], []
    for path in list_flows(package, base):
        try:
            tools.append(load_flow(path, package, slug))
        except (OSError, ToolError) as e:
            errors.append(str(e))
            log.warning("skipping flow %s", e)
    return tools, errors
