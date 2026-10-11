# droidrt device agent

`dev.droidrt.agent` is a small Android app that runs inside the droidrt VM and lets the host read and drive the screen. It has two services and no launcher activity:

- `AgentService`: an AccessibilityService that dumps the view tree of every window on every display (including the virtual displays droidrt opens per app), performs node actions, dispatches taps and swipes on a given display and fires global actions.
- `NotifyService`: a NotificationListenerService that reports active notifications plus the last 200 posted ones and triggers notification actions, filling RemoteInput for quick replies.

Both share one server on the abstract local socket `droidrt_agent`, speaking newline-delimited JSON. Every connection starts with a mutual authentication handshake based on a key the host delivers by broadcast. The protocol is specified in [`../docs/agent-protocol.md`](../docs/agent-protocol.md).

A third component, `KeyReceiver`, is a broadcast receiver that stores that key.

## Build

Requirements: a JDK (17 or newer) and the Android SDK with `platforms;android-34` and `build-tools;34.0.0`. No Gradle, no downloaded dependencies.

```sh
./build.sh    # writes build/droidrt-agent.apk
```

`build.sh` runs `javac`, `d8`, `aapt2 link`, `zipalign` and `apksigner`. The SDK root defaults to the Homebrew location and can be changed with `ANDROID_HOME` or `DROIDRT_SDK`; `JAVA_HOME`, `BUILD_TOOLS` and `PLATFORM` are honored too. On the first run it creates a local debug keystore at `build/debug.keystore`. The `build/` directory is ignored by git: never commit the keystore.

## Install and enable

The MCP server (`droidrt mcp`, see [`../docs/mcp.md`](../docs/mcp.md)) does all of this by itself on first use: it builds the APK when it is missing, installs it with `adb install -r -g`, adds `AgentService` to the enabled accessibility services while keeping the others, allows `NotifyService` as a notification listener and delivers its key. The key lives on the Mac in `~/Library/Application Support/droidrt/agent.key`, created with mode 0600, and is reused across runs.

To set the agent up by hand, for development:

```sh
adb install -r build/droidrt-agent.apk
adb shell settings put secure enabled_accessibility_services dev.droidrt.agent/.AgentService
adb shell settings put secure accessibility_enabled 1
adb shell cmd notification allow_listener dev.droidrt.agent/dev.droidrt.agent.NotifyService
KEY=$(openssl rand -hex 32)
adb shell am broadcast -a dev.droidrt.agent.SET_KEY -n dev.droidrt.agent/.KeyReceiver --es key "$KEY"
adb forward tcp:7912 localabstract:droidrt_agent
```

The broadcast prints `result=1` when the key was stored and `result=0` when it was rejected (it must be exactly 64 hex characters). Sending a new key replaces the old one; connections that are already authenticated stay open.

Note that `enabled_accessibility_services` is a colon separated list: the command above replaces any other enabled service. A key sent by hand replaces the MCP server's key; the server notices the failed handshake on its next connection and delivers its own key again.

Quick check, with `$KEY` from above:

```sh
python3 - "$KEY" <<'PY'
import hashlib, hmac, json, secrets, socket, sys
key = bytes.fromhex(sys.argv[1])
mac = lambda m: hmac.new(key, m.encode(), hashlib.sha256).hexdigest()
f = socket.create_connection(("127.0.0.1", 7912)).makefile("rwb")
hello = json.loads(f.readline())
nonce = secrets.token_hex(16)
f.write(json.dumps({"auth": mac("client:" + hello["nonce"]), "nonce": nonce}).encode() + b"\n")
f.flush()
reply = json.loads(f.readline())
assert hmac.compare_digest(reply["proof"], mac("agent:" + nonce)), "bad proof"
f.write(b'{"id":1,"cmd":"ping","args":{}}\n')
f.flush()
print(f.readline().decode().strip())
PY
```

`./test_agent.py` delivers a fresh key, checks the handshake and its failure modes, and exercises every command against a running VM with WhatsApp installed (it never accepts the WhatsApp terms). It leaves its own key in place, so the host has to deliver its key again afterwards.

## Security model

An accessibility service can read everything on screen and act as the user, so only the host may talk to it. Two independent checks enforce that.

### Peer credentials

The socket lives in the abstract namespace, which any app in the VM could connect to. The server checks the peer credentials of every connection (`LocalSocket.getPeerCredentials()`) and only serves uid 0 (root) and uid 2000 (shell). Connections that arrive through `adb forward` come from `adbd`, which runs as shell. Every other uid is disconnected before a byte is read, and the attempt is logged under the `droidrt-agent` tag.

This does not help on the host side: `adb forward` opens a TCP port on the Mac's loopback interface, and anything that can reach that port (any local process, and any web page through the browser, for example with a `text/plain` POST that needs no CORS preflight) reaches the agent as uid 2000.

### Mutual authentication

Every connection must therefore prove knowledge of a shared key before anything is processed:

- **Key.** 32 random bytes generated by the host, sent as 64 hex characters with `am broadcast` to `KeyReceiver`. The receiver is exported but requires `android.permission.DUMP`, which the adb shell holds and regular apps cannot obtain, so no app in the VM can set or rotate the key. The key is stored in the app's private SharedPreferences (`MODE_PRIVATE`). The HMAC key is the 32 decoded bytes.
- **Handshake.** On accept the agent sends a fresh 128-bit nonce from `SecureRandom`. The client's first line must carry `HMAC-SHA256(key, "client:" + agentNonce)` plus its own nonce; the agent compares in constant time (`MessageDigest.isEqual`) and answers with `HMAC-SHA256(key, "agent:" + clientNonce)`. Requests are accepted only after that.
- **Failure handling.** A first line that is not a well formed auth object (an HTTP request line, a plain request, garbage) closes the connection without a reply and without reading further. A wrong HMAC gets `{"ok":false,"error":"auth failed"}`, and a missing key gets `{"ok":false,"error":"no key"}`; both close the connection.
- **Limits.** The handshake must complete within 10 seconds. Any line, before or after authentication, is capped at 1 MiB; a longer line closes the connection.

What this protects against:

- **Web pages and other host processes** cannot issue commands: they do not know the key, and an HTTP request is dropped at its first line, before the body is ever read.
- **A malicious app in the VM that grabs `droidrt_agent` first** (for example while the agent is restarting) can accept the host's connection, but it cannot produce the `proof`, so a host that verifies the proof aborts before it sends typed text or trusts any tree. Fresh nonces on both sides prevent replaying a recorded handshake in either direction.

What it does not cover: messages after the handshake are not encrypted or individually signed. The transport is a loopback port plus the adb channel, so this is acceptable for an attacker that can only open connections, not one that can read or alter adb traffic.

Accepted risk: the key travels on the `adb shell am broadcast` command line, so it is visible in the Mac's process list for the few milliseconds that command runs. On a single user Mac, any process that could read it there runs as the same user and could read `agent.key` anyway.

### Other measures

- The app requests no permissions at all, not even `INTERNET`. Nothing listens on a TCP port inside the VM; the host port exists only on the Mac, created by `adb forward`, bound to localhost.
- Both services are exported only to the system, guarded by `BIND_ACCESSIBILITY_SERVICE` and `BIND_NOTIFICATION_LISTENER_SERVICE`, and they stay off until the host enables them through adb.

Anyone with adb access to the VM already has full control over it, so the agent does not widen that boundary.
