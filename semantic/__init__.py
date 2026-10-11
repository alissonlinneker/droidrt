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
from .schema import NAME_RE, ToolError, app_slug, package_ok, validate_tool

__all__ = ["tools_for", "load_curated", "validate_tool", "ToolError"]

log = logging.getLogger(__name__)

CURATED_DIR = Path(__file__).resolve().parent / "curated"
MAX_SLUG = 20


def load_curated(package: str, base: Path | None = None) -> tuple[str | None, list[dict]]:
    """Return (slug, tools) from the curated file, or (None, []) when there is none."""
    if not package_ok(package):
        raise ValueError(f"bad package name {package!r}")
    path = (base or CURATED_DIR) / f"{package}.json"
    if not path.is_file():
        return None, []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("package") != package:
        raise ToolError(f"{path.name}: package field does not match {package!r}")
    tools = []
    raw_tools = data.get("tools", [])
    for tool in raw_tools if isinstance(raw_tools, list) else []:
        try:
            tools.append(validate_tool(tool, package))
        except ToolError as e:
            log.warning("skipping curated tool in %s: %s", path.name, e)
    slug = data.get("slug")
    # The slug prefixes every generated and recorded tool name, so it must be a
    # short valid name on its own.
    ok = isinstance(slug, str) and len(slug) <= MAX_SLUG and NAME_RE.match(slug) and not slug.endswith("_")
    return (slug if ok else None), tools


def tools_for(package: str, apk_paths: list[Path] | None = None) -> list[dict]:
    """All semantic tools for `package`, curated first, then flows, then manifest."""
    if not package_ok(package):
        raise ValueError(f"bad package name {package!r}")
    # Each source is best effort: a broken curated file, flow directory or APK
    # must never take the other sources down with it.
    slug, curated = None, []
    try:
        slug, curated = load_curated(package)
    except Exception as e:  # noqa: BLE001
        log.warning("curated tools for %s unavailable: %s", package, e)
    info = None
    if apk_paths:
        try:
            info = manifest.analyze(package, [Path(p) for p in apk_paths])
        except Exception as e:  # noqa: BLE001
            log.warning("manifest analysis for %s failed: %s", package, e)
    slug = slug or app_slug(package, info.label if info else None)
    recorded: list[dict] = []
    try:
        recorded, _ = flows.load_flows(package, slug)
    except Exception as e:  # noqa: BLE001
        log.warning("recorded flows for %s unavailable: %s", package, e)
    generated: list[dict] = []
    if info:
        try:
            generated = manifest.generate_tools(info, slug)
        except Exception as e:  # noqa: BLE001
            log.warning("manifest tools for %s failed: %s", package, e)

    merged: list[dict] = []
    seen: set[str] = set()
    for tool in (*curated, *recorded, *generated):
        if tool["name"] not in seen:
            seen.add(tool["name"])
            merged.append(tool)
    return merged
