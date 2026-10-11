# Compatibility

droidrt runs a stock Android 17 (API 37) arm64 system image with Google Play. Anything that runs on that image runs in droidrt; the limits come from the hardware and from what the emulator can emulate, not from the app framework.

## What works

Apps work when their native code is `arm64-v8a`, or when they have no native code at all. The UI stack does not matter:

- Kotlin and Java with Android views
- Jetpack Compose
- Flutter (Impeller or Skia)
- React Native (Hermes)
- WebView based apps
- OpenGL ES games and C++ engines
- GeckoView and other embedded browser engines

Rendering uses the Mac GPU through Metal. If an app shows graphical artifacts, try `DROIDRT_GPU=software` (CPU rendering, GLES 3.1).

## Test matrix

[`compat/run_matrix.py`](../compat/run_matrix.py) tests a set of open source apps from the F-Droid main repository, listed in [`compat/apps.json`](../compat/apps.json) with their download URL, version and sha256. For each app it installs the APK on a running VM, opens it on a fresh virtual display while recording, keeps the last frame, checks the crash buffer and whether the process is alive, and samples the qemu CPU usage. Results are written to [`compat/report.md`](../compat/report.md), together with what to verify by hand for each app (camera preview, audio, GPS, video playback).

| App | Stack |
|---|---|
| Breezy Weather | Kotlin + Jetpack Compose |
| LocalSend | Flutter |
| Jitsi Meet | React Native (Hermes) |
| CanIWebView | Android System WebView |
| Shattered Pixel Dungeon | libGDX game on OpenGL ES |
| Binary Eye | Kotlin views, Camera API, native ZXing-C++ |
| VLC | Kotlin + native libVLC |
| Fennec F-Droid | GeckoView + Compose |
| Organic Maps | C++ map engine on OpenGL ES/Vulkan, GPS |
| AntennaPod | Java/Kotlin views + Media3/ExoPlayer |

### Results

The run saved in `compat/report.md` (2026-10-11) used the Android 17 image, the Mac GPU (`-gpu host`) and a 720x1560/320 display. **All ten apps opened, none crashed**, and every app drew a frame. Average qemu CPU stayed between 48% and 98% while each app was open.

scrcpy sometimes loses the race with its own server inside the VM and exits within seconds with `Server connection failed`, before a frame is captured. That is a failure of the test harness, not of the app, so the runner retries an app up to three times when scrcpy fails this way. `droidrt launch` retries quick window failures in the same way.

The automated check only proves that the app starts, stays alive and draws a frame. Camera, audio, GPS and media playback still need the manual checks listed in the report.

### Running it

The runner never downloads anything and never starts or stops the VM. Download the APKs listed in `compat/apps.json` from F-Droid into `~/Library/Caches/droidrt/compat-apks/` (the `apk_dir` in `apps.json`, or pass `--apk-dir`), keeping the file names from the `file` field. Each file is checked against its sha256 before it is installed; a missing file or a mismatch is reported and skipped.

```sh
droidrt start
./compat/run_matrix.py --dry-run                 # show the plan, touch nothing
./compat/run_matrix.py                           # all apps, 20 s each by default
./compat/run_matrix.py --only org.videolan.vlc --seconds 30
./compat/run_matrix.py --cleanup                 # uninstall the tested apps at the end
```

Per app artifacts (recording, last frame, logs) go to `compat/out/`, which is ignored by git. APKs are never committed to the repository.

## Known limits

These cannot be solved by droidrt or by any other Android setup on a Mac:

| Case | Reason | What you see |
|---|---|---|
| Only 32-bit ARM code (`armeabi-v7a`) | Apple Silicon cannot execute AArch32, and the arm64 image has no translation layer | `droidrt install` refuses the app with a clear message |
| Only x86 or x86_64 code | The arm64 images have no x86 to ARM translation | Install refused or the app fails to load its libraries |
| Play Integrity `DEVICE` or `STRONG` | The emulator has no hardware backed attestation, so Google returns an empty verdict | Banking apps and games with anti-cheat refuse to run or to log in |
| DRM video | Without a TEE only Widevine L3 exists | Streaming is blocked or limited to SD |
| `FLAG_SECURE` screens | Secure surfaces are not captured by the virtual display | The protected screen appears black |
| NFC, real Bluetooth, telephony, SMS | No equivalent hardware is passed through to the VM | Features are missing or report no hardware |
| Legacy encrypted `.apkm` | The old format is encrypted | Convert with `unapkm` or download the current format |

## Hardware

| Feature | Status |
|---|---|
| Camera | The built-in Mac camera is the Android back camera. Pick another with `DROIDRT_CAMERA`. |
| Audio output | Played by the emulator through CoreAudio. `DROIDRT_AUDIO=0` disables it. |
| Microphone | Off by default. `DROIDRT_MIC=1` passes the Mac microphone through. |
| Keyboard | Physical keyboard and IME per window. |
| Clipboard | Shared both ways. |
| Location | Set with `droidrt geo <lat> <lon>`. The fix reaches the `gps` and `fused` providers, so apps with an active request (checked with Organic Maps) follow it. |
| Window size | Fixed by default. `DROIDRT_RESIZABLE=1` makes windows resizable (scrcpy flex display): the Android display follows the window, sized in points, so the video is softer on Retina screens. |
| Files | Drag a file onto a window to copy it to `/sdcard/Download`; drag an APK to install it. |

## Apps that need a phone number

Messaging apps tied to a phone number usually allow only one primary device. Prefer companion or linked device modes when the app offers them. For WhatsApp, choose **Link as companion device** on the welcome screen and scan the QR code from your phone, instead of registering the number in the VM, which would log WhatsApp out of your phone.

## Reporting

Tried an app? Please share the result, good or bad, with the [compatibility report form](https://github.com/alissonlinneker/droidrt/issues/new?template=compat_report.yml). Never attach APKs.
