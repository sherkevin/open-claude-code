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
if [[ -z "$MAKENSIS" || ! -x "$MAKENSIS" ]]; then
  echo "缺少 NSIS makensis；请通过 MAKENSIS 指定可执行文件" >&2
  exit 1
fi

# makensis 3.08 hardcodes /tmp for its large compression scratch file. Keep
# that file on the project data volume through a private mount namespace so a
# full root filesystem cannot turn the final mmap into SIGBUS.
if ! command -v unshare >/dev/null 2>&1; then
  echo "缺少 unshare，无法把 makensis 临时目录隔离到数据盘" >&2
  exit 1
fi
NSIS_TMPDIR="${NSIS_TMPDIR:-${TMPDIR:-$ROOT/dist}/nsis-work}"
mkdir -p "$NSIS_TMPDIR"

mkdir -p "$(dirname "$OUTPUT_FILE")"
export SOURCE_DATE_EPOCH
unshare -Urmpf --propagation private bash -c '
  set -euo pipefail
  mount --bind "$1" /tmp
  shift
  exec "$@"
' bash "$NSIS_TMPDIR" "$MAKENSIS" -V2 -NOCD \
  -DPAYLOAD_DIR="$PAYLOAD_DIR" \
  -DVERSION="$VERSION" \
  -DVERSION_QUAD="$VERSION_QUAD" \
  -DCOMMIT="$COMMIT" \
  -DOUTPUT_FILE="$OUTPUT_FILE" \
  "$ROOT/packaging/windows/installer.nsi"

if [[ ! -s "$OUTPUT_FILE" ]]; then
  echo "NSIS 未生成安装包: $OUTPUT_FILE" >&2
  exit 1
fi
chmod 0755 "$OUTPUT_FILE"
file "$OUTPUT_FILE"
sha256sum "$OUTPUT_FILE"
