"""Unit tests for iconkit.

Most tests cover the pure parts (no aapt2, no APKs). The hardening tests use
an in-memory resource table and synthetic image headers; the few that run the
Swift compositor are skipped when build/compose has not been built.
"""
import json
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iconkit as ik  # noqa: E402


def approx(test, a, b, places=4):
    test.assertEqual(len(a), len(b), f"{a} != {b}")
    for x, y in zip(a, b):
        test.assertEqual(x[0], y[0], f"{a} != {b}")
        for u, v in zip(x[1:], y[1:]):
            test.assertAlmostEqual(u, v, places=places, msg=f"{a} != {b}")


class ParseColorTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ik.parse_color("#ff9ccc65"), (0x9C, 0xCC, 0x65, 0xFF))
        self.assertEqual(ik.parse_color("#9ccc65"), (0x9C, 0xCC, 0x65, 0xFF))
        self.assertEqual(ik.parse_color("#f00"), (255, 0, 0, 255))
        self.assertEqual(ik.parse_color("#8f00"), (255, 0, 0, 0x88))
        self.assertEqual(ik.parse_color("#00000000"), (0, 0, 0, 0))

    def test_invalid(self):
        for bad in ("", "red", "#12345", "#gg0000", "@0x7f060076"):
            self.assertIsNone(ik.parse_color(bad), bad)

    def test_with_alpha(self):
        self.assertEqual(ik.with_alpha((1, 2, 3, 200), 0.5), (1, 2, 3, 100))


class ResolverColorTest(unittest.TestCase):
    def setUp(self):
        self.r = ik.Resolver(None)

    def test_literal_and_framework(self):
        self.assertEqual(self.r.color("#ff112233"), (0x11, 0x22, 0x33, 0xFF))
        self.assertEqual(self.r.color("@0x0106000b"), (255, 255, 255, 255))
        self.assertEqual(self.r.color("@android:0x0106000c"), (0, 0, 0, 255))

    def test_unresolvable(self):
        self.assertIsNone(self.r.color("?0x7f040111"))
        self.assertIsNone(self.r.color("@0x7f060076"))

    def test_selector_xml(self):
        node = ik.XmlNode("selector", {}, [
            ik.XmlNode("item", {"state_pressed": "true", "color": "#ff0000ff"}, []),
            ik.XmlNode("item", {"color": "#ff00ff00", "alpha": "0.5"}, []),
        ])
        self.assertEqual(self.r.paint_xml(node, 0), {"color": [0, 255, 0, 128]})

    def test_gradient_xml(self):
        node = ik.XmlNode("gradient", {"type": "1", "centerX": "54", "centerY": "54", "gradientRadius": "20",
                                       "startColor": "#ffff0000", "endColor": "#ff0000ff"}, [])
        g = self.r.paint_xml(node, 0)["gradient"]
        self.assertEqual(g["type"], "radial")
        self.assertEqual(g["stops"], [[0.0, [255, 0, 0, 255]], [1.0, [0, 0, 255, 255]]])

    def test_fraction(self):
        self.assertAlmostEqual(self.r.fraction("18.000000dp", 108), 1 / 6)
        self.assertAlmostEqual(self.r.fraction("0.25%", 108), 0.25)
        self.assertEqual(self.r.fraction(None, 108), 0.0)


class DimensionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ik.parse_dimension("24.000000dp"), (24.0, "dp"))
        self.assertEqual(ik.parse_dimension("108"), (108.0, ""))
        self.assertEqual(ik.parse_dimension("0.65%"), (0.65, "%"))
        self.assertIsNone(ik.parse_dimension("@0x7f070012"))


class PathDataTest(unittest.TestCase):
    def test_absolute_lines(self):
        approx(self, ik.parse_path_data("M20,11L7.8,11 L20,13z"),
               [("M", 20, 11), ("L", 7.8, 11), ("L", 20, 13), ("Z",)])

    def test_relative_and_implicit_lineto(self):
        approx(self, ik.parse_path_data("m10 10 5 0 0 5z"),
               [("M", 10, 10), ("L", 15, 10), ("L", 15, 15), ("Z",)])

    def test_h_v(self):
        approx(self, ik.parse_path_data("M1,1H5V7h-2v-1"),
               [("M", 1, 1), ("L", 5, 1), ("L", 5, 7), ("L", 3, 7), ("L", 3, 6)])

    def test_packed_numbers(self):
        approx(self, ik.parse_path_data("M.5.5l-1-2.5.25e1-1E-1"),
               [("M", 0.5, 0.5), ("L", -0.5, -2.0), ("L", 2.0, -2.1)])

    def test_smooth_cubic_reflects(self):
        ops = ik.parse_path_data("M0,0C0,10 10,10 10,0S20,-10 20,0")
        approx(self, ops, [("M", 0, 0), ("C", 0, 10, 10, 10, 10, 0), ("C", 10, -10, 20, -10, 20, 0)])

    def test_smooth_cubic_without_previous(self):
        approx(self, ik.parse_path_data("M5,5s5,5 10,0"), [("M", 5, 5), ("C", 5, 5, 10, 10, 15, 5)])

    def test_quad_and_t(self):
        ops = ik.parse_path_data("M0,0Q5,10 10,0T20,0")
        approx(self, ops, [("M", 0, 0), ("Q", 5, 10, 10, 0), ("Q", 15, -10, 20, 0)])

    def test_relative_quad(self):
        approx(self, ik.parse_path_data("M1,1q1,2 3,0t2,0"),
               [("M", 1, 1), ("Q", 2, 3, 4, 1), ("Q", 6, -1, 6, 1)])

    def test_close_returns_to_subpath_start(self):
        approx(self, ik.parse_path_data("M10,10l5,0z l1,1"),
               [("M", 10, 10), ("L", 15, 10), ("Z",), ("L", 11, 11)])

    def test_arc_semicircle(self):
        ops = ik.parse_path_data("M0,0A10,10 0 0,1 20,0")
        self.assertEqual(ops[0], ("M", 0, 0))
        self.assertTrue(all(o[0] == "C" for o in ops[1:]))
        self.assertEqual(len(ops) - 1, 2)
        self.assertAlmostEqual(ops[-1][5], 20)
        self.assertAlmostEqual(ops[-1][6], 0)
        # sweep=1 in a y-down space bends through negative y: the midpoint is (10,-10).
        self.assertAlmostEqual(ops[1][5], 10, places=6)
        self.assertAlmostEqual(ops[1][6], -10, places=6)

    def test_arc_packed_flags_and_full_circle(self):
        ops = ik.parse_path_data("M54,34a20,20 0 1,1 0,40a20 20 0 1 1 0-40z")
        ends = [(o[5], o[6]) for o in ops if o[0] == "C"]
        self.assertAlmostEqual(ends[-1][0], 54)
        self.assertAlmostEqual(ends[-1][1], 34)
        packed = ik.parse_path_data("M0,0a10,10 0 01 20,0")
        approx(self, packed, ik.parse_path_data("M0,0a10,10 0 0 1 20,0"))

    def test_degenerate_arc(self):
        approx(self, ik.parse_path_data("M0,0A0,5 0 0 1 10,0"), [("M", 0, 0), ("L", 10, 0)])
        self.assertEqual(ik.parse_path_data("M3,3A5,5 0 0 1 3,3"), [("M", 3, 3)])

    def test_arc_radius_scaled_up(self):
        ops = ik.parse_path_data("M0,0A1,1 0 0 1 10,0")
        self.assertAlmostEqual(ops[-1][5], 10)

    def test_errors(self):
        for bad in ("10,10", "M10", "M0,0 X1,1", "M0,0A1,1 0 2 1 3,3"):
            with self.assertRaises(ValueError, msg=bad):
                ik.parse_path_data(bad)

    def test_empty(self):
        self.assertEqual(ik.parse_path_data(""), [])


class ConfigScoreTest(unittest.TestCase):
    def test_ordering(self):
        configs = ["mdpi", "xxxhdpi", "anydpi-v26", "night-xxxhdpi", "hdpi", ""]
        self.assertEqual(max(configs, key=ik.config_score), "anydpi-v26")
        self.assertGreater(ik.config_score("xxxhdpi"), ik.config_score("xxhdpi"))
        self.assertGreater(ik.config_score("xxhdpi"), ik.config_score("night-xxxhdpi"))


class AaptParsingTest(unittest.TestCase):
    def test_xmltree(self):
        text = (
            "N: android=http://schemas.android.com/apk/res/android (line=2)\n"
            "  E: adaptive-icon (line=2)\n"
            "      E: background (line=3)\n"
            "        A: http://schemas.android.com/apk/res/android:drawable(0x01010199)=@0x7f060076\n"
            "      E: foreground (line=4)\n"
            "        A: http://schemas.android.com/apk/res/android:drawable(0x01010199)=@0x7f0f0001\n"
            "          E: path (line=5)\n"
            '            A: http://schemas.android.com/apk/res/android:pathData(0x01010405)="M1,2z" (Raw: "M1,2z")\n'
        )
        root = ik.parse_xmltree(text)
        self.assertEqual(root.name, "adaptive-icon")
        bg, fg = root.children
        self.assertEqual(bg.attrs, {"drawable": "@0x7f060076"})
        self.assertEqual(fg.attrs["drawable"], "@0x7f0f0001")
        self.assertEqual(fg.children[0].attrs["pathData"], "M1,2z")

    def test_resources_dump(self):
        text = (
            "Package name=x id=7f\n"
            "  type color id=06 entryCount=1\n"
            "    resource 0x7f060076 color/ic_launcher_background\n"
            "      () #ff9ccc65\n"
            "    resource 0x7f0f0001 mipmap/ic_launcher_foreground\n"
            "      (mdpi) (file) res/a.png type=PNG\n"
            "      (xxxhdpi) (file) res/b.png type=PNG\n"
        )
        table, paths = {}, {}
        ik.parse_resources_dump(text, Path("base.apk"), table, paths)
        self.assertEqual(table[0x7F060076][0].value, "#ff9ccc65")
        self.assertEqual([e.config for e in table[0x7F0F0001]], ["mdpi", "xxxhdpi"])
        self.assertEqual(paths["res/b.png"], 0x7F0F0001)

    def test_resources_dump_names(self):
        # aapt2 prints references inside the app package by name, not by id.
        text = (
            "Package name=x id=7f\n"
            "  type color id=06 entryCount=2\n"
            "    resource 0x7f060010 color/ic_launcher_background\n"
            "      () @color/brand\n"
            "    resource 0x7f060011 color/brand\n"
            "      () #ff123456\n"
        )
        table, paths, names = {}, {}, {}
        ik.parse_resources_dump(text, Path("base.apk"), table, paths, names)
        self.assertEqual(names, {"color/ic_launcher_background": 0x7F060010, "color/brand": 0x7F060011})
        self.assertEqual(table[0x7F060010][0].value, "@color/brand")

    def test_ref_name(self):
        self.assertEqual(ik._ref_name("@color/brand"), "color/brand")
        self.assertEqual(ik._ref_name("@x.y:drawable/bg_icon"), "drawable/bg_icon")
        self.assertIsNone(ik._ref_name("@android:color/white"))
        self.assertIsNone(ik._ref_name("@0x7f060010"))
        self.assertIsNone(ik._ref_name("?attr/colorPrimary"))


# ---------------------------------------------------------------- hardening

def png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def make_png(width: int, height: int, pixels: bytes | None = None) -> bytes:
    """A PNG whose IHDR claims width x height. Without pixels the IDAT is a tiny stub."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(pixels if pixels is not None else b"\x00" * 16)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", idat)
            + png_chunk(b"IEND", b""))


def solid_png(size: int, rgba: tuple) -> bytes:
    row = b"\x00" + bytes(rgba) * size
    return make_png(size, size, row * size)


def riff_webp(chunk: bytes, body: bytes) -> bytes:
    payload = b"WEBP" + chunk + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def webp_vp8x(width: int, height: int) -> bytes:
    return riff_webp(b"VP8X", b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little")
                     + (height - 1).to_bytes(3, "little"))


def webp_vp8l(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return riff_webp(b"VP8L", b"\x2f" + struct.pack("<I", bits) + b"\x00" * 4)


def webp_vp8(width: int, height: int) -> bytes:
    return riff_webp(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 4)


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


class FakeResources(ik.Resources):
    """A Resources view built from Python data instead of aapt2 output."""

    def __init__(self, workdir: Path, table: dict, xml: dict, apk: Path = Path("base.apk")):
        super().__init__("aapt2", [], workdir)
        self.apk = apk
        for rid, value in table.items():
            self.table[rid] = [ik.Entry("", value, apk)]
        self.xml_nodes = xml
        self.xml_calls = 0

    def _entry_available(self, e) -> bool:
        return True

    def xml(self, apk, name):
        self.xml_calls += 1
        return self.xml_nodes.get(name)


def layer_list(items: list) -> ik.XmlNode:
    return ik.XmlNode("layer-list", {}, [ik.XmlNode("item", attrs, []) for attrs in items])


LOOP = 0x7F080001


class BitmapHeaderTest(unittest.TestCase):
    def test_recognized_formats(self):
        self.assertEqual(ik.bitmap_dimensions(make_png(192, 96)), ("png", 192, 96))
        self.assertEqual(ik.bitmap_dimensions(webp_vp8x(432, 300)), ("webp", 432, 300))
        self.assertEqual(ik.bitmap_dimensions(webp_vp8l(192, 192)), ("webp", 192, 192))
        self.assertEqual(ik.bitmap_dimensions(webp_vp8(144, 72)), ("webp", 144, 72))
        self.assertEqual(ik.bitmap_dimensions(jpeg(640, 480)), ("jpeg", 640, 480))
        self.assertEqual(ik.bitmap_dimensions(b"GIF89a" + struct.pack("<HH", 48, 32)), ("gif", 48, 32))

    def test_huge_headers_rejected(self):
        for data in (make_png(60000, 60000), make_png(4097, 16), webp_vp8x(60000, 60000),
                     webp_vp8l(16384, 16384), webp_vp8(16000, 16000), jpeg(60000, 60000),
                     jpeg(16, 4097), make_png(0, 10)):
            with self.assertRaises(ik.UnsafeBitmap):
                ik.check_bitmap(data)

    def test_unknown_or_truncated_rejected(self):
        for data in (b"", b"BM" + b"\x00" * 40, b"\x89PNG\r\n\x1a\n", b"RIFF\x00\x00\x00\x00WEBPXXXX",
                     b"\xff\xd8\xff\xda\x00\x02", b"<svg/>"):
            with self.assertRaises(ik.UnsafeBitmap):
                ik.check_bitmap(data)

    def test_limit_is_inclusive(self):
        self.assertEqual(ik.check_bitmap(make_png(4096, 4096)), ("png", 4096, 4096))

    def test_extract_validates_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "app.apk"
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("res/huge.png", make_png(60000, 60000))
                z.writestr("res/huge.webp", webp_vp8x(60000, 60000))
                z.writestr("res/fake.png", b"not an image at all")
                z.writestr("res/ok.png", solid_png(4, (255, 0, 0, 255)))
            work = Path(tmp) / "work"
            work.mkdir()
            with ik.Resources("aapt2", [], work) as res:
                for name in ("res/huge.png", "res/huge.webp", "res/fake.png"):
                    with self.assertRaises(ik.UnsafeBitmap, msg=name):
                        res.extract(apk, name)
                self.assertEqual(list(work.iterdir()), [])
                out = res.extract(apk, "res/ok.png")
                self.assertTrue(out.is_file())
                self.assertEqual(len(res._zips), 1)  # the APK was opened once and reused

    def test_resolver_skips_unsafe_bitmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "app.apk"
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("res/mipmap-xxxhdpi/ic.png", make_png(60000, 60000))
            res = FakeResources(Path(tmp), {0x7F0D0001: "(file) res/mipmap-xxxhdpi/ic.png"}, {}, apk)
            with res:
                self.assertIsNone(ik.Resolver(res).drawable("@0x7f0d0001", 108))


class ResolverBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, res, rid=LOOP):
        # CPU time rather than wall time, so a busy test machine does not cause flakes.
        start = time.process_time()
        try:
            return ik.spec_from_resources(res, res.apk, res.table[rid][0].value.split()[-1])
        finally:
            self.assertLess(time.process_time() - start, 2.0)

    def test_self_referencing_layer_list(self):
        node = layer_list([{"drawable": f"@0x{LOOP:08x}"}] * 50)
        res = FakeResources(self.work, {LOOP: "(file) res/drawable/loop.xml"}, {"res/drawable/loop.xml": node})
        self.assertIsNone(self.resolve(res))
        self.assertLessEqual(res.xml_calls, 2)

    def test_self_referencing_layer_list_with_insets(self):
        # Distinct insets give every level new bounds, so only the budget stops it.
        items = [{"drawable": "#ff00ff00"}]
        items += [{"drawable": f"@0x{LOOP:08x}", "left": f"{i / 100:.2f}dp"} for i in range(1, 50)]
        res = FakeResources(self.work, {LOOP: "(file) res/drawable/loop.xml"},
                            {"res/drawable/loop.xml": layer_list(items)})
        with self.assertRaisesRegex(ik.IconError, "too complex"):
            self.resolve(res)

    def test_exponential_reference_chain(self):
        # 16 layer-lists, each with 50 items pointing at the next: 50**16 nodes when expanded.
        table, xml = {}, {}
        for level in range(16):
            table[LOOP + level] = f"(file) res/drawable/l{level}.xml"
            xml[f"res/drawable/l{level}.xml"] = layer_list([{"drawable": f"@0x{LOOP + level + 1:08x}"}] * 50)
        table[LOOP + 16] = "#ff00ff00"
        res = FakeResources(self.work, table, xml)
        with self.assertRaisesRegex(ik.IconError, "too complex"):
            self.resolve(res)

    def test_total_path_commands_capped(self):
        path = ik.XmlNode("path", {"pathData": "M0,0" + "L1,1" * 999, "fillColor": "#ff000000"}, [])
        vec = ik.XmlNode("vector", {"viewportWidth": "24", "viewportHeight": "24"}, [path] * 300)
        res = FakeResources(self.work, {LOOP: "(file) res/drawable/v.xml"}, {"res/drawable/v.xml": vec})
        with self.assertRaisesRegex(ik.IconError, "too complex"):
            self.resolve(res)

    def test_repeated_references_are_memoized(self):
        shared = 0x7F080100
        node = layer_list([{"drawable": f"@0x{shared:08x}"}] * 10)
        circle = ik.XmlNode("shape", {"shape": "oval"}, [ik.XmlNode("solid", {"color": "#ff112233"}, [])])
        res = FakeResources(self.work, {LOOP: "(file) res/drawable/top.xml", shared: "(file) res/drawable/c.xml"},
                            {"res/drawable/top.xml": node, "res/drawable/c.xml": circle})
        spec = self.resolve(res)
        self.assertEqual(len(spec["layer"]["items"]), 10)
        self.assertEqual(res.xml_calls, 2)

    def test_extract_icon_returns_false_when_too_complex(self):
        table, xml = {}, {}
        for level in range(16):
            table[LOOP + level] = f"(file) res/drawable/l{level}.xml"
            xml[f"res/drawable/l{level}.xml"] = layer_list([{"drawable": f"@0x{LOOP + level + 1:08x}"}] * 50)
        table[LOOP + 16] = "#ff00ff00"
        apk = self.work / "app.apk"
        apk.write_bytes(b"")
        res = FakeResources(self.work, table, xml, apk)
        res.paths["res/drawable/l0.xml"] = LOOP
        start = time.process_time()
        with mock.patch.object(ik, "find_aapt2", return_value="aapt2"), \
                mock.patch.object(ik, "_badging_icon", return_value=(apk, "res/drawable/l0.xml")), \
                mock.patch.object(ik, "Resources", return_value=res), \
                mock.patch.object(ik, "render_spec", side_effect=AssertionError("must not render")), \
                mock.patch("sys.stderr"):
            self.assertFalse(ik.extract_icon([apk], self.work / "out.png"))
        self.assertLess(time.process_time() - start, 2.0)

    def test_normal_adaptive_icon(self):
        bg, fg = 0x7F060001, 0x7F080002
        adaptive = ik.XmlNode("adaptive-icon", {}, [
            ik.XmlNode("background", {"drawable": f"@0x{bg:08x}"}, []),
            ik.XmlNode("foreground", {"drawable": f"@0x{fg:08x}"}, []),
        ])
        vec = ik.XmlNode("vector", {"viewportWidth": "108", "viewportHeight": "108",
                                    "width": "108dp", "height": "108dp"}, [
            ik.XmlNode("path", {"pathData": "M54,34a20,20 0 1,1 0,40a20,20 0 1,1 0,-40z",
                                "fillColor": "#ffffffff"}, [])])
        res = FakeResources(self.work, {LOOP: "(file) res/mipmap-anydpi-v26/ic.xml", bg: "#ff25d366",
                                        fg: "(file) res/drawable/fg.xml"},
                            {"res/mipmap-anydpi-v26/ic.xml": adaptive, "res/drawable/fg.xml": vec})
        spec = self.resolve(res)
        self.assertEqual(spec["kind"], "adaptive")
        self.assertEqual(spec["background"], {"type": "color", "color": [0x25, 0xD3, 0x66, 255]})
        self.assertEqual(spec["foreground"]["type"], "vector")
        self.assertEqual(spec["foreground"]["children"][0]["kind"], "path")

    def test_adaptive_background_via_named_reference(self):
        # The background points at a color entry whose value is a symbolic
        # reference to another color entry (and a shape that does the same).
        bg, brand, fg, shape = 0x7F060010, 0x7F060011, 0x7F080002, 0x7F080003
        adaptive = ik.XmlNode("adaptive-icon", {}, [
            ik.XmlNode("background", {"drawable": f"@0x{bg:08x}"}, []),
            ik.XmlNode("foreground", {"drawable": f"@0x{shape:08x}"}, []),
        ])
        oval = ik.XmlNode("shape", {"shape": "oval"}, [ik.XmlNode("solid", {"color": "@color/brand"}, [])])
        res = FakeResources(self.work, {LOOP: "(file) res/mipmap-anydpi-v26/ic.xml", bg: "@color/brand",
                                        brand: "#ff123456", fg: "@color/missing",
                                        shape: "(file) res/drawable/s.xml"},
                            {"res/mipmap-anydpi-v26/ic.xml": adaptive, "res/drawable/s.xml": oval})
        res.names.update({"color/ic_launcher_background": bg, "color/brand": brand,
                          "drawable/s": shape})
        spec = self.resolve(res)
        self.assertEqual(spec["background"], {"type": "color", "color": [0x12, 0x34, 0x56, 255]})
        self.assertEqual(spec["foreground"]["fill"], {"color": [0x12, 0x34, 0x56, 255]})
        resolver = ik.Resolver(res)
        self.assertEqual(resolver.drawable("@color/brand", 108), {"type": "color", "color": [0x12, 0x34, 0x56, 255]})
        self.assertIsNone(resolver.drawable("@color/missing", 108))
        self.assertIsNone(resolver.drawable(f"@0x{fg:08x}", 108))

    def test_named_reference_cycle(self):
        a, b = 0x7F060020, 0x7F060021
        res = FakeResources(self.work, {a: "@color/b", b: "@color/a"}, {})
        res.names.update({"color/a": a, "color/b": b})
        self.assertIsNone(ik.Resolver(res).drawable("@color/a", 108))
        self.assertIsNone(ik.Resolver(res).color("@color/a"))


@unittest.skipUnless(ik.COMPOSE_BIN.is_file(), "build/compose not built (run build.sh)")
class ComposeLimitsTest(unittest.TestCase):
    def run_compose(self, spec: dict, tmp: Path) -> tuple[subprocess.CompletedProcess, Path]:
        spec_file, out = tmp / "spec.json", tmp / "out.png"
        spec_file.write_text(json.dumps(dict(spec, size=256)))
        r = subprocess.run([str(ik.COMPOSE_BIN), str(spec_file), str(out)], capture_output=True, text=True,
                           timeout=20)
        return r, out

    def test_huge_bitmap_header_skipped_without_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "huge.png").write_bytes(make_png(60000, 60000))
            start = time.monotonic()
            r, out = self.run_compose({"kind": "legacy", "layer": {"type": "bitmap", "path": str(tmp / "huge.png")}},
                                      tmp)
            self.assertLess(time.monotonic() - start, 10.0)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("skipping bitmap", r.stderr)

    def test_normal_bitmap_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "ok.png").write_bytes(solid_png(48, (37, 211, 102, 255)))
            r, out = self.run_compose({"kind": "legacy", "layer": {"type": "bitmap", "path": str(tmp / "ok.png")}},
                                      tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("skipping", r.stderr)
            self.assertEqual(ik.bitmap_dimensions(out.read_bytes()), ("png", 256, 256))

    def test_oversized_spec_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            spec_file = tmp / "spec.json"
            with open(spec_file, "wb") as f:
                f.truncate(17 * 1024 * 1024)
            r = subprocess.run([str(ik.COMPOSE_BIN), str(spec_file), str(tmp / "out.png")], capture_output=True,
                               text=True, timeout=20)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("too large", r.stderr)

    def test_node_budget_bounds_render_time(self):
        leaf = {"type": "color", "color": [255, 0, 0, 255]}
        node = leaf
        for _ in range(5):  # 8**5 = 32768 leaves, above the compositor node budget
            node = {"type": "layers", "items": [{"inset": [0, 0, 0, 0], "drawable": node}] * 8}
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            r, _ = self.run_compose({"kind": "legacy", "layer": node}, Path(tmp))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(time.monotonic() - start, 10.0)


if __name__ == "__main__":
    unittest.main()
