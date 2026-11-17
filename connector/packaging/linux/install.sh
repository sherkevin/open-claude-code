#!/usr/bin/env bash
# 开物内网连接器 Linux 安装脚本。
# 用法：sudo ./install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行：sudo $0" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR=/opt/kaiwu-connector
CONFIG_DIR=/etc/kaiwu-connector
DATA_DIR=/var/lib/kaiwu-connector
SERVICE_FILE=/etc/systemd/system/kaiwu-connector.service
SERVICE_USER=kaiwu-connector

if [ ! -f "$SRC/SHA256SUMS" ]; then
  echo "安装包缺少 SHA256SUMS，拒绝安装" >&2
  exit 1
fi
(
  cd "$SRC"
  sha256sum -c SHA256SUMS
) || {
  echo "安装包文件校验失败，拒绝安装" >&2
  exit 1
}

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -m 0755 "$INSTALL_DIR" "$INSTALL_DIR/bin" "$INSTALL_DIR/docs" "$INSTALL_DIR/compliance"
install -m 0755 "$SRC/bin/kaiwu-connector" "$INSTALL_DIR/bin/kaiwu-connector"
install -m 0755 "$SRC/bin/frpc" "$INSTALL_DIR/bin/frpc"
install -m 0644 "$SRC"/docs/*.md "$INSTALL_DIR/docs/" 2>/dev/null || true
install -m 0644 "$SRC"/compliance/* "$INSTALL_DIR/compliance/" 2>/dev/null || true

install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/connector.env" ]; then
  install -m 0640 -o root -g "$SERVICE_USER" "$SRC/config/connector.env.example" "$CONFIG_DIR/connector.env"
  echo "已写入配置模板：$CONFIG_DIR/connector.env，请按交付人员提供的地址确认。"
fi

install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"

install -m 0644 "$SRC/systemd/kaiwu-connector.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now kaiwu-connector.service

echo
echo "安装完成。"
echo "本机配置界面：http://127.0.0.1:17872/"
echo "查看状态：systemctl status kaiwu-connector"
echo "查看日志：journalctl -u kaiwu-connector -f"
echo
echo "注意：本机界面只监听回环地址；如需远程操作，请通过 SSH 端口转发，不要开放到公网。"
