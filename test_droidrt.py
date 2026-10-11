import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import droidrt

BADGING = """package: name='com.whatsapp' versionCode='264007233' versionName='2.26.40.72' platformBuildVersionName='17'
application-label:'WhatsApp'
application-icon-160:'res/mipmap-anydpi-v26/ic_launcher.xml'
launchable-activity: name='com.whatsapp.Main'  label='' icon=''
"""

WEBCAMS = """List of web cameras connected to the computer:
 Camera 'webcam0' is connected to device 'AAAA0000-0001' on channel 0 using pixel format 'YV12'
 Camera 'webcam1' is connected to device 'BBBB0000-0002' on channel 0 using pixel format 'YV12'
"""

CAMERAS = """Camera:

    iPhone Camera:

      Model ID: iPhone
      Unique ID: AAAA0000-0001

    FaceTime HD Camera:

      Model ID: FaceTime HD Camera
      Unique ID: BBBB0000-0002
"""


class ParseTests(unittest.TestCase):
    def test_parse_badging(self):
        info = droidrt.parse_badging(BADGING)
        self.assertEqual(info["package"], "com.whatsapp")
        self.assertEqual(info["version"], "2.26.40.72")
        self.assertEqual(info["label"], "WhatsApp")
        self.assertEqual(info["activity"], "com.whatsapp.Main")

    def test_parse_webcams_maps_names(self):
        self.assertEqual(
            droidrt.parse_webcams(WEBCAMS, CAMERAS),
            [("webcam0", "iPhone Camera"), ("webcam1", "FaceTime HD Camera")],
        )

    def test_parse_webcams_emulator_37_format(self):
        listing = (
            " Camera 'iPhone Camera' can be specified by label as 'webcam0' or by id as 'A' and will use pixel format 'NV12'\n"
            " Camera 'FaceTime HD Camera' can be specified by label as 'webcam1' or by id as 'B' and will use pixel format 'NV12'\n"
        )
        self.assertEqual(droidrt.parse_webcams(listing, ""), [("webcam0", "iPhone Camera"), ("webcam1", "FaceTime HD Camera")])

    def test_parse_packages_and_version(self):
        self.assertEqual(droidrt.parse_packages("package:b.app\npackage:a.app\npackage:b.app\n"), ["a.app", "b.app"])
        self.assertEqual(droidrt.parse_version("    versionCode=264007233 minSdk=23\n    versionName=2.26.40.72\n"), "2.26.40.72")

    def test_avd_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.ini"
            cfg.write_text("image.sysdir.1=system-images/android-37.0/google_apis_playstore/arm64-v8a/\n")
            self.assertEqual(droidrt.avd_image(cfg), "system-images;android-37.0;google_apis_playstore;arm64-v8a")
            self.assertIsNone(droidrt.avd_image(Path(tmp) / "missing.ini"))

    def test_safe_name(self):
        self.assertEqual(droidrt.safe_name('A/B:C"'), "A-B-C-")
        self.assertEqual(droidrt.safe_name("  "), "Android App")

    def test_info_plist(self):
        pl = droidrt.info_plist("WhatsApp", "com.whatsapp", "2.26.40.72")
        self.assertEqual(pl["CFBundleIdentifier"], "local.droidrt.com.whatsapp")
        self.assertIn("NSCameraUsageDescription", pl)
        self.assertEqual(pl["DroidrtPackage"], "com.whatsapp")
        self.assertEqual(pl["CFBundleExecutable"], "droidrt-launcher")
        self.assertEqual(droidrt.short_version("50.7.34-31 [0] [PR] 899074834"), "50.7.34-31")
        self.assertEqual(droidrt.short_version(""), "1")

    def test_apply_avd_settings_replaces_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.ini"
            cfg.write_text("hw.ramSize=1536\nabi.type=arm64-v8a\n")
            droidrt.apply_avd_settings(cfg, {"hw.ramSize": "2048"})
            self.assertEqual(cfg.read_text().splitlines(), ["abi.type=arm64-v8a", "hw.ramSize=2048"])


class WindowTests(unittest.TestCase):
    def test_flex_display_spec_is_in_points(self):
        self.assertEqual(droidrt.flex_display_spec("720x1560/320"), "360x780/160")
        self.assertEqual(droidrt.flex_display_spec("1081x1921/420"), "540x960/210")
        self.assertEqual(droidrt.flex_display_spec("720x1560"), "360x780")
        with self.assertRaises(droidrt.DroidError):
            droidrt.flex_display_spec("big")

    def test_window_command_fixed_by_default(self):
        with mock.patch.object(droidrt, "tool", return_value="scrcpy"), mock.patch.object(droidrt, "DISPLAY", "720x1560/320"):
            fixed = droidrt.window_command("org.example", None, resizable=False)
            flex = droidrt.window_command("org.example", "Example", resizable=True)
        self.assertIn("--new-display=720x1560/320", fixed)
        self.assertNotIn("--flex-display", fixed)
        self.assertIn("--flex-display", flex)
        self.assertIn("--new-display=360x780/160", flex)
        self.assertEqual(flex[flex.index("--window-title") + 1], "Example")


class LocationTests(unittest.TestCase):
    def test_geo_fix_sends_longitude_first(self):
        with mock.patch.object(droidrt, "start"), mock.patch.object(droidrt, "adb", return_value="OK\n") as adb:
            droidrt.set_location(48.8584, 2.2945)
        adb.assert_called_once_with("emu", "geo", "fix", "2.294500", "48.858400", timeout=10)

    def test_geo_rejects_out_of_range_and_console_errors(self):
        with self.assertRaises(droidrt.DroidError):
            droidrt.set_location(91, 0)
        with mock.patch.object(droidrt, "start"), mock.patch.object(droidrt, "adb", return_value="KO: bad\n"):
            with self.assertRaises(droidrt.DroidError):
                droidrt.set_location(0, 0)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_apk(self, name, files):
        path = self.dir / name
        with zipfile.ZipFile(path, "w") as z:
            for f, data in files.items():
                z.writestr(f, data)
        return path

    def test_xapk_uses_manifest_order_and_icon(self):
        base = self.make_apk("base.apk", {"classes.dex": b"x"})
        split = self.make_apk("config.arm64_v8a.apk", {"lib/arm64-v8a/a.so": b"x"})
        manifest = {"package_name": "p", "icon": "icon.png",
                    "split_apks": [{"file": "base.apk", "id": "base"}, {"file": "config.arm64_v8a.apk"}]}
        xapk = self.dir / "app.xapk"
        with zipfile.ZipFile(xapk, "w") as z:
            z.write(base, "base.apk")
            z.write(split, "config.arm64_v8a.apk")
            z.writestr("manifest.json", json.dumps(manifest))
            z.writestr("icon.png", b"png")
        work = self.dir / "work"
        work.mkdir()
        apks, man, icon = droidrt.collect_apks(xapk, work)
        self.assertEqual([a.name for a in apks], ["base.apk", "config.arm64_v8a.apk"])
        self.assertEqual(man["package_name"], "p")
        self.assertEqual(icon.name, "icon.png")
        self.assertEqual(droidrt.base_apk(list(reversed(apks))).name, "base.apk")

    def test_zip_slip_rejected(self):
        evil = self.dir / "evil.xapk"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../escape.apk", b"x")
        work = self.dir / "work"
        work.mkdir()
        with self.assertRaises(droidrt.DroidError):
            droidrt.collect_apks(evil, work)

    def make_xapk(self, manifest, files):
        xapk = self.dir / "pkg.xapk"
        with zipfile.ZipFile(xapk, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest))
            for name, data in files.items():
                z.writestr(name, data)
        work = self.dir / "work"
        work.mkdir(exist_ok=True)
        return xapk, work

    def test_manifest_paths_cannot_escape(self):
        for manifest in ({"split_apks": [{"file": "../../etc/hosts"}]},
                         {"split_apks": [{"file": "/etc/hosts"}]}):
            xapk, work = self.make_xapk(manifest, {"base.apk": b"x"})
            with self.assertRaises(droidrt.DroidError):
                droidrt.collect_apks(xapk, work)
        xapk, work = self.make_xapk({"icon": "../../etc/hosts"}, {"base.apk": b"x"})
        with self.assertRaises(droidrt.DroidError):
            droidrt.collect_apks(xapk, work)

    def test_malformed_manifest_is_rejected(self):
        xapk, work = self.make_xapk({"split_apks": "nope"}, {"base.apk": b"x"})
        with self.assertRaises(droidrt.DroidError):
            droidrt.collect_apks(xapk, work)

    def test_oversized_package_is_rejected(self):
        old = droidrt.MAX_PACKAGE_BYTES
        droidrt.MAX_PACKAGE_BYTES = 10
        try:
            xapk, work = self.make_xapk({}, {"base.apk": b"x" * 100})
            with self.assertRaisesRegex(droidrt.DroidError, "size limit"):
                droidrt.collect_apks(xapk, work)
        finally:
            droidrt.MAX_PACKAGE_BYTES = old

    def test_label_is_sanitized(self):
        self.assertEqual(droidrt.clean_label("Whats" + chr(0x202E) + "App" + chr(0)), "WhatsApp")
        self.assertEqual(len(droidrt.clean_label("x" * 500)), 80)
        self.assertEqual(droidrt.safe_name("../.."), "-")

    def test_launcher_label_collision_gets_package_suffix(self):
        old = droidrt.APPS_DIR
        droidrt.APPS_DIR = self.dir / "Apps"
        try:
            first = droidrt.APPS_DIR / "WhatsApp.app/Contents"
            first.mkdir(parents=True)
            import plistlib
            with open(first / "Info.plist", "wb") as f:
                plistlib.dump({"DroidrtPackage": "com.whatsapp"}, f)
            self.assertEqual(droidrt.launcher_path("WhatsApp", "com.whatsapp").name, "WhatsApp.app")
            self.assertEqual(droidrt.launcher_path("WhatsApp", "evil.pkg").name, "WhatsApp (evil.pkg).app")
        finally:
            droidrt.APPS_DIR = old

    def test_foreign_abi_splits_are_dropped(self):
        for name in ["config.armeabi_v7a.apk", "base-x86.apk", "split_config.x86_64.apk", "base-armeabi_v7a.apk"]:
            self.assertTrue(droidrt.is_foreign_abi_split(name), name)
        for name in ["config.arm64_v8a.apk", "base.apk", "config.xxhdpi.apk", "base-master.apk"]:
            self.assertFalse(droidrt.is_foreign_abi_split(name), name)

    def test_check_abi_rejects_32bit_only(self):
        apk = self.make_apk("old.apk", {"classes.dex": b"x", "lib/armeabi-v7a/libgame.so": b"x"})
        with self.assertRaisesRegex(droidrt.DroidError, "32-bit ARM"):
            droidrt.check_abi([apk])

    def test_check_abi_accepts_arm64_and_pure_java(self):
        droidrt.check_abi([self.make_apk("a.apk", {"lib/arm64-v8a/x.so": b"x", "lib/x86/x.so": b"x"})])
        droidrt.check_abi([self.make_apk("b.apk", {"classes.dex": b"x"})])

    def test_find_icon_png_picks_largest_raster(self):
        apk = self.make_apk("a.apk", {
            "res/mipmap-mdpi/ic_launcher.png": b"1" * 10,
            "res/mipmap-xxhdpi/ic_launcher.png": b"1" * 100,
            "res/mipmap-anydpi-v26/ic_launcher.xml": b"1" * 1000,
        })
        icon = droidrt.find_icon_png([apk], self.dir)
        self.assertEqual(icon.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
