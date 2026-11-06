# 客户端回归与构建证据（2026-11-06）

## 环境

- 项目：open-claude-code（产品“知行工作台”）
- 服务器：腾讯云
- 工作目录：`/data/dengkw/workspace/open-claude-code/client`
- Node.js：22.14.0（交接记录环境）
- npm：10.9.2
- Vite：6.1.0；Vitest：3.0.5
- `client/node_modules` 在本轮通过一次获授权的腾讯云内网镜像安装恢复，目录属主为 `ubuntu:ubuntu`。
- `client/dist` 是在线 Nginx 静态目录，本轮没有写入；构建输出写入 `/home/ubuntu/client-dist-build-20261106-1348`。

## 依赖恢复

```bash
cd /data/dengkw/workspace/open-claude-code/client
npm ci --registry=https://mirrors.tencentyun.com/npm/ --no-audit --no-fund
```

结果：退出码 **0**，新增 197 个包。`package-lock.json` 前后 SHA-256 均为 `a1430efb7d653504641b6e64ebc8b335d2abc560d9f44bd86220910086e9296f`，未发生锁文件变化。npm 输出日志副本：`/tmp/zhixing-client-npm-ci-20261106.log`。

## 测试与构建

```bash
npm test
npx tsc -b
npx vite build --outDir /home/ubuntu/client-dist-build-20261106-1348 --emptyOutDir
```

结果：

- `npm test`：**10 个测试文件通过，53 个测试通过，退出码 0**；日志副本 `/tmp/zhixing-client-npm-test-20261106.log`。
- `npx tsc -b`：**退出码 0**；日志副本 `/tmp/zhixing-client-tsc-20261106.log`。
- `npx vite build ...`：**退出码 0**，1,855 个模块转换完成，输出包含 `index.html`、JavaScript、CSS、字体和 favicon；日志副本 `/tmp/zhixing-client-vite-build-20261106.log`。
- 生产健康检查仍返回 `{"status":"ready","model_configured":true}`；三个 systemd 服务均为 `active`。本轮没有重启或重新部署服务。

## 证据边界

该结果证明当前开发树的客户端测试、类型检查和独立目录构建通过；它不改变 0.4.2 已发布归档的冻结状态，也不代表真实客户浏览器、业务 MCP、生产负载或容器部署已经验收。
