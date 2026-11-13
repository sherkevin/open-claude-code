# 任务板执行会话字段需求（提交 L1）

项目：open-claude-code（知行工作台）
提出人：项目组负责人（L2）
日期：2026-11-12

## 背景

项目子任务由 Cloud Code 执行会话承接。当前任务板只有 `assignee` 和任务状态，无法区分“已分配但尚未启动”“Cloud Code 正在运行”“会话已结束但任务板未更新”。这会让 `running` 被误读为真实进行中。

## 需要 L1 在共享看板实现的字段

任务节点增加以下只读展示字段，数据由任务板命令或调度器写入：

| 字段 | 要求 |
|---|---|
| `st_id` | Cloud Code 真实启动回执中的稳定会话标识，展示格式 `ST-<id>`；没有回执时为空 |
| `session_name` | 执行会话名称，例如 `橙色-项目子负责人` |
| `session_status` | `starting`、`running`、`idle`、`review`、`failed`、`cancelled`、`lost`、`stopped` |
| `session_heartbeat_at` | 最近心跳或进展时间；超过调度器阈值后标记 `lost` |
| `current_node_id` | 该颜色组 Cloud Code 会话当前实际推进的节点编号 |
| `session_evidence` | 启动、进展、提交或停止回执的安全摘要，不包含凭据和客户数据 |

## 状态语义

- 任务卡片只有同时满足 `task.status=running`、`session_status=running`、`st_id` 非空且心跳未过期时，才显示“进行中”。
- 只有负责人、计划、空窗口或项目负责人观察时，任务显示“已分配/等待启动”，不能显示“进行中”。
- `review`、`failed`、`cancelled`、`lost`、`stopped` 均不显示“进行中”。
- 审核人不是执行会话，不显示 `ST ID`，不进入节点数量和进度。

## 调度接口建议

保留现有 `boardctl start/note/submit` 兼容性，新增可选参数：

```bash
boardctl start --task OC-T3 \
  --assignee "橙色-项目子负责人" \
  --st-id "ST-<cloud-code-session-id>" \
  --session-name "橙色-项目子负责人" \
  --session-status running \
  --actor dispatcher
```

`boardctl note` 增加心跳更新能力；Cloud Code 结束或提交审核时，调度器写入 `review`、`failed` 或 `stopped`，并清除“进行中”展示。超过阈值未收到心跳时自动写入 `lost`，由 L2 决定重新启动、阻塞或暂停。

## 本项目监控器与被动刷新

项目目录新增 `scripts/cloud_code_board_monitor.py`。它每轮执行以下流程：

1. 调用 `claude agents --json`，读取 Cloud Code 会话的真实 `ST` ID、短 ID、会话名称、状态和工作阶段。
2. 调用 `boardctl status --project open-claude-code`，只读取本项目节点状态。
3. 对状态变化调用 `boardctl note`；执行会话在没有提交审核而变成 `done`、`missing` 或失联时，调用 `boardctl task update --status failed`，绝不自动标记完成。
4. 审核会话结束时只记录“等待审核结果”，不替审核人判定通过；审核人 Cloud Code 收尾会话单独回写验收项和审核结论。
5. 状态文件放在 `/media/data3/dengkw/.runtime/open-claude-code/cloud-code-board-monitor.json`，不写根分区，不直接操作任务板数据库。

可重复验证：

```bash
cd /media/data3/dengkw/projects/open-claude-code
.venv/bin/python -m pytest -q server/tests/test_cloud_code_board_monitor.py
.venv/bin/python scripts/cloud_code_board_monitor.py --once
```

共享看板本身已经按三秒轮询数据库，浏览器页面不需要用户点击刷新。L1 需要保证常驻调度器或本监控器由用户级服务托管，并让看板保留三秒轮询或改为服务端事件推送；两者都必须在状态写入任务板后自动显示最新状态。项目负责人不修改共享看板代码、不重启共享服务。

开发环境常驻监控的检查与停止命令：

```bash
ps aux | rg 'cloud_code_board_monitor.py --interval' | rg -v rg
kill <monitor-pid>
```

## 本项目当前数据

- `OC-T1` 已由独立审核 Cloud Code 收尾会话回写 C1-C3 并通过审核，当前为 `done`。
- `OC-T2` 依赖 `OC-T1`，已由 DAG 调度解锁并派发 `ST-ebc167cb-e23c-4d2e-80b2-633fedf44e7c`。
- `OC-T3` 和 `OC-T4` 没有前置依赖，已分别派发 `ST-e8f7e353-b1d2-4400-916e-2b41bd44c76a`（橙色收尾/已提交审核，当前由 `ST-5329b1d8-9e67-4ca7-b629-a6f8438fb8a2` 独立审核）与 `ST-b62d4b1e-864f-405a-9c61-ba2d6a76f0b6`，并与 `OC-T2` 并行推进。
- `OC-T7` 虽然没有前置依赖，但因真实 1007/1017 线路证据缺失保持 `blocked`，不启动会话。

## 验收标准

1. 看板可区分等待启动、真实进行中、审核中、失败、取消和会话失联。
2. 真实 Cloud Code 会话启动后才出现 `ST ID` 和进行中状态。
3. 会话结束后卡片不再显示进行中，并保留最后一次安全回执。
4. 刷新大屏后字段不丢失，其他项目和共享看板逻辑不受影响。
