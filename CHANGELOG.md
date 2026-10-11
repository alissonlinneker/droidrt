# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-10-11

### Added

- `DROIDRT_RESIZABLE=1` opens resizable windows that resize the Android display (scrcpy flex display with a point sized display spec).
- `record_flow` takes `overwrite` (default `false`) and writes flows atomically.
- Support section and GitHub Sponsors link.

### Fixed

- Concurrent launches no longer boot two VMs, and `idle-stop` no longer powers the VM off during another launch (file locks for boot, busy windows and per package launches).
- Quitting a launcher during boot no longer leaves an orphan window or a VM that never stops.
- A launcher name taken by an app that droidrt did not create is never overwritten; bundles are built in staging and moved into place.
- `sync` no longer removes every launcher when the package manager returns a short list right after boot, and `uninstall` keeps the launcher when the uninstall fails.
- Switching images downloads the new image before recreating the AVD; every subprocess has a timeout.
- iconkit resolves resource references written by name (`@color/name`), so adaptive icon backgrounds render again (Organic Maps, Fennec), and it never raises on malformed input.
- The icon compositor no longer crashes on out of range or non finite numbers.
- MCP: `notifications/cancelled` is honored, duplicate in flight ids are rejected, shutdown no longer waits for slow calls or leaves scrcpy sessions and adb forwards behind, and semantic reloads no longer race each other.
- The on-device agent survives unexpected errors in a connection and recovers its accept loop.

### Security

- MCP: element ids are checked against the windows of the requested package, so a guessed id cannot act on a blocked app; `press_key` is refused when the app has no droidrt display.
- Generated `open_url` tools take an address and always use `https://`; intent templates only accept placeholders in `data` and extras, after a literal safe scheme.
- Package names from untrusted manifests are validated before they reach file paths or `adb shell`.
- The on-device agent caps pending handshakes (8) and connections (32), applies a total 10 s handshake deadline and limits the auth line to 4 KiB.
- iconkit caps extracted bitmaps, XML dumps and aapt2 output size, and builds its compositor atomically under a lock.

## [0.1.0] - 2026-10-11

First public release.

### Added

- `droidrt` CLI that runs Android apps on Apple Silicon Macs through a headless ARM64 Android 17 (API 37) VM with Google Play, on Hypervisor.framework with Metal GPU rendering. Commands: `setup`, `start`, `stop`, `status`, `install`, `launch`, `store`, `sync`, `list`, `uninstall`, `geo` and `mcp`.
- One macOS window per app through scrcpy virtual displays, with local IME, UHID keyboard, shared clipboard and drag and drop (APKs are installed, other files go to `/sdcard/Download`). Quick scrcpy failures are retried.
- Generated launcher `.app` bundles in `~/Applications/Android`, backed by a native AppKit launcher that shows the app in the Dock, brings its window to the front and quits it with Cmd-Q.
- `iconkit`: launcher icon resolution from APKs (adaptive icons, vector drawables, layer lists, insets, shapes, selectors) rendered into 1024 px macOS icons by a CoreGraphics compositor.
- Installation of `.apk`, `.xapk`, `.apks` and `.apkm`, with an upfront check that refuses apps with only 32-bit ARM or x86 code.
- Play Store integration: launchers are synced automatically when the store window closes (`droidrt store`, `droidrt sync`).
- Automatic VM lifecycle: boot on first launch, shutdown after `DROIDRT_IDLE_GRACE` seconds without windows.
- Camera passthrough, CoreAudio output, optional microphone (`DROIDRT_MIC=1`) and GPS location (`droidrt geo`).
- MCP server (`droidrt mcp` or `python3 -m droidrt_mcp`) over stdio, standard library only:
  - generic tools `list_apps`, `open_app`, `close_app`, `get_screen` (accessibility outline plus screenshot), `find_elements`, `tap`, `type_text`, `swipe`, `press_key`, `wait_for`, `get_notifications`, `reply_notification`, `run_intent` and `record_flow`;
  - resources `droidrt://apps`, `droidrt://notifications` and the template `droidrt://notifications/{package}`;
  - headless automation by default: a virtual display without a window (`scrcpy --no-window`), with the app started through `am start --display`;
  - per-app semantic tools from curated definitions, recorded flows and the app manifest (deep links, exported activities, share targets), loaded in the background and announced with `notifications/tools/list_changed`;
  - curated WhatsApp tools: `whatsapp_open_chat`, `whatsapp_draft_message`, `whatsapp_send_message` and `whatsapp_share_text`;
  - recorded flows saved in `~/Library/Application Support/droidrt/flows/<package>/`;
  - `DROIDRT_SERIAL`, `DROIDRT_MCP_LOG` and `DROIDRT_MCP_PACKAGES` settings.
- On-device agent (`agent-apk/`, package `dev.droidrt.agent`): an AccessibilityService and a NotificationListenerService behind one abstract local socket, built with plain `javac`, `d8` and `aapt2`, no Gradle. The MCP server builds, installs and enables it on first use.
- Compatibility matrix runner (`compat/run_matrix.py`) with ten open source apps from F-Droid across Compose, Flutter, React Native, WebView, GeckoView, OpenGL ES and native engines, verified by sha256, with retries for scrcpy harness failures.
- Open source packaging: MIT license, contributing guide, security policy, code of conduct, issue and pull request templates, CI on Apple Silicon runners and a Makefile (`make build`, `make test`, `make install`).

### Security

- Mutual HMAC-SHA256 handshake between the host and the device agent, with fresh nonces on both sides and a 32 byte key kept in `~/Library/Application Support/droidrt/agent.key` (mode 0600). The agent also checks peer credentials (uid 2000 or 0), caps lines at 1 MiB and drops connections that do not authenticate within 10 seconds.
- MCP input validation for every tool, intent components restricted to the target package, recorded flows confined to their app, a default block on sensitive system packages, an optional package allowlist, caps on batches and notification results, read only and destructive tool annotations, and home directories redacted from errors.
- Size limits when unpacking bundles (4 GiB in total, 2 GiB per entry), path traversal checks, icon size and pixel limits, and depth and node budgets in `iconkit`.
- No `shell=True` anywhere; every subprocess receives an argument list.

[Unreleased]: https://github.com/alissonlinneker/droidrt/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/alissonlinneker/droidrt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/alissonlinneker/droidrt/releases/tag/v0.1.0
