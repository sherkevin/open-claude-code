"""Generate the AMD64 COFF version resource for kaiwu-connector.exe."""

import argparse
import re
import struct
from pathlib import Path


PRODUCT_NAME = "开物内网连接器"
LANGUAGE = 0x0804  # Chinese (Simplified, China)
CODEPAGE = 1200  # UTF-16


def utf16z(text):
    return (text + "\0").encode("utf-16le")


def version_block(key, value=b"", *, text=False, children=()):
    # Version information blocks start on four-byte boundaries. String value
    # lengths count UTF-16 code units, while binary value lengths count bytes.
    payload = bytes(6) + utf16z(key)
    payload += bytes(-len(payload) % 4) + value
    for child in children:
        payload += bytes(-len(payload) % 4) + child
    value_length = len(value) // 2 if text else len(value)
    return struct.pack("<HHH", len(payload), value_length, text) + payload[6:]


def version_info(version):
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValueError("VERSION 必须是三段数字版本")
    parts = [part.lstrip("0") or "0" for part in version.split(".")]
    if any(len(part) > 5 or int(part) > 65535 for part in parts):
        raise ValueError("VERSION 每段必须在 0 到 65535 之间")
    major, minor, patch = map(int, parts)
    version_ms = major << 16 | minor
    version_ls = patch << 16  # The fourth Windows version component is zero.
    fixed = struct.pack(
        "<13I", 0xFEEF04BD, 0x00010000,
        version_ms, version_ls, version_ms, version_ls,
        0x3F, 0, 0x00040004, 1, 0, 0, 0,
    )

    strings = {
        "FileDescription": PRODUCT_NAME,
        "FileVersion": version,
        "OriginalFilename": "kaiwu-connector.exe",
        "ProductName": PRODUCT_NAME,
        "ProductVersion": version,
    }
    string_table = version_block(
        f"{LANGUAGE:04X}{CODEPAGE:04X}", text=True,
        children=tuple(version_block(key, utf16z(value), text=True) for key, value in strings.items()),
    )
    string_info = version_block("StringFileInfo", text=True, children=(string_table,))
    translation = version_block("Translation", struct.pack("<HH", LANGUAGE, CODEPAGE))
    var_info = version_block("VarFileInfo", text=True, children=(translation,))
    return version_block("VS_VERSION_INFO", fixed, children=(string_info, var_info))


def resource_object(version_data):
    # One integer entry per resource directory, in type / name / language order.
    directory_entry = struct.Struct("<IIHHHHII")
    directory_size = directory_entry.size
    leaf_offset = 3 * directory_size
    data_offset = leaf_offset + 16  # IMAGE_RESOURCE_DATA_ENTRY
    subdirectory = 0x80000000
    section = b"".join(
        directory_entry.pack(0, 0, 0, 0, 0, 1, resource_id, target)
        for resource_id, target in (
            (16, subdirectory | directory_size),  # RT_VERSION
            (1, subdirectory | (2 * directory_size)),
            (LANGUAGE, leaf_offset),
        )
    )
    section += struct.pack("<IIII", data_offset, len(version_data), CODEPAGE, 0) + version_data

    # The linker relocates the data entry's address from a section offset to an RVA.
    relocation = struct.pack("<IIH", leaf_offset, 0, 0x0003)  # IMAGE_REL_AMD64_ADDR32NB
    section_offset = 20 + 40  # COFF file and section headers
    relocation_offset = section_offset + len(section)
    symbol_offset = relocation_offset + len(relocation)
    name = b".rsrc\0\0\0"
    header = struct.pack("<HHIIIHH", 0x8664, 1, 0, symbol_offset, 1, 0, 0)
    section_header = struct.pack(
        "<8sIIIIIIHHI", name, 0, 0, len(section), section_offset,
        relocation_offset, 0, 1, 0, 0x40300040,  # Initialized read-only data, aligned to 4 bytes.
    )
    symbol = struct.pack("<8sIhHBB", name, 0, 1, 0, 3, 0)  # Static section symbol.
    return header + section_header + section + relocation + symbol + struct.pack("<I", 4)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        data = version_info(args.version)
    except ValueError as exc:
        parser.error(str(exc))
    with args.output.open("xb") as output:
        output.write(resource_object(data))


if __name__ == "__main__":
    main()
