---
name: rebuild-exe
description: Rebuild AssetManagementTool.exe from source with PyInstaller, avoiding the stale-build-cache footgun. Use when asked to build, rebuild, or package a new .exe for the Asset Management app.
---

Always delete `build/` and `dist/` first — PyInstaller caches stale analysis
otherwise, see `DOCUMENTATION.md` §9:

```
rm -rf build dist
find . -iname "__pycache__" -exec rm -rf {} +
pyinstaller build.spec --noconfirm --clean
```

Bump `APP_VERSION` in `app/version.py` first, and fully close any already-
running `AssetManagementTool.exe` before launching the fresh build — a
running process keeps whatever code it loaded at startup.
