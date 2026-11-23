#!/usr/bin/env bash
# 构建开物内网连接器的交付包。
#
# 构建依赖：Go 1.25+、Python 3、NSIS、SBOM 与漏洞扫描工具。
# 默认构建现有 FRP 交付包；--native-windows 构建内置中继 Windows 包。
# 产物：dist/kaiwu-connector-<版本>-linux-amd64.tar.gz
#       dist/kaiwu-connector-<版本>-windows-amd64.zip
# 仅构建 Windows 主程序：bash scripts/build.sh --windows-exe <输出文件>
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${VERSION:-1.3.18}"
COMMIT="${COMMIT:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
# Bind package metadata and ZIP timestamps to the source commit unless the
# release operator supplies an explicit epoch. Rebuilding the same commit then
# produces the same timestamps instead of silently changing the candidate.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date -u +%s)}"
if [[ ! "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]]; then
  echo "SOURCE_DATE_EPOCH 必须是非负整数秒" >&2
  exit 1
fi
BUILD_TIME="${BUILD_TIME:-$(date -u -d "@${SOURCE_DATE_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)}"
export SOURCE_DATE_EPOCH
DIST="$ROOT/dist"
MODULE="github.com/sherkevin/open-claude-code/connector/internal/version"
LDFLAGS="-s -w -X ${MODULE}.Version=${VERSION} -X ${MODULE}.Commit=${COMMIT} -X ${MODULE}.BuildTime=${BUILD_TIME}"
native_windows=false
targets=(linux windows)
metadata_target=linux
windows_name="kaiwu-connector-${VERSION}-windows-amd64"
if [[ "${1:-}" == --native-windows || "${1:-}" == --native-windows-exe ]]; then
  native_windows=true
  targets=(windows)
  metadata_target=windows
  DIST="$DIST/native-windows"
  windows_name="kaiwu-connector-${VERSION}-native-windows-amd64"
  LDFLAGS+=" -X main.nativeRelayDefault=true"
fi
metadata_dir="$DIST/$metadata_target/compliance"

FRPC_LINUX="$ROOT/packaging/bin/frpc-linux-amd64"
FRPC_WINDOWS="$ROOT/packaging/bin/frpc-windows-amd64.exe"
FRPC_LINUX_SHA256="${FRPC_LINUX_SHA256:-0bb98b7a69ce8ee23bc60fec1664f22040a2d44032296e03c8b6573a5d28dd0b}"
FRPC_WINDOWS_SHA256="${FRPC_WINDOWS_SHA256:-d9a8cf8bc61bd5c8d57a2a4801316a18291f76d45dc5fd8aaf425bd91b57159d}"
SBOM_TOOL="${CYCLONEDX_GOMOD:-$(command -v cyclonedx-gomod || true)}"
VULN_TOOL="${GOVULNCHECK:-$(command -v govulncheck || true)}"
GO_TOOL="${GO:-$(command -v go || true)}"
PYTHON_TOOL="${PYTHON:-$(command -v python3 || true)}"

# Several SBOM/scanning tools invoke `go` as a child process even when the
# release operator supplies GO explicitly. Keep that toolchain reachable on
# PATH so a controlled build does not depend on a system-wide installation.
if [ -n "$GO_TOOL" ] && [ -x "$GO_TOOL" ]; then
  export PATH="$(cd "$(dirname "$GO_TOOL")" && pwd):$PATH"
fi

if [ -z "$GO_TOOL" ] || [ ! -x "$GO_TOOL" ]; then
  echo "缺少 Go 工具链，无法构建连接器" >&2
  exit 1
fi
if [ -z "$PYTHON_TOOL" ] || [ ! -x "$PYTHON_TOOL" ]; then
  echo "缺少 python3，无法生成 Windows 主程序版本资源" >&2
  exit 1
fi

build_windows_executable() (
  local output="$1" work
  work="$(mktemp -d "${TMPDIR:-/tmp}/kaiwu-connector-windows.XXXXXXXX")"
  trap 'rm -rf -- "$work"' EXIT

  # Keep the generated object private to this build. The staged module retains
  # the original package paths so -trimpath produces the same build identity.
  mkdir -p "$work/cmd"
  cp -R "$ROOT/cmd/connector" "$work/cmd/connector"
  "$PYTHON_TOOL" "$ROOT/scripts/windows-exe-version-resource.py" "$VERSION" \
    --icon "$ROOT/packaging/windows/connector.ico" \
    "$work/cmd/connector/kaiwu_version_windows_amd64.syso"
  cp -R "$ROOT/go.mod" "$ROOT/go.sum" "$ROOT/internal" "$work/"

  mkdir -p "$(dirname "$output")"
  output="$(cd "$(dirname "$output")" && pwd)/$(basename "$output")"
  GOOS=windows GOARCH=amd64 CGO_ENABLED=0 GOFLAGS=-trimpath GOWORK=off \
    "$GO_TOOL" -C "$work" build -trimpath -buildvcs=false -ldflags "$LDFLAGS" \
    -o "$output" ./cmd/connector
)

if [[ $# -eq 2 && ( "$1" == --windows-exe || "$1" == --native-windows-exe ) ]]; then
  build_windows_executable "$2"
  exit 0
fi
if [[ $# -ne 0 && ! ( $# -eq 1 && "$1" == --native-windows ) ]]; then
  echo "用法: $0 [--native-windows | --windows-exe <输出文件> | --native-windows-exe <输出文件>]" >&2
  exit 2
fi

if ! "$native_windows"; then
  for file in "$FRPC_LINUX" "$FRPC_WINDOWS"; do
    if [ ! -f "$file" ]; then
      echo "缺少 frpc 二进制: $file" >&2
      exit 1
    fi
  done
fi

if [ -z "$SBOM_TOOL" ] || [ ! -x "$SBOM_TOOL" ]; then
  echo "缺少 cyclonedx-gomod，无法生成受控试装 SBOM" >&2
  exit 1
fi
if [ -z "$VULN_TOOL" ] || [ ! -x "$VULN_TOOL" ]; then
  echo "缺少 govulncheck，无法执行依赖漏洞扫描" >&2
  exit 1
fi

GO_ROOT="$("$GO_TOOL" env GOROOT)"
GO_LICENSE="$GO_ROOT/LICENSE"
GO_PATENTS="$GO_ROOT/PATENTS"
if [ ! -f "$GO_LICENSE" ] || [ ! -f "$GO_PATENTS" ]; then
  echo "缺少 Go 标准库许可证文件: $GO_ROOT" >&2
  exit 1
fi

verify_frpc_hash() {
  local file="$1"
  local expected="$2"
  local actual
  if [ "$expected" = "PENDING" ]; then
    echo "缺少已冻结的 frpc 二进制 SHA-256: $file" >&2
    exit 1
  fi
  actual="$(sha256sum "$file" | awk '{print $1}')"
  if [ "$actual" != "$expected" ]; then
    echo "frpc 二进制哈希不匹配: $file" >&2
    echo "expected=$expected actual=$actual" >&2
    exit 1
  fi
}

if ! "$native_windows"; then
  verify_frpc_hash "$FRPC_LINUX" "$FRPC_LINUX_SHA256"
  verify_frpc_hash "$FRPC_WINDOWS" "$FRPC_WINDOWS_SHA256"
fi

rm -rf "$DIST"
for target in "${targets[@]}"; do
  mkdir -p "$DIST/$target/"{bin,docs,config,compliance/go-deps}
done

export CGO_ENABLED=0
export GOFLAGS=-trimpath

BUILD_LOG="$DIST/build.log"
{
  echo "Kaiwu connector build"
  echo "version=$VERSION"
  echo "commit=$COMMIT"
  echo "build_time=$BUILD_TIME"
  echo "go_version=$("$GO_TOOL" version)"
  echo "go_root=$GO_ROOT"
  echo "host=$(uname -a)"
  echo "cyclonedx_gomod=$("$SBOM_TOOL" version 2>&1 | tr '\n' ' ')"
  echo "govulncheck=$("$VULN_TOOL" -version 2>&1 | tr '\n' ' ')"
  if "$native_windows"; then
    echo "transport=native-wss"
  else
    echo "frpc_linux_sha256=$(sha256sum "$FRPC_LINUX" | awk '{print $1}')"
    echo "frpc_windows_sha256=$(sha256sum "$FRPC_WINDOWS" | awk '{print $1}')"
  fi
} > "$BUILD_LOG"

echo "构建 Windows 版本..."
build_windows_executable "$DIST/windows/bin/kaiwu-connector.exe"

if "$native_windows"; then
  printf 'native-wss\n' > "$DIST/windows/transport.txt"
else
  echo "构建 Linux 版本..."
  GOOS=linux GOARCH=amd64 "$GO_TOOL" build -trimpath -buildvcs=false -ldflags "$LDFLAGS" -o "$DIST/linux/bin/kaiwu-connector" "$ROOT/cmd/connector"
  cp "$FRPC_LINUX" "$DIST/linux/bin/frpc"
  cp "$FRPC_WINDOWS" "$DIST/windows/bin/frpc.exe"
  chmod 0755 "$DIST/linux/bin/kaiwu-connector" "$DIST/linux/bin/frpc"
  mkdir -p "$DIST/linux/systemd"
  cp "$ROOT/packaging/linux/"*.sh "$DIST/linux/"
  cp "$ROOT/packaging/linux/"*.service "$DIST/linux/systemd/"
  cp "$ROOT/packaging/config/connector.env.example" "$DIST/linux/config/"
  cp "$ROOT/docs/"*.md "$DIST/linux/docs/"
  cp "$ROOT/README.md" "$DIST/linux/docs/"
fi
cp "$ROOT/packaging/windows/"*.ps1 "$DIST/windows/"
cp "$ROOT/packaging/windows/connector.ico" "$DIST/windows/"
cp "$ROOT/scripts/windows-smoke.ps1" "$DIST/windows/"
cp "$ROOT/packaging/config/connector.env.example" "$DIST/windows/config/"

# Customer packages must never ship with the old empty public endpoint
# template. Keep the check close to the copy so a future template regression
# fails the build instead of producing a package that cannot reach the edge.
ENV_TEMPLATE="$ROOT/packaging/config/connector.env.example"
CLOUD_BASE_VALUE="$(sed -n 's/^KAIWU_CLOUD_BASE=//p' "$ENV_TEMPLATE" | tail -n 1)"
EDGE_BASE_VALUE="$(sed -n 's/^KAIWU_EDGE_BASE=//p' "$ENV_TEMPLATE" | tail -n 1)"
for value in "$CLOUD_BASE_VALUE" "$EDGE_BASE_VALUE"; do
  if [[ ! "$value" =~ ^https://[^[:space:]]+$ ]]; then
    echo "公共端点模板必须预配置 HTTPS 地址: $ENV_TEMPLATE" >&2
    exit 1
  fi
done
if "$native_windows"; then
  sed "s/<版本号>/${VERSION}/g" "$ROOT/docs/内置中继交付说明.md" > "$DIST/windows/docs/交付与运维说明.md"
else
  for target in "${targets[@]}"; do
    sed "s/<版本号>/${VERSION}/g" "$ROOT/docs/交付与运维说明.md" > "$DIST/$target/docs/交付与运维说明.md"
  done
fi

for target in "${targets[@]}"; do
  cp "$ROOT/third_party/README.md" "$DIST/$target/compliance/THIRD_PARTY_NOTICES.md"
  if ! "$native_windows"; then
    cp "$ROOT/third_party/frp/LICENSE" "$DIST/$target/compliance/frp-LICENSE"
    cp "$ROOT/third_party/frp/NOTICE.txt" "$DIST/$target/compliance/frp-NOTICE.txt"
    cp "$ROOT/third_party/frp/OFFICIAL_ARCHIVE_CHECKSUMS.txt" "$DIST/$target/compliance/frp-OFFICIAL_ARCHIVE_CHECKSUMS.txt"
    cp "$ROOT/third_party/frp/OFFICIAL_BINARY_SHA256.txt" "$DIST/$target/compliance/frp-OFFICIAL_BINARY_SHA256.txt"
  fi
  cp "$ROOT/third_party/mcp-go-sdk/LICENSE" "$DIST/$target/compliance/mcp-go-sdk-LICENSE.txt"
  cp -a "$ROOT/third_party/go-deps/." "$DIST/$target/compliance/go-deps/"
  cp "$GO_LICENSE" "$DIST/$target/compliance/go-LICENSE.txt"
  cp "$GO_PATENTS" "$DIST/$target/compliance/go-PATENTS.txt"
  cp "$BUILD_LOG" "$DIST/$target/compliance/BUILD_LOG.txt"
done

(
  cd "$ROOT"
  "$GO_TOOL" list -m -json all
) | "$PYTHON_TOOL" -c '
import json
import sys

text = sys.stdin.read()
decoder = json.JSONDecoder()
offset = 0
while offset < len(text):
    while offset < len(text) and text[offset].isspace():
        offset += 1
    if offset >= len(text):
        break
    item, offset = decoder.raw_decode(text, offset)
    item.pop("Dir", None)
    item.pop("GoMod", None)
    print(json.dumps(item, ensure_ascii=False, sort_keys=True))
' > "$metadata_dir/GoModules.json"

(
  cd "$ROOT"
  "$SBOM_TOOL" mod -licenses -assert-licenses -noserial -type application -std \
    -output-version 1.5 -json -output "$metadata_dir/SBOM.cdx.json" . \
    2> "$metadata_dir/SBOM-WARNINGS.raw.txt"
)
"$PYTHON_TOOL" - "$metadata_dir/SBOM-WARNINGS.raw.txt" "$metadata_dir/SBOM-WARNINGS.txt" <<'PY'
import re
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
lines = []
for line in source.read_text(encoding="utf-8").splitlines():
    line = re.sub(r"^\d{1,2}:\d{2}(?:AM|PM)\s+", "", line).rstrip()
    if line.startswith("WRN failed to determine version of main module "):
        continue
    if line:
        lines.append(line)
target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
source.unlink()
PY

(
  cd "$ROOT"
  "$VULN_TOOL" ./...
) > "$metadata_dir/GOVULNCHECK.txt" 2>&1

"$PYTHON_TOOL" - "$metadata_dir/SBOM.cdx.json" "$VERSION" "$COMMIT" "$BUILD_TIME" <<'PY'
import json
import sys

path = sys.argv[1]
release_version = sys.argv[2]
release_commit = sys.argv[3]
build_time = sys.argv[4]
with open(path, encoding="utf-8") as handle:
    document = json.load(handle)

if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.5":
    raise SystemExit("SBOM 格式或版本不符合冻结要求")

components = {item.get("name"): item for item in document.get("components", [])}
std = components.get("std")
if not std:
    raise SystemExit("SBOM 缺少 Go 标准库组件")
std["licenses"] = [{"license": {"id": "BSD-3-Clause"}}]

metadata = document.get("metadata") or {}
metadata["timestamp"] = build_time
metadata_component = metadata.get("component")
if not metadata_component:
    raise SystemExit("SBOM 缺少应用元数据")
metadata_component["licenses"] = [{"expression": "NOASSERTION"}]
metadata_component["version"] = release_version
metadata_component["properties"] = [
    {"name": "kaiwu:build:commit", "value": release_commit},
]
for field in ("bom-ref", "purl"):
    value = metadata_component.get(field, "")
    if value:
        base, separator, query = value.partition("?")
        metadata_component[field] = f"{base}@{release_version}"
        if separator:
            metadata_component[field] += separator + query

with open(path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

cat > "$metadata_dir/SBOM-NOTES.txt" <<'EOF'
The SBOM generator does not detect the Go standard library license from the
toolchain. The build injects BSD-3-Clause for the `std` component and includes
the pinned Go LICENSE and PATENTS files in this package. The connector
application's own licensing status is marked NOASSERTION and is not inferred
from unrelated files.
EOF

(cd "$DIST/windows" && "$GO_TOOL" version -m ./bin/kaiwu-connector.exe) > "$DIST/windows/compliance/BinaryBuildInfo.txt"
components="kaiwu-connector（内置 MCP 中继）、Go 1.27.4 标准库（BSD License）"
if ! "$native_windows"; then
  cp "$metadata_dir/"{GoModules.json,SBOM.cdx.json,SBOM-WARNINGS.txt,SBOM-NOTES.txt,GOVULNCHECK.txt} "$DIST/windows/compliance/"
  (cd "$DIST/linux" && "$GO_TOOL" version -m ./bin/kaiwu-connector) > "$DIST/linux/compliance/BinaryBuildInfo.txt"
  (cd "$DIST/linux" && file ./bin/frpc) > "$DIST/linux/compliance/frpc-BinaryInfo.txt"
  (cd "$DIST/windows" && file ./bin/frpc.exe) > "$DIST/windows/compliance/frpc-BinaryInfo.txt"
  (cd "$DIST/linux" && sha256sum ./bin/frpc) > "$DIST/linux/compliance/frpc-SHA256.txt"
  (cd "$DIST/windows" && sha256sum ./bin/frpc.exe) > "$DIST/windows/compliance/frpc-SHA256.txt"
  components="kaiwu-connector、frpc 0.61.1（Apache License 2.0）、Go 1.27.4 标准库（BSD License）"
fi

for target in "${targets[@]}"; do
  cat > "$DIST/$target/版本信息.txt" <<EOF
开物内网连接器 ${VERSION}
提交：${COMMIT}
构建时间：${BUILD_TIME}
包含：${components}
EOF

  # ZIP records local file times. Normalize every package entry first, then
  # normalize the checksum file itself after it has been generated.
  find "$DIST/$target" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
  (
    cd "$DIST/$target"
    LC_ALL=C find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS
    touch -d "@${SOURCE_DATE_EPOCH}" SHA256SUMS
  )
done

artifacts=("$windows_name.zip" "$windows_name-setup.exe")
if ! "$native_windows"; then
  linux_name="kaiwu-connector-${VERSION}-linux-amd64.tar.gz"
  (
    cd "$DIST/linux"
    # gzip -n keeps the archive header free of the build timestamp.
    TZ=UTC LC_ALL=C tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner -cf - . \
      | gzip -n > "$DIST/$linux_name"
  )
  artifacts=("$linux_name" "${artifacts[@]}")
fi
# Zip entry names must carry the UTF-8 flag, otherwise Windows Explorer decodes
# docs/交付与运维说明.md and 版本信息.txt with the local ANSI codepage and shows
# garbled names. The writer also pins timestamps for reproducible archives.
"$PYTHON_TOOL" "$ROOT/scripts/make-zip.py" \
  "$DIST/windows" "$DIST/$windows_name.zip" "$SOURCE_DATE_EPOCH"

"$ROOT/scripts/build-windows-installer.sh" \
  "$DIST/windows" \
  "$VERSION" \
  "$COMMIT" \
  "$DIST/$windows_name-setup.exe" \
  "$SOURCE_DATE_EPOCH"

(
  cd "$DIST"
  sha256sum "${artifacts[@]}" > SHA256SUMS
  echo "完成："
  ls -lh "${artifacts[@]}" SHA256SUMS
)
