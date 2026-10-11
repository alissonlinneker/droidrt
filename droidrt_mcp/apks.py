"""Local cache of base APKs pulled from the VM, used to derive manifest tools.

Only the base APK is pulled (the manifest lives there). Files are kept under
`<cache>/<package>/<versionCode>/base.apk`; older versions are removed.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from .agent import Adb, DeviceError
from .validate import PACKAGE_RE

log = logging.getLogger("droidrt_mcp.apks")

VERSION_RE = re.compile(r"versionCode=(\d+)")


class ApkCache:
    def __init__(self, adb: Adb, root: Path):
        self.adb = adb
        self.root = root
        # The startup reload and the reload after record_flow can ask for the
        # same package at once; one pull must not delete the other's temp dir.
        self._lock = threading.Lock()

    @staticmethod
    def _usable(apk: Path) -> bool:
        """A regular file that is a complete zip (a damaged copy is pulled again)."""
        try:
            return apk.is_file() and not apk.is_symlink() and zipfile.is_zipfile(apk)
        except OSError:
            return False

    def _cached(self, pkg: str) -> list[Path] | None:
        folder = self.root / pkg
        if not folder.is_dir():
            return None
        versions = sorted((p for p in folder.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))
        for v in reversed(versions):
            apk = v / "base.apk"
            if self._usable(apk):
                return [apk]
        return None

    def base_apks(self, pkg: str) -> list[Path] | None:
        """Base APK of `pkg`, pulled once per versionCode. Falls back to the newest
        cached copy when the VM is off, and to None when nothing is available."""
        if not PACKAGE_RE.fullmatch(pkg):
            return None
        with self._lock:
            return self._base_apks(pkg)

    def _base_apks(self, pkg: str) -> list[Path] | None:
        if not self.adb.is_ready():
            return self._cached(pkg)
        try:
            m = VERSION_RE.search(self.adb.shell("dumpsys", "package", pkg, timeout=20, check=False))
            if not m:
                return self._cached(pkg)
            target = self.root / pkg / m.group(1) / "base.apk"
            if self._usable(target):
                return [target]
            paths = [l.split(":", 1)[1].strip() for l in self.adb.shell("pm", "path", pkg, timeout=15).splitlines()
                     if l.startswith("package:")]
            remote = next((p for p in paths if p.endswith("/base.apk")), paths[0] if paths else None)
            if not remote:
                return self._cached(pkg)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent.parent) as tmp:
                local = Path(tmp) / "base.apk"
                self.adb.run("pull", remote, str(local), timeout=600)
                if not zipfile.is_zipfile(local):
                    raise DeviceError(f"the pulled APK of {pkg} is not a valid zip")
                local.replace(target)
            for old in (self.root / pkg).iterdir():
                if old.is_dir() and old.name != m.group(1):
                    shutil.rmtree(old, ignore_errors=True)
            return [target]
        except (DeviceError, OSError) as e:
            log.info("cannot pull the APK of %s: %s", pkg, e)
            return self._cached(pkg)
