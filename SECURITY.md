# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub Security Advisories:

**https://github.com/alissonlinneker/droidrt/security/advisories/new**

Do not open a public issue, pull request or discussion for a suspected vulnerability.

Include, when possible:

- the affected component (`droidrt.py`, `launcher/`, `iconkit/`, `agent-apk/`, `droidrt_mcp/`, `semantic/`)
- the commit you tested (`git rev-parse --short HEAD`)
- steps to reproduce, or a proof of concept
- the impact you expect (for example code execution on the Mac, access to app data in the VM)

You can expect an acknowledgement within 7 days. Once the issue is confirmed, a fix is prepared in a private fork, and the advisory is published together with the fix. Reporters are credited in the advisory unless they prefer otherwise.

## Supported versions

| Version | Supported |
|---|---|
| `main` | yes |
| 0.1.x | yes |

droidrt is pre 1.0. Security fixes land on `main` and in the next 0.1.x release.

## Scope

In scope:

- Code execution, file writes or privilege gain on the Mac caused by a crafted APK, XAPK, APKS or APKM, or by a crafted app installed in the VM
- Path traversal or resource exhaustion when unpacking bundles or rendering icons
- Command injection through package names, labels, file names or any other app controlled value
- Ways for apps inside the VM, or for other local users or processes on the Mac, to drive the device agent or the MCP server, including weaknesses in the agent handshake
- Ways around the MCP server's argument validation, the `DROIDRT_MCP_PACKAGES` allowlist or the system package block
- Leaks of notification content, screen content or account data beyond what a documented tool returns
- Weaknesses in the generated launcher bundles or in the CI workflow

Out of scope:

- Vulnerabilities in the Android Emulator, the Android system image, scrcpy or adb themselves (please report them upstream)
- The known platform limits described in [docs/compatibility.md](docs/compatibility.md), such as Play Integrity or DRM
- Attacks that require an already compromised Mac user account
- Misuse of automation against third party services, which is covered by the terms of those services

## Threat model (summary)

- **APKs are untrusted.** Installing an app must never run its code on the Mac. Bundles are unpacked into temporary directories with path traversal checks and size caps (4 GiB in total and 2 GiB per entry). Raster icons above 32 MiB or 4096 px per side are skipped. `iconkit` parses resources as data, with a 32 MiB cap per entry, a depth limit of 16 on every recursive lookup, caps on drawable nodes and path commands, and bitmaps limited to 4096 px per side; the CoreGraphics compositor checks the same kind of limits again. App labels lose control, bidi and zero width characters before they reach file names and `Info.plist`. Apps run only inside the VM.
- **No shell interpolation.** Every subprocess, on the host and in adb commands built by droidrt, receives an argument list. `shell=True` is not used. Commands run inside the VM by the MCP server quote every argument with `shlex.quote`.
- **The device agent requires mutual authentication.** The agent listens on the abstract local socket `droidrt_agent` inside the VM and serves only peers running as uid 2000 (shell, which is what adb forwards use) or uid 0 (root). That check alone is not enough, because the forwarded port on the Mac can be reached by any local process. The main protection is a mutual HMAC-SHA256 handshake with fresh nonces on both sides: the agent only accepts a client that knows the shared key, and the host only trusts an agent that proves the same key. The key is 32 random bytes kept in `~/Library/Application Support/droidrt/agent.key` (mode 0600) and delivered with `am broadcast` to a receiver that requires `android.permission.DUMP`, so no app in the VM can set it. Details are in [agent-apk/README.md](agent-apk/README.md#security-model).
- **Accepted risk: key delivery.** While the key is delivered, it is part of the `adb shell am broadcast` command line and is visible in the Mac's process list for a few milliseconds. On a single user Mac this gives nothing that the same user cannot already read from `agent.key`. On a Mac shared with untrusted local users, do not run the MCP server.
- **Host exposure is local only.** The agent is reached from the Mac through `adb forward`, bound to localhost. The MCP server talks to its client over stdio and opens no network listener.
- **MCP input is untrusted.** Every tool argument is validated against strict patterns and length limits, and unknown arguments are rejected. Intent components must belong to the target package and recorded flows cannot leave their app. Sensitive system packages (`com.android.settings`, `com.google.android.packageinstaller`, `com.android.packageinstaller`, `com.android.vending`, `com.android.shell`, `com.android.systemui`, `dev.droidrt.agent`) are blocked unless listed in the `DROIDRT_MCP_PACKAGES` allowlist. JSON-RPC batches are capped at 32 requests and notification results at 50 items and 100 KB. Errors sent to the client have the home directory redacted to `~`.
- **Least privilege on the Mac.** The microphone is off unless `DROIDRT_MIC=1`. Camera and microphone access is mediated by macOS privacy prompts, per launcher.
- **Nothing third party is redistributed.** The repository contains no APKs, and CI rejects any committed `.apk`, `.xapk`, `.apks` or `.apkm` file.

The VM itself is a standard Android emulator image with adb enabled. Anyone who can run commands as your Mac user can control it, as with any local Android emulator.

## Prompt injection

The MCP server hands screen text, screenshots and notifications to an automated client. That content is untrusted: anyone who can send you a message, an email or a web page controls part of it, and can try to make the client follow instructions written there. droidrt cannot tell data from instructions, so the defense is on the configuration side:

- Set `DROIDRT_MCP_PACKAGES` to only the apps the client needs.
- Require human confirmation for every tool that acts on an app (tools annotated with `destructiveHint`, and semantic tools that send, such as `whatsapp_send_message`).
- Do not leave the server running unattended with permission to send messages or change settings.

Prompt injection that makes a client misuse a tool is not a droidrt vulnerability by itself. Ways to bypass the package allowlist, the system package block, argument validation or the agent handshake are, and should be reported.
