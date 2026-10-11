# droidrt compatibility matrix

- Date: 2026-10-11 14:33
- Serial: emulator-5580
- Build: google/sdk_gphone64_arm64/emu64a:17/CE2A.260420.019/15611780:user/release-keys
- Time per app: 25 s
- Display: 720x1560/320

| App | Stack | Opened | Crashed | Frame | qemu CPU (avg / peak) | Notes |
|---|---|---|---|---|---|---|
| Breezy Weather `org.breezyweather` 6.2.2_freenet | Kotlin + Jetpack Compose | yes | no | yes | 64% / 129% | ok |
| LocalSend `org.localsend.localsend_app` 1.18.2 | Flutter | yes | no | yes | 95% / 211% | ok |
| Jitsi Meet `org.jitsi.meet` 26.0.0 | React Native (Hermes) | yes | no | yes | 98% / 200% | ok |
| CanIWebView `com.caniwebview.android` 1.1.0 | Android System WebView (web content) | yes | no | yes | 64% / 117% | ok |
| Shattered Pixel Dungeon `com.shatteredpixel.shatteredpixeldungeon` 4.0.1 | Game, libGDX on OpenGL ES | yes | no | yes | 55% / 110% | ok |
| Binary Eye `de.markusfisch.android.binaryeye` 1.75.4 | Kotlin views + Camera API + native ZXing-C++ | yes | no | yes | 68% / 160% | ok |
| VLC `org.videolan.vlc` 3.7.1 | Kotlin + native libVLC (audio/video decoding) | yes | no | yes | 49% / 88% | ok |
| Fennec F-Droid `org.mozilla.fennec_fdroid` 157.0.0 | Browser, GeckoView (native Gecko) + Compose | yes | no | yes | 63% / 123% | ok |
| Organic Maps `app.organicmaps` 2026.09.29-32-FDroid | C++ map engine on OpenGL ES/Vulkan + Java UI, GPS | yes | no | yes | 58% / 238% | ok |
| AntennaPod `de.danoeh.antennapod` 3.12.2 | Java/Kotlin views + Media3/ExoPlayer | yes | no | yes | 48% / 166% | ok |

## Manual checks

- **Breezy Weather**: Compose UI renders (onboarding or main screen), animations are smooth, location permission prompt works
- **LocalSend**: Flutter engine starts (Impeller or Skia), first frame is not black, local network discovery screen shows
- **Jitsi Meet**: React Native bundle loads, welcome screen renders, camera preview in a test meeting uses the Mac camera
- **CanIWebView**: WebView provider loads, web page renders with GPU compositing, scrolling works
- **Shattered Pixel Dungeon**: GLES context is created, title screen renders with animation, frame rate is stable, music plays through the Mac speakers
- **Binary Eye**: Camera preview shows the Mac webcam image, a QR code held in front of the camera is decoded
- **VLC**: Media library opens, a pushed H.264 video plays with picture, audio plays through the Mac speakers
- **Fennec F-Droid**: Gecko content process starts (multi-process), a web page renders, video tag playback works
- **Organic Maps**: Map renders (world map is bundled), pan and zoom work, emulated GPS location (adb emu geo fix) moves the marker
- **AntennaPod**: Home screen renders, streaming an episode plays through the Mac speakers, media notification shows
