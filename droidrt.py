#!/opt/homebrew/bin/python3
"""droidrt: run Android apps on macOS as individual windows.

Each app runs on a headless ARM64 Android micro-VM (Hypervisor.framework, no
instruction translation) and is shown in its own macOS window through a scrcpy
virtual display. Installing an APK/XAPK/APKS also creates a launcher .app.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HOME = Path.home()
SDK = Path(os.environ.get("DROIDRT_SDK", "/opt/homebrew/share/android-commandlinetools"))
AVD_HOME = Path(os.environ.get("ANDROID_AVD_HOME", HOME / ".android/avd"))
AVD = os.environ.get("DROIDRT_AVD", "droidrt")
PORT = int(os.environ.get("DROIDRT_PORT", "5580"))
SERIAL = f"emulator-{PORT}"
IMAGE = os.environ.get("DROIDRT_IMAGE", "system-images;android-37.0;google_apis_playstore;arm64-v8a")
AUDIO = os.environ.get("DROIDRT_AUDIO", "1") != "0"
# The host microphone stays off unless asked for: an open input keeps macOS's mic indicator lit.
MIC = os.environ.get("DROIDRT_MIC", "0") == "1"
GPU = os.environ.get("DROIDRT_GPU", "host")
VIDEO_BITRATE = os.environ.get("DROIDRT_BITRATE", "6M")
HOST_ABI = "arm64-v8a"
# Packages come from the internet: cap what an archive may expand to.
MAX_PACKAGE_BYTES = 4 << 30
MAX_ENTRY_BYTES = 2 << 30
MAX_ICON_BYTES = 32 << 20
MAX_ICON_PIXELS = 4096
FOREIGN_ABIS = ("armeabi_v7a", "armeabi-v7a", "armeabi", "x86_64", "x86", "mips64", "mips")
STORE_PACKAGE = "com.android.vending"
APPS_DIR = Path(os.environ.get("DROIDRT_APPS_DIR", HOME / "Applications/Android"))
STATE_DIR = HOME / "Library/Application Support/droidrt"
LOG_DIR = HOME / "Library/Logs/droidrt"
DISPLAY = os.environ.get("DROIDRT_DISPLAY", "720x1560/320")
# Opt-in resizable windows (scrcpy --flex-display). Off by default: see flex_display_spec().
RESIZABLE = os.environ.get("DROIDRT_RESIZABLE", "0") == "1"
IDLE_GRACE = int(os.environ.get("DROIDRT_IDLE_GRACE", "120"))
MAX_FPS = os.environ.get("DROIDRT_MAX_FPS", "30")
BOOT_TIMEOUT = 240

# Finder-launched bundles get a minimal PATH, so tools are resolved explicitly.
TOOL_PATH = os.pathsep.join(
    [str(SDK / "platform-tools"), str(SDK / "emulator"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
)
os.environ["PATH"] = TOOL_PATH + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("ANDROID_SDK_ROOT", str(SDK))
os.environ.setdefault("ANDROID_AVD_HOME", str(AVD_HOME))

AVD_SETTINGS = {
    "hw.ramSize": "3072",
    "hw.audioInput": "yes" if AUDIO else "no",
    "hw.audioOutput": "yes" if AUDIO else "no",
    "hw.camera.back": "webcam0",
    "hw.camera.front": "none",
    "hw.keyboard": "yes",
    "hw.gpu.enabled": "yes",
    "showDeviceFrame": "no",
    "disk.dataPartition.size": "8G",
    "PlayStore.enabled": "yes" if "playstore" in IMAGE else "no",
}


class DroidError(Exception):
    pass


def tool(name: str) -> str:
    path = shutil.which(name, path=TOOL_PATH)
    if not path:
        raise DroidError(f"'{name}' not found (searched PATH: {TOOL_PATH})")
    return path


def run(*args: str, check: bool = True, timeout: float | None = None) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise DroidError(f"{' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def adb(*args: str, check: bool = True, timeout: float | None = None) -> str:
    return run(tool("adb"), "-s", SERIAL, *args, check=check, timeout=timeout)


def pgrep(pattern: str) -> list[int]:
    out = run("/usr/bin/pgrep", "-f", pattern, check=False)
    return [int(p) for p in out.split()]


def log(msg: str) -> None:
    print(f"droidrt: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- VM lifecycle

def apply_avd_settings(config: Path, settings: dict[str, str]) -> None:
    lines = config.read_text().splitlines() if config.exists() else []
    kept = [l for l in lines if l.split("=", 1)[0].strip() not in settings]
    kept += [f"{k}={v}" for k, v in settings.items()]
    config.write_text("\n".join(kept) + "\n")


def avd_image(config: Path) -> str | None:
    if not config.exists():
        return None
    m = re.search(r"^image\.sysdir\.1\s*=\s*(\S+)", config.read_text(), re.M)
    return m.group(1).strip("/").replace("/", ";") if m else None


def setup() -> None:
    avd_dir = AVD_HOME / f"{AVD}.avd"
    current = avd_image(avd_dir / "config.ini")
    if current and current != IMAGE:
        if vm_pids():
            raise DroidError("the VM is running; run 'droidrt stop' before changing the image")
        log(f"image changed ({current} to {IMAGE}): recreating the AVD, installed apps will be lost")
        shutil.rmtree(avd_dir)
        (AVD_HOME / f"{AVD}.ini").unlink(missing_ok=True)
    if not avd_dir.exists():
        log(f"creating AVD '{AVD}' ({IMAGE})")
        image_dir = SDK / Path(*IMAGE.split(";"))
        if not image_dir.exists():
            run(tool("sdkmanager"), f"--sdk_root={SDK}", IMAGE)
        # avdmanager prints a devices.xml warning on Homebrew SDKs but still creates the AVD.
        subprocess.run(
            [tool("avdmanager"), "create", "avd", "-n", AVD, "-k", IMAGE, "-d", "pixel_6", "--force"],
            input="no\n", capture_output=True, text=True,
        )
        if not avd_dir.exists():
            raise DroidError("avdmanager did not create the AVD")
    apply_avd_settings(avd_dir / "config.ini", AVD_SETTINGS)


def is_booted() -> bool:
    try:
        return adb("shell", "getprop", "sys.boot_completed", check=False, timeout=5).strip() == "1"
    except subprocess.TimeoutExpired:
        return False


def vm_pids() -> list[int]:
    return pgrep(f"qemu-system-aarch64.*-avd {AVD}( |$)")


def parse_webcams(webcam_list: str, cameras: str) -> list[tuple[str, str]]:
    """Map emulator webcam ids to macOS camera names: [(webcamN, name)]."""
    names = dict(
        (uid, name) for name, uid in re.findall(r"^\s{4}(\S.*?):\s*\n(?:\s{6}.*\n)*?\s{6}Unique ID: (\S+)", cameras, re.M)
    )
    # Emulator 37+ prints the camera name directly.
    labeled = re.findall(r"Camera '([^']+)' can be specified by label as '(webcam\d+)'", webcam_list)
    if labeled:
        return [(wid, name) for name, wid in labeled]
    return [(wid, names.get(uid, uid)) for wid, uid in re.findall(r"Camera '(webcam\d+)' is connected to device '([^']+)'", webcam_list)]


def pick_webcam() -> str:
    """DROIDRT_CAMERA (webcamN or part of a name) or the built-in camera, else webcam0."""
    wanted = os.environ.get("DROIDRT_CAMERA", "")
    if re.fullmatch(r"webcam\d+|none|emulated", wanted):
        return wanted
    cams = parse_webcams(
        run(tool("emulator"), "-webcam-list", check=False, timeout=60),
        run("/usr/sbin/system_profiler", "SPCameraDataType", check=False, timeout=60),
    )
    for pattern in [wanted] if wanted else ["FaceTime", "Built-in", "Integrada"]:
        for wid, name in cams:
            if pattern.lower() in name.lower():
                return wid
    return cams[0][0] if cams else "emulated"


def start() -> None:
    if is_booted():
        return
    setup()
    if not vm_pids():
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        camera = pick_webcam()
        log(f"starting headless VM (back camera: {camera})")
        with open(LOG_DIR / "vm.log", "ab") as out:
            subprocess.Popen(
                [tool("emulator"), "-avd", AVD, "-port", str(PORT), "-no-window",
                 *([] if AUDIO else ["-no-audio"]), *(["-allow-host-audio"] if AUDIO and MIC else []),
                 "-no-boot-anim", "-no-snapshot-save", "-gpu", GPU,
                 "-camera-back", camera, "-camera-front", "none"],
                stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
            )
    t0 = time.monotonic()
    run(tool("adb"), "-s", SERIAL, "wait-for-device", timeout=BOOT_TIMEOUT)
    while not is_booted():
        if time.monotonic() - t0 > BOOT_TIMEOUT:
            raise DroidError(f"VM did not finish booting in {BOOT_TIMEOUT}s (see ~/Library/Logs/droidrt/vm.log)")
        time.sleep(1)
    log(f"VM ready in {time.monotonic() - t0:.0f}s")


def stop() -> None:
    if not vm_pids():
        return
    adb("emu", "kill", check=False, timeout=10)
    for _ in range(20):
        if not vm_pids():
            return
        time.sleep(0.5)
    for pid in vm_pids():
        os.kill(pid, 15)


def window_pids() -> list[int]:
    return pgrep(f"scrcpy.*-s {SERIAL}")


def idle_stop(grace: int) -> None:
    """Stop the VM once no app window has been open for `grace` seconds."""
    time.sleep(grace)
    if not window_pids():
        log("no open windows, stopping the VM")
        stop()


# ---------------------------------------------------------------- packages

def collect_apks(src: Path, workdir: Path) -> tuple[list[Path], dict, Path | None]:
    """Return (apk files, xapk manifest or {}, bundled icon) for .apk/.xapk/.apks/.apkm."""
    if src.suffix.lower() == ".apk":
        return [src], {}, None
    if not zipfile.is_zipfile(src):
        hint = " (legacy .apkm files are encrypted; download the current format or convert it with unapkm)" if src.suffix.lower() == ".apkm" else ""
        raise DroidError(f"{src} is neither an APK nor a zip package{hint}")
    with zipfile.ZipFile(src) as z:
        infos = z.infolist()
        if sum(i.file_size for i in infos) > MAX_PACKAGE_BYTES or any(i.file_size > MAX_ENTRY_BYTES for i in infos):
            raise DroidError("package expands beyond the size limit")
        for name in z.namelist():
            inside(workdir, name)
        z.extractall(workdir)
    manifest = {}
    if (workdir / "manifest.json").exists():
        try:
            manifest = json.loads((workdir / "manifest.json").read_text())
        except (ValueError, RecursionError) as e:
            raise DroidError(f"invalid manifest.json: {e}") from None
        if not isinstance(manifest, dict):
            raise DroidError("invalid manifest.json")
    splits = manifest.get("split_apks")
    if splits:
        if not isinstance(splits, list) or not all(isinstance(x, dict) and isinstance(x.get("file"), str) for x in splits):
            raise DroidError("invalid split_apks in manifest.json")
        apks = [inside(workdir, x["file"]) for x in splits]
        if not all(a.is_file() for a in apks):
            raise DroidError("manifest.json lists a split that is not in the package")
    else:
        apks = sorted(workdir.rglob("*.apk"))
        # bundletool .apks ships standalone APKs next to the splits; the splits are enough.
        if any("standalones" not in a.parts for a in apks):
            apks = [a for a in apks if "standalones" not in a.parts]
    apks = [a for a in apks if not is_foreign_abi_split(a.name)]
    if not apks:
        raise DroidError("no APK inside the package")
    icon = inside(workdir, manifest["icon"]) if isinstance(manifest.get("icon"), str) else None
    return apks, manifest, icon if icon and icon.is_file() else None


def inside(root: Path, name: str) -> Path:
    """Resolve an untrusted relative path, refusing anything that escapes `root`."""
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise DroidError(f"suspicious path in package: {name}")
    return target


def is_foreign_abi_split(name: str) -> bool:
    """Config splits for ABIs the VM cannot run (e.g. config.armeabi_v7a.apk, base-x86.apk)."""
    stem = name.lower().removesuffix(".apk")
    return any(stem.endswith(("." + abi, "-" + abi, "_" + abi)) for abi in FOREIGN_ABIS)


def native_abis(apks: list[Path]) -> set[str]:
    abis = set()
    for apk in apks:
        with zipfile.ZipFile(apk) as z:
            abis |= {n.split("/")[1] for n in z.namelist() if n.startswith("lib/") and n.endswith(".so") and n.count("/") >= 2}
    return abis


def check_abi(apks: list[Path]) -> None:
    abis = native_abis(apks)
    if abis and HOST_ABI not in abis:
        kind = "32-bit ARM" if abis & {"armeabi-v7a", "armeabi"} else ", ".join(sorted(abis))
        raise DroidError(
            f"the app only ships {kind} native code ({', '.join(sorted(abis))}); the VM runs {HOST_ABI} only. "
            "Apple Silicon cannot execute 32-bit ARM and there is no x86 translation. Look for the arm64-v8a build of the app."
        )


def base_apk(apks: list[Path]) -> Path:
    for apk in apks:
        with zipfile.ZipFile(apk) as z:
            if "classes.dex" in z.namelist():
                return apk
    return apks[0]


def parse_badging(text: str) -> dict:
    info = {}
    if m := re.search(r"^package: name='([^']+)'.*?versionName='([^']*)'", text, re.M):
        info["package"], info["version"] = m.group(1), m.group(2)
    if m := re.search(r"^application-label:'([^']*)'", text, re.M):
        info["label"] = m.group(1)
    if m := re.search(r"^launchable-activity: name='([^']+)'", text, re.M):
        info["activity"] = m.group(1)
    return info


def aapt2() -> str | None:
    for root in (SDK, HOME / "Library/Android/sdk"):
        found = sorted((root / "build-tools").glob("*/aapt2"))
        if found:
            return str(found[-1])
    return None


def apk_info(apk: Path, manifest: dict) -> dict:
    info = {}
    if a := aapt2():
        info = parse_badging(run(a, "dump", "badging", str(apk), check=False))
    info.setdefault("package", manifest.get("package_name"))
    info.setdefault("label", manifest.get("name") or info.get("package"))
    info.setdefault("version", manifest.get("version_name", ""))
    if not info["package"]:
        raise DroidError(f"could not read the package name of {apk}")
    return info


def render_icon(apks: list[Path], workdir: Path) -> Path | None:
    """Adaptive/vector icon through iconkit, falling back to the raster lookup."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "iconkit"))
    try:
        import iconkit
        out = workdir / "icon-rendered.png"
        if iconkit.extract_icon(apks, out):
            return out
    except Exception as e:  # iconkit is best effort; a missing icon must not block installs
        log(f"iconkit failed: {e}")
    finally:
        sys.path.pop(0)
    return find_icon_png(apks, workdir)


def find_icon_png(apks: list[Path], workdir: Path) -> Path | None:
    """Largest raster launcher icon across the APKs (adaptive XML icons are skipped)."""
    best = None
    for apk in apks:
        with zipfile.ZipFile(apk) as z:
            for i in z.infolist():
                if i.file_size > MAX_ICON_BYTES:
                    continue
                if re.search(r"ic_launcher(_round)?\.(png|webp)$", i.filename) and (best is None or i.file_size > best[2]):
                    best = (apk, i.filename, i.file_size)
    if not best:
        return None
    out = workdir / ("icon" + Path(best[1]).suffix)
    with zipfile.ZipFile(best[0]) as z:
        out.write_bytes(z.read(best[1]))
    return out


# ---------------------------------------------------------------- launcher .app

def clean_label(label: str) -> str:
    """App labels are untrusted: drop control, bidi and zero-width characters and cap the length."""
    label = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", label or "")
    return label.strip()[:80]


def safe_name(label: str) -> str:
    return re.sub(r'[/:\\"]', "-", clean_label(label)).strip(" .") or "Android App"


def icon_dimensions_ok(png: Path) -> bool:
    """Check the declared size before any decoder allocates the bitmap."""
    if png.stat().st_size > MAX_ICON_BYTES:
        return False
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(png)],
                         capture_output=True, text=True, timeout=30).stdout
    dims = [int(v) for v in re.findall(r"pixel(?:Width|Height): (\d+)", out)]
    return len(dims) == 2 and all(0 < d <= MAX_ICON_PIXELS for d in dims)


def short_version(version: str) -> str:
    """Bundle versions cannot carry the suffixes some apps add (e.g. '50.7.34-31 [0] [PR]')."""
    return version.split()[0] if version and version.split() else "1"


def launcher_binary() -> Path:
    root = Path(__file__).resolve().parent / "launcher"
    binary = root / "build/droidrt-launcher"
    sources = [root / "main.swift", root / "build.sh"]
    if not binary.exists() or any(src.stat().st_mtime > binary.stat().st_mtime for src in sources):
        log("building the native launcher")
        run("/bin/sh", str(root / "build.sh"), timeout=300)
    return binary


def info_plist(label: str, package: str, version: str) -> dict:
    return {
        "CFBundleName": label,
        "CFBundleDisplayName": label,
        "CFBundleIdentifier": f"local.droidrt.{package}",
        "CFBundleVersion": short_version(version),
        "CFBundleShortVersionString": short_version(version),
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "droidrt-launcher",
        "CFBundleIconFile": "icon",
        "LSUIElement": False,
        "DroidrtCommand": [sys.executable, str(Path(__file__).resolve())],
        "NSCameraUsageDescription": f"{label} uses the camera through Android.",
        "NSMicrophoneUsageDescription": f"{label} uses the microphone through Android.",
        "DroidrtPackage": package,
    }


def make_icns(png: Path, dest: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        src = Path(tmp) / "src.png"
        if subprocess.run(["sips", "-s", "format", "png", str(png), "--out", str(src)], capture_output=True).returncode:
            return False
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                suffix = "" if scale == 1 else "@2x"
                subprocess.run(["sips", "-z", str(px), str(px), str(src), "--out",
                                str(iconset / f"icon_{size}x{size}{suffix}.png")], capture_output=True)
        return subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)],
                              capture_output=True).returncode == 0


def launcher_path(label: str, package: str) -> Path:
    """Bundle path for a package; a label already taken by another package gets the package appended."""
    app = APPS_DIR / f"{safe_name(label)}.app"
    owner = launchers_by_path().get(app)
    if owner and owner != package:
        app = APPS_DIR / f"{safe_name(label)} ({package}).app"
    return app


def launchers_by_path() -> dict[Path, str]:
    return {app: pkg for pkg, (app, _) in launchers().items()}


def create_launcher(info: dict, icon: Path | None) -> Path:
    info = {**info, "label": clean_label(info["label"]) or info["package"]}
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    app = launcher_path(info["label"], info["package"])
    for old_pkg, (old_app, _) in launchers().items():
        if old_pkg == info["package"] and old_app != app:
            shutil.rmtree(old_app)
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents/MacOS"
    res = app / "Contents/Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)
    with open(app / "Contents/Info.plist", "wb") as f:
        plistlib.dump(info_plist(info["label"], info["package"], info.get("version", "")), f)
    shutil.copy2(launcher_binary(), macos / "droidrt-launcher")
    if icon and icon_dimensions_ok(icon):
        make_icns(icon, res / "icon.icns")
    subprocess.run(["xattr", "-cr", str(app)], capture_output=True)
    return app


# ---------------------------------------------------------------- commands

def install(src: Path, launcher: bool = True) -> dict:
    start()
    with tempfile.TemporaryDirectory(prefix="droidrt-") as tmp:
        work = Path(tmp)
        apks, manifest, icon = collect_apks(src, work)
        check_abi(apks)
        info = apk_info(base_apk(apks), manifest)
        log(f"installing {info['label']} ({info['package']} {info['version']}), {len(apks)} APK(s)")
        cmd = ["install-multiple", "-r", "-g"] if len(apks) > 1 else ["install", "-r", "-g"]
        out = adb(*cmd, *map(str, apks), timeout=600)
        if "Success" not in out:
            raise DroidError(f"install failed: {out.strip()}")
        if launcher:
            app = create_launcher(info, icon or render_icon(apks, work))
            info["app"] = str(app)
            log(f"launcher created: {app}")
    return info


def launch(package: str, title: str | None) -> int:
    if pgrep(f"scrcpy.*-s {SERIAL}.*--start-app={re.escape(package)}( |$)"):
        log(f"{package} is already open")
        return 0
    start()
    if not adb("shell", "pm", "path", package, check=False).strip():
        raise DroidError(f"{package} is not installed (use: droidrt install <file>)")
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # scrcpy sometimes loses the race with its device server ("Server connection failed")
    # and exits within seconds; a quick failure is retried, a normal window close is not.
    for attempt in range(3):
        t0 = time.monotonic()
        code = run_window(package, title)
        if code == 0 or time.monotonic() - t0 > 8:
            break
        log(f"window failed to open (attempt {attempt + 1}), retrying")
        time.sleep(1)
    if package == STORE_PACKAGE:
        # Apps installed from the Play Store get their launchers when the store closes.
        try:
            sync()
        except DroidError as e:
            log(f"sync failed: {e}")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "idle-stop"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return code


def flex_display_spec(spec: str) -> str:
    """Display spec for a resizable window, in macOS points instead of pixels.

    scrcpy's flex display sizes the Android display from the window size in points
    and draws one video pixel per point. A pixel sized spec such as 720x1560/320
    then needs a 720x1560 point window, twice the physical size on a Retina screen:
    the window is clamped to the screen without resizing the display, so the frame
    shows zoomed and cropped, and later resizes keep 320 dpi on a point sized
    display, which doubles the UI. Halving size and density keeps the same layout
    in dp (360x780/160 is 360x780 dp, like 720x1560/320), at the cost of a frame
    with half the pixels of the fixed size window.
    """
    m = re.fullmatch(r"(\d+)x(\d+)(?:/(\d+))?", spec)
    if not m:
        raise DroidError(f"invalid DROIDRT_DISPLAY '{spec}' (expected WIDTHxHEIGHT/DPI)")
    width, height = (max(2, int(v) // 2 // 2 * 2) for v in m.group(1, 2))
    return f"{width}x{height}" + (f"/{max(1, int(m.group(3)) // 2)}" if m.group(3) else "")


def window_command(package: str, title: str | None, resizable: bool = RESIZABLE) -> list[str]:
    display = ["--flex-display", f"--new-display={flex_display_spec(DISPLAY)}"] if resizable else [f"--new-display={DISPLAY}"]
    return [tool("scrcpy"), "-s", SERIAL, *display, f"--start-app={package}",
            "--display-ime-policy=local", "--keyboard=uhid",
            "--video-codec=h264", "--video-encoder=c2.android.avc.encoder", f"--video-bit-rate={VIDEO_BITRATE}",
            "--no-vd-system-decorations", "--no-audio", "--window-title", title or package,
            "--stay-awake", f"--max-fps={MAX_FPS}"]


def run_window(package: str, title: str | None) -> int:
    with open(LOG_DIR / f"{package}.log", "ab") as out:
        return subprocess.call(window_command(package, title), stdout=out, stderr=out)


def launchers() -> dict[str, tuple[Path, dict]]:
    """Installed launcher bundles keyed by Android package."""
    found = {}
    for app in sorted(APPS_DIR.glob("*.app")):
        try:
            with open(app / "Contents/Info.plist", "rb") as f:
                pl = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException):
            continue
        if pkg := pl.get("DroidrtPackage"):
            found[pkg] = (app, pl)
    return found


def list_apps() -> None:
    for pkg, (app, pl) in launchers().items():
        print(f"{pkg:40} {pl.get('CFBundleShortVersionString', ''):12} {app.name}")


def parse_packages(text: str) -> list[str]:
    return sorted(set(re.findall(r"^package:(\S+)", text, re.M)))


def parse_version(dumpsys: str) -> str:
    m = re.search(r"versionName=(\S+)", dumpsys)
    return m.group(1) if m else ""


def launchable_user_packages() -> list[str]:
    """Third-party packages with a launcher activity, plus the Play Store itself."""
    user = set(parse_packages(adb("shell", "pm", "list", "packages", "-3")))
    if adb("shell", "pm", "path", STORE_PACKAGE, check=False).strip():
        user.add(STORE_PACKAGE)
    out = adb("shell", "cmd", "package", "query-activities", "--brief",
              "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER")
    launchable = {line.strip().split("/", 1)[0] for line in out.splitlines() if "/" in line}
    return sorted(user & launchable)


def pull_apks(package: str, workdir: Path) -> list[Path]:
    paths = [l.split(":", 1)[1].strip() for l in adb("shell", "pm", "path", package).splitlines() if l.startswith("package:")]
    apks = []
    for i, remote in enumerate(paths):
        local = workdir / f"{i}-{Path(remote).name}"
        adb("pull", remote, str(local), timeout=600)
        apks.append(local)
    return apks


def sync() -> list[str]:
    """Create launchers for apps installed inside the VM and drop stale ones."""
    start()
    installed = launchable_user_packages()
    existing = launchers()
    created = []
    for pkg in installed:
        version = parse_version(adb("shell", "dumpsys", "package", pkg, check=False))
        if pkg in existing and existing[pkg][1].get("CFBundleShortVersionString") == short_version(version):
            continue
        with tempfile.TemporaryDirectory(prefix="droidrt-") as tmp:
            work = Path(tmp)
            apks = pull_apks(pkg, work)
            if not apks:
                continue
            info = apk_info(base_apk(apks), {"package_name": pkg, "version_name": version})
            info["package"], info["version"] = pkg, version
            if pkg in existing:
                shutil.rmtree(existing[pkg][0])
            app = create_launcher(info, render_icon(apks, work))
            created.append(str(app))
            log(f"launcher created: {app}")
    for pkg, (app, _) in existing.items():
        if pkg not in installed:
            shutil.rmtree(app)
            log(f"removed {app} ({pkg} is no longer installed)")
    return created


def set_location(lat: float, lon: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise DroidError("latitude must be within -90..90 and longitude within -180..180")
    start()
    # The emulator console takes longitude first. The fix reaches the gps provider and,
    # through it, the fused provider, so apps with an active location request get it.
    out = adb("emu", "geo", "fix", f"{lon:.6f}", f"{lat:.6f}", timeout=10)
    if "OK" not in out:
        raise DroidError(f"geo fix failed: {out.strip()}")


def uninstall(package: str) -> None:
    start()
    adb("uninstall", package, check=False)
    if package in (found := launchers()):
        shutil.rmtree(found[package][0])
        log(f"removed {found[package][0]}")


def status() -> None:
    pids = vm_pids()
    print(f"VM {AVD}: {'running' if pids else 'stopped'} {('pid ' + ','.join(map(str, pids))) if pids else ''}".rstrip())
    if pids:
        print(f"boot completed: {'yes' if is_booted() else 'no'}")
        print(f"open windows: {len(window_pids())}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="droidrt", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="create or update the headless AVD")
    sub.add_parser("start", help="start the VM and wait for boot")
    sub.add_parser("stop", help="stop the VM")
    sub.add_parser("status")
    sub.add_parser("list", help="list installed launchers")
    s = sub.add_parser("install", help="install an .apk/.xapk/.apks/.apkm and create its launcher .app")
    s.add_argument("file", type=Path)
    s.add_argument("--no-launcher", action="store_true")
    s = sub.add_parser("launch", help="open an app in its own window")
    s.add_argument("package")
    s.add_argument("--title")
    sub.add_parser("sync", help="create launchers for apps installed through the Play Store")
    sub.add_parser("store", help="open the Play Store; launchers are synced when it closes")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    s = sub.add_parser("geo", help="set the VM GPS location")
    s.add_argument("lat", type=float)
    s.add_argument("lon", type=float)
    s = sub.add_parser("uninstall")
    s.add_argument("package")
    s = sub.add_parser("idle-stop", help=argparse.SUPPRESS)
    s.add_argument("--grace", type=int, default=IDLE_GRACE)
    a = p.parse_args(argv)
    try:
        match a.cmd:
            case "setup": setup()
            case "start": start()
            case "stop": stop()
            case "status": status()
            case "list": list_apps()
            case "install":
                print(json.dumps(install(a.file.expanduser().resolve(), not a.no_launcher), ensure_ascii=False))
            case "launch": return launch(a.package, a.title)
            case "uninstall": uninstall(a.package)
            case "sync": print(json.dumps(sync(), ensure_ascii=False))
            case "store": return launch(STORE_PACKAGE, "Play Store")
            case "geo": set_location(a.lat, a.lon)
            case "mcp":
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from droidrt_mcp.server import main as mcp_main
                return mcp_main()
            case "idle-stop": idle_stop(a.grace)
    except DroidError as e:
        log(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
