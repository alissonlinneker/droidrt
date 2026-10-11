#!/usr/bin/env python3
"""Extract a 1024x1024 macOS style PNG icon from one or more Android APKs.

The launcher icon is resolved through aapt2 (application icon from the
manifest, then the resource table of every APK, including density splits),
references are followed down to concrete drawables (bitmaps, colors, vector
drawables, layer-list, inset, shape, selector) and the result is described
as a JSON spec. A small Swift helper (compose.swift, built on demand by
build.sh) rasterizes the spec with CoreGraphics and applies the macOS mask.

APKs are treated as untrusted data: nothing from inside them is executed,
entries are only read into memory with size limits, every recursive lookup
is depth limited, the total work of one extraction is capped by a node and
path command budget, and bitmaps are only handed to the compositor after
their header dimensions have been checked.

Only the Python standard library is used (Python 3.10+).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPOSE_SRC = HERE / "compose.swift"
COMPOSE_BIN = HERE / "build" / "compose"
BUILD_SCRIPT = HERE / "build.sh"

MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 16
MAX_PATH_OPS = 200_000        # per path, while parsing pathData
MAX_NODES = 5_000             # drawable/paint/vector nodes per extraction
MAX_TOTAL_PATH_OPS = 200_000  # path commands summed over the whole icon
MAX_BITMAP_PX = 4096          # largest accepted bitmap side, in pixels
MAX_SPEC_BYTES = 16 * 1024 * 1024
MAX_BITMAPS = 64                         # distinct bitmap entries copied out per extraction
MAX_BITMAP_BYTES = 64 * 1024 * 1024      # their total size
MAX_XML_FILES = 256                      # distinct XML resources dumped per extraction
MAX_XML_ELEMENTS = 20_000                # elements kept from one xmltree dump
MAX_COORD = 1e6                          # largest magnitude accepted for a path coordinate
MAX_DUMP_BYTES = 256 * 1024 * 1024       # aapt2 output read for one command
MAX_XML_DUMP_BYTES = 16 * 1024 * 1024    # aapt2 output read for one xmltree dump
AAPT2_TIMEOUT = 120
COMPOSE_TIMEOUT = 20
BUILD_TIMEOUT = 600
ADAPTIVE_DP = 108.0
LEGACY_DP = 48.0

DENSITIES = {
    "ldpi": 120, "mdpi": 160, "tvdpi": 213, "hdpi": 240, "xhdpi": 320,
    "xxhdpi": 480, "xxxhdpi": 640, "nodpi": 5000, "anydpi": 10000,
}

# Public framework colors (android.R.color) that icons commonly reference.
FRAMEWORK_COLORS = {
    0x01060000: "#ffaaaaaa", 0x0106000B: "#ffffffff", 0x0106000C: "#ff000000",
    0x0106000D: "#00000000", 0x0106000E: "#ff000000", 0x0106000F: "#ffffffff",
    0x01060012: "#ff33b5e5", 0x01060013: "#ff0099cc", 0x01060014: "#ff99cc00",
    0x01060015: "#ff669900", 0x01060016: "#ffff4444", 0x01060017: "#ffcc0000",
    0x01060018: "#ffffbb33", 0x01060019: "#ffff8800", 0x0106001A: "#ffaa66cc",
    0x0106001B: "#ff00ddff",
}

RGBA = tuple[int, int, int, int]


class IconError(Exception):
    pass


class UnsafeBitmap(IconError):
    """A bitmap entry whose format is unknown or whose dimensions are too large."""


# ---------------------------------------------------------------- bitmap headers

def bitmap_dimensions(data: bytes) -> tuple[str, int, int] | None:
    """Read (format, width, height) from a PNG, WebP, JPEG or GIF header.

    Nothing is decoded: only the fixed header fields are parsed, so a file
    that claims a huge canvas costs nothing to inspect. Returns None for an
    unrecognized or truncated header.
    """
    def be16(o):
        return int.from_bytes(data[o:o + 2], "big")

    def le16(o):
        return int.from_bytes(data[o:o + 2], "little")

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        return "png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 " and len(data) >= 30:
            if data[23:26] != b"\x9d\x01\x2a":
                return None
            return "webp", le16(26) & 0x3FFF, le16(28) & 0x3FFF
        if chunk == b"VP8L" and len(data) >= 25:
            if data[20] != 0x2F:
                return None
            bits = int.from_bytes(data[21:25], "little")
            return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X" and len(data) >= 30:
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return "webp", w, h
        return None
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            if marker == 0xFF:  # fill byte
                i += 1
                continue
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker in (0xD9, 0xDA):  # end of image or scan data before any frame header
                return None
            length = be16(i + 2)
            if length < 2:
                return None
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 > len(data):
                    return None
                return "jpeg", be16(i + 7), be16(i + 5)
            i += 2 + length
        return None
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return "gif", le16(6), le16(8)
    return None


def check_bitmap(data: bytes, name: str = "bitmap") -> tuple[str, int, int]:
    """Validate a bitmap header; raise UnsafeBitmap unless it is small and recognized."""
    dims = bitmap_dimensions(data)
    if dims is None:
        raise UnsafeBitmap(f"unrecognized bitmap format: {name}")
    _, w, h = dims
    if not (0 < w <= MAX_BITMAP_PX and 0 < h <= MAX_BITMAP_PX):
        raise UnsafeBitmap(f"bitmap dimensions {w}x{h} exceed {MAX_BITMAP_PX}px: {name}")
    return dims


# ---------------------------------------------------------------- pure helpers

def parse_color(text: str) -> RGBA | None:
    """Parse an Android color literal (#RGB, #ARGB, #RRGGBB, #AARRGGBB) to RGBA ints."""
    t = text.strip()
    if not t.startswith("#"):
        return None
    h = t[1:]
    if not re.fullmatch(r"[0-9a-fA-F]+", h or "x"):
        return None
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    if len(h) == 6:
        h = "ff" + h
    if len(h) != 8:
        return None
    a, r, g, b = (int(h[i:i + 2], 16) for i in range(0, 8, 2))
    return (r, g, b, a)


def with_alpha(color: RGBA, factor: float) -> RGBA:
    r, g, b, a = color
    return (r, g, b, max(0, min(255, round(a * factor))))


def parse_dimension(text: str) -> tuple[float, str] | None:
    """Parse '24.000000dp', '12px', '0.5' or the aapt2 fraction form '0.25%'.

    aapt2 prints fractions as the raw fraction followed by '%' (0.65% means 65%),
    so the returned value for unit '%' or '%p' is already a 0..1 fraction.
    """
    m = re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(dp|dip|sp|px|pt|in|mm|%p|%)?\s*", text)
    if not m:
        return None
    value = float(m.group(1))
    if not math.isfinite(value):
        return None
    return value, (m.group(2) or "")


class _PathScanner:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def skip(self) -> None:
        while self.i < len(self.s) and (self.s[self.i].isspace() or self.s[self.i] == ","):
            self.i += 1

    def at_number(self) -> bool:
        self.skip()
        return self.i < len(self.s) and (self.s[self.i].isdigit() or self.s[self.i] in "+-.")

    def number(self) -> float:
        self.skip()
        m = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?").match(self.s, self.i)
        if not m:
            raise ValueError(f"bad number in pathData at {self.i}")
        self.i = m.end()
        value = float(m.group(0))
        if not (math.isfinite(value) and abs(value) <= MAX_COORD):
            raise ValueError(f"number out of range in pathData at {m.start()}")
        return value

    def flag(self) -> float:
        self.skip()
        if self.i < len(self.s) and self.s[self.i] in "01":
            self.i += 1
            return float(self.s[self.i - 1])
        raise ValueError(f"bad arc flag in pathData at {self.i}")


def _arc_to_cubics(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2):
    """Convert an SVG/VectorDrawable elliptical arc to cubic bezier segments."""
    if (x1 == x2 and y1 == y2):
        return []
    rx, ry = abs(rx), abs(ry)
    # Radii this small (or zero) would underflow when squared below; the arc
    # is a straight line for any practical purpose.
    if rx < 1e-9 or ry < 1e-9:
        return [("L", x2, y2)]
    phi = math.radians(phi_deg % 360)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cp * dx + sp * dy
    y1p = -sp * dx + cp * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if bool(large) == bool(sweep):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(1, math.ceil(abs(dt) / (math.pi / 2) - 1e-9))
    step = dt / n
    k = 4 / 3 * math.tan(step / 4)
    out = []
    t = t1
    for _ in range(n):
        c1, s1 = math.cos(t), math.sin(t)
        c2, s2 = math.cos(t + step), math.sin(t + step)
        p1 = (c1 - k * s1, s1 + k * c1)
        p2 = (c2 + k * s2, s2 - k * c2)
        p3 = (c2, s2)

        def tr(p):
            px, py = p[0] * rx, p[1] * ry
            return cp * px - sp * py + cx, sp * px + cp * py + cy

        a, b, c = tr(p1), tr(p2), tr(p3)
        out.append(("C", a[0], a[1], b[0], b[1], c[0], c[1]))
        t += step
    # Snap the final point to the exact target to avoid drift.
    last = out[-1]
    out[-1] = last[:5] + (x2, y2)
    return out


class PathTooLarge(ValueError):
    pass


def parse_path_data(text: str, max_ops: int = MAX_PATH_OPS) -> list[tuple]:
    """Parse VectorDrawable pathData into absolute ops using only M, L, C, Q and Z.

    Supports M L H V C S Q T A Z in absolute and relative forms, implicit
    command repetition (extra pairs after M become L), packed numbers such as
    '1.5.5' or '-1-2', packed arc flags and exponents.
    """
    sc = _PathScanner(text)
    ops: list[tuple] = []
    cx = cy = sx = sy = 0.0
    last_cubic_ctrl: tuple[float, float] | None = None
    last_quad_ctrl: tuple[float, float] | None = None
    cmd = None
    while True:
        sc.skip()
        if sc.i >= len(sc.s):
            break
        ch = sc.s[sc.i]
        if ch.isalpha():
            cmd = ch
            sc.i += 1
            if cmd not in "MmZzLlHhVvCcSsQqTtAa":
                raise ValueError(f"unknown pathData command {cmd!r}")
        elif cmd is None:
            raise ValueError("pathData must start with a command")
        elif cmd in "Zz":
            raise ValueError("numbers after Z")
        rel = cmd.islower()
        up = cmd.upper()
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        if up == "Z":
            ops.append(("Z",))
            cx, cy = sx, sy
            last_cubic_ctrl = last_quad_ctrl = None
            continue
        if up == "M":
            x, y = sc.number() + ox, sc.number() + oy
            ops.append(("M", x, y))
            cx, cy = sx, sy = x, y
            cmd = "l" if rel else "L"
            last_cubic_ctrl = last_quad_ctrl = None
        elif up == "L":
            x, y = sc.number() + ox, sc.number() + oy
            ops.append(("L", x, y))
            cx, cy = x, y
            last_cubic_ctrl = last_quad_ctrl = None
        elif up == "H":
            x = sc.number() + ox
            ops.append(("L", x, cy))
            cx = x
            last_cubic_ctrl = last_quad_ctrl = None
        elif up == "V":
            y = sc.number() + oy
            ops.append(("L", cx, y))
            cy = y
            last_cubic_ctrl = last_quad_ctrl = None
        elif up == "C":
            x1, y1 = sc.number() + ox, sc.number() + oy
            x2, y2 = sc.number() + ox, sc.number() + oy
            x, y = sc.number() + ox, sc.number() + oy
            ops.append(("C", x1, y1, x2, y2, x, y))
            last_cubic_ctrl, last_quad_ctrl = (x2, y2), None
            cx, cy = x, y
        elif up == "S":
            if last_cubic_ctrl:
                x1, y1 = 2 * cx - last_cubic_ctrl[0], 2 * cy - last_cubic_ctrl[1]
            else:
                x1, y1 = cx, cy
            x2, y2 = sc.number() + ox, sc.number() + oy
            x, y = sc.number() + ox, sc.number() + oy
            ops.append(("C", x1, y1, x2, y2, x, y))
            last_cubic_ctrl, last_quad_ctrl = (x2, y2), None
            cx, cy = x, y
        elif up == "Q":
            x1, y1 = sc.number() + ox, sc.number() + oy
            x, y = sc.number() + ox, sc.number() + oy
            ops.append(("Q", x1, y1, x, y))
            last_quad_ctrl, last_cubic_ctrl = (x1, y1), None
            cx, cy = x, y
        elif up == "T":
            if last_quad_ctrl:
                x1, y1 = 2 * cx - last_quad_ctrl[0], 2 * cy - last_quad_ctrl[1]
            else:
                x1, y1 = cx, cy
            x, y = sc.number() + ox, sc.number() + oy
            ops.append(("Q", x1, y1, x, y))
            last_quad_ctrl, last_cubic_ctrl = (x1, y1), None
            cx, cy = x, y
        elif up == "A":
            rx, ry, rot = sc.number(), sc.number(), sc.number()
            large, sweep = sc.flag(), sc.flag()
            x, y = sc.number() + ox, sc.number() + oy
            ops.extend(_arc_to_cubics(cx, cy, rx, ry, rot, large, sweep, x, y))
            cx, cy = x, y
            last_cubic_ctrl = last_quad_ctrl = None
        if len(ops) > max_ops:
            raise PathTooLarge("pathData too large")
        # Further numbers without a new letter repeat the current command.
    return ops


def config_score(config: str) -> tuple:
    """Rank a resource configuration; higher is better for a launcher icon."""
    parts = [p for p in config.split("-") if p]
    density = 160
    version = 0
    penalty = 0
    for p in parts:
        if p in DENSITIES:
            density = DENSITIES[p]
        elif re.fullmatch(r"v\d+", p):
            version = int(p[1:])
        else:
            penalty += 1
    return (-penalty, density, version)


# ---------------------------------------------------------------- aapt2 access

def find_aapt2() -> str | None:
    if env := os.environ.get("AAPT2"):
        return env if Path(env).is_file() else None
    roots = []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]))
    roots += [Path("/opt/homebrew/share/android-commandlinetools"), Path.home() / "Library/Android/sdk"]
    for root in roots:
        found = sorted((root / "build-tools").glob("*/aapt2"),
                       key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", p.parent.name)])
        if found:
            return str(found[-1])
    return shutil.which("aapt2")


def _run(args: list[str], timeout: float = AAPT2_TIMEOUT, limit: int = MAX_DUMP_BYTES) -> str:
    """Run aapt2 and return its stdout, bounded in time and in size.

    Raises subprocess.TimeoutExpired past `timeout` seconds and IconError when
    the output grows beyond `limit` bytes; the child is killed in both cases.
    """
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as proc:
        try:
            fd = proc.stdout.fileno()
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                ready, _, _ = select.select([fd], [], [], left)
                if not ready:
                    continue
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise IconError(f"aapt2 output larger than {limit} bytes")
                chunks.append(chunk)
        finally:
            if proc.poll() is None:
                proc.kill()
    return b"".join(chunks).decode("utf-8", "replace")


@dataclass
class Entry:
    config: str
    value: str
    apk: Path


@dataclass
class XmlNode:
    name: str
    attrs: dict
    children: list


def parse_resources_dump(text: str, apk: Path, table: dict, paths: dict, names: dict | None = None) -> None:
    """Parse `aapt2 dump resources` output into {resid: [Entry]} and {path: resid}.

    When `names` is given it also receives {"type/name": resid}. aapt2 prints
    references between entries of the app package by name (`@color/logo`),
    not by id, so this index is needed to follow them.
    """
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s+resource (0x[0-9a-fA-F]{8}) (\S+)", line)
        if m:
            current = int(m.group(1), 16)
            if names is not None:
                names.setdefault(m.group(2), current)
            continue
        if current is None:
            continue
        m = re.match(r"^\s+\(([^)]*)\)\s+(.*)$", line)
        if not m:
            continue
        cfg, value = m.group(1), m.group(2).strip()
        table.setdefault(current, []).append(Entry(cfg, value, apk))
        fm = re.match(r"^\(file\)\s+(\S+)", value)
        if fm:
            paths.setdefault(fm.group(1), current)


def parse_xmltree(text: str) -> XmlNode | None:
    """Parse `aapt2 dump xmltree` output into a small element tree."""
    root = None
    stack: list[tuple[int, XmlNode]] = []
    count = 0
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped.startswith("E: "):
            m = re.match(r"E: (\S+)", stripped)
            if not m:
                continue  # an element without a printable name
            count += 1
            if count > MAX_XML_ELEMENTS:
                raise IconError("xml resource too large")
            node = XmlNode(m.group(1), {}, [])
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if stack:
                stack[-1][1].children.append(node)
            elif root is None:
                root = node
            stack.append((indent, node))
        elif stripped.startswith("A: "):
            body = stripped[3:]
            if "=" not in body:
                continue
            key, value = body.split("=", 1)
            key = re.sub(r"\(0x[0-9a-fA-F]+\)$", "", key)
            key = key.rsplit(":", 1)[-1]
            if value.startswith('"'):
                end = value.find('"', 1)
                value = value[1:end] if end > 0 else value[1:]
            else:
                value = re.sub(r'\s+\(Raw: .*\)$', "", value).strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if stack:
                stack[-1][1].attrs[key] = value
    return root


class Resources:
    """Merged view of the resource tables of a base APK plus its splits."""

    def __init__(self, aapt2: str, apks: list[Path], workdir: Path):
        self.aapt2 = aapt2
        self.apks = apks
        self.workdir = workdir
        self.table: dict[int, list[Entry]] = {}
        self.paths: dict[str, int] = {}
        self.names: dict[str, int] = {}
        self._xml: dict[tuple, XmlNode | None] = {}
        self._best: dict[tuple, Entry | None] = {}
        self._zips: dict[str, zipfile.ZipFile | None] = {}
        self._n = 0
        self._bitmap_bytes = 0
        self._xml_dumps = 0
        for apk in apks:
            parse_resources_dump(_run([aapt2, "dump", "resources", str(apk)]), apk, self.table, self.paths,
                                 self.names)

    def close(self) -> None:
        for z in self._zips.values():
            if z is not None:
                z.close()
        self._zips.clear()

    def __enter__(self) -> "Resources":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _zip(self, apk: Path) -> zipfile.ZipFile | None:
        """Open each APK once per extraction; None when it is not a readable zip."""
        key = str(apk)
        if key not in self._zips:
            try:
                self._zips[key] = zipfile.ZipFile(apk)
            except (zipfile.BadZipFile, OSError):
                self._zips[key] = None
        return self._zips[key]

    def best(self, resid: int, want_file: bool | None = None) -> Entry | None:
        key = (resid, want_file)
        if key in self._best:
            return self._best[key]
        entries = self.table.get(resid) or []
        if want_file is not None:
            filtered = [e for e in entries if e.value.startswith("(file)") == want_file]
            entries = filtered or entries
        usable = [e for e in entries if self._entry_available(e)]
        pick = max(usable, key=lambda e: config_score(e.config)) if usable else None
        self._best[key] = pick
        return pick

    def _entry_available(self, e: Entry) -> bool:
        fm = re.match(r"^\(file\)\s+(\S+)", e.value)
        if not fm:
            return True
        z = self._zip(e.apk)
        if z is None:
            return False
        try:
            z.getinfo(fm.group(1))
            return True
        except KeyError:
            return False

    def read_bytes(self, apk: Path, name: str) -> bytes:
        z = self._zip(apk)
        if z is None:
            raise IconError(f"unreadable APK: {apk}")
        try:
            info = z.getinfo(name)
        except KeyError:
            raise UnsafeBitmap(f"missing entry: {name}") from None
        if info.file_size > MAX_ENTRY_BYTES:
            raise IconError(f"entry too large: {name}")
        try:
            with z.open(info) as f:
                data = f.read(MAX_ENTRY_BYTES + 1)
        except Exception as exc:  # noqa: BLE001
            # Encrypted entries (RuntimeError), unknown compression methods
            # (NotImplementedError), corrupt deflate streams (zlib.error), CRC errors.
            raise UnsafeBitmap(f"unreadable entry {name}: {exc}") from None
        if len(data) > MAX_ENTRY_BYTES:
            raise IconError(f"entry too large: {name}")
        return data

    def extract(self, apk: Path, name: str) -> Path:
        """Copy a bitmap entry to the work dir after validating its header.

        Raises UnsafeBitmap when the format is not PNG, WebP, JPEG or GIF, or
        when either side exceeds MAX_BITMAP_PX, so the compositor never sees it.
        """
        if self._n >= MAX_BITMAPS:
            raise IconError("icon too complex")
        data = self.read_bytes(apk, name)
        fmt, _, _ = check_bitmap(data, name)
        self._bitmap_bytes += len(data)
        if self._bitmap_bytes > MAX_BITMAP_BYTES:
            raise IconError("icon too complex")
        self._n += 1
        out = self.workdir / f"res{self._n}.{fmt}"
        # The work dir is private (mkdtemp); O_EXCL refuses anything already there.
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return out

    def xml(self, apk: Path, name: str) -> XmlNode | None:
        key = (str(apk), name)
        if key not in self._xml:
            self._xml_dumps += 1
            if self._xml_dumps > MAX_XML_FILES:
                raise IconError("icon too complex")
            self._xml[key] = parse_xmltree(_run([self.aapt2, "dump", "xmltree", "--file", name, str(apk)],
                                                limit=MAX_XML_DUMP_BYTES))
        return self._xml[key]


# ---------------------------------------------------------------- resolution

def _ref_id(value: str) -> int | None:
    m = re.fullmatch(r"@\+?(?:\w+:)?(0x[0-9a-fA-F]{8})", value.strip())
    return int(m.group(1), 16) if m else None


def _ref_name(value: str) -> str | None:
    """'type/name' of a symbolic reference such as '@color/logo' or '@pkg:drawable/bg'.

    Framework references ('@android:...') are left out: aapt2 prints those by
    id and the app resource table cannot answer them anyway.
    """
    m = re.fullmatch(r"@\+?(?:([\w.]+):)?(\w+/[\w.]+)", value.strip())
    if not m or m.group(1) == "android":
        return None
    return m.group(2)


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def _num(value, default: float = 0.0, lo: float = -MAX_COORD, hi: float = MAX_COORD) -> float:
    """A finite number from an attribute, clamped to [lo, hi]."""
    if value is None:
        return default
    d = parse_dimension(str(value))
    return _clamp(d[0], lo, hi) if d else default


def _enum(value, names: dict[str, int], default: int) -> int:
    """One of the values of `names`, from its name or its integer form."""
    if value is None:
        return default
    v = str(value).strip()
    if v in names:
        return names[v]
    try:
        n = int(v, 0) if len(v) <= 32 else default
    except ValueError:
        return default
    return n if n in names.values() else default


def _gradient_stops(node: XmlNode, res: "Resolver", depth: int) -> list:
    stops = []
    for item in node.children:
        res._charge()
        if item.name != "item":
            continue
        c = res.color(item.attrs.get("color"), depth + 1)
        if c is not None:
            stops.append([_num(item.attrs.get("offset"), 0.0, 0.0, 1.0), list(c)])
    if stops:
        stops.sort(key=lambda s: s[0])
        return stops
    sc = res.color(node.attrs.get("startColor"), depth + 1)
    cc = res.color(node.attrs.get("centerColor"), depth + 1)
    ec = res.color(node.attrs.get("endColor"), depth + 1)
    seq = [c for c in (sc, cc, ec) if c is not None]
    if cc is not None and sc is not None and ec is not None:
        return [[0.0, list(sc)], [0.5, list(cc)], [1.0, list(ec)]]
    if len(seq) == 1:
        return [[0.0, list(seq[0])], [1.0, list(seq[0])]]
    if len(seq) >= 2:
        return [[0.0, list(seq[0])], [1.0, list(seq[-1])]]
    return []


class Resolver:
    """Turns resource references into spec nodes under a global work budget.

    Untrusted resources can reference each other many times over (a layer-list
    whose items all point at the next layer-list, and so on), which grows
    exponentially even with a depth limit. Three guards keep one extraction
    bounded:

    * every resolved node is charged against MAX_NODES and every emitted path
      command against MAX_TOTAL_PATH_OPS; going over raises IconError;
    * resolutions are memoized per (resource, bounds, depth), and a memo hit
      charges the full cost of the subtree it reuses, so the budget also
      bounds the size of the expanded spec handed to the compositor (depth is
      part of the key so a result cut short by MAX_DEPTH deep in the tree is
      never reused higher up);
    * a resource that references itself (with the same bounds) while it is
      still being resolved is a cycle and resolves to nothing.
    """

    def __init__(self, resources: Resources | None, max_nodes: int = MAX_NODES,
                 max_path_ops: int = MAX_TOTAL_PATH_OPS):
        self.r = resources
        self.max_nodes = max_nodes
        self.max_path_ops = max_path_ops
        self.nodes = 0
        self.path_ops = 0
        self._memo: dict[tuple, tuple] = {}
        self._active: set[tuple] = set()

    # ---- budget and memoization

    def _charge(self, nodes: int = 1, path_ops: int = 0) -> None:
        self.nodes += nodes
        self.path_ops += path_ops
        if self.nodes > self.max_nodes or self.path_ops > self.max_path_ops:
            raise IconError("icon too complex")

    def _memoized(self, key: tuple, compute, depth: int = 0):
        """Return compute() once per (key, depth), charging the cached cost on every reuse.

        `key` identifies the resource and its bounds; re-entering a key that is
        still being resolved, at any depth, is a reference cycle.
        """
        memo_key = key + (depth,)
        if memo_key in self._memo:
            result, nodes, ops = self._memo[memo_key]
            self._charge(nodes, ops)
            return result
        if key in self._active:
            return None  # reference cycle
        self._active.add(key)
        n0, p0 = self.nodes, self.path_ops
        try:
            result = compute()
        finally:
            self._active.discard(key)
        self._memo[memo_key] = (result, self.nodes - n0, self.path_ops - p0)
        return result

    def _rid(self, value: str) -> int | None:
        """Resource id of an '@0x...' or '@type/name' reference, None otherwise."""
        rid = _ref_id(value)
        if rid is None and self.r:
            name = _ref_name(value)
            if name is not None:
                rid = self.r.names.get(name)
        return rid

    # ---- colors and paints

    def color(self, value, depth: int = 0) -> RGBA | None:
        p = self.paint(value, depth)
        if p and "color" in p:
            return tuple(p["color"])
        if p and "gradient" in p and p["gradient"]["stops"]:
            return tuple(p["gradient"]["stops"][0][1])
        return None

    def paint(self, value, depth: int = 0) -> dict | None:
        """Resolve a color value, reference or color/gradient XML into a paint dict."""
        if value is None or depth > MAX_DEPTH:
            return None
        self._charge()
        v = str(value).strip()
        if c := parse_color(v):
            return {"color": list(c)}
        rid = self._rid(v)
        if rid is None:
            return None
        if rid in FRAMEWORK_COLORS:
            return {"color": list(parse_color(FRAMEWORK_COLORS[rid]))}
        if not self.r:
            return None
        return self._memoized(("paint", rid), lambda: self._paint_ref(rid, depth), depth)

    def _paint_ref(self, rid: int, depth: int) -> dict | None:
        e = self.r.best(rid)
        if not e:
            return None
        fm = re.match(r"^\(file\)\s+(\S+)", e.value)
        if not fm:
            return self.paint(e.value, depth + 1)
        if not fm.group(1).endswith(".xml"):
            return None
        node = self.r.xml(e.apk, fm.group(1))
        return self.paint_xml(node, depth + 1) if node else None

    def paint_xml(self, node: XmlNode, depth: int) -> dict | None:
        if node.name == "selector":
            self._charge(len(node.children))
            items = [i for i in node.children if i.name == "item"]
            plain = [i for i in items if not any(k.startswith("state_") for k in i.attrs)]
            pick = (plain or items or [None])[0]
            if pick is None:
                return None
            c = self.color(pick.attrs.get("color"), depth + 1)
            if c is None:
                return None
            alpha = _num(pick.attrs.get("alpha"), 1.0, 0.0, 1.0)
            return {"color": list(with_alpha(c, alpha))}
        if node.name == "gradient":
            return {"gradient": self.vector_gradient(node, depth)}
        return None

    def vector_gradient(self, node: XmlNode, depth: int) -> dict:
        a = node.attrs
        kind = _enum(a.get("type"), {"linear": 0, "radial": 1, "sweep": 2}, 0)
        return {
            "type": ["linear", "radial", "sweep"][kind if kind in (0, 1, 2) else 0],
            "x1": _num(a.get("startX")), "y1": _num(a.get("startY")),
            "x2": _num(a.get("endX")), "y2": _num(a.get("endY")),
            "cx": _num(a.get("centerX")), "cy": _num(a.get("centerY")),
            "r": _num(a.get("gradientRadius"), 0.0, 0.0),
            "stops": _gradient_stops(node, self, depth),
        }

    # ---- dimensions

    def fraction(self, value, bounds_dp: float, depth: int = 0) -> float:
        """Convert an inset or radius value into a fraction of bounds_dp."""
        if value is None or depth > MAX_DEPTH:
            return 0.0
        v = str(value).strip()
        rid = self._rid(v)
        if rid is not None and self.r:
            self._charge()
            e = self.r.best(rid)
            return self.fraction(e.value, bounds_dp, depth + 1) if e else 0.0
        d = parse_dimension(v)
        if not d:
            return 0.0
        num, unit = d
        frac = num if unit in ("%", "%p") else (num / bounds_dp if bounds_dp else 0.0)
        # Insets, radii and stroke widths beyond twice the bounds draw nothing useful.
        return _clamp(frac, -2.0, 2.0)

    def dp(self, value, default: float, depth: int = 0) -> float:
        if value is None or depth > MAX_DEPTH:
            return default
        v = str(value).strip()
        rid = self._rid(v)
        if rid is not None and self.r:
            self._charge()
            e = self.r.best(rid)
            return self.dp(e.value, default, depth + 1) if e else default
        d = parse_dimension(v)
        return d[0] if d and d[1] not in ("%", "%p") else default

    # ---- drawables

    def drawable(self, value, bounds_dp: float, depth: int = 0) -> dict | None:
        if value is None or depth > MAX_DEPTH:
            return None
        self._charge()
        v = str(value).strip()
        if c := parse_color(v):
            return {"type": "color", "color": list(c)}
        rid = self._rid(v)
        if rid is None:
            return None
        if rid in FRAMEWORK_COLORS:
            return {"type": "color", "color": list(parse_color(FRAMEWORK_COLORS[rid]))}
        if not self.r:
            return None

        def compute():
            e = self.r.best(rid)
            return self.entry_drawable(e, bounds_dp, depth + 1) if e else None

        return self._memoized(("drawable", rid, round(bounds_dp, 6)), compute, depth)

    def entry_drawable(self, e: Entry, bounds_dp: float, depth: int) -> dict | None:
        if depth > MAX_DEPTH:
            return None
        self._charge()
        fm = re.match(r"^\(file\)\s+(\S+)", e.value)
        if not fm:
            return self.drawable(e.value, bounds_dp, depth + 1)
        name = fm.group(1)
        if name.endswith(".xml"):
            def compute():
                node = self.r.xml(e.apk, name)
                return self.xml_drawable(node, bounds_dp, depth + 1) if node else None
            return self._memoized(("xml", str(e.apk), name, round(bounds_dp, 6)), compute, depth)
        if re.search(r"\.(png|webp|jpe?g|gif)$", name, re.I):
            def extract():
                try:
                    return {"type": "bitmap", "path": str(self.r.extract(e.apk, name))}
                except UnsafeBitmap as exc:
                    sys.stderr.write(f"iconkit: skipping {exc}\n")
                    return None
            return self._memoized(("bitmap", str(e.apk), name), extract)
        return None

    def _child_drawable(self, node: XmlNode, bounds_dp: float, depth: int) -> dict | None:
        if "drawable" in node.attrs:
            return self.drawable(node.attrs["drawable"], bounds_dp, depth + 1)
        for ch in node.children:
            self._charge()
            d = self.xml_drawable(ch, bounds_dp, depth + 1)
            if d:
                return d
        return None

    def xml_drawable(self, node: XmlNode, bounds_dp: float, depth: int) -> dict | None:
        if depth > MAX_DEPTH:
            return None
        self._charge()
        n, a = node.name, node.attrs
        if n == "vector":
            return self.vector(node, depth)
        if n in ("bitmap", "nine-patch"):
            return self.drawable(a.get("src"), bounds_dp, depth + 1)
        if n == "color":
            p = self.paint(a.get("color"), depth + 1)
            return {"type": "color", "color": p["color"]} if p and "color" in p else None
        if n == "shape":
            return self.shape(node, bounds_dp, depth)
        if n == "inset":
            all_in = a.get("inset")
            ins = [self.fraction(a.get(k, all_in), bounds_dp, depth + 1)
                   for k in ("insetLeft", "insetTop", "insetRight", "insetBottom")]
            inner_dp = bounds_dp * max(0.05, 1 - ins[0] - ins[2])
            child = self._child_drawable(node, inner_dp, depth)
            return {"type": "layers", "items": [{"inset": ins, "drawable": child}]} if child else None
        if n in ("layer-list", "ripple", "transition"):
            items = []
            for it in node.children:
                self._charge()
                if it.name != "item":
                    continue
                if n == "ripple" and it.attrs.get("id"):
                    continue  # ripple mask layer
                ins = [self.fraction(it.attrs.get(k), bounds_dp, depth + 1)
                       for k in ("left", "top", "right", "bottom")]
                inner_dp = bounds_dp * max(0.05, 1 - ins[0] - ins[2])
                child = self._child_drawable(it, inner_dp, depth)
                if child:
                    items.append({"inset": ins, "drawable": child})
            return {"type": "layers", "items": items} if items else None
        if n in ("selector", "animated-selector", "level-list", "state-list"):
            self._charge(len(node.children))
            items = [i for i in node.children if i.name == "item"]
            plain = [i for i in items if not any(k.startswith("state_") for k in i.attrs)]
            for it in plain + [i for i in items if i not in plain]:
                d = self._child_drawable(it, bounds_dp, depth)
                if d:
                    return d
            return None
        if n in ("animated-vector", "rotate", "scale", "clip", "animation-list", "maskable-icon"):
            if n == "animation-list":
                self._charge(len(node.children))
                for it in node.children:
                    d = self._child_drawable(it, bounds_dp, depth)
                    if d:
                        return d
                return None
            return self._child_drawable(node, bounds_dp, depth)
        if n == "adaptive-icon":
            return self.adaptive(node, depth)
        return None

    def shape(self, node: XmlNode, bounds_dp: float, depth: int) -> dict:
        a = node.attrs
        kind = _enum(a.get("shape"), {"rectangle": 0, "oval": 1, "line": 2, "ring": 3}, 0)
        out = {"type": "shape", "shape": "oval" if kind == 1 else "rect", "fill": None,
               "radius": 0.0, "stroke": None, "strokeWidth": 0.0}
        self._charge(len(node.children))
        for ch in node.children:
            ca = ch.attrs
            if ch.name == "solid":
                out["fill"] = self.paint(ca.get("color"), depth + 1)
            elif ch.name == "corners":
                out["radius"] = self.fraction(
                    ca.get("radius") or ca.get("topLeftRadius"), bounds_dp, depth + 1)
            elif ch.name == "stroke":
                out["stroke"] = self.paint(ca.get("color"), depth + 1)
                out["strokeWidth"] = self.fraction(ca.get("width"), bounds_dp, depth + 1)
            elif ch.name == "gradient":
                out["fill"] = {"gradient": self.shape_gradient(ch, bounds_dp, depth)}
        if out["fill"] is None and out["stroke"] is None:
            out["fill"] = None
        return out

    def shape_gradient(self, node: XmlNode, bounds_dp: float, depth: int) -> dict:
        """Shape gradients live in unit space (0..1 over the drawable bounds)."""
        a = node.attrs
        kind = _enum(a.get("type"), {"linear": 0, "radial": 1, "sweep": 2}, 0)
        ang = math.radians(_num(a.get("angle"), 0.0))
        dx, dy = math.cos(ang), -math.sin(ang)
        cx = self.fraction(a.get("centerX"), 1.0, depth + 1) if a.get("centerX") else 0.5
        cy = self.fraction(a.get("centerY"), 1.0, depth + 1) if a.get("centerY") else 0.5
        radius = abs(self.fraction(a.get("gradientRadius"), bounds_dp, depth + 1)) or 0.5
        return {
            "type": ["linear", "radial", "sweep"][kind if kind in (0, 1, 2) else 0],
            "x1": 0.5 - dx / 2, "y1": 0.5 - dy / 2, "x2": 0.5 + dx / 2, "y2": 0.5 + dy / 2,
            "cx": cx, "cy": cy, "r": radius,
            "stops": _gradient_stops(node, self, depth),
        }

    def vector(self, node: XmlNode, depth: int) -> dict:
        a = node.attrs
        # A viewport this small would blow the scale up to infinity in the compositor.
        vw = _num(a.get("viewportWidth"), 24.0)
        vh = _num(a.get("viewportHeight"), 24.0)
        vw = vw if vw >= 1e-3 else 24.0
        vh = vh if vh >= 1e-3 else 24.0
        tint = self.color(a.get("tint"), depth + 1) if a.get("tint") else None
        return {
            "type": "vector", "vw": vw, "vh": vh,
            "w": _clamp(self.dp(a.get("width"), vw), 0.0, MAX_COORD),
            "h": _clamp(self.dp(a.get("height"), vh), 0.0, MAX_COORD),
            "alpha": _num(a.get("alpha"), 1.0, 0.0, 1.0),
            "tint": list(tint) if tint else None,
            "children": self.vector_children(node, depth),
        }

    def vector_children(self, node: XmlNode, depth: int) -> list:
        out = []
        for ch in node.children:
            a = ch.attrs
            if ch.name in ("group", "path", "clip-path"):
                self._charge()
            if ch.name == "group":
                out.append({
                    "kind": "group",
                    "rotation": _num(a.get("rotation"), 0.0, -3600.0, 3600.0), "pivotX": _num(a.get("pivotX")),
                    "pivotY": _num(a.get("pivotY")), "scaleX": _num(a.get("scaleX"), 1.0, -1e4, 1e4),
                    "scaleY": _num(a.get("scaleY"), 1.0, -1e4, 1e4), "translateX": _num(a.get("translateX")),
                    "translateY": _num(a.get("translateY")),
                    "children": self.vector_children(ch, depth + 1) if depth < MAX_DEPTH * 4 else [],
                })
            elif ch.name in ("path", "clip-path"):
                remaining = max(0, self.max_path_ops - self.path_ops)
                try:
                    ops = parse_path_data(a.get("pathData", ""), min(MAX_PATH_OPS, remaining))
                except PathTooLarge:
                    raise IconError("icon too complex") from None
                except (ValueError, ArithmeticError):
                    continue  # malformed or numerically degenerate path: drop it
                self._charge(0, len(ops))
                d = [list(op) for op in ops]
                if ch.name == "clip-path":
                    out.append({"kind": "clip", "d": d})
                    continue
                fill = self.paint(a.get("fillColor"), depth + 1) if a.get("fillColor") else None
                if fill is None and str(a.get("fillColor", "")).startswith("?"):
                    fill = {"color": [0, 0, 0, 255]}  # unresolvable theme attribute
                stroke = self.paint(a.get("strokeColor"), depth + 1) if a.get("strokeColor") else None
                out.append({
                    "kind": "path", "d": d, "fill": fill, "stroke": stroke,
                    "fillAlpha": _num(a.get("fillAlpha"), 1.0, 0.0, 1.0),
                    "strokeAlpha": _num(a.get("strokeAlpha"), 1.0, 0.0, 1.0),
                    "strokeWidth": _num(a.get("strokeWidth"), 0.0, 0.0, 1e4),
                    "evenOdd": _enum(a.get("fillType"), {"nonZero": 0, "evenOdd": 1}, 0) == 1,
                    "cap": _enum(a.get("strokeLineCap"), {"butt": 0, "round": 1, "square": 2}, 0),
                    "join": _enum(a.get("strokeLineJoin"), {"miter": 0, "round": 1, "bevel": 2}, 0),
                    "miter": _num(a.get("strokeMiterLimit"), 4.0, 1.0, 1e3),
                })
        return out

    def adaptive(self, node: XmlNode, depth: int) -> dict:
        layers = {}
        self._charge(len(node.children))
        for ch in node.children:
            if ch.name in ("background", "foreground"):
                layers[ch.name] = self._child_drawable(ch, ADAPTIVE_DP, depth)
        return {"type": "adaptive", "background": layers.get("background"),
                "foreground": layers.get("foreground")}


# ---------------------------------------------------------------- icon lookup

def _badging_icon(aapt2: str, apks: list[Path]) -> tuple[Path, str] | None:
    for apk in apks:
        text = _run([aapt2, "dump", "badging", str(apk)])
        m = re.search(r"^application: .*?icon='([^']+)'", text, re.M)
        if m:
            return apk, m.group(1)
        dens = re.findall(r"^application-icon-(\d+):'([^']+)'", text, re.M)
        if dens:
            return apk, max(dens, key=lambda d: int(d[0]))[1]
        m = re.search(r"^launchable-activity: .*?icon='([^']+)'", text, re.M)
        if m:
            return apk, m.group(1)
    return None


def build_spec(apks: list[Path], workdir: Path, aapt2: str | None = None) -> dict | None:
    """Resolve the launcher icon of the APK set into a compose spec."""
    aapt2 = aapt2 or find_aapt2()
    if not aapt2:
        raise IconError("aapt2 not found")
    found = _badging_icon(aapt2, apks)
    if not found:
        return None
    base_apk, icon_path = found
    with Resources(aapt2, apks, workdir) as res:
        return spec_from_resources(res, base_apk, icon_path)


def spec_from_resources(res: Resources, base_apk: Path, icon_path: str) -> dict | None:
    """Resolve icon_path against a loaded resource set; raises IconError when too complex."""
    resolver = Resolver(res)
    rid = res.paths.get(icon_path)
    node = None
    if rid is not None:
        # Prefer the adaptive XML when present, otherwise the densest raster.
        e = res.best(rid)
        if e:
            node = resolver.entry_drawable(e, ADAPTIVE_DP, 0)
        if node is None:
            e = res.best(rid, want_file=True)
            if e:
                node = resolver.entry_drawable(e, ADAPTIVE_DP, 0)
    if node is None:
        node = resolver.entry_drawable(Entry("", f"(file) {icon_path}", base_apk), ADAPTIVE_DP, 0)
    if node is None:
        return None
    if node.get("type") == "adaptive":
        if not node.get("background") and not node.get("foreground"):
            return None
        return {"kind": "adaptive", "background": node.get("background"),
                "foreground": node.get("foreground")}
    return {"kind": "legacy", "layer": node}


# ---------------------------------------------------------------- compose

def _compose_fresh() -> bool:
    try:
        return COMPOSE_BIN.is_file() and COMPOSE_BIN.stat().st_mtime >= COMPOSE_SRC.stat().st_mtime
    except OSError:
        return False


def ensure_compose() -> Path | None:
    """Return the compose binary, building it with build.sh when missing or stale.

    Two installs may get here at once. The build runs under an exclusive lock
    on build/.lock and build.sh links into a unique temporary name that is
    renamed over the binary, so nobody ever executes a half written file.
    """
    if _compose_fresh():
        return COMPOSE_BIN
    import fcntl

    COMPOSE_BIN.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(COMPOSE_BIN.parent / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + BUILD_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise IconError("timed out waiting for another compose build") from None
                time.sleep(0.2)
        if not _compose_fresh():  # another process may have built it while we waited
            r = subprocess.run(["/bin/bash", str(BUILD_SCRIPT)], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT)
            if r.returncode or not COMPOSE_BIN.is_file():
                sys.stderr.write(r.stderr)
                return None
    finally:
        os.close(fd)  # also releases the lock
    return COMPOSE_BIN


def render_spec(spec: dict, out_png: Path, size: int = 1024) -> bool:
    binary = ensure_compose()
    if not binary:
        return False
    # allow_nan=False: NaN or Infinity would make the whole spec invalid JSON.
    payload = json.dumps(dict(spec, size=size), allow_nan=False)
    if len(payload) > MAX_SPEC_BYTES:
        raise IconError("icon too complex")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        spec_file = Path(tmp) / "spec.json"
        spec_file.write_text(payload, encoding="ascii")
        rendered = Path(tmp) / "icon.png"
        r = subprocess.run([str(binary), str(spec_file), str(rendered)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=COMPOSE_TIMEOUT)
        if r.returncode:
            sys.stderr.write(r.stderr)
            return False
        data = rendered.read_bytes() if rendered.is_file() else b""
        if bitmap_dimensions(data) != ("png", size, size):
            return False
        # Write next to the destination and rename, so a reader never sees a
        # partial PNG and a failed render never clobbers an existing icon.
        fd, part = tempfile.mkstemp(prefix=f".{out_png.name}.", suffix=".part", dir=out_png.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(part, 0o644)
            os.replace(part, out_png)
        except BaseException:
            Path(part).unlink(missing_ok=True)
            raise
    return True


def extract_icon(apk_paths: list[Path], out_png: Path) -> bool:
    """Write a 1024x1024 macOS style PNG of the launcher icon; False on failure."""
    try:
        apks = [Path(p) for p in apk_paths if Path(p).is_file()]
        if not apks:
            return False
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_spec(apks, Path(tmp))
            if not spec:
                return False
            return render_spec(spec, Path(out_png))
    except Exception as exc:  # noqa: BLE001
        # The APKs are untrusted: whatever they trigger, the caller gets False,
        # never an exception (RecursionError and MemoryError included).
        sys.stderr.write(f"iconkit: {type(exc).__name__}: {exc}\n")
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract a macOS style 1024x1024 PNG icon from APKs.")
    ap.add_argument("out", type=Path, help="output PNG path")
    ap.add_argument("apks", type=Path, nargs="+", help="base APK followed by any split APKs")
    ap.add_argument("--spec", action="store_true", help="print the resolved spec instead of rendering")
    args = ap.parse_args(argv)
    if args.spec:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(build_spec(args.apks, Path(tmp)), indent=1)[:20000])
        return 0
    ok = extract_icon(args.apks, args.out)
    print(args.out if ok else "no icon", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
