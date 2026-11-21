#!/usr/bin/env python3
"""Write a deterministic Windows-targeted ZIP with correct UTF-8 name flags.

Info-ZIP on macOS stores non-ASCII entry names as raw UTF-8 bytes without
setting the UTF-8 flag, so Windows Explorer decodes them with the local ANSI
codepage and shows garbled names (docs/交付与运维说明.md, 版本信息.txt).
This writer sets the flag, sorts entries and pins timestamps so repeated builds
produce identical archives.
"""
import datetime
import os
import stat
import sys
import zipfile


def main():
    if len(sys.argv) != 4:
        print("用法: make-zip.py <source-dir> <output.zip> <source-date-epoch>", file=sys.stderr)
        return 2
    root, output, epoch = sys.argv[1], sys.argv[2], int(sys.argv[3])
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).timetuple()[:6]

    entries = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            entries.append(os.path.relpath(full, root).replace(os.sep, "/"))
    entries.sort()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in entries:
            full = os.path.join(root, relative)
            info = zipfile.ZipInfo(relative, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = stat.S_IMODE(os.stat(full).st_mode)
            info.external_attr = (stat.S_IFREG | mode) << 16
            with open(full, "rb") as handle:
                archive.writestr(info, handle.read())
    return 0


if __name__ == "__main__":
    sys.exit(main())
