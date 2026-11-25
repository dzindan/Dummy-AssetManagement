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
        # the central server (`serve`, app/server.py imports them lazily)
        "cheroot.wsgi",
        "cheroot.ssl.builtin",
        "cryptography.x509",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        # AD sign-in (app/winauth.py imports them lazily)
        "sspi",
        "sspicon",
        "win32security",
        "win32api",
        "win32con",
        "win32net",
        "pywintypes",
        "docxtpl",
        "docxcompose",
        "docx",
        "segno",
        "lxml",
        "lxml.etree",
        "zeep",
        "zeep.wsdl",
        "zeep.transports",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# SQLite newer than the one Python 3.14 comes with (3.50.4 has the WAL-reset
# database corruption bug, fixed in 3.51.3 - https://sqlite.org/wal.html).
# vendor/sqlite3.dll = 3.54.1 from sqlite.org's sqlite-dll-win-x64-3540100.zip,
# SHA3-256 checked. Drop this once Python itself ships >= 3.51.3.
import os

a.binaries = [b for b in a.binaries if os.path.basename(b[0]).lower() != "sqlite3.dll"]
a.binaries.append(("sqlite3.dll", os.path.join(SPECPATH, "vendor", "sqlite3.dll"), "BINARY"))

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
    console=False,  # no black window; CLI commands print into their cmd (main._attach_console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    onefile=True,
)
