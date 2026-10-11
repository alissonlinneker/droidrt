#!/usr/bin/env python3
"""Compatibility matrix runner for droidrt.

For every app listed in apps.json this script installs the APK on the already
running droidrt VM, opens it on a fresh scrcpy virtual display while recording,
grabs the last frame, checks the crash log buffer and whether the process is
still alive, samples the qemu CPU usage and writes a Markdown report.

The VM is never started or stopped here: the script only checks that the
device is ready and aborts otherwise.

    ./compat/run_matrix.py --dry-run
    ./compat/run_matrix.py --seconds 30 --only org.videolan.vlc
    ./compat/run_matrix.py --cleanup
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
SDK = Path(os.environ.get("DROIDRT_SDK", "/opt/homebrew/share/android-commandlinetools"))
TOOL_PATH = os.pathsep.join(
    [str(SDK / "platform-tools"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
)
DEFAULT_SERIAL = "emulator-5580"
DEFAULT_DISPLAY = "720x1560/320"
TERM_WAIT = 15  # seconds to wait for scrcpy to exit after each SIGTERM


class MatrixError(Exception):
    pass


class Terminated(Exception):
    """Raised from the SIGTERM/SIGHUP handler so cleanup code still runs."""


def _on_terminate(signum: int, _frame) -> None:
    raise Terminated(f"received signal {signum}")


def tool(name: str) -> str:
    path = shutil.which(name, path=TOOL_PATH)
    if not path:
        raise MatrixError(f"'{name}' not found (PATH used: {TOOL_PATH})")
    return path


def log(msg: str) -> None:
    print(f"run_matrix: {msg}", file=sys.stderr, flush=True)


def run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


@dataclass
class App:
    package: str
    name: str
    stack: str
    version: str
    file: Path
    sha256: str
    test: str


@dataclass
class Result:
    app: App
    installed: bool = False
    opened: bool = False
    crashed: bool = False
    frame: Path | None = None
    frame_luma: float | None = None
    cpu_avg: float | None = None
    cpu_peak: float | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- inputs

def load_apps(path: Path, apk_dir: Path | None, only: list[str]) -> list[App]:
    data = json.loads(path.read_text())
    base = (apk_dir or Path(data["apk_dir"])).expanduser()
    apps = [
        App(a["package"], a["name"], a["stack"], a["version"], base / a["file"], a["sha256"], a["test"])
        for a in data["apps"]
    ]
    if only:
        unknown = set(only) - {a.package for a in apps}
        if unknown:
            raise MatrixError(f"unknown package(s) in --only: {', '.join(sorted(unknown))}")
        apps = [a for a in apps if a.package in only]
    return apps


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- device

class Device:
    def __init__(self, serial: str):
        self.serial = serial
        self.adb_path = tool("adb")

    def adb(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return run([self.adb_path, "-s", self.serial, *args], timeout=timeout)

    def shell(self, *args: str, timeout: float = 30) -> str:
        return self.adb("shell", *args, timeout=timeout).stdout.strip()

    def require_ready(self) -> None:
        state = self.adb("get-state", timeout=10)
        if state.returncode != 0 or state.stdout.strip() != "device":
            detail = (state.stderr or state.stdout).strip() or "no output"
            raise MatrixError(f"{self.serial} is not ready (get-state: {detail}). Start the VM with droidrt first.")
        if self.shell("getprop", "sys.boot_completed", timeout=10) != "1":
            raise MatrixError(f"{self.serial} is attached but has not finished booting")

    def pidof(self, package: str) -> str:
        return self.shell("pidof", package, timeout=10)


# ---------------------------------------------------------------- qemu CPU

class CpuSampler(threading.Thread):
    """Samples the qemu process with ps while an app is on screen.

    The average comes from the cumulative CPU time delta (exact), the peak from
    the instantaneous %cpu column (macOS decaying average).
    """

    def __init__(self, avd: str, interval: float = 2.0):
        super().__init__(daemon=True)
        self.avd = avd
        self.interval = interval
        self.stop_event = threading.Event()
        self.pid = self._find_pid()
        self.peak: float | None = None
        self.avg: float | None = None

    def _find_pid(self) -> int | None:
        out = run(["/usr/bin/pgrep", "-f", f"qemu-system-aarch64.*-avd {re.escape(self.avd)}( |$)"], timeout=10).stdout.split()
        return int(out[0]) if out else None

    def _ps(self, column: str) -> str:
        return run(["/bin/ps", "-o", f"{column}=", "-p", str(self.pid)], timeout=10).stdout.strip()

    @staticmethod
    def _cputime(text: str) -> float:
        # ps prints [[dd-]hh:]mm:ss.cc
        days, _, rest = text.rpartition("-")
        parts = [float(p) for p in rest.split(":")]
        secs = 0.0
        for p in parts:
            secs = secs * 60 + p
        return secs + (int(days) * 86400 if days else 0)

    def run(self) -> None:
        if self.pid is None:
            return
        try:
            t0, c0 = time.monotonic(), self._cputime(self._ps("time"))
            while not self.stop_event.wait(self.interval):
                pct = self._ps("%cpu")
                if pct:
                    self.peak = max(self.peak or 0.0, float(pct.replace(",", ".")))
            t1, c1 = time.monotonic(), self._cputime(self._ps("time"))
            if t1 > t0:
                self.avg = 100.0 * (c1 - c0) / (t1 - t0)
        except (ValueError, subprocess.SubprocessError):
            pass

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=15)


# ---------------------------------------------------------------- scrcpy

def scrcpy_cmd(serial: str, display: str, package: str, record: Path) -> list[str]:
    return [
        tool("scrcpy"), "-s", serial, f"--new-display={display}", f"--start-app={package}",
        "--no-window", "--no-audio", f"--record={record}",
    ]


def stop_scrcpy(proc: subprocess.Popen, notes: list[str]) -> None:
    """Stops scrcpy with SIGTERM only.

    SIGINT is ignored by background jobs and SIGKILL leaves the device side
    server dangling (it has hung system_server before), so neither is used.
    A SIGTERM/SIGHUP sent to this script while it waits is held back until
    scrcpy is stopped, so an interrupted run does not orphan it.
    """
    held = {signal.SIGTERM, signal.SIGHUP}
    signal.pthread_sigmask(signal.SIG_BLOCK, held)
    try:
        for attempt in (1, 2):
            if proc.poll() is not None:
                return
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=TERM_WAIT)
                return
            except subprocess.TimeoutExpired:
                log(f"scrcpy pid {proc.pid} still running after SIGTERM #{attempt}")
        notes.append(f"scrcpy pid {proc.pid} did not exit after two SIGTERM, left running for manual check")
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, held)


# ---------------------------------------------------------------- media

def last_frame(ffmpeg: str, video: Path, image: Path) -> bool:
    if not video.exists() or video.stat().st_size == 0:
        return False
    # Seek near the end and keep overwriting the output so the final frame wins.
    for pre in (["-sseof", "-3"], []):
        proc = run([ffmpeg, "-v", "error", "-y", *pre, "-i", str(video), "-update", "1", str(image)], timeout=120)
        if proc.returncode == 0 and image.exists() and image.stat().st_size > 0:
            return True
    return False


def mean_luma(ffmpeg: str, image: Path) -> float | None:
    proc = run([ffmpeg, "-v", "error", "-i", str(image), "-vf", "signalstats,metadata=print:file=-",
                "-f", "null", "-"], timeout=60)
    m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", proc.stdout)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- per app

def open_with_scrcpy(dev: Device, app: App, args: argparse.Namespace, work: Path, record: Path,
                     res: Result) -> tuple[int | None, str]:
    """Keeps the app open on a new scrcpy display for args.seconds.

    Returns (scrcpy exit code if it quit early, else None; pid of the app).
    scrcpy is always stopped before returning, including on errors.
    """
    early_exit: int | None = None
    pid = ""
    for attempt in range(3):
        started = time.monotonic()
        with open(work / "scrcpy.log", "wb") as slog:
            proc = subprocess.Popen(scrcpy_cmd(args.serial, args.display, app.package, record),
                                    stdout=slog, stderr=slog, stdin=subprocess.DEVNULL,
                                    start_new_session=True)  # keep terminal Ctrl-C away from scrcpy
            try:
                deadline = started + args.seconds
                while time.monotonic() < deadline and proc.poll() is None:
                    time.sleep(0.5)
                early_exit = proc.poll()
                # Check the process while the display still exists.
                pid = dev.pidof(app.package)
            finally:
                stop_scrcpy(proc, res.notes)
        # scrcpy sometimes loses the race with its device server and quits within
        # seconds ("Server connection failed"); that is a harness failure, so retry.
        flaky = b"Server connection failed" in (work / "scrcpy.log").read_bytes()
        if early_exit is None or not flaky or time.monotonic() - started > 10:
            break
        log(f"{app.package}: scrcpy connection failed, retrying ({attempt + 1})")
        time.sleep(2)
    return early_exit, pid


def test_app(dev: Device, app: App, args: argparse.Namespace, out_dir: Path) -> Result:
    res = Result(app)
    work = out_dir / app.package
    work.mkdir(parents=True, exist_ok=True)

    if not app.file.exists():
        res.notes.append(f"APK not found: {app.file}")
        return res
    if not args.skip_hash and sha256_of(app.file) != app.sha256:
        res.notes.append("sha256 mismatch, skipped")
        return res

    log(f"{app.package}: installing")
    inst = dev.adb("install", "-r", "-g", str(app.file), timeout=600)
    (work / "install.log").write_text(inst.stdout + inst.stderr)
    if "Success" not in inst.stdout:
        tail = (inst.stderr or inst.stdout).strip().splitlines()
        res.notes.append("install failed" + (f": {tail[-1][:120]}" if tail else ""))
        return res
    res.installed = True

    dev.adb("logcat", "-b", "crash", "-c", timeout=15)
    record = work / "screen.mp4"
    record.unlink(missing_ok=True)
    sampler = CpuSampler(args.avd)
    if sampler.pid is None:
        res.notes.append("qemu process not found, CPU not measured")
    sampler.start()

    log(f"{app.package}: opening for {args.seconds}s")
    try:
        early_exit, pid = open_with_scrcpy(dev, app, args, work, record, res)
    finally:
        # Always stop the sampler, even when adb times out mid run; otherwise
        # its thread keeps polling ps for the rest of the matrix.
        sampler.stop()

    if early_exit is not None:
        res.notes.append(f"scrcpy exited early with code {early_exit}")
    res.opened = bool(pid)
    res.cpu_avg, res.cpu_peak = sampler.avg, sampler.peak

    crash = dev.adb("logcat", "-b", "crash", "-d", timeout=30).stdout
    (work / "crash.log").write_text(crash)
    if re.search(rf"(Process: |>>> ){re.escape(app.package)}\b|\b{re.escape(app.package)}\b.*(FATAL|Fatal signal)", crash):
        res.crashed = True
        m = re.search(r"^.*(FATAL EXCEPTION|Fatal signal|Exception|Error).*$", crash, re.M)
        if m:
            res.notes.append(m.group(0).split(": ", 1)[-1].strip()[:120])

    frame = work / "last.png"
    if last_frame(args.ffmpeg, record, frame):
        res.frame = frame
        res.frame_luma = mean_luma(args.ffmpeg, frame)
        if res.frame_luma is not None and res.frame_luma < 8:
            res.notes.append("last frame is nearly black")
    else:
        res.notes.append("no frame extracted")

    dev.adb("shell", "am", "force-stop", app.package, timeout=15)
    return res


# ---------------------------------------------------------------- report

def fmt_pct(v: float | None) -> str:
    return f"{v:.0f}%" if v is not None else "n/a"


def write_report(path: Path, results: list[Result], meta: dict[str, str]) -> None:
    yes_no = {True: "yes", False: "no"}
    lines = [
        "# droidrt compatibility matrix",
        "",
        *(f"- {k}: {v}" for k, v in meta.items()),
        "",
        "| App | Stack | Opened | Crashed | Frame | qemu CPU (avg / peak) | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        frame = "yes" if r.frame else "no"  # artifacts stay in the ignored out/ dir
        opened = yes_no[r.opened] if r.installed else "install failed"
        obs = "; ".join(r.notes) or "ok"
        lines.append(
            f"| {r.app.name} `{r.app.package}` {r.app.version} | {r.app.stack} | {opened} | "
            f"{yes_no[r.crashed]} | {frame} | {fmt_pct(r.cpu_avg)} / {fmt_pct(r.cpu_peak)} | "
            f"{obs.replace('|', '/')} |"
        )
    lines += ["", "## Manual checks", ""]
    lines += [f"- **{r.app.name}**: {r.app.test}" for r in results]
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- main

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the droidrt compatibility matrix against a running VM.")
    p.add_argument("--apps", type=Path, default=HERE / "apps.json", help="app list (default: compat/apps.json)")
    p.add_argument("--apk-dir", type=Path, help="override apk_dir from apps.json")
    p.add_argument("--out", type=Path, default=HERE / "out", help="per app artifacts (default: compat/out)")
    p.add_argument("--report", type=Path, default=HERE / "report.md", help="report path (default: compat/report.md)")
    p.add_argument("--serial", default=DEFAULT_SERIAL)
    p.add_argument("--avd", default=os.environ.get("DROIDRT_AVD", "droidrt"), help="AVD name used to find qemu")
    p.add_argument("--display", default=os.environ.get("DROIDRT_DISPLAY", DEFAULT_DISPLAY))
    p.add_argument("--seconds", type=int, default=20, help="time each app stays open (default: 20)")
    p.add_argument("--only", nargs="+", default=[], metavar="PKG", help="test only these packages")
    p.add_argument("--skip-hash", action="store_true", help="do not re-check the APK sha256")
    p.add_argument("--cleanup", action="store_true", help="uninstall the tested apps at the end")
    p.add_argument("--dry-run", action="store_true", help="print the plan without touching the VM")
    args = p.parse_args(argv)
    if args.seconds < 3:
        p.error("--seconds must be at least 3")
    return args


def dry_run(apps: list[App], args: argparse.Namespace) -> int:
    print(f"dry run: {len(apps)} app(s), serial {args.serial}, {args.seconds}s each, display {args.display}")
    print(f"check: adb -s {args.serial} get-state == device and sys.boot_completed == 1 (abort otherwise)")
    for i, app in enumerate(apps, 1):
        state = "ok" if app.file.exists() else "MISSING"
        rec = args.out / app.package / "screen.mp4"
        print(f"\n[{i}] {app.name} ({app.package} {app.version}) [{app.stack}]")
        print(f"    apk: {app.file} ({state})")
        print(f"    adb -s {args.serial} install -r -g {app.file}")
        print(f"    adb -s {args.serial} logcat -b crash -c")
        print(f"    {' '.join(scrcpy_cmd(args.serial, args.display, app.package, rec))}")
        print(f"    wait {args.seconds}s, adb shell pidof {app.package}, SIGTERM scrcpy")
        print(f"    ffmpeg last frame -> {rec.with_name('last.png')}")
        print(f"    adb logcat -b crash -d -> {rec.with_name('crash.log')}, am force-stop {app.package}")
        print(f"    expect: {app.test}")
    print(f"\nreport -> {args.report}")
    if args.cleanup:
        print("cleanup: adb uninstall " + " ".join(a.package for a in apps))
    return 0


def main(argv: list[str]) -> int:
    # The default SIGTERM action kills Python without running finally blocks,
    # which would leave scrcpy (in its own session) recording forever.
    signal.signal(signal.SIGTERM, _on_terminate)
    signal.signal(signal.SIGHUP, _on_terminate)
    args = parse_args(argv)
    apps = load_apps(args.apps, args.apk_dir, args.only)
    if args.dry_run:
        return dry_run(apps, args)

    args.ffmpeg = tool("ffmpeg")
    tool("scrcpy")
    dev = Device(args.serial)
    dev.require_ready()
    args.out.mkdir(parents=True, exist_ok=True)
    meta = {
        "Date": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Serial": args.serial,
        "Build": dev.shell("getprop", "ro.build.fingerprint") or "n/a",
        "Time per app": f"{args.seconds} s",
        "Display": args.display,
    }

    results: list[Result] = []
    try:
        for app in apps:
            try:
                results.append(test_app(dev, app, args, args.out))
            except subprocess.TimeoutExpired as e:
                r = Result(app)
                r.notes.append(f"timeout: {' '.join(map(str, e.cmd))[:80]}")
                results.append(r)
            write_report(args.report, results, meta)  # keep partial results on disk
    finally:
        if args.cleanup:
            for app in apps:
                log(f"{app.package}: uninstalling")
                dev.adb("uninstall", app.package, timeout=120)
    write_report(args.report, results, meta)
    log(f"report written to {args.report}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except MatrixError as e:
        log(str(e))
        sys.exit(1)
    except (KeyboardInterrupt, Terminated) as e:
        log(f"interrupted ({e or 'Ctrl-C'}); partial report kept")
        sys.exit(130)
