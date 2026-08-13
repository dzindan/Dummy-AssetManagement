# PyInstaller spec for the Asset Management Tool.
# Build with: pyinstaller build.spec --noconfirm --clean
# IMPORTANT: always `rm -rf build dist` (or delete those folders) before
# rebuilding - PyInstaller's incremental build cache in build/ can silently
# keep stale code in a "rebuilt" exe otherwise.
# Produces a single standalone dist/AssetManagementTool.exe

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("app/templates", "app/templates"),
        ("app/static", "app/static"),
        ("templates_docx", "templates_docx"),
    ],
    hiddenimports=[
        "docxtpl",
        "docxcompose",
        "docx",
        "lxml",
        "lxml.etree",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AssetManagementTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    onefile=True,
)
