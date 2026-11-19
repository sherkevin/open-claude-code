#!/usr/bin/env bash
# 构建开物内网连接器的交付包。
#
# 依赖：Go 1.22+、frpc 二进制（packaging/bin/ 下）、zip。
# 产物：dist/kaiwu-connector-<版本>-linux-amd64.tar.gz
#       dist/kaiwu-connector-<版本>-windows-amd64.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${VERSION:-1.3.2}"
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

for file in "$FRPC_LINUX" "$FRPC_WINDOWS"; do
  if [ ! -f "$file" ]; then
    echo "缺少 frpc 二进制: $file" >&2
    exit 1
  fi
done

if [ -z "$SBOM_TOOL" ] || [ ! -x "$SBOM_TOOL" ]; then
  echo "缺少 cyclonedx-gomod，无法生成受控试装 SBOM" >&2
  exit 1
fi
if [ -z "$VULN_TOOL" ] || [ ! -x "$VULN_TOOL" ]; then
  echo "缺少 govulncheck，无法执行依赖漏洞扫描" >&2
  exit 1
fi
if [ -z "$GO_TOOL" ] || [ ! -x "$GO_TOOL" ]; then
  echo "缺少 Go 工具链，无法构建连接器" >&2
  exit 1
fi
if [ -z "$PYTHON_TOOL" ] || [ ! -x "$PYTHON_TOOL" ]; then
  echo "缺少 python3，无法规范化 SBOM 许可证声明" >&2
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

verify_frpc_hash "$FRPC_LINUX" "$FRPC_LINUX_SHA256"
verify_frpc_hash "$FRPC_WINDOWS" "$FRPC_WINDOWS_SHA256"

rm -rf "$DIST"
mkdir -p "$DIST/linux/bin" "$DIST/linux/docs" "$DIST/linux/systemd" "$DIST/linux/config" "$DIST/linux/compliance" \
         "$DIST/windows/bin" "$DIST/windows/docs" "$DIST/windows/config" "$DIST/windows/compliance"

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
  echo "frpc_linux_sha256=$(sha256sum "$FRPC_LINUX" | awk '{print $1}')"
  echo "frpc_windows_sha256=$(sha256sum "$FRPC_WINDOWS" | awk '{print $1}')"
} > "$BUILD_LOG"

echo "构建 Linux 版本..."
GOOS=linux GOARCH=amd64 "$GO_TOOL" build -trimpath -buildvcs=false -ldflags "$LDFLAGS" -o "$DIST/linux/bin/kaiwu-connector" "$ROOT/cmd/connector"

echo "构建 Windows 版本..."
GOOS=windows GOARCH=amd64 "$GO_TOOL" build -trimpath -buildvcs=false -ldflags "$LDFLAGS" -o "$DIST/windows/bin/kaiwu-connector.exe" "$ROOT/cmd/connector"

cp "$FRPC_LINUX" "$DIST/linux/bin/frpc"
cp "$FRPC_WINDOWS" "$DIST/windows/bin/frpc.exe"
chmod 0755 "$DIST/linux/bin/kaiwu-connector" "$DIST/linux/bin/frpc"

cp "$ROOT/packaging/linux/"*.sh "$DIST/linux/"
cp "$ROOT/packaging/linux/"*.service "$DIST/linux/systemd/"
cp "$ROOT/packaging/windows/"*.ps1 "$DIST/windows/"
cp "$ROOT/scripts/windows-smoke.ps1" "$DIST/windows/"
cp "$ROOT/packaging/config/connector.env.example" "$DIST/linux/config/"
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
cp "$ROOT/docs/"*.md "$DIST/linux/docs/"
cp "$ROOT/docs/"*.md "$DIST/windows/docs/"
cp "$ROOT/README.md" "$DIST/linux/docs/"
cp "$ROOT/README.md" "$DIST/windows/docs/"

for target in linux windows; do
  cp "$ROOT/third_party/README.md" "$DIST/$target/compliance/THIRD_PARTY_NOTICES.md"
  cp "$ROOT/third_party/frp/LICENSE" "$DIST/$target/compliance/frp-LICENSE"
  cp "$ROOT/third_party/frp/NOTICE.txt" "$DIST/$target/compliance/frp-NOTICE.txt"
  cp "$ROOT/third_party/frp/OFFICIAL_ARCHIVE_CHECKSUMS.txt" "$DIST/$target/compliance/frp-OFFICIAL_ARCHIVE_CHECKSUMS.txt"
  cp "$ROOT/third_party/frp/OFFICIAL_BINARY_SHA256.txt" "$DIST/$target/compliance/frp-OFFICIAL_BINARY_SHA256.txt"
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
' > "$DIST/linux/compliance/GoModules.json"
cp "$DIST/linux/compliance/GoModules.json" "$DIST/windows/compliance/GoModules.json"

(
  cd "$ROOT"
  "$SBOM_TOOL" mod -licenses -assert-licenses -noserial -type application -std \
    -output-version 1.5 -json -output "$DIST/linux/compliance/SBOM.cdx.json" . \
    2> "$DIST/linux/compliance/SBOM-WARNINGS.raw.txt"
)
"$PYTHON_TOOL" - "$DIST/linux/compliance/SBOM-WARNINGS.raw.txt" "$DIST/linux/compliance/SBOM-WARNINGS.txt" <<'PY'
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
) > "$DIST/linux/compliance/GOVULNCHECK.txt" 2>&1

"$PYTHON_TOOL" - "$DIST/linux/compliance/SBOM.cdx.json" "$VERSION" "$COMMIT" "$BUILD_TIME" <<'PY'
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

cat > "$DIST/linux/compliance/SBOM-NOTES.txt" <<'EOF'
The SBOM generator does not detect the Go standard library license from the
toolchain. The build injects BSD-3-Clause for the `std` component and includes
the pinned Go LICENSE and PATENTS files in this package. The connector
application's own licensing status is marked NOASSERTION and is not inferred
from unrelated files.
EOF

cp "$DIST/linux/compliance/SBOM.cdx.json" "$DIST/windows/compliance/SBOM.cdx.json"
cp "$DIST/linux/compliance/SBOM-WARNINGS.txt" "$DIST/windows/compliance/SBOM-WARNINGS.txt"
cp "$DIST/linux/compliance/SBOM-NOTES.txt" "$DIST/windows/compliance/SBOM-NOTES.txt"
cp "$DIST/linux/compliance/GOVULNCHECK.txt" "$DIST/windows/compliance/GOVULNCHECK.txt"

(cd "$DIST/linux" && "$GO_TOOL" version -m ./bin/kaiwu-connector) > "$DIST/linux/compliance/BinaryBuildInfo.txt"
(cd "$DIST/windows" && "$GO_TOOL" version -m ./bin/kaiwu-connector.exe) > "$DIST/windows/compliance/BinaryBuildInfo.txt"
(cd "$DIST/linux" && file ./bin/frpc) > "$DIST/linux/compliance/frpc-BinaryInfo.txt"
(cd "$DIST/windows" && file ./bin/frpc.exe) > "$DIST/windows/compliance/frpc-BinaryInfo.txt"
(cd "$DIST/linux" && sha256sum ./bin/frpc) > "$DIST/linux/compliance/frpc-SHA256.txt"
(cd "$DIST/windows" && sha256sum ./bin/frpc.exe) > "$DIST/windows/compliance/frpc-SHA256.txt"

cat > "$DIST/linux/版本信息.txt" <<EOF
开物内网连接器 ${VERSION}
提交：${COMMIT}
构建时间：${BUILD_TIME}
包含：kaiwu-connector、frpc 0.61.1（Apache License 2.0）、Go 1.27.4 标准库（BSD License）
EOF
cp "$DIST/linux/版本信息.txt" "$DIST/windows/版本信息.txt"

for target in linux windows; do
  # ZIP records local file times. Normalize every package entry first, then
  # normalize the checksum file itself after it has been generated.
  find "$DIST/$target" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
  (
    cd "$DIST/$target"
    LC_ALL=C find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS
    touch -d "@${SOURCE_DATE_EPOCH}" SHA256SUMS
  )
done

(
  cd "$DIST/linux"
  TZ=UTC LC_ALL=C tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner -czf "$DIST/kaiwu-connector-${VERSION}-linux-amd64.tar.gz" .
)
(
  cd "$DIST/windows"
  LC_ALL=C find . -mindepth 1 -printf '%P\n' | LC_ALL=C sort | TZ=UTC zip -X -q "$DIST/kaiwu-connector-${VERSION}-windows-amd64.zip" -@
)

"$ROOT/scripts/build-windows-installer.sh" \
  "$DIST/windows" \
  "$VERSION" \
  "$COMMIT" \
  "$DIST/kaiwu-connector-${VERSION}-windows-amd64-setup.exe" \
  "$SOURCE_DATE_EPOCH"

(
  cd "$DIST"
  sha256sum \
    kaiwu-connector-"${VERSION}"-linux-amd64.tar.gz \
    kaiwu-connector-"${VERSION}"-windows-amd64.zip \
    kaiwu-connector-"${VERSION}"-windows-amd64-setup.exe > SHA256SUMS
)

echo "完成："
ls -lh "$DIST"/*.tar.gz "$DIST"/*.zip "$DIST"/*.exe "$DIST"/SHA256SUMS
