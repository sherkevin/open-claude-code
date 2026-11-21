#!/usr/bin/env bash
# Integration test for the Windows main executable. Requires 7-Zip.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/kaiwu-connector-version-test.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT

read -r source_commit source_epoch <<< "$(git -C "$SCRIPT_DIR" log -1 --format='%h %ct')"
export COMMIT="${COMMIT:-$source_commit}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$source_epoch}"
printf '构建提交 %s，源时间戳 %s\n' "$COMMIT" "$SOURCE_DATE_EPOCH"

assert_version() {
  local exe="$1" version="$2" metadata expected
  # 7-Zip decodes the executable's PE version resource, listing both fixed
  # numeric versions and display strings under the archive comment.
  metadata="$(LC_ALL=C.UTF-8 7z l -slt "$exe" | awk '
    /^Comment = / { resource = 1; sub(/^Comment = /, "") }
    !NF { resource = 0 }
    resource { print }
  ')"
  expected="$(cat <<EOF
FileVersion: $version.0
FileVersion: $version
ProductVersion: $version.0
ProductVersion: $version
FileDescription: 开物内网连接器
OriginalFilename: kaiwu-connector.exe
ProductName: 开物内网连接器
EOF
)"
  if [[ "$metadata" != "$expected" ]]; then
    printf '版本资源不符合预期 %s\n%s\n' "$exe" "$metadata" >&2
    return 1
  fi
  printf '%s\n' "$metadata"
}

version=1.3.14
for build in 1 2; do
  echo "验证 $version 第 $build 次构建"
  VERSION="$version" bash "$SCRIPT_DIR/build.sh" --windows-exe "$work/$build/kaiwu-connector.exe"
  assert_version "$work/$build/kaiwu-connector.exe" "$version"
done
cmp "$work/1/kaiwu-connector.exe" "$work/2/kaiwu-connector.exe"
digest="$(sha256sum "$work/1/kaiwu-connector.exe")"
printf '两次构建逐字节一致，SHA-256 %s\n' "${digest%% *}"

version=12.34.65535
echo "验证覆盖版本 $version"
VERSION="$version" bash "$SCRIPT_DIR/build.sh" --windows-exe "$work/override/kaiwu-connector.exe"
assert_version "$work/override/kaiwu-connector.exe" "$version"

for version in 1.3.65536 1.3.14-rc.1; do
  if VERSION="$version" bash "$SCRIPT_DIR/build.sh" --windows-exe "$work/invalid.exe" \
      > "$work/invalid.log" 2>&1; then
    printf '构建错误地接受了无效版本 %s\n' "$version" >&2
    exit 1
  fi
  case "$version" in
    1.3.65536) expected='VERSION 每段必须在 0 到 65535 之间' ;;
    1.3.14-rc.1) expected='VERSION 必须是三段数字版本' ;;
  esac
  if [[ -e "$work/invalid.exe" || $(< "$work/invalid.log") != *"$expected"* ]]; then
    cat "$work/invalid.log" >&2
    exit 1
  fi
  printf '已拒绝无效版本 %s，未生成可执行文件\n' "$version"
done
