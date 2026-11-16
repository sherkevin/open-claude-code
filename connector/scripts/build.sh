#!/usr/bin/env bash
# 构建开物内网连接器的交付包。
#
# 依赖：Go 1.22+、frpc 二进制（packaging/bin/ 下）、zip。
# 产物：dist/kaiwu-connector-<版本>-linux-amd64.tar.gz
#       dist/kaiwu-connector-<版本>-windows-amd64.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${VERSION:-1.0.0}"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DIST="$ROOT/dist"
MODULE="github.com/sherkevin/open-claude-code/connector/internal/version"
LDFLAGS="-s -w -X ${MODULE}.Version=${VERSION} -X ${MODULE}.Commit=${COMMIT} -X ${MODULE}.BuildTime=${BUILD_TIME}"

FRPC_LINUX="$ROOT/packaging/bin/frpc-linux-amd64"
FRPC_WINDOWS="$ROOT/packaging/bin/frpc-windows-amd64.exe"

for file in "$FRPC_LINUX" "$FRPC_WINDOWS"; do
  if [ ! -f "$file" ]; then
    echo "缺少 frpc 二进制: $file" >&2
    exit 1
  fi
done

rm -rf "$DIST"
mkdir -p "$DIST/linux/bin" "$DIST/linux/docs" "$DIST/linux/systemd" "$DIST/linux/config" \
         "$DIST/windows/bin" "$DIST/windows/docs" "$DIST/windows/config"

export CGO_ENABLED=0
export GOFLAGS=-trimpath

echo "构建 Linux 版本..."
GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "$LDFLAGS" -o "$DIST/linux/bin/kaiwu-connector" "$ROOT/cmd/connector"

echo "构建 Windows 版本..."
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "$LDFLAGS" -o "$DIST/windows/bin/kaiwu-connector.exe" "$ROOT/cmd/connector"

cp "$FRPC_LINUX" "$DIST/linux/bin/frpc"
cp "$FRPC_WINDOWS" "$DIST/windows/bin/frpc.exe"
chmod 0755 "$DIST/linux/bin/kaiwu-connector" "$DIST/linux/bin/frpc"

cp "$ROOT/packaging/linux/"*.sh "$DIST/linux/"
cp "$ROOT/packaging/linux/"*.service "$DIST/linux/systemd/"
cp "$ROOT/packaging/windows/"*.ps1 "$DIST/windows/"
cp "$ROOT/packaging/config/connector.env.example" "$DIST/linux/config/"
cp "$ROOT/packaging/config/connector.env.example" "$DIST/windows/config/"
cp "$ROOT/docs/"*.md "$DIST/linux/docs/"
cp "$ROOT/docs/"*.md "$DIST/windows/docs/"
cp "$ROOT/README.md" "$DIST/linux/docs/"
cp "$ROOT/README.md" "$DIST/windows/docs/"

cat > "$DIST/linux/版本信息.txt" <<EOF
开物内网连接器 ${VERSION}
提交：${COMMIT}
构建时间：${BUILD_TIME}
包含：kaiwu-connector、frpc 0.61.1（Apache License 2.0）
EOF
cp "$DIST/linux/版本信息.txt" "$DIST/windows/版本信息.txt"

(
  cd "$DIST/linux"
  tar -czf "$DIST/kaiwu-connector-${VERSION}-linux-amd64.tar.gz" .
)
(
  cd "$DIST/windows"
  zip -qr "$DIST/kaiwu-connector-${VERSION}-windows-amd64.zip" .
)

echo "完成："
ls -lh "$DIST"/*.tar.gz "$DIST"/*.zip
