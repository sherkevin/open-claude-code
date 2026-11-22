# 开物用户归档

每个用户使用一个 PostgreSQL 逻辑数据库，库名和只读角色由开物内部用户 ID 派生。一个 PostgreSQL 实例承载这些库，用户改名时数据库不变。归档库保存账号基础资料、操作与用量元数据、结构化审核规范版本。归档不读取密码散列、会话令牌、对话正文或工具参数。全局规范仍由开物应用维护。

采用 PostgreSQL 17 是因为它支持事务、并发写入、每库连接授权和逐库备份恢复。SQLite 继续承担现有应用的登录和业务状态。一次归档从源数据库的同一只读事务读取，按用户提交目标事务。策略以版本号保存，操作以事件 ID 去重；新版审核模块在保存用户规范的同一事务内追加版本历史。旧版本如果只保留当前规范，归档也只能取得现存的版本。尚未结束的模型调用和任务等结束后再归档。

同一实例的库共享 CPU、内存、磁盘与系统管理员。按库隔离权限也意味着跨用户统计需要独立的汇总接口。正式接入在线后台时，需要为它设置有限的连接数和单独的只读角色，并验证生产负载、保留周期、备份目标与故障切换。本次是按需归档，应用在线读写和公网运营后台尚未切到归档库。

## 安装和运行

Ubuntu 22.04 amd64 的隔离部署使用 PostgreSQL 社区 PGDG 仓库的 PostgreSQL 17、17 客户端和 libpq 包。通过签名的包索引校验后，将这三个包解包到专用目录 `/opt/kaiwu-user-archives/runtime`。将本目录的 `initialize.sh` 安装到 `/opt/kaiwu-user-archives/`，将 systemd 单元安装到 `/etc/systemd/system/`。启动前使用 `systemd-analyze verify` 校验单元。实例只监听 `/run/kaiwu-user-archives` 内的 Unix socket，使用操作系统账号认证。数据目录和 socket 目录权限为 `0700`。

在独立 Python 环境安装归档工具的依赖。归档方通过 PostgreSQL 的标准连接环境变量或服务配置指定实例，建库由专用归档管理员执行。

```bash
python3 -m pip install -r deploy/kaiwu-user-archives/requirements.txt
PGHOST=/run/kaiwu-user-archives PGPORT=55444 PGDATABASE=postgres PGUSER=kaiwu-user-archives \
  PYTHONPATH=server python3 scripts/kaiwu_user_storage.py /path/to/app.sqlite3 --user EXISTING_USER
```

归档工具可以处理 SQLite 一致快照，也可以在同主机上直接打开只读视图。命令仅输出归档计数和错误类别。不同数据库分别提交，重复执行可补齐未完成的账号。使用同版本的 `pg_dump --format=custom` 备份单库，用 `pg_restore --list` 检查包目录并在隔离实例演练恢复。

## 验证

```bash
sh deploy/kaiwu-user-archives/test-local.sh /path/to/postgresql/17/bin
PYTHONPATH=server python3 -m pytest -q -c server/pyproject.toml server/tests/test_prompt_safety.py
```

本地 PostgreSQL 17.12 的合成测试覆盖两个用户库、只读角色越权失败、拒绝错库写入、规范版本归档以及备份恢复。16 个并发事务写入 640 条独立事件并重放同一批 640 条事件，实际新增 640 条，整组耗时 0.293 秒。此数据用于本次回归，生产容量需另测。

参考 [PostgreSQL 库和角色隔离](https://www.postgresql.org/docs/17/manage-ag-overview.html)、[Ubuntu 官方仓库](https://www.postgresql.org/download/linux/ubuntu/)和 [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)。
