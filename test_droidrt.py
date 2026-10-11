import json
import plistlib
import signal
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import droidrt

REAL_SLEEP = time.sleep

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


class StateDirTestCase(unittest.TestCase):
    """Points every state, log and launcher directory at a temporary one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patches = {
            "LOCK_DIR": self.dir / "state/locks",
            "LAST_CLOSE": self.dir / "state/last-window-close",
            "LOG_DIR": self.dir / "logs",
            "APPS_DIR": self.dir / "Apps",
        }
        for name, value in patches.items():
            p = mock.patch.object(droidrt, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)


class LockTests(StateDirTestCase):
    def test_exclusive_and_shared_locks(self):
        a = droidrt.acquire_lock("x.lock", shared=True)
        b = droidrt.acquire_lock("x.lock", shared=True)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertIsNone(droidrt.acquire_lock("x.lock"))
        droidrt.release_lock(a)
        droidrt.release_lock(b)
        c = droidrt.acquire_lock("x.lock")
        self.assertIsNotNone(c)
        self.assertIsNone(droidrt.acquire_lock("x.lock", shared=True))
        droidrt.release_lock(c)
        self.assertEqual((droidrt.LOCK_DIR / "x.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(droidrt.LOCK_DIR.stat().st_mode & 0o777, 0o700)

    def test_locked_times_out_with_droid_error(self):
        held = droidrt.acquire_lock("y.lock")
        try:
            with self.assertRaisesRegex(droidrt.DroidError, "another droidrt command"):
                with droidrt.locked("y.lock", timeout=0.3):
                    pass
        finally:
            droidrt.release_lock(held)


class StartRaceTests(StateDirTestCase):
    def test_concurrent_starts_spawn_one_emulator(self):
        spawned = []
        booted = threading.Event()

        def fake_popen(*args, **kwargs):
            spawned.append(args)
            # qemu shows up only after a moment, which is what made the race possible.
            threading.Timer(0.5, booted.set).start()
            proc = mock.Mock()
            proc.poll.return_value = None
            return proc

        with mock.patch.object(droidrt, "is_booted", side_effect=lambda: booted.is_set()), \
             mock.patch.object(droidrt, "vm_pids", return_value=[]), \
             mock.patch.object(droidrt, "setup"), \
             mock.patch.object(droidrt, "pick_webcam", return_value="emulated"), \
             mock.patch.object(droidrt, "tool", return_value="emulator"), \
             mock.patch.object(droidrt.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(droidrt.time, "sleep", lambda s: REAL_SLEEP(min(s, 0.05))):
            errors = []

            def worker():
                try:
                    droidrt.start()
                except Exception as e:  # pragma: no cover - reported below
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(spawned), 1)

    def test_emulator_exit_is_reported_without_waiting_for_the_timeout(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.returncode = 1
        with mock.patch.object(droidrt, "is_booted", return_value=False), \
             mock.patch.object(droidrt, "vm_pids", return_value=[]), \
             mock.patch.object(droidrt, "setup"), \
             mock.patch.object(droidrt, "pick_webcam", return_value="emulated"), \
             mock.patch.object(droidrt, "tool", return_value="emulator"), \
             mock.patch.object(droidrt.subprocess, "Popen", return_value=proc):
            with self.assertRaisesRegex(droidrt.DroidError, "exited with status 1"):
                droidrt.start()


class IdleStopTests(StateDirTestCase):
    def run_idle_stop(self, windows=()):
        with mock.patch.object(droidrt, "window_pids", return_value=list(windows)), \
             mock.patch.object(droidrt, "stop") as stop:
            droidrt.idle_stop(0)
        return stop

    def test_stops_an_idle_vm(self):
        self.run_idle_stop().assert_called_once()

    def test_keeps_the_vm_while_a_window_is_open(self):
        self.run_idle_stop(windows=[123]).assert_not_called()

    def test_keeps_the_vm_while_a_launch_is_in_flight(self):
        busy = droidrt.acquire_lock("busy.lock", shared=True)
        try:
            self.run_idle_stop().assert_not_called()
        finally:
            droidrt.release_lock(busy)

    def test_keeps_the_vm_while_it_boots(self):
        vm = droidrt.acquire_lock("vm.lock")
        try:
            self.run_idle_stop().assert_not_called()
        finally:
            droidrt.release_lock(vm)

    def test_defers_to_a_newer_countdown(self):
        def close_window_meanwhile(_):
            droidrt.LAST_CLOSE.parent.mkdir(parents=True, exist_ok=True)
            droidrt.LAST_CLOSE.touch()
            future = time.time() + 5
            droidrt.os.utime(droidrt.LAST_CLOSE, (future, future))

        with mock.patch.object(droidrt.time, "sleep", close_window_meanwhile):
            self.run_idle_stop().assert_not_called()


class StopTests(unittest.TestCase):
    def test_stop_survives_console_timeout_and_vanished_pid(self):
        pids = iter([[42]] + [[42]] * 20 + [[42]])
        with mock.patch.object(droidrt, "vm_pids", side_effect=lambda: next(pids)), \
             mock.patch.object(droidrt, "adb", side_effect=droidrt.DroidError("timed out")), \
             mock.patch.object(droidrt.time, "sleep"), \
             mock.patch.object(droidrt.os, "kill", side_effect=ProcessLookupError) as kill:
            droidrt.stop()
        kill.assert_called_once_with(42, signal.SIGTERM)


class RunTests(unittest.TestCase):
    def test_timeout_and_missing_binary_become_droid_errors(self):
        with self.assertRaisesRegex(droidrt.DroidError, "timed out"):
            droidrt.run("/bin/sleep", "5", timeout=0.2)
        with self.assertRaises(droidrt.DroidError):
            droidrt.run("/nonexistent/tool")

    def test_is_booted_is_false_when_adb_hangs(self):
        with mock.patch.object(droidrt, "adb", side_effect=droidrt.DroidError("timed out")):
            self.assertFalse(droidrt.is_booted())

    def test_pick_webcam_falls_back_when_probing_fails(self):
        with mock.patch.dict(droidrt.os.environ, {"DROIDRT_CAMERA": ""}), \
             mock.patch.object(droidrt, "run", side_effect=droidrt.DroidError("timed out")), \
             mock.patch.object(droidrt, "tool", return_value="emulator"):
            self.assertEqual(droidrt.pick_webcam(), "emulated")

    def test_package_names_are_validated(self):
        for good in ["com.whatsapp", "a", "org.example_app.v2", "com.android.vending"]:
            self.assertEqual(droidrt.check_package(good), good)
        for bad in ["", "../x", "a b", "x;reboot", "com..x", ".com", "a/b", "com.x$(id)", "+com.x", None]:
            with self.assertRaises(droidrt.DroidError, msg=repr(bad)):
                droidrt.check_package(bad)


class LaunchTests(StateDirTestCase):
    def launch(self, codes, durations=None, pm_path="package:/data/app/base.apk\n", start=None):
        codes = list(codes)
        calls = []

        def fake_run_window(package, title, state):
            calls.append(package)
            return codes.pop(0)

        with mock.patch.object(droidrt, "pgrep", return_value=[]), \
             mock.patch.object(droidrt, "start", side_effect=start), \
             mock.patch.object(droidrt, "adb", return_value=pm_path), \
             mock.patch.object(droidrt, "run_window", side_effect=fake_run_window), \
             mock.patch.object(droidrt, "spawn_idle_stop") as idle, \
             mock.patch.object(droidrt.time, "sleep"):
            try:
                result = droidrt.launch("org.example", "Example")
            except droidrt.DroidError as e:
                result = e
        return result, calls, idle

    def test_quick_failure_is_retried_until_the_window_opens(self):
        code, calls, idle = self.launch([1, 1, 0])
        self.assertEqual((code, len(calls)), (0, 3))
        idle.assert_called_once()

    def test_window_stopped_by_signal_is_not_reopened(self):
        code, calls, _ = self.launch([-15, 0])
        self.assertEqual((code, len(calls)), (-15, 1))

    def test_already_running_launch_is_not_duplicated(self):
        held = droidrt.acquire_lock("launch-org.example.lock")
        try:
            code, calls, idle = self.launch([0])
        finally:
            droidrt.release_lock(held)
        self.assertEqual((code, calls), (0, []))
        idle.assert_not_called()

    def test_failed_boot_still_schedules_the_idle_shutdown(self):
        result, calls, idle = self.launch([0], start=droidrt.DroidError("boot failed"))
        self.assertIsInstance(result, droidrt.DroidError)
        self.assertEqual(calls, [])
        idle.assert_called_once()
        # All locks are free again.
        for name in ("busy.lock", "launch-org.example.lock"):
            fd = droidrt.acquire_lock(name)
            self.assertIsNotNone(fd, name)
            droidrt.release_lock(fd)

    def test_sigterm_during_boot_aborts_and_restores_the_handler(self):
        before = signal.getsignal(signal.SIGTERM)

        def boot():
            droidrt.os.kill(droidrt.os.getpid(), signal.SIGTERM)
            REAL_SLEEP(2)  # the handler raises before this ends
            self.fail("SIGTERM did not interrupt the boot")

        result, calls, idle = self.launch([0], start=boot)
        self.assertIsInstance(result, droidrt.Interrupted)
        self.assertEqual(calls, [])
        idle.assert_called_once()
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_sigterm_reaches_the_window(self):
        with mock.patch.object(droidrt, "window_command",
                               return_value=["/bin/sh", "-c", "trap 'exit 0' TERM; sleep 30 & wait"]):
            state = {"stopping": False, "spawning": False, "abortable": True, "window": None}
            droidrt.LOG_DIR.mkdir(parents=True)
            threading.Timer(0.5, lambda: state["window"].terminate()).start()
            t0 = time.monotonic()
            code = droidrt.run_window("org.example", None, state)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn(code, (0, -signal.SIGTERM))  # the TERM may land before the trap is set
        self.assertIsNone(state["window"])

    def test_invalid_package_is_rejected(self):
        with self.assertRaises(droidrt.DroidError):
            droidrt.launch("x; reboot", None)


class LauncherBundleTests(StateDirTestCase):
    def make_bundle(self, name, plist):
        contents = droidrt.APPS_DIR / name / "Contents"
        contents.mkdir(parents=True)
        if plist is not None:
            (contents / "Info.plist").write_bytes(plist if isinstance(plist, bytes) else plistlib.dumps(plist))
        return droidrt.APPS_DIR / name

    def create(self, label, package, version="1.0", binary_error=None):
        binary = self.dir / "droidrt-launcher"
        binary.write_bytes(b"bin")
        build = mock.Mock(side_effect=binary_error, return_value=binary)
        with mock.patch.object(droidrt, "launcher_binary", build):
            return droidrt.create_launcher({"label": label, "package": package, "version": version}, None)

    def test_unrelated_app_with_the_same_name_is_never_replaced(self):
        foreign = self.make_bundle("Slack.app", {"CFBundleIdentifier": "com.tinyspeck.slackmacgap"})
        app = self.create("Slack", "com.Slack")
        self.assertEqual(app.name, "Slack (com.Slack).app")
        self.assertTrue((foreign / "Contents/Info.plist").exists())

    def test_rename_replaces_the_old_bundle(self):
        old = self.create("Old Name", "org.example")
        new = self.create("New Name", "org.example", "2.0")
        self.assertFalse(old.exists())
        self.assertEqual(droidrt.launchers()["org.example"][0], new)
        self.assertEqual(droidrt.launchers()["org.example"][1]["DroidrtVersion"], "2.0")
        self.assertEqual([p.name for p in droidrt.APPS_DIR.iterdir()], ["New Name.app"])

    def test_failed_build_keeps_the_previous_launcher(self):
        old = self.create("Example", "org.example")
        with self.assertRaises(droidrt.DroidError):
            self.create("Example", "org.example", "2.0", binary_error=droidrt.DroidError("swiftc failed"))
        self.assertTrue((old / "Contents/Info.plist").exists())
        self.assertEqual([p.name for p in droidrt.APPS_DIR.iterdir()], ["Example.app"])

    def test_malformed_plists_are_skipped(self):
        self.make_bundle("Broken.app", b"<?xml version='1.0'?><plist><dict><key>a</key>")
        self.make_bundle("List.app", plistlib.dumps(["not", "a", "dict"]))
        self.make_bundle("Number.app", {"DroidrtPackage": 7})
        self.make_bundle("Good.app", {"DroidrtPackage": "org.good"})
        self.assertEqual(list(droidrt.launchers()), ["org.good"])

    def test_long_labels_fit_in_a_file_name(self):
        name = droidrt.safe_name(chr(0x1F600) * 80)
        self.assertLessEqual(len(name.encode()), 120)
        self.assertLessEqual(len(f"{name} (com.example.app).app".encode()), 255)

    def test_bundle_id_has_no_underscores(self):
        self.assertEqual(droidrt.info_plist("A", "org.my_app", "1")["CFBundleIdentifier"], "local.droidrt.org.my-app")

    def test_manifest_package_name_is_validated(self):
        with mock.patch.object(droidrt, "aapt2", return_value=None):
            with self.assertRaises(droidrt.DroidError):
                droidrt.apk_info(Path("base.apk"), {"package_name": "../../../tmp/x"})

    def test_uninstall_keeps_the_launcher_when_uninstall_fails(self):
        app = self.make_bundle("Example.app", {"DroidrtPackage": "org.example"})
        with mock.patch.object(droidrt, "start"), \
             mock.patch.object(droidrt, "adb", side_effect=["Failure [DELETE_FAILED_INTERNAL_ERROR]\n",
                                                            "package:/data/app/base.apk\n"]):
            with self.assertRaisesRegex(droidrt.DroidError, "uninstall failed"):
                droidrt.uninstall("org.example")
        self.assertTrue(app.exists())
        with mock.patch.object(droidrt, "start"), mock.patch.object(droidrt, "adb", return_value="Success\n"):
            droidrt.uninstall("org.example")
        self.assertFalse(app.exists())

    def test_sync_keeps_launchers_when_the_listing_comes_back_short(self):
        app = self.make_bundle("Example.app", {"DroidrtPackage": "org.example", "DroidrtVersion": "1.0"})
        with mock.patch.object(droidrt, "start"), \
             mock.patch.object(droidrt, "launchable_user_packages", return_value=[]), \
             mock.patch.object(droidrt, "adb", return_value="package:/data/app/base.apk\n"):
            droidrt.sync()
        self.assertTrue(app.exists())
        with mock.patch.object(droidrt, "start"), \
             mock.patch.object(droidrt, "launchable_user_packages", return_value=[]), \
             mock.patch.object(droidrt, "adb", return_value=""):
            droidrt.sync()
        self.assertFalse(app.exists())

    def test_sync_compares_full_versions(self):
        self.assertTrue(droidrt.launcher_is_current({"DroidrtVersion": "5.0 [1]"}, "5.0 [1]"))
        self.assertFalse(droidrt.launcher_is_current({"DroidrtVersion": "5.0 [1]", "CFBundleShortVersionString": "5.0"},
                                                     "5.0 [2]"))
        # Bundles from older releases only carry the short version.
        self.assertTrue(droidrt.launcher_is_current({"CFBundleShortVersionString": "5.0"}, "5.0 [2]"))


class InstallTests(StateDirTestCase):
    def test_bad_files_fail_before_booting_the_vm(self):
        bogus = self.dir / "bogus.apk"
        bogus.write_bytes(b"not a zip")
        with mock.patch.object(droidrt, "start") as start:
            for src in (bogus, self.dir / "missing.apk"):
                with self.assertRaises(droidrt.DroidError):
                    droidrt.install(src)
        start.assert_not_called()

    def test_main_reports_errors_without_traceback(self):
        with mock.patch.object(droidrt, "list_apps", side_effect=PermissionError("denied")):
            self.assertEqual(droidrt.main(["list"]), 1)


if __name__ == "__main__":
    unittest.main()
