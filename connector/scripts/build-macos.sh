#!/usr/bin/env bash
# 在 macOS 上构建全部交付物（Linux tar.gz、Windows zip、Windows setup.exe）。
#
# 发布脚本按 GNU 工具链编写（date -d、sha256sum、find -printf、tar --sort、unshare），
# 这里只把 Homebrew 提供的同名 GNU 工具放进 PATH，然后原样调用 scripts/build.sh，
# 不改动发布脚本的语义或产物内容。
#
# 依赖：brew install coreutils findutils gnu-tar nsis mingw-w64
#       go install github.com/CycloneDX/cyclonedx-gomod/cmd/cyclonedx-gomod@latest
#       go install golang.org/x/vuln/cmd/govulncheck@latest
#       connector/packaging/bin/ 下的 frpc 0.61.1 两个二进制
#
# 宿主机 makensis 不可用（Homebrew 的 arm64 3.13 在编译含 MUI2 的脚本时会以
# std::bad_alloc 崩溃）时，自动改用 Docker 里的 makensis。镜像名可用 NSIS_IMAGE 覆盖，
# 默认 kaiwu-nsis:bookworm，本地缺失时按需构建。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NSIS_IMAGE="${NSIS_IMAGE:-kaiwu-nsis:bookworm}"

if ! command -v brew >/dev/null 2>&1; then
  echo "需要 Homebrew：brew install coreutils findutils gnu-tar nsis mingw-w64" >&2
  exit 1
fi
PREFIX="$(brew --prefix)"
for dir in opt/coreutils/libexec/gnubin opt/findutils/libexec/gnubin opt/gnu-tar/libexec/gnubin; do
  if [[ -d "$PREFIX/$dir" ]]; then
    PATH="$PREFIX/$dir:$PATH"
  fi
done
export PATH

missing=()
for tool in go python3 i686-w64-mingw32-gcc sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
[[ -n "${CYCLONEDX_GOMOD:-}" ]] || command -v cyclonedx-gomod >/dev/null 2>&1 || missing+=("cyclonedx-gomod")
[[ -n "${GOVULNCHECK:-}" ]] || command -v govulncheck >/dev/null 2>&1 || missing+=("govulncheck")
if ((${#missing[@]})); then
  printf '缺少构建依赖: %s\n' "${missing[*]}" >&2
  echo "brew install coreutils findutils gnu-tar nsis mingw-w64" >&2
  echo "go install github.com/CycloneDX/cyclonedx-gomod/cmd/cyclonedx-gomod@latest" >&2
  echo "go install golang.org/x/vuln/cmd/govulncheck@latest" >&2
  exit 1
fi

# 交付包固定用 Go 1.27.4 构建（与 版本信息.txt 一致）。Go 会按需拉取该工具链，
# 同时保证 $GOROOT 下存在构建脚本要求的 LICENSE / PATENTS。子进程（go list、
# govulncheck 的加载器）必须看到同一个工具链，否则会按旧版本解析新语法而失败。
GO_PINNED="go1.27.4"
if ! go version 2>/dev/null | grep -q "$GO_PINNED"; then
  if ! GO_TOOLCHAIN_DIR="$(GOTOOLCHAIN="$GO_PINNED" go env GOROOT 2>/dev/null)" ||
     [[ ! -x "$GO_TOOLCHAIN_DIR/bin/go" ]]; then
    echo "无法获取 $GO_PINNED 工具链" >&2
    exit 1
  fi
  PATH="$GO_TOOLCHAIN_DIR/bin:$PATH"
  export PATH
fi
export GOTOOLCHAIN=local

# macOS 没有 GNU date -d，把源时间戳固定成提交时间，避免依赖主机时钟。
# 这里的 date 来自 coreutils，支持 -d "@<epoch>"。
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}"
export SOURCE_DATE_EPOCH
export BUILD_TIME="${BUILD_TIME:-$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)}"

# makensis 自检：需要同时覆盖 MUI2 与 File，才能暴露宿主机 NSIS 的崩溃。
nsis_selftest() {
  local compiler="$1" dir
  dir="$(mktemp -d "${TMPDIR:-/tmp}/kaiwu-nsis-selftest.XXXXXX")" || return 1
  cat > "$dir/probe.nsi" <<'PROBE'
OutFile "PROBE_OUTPUT"
!include MUI2.nsh
SetCompressor /SOLID lzma
Section "probe"
  SetOutPath "$TEMP\kaiwu-probe"
  File "${__FILEDIR__}/probe.nsi"
SectionEnd
PROBE
  sed "s|PROBE_OUTPUT|$dir/probe.exe|" "$dir/probe.nsi" > "$dir/probe.out.nsi"
  if ( "$compiler" -V2 -NOCD "$dir/probe.out.nsi" ) >/dev/null 2>&1 && [[ -s "$dir/probe.exe" ]]; then
    rm -rf -- "$dir"
    return 0
  fi
  rm -rf -- "$dir"
  return 1
}

HOST_MAKENSIS="$(command -v makensis || true)"
if [[ -z "$HOST_MAKENSIS" ]] || ! nsis_selftest "$HOST_MAKENSIS"; then
  echo "宿主机 makensis 不可用，改用 Docker 中的 NSIS：$NSIS_IMAGE" >&2
  if ! command -v docker >/dev/null 2>&1; then
    echo "需要 Docker，或安装可用的 NSIS 后设置 MAKENSIS 指向它" >&2
    exit 1
  fi
  if ! docker image inspect "$NSIS_IMAGE" >/dev/null 2>&1; then
    echo "本地缺少镜像 $NSIS_IMAGE，正在构建（需要访问 Docker 镜像源）" >&2
    docker build -q -t "$NSIS_IMAGE" - <<'DOCKERFILE'
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends nsis \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
  fi
  wrapper_dir="$(mktemp -d "${TMPDIR:-/tmp}/kaiwu-makensis.XXXXXX")"
  wrapper="$wrapper_dir/makensis"
  # 中文文件名需要 C.UTF-8：POSIX 版 makensis 在 C locale 下处理中文名会崩溃。
  cat > "$wrapper" <<WRAPPER
#!/usr/bin/env bash
set -euo pipefail
exec docker run --rm --entrypoint makensis \\
  --user "\$(id -u):\$(id -g)" \\
  -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 \\
  -v "$ROOT":"$ROOT" -v /tmp:/tmp -v /private/tmp:/private/tmp \\
  -w "$ROOT" "$NSIS_IMAGE" "\$@"
WRAPPER
  chmod +x "$wrapper"
  export MAKENSIS="$wrapper"
  # makensis 的临时目录必须落在挂载进来的路径里。
  export NSIS_TMPDIR="$ROOT/dist/nsis-work"
fi

exec "$ROOT/scripts/build.sh" "$@"
