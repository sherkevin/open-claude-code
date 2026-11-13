# 运维手册

适用于当前单客户、单 server 进程部署。以下 Linux 命令从同一版本的项目根目录执行；先按根 README 安装依赖、配置私有环境变量并创建管理员。示例路径须改成部署实际路径。不要同时启动两个 server 进程使用同一份数据。

## 启动、健康和日志

2026-11-11 起，当前源码中新会话默认执行器为 DeepSeek Harness，旧会话继续使用存储的执行器。本轮没有部署或重启现有服务。采用本版前须按 [Harness 操作](operations/HARNESS_PILOT.md) 显式配置固定源码、专用 Node 路径、API Key 兼容接口和支持的推理档位；仅启动 Python 或看到 `health/ready` 成功不代表 Harness 可以回答。完整功能迁移尚未结束，不能据此直接上线；显式 `AGENT_RUNTIME_TYPE=claude` 仅回滚之后创建的会话默认，不转换既有 Harness 历史。

原生部署由现有进程管理器托管以下命令；手工排查时也可在前台运行，按 Ctrl+C 优雅退出：

```bash
export AGENT_PROVIDER_SETTINGS=/absolute/private/path/provider-settings.json
export PYTHONPATH=server
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

启动前必须显式提供已授权的 provider settings，或在进程环境中提供支持的模型凭据；不要依赖窗口或宿主用户的隐式配置。先检查 `health/ready` 的 `model_configured=true`，再开放聊天入口。若为本机开发且已授权使用 Claude Code settings，可将 `AGENT_PROVIDER_SETTINGS` 指向受控的本机 settings 文件；不要把该文件复制进仓库、客户端或日志。

必须保持 `--workers 1`，不要使用生产热重载。容器部署使用 `docker compose up -d`，停机使用 `docker compose stop`；开发预览使用 `scripts/preview.py start/status/stop`，不要混用三种启动方式。

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/health/live
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/health/ready
# 容器日志：
docker compose logs --tail 100 server client
```

`live` 返回 `status: ok`；`ready` 查询数据库并返回 `status: ready` 和 `model_configured`。后者只检查是否配置模型凭据，不验证凭据有效性、模型余额、MCP 连通性或推理成功。开发预览 API 端口为 18080；容器部署时请从客户端入口 `http://127.0.0.1:8080/api/v1/health/ready` 手工检查 ready。`compose.yaml` 当前没有配置容器健康检查（healthcheck），不会自动判定容器是否就绪。

原生进程日志在 stdout/stderr，由进程管理器收集与轮转；服务不会自动向数据目录的 `logs/` 写运行日志。开发预览日志为 `.runtime/server.log`、`.runtime/client.log`、`.runtime/mcp.log`，Nginx 日志在 `.runtime/nginx/`。定位问题先保留运行 ID、时间、状态和界面的轨迹下载。日志及轨迹可能包含业务内容，只在授权范围保存，不公开粘贴完整环境、请求认证头或会话文件。

## 配置变更与撤权

从0.3.0起支持 `AGENT_MODEL_CATALOG` 指向服务器上的私有 JSON。复制 `server/model-catalog.example.json` 到受控配置目录，填入实际已授权的上游型号并启用，权限设为0600；客户端仅接收公共 ID、名称、可选推理级别及会话切换能力标记。`AGENT_DEFAULT_MODEL_ID` 可覆盖新会话默认项。Dano 的公开档位 light/high/max 在私有目录中映射为上游 low/high/max；具体线路保留在内部审计中。

GLM-5.3 目录项保持 `requires_new_conversation_on_switch:true`。已有原生会话时，切入或切出带此标记的模型都会返回 `409 MODEL_NEW_CONVERSATION_REQUIRED`，提示新建对话；旧的公开别名若对应同一 GLM 上游线路也受约束。空对话尚未形成原生会话时可正常选择 GLM，同模型调整推理级别以及 Dano 与 DeepSeek 之间的切换仍可用。客户端使用 `has_native_session` 和目录能力标记禁用受限选择；服务端独立执行校验。新对话使用独立的空记忆，原会话、记忆和附件保留。这个边界来自当前兼容线路的跨模型历史验收结果，后续放开需要重新验证实际连续性与完成时间。

模型目录在 server 启动时加载，更新后正常重启；先等待活动任务完成。会话保存模型、级别和 provider 配置摘要，运行另存独立快照。全局或单模型 provider 配置变更后，旧会话返回 `409 MODEL_SELECTION_REQUIRED`，需要用户点击**重新连接当前模型**，明确采用更新后的服务配置。按钮提交相同公共模型与级别，不自动发送消息或重放旧任务；连接成功后由用户手动继续。GLM 会话也可重新连接同一 GLM 选项，无需跨模型切换。若原选项已不可用或属于未列出的历史模型，用户需选择可用选项，并遵守 GLM 新对话边界。旧版本未记录过凭据摘要的会话只能在升级时补记当前摘要，不代表能追溯旧凭据。数据库快照不保存模型密钥明文。

升级时保留原 `AGENT_MODEL` 和 `AGENT_EFFORT`，用于旧会话迁移；默认目录项仅影响新会话。完整备份须包含私有模型目录和 provider 配置。费用仍按供应商可靠数据提供，不以 SDK 的其他模型估价作为 TokenHub 账单。

| 配置 | 当前行为与运维操作 |
| --- | --- |
| `AGENT_DATA_DIR` | 数据根目录；使用固定绝对路径。不能通过改路径直接迁移已有数据库和会话。 |
| `AGENT_MODEL`、`AGENT_EFFORT` | 明确的 `AGENT_MODEL` 优先；否则取模型配置中的 `ANTHROPIC_MODEL`，再取默认型号。模型环境字段以进程环境优先于 `AGENT_PROVIDER_SETTINGS` 文件。默认 effort 为 `medium`。 |
| `AGENT_DEPLOYMENT_MODE`、`AGENT_TRIAL_CAPABILITIES` | 部署能力边界。默认 `normal` 保持现有功能；`trial` 只放行显式能力清单。可选用逗号分隔的已知能力名覆盖试用默认集合；未知模式、未知能力和错误类型都会启动失败。 |
| `AGENT_ALLOWED_ORIGINS`、`AGENT_COOKIE_SECURE` | 生产填写实际 HTTPS 站点来源并设 `true`；源列表用逗号分隔。 |
| `AGENT_MCP_ALLOWED_HOSTS` | MCP 主机允许名单，逗号分隔主机名，不含协议或路径。修改后重启 server，再在业务连接页面测试。 |
| `AGENT_IDLE_SECONDS`、`AGENT_APPROVAL_SECONDS` | 默认分别为 600 秒；空闲执行进程回收不删除历史，审批超时视为拒绝。 |
| `AGENT_MAX_ACTIVE_SESSIONS`、`AGENT_MAX_TURNS`、`AGENT_MAX_BUDGET_USD` | 默认分别为 4、30、5；预算阈值不是绝对账单上限。`AGENT_PROVIDER_COST_RELIABLE=false` 时费用保留未知。 |

环境、模型配置或密钥变更后，先停止接收新任务，等待运行完成或由任务所有者取消，再优雅重启 server，使活动执行进程一致使用新配置。原生进程不自动读取根目录 `.env`；容器修改 `.env` 后用 `docker compose up -d --force-recreate server`，仅 `restart` 不会重新注入环境变量。修改 provider 文件也应重启，避免活动会话继续使用旧连接。

页面修改连接或助手会递增配置版本；连接编辑后需重新测试、确认允许的工具。运行中的下一次工具检查遇到版本变化会拒绝继续使用旧配置，并提示重新发送任务；下一轮会重建执行连接。停用用户、停用连接、收回助手访问或工具授权会在后续授权检查生效，待批准工具在执行前也重新检查。它们不会撤回已经发往外部系统的调用或撤销已产生的业务结果；紧急撤权还应在客户 MCP／业务系统中吊销对应凭据。

## 停机整体备份

先停止入口接收新任务，等待运行完成或取消，然后通过实际使用的进程管理器、`docker compose stop` 或 `scripts/preview.py stop` 停止服务，并确认 API 与其执行子进程已退出。**以下备份命令不自动停止进程，不能在服务运行时执行。**

整份数据目录包括 `app.sqlite3` 及可能存在的 WAL/SHM、`encryption.key`、`sdk-config/`、`workspaces/`、`memory/`、`conversation-memory/`、`files/`、`knowledge/` 等。0.4 的知识原件存于 knowledge，版本、文本索引、资源权限和来源注册存于数据库，须配套恢复。不要只复制数据库。若配置 `AGENT_SECRET_KEY_FILE` 或 `AGENT_SECRET_KEY`，外部密钥优先于数据目录密钥，必须通过同样受控的秘密备份机制额外保存；丢失原密钥无法解密已存 MCP 凭据。模型配置和进程环境文件也需另外受控备份。

以下示例面向原生目录部署，要求 Linux 上具备 `realpath`、GNU `tar` 和 `sha256sum`。备份目录必须在数据目录之外：

```bash
set -eu
umask 077
: "${AGENT_DATA_DIR:?请先设置实际数据目录}"
task_data=$(realpath -- "$AGENT_DATA_DIR")
test "$task_data" != /
test -f "$task_data/app.sqlite3"
task_backup_dir=/srv/zhixing-backups
install -d -m 700 -- "$task_backup_dir"
task_backup_dir=$(realpath -- "$task_backup_dir")
case "$task_backup_dir/" in "$task_data/"*) exit 1 ;; esac
task_archive="data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
(set -C; tar -C "$task_data" -czf - . > "$task_backup_dir/$task_archive")
(cd "$task_backup_dir"; set -C; sha256sum "$task_archive" > "$task_archive.sha256")
```

`set -C` 拒绝覆盖同名备份。归档包含连接解密材料和用户数据，须按秘密数据管理，加密离机保存，并另存应用版本、发行包摘要及原 `AGENT_DATA_DIR` 绝对路径。备份成功后可启动原服务。容器的 `/data` 对应 `agent-data` 卷，不是宿主机同名目录；应在 server 已停止后通过只读挂载该卷的备份容器导出完整 `/data`，不要备份空的宿主机目录。

## 恢复到空目录并验证

使用备份对应的应用版本与依赖。在新主机保留原 `AGENT_DATA_DIR` 绝对路径；容器应保持原容器内 `/data` 路径。数据库附件记录及 SDK 会话涉及绝对路径，本版本没有自动改写迁移工具。恢复验收期间隔离入口，避免测试服务与生产共同操作业务系统。

先停止目标服务，确认原密钥与模型私有配置已按原方式就绪。下面的命令要求目标不存在或完全为空；若已有数据会退出，请先由运维另行保存，**不要删除或覆盖它来继续执行**。

```bash
set -eu
umask 077
: "${AGENT_DATA_DIR:?必须使用备份对应的原绝对路径}"
task_backup_dir=/srv/zhixing-backups
task_archive=data-YYYYMMDDTHHMMSSZ.tar.gz
(cd "$task_backup_dir"; sha256sum --check "$task_archive.sha256")
case "$AGENT_DATA_DIR" in /*) ;; *) exit 1 ;; esac
test "$AGENT_DATA_DIR" != /
test ! -L "$AGENT_DATA_DIR"
if test -e "$AGENT_DATA_DIR"; then
  test -d "$AGENT_DATA_DIR"
  test -z "$(find "$AGENT_DATA_DIR" -mindepth 1 -print -quit)"
else
  install -d -m 700 -- "$AGENT_DATA_DIR"
fi
tar --extract --gzip --keep-old-files --no-same-owner \
  --file "$task_backup_dir/$task_archive" --directory "$AGENT_DATA_DIR"
```

以服务运行账号执行恢复，或由运维将恢复目录的所有权交还该账号；容器默认账号 UID 为 10001。不要用宽松的全员可读权限解决权限报错。先离线检查数据库与附件路径，下面只打印检查结果，不输出业务内容或密钥：

```bash
.venv/bin/python - <<'PY'
import os, sqlite3
from pathlib import Path
root = Path(os.environ["AGENT_DATA_DIR"]).resolve()
db = sqlite3.connect((root / "app.sqlite3").as_uri() + "?mode=ro", uri=True)
assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "数据库检查失败"
missing = sum(not Path(row[0]).is_file() for row in db.execute("SELECT path FROM files"))
db.close()
assert missing == 0, "存在缺失附件或恢复路径不一致"
print("数据库与已登记文件路径检查通过")
PY
```

随后用同版本启动一个 server worker，检查 live/ready、管理员登录、原有连接测试、历史消息和附件下载；在受控只读助手上手动继续一轮历史会话，确认原上下文与轨迹可用。最后一项会产生模型用量。只有这些实际检查通过才恢复对用户开放；本手册的恢复命令尚未代表已在客户环境完成灾备演练。

## 升级、回退和账号凭据

当前记忆并发验收使用 `scripts/session_isolation_smoke.py`，模型身份切换使用 `scripts/identity_selection_smoke.py`（均须显式 `--real-model`）。`native_context_smoke.py` 与 `complete_client_browser.mjs` 的共享记忆流程对应0.4.1及更早版本，保留作历史证据，不作为0.4.2验收命令。

0.4.2 起使用 `conversation-memory/<user_id>/<conversation_id>/`，原生记忆按会话独立。升级时数据库将已有会话标为待迁移，首次使用时各自复制一份旧 `memory/<user_id>/<assistant_id>/` 记忆到独立目录，持久记录完成状态。原件保留、旧助手记忆接口只读；之后新建会话从空记忆开始。完成迁移的会话清空后不会在重启时再次继承旧文件。工作区、SDK配置和已有原生会话ID保持各自归属；旧 `workspaces/<conversation_id>/memory/` 历史目录也不删除或自动合并。

修改或清除记忆需要最新版本号，且该会话没有任务执行或上下文统计请求。同一会话一次执行一个任务；同助手的其它会话可以并行，受 `AGENT_MAX_ACTIVE_SESSIONS` 总容量限制。取消、审批和上下文统计均绑定具体会话。清除记忆不删除已经进入该会话上下文的事实。原生文件仍由运行时管理，应用没有另一套摘要服务。

上下文统计只在用户手动刷新或手动压缩前后请求运行时。Claude 统计可能触发模型供应商的额外 Token 计数请求；Harness 统计使用官方估算接口，不额外调用模型。刷新缓存60秒，供应商无法提供统计时显示未知，不影响普通聊天。Claude 与 Harness 的手动压缩都只有收到真实压缩完成边界才标记完成；历史太短而未触发压缩时会明确显示无需压缩。Claude 继续采用其运行时报告的自动压缩设置；当前 Harness 固定配置关闭自动整理，手动压缩已完成开发迁移。

升级前完成上述停机整体备份，并保留旧 client/server 发行包、摘要、私有配置与对应数据快照。先在隔离环境用恢复副本检查新版本，再切换整套 client/server。当前没有承诺数据库自动降级兼容；若需回退，应停止新版本，将旧版本与升级前对应数据快照一起恢复到空目录。升级后产生的数据须另行保全和处理，不能让旧程序直接写未经兼容验证的新数据库。

管理员通过“成员与用量”创建、停用或重置成员密码；密码或角色变更、停用用户会注销该用户现有登录会话。至少保留一个启用管理员。遗失全部管理员登录时，由拥有服务器维护权限的人员运行 `.venv/bin/python -m app.cli init-admin --username recovery-admin` 创建新的维护账号，交互输入新密码；此命令不会重置同名账号。恢复控制后在页面收回临时账号权限。

连接凭据只在服务端加密保存，页面不回显原值；轮换时在业务系统生成新凭据，在连接页面替换、测试，再吊销旧凭据。不要在命令行参数、工单或聊天中明文传递密码；CLI 可用 `--password-file` 指向权限受限的临时文件，完成后按组织秘密管理流程处理。不要直接替换连接加密主密钥：本版本没有在线密钥重加密工具，必须保留原密钥与其数据快照配对。

## 托管 Linux 开发预览

已经用 `scripts/preview.py start` 初始化的独立预览，可改用 systemd 在进程意外退出后自动重启。这个工具只服务于回环地址开发预览；生产站点仍按前述实际域名、TLS 和进程管理配置部署。

```bash
# 先等待任务结束，再停止原先的普通预览进程。
.venv/bin/python scripts/preview.py stop
sudo .venv/bin/python scripts/managed_preview.py install --user YOUR_SERVICE_USER
sudo .venv/bin/python scripts/managed_preview.py start
.venv/bin/python scripts/managed_preview.py status
# 受控重启 API；或省略 server 控制本项目的三个组件。
sudo .venv/bin/python scripts/managed_preview.py restart server
sudo .venv/bin/python scripts/managed_preview.py stop
```

服务名称由项目绝对路径生成，安装前验证所有 unit；只允许覆盖和控制带有本项目所有权标记的服务。程序以指定的非 root 账号执行，只绑定原有回环端口。安装后的启动、停止和重启统一使用 `managed_preview.py`，不要混用普通 `preview.py stop`，否则 systemd 会把被终止的进程重新拉起。

模型私有配置继续从原文件加载；可选 `.runtime/service.env` 注入预览启动环境中的模型和空闲回收参数，文件设为0600。预览固定回环来源、演示 MCP 主机名单及非安全 Cookie，不作为生产环境配置入口。日志进入 journal，服务名称见 `status`，例如 `journalctl -u <实际server服务名称> --since '10 minutes ago'`。这里的自动重启不承诺自动重放中断任务，也不会自动批准旧审批。


## 0.4 权限、迁移与知识部署

0.4 启动时幂等增加资源范围、助手能力、会话分类/标签、Skill版本、知识文件及来源记录等表和字段。既有助手、连接迁移为团队配置，保留原有使用授权；历史会话与附件不因升级重建。新成员默认创建私有配置，只有管理员能管理团队资源。管理员不能以角色绕过另一个成员的私有内容权限。

升级前先按本手册停机整体备份，使用独立副本验证，再成套切换 client/server。启动后检查旧会话和原模型选择、成员私有隔离、团队使用权限、原件下载和来源撤权；不要以页面隐藏按钮代替服务端权限验证。没有通用数据库降级迁移，回退需旧发行包和升级前完整数据快照。

助手与会话分离，分类、标签和归档只改变会话管理信息。新助手自动绑定管理员明确标记的默认公司知识库；业务知识和数据连接仍逐助手选择。管理员调整默认知识库不会自动给已有助手追加绑定。私有连接或知识不可直接挂到团队助手。

### 准备本地 OCR 与语义向量

从 `server/pyproject.toml` 安装服务器依赖。Linux OCR 运行时需要 `libgl1` 和 `libglib2.0-0`；镜像已声明相应系统依赖，容器是否已运行验证仍以交付记录为准。

```bash
sudo apt-get install --no-install-recommends libgl1 libglib2.0-0
.venv/bin/python scripts/prepare_knowledge_models.py \
  --cache-dir /srv/zhixing-models --download
```

此命令只下载固定版本的公开模型文件，验证 SHA-256 并进行一次本地向量计算，不读取或上传企业文档。按输出设置以下环境变量后再启动服务：

| 环境变量 | 用途 |
| --- | --- |
| `AGENT_KNOWLEDGE_EMBEDDING_MODEL` | 使用语义检索时设 `BAAI/bge-small-zh-v1.5`。省略时是明确标注的 `local-lexical-v1` 词法向量，不是语义模型。 |
| `AGENT_KNOWLEDGE_MODEL_PATH` | 命令输出的已验证模型目录，运行账号只需读取。 |
| `AGENT_KNOWLEDGE_MODEL_CACHE` | 本地缓存目录，保留固定版本供恢复或离线使用。 |
| `AGENT_KNOWLEDGE_ALLOW_MODEL_DOWNLOAD` | 常规服务设 `false`，模型预先准备；不会在用户索引任务中临时联网下载。 |

离线复制后仍需提供缓存目录并校验：

```bash
.venv/bin/python scripts/prepare_knowledge_models.py \
  --cache-dir /srv/zhixing-models \
  --model-path /srv/zhixing-models/verified-bge-directory
```

模型目录不必放在业务数据目录；外置时将固定模型版本、摘要和目录位置纳入运维备份。切换向量模型后重启服务，并通过有编辑权限的账户调用 `POST /api/v1/knowledge-documents/{id}/reindex` 重建现有文档索引。原件版本与历史来源保留。

PDF 由服务器本地提取文本，扫描页由 RapidOCR 识别，向量由本地 ONNX 模型计算。支持PDF、DOCX、Markdown、TXT、CSV和JSON；文件默认20MB，最多200个PDF页、200万提取字符及5000片段。大小配置由当前服务配置提供。状态为 `processing/ready/needs_ocr/failed`，空文本或失败不标记ready。OCR依赖缺失、损坏文件或模型文件缺失时检查文件错误信息并重试；服务重启会将上次中断的processing标为failed，避免永久假进度。

### 初始化公司与试用资源

`scripts/bootstrap_workspace_04.py` 通过正常产品API创建团队公司知识库、独立业务试用知识库和三个基础Skills，导入显式传入的公司PDF，并更新指定试用助手及连接。它还会给可编辑的启用助手补上公司库；因此应先审核目标助手与资源范围，再运行。公司PDF及账号凭据不随发行包提供。

```bash
.venv/bin/python scripts/bootstrap_workspace_04.py \
  --base-url http://127.0.0.1:18080 \
  --password-file /private/admin-password \
  --company-pdf /private/company-profile.pdf \
  --assistant-id TARGET_ASSISTANT_ID \
  --connection-id TARGET_FIXTURE_CONNECTION_ID \
  --report /private/bootstrap-0.4-report.json
```

先启动对应新版验收MCP并确认它提供质量统计和工单工具。此脚本配置试用资源，正式业务助手应由用户在页面选择自己的资料和连接，不能把试用数据库作为正式系统自动接入。

### 搜索、来源和故障定位

网页搜索由助手开关控制，默认使用公开 Bing RSS，网页读取使用受限 HTTP 客户端。服务端还支持显式配置的公共 SearXNG 或 Brave provider：将 `AGENT_WEB_SEARCH_PROVIDER` 设置为 `searxng` 时必须同时提供 `AGENT_WEB_SEARCH_SEARXNG_URL`；设置为 `brave` 时必须同时提供 `AGENT_WEB_SEARCH_BRAVE_API_KEY`（可用 `AGENT_WEB_SEARCH_BRAVE_URL` 覆盖默认接口）。未配置这些变量时不会偷偷调用可选 provider，仍只走 Bing；可选 provider 失败时默认回退 Bing，可用 `AGENT_WEB_SEARCH_FALLBACK_BING=false` 关闭回退。

每次搜索最多返回 8 个结果，结果按规范化公开地址去重（移除 `utm_*`、`gclid` 等跟踪参数和片段），搜索响应上限 500 KB，页面读取上限 2 MB，网络超时与公开 DNS、重定向、响应大小检查保持不变。返回结果明确记录请求 provider、实际 provider 和是否回退；每个摘要和正文仍通过受权限校验的 source 标记引用。搜索只发送必要关键词，不发送凭据、企业文档全文或内部地址。网页网络可用性与 TokenHub 模型线路独立；不要通过放开内网地址来解决搜索失败。知识 OCR 与向量计算本地完成。

MCP应按 [来源契约](MCP_SOURCE_CONTRACT.md) 返回取数依据。页面中的逻辑数据库说明不会代替真实查询中的字段、筛选、时间和汇总口径。缺失来源字段保持缺失，不应通过角色提示词猜测补全。

来源详情读取时再次检查当前用户、助手、资源和工具权限。停用或撤权会阻断来源重开；已生成的回答作为历史记录保留。运维导出轨迹仍须按用户业务数据管理。恢复检查需覆盖知识原件、固定文件版本及来源API，不能只看聊天文字。

| 现象 | 优先核对 |
| --- | --- |
| 健康接口正常，模型迟迟无正文 | 本次运行的runtime_ready_ms、first_model_event_ms和first_text_ms；健康接口不验证供应商推理。 |
| 模型首事件已有但正文晚 | 结合推理档位、工具事件和同任务对照，不将全部等待归因网络。 |
| `needs_ocr`或解析失败 | 本地OCR依赖、文档格式与文件错误状态，修复后重新索引。 |
| 语义检索未启用 | 检查检索metadata的semantic/vector_model以及启动环境和固定模型路径。 |
| 来源无法打开 | 原件或连接是否删除、助手是否解绑、工具权限是否撤回、当前账号是否拥有原运行。 |
| 当前会话提示忙 | 当前会话仍有任务或统计操作；可等待或停止该任务，其它会话可独立执行。若出现旧版“此助手的另一个会话”提示，检查前后端是否均已升级至0.4.2。 |
| 搜索失败 | 搜索或源网站连通性、文本格式、重定向与公网地址校验，和模型健康分别排查。 |

`timings` 是从任务进入服务起计算的观察值，不等于供应商内部排队或纯推理时间；未测阶段留空。服务自动恢复不重放业务写操作，也不会自动同意旧审批。TokenHub按量续费、券抵扣和余额属于提供方账号配置，不由本应用健康接口证明。

真实验收使用 `scripts/workspace_04_smoke.py --password-file ... --seed-report ... --report ... --real-model`；未加 `--real-model` 只显示计划。脚本创建专用助手/会话，验证公司、数据和网页来源及Skill记录；是否实际通过、清理结果和费用用量应以报告与交付记录为准。
