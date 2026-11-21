"""Generate AMD64 COFF Windows resources for kaiwu-connector.exe."""

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


def icon_resources(data):
    header = struct.Struct("<HHH")
    entry = struct.Struct("<BBBBHHII")
    if len(data) < header.size:
        raise ValueError("Incomplete Windows icon header")
    reserved, kind, count = header.unpack_from(data)
    table_size = header.size + count * entry.size
    if reserved != 0 or kind != 1 or count == 0 or table_size > len(data):
        raise ValueError("Invalid Windows icon directory")

    images = {}
    group = [header.pack(reserved, kind, count)]
    for index in range(count):
        *attributes, size, offset = entry.unpack_from(data, header.size + index * entry.size)
        if size == 0 or offset < table_size or offset + size > len(data):
            raise ValueError("Invalid Windows icon image")
        resource_id = index + 1
        images[resource_id] = {LANGUAGE: (data[offset:offset + size], 0)}
        # A group icon references each image by resource ID, not ICO file offset.
        group.append(struct.pack("<BBBBHHIH", *attributes, size, resource_id))
    return {3: images, 14: {1: {LANGUAGE: (b"".join(group), 0)}}}  # RT_ICON, RT_GROUP_ICON


def resource_object(version_data, icon_data=None):
    tree = {16: {1: {LANGUAGE: (version_data, CODEPAGE)}}}  # RT_VERSION
    if icon_data is not None:
        tree.update(icon_resources(icon_data))
    section = bytearray()
    leaves = []

    def directory(entries):
        offset = len(section)
        section.extend(struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(entries)))
        table = len(section)
        section.extend(bytes(8 * len(entries)))
        for index, (resource_id, child) in enumerate(sorted(entries.items())):
            if isinstance(child, dict):
                target = directory(child) | 0x80000000
            else:
                target = len(section)
                section.extend(bytes(16))  # IMAGE_RESOURCE_DATA_ENTRY
                leaves.append((target, *child))
            struct.pack_into("<II", section, table + 8 * index, resource_id, target)
        return offset

    directory(tree)
    relocations = bytearray()
    for offset, payload, codepage in leaves:
        section.extend(bytes(-len(section) % 4))
        struct.pack_into("<IIII", section, offset, len(section), len(payload), codepage, 0)
        section.extend(payload)
        # Relocate the address in the data entry from a section offset to an RVA.
        relocations.extend(struct.pack("<IIH", offset, 0, 0x0003))  # IMAGE_REL_AMD64_ADDR32NB

    section_offset = 20 + 40  # COFF file and section headers
    relocation_offset = section_offset + len(section)
    symbol_offset = relocation_offset + len(relocations)
    name = b".rsrc\0\0\0"
    header = struct.pack("<HHIIIHH", 0x8664, 1, 0, symbol_offset, 1, 0, 0)
    section_header = struct.pack(
        "<8sIIIIIIHHI", name, 0, 0, len(section), section_offset,
        relocation_offset, 0, len(leaves), 0, 0x40300040,  # Initialized read-only data, aligned to 4 bytes.
    )
    symbol = struct.pack("<8sIhHBB", name, 0, 1, 0, 3, 0)  # Static section symbol.
    return header + section_header + section + relocations + symbol + struct.pack("<I", 4)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    parser.add_argument("--icon", type=Path)
    args = parser.parse_args()
    try:
        icon = args.icon.read_bytes() if args.icon else None
        data = resource_object(version_info(args.version), icon)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    with args.output.open("xb") as output:
        output.write(data)


if __name__ == "__main__":
    main()
