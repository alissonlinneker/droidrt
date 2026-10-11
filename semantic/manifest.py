"""Tools generated from an APK's AndroidManifest.xml.

The manifest is read with `aapt2 dump xmltree`, so nothing from the APK is
ever executed or unpacked to disk. The APK is untrusted: every value taken
from it (class names, schemes, hosts, paths, labels) is validated before it
lands in a tool name, description or intent template.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .schema import PACKAGE_RE, app_slug, clean_text, prefixed_name, sanitize_name

log = logging.getLogger(__name__)

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ACTION_MAIN = "android.intent.action.MAIN"
ACTION_VIEW = "android.intent.action.VIEW"
ACTION_SEND = "android.intent.action.SEND"
ACTION_SEND_MULTIPLE = "android.intent.action.SEND_MULTIPLE"
CATEGORY_LAUNCHER = "android.intent.category.LAUNCHER"
CATEGORY_BROWSABLE = "android.intent.category.BROWSABLE"
CATEGORY_DEFAULT = "android.intent.category.DEFAULT"
EXTRA_TEXT = "android.intent.extra.TEXT"

TOOL_LIMIT = 15
AAPT2_TIMEOUT = 60
MAX_APKS = 32
MAX_PATHS_IN_DESCRIPTION = 4

# Framework attribute resource ids. aapt2 prints them next to the name, and
# they survive when a shrinker strips the attribute names from the manifest.
ATTR_IDS = {
    0x01010001: "label",
    0x01010003: "name",
    0x01010006: "permission",
    0x0101000E: "enabled",
    0x01010010: "exported",
    0x01010026: "mimeType",
    0x01010027: "scheme",
    0x01010028: "host",
    0x01010029: "port",
    0x0101002A: "path",
    0x0101002B: "pathPrefix",
    0x0101002C: "pathPattern",
    0x01010202: "targetActivity",
    0x010104EE: "autoVerify",
    0x0101061E: "pathSuffix",
    0x01010270: "targetSdkVersion",
}

# Schemes that never make sense as an agent tool: local files, content
# providers that need grants, and URL kinds that run code.
SKIP_SCHEMES = frozenset({"file", "content", "data", "javascript", "about", "intent", "android-app", "*"})

SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{0,31}$")
HOST_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,62})(?:\.[a-z0-9_](?:[a-z0-9_-]{0,62}))*$")
PORT_RE = re.compile(r"^[0-9]{1,5}$")
CLASS_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$")
MIME_RE = re.compile(r"^[A-Za-z0-9*.+-]+/[A-Za-z0-9*.+-]+$")
ACTION_RE = re.compile(r"^[A-Za-z0-9_.$]{1,200}$")

LINE_RE = re.compile(r"^(?P<indent> *)(?P<kind>[ENA]): (?P<rest>.*)$")
ELEMENT_RE = re.compile(r"^(?P<tag>[A-Za-z0-9_:.-]+)(?: \(line=\d+\))?$")
ATTR_RE = re.compile(
    r"^(?:(?P<ns>\S*?):)?(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)?"
    r"(?:\((?P<rid>0x[0-9a-fA-F]+)\))?=(?P<value>.*)$"
)
QUOTED_RE = re.compile(r'^"(?P<v>.*)" \(Raw: ".*"\)$|^"(?P<w>.*)"$', re.S)


# Parsing --------------------------------------------------------------------


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element"] = field(default_factory=list)

    def iter(self, tag: str):
        for child in self.children:
            if child.tag == tag:
                yield child
            yield from child.iter(tag)

    def kids(self, *tags: str):
        return [c for c in self.children if c.tag in tags]


def _attr_value(raw: str) -> str:
    raw = raw.strip()
    if m := QUOTED_RE.match(raw):
        return m.group("v") if m.group("v") is not None else m.group("w")
    # Typed values look like "(type 0x12)0xffffffff"; keep only the value.
    raw = re.sub(r"^\(type 0x[0-9a-fA-F]+\)", "", raw)
    return raw


def parse_xmltree(text: str) -> Element | None:
    """Turn `aapt2 dump xmltree` output into a small element tree."""
    root: Element | None = None
    stack: list[tuple[int, Element]] = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        indent, kind, rest = len(m.group("indent")), m.group("kind"), m.group("rest")
        if kind == "E":
            em = ELEMENT_RE.match(rest.strip())
            if not em:
                continue
            el = Element(em.group("tag"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if stack:
                stack[-1][1].children.append(el)
            elif root is None:
                root = el
            else:
                continue
            stack.append((indent, el))
        elif kind == "A" and stack:
            am = ATTR_RE.match(rest)
            if not am:
                continue
            ns, name, rid = am.group("ns"), am.group("name"), am.group("rid")
            if ns and ns not in (ANDROID_NS, "android"):
                continue
            key = ATTR_IDS.get(int(rid, 16)) if rid else None
            key = key or name
            if not key:
                continue
            # The attribute belongs to the deepest element above this line.
            owner = stack[-1][1]
            for depth, el in reversed(stack):
                if depth < indent:
                    owner = el
                    break
            owner.attrs.setdefault(key, _attr_value(am.group("value")))
    return root


def _truthy(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("true", "0xffffffff", "-1", "1"):
        return True
    if v in ("false", "0x0", "0x00000000", "0"):
        return False
    return None


# Manifest model -------------------------------------------------------------


@dataclass
class DataSpec:
    scheme: str | None = None
    host: str | None = None
    port: str | None = None
    path: str | None = None
    pathPrefix: str | None = None
    pathPattern: str | None = None
    pathSuffix: str | None = None
    mimeType: str | None = None
    invalid: bool = False  # some attribute was present but failed validation


@dataclass
class IntentFilter:
    actions: list[str]
    categories: list[str]
    data: list[DataSpec]
    auto_verify: bool = False

    @property
    def usable(self) -> bool:
        # Dropping one bad value could widen the filter (a rejected host would
        # read as "any host"), so a filter with any rejected value is ignored.
        return not any(d.invalid for d in self.data)

    @property
    def schemes(self) -> list[str]:
        return _unique(d.scheme for d in self.data if d.scheme)

    @property
    def hosts(self) -> list[tuple[str, str | None]]:
        return _unique((d.host, d.port) for d in self.data if d.host)

    @property
    def paths(self) -> list[str]:
        out = []
        for d in self.data:
            if d.path:
                out.append(d.path)
            if d.pathPrefix:
                out.append(d.pathPrefix.rstrip("*") + "*")
            if d.pathPattern:
                out.append(d.pathPattern)
            if d.pathSuffix:
                out.append("*" + d.pathSuffix)
        return _unique(out)

    @property
    def mime_types(self) -> list[str]:
        return _unique(d.mimeType for d in self.data if d.mimeType)


@dataclass
class Activity:
    name: str
    exported: bool
    enabled: bool
    permission: str | None
    filters: list[IntentFilter]
    alias_of: str | None = None

    @property
    def simple_name(self) -> str:
        return self.name.rsplit(".", 1)[-1]


@dataclass
class DeepLink:
    scheme: str
    host: str | None
    port: str | None
    paths: list[str]
    activity: str
    verified: bool


@dataclass
class ManifestInfo:
    package: str
    label: str | None
    target_sdk: int | None
    activities: list[Activity]

    @property
    def exported(self) -> list[Activity]:
        return [a for a in self.activities if a.exported and a.enabled and not a.permission]

    def deep_links(self) -> list[DeepLink]:
        """Browsable VIEW targets, one per scheme and host, http folded into https."""
        found: dict[tuple[str, str | None, str | None], DeepLink] = {}
        for act in self.exported:
            for f in act.filters:
                if ACTION_VIEW not in f.actions or CATEGORY_BROWSABLE not in f.categories:
                    continue
                # "*" matches any host; "*.example.org" cannot be templated, so it is left out.
                hosts = [(None, None) if h == "*" else (h, p) for h, p in f.hosts if not h.startswith("*.")]
                if not f.hosts:
                    hosts = [(None, None)]
                for scheme in f.schemes:
                    if scheme in SKIP_SCHEMES:
                        continue
                    for host, port in hosts:
                        family = "https" if scheme in ("http", "https") else scheme
                        bare = host.removeprefix("www.") if host and family == "https" else host
                        key = (family, bare, port)
                        link = found.get(key)
                        if link is None:
                            found[key] = DeepLink(family, host, port, list(f.paths), act.name, f.auto_verify)
                        else:
                            link.paths = _unique(link.paths + f.paths)
                            link.verified = link.verified or f.auto_verify
        return list(found.values())

    def share_targets(self, multiple: bool = False) -> list[tuple[str, list[str]]]:
        """Activities accepting ACTION_SEND (or SEND_MULTIPLE) and their MIME types."""
        action = ACTION_SEND_MULTIPLE if multiple else ACTION_SEND
        out = []
        for act in self.exported:
            mimes: list[str] = []
            for f in act.filters:
                if action in f.actions:
                    mimes.extend(f.mime_types)
            if mimes:
                out.append((act.name, _unique(mimes)))
        return out

    def summary(self) -> dict:
        return {
            "package": self.package,
            "label": self.label,
            "activities": len(self.activities),
            "exported": len(self.exported),
            "deep_links": len(self.deep_links()),
            "share_targets": len(self.share_targets()),
            "share_multiple_targets": len(self.share_targets(multiple=True)),
        }


def _unique(items) -> list:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _class_name(package: str, raw: str | None) -> str | None:
    if not raw:
        return None
    name = raw.strip()
    if name.startswith("."):
        name = package + name
    elif "." not in name:
        name = f"{package}.{name}"
    return name if CLASS_RE.match(name) and len(name) <= 256 else None


def _safe_path(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 200 or any(c in value for c in "{}\"'<>`") or re.search(r"\s", value):
        return None
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in value):
        return None
    return value


def _valid_host(host: str) -> bool:
    """Plain host names plus the manifest wildcards "*" and "*.example.org"."""
    if host == "*":
        return True
    bare = host[2:] if host.startswith("*.") else host
    return len(bare) <= 253 and bool(HOST_RE.match(bare))


def _data_spec(el: Element) -> DataSpec:
    a = el.attrs
    scheme = (a.get("scheme") or "").lower() or None
    host = (a.get("host") or "").lower() or None
    port = a.get("port")
    mime = a.get("mimeType")
    spec = DataSpec(
        scheme=scheme if scheme and (scheme == "*" or SCHEME_RE.match(scheme)) else None,
        host=host if host and _valid_host(host) else None,
        port=port if port and PORT_RE.match(port) else None,
        path=_safe_path(a.get("path")),
        pathPrefix=_safe_path(a.get("pathPrefix")),
        pathPattern=_safe_path(a.get("pathPattern")),
        pathSuffix=_safe_path(a.get("pathSuffix")),
        mimeType=mime if mime and MIME_RE.match(mime) else None,
    )
    for key in ("scheme", "host", "port", "path", "pathPrefix", "pathPattern", "pathSuffix", "mimeType"):
        if a.get(key) and getattr(spec, key) is None:
            spec.invalid = True
    return spec


def _intent_filter(el: Element) -> IntentFilter:
    def names(tag):
        return _unique(
            n for n in (c.attrs.get("name") for c in el.kids(tag)) if n and ACTION_RE.match(n)
        )

    return IntentFilter(
        actions=names("action"),
        categories=names("category"),
        data=[_data_spec(d) for d in el.kids("data")],
        auto_verify=bool(_truthy(el.attrs.get("autoVerify"))),
    )


def parse_manifest(text: str, label: str | None = None) -> ManifestInfo | None:
    """Build a ManifestInfo from xmltree output. Returns None for split APKs."""
    root = parse_xmltree(text)
    if root is None or root.tag != "manifest":
        return None
    if root.attrs.get("split"):
        return None
    package = root.attrs.get("package", "")
    if not PACKAGE_RE.match(package):
        return None
    target_sdk = None
    for sdk in root.kids("uses-sdk"):
        try:
            target_sdk = int(sdk.attrs.get("targetSdkVersion", ""), 0)
        except ValueError:
            pass
    activities = []
    for app in root.kids("application"):
        for el in app.children:
            if el.tag not in ("activity", "activity-alias"):
                continue
            name = _class_name(package, el.attrs.get("name"))
            if not name:
                continue
            all_filters = [_intent_filter(f) for f in el.kids("intent-filter")]
            filters = [f for f in all_filters if f.usable]
            exported = _truthy(el.attrs.get("exported"))
            if exported is None:
                # Before API 31 a component with an intent filter is exported by default.
                exported = bool(all_filters)
            enabled = _truthy(el.attrs.get("enabled"))
            activities.append(Activity(
                name=name,
                exported=exported,
                enabled=enabled is not False,
                permission=el.attrs.get("permission") or None,
                filters=filters,
                alias_of=_class_name(package, el.attrs.get("targetActivity")) if el.tag == "activity-alias" else None,
            ))
    return ManifestInfo(package=package, label=safe_label(label),
                        target_sdk=target_sdk, activities=activities)


MAX_LABEL = 40


def safe_label(label: object) -> str | None:
    """The app label as it may appear in tool descriptions. It comes from the
    APK, so it is reduced to ASCII letters, digits, spaces and a few
    punctuation marks and kept short, which leaves no room for instructions
    aimed at the model reading the description."""
    text = clean_text(label, 200)
    text = re.sub(r"[^\w .&'+-]", "", text, flags=re.ASCII)
    text = re.sub(r" {2,}", " ", text).strip()[:MAX_LABEL].strip()
    return text or None


# aapt2 ----------------------------------------------------------------------


def _version_key(path: Path) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.-]", path.parent.name))


def find_aapt2() -> str | None:
    """Newest aapt2 from the Homebrew SDK, the Android Studio SDK or $ANDROID_HOME."""
    roots = []
    for var in ("DROIDRT_SDK", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]))
    roots += [Path("/opt/homebrew/share/android-commandlinetools"), Path.home() / "Library/Android/sdk"]
    for root in roots:
        found = sorted((root / "build-tools").glob("*/aapt2"), key=_version_key)
        if found:
            return str(found[-1])
    return None


def _run_aapt2(aapt2: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            [aapt2, *args], capture_output=True, timeout=AAPT2_TIMEOUT,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("aapt2 %s failed: %s", args[0], e)
        return None
    if proc.returncode != 0:
        log.warning("aapt2 %s exited with %d", args[0], proc.returncode)
        return None
    return proc.stdout.decode("utf-8", "replace")


def dump_manifest(apk: Path, aapt2: str) -> str | None:
    return _run_aapt2(aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk))


def dump_label(apk: Path, aapt2: str) -> str | None:
    text = _run_aapt2(aapt2, "dump", "badging", str(apk))
    if text and (m := re.search(r"^application-label:'([^'\n]*)'", text, re.M)):
        return m.group(1)
    return None


def analyze(package: str, apk_paths: list[Path], aapt2: str | None = None) -> ManifestInfo | None:
    """Read the base APK's manifest. Split APKs and other packages are ignored."""
    aapt2 = aapt2 or find_aapt2()
    if not aapt2:
        log.warning("aapt2 not found; no tools generated from the manifest")
        return None
    for apk in list(apk_paths)[:MAX_APKS]:
        apk = Path(apk)
        if not apk.is_file():
            continue
        text = dump_manifest(apk, aapt2)
        if not text:
            continue
        info = parse_manifest(text)
        if info is None or info.package != package:
            continue
        info.label = safe_label(dump_label(apk, aapt2))
        return info
    return None


# Tool generation ------------------------------------------------------------


def _intent(action: str, *, package: str, data: str | None = None, component: str | None = None,
            extras: dict | None = None, mime: str | None = None, categories: list | None = None) -> dict:
    return {
        "type": "intent", "action": action, "data": data, "package": package,
        "component": component, "extras": extras or {}, "mimeType": mime,
        "categories": categories or [],
    }


def _link_rank(link: DeepLink) -> int:
    if link.scheme == "https" and link.host:
        return 0 if link.verified else 2
    if link.scheme == "https":
        return 1  # accepts any web URL, as a browser does
    if link.host:
        return 3
    return 4


def _paths_note(paths: list[str]) -> str:
    if not paths:
        return ""
    shown = ", ".join("/" + p.lstrip("/") if not p.startswith("*") else p for p in paths[:MAX_PATHS_IN_DESCRIPTION])
    more = f" and {len(paths) - MAX_PATHS_IN_DESCRIPTION} more" if len(paths) > MAX_PATHS_IN_DESCRIPTION else ""
    return f" The app declares these paths: {shown}{more}."


def _link_tool(info: ManifestInfo, slug: str, app: str, link: DeepLink, single: bool) -> dict:
    pkg = info.package
    cats = [CATEGORY_BROWSABLE]
    if link.scheme == "https" and not link.host:
        return {
            "name": prefixed_name(slug, "open_url"),
            "description": f"Open a web address in {app}.",
            "package": pkg,
            "inputSchema": {"type": "object", "properties": {
                "url": {"type": "string", "description": "Full http or https URL."}}, "required": ["url"]},
            "action": _intent(ACTION_VIEW, package=pkg, data="{url}", categories=cats),
        }
    if not link.host:
        return {
            "name": prefixed_name(slug, "open", link.scheme, "uri"),
            "description": f"Open a {link.scheme}: URI in {app}.",
            "package": pkg,
            "inputSchema": {"type": "object", "properties": {
                "uri": {"type": "string", "description": f"Everything after '{link.scheme}:'."}}, "required": ["uri"]},
            "action": _intent(ACTION_VIEW, package=pkg, data=f"{link.scheme}:{{uri}}", categories=cats),
        }
    authority = link.host + (f":{link.port}" if link.port else "")
    if single:
        name = prefixed_name(slug, "open_link")
    elif link.scheme == "https":
        name = prefixed_name(slug, "open_link", link.host.removeprefix("www."))
    else:
        name = prefixed_name(slug, "open_link", link.scheme, link.host)
    verified = " Verified app link." if link.verified else ""
    return {
        "name": name,
        "description": (f"Open a {link.scheme}://{authority} link inside {app}.{verified}"
                        f"{_paths_note(link.paths)}"),
        "package": pkg,
        "inputSchema": {"type": "object", "properties": {"path": {
            "type": "string",
            "description": "Path after the host, without the leading slash, optionally followed by ?query. Empty opens the host root.",
        }}, "required": []},
        "action": _intent(ACTION_VIEW, package=pkg, data=f"{link.scheme}://{authority}/{{path}}", categories=cats),
    }


def _share_tool(info: ManifestInfo, slug: str, app: str) -> dict | None:
    best = None
    for act, mimes in info.share_targets():
        score = 2 if "text/plain" in mimes else 1 if ({"text/*", "*/*", "*"} & set(mimes)) else 0
        if score and (best is None or score > best[0]):
            best = (score, act)
    if not best:
        return None
    return {
        "name": prefixed_name(slug, "share_text"),
        "description": f"Share plain text with {app} through the Android share sheet target.",
        "package": info.package,
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Text to share."}}, "required": ["text"]},
        "action": _intent(ACTION_SEND, package=info.package, component=f"{info.package}/{best[1]}",
                          extras={EXTRA_TEXT: "{text}"}, mime="text/plain"),
    }


def _screen_name(act: Activity) -> str:
    base = act.simple_name.strip("$_")
    for suffix in ("Activity", "Act"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    return sanitize_name(base, 40) or "main"


def _activity_tools(info: ManifestInfo, slug: str, app: str) -> tuple[dict | None, list[dict]]:
    launcher, others = None, []
    for act in info.exported:
        launch = any(ACTION_MAIN in f.actions and CATEGORY_LAUNCHER in f.categories for f in act.filters)
        main = any(ACTION_MAIN in f.actions for f in act.filters)
        plain_view = any(ACTION_VIEW in f.actions and not f.schemes and not f.mime_types for f in act.filters)
        if not (main or plain_view):
            continue
        action = ACTION_MAIN if main else ACTION_VIEW
        screen = _screen_name(act)
        tool = {
            "name": prefixed_name(slug, "open", screen),
            "description": (f"Launch {app} on its main screen." if launch else
                            f"Open the {act.simple_name} screen of {app}."),
            "package": info.package,
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "action": _intent(action, package=info.package, component=f"{info.package}/{act.name}",
                              categories=[CATEGORY_LAUNCHER] if launch else []),
        }
        if launch and launcher is None:
            launcher = tool
        else:
            # Activities from the app's own namespace come before library ones.
            tool["_rank"] = 0 if act.name.startswith(info.package + ".") else 1
            others.append(tool)
    others.sort(key=lambda t: t.pop("_rank"))
    return launcher, others


def generate_tools(info: ManifestInfo, slug: str | None = None, limit: int = TOOL_LIMIT) -> list[dict]:
    """Tools for one app, deep links first, capped at `limit`."""
    label = safe_label(info.label)
    slug = slug or app_slug(info.package, label)
    app = label or info.package
    links = sorted(info.deep_links(), key=_link_rank)
    single = sum(1 for l in links if l.host) == 1
    link_tools = [_link_tool(info, slug, app, l, single) for l in links]
    share = _share_tool(info, slug, app)
    launcher, screens = _activity_tools(info, slug, app)
    reserved = [t for t in (share, launcher) if t]
    ordered = link_tools[: max(0, limit - len(reserved))] + reserved + link_tools[max(0, limit - len(reserved)):] + screens
    out, names = [], set()
    for tool in ordered:
        if len(out) >= limit:
            break
        base, n = tool["name"], 2
        while tool["name"] in names:
            suffix = f"_{n}"
            tool["name"] = base[: 64 - len(suffix)] + suffix
            n += 1
        names.add(tool["name"])
        out.append(tool)
    return out


def tools_from_apks(package: str, apk_paths: list[Path], slug: str | None = None) -> list[dict]:
    info = analyze(package, apk_paths)
    return generate_tools(info, slug) if info else []
