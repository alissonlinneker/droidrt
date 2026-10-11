## What and why

<!-- What does this change do, and what problem does it solve? Link the issue: "Closes #123". -->

## How it was tested

<!-- Commands you ran, the Mac (chip, macOS version) and the apps you tried, if relevant. -->

- [ ] `make test` passes
- [ ] `make build` passes (launcher and iconkit compile)
- [ ] Tried on a real VM (describe below), or the change does not touch runtime behavior

## Checklist

- [ ] No APK, XAPK, APKS or APKM files, and no third party binaries, are added
- [ ] No new runtime dependency outside the Python standard library (or it is discussed in the issue)
- [ ] Subprocesses are called with argument lists, never `shell=True`
- [ ] README, `docs/` and `CHANGELOG.md` are updated when behavior or commands change
- [ ] Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:` and so on)
