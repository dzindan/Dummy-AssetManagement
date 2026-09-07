---
name: rebuild-exe
description: Rebuild AssetManagementTool.exe from source with PyInstaller and package it into release/ as a versioned zip. Use when asked to build, rebuild, release, or package a new .exe for the Asset Management app.
---

## 1. Bump the version - always, every build

Bump `APP_VERSION` in `app/version.py`, even if no app code changed since
the last build - it's bumped on *every* build so the running app's footer
can prove which build it actually is (see the file's own docstring). If
today already has an unreleased `X.N` version with no corresponding
`release/AssetManagementTool_vX.N.zip`, bump to `X.N+1` rather than
reusing it.

Fully close any already-running `AssetManagementTool.exe` first - a
running process keeps whatever code it loaded at startup, so rebuilding
under it proves nothing.

## 2. Rebuild

Always delete `build/` and `dist/` first - PyInstaller caches stale
analysis otherwise, see `DOCUMENTATION.md` §9:

```
rm -rf build dist
find . -iname "__pycache__" -exec rm -rf {} +
pyinstaller build.spec --noconfirm --clean
```

## 3. Package the release (release/ - gitignored, not part of any commit)

1. Add a new top "Có gì mới ở bản `<version>`:" section to `release/
   AssetManagementTool/HUONG DAN CAI DAT.txt` (Vietnamese, same voice as
   the existing entries - `MỚI -` for new features, plain `Fix:` for bug
   fixes, "Dọn dẹp code nội bộ" for internal refactors with no visible
   change) and update its `Build: <version>` line at the top.
2. Copy the fresh `dist/AssetManagementTool.exe` and current
   `DOCUMENTATION.md` into `release/AssetManagementTool/` (overwriting
   the previous build's copies).
3. Zip those 3 files (flat, no subfolder inside the archive) into
   `release/AssetManagementTool_v<version>.zip` - `zip` isn't on PATH in
   this environment, so use:
   ```
   python -c "
   import zipfile, os
   with zipfile.ZipFile('release/AssetManagementTool_v<version>.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
       for f in ['AssetManagementTool.exe', 'DOCUMENTATION.md', 'HUONG DAN CAI DAT.txt']:
           zf.write(os.path.join('release/AssetManagementTool', f), arcname=f)
   "
   ```
Every past version bump has its own zip in `release/` (check `ls release/`
for the pattern) - this step isn't optional even for a same-day patch.
