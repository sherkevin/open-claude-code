#!/usr/bin/env bash
# 开物内网连接器 Linux 卸载脚本。
# 用法：sudo ./uninstall.sh [--purge]
#   默认保留 /etc/kaiwu-connector 与 /var/lib/kaiwu-connector；
#   --purge 同时删除本机身份、配置与秘密。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行：sudo $0" >&2
  exit 1
fi

PURGE=0
if [ "${1:-}" = "--purge" ]; then
  PURGE=1
fi

systemctl disable --now kaiwu-connector.service 2>/dev/null || true
rm -f /etc/systemd/system/kaiwu-connector.service
systemctl daemon-reload
rm -rf /opt/kaiwu-connector

if [ "$PURGE" -eq 1 ]; then
  rm -rf /etc/kaiwu-connector /var/lib/kaiwu-connector
  if id kaiwu-connector >/dev/null 2>&1; then
    userdel kaiwu-connector 2>/dev/null || true
  fi
  echo "已删除程序、配置与本机身份。"
else
  echo "已删除程序与服务；配置和本机身份保留在 /etc/kaiwu-connector 与 /var/lib/kaiwu-connector。"
  echo "如需彻底清除，请运行：sudo $0 --purge"
fi
