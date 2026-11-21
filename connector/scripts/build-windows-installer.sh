#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "用法: $0 <payload-dir> <version> <commit> <output-file> <source-date-epoch>" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PAYLOAD_DIR="$(cd "$1" && pwd)"
VERSION="$2"
COMMIT="$3"
OUTPUT_FILE="$4"
SOURCE_DATE_EPOCH="$5"

if [[ ! "$VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "VERSION 必须是三段数字版本: $VERSION" >&2
  exit 1
fi
VERSION_QUAD="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}.0"
if [[ ! "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]]; then
  echo "SOURCE_DATE_EPOCH 必须是非负整数秒" >&2
  exit 1
fi

MAKENSIS="${MAKENSIS:-$(command -v makensis || true)}"

if [[ ! -f "$PAYLOAD_DIR/connector.ico" ]]; then
  echo "缺少安装器图标: $PAYLOAD_DIR/connector.ico" >&2
  exit 1
fi
if [[ -z "$MAKENSIS" || ! -x "$MAKENSIS" ]]; then
  echo "缺少 NSIS makensis；请通过 MAKENSIS 指定可执行文件" >&2
  exit 1
fi

# A locally extracted NSIS keeps its executable under <prefix>/usr/bin but the
# binary still resolves stubs and includes from the compile-time /usr/share/
# nsis path. When such a prefix is used, bind its share tree inside the private
# mount namespace below instead of writing anything to the root filesystem.
NSIS_SHARE_DIR="${NSIS_SHARE_DIR:-}"
if [[ -z "$NSIS_SHARE_DIR" ]]; then
  candidate_share="$(cd "$(dirname "$MAKENSIS")/../share" 2>/dev/null && pwd || true)"
  if [[ -n "$candidate_share" && -f "$candidate_share/nsis/Stubs/zlib-x86-unicode" ]]; then
    NSIS_SHARE_DIR="$candidate_share"
  fi
fi
if [[ -n "$NSIS_SHARE_DIR" ]]; then
  NSIS_SHARE_DIR="$(cd "$NSIS_SHARE_DIR" && pwd)"
  if [[ ! -f "$NSIS_SHARE_DIR/nsis/Stubs/zlib-x86-unicode" ]]; then
    echo "NSIS_SHARE_DIR 缺少 nsis/Stubs/zlib-x86-unicode: $NSIS_SHARE_DIR" >&2
    exit 1
  fi
fi

# makensis 3.08 hardcodes /tmp for its large compression scratch file. Keep
# that file on the project data volume through a private mount namespace so a
# full root filesystem cannot turn the final mmap into SIGBUS.
# Linux CI keeps the private mount namespace so a full root filesystem cannot
# turn the final mmap into SIGBUS. Hosts without unshare (macOS cross builds)
# fall back to a plain invocation with every temp variable pointed at the same
# data volume; the produced installer is identical.
ISOLATE_NSIS=1
if ! command -v unshare >/dev/null 2>&1; then
  ISOLATE_NSIS=0
  echo "提示：当前主机没有 unshare，直接调用 makensis（临时目录见下）" >&2
fi
NSIS_TMPDIR="${NSIS_TMPDIR:-${TMPDIR:-$ROOT/dist}/nsis-work}"
mkdir -p "$NSIS_TMPDIR"
plugin_dir=$(mktemp -d "$(cd "$NSIS_TMPDIR" && pwd)/journal-plugin.XXXXXXXX")
trap 'rm -rf -- "$plugin_dir"' EXIT
windows_x86_cc="${WINDOWS_X86_CC:-i686-w64-mingw32-gcc}"
if ! command -v "$windows_x86_cc" >/dev/null 2>&1; then
  echo "缺少 32 位 Windows MinGW 编译器。请安装 i686-w64-mingw32-gcc 或通过 WINDOWS_X86_CC 指定。" >&2
  exit 1
fi
# MinGW otherwise derives the DLL's preferred base from the temporary output
# path. The loader can still relocate the image at runtime using ASLR.
"$windows_x86_cc" -std=c11 -Os -Wall -Wextra -Werror -shared -static-libgcc \
  -Wl,--no-insert-timestamp,--disable-auto-image-base -o "$plugin_dir/KaiwuJournal.dll" \
  "$ROOT/packaging/windows/attempt-journal.c" -ladvapi32 -lbcrypt -lshell32 -lntdll
touch -d "@$SOURCE_DATE_EPOCH" "$plugin_dir/KaiwuJournal.dll"

mkdir -p "$(dirname "$OUTPUT_FILE")"
export SOURCE_DATE_EPOCH NSIS_SHARE_DIR

run_makensis_plain() {
  echo "makensis 临时目录：$NSIS_TMPDIR" >&2
  TMPDIR="$NSIS_TMPDIR" TMP="$NSIS_TMPDIR" TEMP="$NSIS_TMPDIR" \
    "$MAKENSIS" -V2 -NOCD -INPUTCHARSET UTF8 \
      -DPAYLOAD_DIR="$PAYLOAD_DIR" \
      -DJOURNAL_PLUGIN_DIR="$plugin_dir" \
      -DVERSION="$VERSION" \
      -DVERSION_QUAD="$VERSION_QUAD" \
      -DCOMMIT="$COMMIT" \
      -DOUTPUT_FILE="$OUTPUT_FILE" \
      "$ROOT/packaging/windows/installer.nsi"
}

if [[ "$ISOLATE_NSIS" == 1 ]]; then
  if ! unshare -Urmpf --propagation private bash -c '
    set -euo pipefail
    mount --bind "$1" /tmp
    if [[ -n "${NSIS_SHARE_DIR:-}" ]]; then
      mount --bind "$NSIS_SHARE_DIR" /usr/share
    fi
    shift
    exec "$@"
  ' bash "$NSIS_TMPDIR" "$MAKENSIS" -V2 -NOCD -INPUTCHARSET UTF8 \
    -DPAYLOAD_DIR="$PAYLOAD_DIR" \
    -DJOURNAL_PLUGIN_DIR="/tmp/${plugin_dir##*/}" \
    -DVERSION="$VERSION" \
    -DVERSION_QUAD="$VERSION_QUAD" \
    -DCOMMIT="$COMMIT" \
    -DOUTPUT_FILE="$OUTPUT_FILE" \
    "$ROOT/packaging/windows/installer.nsi"; then
    echo "提示：私有挂载命名空间不可用，改用直接调用 makensis" >&2
    run_makensis_plain
  fi
else
  run_makensis_plain
fi

if [[ ! -s "$OUTPUT_FILE" ]]; then
  echo "NSIS 未生成安装包: $OUTPUT_FILE" >&2
  exit 1
fi
chmod 0755 "$OUTPUT_FILE"
file "$OUTPUT_FILE"
sha256sum "$OUTPUT_FILE"
