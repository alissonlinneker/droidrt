#!/opt/homebrew/bin/python3
"""droidrt: run Android apps on macOS as individual windows.

Each app runs on a headless ARM64 Android micro-VM (Hypervisor.framework, no
instruction translation) and is shown in its own macOS window through a scrcpy
virtual display. Installing an APK/XAPK/APKS also creates a launcher .app.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import plistlib
import re
import shutil
import signal
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
LOCK_DIR = STATE_DIR / "locks"
# Marker touched whenever a window closes; idle-stop leaves the VM alone if a newer one exists.
LAST_CLOSE = STATE_DIR / "last-window-close"
DISPLAY = os.environ.get("DROIDRT_DISPLAY", "720x1560/320")
# Opt-in resizable windows (scrcpy --flex-display). Off by default: see flex_display_spec().
RESIZABLE = os.environ.get("DROIDRT_RESIZABLE", "0") == "1"
IDLE_GRACE = int(os.environ.get("DROIDRT_IDLE_GRACE", "120"))
MAX_FPS = os.environ.get("DROIDRT_MAX_FPS", "30")
BOOT_TIMEOUT = 240
# Default for any external command; long operations pass their own timeout.
CMD_TIMEOUT = 120
# Android package names: dot separated segments of letters, digits and underscores.
PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*")

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


class Interrupted(DroidError):
    """SIGTERM arrived while `launch` had no window to forward it to."""


def tool(name: str) -> str:
    path = shutil.which(name, path=TOOL_PATH)
    if not path:
        raise DroidError(f"'{name}' not found (searched PATH: {TOOL_PATH})")
    return path


def run(*args: str, check: bool = True, timeout: float = CMD_TIMEOUT) -> str:
    """Run a command without a shell; a hang or a missing binary becomes a DroidError."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired:
        raise DroidError(f"{' '.join(args)} timed out after {timeout:.0f}s") from None
    except OSError as e:
        raise DroidError(f"{args[0]}: {e.strerror or e}") from None
    if check and proc.returncode != 0:
        raise DroidError(f"{' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def adb(*args: str, check: bool = True, timeout: float = CMD_TIMEOUT) -> str:
    return run(tool("adb"), "-s", SERIAL, *args, check=check, timeout=timeout)


def pgrep(pattern: str) -> list[int]:
    out = run("/usr/bin/pgrep", "-f", pattern, check=False, timeout=10)
    return [int(p) for p in out.split() if p.isdigit()]


def check_package(package: str) -> str:
    """Package names end up in adb shell commands, file names and bundle ids: accept only valid ones."""
    if not isinstance(package, str) or len(package) > 255 or not PACKAGE_RE.fullmatch(package):
        raise DroidError(f"invalid package name: {package!r}")
    return package


# ---------------------------------------------------------------- locks

def acquire_lock(name: str, shared: bool = False, timeout: float = 0) -> int | None:
    """flock() a file in LOCK_DIR; returns the descriptor, or None if still busy after `timeout`.

    The kernel drops the lock when the process dies, so a crash never leaves it stale.
    """
    for d in (LOCK_DIR.parent, LOCK_DIR):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(LOCK_DIR / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.2)


def release_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)


@contextlib.contextmanager
def locked(name: str, shared: bool = False, timeout: float = CMD_TIMEOUT):
    fd = acquire_lock(name, shared, timeout)
    if fd is None:
        raise DroidError(f"timed out waiting for another droidrt command ({name})")
    try:
        yield
    finally:
        release_lock(fd)


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
    """Create or update the AVD. Callers hold vm.lock (see start() and main())."""
    avd_dir = AVD_HOME / f"{AVD}.avd"
    current = avd_image(avd_dir / "config.ini")
    image_dir = SDK / Path(*IMAGE.split(";"))
    if current and current != IMAGE and vm_pids():
        raise DroidError("the VM is running; run 'droidrt stop' before changing the image")
    if (current != IMAGE or not avd_dir.exists()) and not image_dir.exists():
        # Download first: a failed download must not cost the existing AVD and its apps.
        log(f"downloading {IMAGE}")
        run(tool("sdkmanager"), f"--sdk_root={SDK}", IMAGE, timeout=3600)
    if current and current != IMAGE:
        log(f"image changed ({current} to {IMAGE}): recreating the AVD, installed apps will be lost")
        shutil.rmtree(avd_dir)
        (AVD_HOME / f"{AVD}.ini").unlink(missing_ok=True)
    if not avd_dir.exists():
        log(f"creating AVD '{AVD}' ({IMAGE})")
        # avdmanager prints a devices.xml warning on Homebrew SDKs but still creates the AVD.
        try:
            subprocess.run(
                [tool("avdmanager"), "create", "avd", "-n", AVD, "-k", IMAGE, "-d", "pixel_6", "--force"],
                input="no\n", capture_output=True, text=True, timeout=CMD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise DroidError("avdmanager timed out") from None
        if not avd_dir.exists():
            raise DroidError("avdmanager did not create the AVD")
    apply_avd_settings(avd_dir / "config.ini", AVD_SETTINGS)


def is_booted() -> bool:
    try:
        return adb("shell", "getprop", "sys.boot_completed", check=False, timeout=5).strip() == "1"
    except DroidError:  # adb hung (server starting, device offline) or is missing
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
    try:
        cams = parse_webcams(
            run(tool("emulator"), "-webcam-list", check=False, timeout=60),
            run("/usr/sbin/system_profiler", "SPCameraDataType", check=False, timeout=60),
        )
    except DroidError as e:  # a camera probe must never keep the VM from starting
        log(f"camera detection failed ({e}), using the emulated camera")
        return "emulated"
    for pattern in [wanted] if wanted else ["FaceTime", "Built-in", "Integrada"]:
        for wid, name in cams:
            if pattern.lower() in name.lower():
                return wid
    if wanted:
        log(f"no camera matches DROIDRT_CAMERA={wanted!r}")
    return cams[0][0] if cams else "emulated"


def start() -> None:
    if is_booted():
        return
    # vm.lock serializes boots: without it two launches with the VM stopped both see no
    # qemu process (the emulator takes a moment to exec it) and both spawn one.
    with locked("vm.lock", timeout=BOOT_TIMEOUT + 60):
        if is_booted():  # booted by whoever held the lock
            return
        setup()
        emulator = None
        if not vm_pids():
            LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            camera = pick_webcam()
            log(f"starting headless VM (back camera: {camera})")
            with open(LOG_DIR / "vm.log", "ab") as out:
                emulator = subprocess.Popen(
                    [tool("emulator"), "-avd", AVD, "-port", str(PORT), "-no-window",
                     *([] if AUDIO else ["-no-audio"]), *(["-allow-host-audio"] if AUDIO and MIC else []),
                     "-no-boot-anim", "-no-snapshot-save", "-gpu", GPU,
                     "-camera-back", camera, "-camera-front", "none"],
                    stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
                )
        t0 = time.monotonic()
        while not is_booted():
            if emulator is not None and emulator.poll() is not None and not vm_pids():
                raise DroidError(f"the emulator exited with status {emulator.returncode} "
                                 "(see ~/Library/Logs/droidrt/vm.log)")
            if time.monotonic() - t0 > BOOT_TIMEOUT:
                raise DroidError(f"VM did not finish booting in {BOOT_TIMEOUT}s (see ~/Library/Logs/droidrt/vm.log)")
            time.sleep(1)
        log(f"VM ready in {time.monotonic() - t0:.0f}s")


def stop() -> None:
    if not vm_pids():
        return
    try:
        adb("emu", "kill", check=False, timeout=10)
    except DroidError as e:  # console unresponsive: fall through to SIGTERM
        log(f"emu kill failed: {e}")
    for _ in range(20):
        if not vm_pids():
            return
        time.sleep(0.5)
    for pid in vm_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def window_pids() -> list[int]:
    return pgrep(f"scrcpy.*-s {SERIAL}")


def last_window_close() -> float:
    try:
        return LAST_CLOSE.stat().st_mtime
    except OSError:
        return 0.0


def idle_stop(grace: int) -> None:
    """Stop the VM once no app window has been open for `grace` seconds.

    Checking for windows and stopping happen while holding busy.lock exclusively and
    vm.lock: a launch holds busy.lock shared from before booting until its window
    closes, so a launch that is booting or opening a window can never be cut off.
    """
    began = time.time()
    time.sleep(grace)
    if last_window_close() > began:
        return  # a window closed after this countdown began; its own idle-stop decides
    busy = acquire_lock("busy.lock")
    if busy is None:
        return  # a launch is booting the VM or showing a window
    try:
        vm = acquire_lock("vm.lock")
        if vm is None:
            return  # someone is booting the VM
        try:
            if window_pids():
                return
            log("no open windows, stopping the VM")
            stop()
        finally:
            release_lock(vm)
    finally:
        release_lock(busy)


# ---------------------------------------------------------------- packages

def collect_apks(src: Path, workdir: Path) -> tuple[list[Path], dict, Path | None]:
    """Return (apk files, xapk manifest or {}, bundled icon) for .apk/.xapk/.apks/.apkm."""
    if not src.is_file():
        raise DroidError(f"{src} does not exist or is not a file")
    if src.suffix.lower() == ".apk":
        if not zipfile.is_zipfile(src):
            raise DroidError(f"{src} is not a valid APK")
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
    if bad := [a.name for a in apks if not zipfile.is_zipfile(a)]:
        raise DroidError(f"corrupt APK inside the package: {', '.join(bad)}")
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
    # manifest.json is untrusted metadata, used only when aapt2 is missing or silent.
    info.setdefault("package", manifest.get("package_name"))
    info.setdefault("label", manifest.get("name") or info.get("package"))
    info.setdefault("version", manifest.get("version_name", ""))
    if not info["package"]:
        raise DroidError(f"could not read the package name of {apk}")
    check_package(info["package"])
    info["label"] = info["label"] if isinstance(info["label"], str) else info["package"]
    info["version"] = info["version"] if isinstance(info["version"], str) else ""
    return info


def render_icon(apks: list[Path], workdir: Path) -> Path | None:
    """Adaptive/vector icon through iconkit, falling back to the raster lookup."""
    entry = str(Path(__file__).resolve().parent / "iconkit")
    sys.path.insert(0, entry)
    try:
        import iconkit
        out = workdir / "icon-rendered.png"
        if iconkit.extract_icon(apks, out):
            return out
    except Exception as e:  # iconkit is best effort; a missing icon must not block installs
        log(f"iconkit failed: {e}")
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(entry)
    try:
        return find_icon_png(apks, workdir)
    except (OSError, zipfile.BadZipFile) as e:
        log(f"icon lookup failed: {e}")
        return None


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
    name = re.sub(r'[/:\\"]', "-", clean_label(label)).strip(" .")
    # 80 characters can be 320 UTF-8 bytes; keep room under the 255 byte file name limit.
    while len(name.encode()) > 120:
        name = name[:-1]
    return name.rstrip(" .") or "Android App"


def icon_dimensions_ok(png: Path) -> bool:
    """Check the declared size before any decoder allocates the bitmap."""
    if png.stat().st_size > MAX_ICON_BYTES:
        return False
    try:
        out = run("/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(png), check=False, timeout=30)
    except DroidError:
        return False
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
        # Bundle ids allow letters, digits, hyphens and periods; package names may have underscores.
        "CFBundleIdentifier": "local.droidrt." + package.replace("_", "-"),
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
        # The full version; CFBundleShortVersionString drops suffixes and cannot tell builds apart.
        "DroidrtVersion": version,
    }


def make_icns(png: Path, dest: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        src = Path(tmp) / "src.png"
        try:
            run("/usr/bin/sips", "-s", "format", "png", str(png), "--out", str(src), timeout=60)
            for size in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    px = size * scale
                    suffix = "" if scale == 1 else "@2x"
                    run("/usr/bin/sips", "-z", str(px), str(px), str(src), "--out",
                        str(iconset / f"icon_{size}x{size}{suffix}.png"), check=False, timeout=60)
            run("/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(dest), timeout=60)
        except DroidError as e:  # the bundle keeps the generic icon
            log(f"icon conversion failed: {e}")
            return False
        return True


def bundle_package(app: Path) -> str | None:
    """DroidrtPackage of a launcher bundle, or None for anything droidrt did not create."""
    if app.is_symlink():
        return None
    try:
        with open(app / "Contents/Info.plist", "rb") as f:
            pl = plistlib.load(f)
    except Exception:  # OSError, InvalidFileException, ExpatError, ...: not one of ours
        return None
    pkg = pl.get("DroidrtPackage") if isinstance(pl, dict) else None
    return pkg if isinstance(pkg, str) else None


def launcher_path(label: str, package: str) -> Path:
    """Bundle path for a package. A name taken by anything else (another package's launcher
    or an unrelated app) is never reused: the package, then a counter, is appended."""
    base = safe_name(label)
    names = [f"{base}.app", f"{base} ({package}).app", *(f"{base} ({package}) {n}.app" for n in range(2, 100))]
    for name in names:
        app = APPS_DIR / name
        # exists() follows the case-insensitive file system: "whatsapp.app" blocks "WhatsApp.app".
        if (not app.exists() and not app.is_symlink()) or bundle_package(app) == package:
            return app
    raise DroidError(f"no free launcher name for {package} in {APPS_DIR}")


def remove_bundle(app: Path) -> None:
    try:
        if app.is_symlink():
            app.unlink()
        else:
            shutil.rmtree(app)
    except FileNotFoundError:
        pass


def create_launcher(info: dict, icon: Path | None) -> Path:
    """Build the bundle in a staging directory and swap it in, so a failure halfway
    (launcher build, disk full) leaves the previous launcher untouched.
    Callers hold launchers.lock."""
    package = check_package(info["package"])
    label = clean_label(info["label"]) or package
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    binary = launcher_binary()
    staging = Path(tempfile.mkdtemp(prefix=".droidrt-", dir=APPS_DIR))
    try:
        staging.chmod(0o755)
        macos = staging / "Contents/MacOS"
        res = staging / "Contents/Resources"
        macos.mkdir(parents=True)
        res.mkdir(parents=True)
        with open(staging / "Contents/Info.plist", "wb") as f:
            plistlib.dump(info_plist(label, package, info.get("version", "")), f)
        shutil.copy2(binary, macos / "droidrt-launcher")
        if icon and icon_dimensions_ok(icon):
            make_icns(icon, res / "icon.icns")
        run("/usr/bin/xattr", "-cr", str(staging), check=False, timeout=60)
        app = launcher_path(label, package)
        for old in launcher_bundles():
            if old[1].get("DroidrtPackage") == package:
                remove_bundle(old[0])
        os.rename(staging, app)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return app


# ---------------------------------------------------------------- commands

def install(src: Path, launcher: bool = True) -> dict:
    with tempfile.TemporaryDirectory(prefix="droidrt-") as tmp:
        work = Path(tmp)
        # Validate the package before booting the VM for it.
        apks, manifest, icon = collect_apks(src, work)
        check_abi(apks)
        info = apk_info(base_apk(apks), manifest)
        start()
        log(f"installing {info['label']} ({info['package']} {info['version']}), {len(apks)} APK(s)")
        cmd = ["install-multiple", "-r", "-g"] if len(apks) > 1 else ["install", "-r", "-g"]
        out = adb(*cmd, *map(str, apks), timeout=600)
        if "Success" not in out:
            raise DroidError(f"install failed: {out.strip()}")
        if launcher:
            icon = icon or render_icon(apks, work)
            with locked("launchers.lock"):
                app = create_launcher(info, icon)
            info["app"] = str(app)
            log(f"launcher created: {app}")
    return info


def spawn_idle_stop() -> None:
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "idle-stop"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        log(f"could not schedule the idle VM shutdown: {e}")


def launch(package: str, title: str | None) -> int:
    check_package(package)
    # One launch per package: a second one (double click, launcher plus MCP) while the
    # first is still booting would otherwise open a second window for the same app.
    own = acquire_lock(f"launch-{package}.lock")
    if own is None or pgrep(f"scrcpy.*-s {SERIAL}.*--start-app={re.escape(package)}( |$)"):
        release_lock(own)
        log(f"{package} is already open")
        return 0
    # The launcher stops a window with SIGTERM. Forward it to scrcpy while a window is up,
    # otherwise abort (boot, retry pause): either way the idle shutdown below still runs.
    # Raising is only safe inside the try block below ("abortable") and never between
    # spawning scrcpy and recording it ("spawning"), or the window would be orphaned.
    state = {"stopping": False, "spawning": False, "abortable": False, "window": None}

    def on_term(signum, frame):
        state["stopping"] = True
        window = state["window"]
        if window is not None and window.poll() is None:
            window.terminate()
        elif state["abortable"] and not state["spawning"]:
            raise Interrupted("stopped by signal")

    previous = signal.signal(signal.SIGTERM, on_term)
    busy = None
    code = 1
    try:
        state["abortable"] = True
        # busy.lock (shared) tells idle-stop that a launch is in flight; see idle_stop().
        busy = acquire_lock("busy.lock", shared=True, timeout=60)
        if busy is None:
            raise DroidError("timed out waiting for the VM to stop")
        start()
        if not adb("shell", "pm", "path", package, check=False, timeout=30).strip():
            raise DroidError(f"{package} is not installed (use: droidrt install <file>)")
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        # scrcpy sometimes loses the race with its device server ("Server connection failed")
        # and exits within seconds with status 1; such a quick failure is retried. A window
        # closed by the user (0) or stopped by a signal (negative status) is not.
        for attempt in range(3):
            t0 = time.monotonic()
            code = run_window(package, title, state)
            if state["stopping"] or code <= 0 or time.monotonic() - t0 > 8 or attempt == 2:
                break
            log(f"window failed to open (attempt {attempt + 1}), retrying")
            time.sleep(1)
        # The window is gone: a new launch of this package may proceed during the sync below.
        release_lock(own)
        own = None
        if package == STORE_PACKAGE and not state["stopping"]:
            # Apps installed from the Play Store get their launchers when the store closes.
            try:
                sync()
            except Interrupted:
                raise
            except DroidError as e:
                log(f"sync failed: {e}")
        state["abortable"] = False
        return code
    finally:
        state["abortable"] = False
        signal.signal(signal.SIGTERM, previous)
        with contextlib.suppress(OSError):
            LAST_CLOSE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            LAST_CLOSE.touch()
        release_lock(busy)
        release_lock(own)
        spawn_idle_stop()


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


def run_window(package: str, title: str | None, state: dict | None = None) -> int:
    """Run scrcpy until the window closes. `state` lets launch()'s SIGTERM handler reach it."""
    state = state if state is not None else {"stopping": False, "spawning": False, "window": None}
    with open(LOG_DIR / f"{package}.log", "ab") as out:
        state["spawning"] = True
        try:
            window = subprocess.Popen(window_command(package, title), stdin=subprocess.DEVNULL, stdout=out, stderr=out)
            state["window"] = window
        finally:
            state["spawning"] = False
        try:
            if state["stopping"]:  # SIGTERM arrived while scrcpy was being spawned
                window.terminate()
            return window.wait()
        finally:
            state["window"] = None


def launcher_bundles() -> list[tuple[Path, dict]]:
    """Every launcher bundle in APPS_DIR with its Info.plist; unreadable bundles are skipped."""
    found = []
    for app in sorted(APPS_DIR.glob("*.app")):
        try:
            with open(app / "Contents/Info.plist", "rb") as f:
                pl = plistlib.load(f)
        except Exception:  # OSError, InvalidFileException, ExpatError: not a usable launcher
            continue
        if isinstance(pl, dict) and isinstance(pl.get("DroidrtPackage"), str):
            found.append((app, pl))
    return found


def launchers() -> dict[str, tuple[Path, dict]]:
    """Installed launcher bundles keyed by Android package."""
    return {pl["DroidrtPackage"]: (app, pl) for app, pl in launcher_bundles()}


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


def launcher_is_current(pl: dict, version: str) -> bool:
    """Compare the full version; bundles from older releases only kept the short one."""
    built = pl.get("DroidrtVersion")
    if isinstance(built, str):
        return built == version
    return pl.get("CFBundleShortVersionString") == short_version(version)


def is_installed(package: str) -> bool:
    return bool(adb("shell", "pm", "path", package, check=False, timeout=30).strip())


def sync() -> list[str]:
    """Create launchers for apps installed inside the VM and drop stale ones."""
    start()
    with locked("launchers.lock", timeout=600):
        installed = launchable_user_packages()
        existing = launchers()
        created = []
        for pkg in installed:
            version = parse_version(adb("shell", "dumpsys", "package", pkg, check=False))
            if pkg in existing and launcher_is_current(existing[pkg][1], version):
                continue
            with tempfile.TemporaryDirectory(prefix="droidrt-") as tmp:
                work = Path(tmp)
                try:
                    apks = pull_apks(pkg, work)
                    if not apks:
                        continue
                    info = apk_info(base_apk(apks), {"package_name": pkg, "version_name": version})
                    info["package"], info["version"] = pkg, version
                    # create_launcher replaces the old bundle only once the new one is ready.
                    app = create_launcher(info, render_icon(apks, work))
                except (DroidError, zipfile.BadZipFile) as e:  # one bad app must not stop the others
                    log(f"skipped {pkg}: {e}")
                    continue
                created.append(str(app))
                log(f"launcher created: {app}")
        for pkg, (app, _) in existing.items():
            # The listing above can come back short (package manager still settling, apps
            # without a launcher activity): only drop a launcher whose package is really gone.
            if pkg not in installed and not is_installed(pkg):
                remove_bundle(app)
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
    check_package(package)
    start()
    out = adb("uninstall", package, check=False, timeout=CMD_TIMEOUT)
    if "Success" not in out and is_installed(package):
        raise DroidError(f"uninstall failed: {out.strip() or 'unknown error'}")
    with locked("launchers.lock"):
        for app, pl in launcher_bundles():
            if pl["DroidrtPackage"] == package:
                remove_bundle(app)
                log(f"removed {app}")


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
            case "setup":
                with locked("vm.lock", timeout=BOOT_TIMEOUT + 60):
                    setup()
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
    except (OSError, zipfile.BadZipFile) as e:  # unreadable files, full disk: a message, not a traceback
        log(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
