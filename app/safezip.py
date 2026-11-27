"""Excel workbooks are zip files: a small upload can unpack to gigabytes (a
"zip bomb") and take the app's memory down (2026-11-27). install() wraps
openpyxl.load_workbook - every caller, whatever module - so a workbook whose
entries declare more than MAX_UNPACKED in all is refused before openpyxl
reads it (zipfile never gives more than an entry declares, so the declared
sizes bound what is read). The XML itself is read through defusedxml
(installed: openpyxl uses it by itself).
"""

from __future__ import annotations

import io
import os
import zipfile

MAX_UNPACKED = 300 * 1024 * 1024
_installed = False


class TooLarge(ValueError):
    pass


def check(source) -> None:
    """source: a path, bytes or a binary file object. Not a zip: left to the reader to refuse."""
    rewind = None
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    elif hasattr(source, "seek") and hasattr(source, "tell"):
        rewind = source.tell()
    elif not isinstance(source, (str, os.PathLike)):
        return
    try:
        with zipfile.ZipFile(source) as z:
            total = sum(info.file_size for info in z.infolist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return
    finally:
        if rewind is not None:
            source.seek(rewind)
    if total > MAX_UNPACKED:
        raise TooLarge(f"The workbook unpacks to {total // (1024 * 1024)} MB - over the "
                       f"{MAX_UNPACKED // (1024 * 1024)} MB this app opens. Is it the right file?")


def install() -> None:
    global _installed
    if _installed:
        return
    import openpyxl
    import openpyxl.reader.excel as excel

    real = excel.load_workbook

    def load_workbook(filename, *args, **kwargs):
        check(filename)
        return real(filename, *args, **kwargs)

    load_workbook.__doc__ = real.__doc__
    openpyxl.load_workbook = excel.load_workbook = load_workbook
    _installed = True
