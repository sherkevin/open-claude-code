# OC-T9 聊天内工作流、Excel 与图片生成证据（2026-11-12）

- 任务：OC-T9（项目 open-claude-code / 知行工作台，绿色“工作流与产物”颜色组）
- 依赖：OC-T3 已完成
- 工作目录：`server`；隔离方式：worktree
- 数据边界：只用公开或合成数据；未部署、未重启服务、未使用真实客户数据、凭据或未授权付费模型

## 交付物

| 类型 | 路径 |
| --- | --- |
| 服务端实现 | `server/app/workflows.py`、`server/app/artifacts.py`、`server/app/image_generation.py`、`server/app/workspace_tools.py` |
| 服务端接线 | `server/app/models.py`、`server/app/db.py`、`server/app/config.py`、`server/app/main.py`、`server/.env.example` |
| 服务端测试 | `server/tests/test_workflow_chat.py`（7 项） |
| 客户端实现 | `client/src/WorkflowChat.tsx`、`client/src/XlsxPreview.tsx`、`client/src/components.tsx`、`client/src/ChatPage.tsx`、`client/src/types.ts`、`client/src/styles.css` |
| 客户端测试 | `client/src/workflowChat.test.tsx`（10 项） |
| 合同与设计 | `docs/API_CONTRACT.md`、`docs/design/ARTIFACTS_EXCEL_IMAGE.md` |
| 示例文件 | `docs/evidence/OC_T9_SAMPLE_WORKFLOW_EXCEL_2026-11-12.xlsx`、`docs/evidence/OC_T9_SAMPLE_BATCH_QUALITY_2026-11-12.xlsx` |
| 图片失败证据 | `docs/evidence/BOARD_OC_T9_IMAGE_FAILURE_2026-11-12.log` |
| 截图 | `docs/screenshots/oc-t9-*.png` |

## 实现摘要

### 聊天内工作流

- 新增 `POST /api/v1/conversations/{conversation_id}/workflows/{workflow_id}/runs`，只接受已发布版本，要求会话属于同一助手且未归档；`client_request_id` 保证重复提交幂等，同一会话已有活动运行时返回 409。
- 运行仍以版本化 `workflow_runs` 为准；聊天侧新增普通用户消息（`运行工作流：{名称}`）和助手消息，并通过 `workflow_chat_links` 与 `messages.workflow_run_id` 建立可回放的绑定。
- 助手消息文本由服务端按状态生成，长度受限、不回显输入值：等待审批、已完成（条件结论与已生成产物名）、已取消、执行失败。
- 审批沿用 `POST /workflow-runs/{run_id}/approvals/{node_id}/decision`，决定后同步消息状态；`POST /workflow-runs/{run_id}/cancel` 只允许取消等待审批的运行，并同时把运行、节点记录和待审批记录置为 `cancelled`。
- `GET /conversations/{id}/workflow-runs` 返回会话内工作流历史，聊天卡片通过 `GET /workflow-runs/{id}` 恢复审批、节点记录和产物。

### Excel 交互预览与安全

- 聊天内运行成功时，`artifact` 节点（`format=xlsx`）生成真实 Office Open XML 文件，写入会话文件区、登记 `files` 行、挂到助手消息附件，并在运行输出中保留 `file_id/download_path/preview_path`。
- 新增 `GET /api/v1/files/{id}/xlsx-preview`，标准库 `zipfile` 只读解析，返回工作表、列、单元格文本、行数、截断标记与公式/转义统计；从不计算公式、宏或外部链接。
- 导出沿用 `= + - @` 前缀转义；预览把转义后的显示文本还原成普通字符串展示，但文件里永远不是公式。
- 客户端 `XlsxPreview` 提供工作表切换、单元格筛选、按列排序（数字按数值、文本按自然顺序）、分页、重新读取和下载原文件，并明确提示公式与文本注入边界。
- 图片附件预览支持 25%–400% 缩放和复位。

### 图片生成边界

- 只有同时满足 `AGENT_IMAGE_GENERATION_ENABLED=true`、生成器地址主机在 `AGENT_MCP_ALLOWED_HOSTS` 内、契约为评审过的 `openai-images-v1` 时才判定可用。
- 请求只发往 `{url}/images/generations`，只接受 `data[0].b64_json`；返回 URL 或其他形状一律 `IMAGE_RESPONSE_UNSUPPORTED`，不主动拉取外部地址。图片必须是合法 PNG/JPEG/WebP 且不超过上限。
- 每次请求先写 `image_generation_jobs`（提示词摘要与字符数、状态、错误码、文件、字节数、用量、时间）；线路缺失时记录 `unavailable` 并返回 503 `IMAGE_MODEL_UNAVAILABLE`，不生成占位图、不伪造凭据。
- 当前本机没有授权的图片模型路由，因此运行时是明确失败路径；测试用本地 `ThreadingHTTPServer` 合成 1×1 PNG 证明真实 HTTP 往返可用。

## 验收证据

### C1：聊天内工作流端到端、审批、取消和引用

命令（在 `server` 目录）：

```bash
pytest -q tests/test_workflow_chat.py
```

结果：`7 passed, 1 warning`，退出码 0（约 646 秒；机器当时负载较高）。

覆盖内容：

1. 运行进入 `awaiting_approval`，用户与助手消息都带 `workflow_run_id`，助手消息提示“等待审批节点”。
2. 重复 `client_request_id` 返回同一运行，消息不重复。
3. 批准后运行成功，产物写入会话：`written=true`、文件名、下载路径与预览路径齐全。
4. 下载得到真实 Office Open XML；工作表 XML 含 `'=` 转义且不含 `<f>`。
5. 预览 `evaluated_formulas=false`，`escaped_text_cells>=1`，显示文本为原始 `=HYPERLINK(...)` 字符串；重复读取结果完全一致（重开核对）。
6. 助手消息更新为“已完成 / 已生成产物”，附件带上 `file_id`；历史接口返回该运行。
7. 取消：运行、审批记录与节点记录都变为 `cancelled`，产物 `written=false`，取消后再审批返回 409。
8. 跨助手绑定被拒绝。
9. 图片线路缺失：503 `IMAGE_MODEL_UNAVAILABLE`，并留下 `unavailable` 任务记录；适配器拒绝 URL 响应，接受评审内 base64 契约。
10. 图片适配器经本地 HTTP 端到端成功：文件、用量、任务记录齐全。
11. Excel 预览从不计算公式单元格。

### C2：Excel 交互预览、下载和重新打开（人工核对）

界面截图（合成数据，本地临时 API + Vite，仅本机回环）：

- `docs/screenshots/oc-t9-chat-workflow-dialog-20261112.png`：从聊天输入区的“工作流”按钮选择已发布版本并填写输入字段，说明只运行发布版本。
- `docs/screenshots/oc-t9-chat-workflow-approval-20261112.png`：聊天内工作流卡片等待审批，同意/拒绝、取消和“引用此结果”均在同一对话。
- `docs/screenshots/oc-t9-chat-workflow-completed-20261112.png`：批准后助手消息与卡片显示“已完成”、条件结论摘要，Excel 附件出现在消息附件和“工作流产物”。
- `docs/screenshots/oc-t9-chat-workflow-cancelled-20261112.png`：等待审批时取消，运行、审批和节点记录变为已取消，未执行节点不再继续。
- `docs/screenshots/oc-t9-excel-preview-20261112.png`：点击产物打开交互预览，显示表头、数据、公式安全提示与下载入口。
- `docs/screenshots/oc-t9-excel-sort-20261112.png`：按“数量”列排序。
- `docs/screenshots/oc-t9-excel-filter-20261112.png`：筛选“待复检”，底部显示“共 2 行（已筛选，原 6 行）”。
- `docs/screenshots/oc-t9-excel-reopen-20261112.png`：关闭弹窗、刷新页面、重新进入会话后再次打开同一 Excel，内容一致。

下载与文件核对：

```bash
python - <<'PY'
from zipfile import ZipFile
for name in ['OC_T9_SAMPLE_WORKFLOW_EXCEL_2026-11-12.xlsx', 'OC_T9_SAMPLE_BATCH_QUALITY_2026-11-12.xlsx']:
    with ZipFile('docs/evidence/' + name) as z:
        xml = z.read('xl/worksheets/sheet1.xml').decode()
        print(name, z.testzip() is None, '<f>' in xml, "'=" in xml)
PY
```

结果：两个文件 `zip_ok=True`、`has_formula=False`、`escaped_hyperlink=True`；工作流产物 1673 字节，多行台账 1916 字节。

### C3：图片生成真实可用或明确失败

当前选择“明确失败”路径，因为本机没有已授权的图片模型路由：

```text
GET  /api/v1/artifacts/image-capability -> 200
{"available":false,"model_id":null,"status":"pending_authorization",
 "reason":"管理员尚未启用图片生成；不会伪造图片或调用未授权模型",
 "contract":"openai-images-v1","endpoint_configured":false,"credential_configured":false}

POST /api/v1/artifacts/image -> 503
{"error":{"code":"IMAGE_MODEL_UNAVAILABLE","message":"管理员尚未启用图片生成；不会伪造图片或调用未授权模型"}}

GET  /api/v1/artifacts/image-jobs?limit=3 -> 200
[{"status":"unavailable","error_code":"IMAGE_MODEL_UNAVAILABLE",
  "prompt_digest":"ceb9557c…","prompt_chars":16,"model_id":"","file_id":null}]
```

完整响应见 `docs/evidence/BOARD_OC_T9_IMAGE_FAILURE_2026-11-12.log`，能力端点截图见 `docs/screenshots/oc-t9-image-failure-20261112.png`。

适配器“真实可用”的另一半由测试证明：`test_image_endpoint_stores_real_image_and_usage_with_local_adapter` 用本地 `ThreadingHTTPServer` 返回合成 1×1 PNG，接口写出真实文件、用量与任务记录；`test_image_adapter_accepts_only_a_reviewed_base64_contract` 证明 URL 响应被拒绝。

## 回归

- 服务端：`pytest -q tests/test_workflow_chat.py` → 7 passed，退出码 0。
- 修改摘要文本后复跑主链路用例：`pytest tests/test_workflow_chat.py -k approval_and_produces` → 1 passed，退出码 0（43 秒）。
- 服务端相关基线（历史任务记录）：`tests/test_workflows.py`、`tests/test_artifacts.py` 曾在本机通过；本轮主链路改动位于聊天绑定、产物物化与摘要文本，未改变既有独立运行契约。
- 客户端：`npm test` → 22 个测试文件、154 项通过；`tsc -b` 退出码 0。
- 回归过程中修正：助手消息摘要最初直接拼接节点 JSON，已改为只输出条件结论和简短文本预览，避免把内部结构粘贴进聊天。

## 复现步骤（本地回环、合成数据）

1. `cd server && pytest -q tests/test_workflow_chat.py`
2. `cd client && npm test && npx tsc -b`
3. 如需界面复现：用临时 `AGENT_DATA_DIR` 启动 `uvicorn app.main:app --host 127.0.0.1 --port 8019`，创建管理员、助手、工作流并发布，再用 Vite 将 `/api` 代理到 8019，登录后从“会话管理”进入会话。
4. 运行一次工作流 → 审批 → 点击 Excel 产物 → 排序/筛选/下载 → 刷新后重开。

## 已知限制

- 本机没有授权的图片生成路由，因此不使用真实图片模型；启用需要管理员提供已评审的 `openai-images-v1` 端点、主机白名单和额度边界。
- Excel 预览为有界只读解析（默认最多 200 行、10 个工作表、50 列），超出时明确标注截断；完整数据需下载原文件。
- 工作流产物物化只覆盖聊天绑定运行；独立运行接口仍只返回结构化预览，避免在没有会话归属时散落文件。
- 本次未部署、未重启任何服务，界面证据来自单独的临时回环进程。
