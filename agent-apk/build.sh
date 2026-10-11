#!/bin/sh
# Builds build/droidrt-agent.apk with javac, d8, aapt2, zipalign and apksigner.
# No Gradle and no downloaded dependencies.
#
# Environment overrides:
#   ANDROID_HOME / DROIDRT_SDK   Android SDK root
#   BUILD_TOOLS                  build-tools version (default 34.0.0)
#   PLATFORM                     compile platform (default android-34)
#   JAVA_HOME                    JDK used for javac, keytool and apksigner
set -eu
# pipefail is POSIX since 2024 but older dash builds lack it; use it when present.
# shellcheck disable=SC3040
if (set -o pipefail) 2>/dev/null; then set -o pipefail; fi

cd "$(dirname "$0")"
ROOT=$(pwd)

SDK=${ANDROID_HOME:-${DROIDRT_SDK:-/opt/homebrew/share/android-commandlinetools}}
BUILD_TOOLS=${BUILD_TOOLS:-34.0.0}
PLATFORM=${PLATFORM:-android-34}
BT="$SDK/build-tools/$BUILD_TOOLS"
ANDROID_JAR="$SDK/platforms/$PLATFORM/android.jar"

if [ -z "${JAVA_HOME:-}" ]; then
    if [ -x /opt/homebrew/opt/openjdk/bin/javac ]; then
        JAVA_HOME=/opt/homebrew/opt/openjdk
    elif [ -x /usr/libexec/java_home ]; then
        JAVA_HOME=$(/usr/libexec/java_home)
    fi
fi
if [ -n "${JAVA_HOME:-}" ]; then
    export JAVA_HOME
    PATH="$JAVA_HOME/bin:$PATH"
    export PATH
fi

for f in "$BT/d8" "$BT/aapt2" "$BT/zipalign" "$BT/apksigner" "$ANDROID_JAR"; do
    [ -e "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
for t in javac keytool zip; do
    command -v "$t" >/dev/null || { echo "$t not found (set JAVA_HOME, install zip)" >&2; exit 1; }
done

MIN_SDK=30
TARGET_SDK=34
VERSION_CODE=1
VERSION_NAME=1.0

OUT="$ROOT/build"
TMP="$OUT/tmp"
APK="$OUT/droidrt-agent.apk"
KEYSTORE="$OUT/debug.keystore"
# Fixed timestamp for every file that goes into the archive.
EPOCH=198001010000

rm -rf "$TMP"
mkdir -p "$TMP/classes" "$TMP/dex" "$TMP/res"

echo "javac"
find src -name '*.java' | LC_ALL=C sort > "$TMP/sources.txt"
javac --release 17 -encoding UTF-8 -Xlint:-options -nowarn \
    -classpath "$ANDROID_JAR" -d "$TMP/classes" @"$TMP/sources.txt"

echo "d8"
find "$TMP/classes" -name '*.class' | LC_ALL=C sort > "$TMP/classes.txt"
"$BT/d8" --release --min-api "$MIN_SDK" --lib "$ANDROID_JAR" \
    --output "$TMP/dex" @"$TMP/classes.txt"

echo "aapt2"
"$BT/aapt2" compile --dir res -o "$TMP/res"
"$BT/aapt2" link -o "$TMP/unaligned.apk" \
    -I "$ANDROID_JAR" \
    --manifest AndroidManifest.xml \
    --min-sdk-version "$MIN_SDK" --target-sdk-version "$TARGET_SDK" \
    --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
    "$TMP"/res/*.flat

touch -t "$EPOCH" "$TMP/dex/classes.dex"
(cd "$TMP/dex" && zip -q -X -D "$TMP/unaligned.apk" classes.dex)

echo "zipalign"
"$BT/zipalign" -f -p 4 "$TMP/unaligned.apk" "$TMP/aligned.apk"

if [ ! -f "$KEYSTORE" ]; then
    echo "keytool (new local debug keystore)"
    # Kept only under build/ (git ignored). Write to a temporary name so an
    # interrupted run never leaves a half written keystore behind.
    rm -f "$KEYSTORE.tmp"
    if ! keytool -genkeypair -noprompt -keystore "$KEYSTORE.tmp" -storetype PKCS12 \
        -storepass android -keypass android -alias droidrtdebug \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=droidrt debug" >"$TMP/keytool.log" 2>&1; then
        cat "$TMP/keytool.log" >&2
        rm -f "$KEYSTORE.tmp"
        exit 1
    fi
    mv "$KEYSTORE.tmp" "$KEYSTORE"
fi

echo "apksigner"
"$BT/apksigner" -J-enable-native-access=ALL-UNNAMED sign --ks "$KEYSTORE" --ks-pass pass:android \
    --ks-key-alias droidrtdebug --key-pass pass:android \
    --out "$APK" "$TMP/aligned.apk"
"$BT/apksigner" -J-enable-native-access=ALL-UNNAMED verify "$APK"
rm -f "$APK.idsig"
rm -rf "$TMP"

echo "built $APK"
