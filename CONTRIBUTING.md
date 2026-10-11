# Contributing to droidrt

Thanks for your interest in droidrt. Bug reports, compatibility reports, documentation fixes and code are all welcome.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to help

- **Compatibility reports.** Try apps and report how they behave with the [compatibility form](https://github.com/alissonlinneker/droidrt/issues/new?template=compat_report.yml). Reports of apps that work are useful too.
- **Bug reports.** Use the [bug form](https://github.com/alissonlinneker/droidrt/issues/new?template=bug_report.yml) and include your macOS version, chip, emulator and scrcpy versions, and relevant lines from `~/Library/Logs/droidrt/`.
- **Code and docs.** Pick an open issue, or open one first for larger changes so the approach can be discussed before you invest time.

Security issues must be reported privately, as described in [SECURITY.md](SECURITY.md).

## Development setup

Follow the installation steps in the [README](README.md), then:

```sh
git clone https://github.com/<you>/droidrt.git
cd droidrt
make build     # launcher, icon compositor and device agent
make test      # test_droidrt, test_mcp, semantic/test_semantic.py and iconkit, no VM needed
```

Running `./droidrt.py <command>` from the clone uses your working copy directly. `make install` links it into your `PATH`.

The compatibility matrix needs a running VM (`droidrt start`):

```sh
./compat/run_matrix.py --dry-run
./compat/run_matrix.py --only org.localsend.localsend_app
```

## Guidelines

### Code

- **Python:** 3.10 or newer, standard library only. A new third party dependency needs a discussion in an issue first.
- **Swift:** system frameworks only, built by the plain `build.sh` scripts (no Xcode project).
- **Device agent:** plain Java against the Android SDK, built without Gradle or downloaded dependencies.
- **Subprocesses:** always pass an argument list. Never use `shell=True` or build shell strings from values that come from APKs, apps or users.
- **Untrusted input:** treat APKs, bundle manifests and anything read from the VM as untrusted. Check paths when extracting, limit sizes and recursion depth.
- Keep functions small and testable. Pure parsing logic (badging, manifests, icons, selectors) should have unit tests that run without a VM.
- Follow `.editorconfig`: 4 spaces for Python, Swift and Java, 2 for YAML and JSON.

### Tests

- `make test` must pass. CI runs it on an Apple Silicon runner, together with the Swift builds.
- MCP changes: keep every tool argument validated in `droidrt_mcp/validate.py`, keep tool annotations accurate, and update the tool tables in `docs/mcp.md`. Semantic tool format changes go in `docs/agent-protocol.md`.
- Add or update tests for every behavior change. Tests must not need a VM, network access or APK files.
- If you changed runtime behavior, describe in the pull request how you tried it on a real VM.

### APKs and third party content

- **Never commit APKs** (`.apk`, `.xapk`, `.apks`, `.apkm`) or any other third party binary. CI rejects them.
- Do not add screenshots or logs that show personal data, chats, phone numbers or account details.
- Example apps in docs and tests are limited to WhatsApp (installed by the user from official sources) and the open source apps in `compat/apps.json`.

### Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) in English: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `ci:`, `chore:`.
- Reference the issue in the commit or pull request (`Closes #123`).
- Keep pull requests focused. Unrelated refactors belong in separate pull requests.
- Update `README.md`, the files in `docs/` and `CHANGELOG.md` when you change commands, options or behavior.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
