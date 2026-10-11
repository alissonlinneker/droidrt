# MCP server

`droidrt_mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server that lets MCP clients operate the Android apps installed in droidrt. It runs on the Mac, speaks MCP (protocol revision `2025-06-18`) over stdio, and drives the VM through adb and through the on-device agent (`dev.droidrt.agent`).

By default every app is driven **headless**: the server creates an Android virtual display with no macOS window, starts the app on it and reads and acts on that display only. Automation never fights with the windows you are using, and an app you already have open in a droidrt window is driven in place.

## Contents

- [Requirements](#requirements)
- [Registering the server](#registering-the-server)
- [Environment variables](#environment-variables)
- [How it works](#how-it-works)
- [Generic tools](#generic-tools)
- [What get_screen returns](#what-get_screen-returns)
- [Resources](#resources)
- [Semantic tools](#semantic-tools)
- [Recording flows](#recording-flows)
- [Example: WhatsApp](#example-whatsapp)
- [Responsible use](#responsible-use)
- [Security](#security)

## Requirements

- A working droidrt installation (see the [README](../README.md)).
- The device agent APK. `make build` builds it into `agent-apk/build/droidrt-agent.apk` (it needs a JDK, `build-tools;34.0.0` and `platforms;android-34`). When the APK is missing, the server runs `agent-apk/build.sh` itself on first use.
- Python 3.10 or newer. The server uses only the standard library.

The server sets up the agent on first use: it installs the APK (`adb install -r -g`), adds its accessibility service to the enabled ones (other enabled services are kept), allows its notification listener and delivers the shared key (see [Security](#security)). An agent left over from an older checkout is upgraded once from the local build.

## Registering the server

After `make install`, the server starts with `droidrt mcp`. Any MCP client that reads a JSON configuration can use:

```json
{
  "mcpServers": {
    "droidrt": {
      "command": "droidrt",
      "args": ["mcp"]
    }
  }
}
```

Without `make install`, run the package from the clone:

```json
{
  "mcpServers": {
    "droidrt": {
      "command": "python3",
      "args": ["-m", "droidrt_mcp"],
      "env": {"PYTHONPATH": "/path/to/droidrt"}
    }
  }
}
```

`./droidrt.py mcp` from the clone works as well. The server boots the VM on demand, the same way a launcher does, and logs to stderr only, so stdout carries nothing but the protocol.

To restrict the server to the apps you actually want automated, add an allowlist (recommended, see [Prompt injection](#prompt-injection)):

```json
"env": {"DROIDRT_MCP_PACKAGES": "com.whatsapp"}
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DROIDRT_MCP_PACKAGES` | unset (all apps except the blocked system packages) | Comma separated allowlist of packages the server may open, read and act on. When set, only the listed packages are allowed, and it is the only way to allow a blocked system package. Entries that are not valid package names are ignored, so a value with no valid entry allows nothing. Unset or empty means every app except the blocked ones. |
| `DROIDRT_SERIAL` | `emulator-<DROIDRT_PORT>` (`emulator-5580`) | adb serial of the device to drive |
| `DROIDRT_MCP_LOG` | `INFO` | log level written to stderr (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

The droidrt variables from the [README](../README.md#environment-variables) apply too, in particular `DROIDRT_PORT`, `DROIDRT_SDK` and `DROIDRT_DISPLAY` (the size of each headless display).

## How it works

```
MCP client ──stdio──► droidrt_mcp ──adb──────────────────────────► VM (install, am start --display, screencap)
                          │
                          ├──scrcpy --no-window --new-display ────► headless virtual display per app
                          │
                          └──adb forward tcp:<free port> → localabstract:droidrt_agent
                                                        │  (mutual HMAC handshake)
                                                        ▼
                                          dev.droidrt.agent (accessibility tree,
                                          actions, gestures, notifications)
```

- **Headless sessions.** `open_app` starts `scrcpy --no-window --no-control --new-display=<DROIDRT_DISPLAY>` with a 1 fps video stream recorded to `/dev/null` (scrcpy needs a video sink to keep a virtual display), reads the new display id, then starts the app's launcher activity on it with `am start -W --display <id>`. Quick scrcpy failures are retried up to three times. Sessions are stopped with SIGTERM on `close_app` and when the server exits; their state is kept in `~/Library/Application Support/droidrt/mcp-sessions.json`, so a session that outlives a crashed server is adopted by the next one. scrcpy output goes to `~/Library/Logs/droidrt/mcp-<package>.log`.
- **Windows.** If the app already has a droidrt window, its display is reused. `open_app` with `headless: false` opens a regular window instead.
- **Screen reading** uses the accessibility tree from the agent, so results are structured (text, resource id, content description, bounds, state). `get_screen` also attaches a PNG screenshot of the app's display, scaled to at most 1024 px, unless `include_screenshot` is `false`.
- **Element ids** such as `w12:0.3.1` come from `get_screen` and `find_elements`. They stay valid until the window content changes; after that, read the screen again.
- **One call at a time.** Tool calls and resource reads are serialized on the device.

## Generic tools

These work with any app. Every `package` argument must match `^[a-zA-Z][\w.]*$`, and unknown arguments are rejected.

| Tool | What it does | Arguments |
|---|---|---|
| `list_apps` | Lists installed apps that have a launcher and that the server may access: package, label, whether a droidrt window is open and whether a headless session runs | none |
| `open_app` | Opens an app so it can be driven and returns its display id. Reuses an existing window, otherwise creates a headless display | `package`, `headless` (boolean, default `true`) |
| `close_app` | Closes the app's headless session or droidrt window | `package` |
| `get_screen` | Outline of the accessibility tree with element ids, plus a PNG screenshot. Coordinates are display pixels | `package`, `include_screenshot` (default `true`), `max_depth` (1 to 100) |
| `find_elements` | Finds elements. `text` is exact and case sensitive, `text_contains` is case insensitive, `resource_id` takes the short (`send`) or full (`com.app:id/send`) form. At least one filter is required | `package`, `text`, `text_contains`, `resource_id`, `content_desc`, `class_name`, `clickable`, `limit` (1 to 100, default 30) |
| `tap` | Taps one target: an element id, a text (exact, then contains, then content description), a resource id, or the point `x`,`y` | `package` and exactly one of `element_id`, `text`, `resource_id`, or `x` with `y` |
| `type_text` | Replaces the text of an editable field (the focused or first editable one when `element_id` is omitted). `submit: true` sends the IME action afterwards | `package`, `text` (up to 4096 characters), `element_id`, `submit` (default `false`) |
| `swipe` | Swipes on the app's display. The direction is where the finger moves: `up` scrolls the content down | `package`, `direction` (`up`, `down`, `left`, `right`) |
| `press_key` | Presses a key on the app's own display (refused when the app has no droidrt display, so a key never reaches another app) | `package`, `key` (`back`, `enter`, `home`) |
| `wait_for` | Waits until an element appears, matched by text (case insensitive contains) or resource id | `package`, exactly one of `text` or `resource_id`, `timeout_ms` (100 to 120000, default 10000) |
| `get_notifications` | Lists active and recently posted notifications of allowed packages, optionally for one package. Returns `{"count", "notifications", "truncated"}` (see below) | `package` (optional) |
| `reply_notification` | Replies through the quick reply (RemoteInput) action of a notification | `key` (up to 512 characters), `text` (up to 4096 characters) |
| `run_intent` | Starts an activity with an intent (`am start`) on the app's display, opening the app headless first when needed. The intent always targets `package`: a `component` must belong to it | `package`, `action`, `data`, `extras` (string, number or boolean values), `mime_type`, `component`, `categories` |
| `record_flow` | Saves a reusable flow and exposes it as a new tool named `<app>_<name>` | `package`, `name` (`^[a-z][a-z0-9_]{0,47}$`), `description` (up to 1024 characters), `steps` (1 to 200), `input_schema` (optional), `overwrite` (default `false`) |

`get_notifications` returns `count` (the number of matching notifications), `notifications` (at most 50 entries, about 100 KB in total, every text field cut at 2000 characters) and `truncated: true` when something was left out. Each notification has a `key`, its package, title, text and `actions`; an action with `remoteInput` is a quick reply.

Every tool carries MCP annotations, so clients can decide what needs confirmation. They are hints for the client, not a security boundary:

| Annotation | Tools |
|---|---|
| `readOnlyHint` | `list_apps`, `get_screen`, `find_elements`, `wait_for`, `get_notifications` |
| `destructiveHint` | `tap`, `type_text`, `press_key`, `reply_notification`, `run_intent`, `record_flow`, and semantic tools that run a flow or send or share (for example `whatsapp_send_message` and `whatsapp_share_text`) |
| neither | `open_app`, `close_app`, `swipe`, and semantic tools that only open a screen, a link or a draft (for example `whatsapp_open_chat` and `whatsapp_draft_message`) |

## What get_screen returns

`get_screen` returns a compact accessibility tree, with element ids that `tap`, `type_text` and `find_elements` accept, plus a screenshot scaled to at most 1024 px. This is a real response for Breezy Weather running headless:

<p align="center"><img src="assets/screenshots/mcp-get-screen.png" width="280" alt="Screenshot returned by get_screen for Breezy Weather running headless"></p>

```text
package org.breezyweather, display 5 (720x1560); screenshot 472x1024, multiply screenshot coordinates by 1.5254 to get display pixels
window w24:0 pkg=org.breezyweather display=5 @0,0,720,1560
  [w24:0.0.0.0] LinearLayout id=action_bar_root @0,0,720,1560
    [w24:0.0.0.0.0] FrameLayout id=content @0,0,720,1560
      [w24:0.0.0.0.0.0] ViewGroup id=container @0,0,720,1560
        [w24:0.0.0.0.0.0.3] FrameLayout id=fragment @0,0,720,1560
          [w24:0.0.0.0.0.0.3.0] ViewGroup id=fragment_root @0,0,720,1560
            [w24:0.0.0.0.0.0.3.0.1.0] LinearLayout id=appBar @0,0,720,128
              [w24:0.0.0.0.0.0.3.0.1.0.0] ViewGroup id=toolbar @0,0,720,128
                [w24:0.0.0.0.0.0.3.0.1.0.0.0] ImageButton desc="Location list" {clickable} @0,8,112,120
                [w24:0.0.0.0.0.0.3.0.1.0.0.1] TextView "Paris" @120,37,215,91
                [w24:0.0.0.0.0.0.3.0.1.0.0.2.0] Button desc="Edit this location" id=action_edit {clickable} @528,16,624,112
...
```

The full tree of this example is in [assets/screenshots/mcp-get-screen.txt](assets/screenshots/mcp-get-screen.txt).

## Resources

| URI | Content |
|---|---|
| `droidrt://apps` | Installed apps with a launcher and their window state (JSON, same data as `list_apps`, blocked packages left out) |
| `droidrt://notifications` | Active and recently posted notifications of allowed packages (JSON) |
| `droidrt://notifications/{package}` | Notifications of one package (resource template) |

## Semantic tools

On top of the generic tools, the server exposes per-app tools built by `semantic/`. Their names start with a short app slug and match `^[a-z0-9_]{1,64}$`. Sources, in order of precedence:

1. **Curated definitions** shipped in [`semantic/curated/<package>.json`](../semantic/curated/).
2. **Recorded flows** saved with `record_flow`.
3. **Generated tools** derived from the app manifest (up to 15 per app): browsable deep links, exported activities with intent filters and `ACTION_SEND` share targets. The server pulls the base APK of each app once per version into `~/Library/Caches/droidrt/apks/<package>/<versionCode>/` to read its manifest.

Generated link tools keep the scheme and host fixed: for example a browser's `open_url` tool takes an `address` without a scheme and always opens it over `https://`, so an argument can never switch to `javascript:`, `intent:` or another host.

When two sources produce the same name, the higher precedence wins, and generic tool names always win over semantic ones. With the VM running and the semantic tools loaded, a typical installation exposes about a hundred tools: the 14 generic ones plus the semantic tools of every installed app. Use `DROIDRT_MCP_PACKAGES` to keep the list short.

Semantic tools load in the background when the server starts, so the first `tools/list` answers right away with the generic tools. When the semantic tools are ready, and again whenever `record_flow` saves a flow, the server sends `notifications/tools/list_changed` and clients fetch the new list. Packages considered are the apps installed in the VM (when it is running), the apps with a launcher, the apps with curated definitions and the apps with recorded flows.

Each semantic tool runs either an **intent** (for example a deep link opened with `am start` on the app's display) or a **flow** of steps. The format is specified in [agent-protocol.md](agent-protocol.md#2-semantic-tool-definitions).

## Recording flows

`record_flow` writes `~/Library/Application Support/droidrt/flows/<package>/<name>.json` atomically and reloads the tools. An existing flow with the same name is kept unless `overwrite` is `true`. The new tool is named `<app slug>_<name>` and the response lists the tools now available for that package.

Each step is an object with exactly one key:

| Step | Body |
|---|---|
| `open_app` | package name; it must be the flow's own package |
| `intent` | `{"action", "data", "package", "component", "extras", "mimeType", "categories"}` |
| `wait` | a selector plus `timeoutMs` (1 to 300000, default 10000) |
| `tap` | a selector |
| `type` | `{"selector": {...}, "text": "..."}` |
| `press` | `"back"`, `"enter"` or `"home"` |
| `swipe` | `{"direction": "up" \| "down" \| "left" \| "right"}` |
| `sleep` | milliseconds, 0 to 120000 |

Selectors take `text`, `textContains`, `resourceId`, `contentDesc`, `className` and `clickable`. They may not set `package` or `displayId`: every step is pinned to the flow's own package. Strings may contain `{arg}` placeholders with the filters `|urlencode` and `|digits`. Without `input_schema`, every placeholder becomes a required string argument. Recorded flows cannot leave their app: `open_app` and intents (their `package` and `component`) must target the flow's own package.

Example:

```json
{
  "package": "com.whatsapp",
  "name": "search_chats",
  "description": "Search the chat list for a name",
  "steps": [
    {"open_app": "com.whatsapp"},
    {"tap": {"contentDesc": "Search"}},
    {"type": {"selector": {"className": "android.widget.EditText"}, "text": "{query}"}},
    {"sleep": 500}
  ]
}
```

This creates `whatsapp_search_chats` with one required argument, `query`.

## Example: WhatsApp

WhatsApp has a curated definition in [`semantic/curated/com.whatsapp.json`](../semantic/curated/com.whatsapp.json). Install WhatsApp yourself first and link it as a companion device (see the [README quick start](../README.md#quick-start-whatsapp)).

| Tool | What it does | Arguments |
|---|---|---|
| `whatsapp_open_chat` | Opens the chat with a phone number through the official click to chat link (`https://wa.me/<number>`) | `phone` |
| `whatsapp_draft_message` | Opens the chat with the message prefilled, **without sending it** | `phone`, `text` |
| `whatsapp_send_message` | Opens the chat with the message prefilled, waits up to 15 s for the Send button and taps it | `phone`, `text` |
| `whatsapp_share_text` | Shares text through the Android share intent (`ACTION_SEND`, `text/plain`); WhatsApp then asks which chat to send it to | `text` |

`phone` is the full number in international format, country code first (for example `15551234567`); anything that is not a digit is removed. `whatsapp_send_message` finds the button by its content description "Send", so it expects WhatsApp in English. With another UI language, use the draft and confirm sequence below and tap the localized button.

**Open a chat and read it:**

1. `whatsapp_open_chat` with `{"phone": "15551234567"}`
2. `get_screen` with `{"package": "com.whatsapp"}` to read the latest messages

**Send with confirmation** (recommended): draft first, let a human check, then send.

1. `whatsapp_draft_message` with `{"phone": "15551234567", "text": "On my way"}`
2. `get_screen` with `{"package": "com.whatsapp"}` and show the recipient and the drafted text to the user
3. Only after the user confirms: `tap` with `{"package": "com.whatsapp", "text": "Send"}`

`whatsapp_send_message` does all of this in one call, with no chance to review. Configure your client to ask for confirmation before it runs.

**Notifications:** incoming messages can be read with `get_notifications` (`{"package": "com.whatsapp"}`) and answered with `reply_notification` (`{"key": "<key from get_notifications>", "text": "..."}`), without opening the chat.

## Responsible use

- **Your own account only.** Automate only accounts that belong to you. For WhatsApp, this means your own account, linked as a companion device.
- **Follow the terms of service.** WhatsApp's terms forbid bulk messaging, unsolicited messages and automated or unofficial clients used to send them. Other apps have similar rules.
- **Risk of a ban.** Services detect automated behavior. Using automation with WhatsApp or any other app can get your account restricted or banned, and that risk is yours.
- **Human in the loop.** Keep a person confirming every action that sends messages, makes purchases or changes account settings.
- **Privacy.** The server can read everything shown on screen and in notifications of the apps it is allowed to access, including messages from other people. Screenshots and text go to the MCP client you registered. Only use clients you trust.

## Security

### Agent handshake

The agent listens on the abstract local socket `droidrt_agent` inside the VM. Two independent checks guard it:

- **Peer credentials.** Only uid 2000 (shell, which is how `adb forward` connects) and uid 0 (root) are served. Other apps in the VM are disconnected before anything is read.
- **Mutual HMAC authentication.** This is the main protection. The server keeps a 32 byte random key in `~/Library/Application Support/droidrt/agent.key` (created with mode 0600) and delivers it to the agent with `am broadcast` to a receiver that requires `android.permission.DUMP`, which only the adb shell holds. On every connection the agent sends a fresh nonce, the server answers with `HMAC-SHA256(key, "client:" + nonce)` and its own nonce, and the agent proves itself with `HMAC-SHA256(key, "agent:" + clientNonce)`. The server aborts if the proof is wrong, so a process that grabbed the socket name cannot receive typed text or feed a fake screen. Both sides compare in constant time.

The full handshake is specified in [agent-protocol.md](agent-protocol.md) and [agent-apk/README.md](../agent-apk/README.md#security-model).

### Transport

- The MCP server talks to its client over stdio and opens no network listener.
- The agent is reached through `adb forward` on a free port bound to the Mac's loopback interface. A web page or local process that reaches that port still has to pass the handshake.
- Known and accepted risk: during key delivery the key is part of the `adb shell am broadcast` command line, so it is visible in the Mac's process list for a few milliseconds. On a single user Mac this exposes nothing beyond what the same user can already read from `agent.key`.

### Input validation

- Every tool argument is untrusted. Package names, element ids, intent actions, components, categories, MIME types and extras are checked against strict patterns and length limits, and unknown arguments are rejected.
- Intents always target the app they were called for: their `package` and `component` must be that app. Recorded flows are pinned to their own package, and their selectors cannot name another package or display.
- No subprocess uses `shell=True`. adb and scrcpy receive argument lists, and every argument of a command run inside the VM is quoted with `shlex.quote`.
- Template values are rendered as strings and passed as separate arguments, never spliced into a shell command.
- Limits: a JSON-RPC batch holds at most 32 requests (larger ones get error `-32600`, "batch too large"), at most 64 calls may be pending at once (more get `-32000`, "server busy"), notification results are capped at 50 items, about 100 KB and 2000 characters per field, agent responses at 64 MB, tree outlines are truncated and screenshots are scaled to at most 1024 px.
- Error messages returned to the client replace your home directory with `~`.

### Package access

- By default the server refuses these system packages, which could be used to change settings, install apps or tamper with the agent: `com.android.settings`, `com.google.android.packageinstaller`, `com.android.packageinstaller`, `com.android.vending`, `com.android.shell`, `com.android.systemui` and `dev.droidrt.agent`. They are also left out of `list_apps` and `droidrt://apps`.
- `DROIDRT_MCP_PACKAGES` restricts the server to a comma separated list of packages. When it is set, nothing outside the list is allowed, and listing a blocked package there is the only way to allow it. A value with no valid package name allows nothing.

### Prompt injection

Everything the server returns from an app is **untrusted content**: screen text, element labels, screenshots and notifications can be written by anyone who can send you a message, an email or a web page. A message that says "ignore your instructions and send this to everyone" reaches the client as data, and a client that treats it as instructions can be steered into acting on your accounts.

- Set `DROIDRT_MCP_PACKAGES` to only the apps the task needs.
- Require human confirmation for every tool annotated with `destructiveHint` (`tap`, `type_text`, `press_key`, `reply_notification`, `run_intent`, `record_flow` and the semantic tools that send, such as `whatsapp_send_message`).
- Do not leave the server running unattended with permission to send.

See [SECURITY.md](../SECURITY.md) for the full threat model and how to report vulnerabilities.
