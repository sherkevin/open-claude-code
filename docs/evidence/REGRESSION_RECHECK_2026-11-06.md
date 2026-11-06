# 学校云接手复核：回归环境记录

**日期：** 2026-11-06（Asia/Shanghai）  
**项目：** open-claude-code / 知行工作台  
**目的：** 接替会话 `01a1af39-f0d7-7b03-aa48-e4552a4059a1` 后，校准本机工作树与远端交接记录的测试口径。

## 环境边界

- 工作目录：`/media/data3/dengkw/projects/open-claude-code`
- Git：复核开始时 `main` 与 `origin/main` 一致，基线提交为 `a4fe9f8`；本轮新增的产品源码、测试和审计文档均保持未提交。
- 本次只在学校云副本检查，不访问生产数据、凭据或在线客户端构建目录；测试依赖只写入临时目录和被忽略的本地 `client/node_modules`。
- 远端记录中的服务端 322 项、客户端 53 项通过，是开发树历史实测；不能写成本机本轮重跑结果。

## 命令与结果

### 服务端

```bash
cd server
../.venv/bin/python -m pytest -q --basetemp=/tmp/zhixing-t0-server
../.venv/bin/python -m compileall app
```

结果：未执行成功。学校云副本没有项目内 `.venv`，系统 Python 也缺少 `fastapi`，因此无法收集测试。此前直接执行 `python3 -m pytest -q` 的收集阶段错误为 `ModuleNotFoundError: No module named 'fastapi'`；没有产生项目测试通过数。

### 客户端

```bash
cd client
npm test
npx tsc -b
npx vite build --outDir /tmp/zhixing-t0-client --emptyOutDir
```

结果：未执行成功。学校云副本没有 `client/node_modules`，因此 `vitest` 和 `tsc` 均不可用；没有产生客户端测试、TypeScript 或构建通过数。

## 对账结论

- 本机复核状态是 **环境阻塞，非代码失败**。本轮不能用本机结果推翻或重述远端历史的 322/53 数字。
- 远端历史证据仍以 [SERVER_REGRESSION_2026-11-06](SERVER_REGRESSION_2026-11-06.md) 和 [CLIENT_REGRESSION_2026-11-06](CLIENT_REGRESSION_2026-11-06.md) 为准；它们记录的是远端开发树，不是学校云副本当前环境。
- 若要重新取得可重复的本机数字，需要用户批准安装依赖或提供可用的项目虚拟环境；安装依赖不属于本轮只读接手校准。

## 后续验证附录（本轮继续执行）

用户要求继续完成工程后，本轮没有写入项目虚拟环境，也没有连接生产服务；仅在 `/tmp/open-claude-pylib` 安装了测试用 Python 依赖，并在 `client/` 执行了锁定版本的 `npm ci`。因此下面的数字是学校云工作树当前代码的可重复验证结果，但 Python 依赖路径是临时目录，不能当作已经建立项目 `.venv`。

```bash
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests
# 326 passed, 1 warning

cd client
npm test -- --run
# 11 files, 57 passed
npx tsc -b
npx vite build --outDir /tmp/open-claude-client-dist
# build succeeded; production output stayed outside client/dist
```

新增的知识库图谱、XLSX、自动路由和审批审计测试包含在 326 项服务端通过结果内；客户端新增的合成对标页面测试包含在 57 项通过结果内。警告仍是 Starlette/httpx 的第三方弃用提示；没有失败用例。生产部署、systemd 重启、真实客户 MCP、真实模型和付费上游仍未执行。

## 最终优先级 3 回归附录（本轮）

在加入高风险管理员/双人审批 4 项专项测试、模型代审陈旧授权 1 项测试以及客户端审批进度字段后，重新执行：

```bash
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests
# 340 passed, 1 warning, 0 failed
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m py_compile server/app/*.py
# pass
cd client && npm test -- --run
# 11 files, 57 passed
npx tsc -b
# pass
```

唯一警告仍为临时测试依赖发出的第三方 Starlette/httpx 弃用提示。没有执行 Vite 线上构建、生产部署、服务重启、真实客户调用、凭据读取或未授权付费模型调用。
## 12. 继续收口回归（2026-11-07，学校云）

本节覆盖优先级 2/3 收口后的最新工作树，不覆盖生产环境、真实客户数据、凭据、付费模型或线上构建。

```text
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests
358 passed, 1 warning

cd client && npm test -- --run
12 files / 61 passed

cd client && npx tsc -b
passed

PYTHONPATH=/tmp/open-claude-pylib:server python3 -m py_compile server/app/*.py
passed

git diff --check
passed

python3 -m unittest discover -s examples -p 'test_synthetic_benchmark.py' -q
5 passed
```

唯一警告来自临时测试依赖中的 Starlette/httpx 弃用提示，不来自项目源码。全量服务端回归约 10 分 32 秒；客户端未执行生产构建。当前仍只证明本地合成协议、审批和子任务边界，真实上游兼容性、模型质量/成本、客户角色和生产负载仍未验收。
