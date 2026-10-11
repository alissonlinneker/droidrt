"""Per-app semantic tools for droidrt (docs/agent-protocol.md, section 2).

`tools_for(package, apk_paths)` merges three sources, in order of precedence:

1. curated definitions in `semantic/curated/<package>.json`;
2. recorded flows in `~/Library/Application Support/droidrt/flows/<package>/`;
3. tools generated from the APK manifest (only when `apk_paths` is given).

When two sources produce the same tool name, the higher precedence wins.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import flows, manifest
from .schema import PACKAGE_RE, ToolError, app_slug, validate_tool

__all__ = ["tools_for", "load_curated", "validate_tool", "ToolError"]

log = logging.getLogger(__name__)

CURATED_DIR = Path(__file__).resolve().parent / "curated"


def load_curated(package: str, base: Path | None = None) -> tuple[str | None, list[dict]]:
    """Return (slug, tools) from the curated file, or (None, []) when there is none."""
    path = (base or CURATED_DIR) / f"{package}.json"
    if not path.is_file():
        return None, []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("package") != package:
        raise ToolError(f"{path.name}: package field does not match {package!r}")
    tools = []
    for tool in data.get("tools", []):
        try:
            tools.append(validate_tool(tool, package))
        except ToolError as e:
            log.warning("skipping curated tool in %s: %s", path.name, e)
    slug = data.get("slug")
    return (slug if isinstance(slug, str) and slug else None), tools


def tools_for(package: str, apk_paths: list[Path] | None = None) -> list[dict]:
    """All semantic tools for `package`, curated first, then flows, then manifest."""
    if not isinstance(package, str) or not PACKAGE_RE.match(package):
        raise ValueError(f"bad package name {package!r}")
    slug, curated = load_curated(package)
    info = manifest.analyze(package, [Path(p) for p in apk_paths]) if apk_paths else None
    slug = slug or app_slug(package, info.label if info else None)
    recorded, _ = flows.load_flows(package, slug)
    generated = manifest.generate_tools(info, slug) if info else []

    merged: list[dict] = []
    seen: set[str] = set()
    for tool in (*curated, *recorded, *generated):
        if tool["name"] not in seen:
            seen.add(tool["name"])
            merged.append(tool)
    return merged
