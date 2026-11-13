# 华微世纪项目机器人部署记录

这个独立的飞书应用已加入华微世纪群，连接原有 Codex 会话。用户手动将应用名称设为 `dengkw-华微世纪`，部署沿用这个名称；本机独立 profile 名称仍为 `dengkw-华微世纪负责人`。仅群成员明确 @ 本机器人时处理任务；普通聊天用于理解背景。电脑端仍可继续使用原会话。

## 已确认的目标

- 群名称：华微世纪。
- 群编号：`oc_0e594fd71ee1b3e2e0b46365e4f42a7f`。
- 群配置允许所有成员邀请，入群不需要审批。
- 用户指定的目标会话：`01a1afd9-455e-7a81-9db7-f9a1d768a396`。
- 工作目录：`/media/data3/dengkw/projects/open-claude-code`。
- 本会话尚未在服务器团队登记表中正式登记为 L2；机器人部署不会自动完成项目负责人登记，本次也没有修改登记表。
- 已有学校大管家应用 `cli_aa5e2ae5a638dd0d` 已在群中；它不是此次新增项目机器人。禁止将该应用配置为本集成的身份。
- 本机默认 lark-cli profile `dengkw-通用助手` 实际属于学校大管家，不能仅凭 profile 名称判断应用身份。
- 独立应用：`cli_aa67dc332c389c0d`；所属租户：万物智灵（深圳）科技。
- 独立 profile：`dengkw-华微世纪负责人`；经 `auth status --json --verify` 核验的应用名称为 `dengkw-华微世纪`。
- 已使用邓凯文的用户身份将该应用加入华微世纪群。邀请结果 `ok=true`，无无效成员、无待审批成员；随后群成员回读确认两个应用分别存在。
- 使用目标 profile 验证的机器人自身 open_id 为 `ou_78b44db8646ef71a11a987acd19165a8`。配置和 @ 匹配使用此值，不使用其他应用查询视角下的 open_id。
- 发布后再次使用目标 profile 的机器人身份读取群成员，目标应用名称为 `dengkw-华微世纪`，自身 open_id 与配置一致，学校大管家仍作为独立应用存在。

## 当前部署状态

**应用已发布，服务正在运行；首条真实群 @ 已在指定会话执行并成功回复到群里。**

2026-11-10 16:23（Asia/Shanghai），版本 `1.0.3` 已发布，开发者后台显示“当前修改均已发布”。应用名称已由机器人身份读回验证。长连接和 `im.message.receive_v1` 事件原先已有配置，本次新增机器人权限 `im:chat.members:read`、`im:message.group_msg`；原有 `im:message:readonly`、`im:message:send_as_bot` 继续用于历史读取与回复。

部署核验结果：

| 检查项 | 已有证据 |
| --- | --- |
| 机器人身份与读取权限 | `--check` 返回 `ok=true`，观察到 3 名群成员，`history_readable=true` |
| 独立监听 | 启动前查询该应用的远端长连接数为 0；启动后日志出现 `ready event_key=im.message.receive_v1` 和 `feishu-websocket connected` |
| 用户服务 | 状态为 `active/running`，`NRestarts=0`，`Linger=yes`；收件箱和发件箱各有 1 条 `sent`，无待处理任务 |
| 目标 Codex 会话 | 切换时核实唯一写入进程 PID `2596626` 与目标面板；原生探测返回 `busy`，群任务等待该会话空闲 |
| 离线回归 | 部署前 48 项离线测试通过；此结果不代替真实群消息验收 |

私有配置位于 `/media/data3/dengkw/.feishu-huawei-project-bot/config.json`，已设置 `enabled=true`、`send_replies=true`，绑定目标应用、群、原会话及核验过的 Orca 目标；该文件不含密钥，文件权限为 600，目录为 700。用户服务已安装启用，配置文件为 `/home/dengkw/.config/systemd/user/feishu-huawei-project-bot.service`。

2026-11-10，按用户明确要求，将入口从部署操作会话 `01a1c45e-772e-7cd1-929f-83987823e401` 切换到 `01a1afd9-455e-7a81-9db7-f9a1d768a396`。目标面板为 `f75282dc-d5c2-4269-8135-3489fd3335f4:26aa2a35-3c8b-463a-abc3-5e17b44e7df7`，工作目录和 Codex 运行时目录不变。停止旧入口后确认收件箱与发件箱均为空；原配置已备份、原状态目录保留。新状态目录为 `/media/data3/dengkw/.feishu-huawei-project-bot/state-01a1afd9-455e-7a81-9db7-f9a1d768a396`，避免混用绑定到旧会话的任务记录。没有向任一会话注入测试任务。

## 配置及检查

会话切换后，服务重新出现 `ready event_key=im.message.receive_v1` 与 `feishu-websocket: connected`；新状态数据库的绑定记录与配置中的目标会话一致，服务保持 `active/running`。

身份定位、独立 profile、入群、机器人身份验证、权限发布、读取检查和服务启用均已完成。接口与权限依据见 [接入契约](../../docs/plans/2026-11-10-feishu-project-bot-contract.md)，参数及启动规则见 [使用说明](README.md)。复核或迁移时应保留以下约束：

1. 先核对应用编号、所属租户和机器人自身 open_id；应用显示名与本机 profile 名称可以不同。不得使用学校大管家的应用或切换本机默认 profile。
2. 凭据由独立 profile 管理，不写入仓库、私有业务配置或日志。成员 open_id 由本应用的机器人身份查询。
3. 私有配置使用实际的会话、Orca 工作区、面板和可执行路径；目录权限 700、文件权限 600。仓库示例保持默认关闭，不能覆盖正在运行的私有配置。
4. `python3 -m integrations.feishu_project_bot /media/data3/dengkw/.feishu-huawei-project-bot/config.json --check` 只检查身份、读取权限与配置，不监听事件、不发送群消息；它不替代真实群消息验收。
5. 全应用只保留一套预期接收链路；多实例飞书长连接会分流事件。运行中的服务已有监听，不要为测试额外启动第二个消费者。

## 群内验收

首条真实任务已完成核验：任务 `feishu-412a65e4aab9a5fbd1cd3110cb0366ce` 对应指定会话 `01a1afd9-455e-7a81-9db7-f9a1d768a396` 的原生轮次 `01a1c4ad-dc3e-7041-a443-474ac798b6d6`，收件箱和发件箱均为 `sent`，回复回执为 `om_x100b6019800f04a8c0c3303e4c60ba6`。公开回复包含 1 个经过校验的成员 @。本次上下文成功查询到 3 名成员，近期历史返回 0 条，因此尚不能用本次问候验证非空群历史或关联线程的理解效果。

随后按场景验证：普通消息不回复；@ 学校大管家不触发本机器人；回复前读取最近相关历史和线程；同一事件重投不重复执行；电脑端同时有任务时群任务排队；电脑端直接对话不自动发送到群里。目前这些真实群场景尚未完成验收。

只读取本次群任务对应的确定完成结果，并要求显式公开回复结构。无法确定派发或发送结果时保留待核验记录，不能通过重放任务猜测是否执行成功。

## 运维

使用 `systemctl --user status feishu-huawei-project-bot.service` 查看已安装服务的状态。停止该独立服务不会结束 Codex 项目会话。

本地任务记录通过 `python3 -m integrations.feishu_project_bot /media/data3/dengkw/.feishu-huawei-project-bot/config.json --status` 查看。状态目录包含群内容，必须保持仅本人可读；日志不得记录认证信息或整段群消息。

现有 Codex 终端退出或目标身份变化时，保持任务记录并重新核验原会话，不启动另一个并发 writer 来继续同一会话。

## 2026-11-10 受阻目标排队修复

- 首条真实群消息 `@dengkw-华微世纪 hello` 于 16:37:28 被持久化，任务编号 `feishu-412a65e4aab9a5fbd1cd3110cb0366ce`。后续已核验指定会话执行与群回复成功，具体回执见上文“群内验收”。
- 排队根因：原 `_native_idle()` 拒绝所有非 `completed` 的目标，导致 `blocked` 目标也被当成正在运行。
- 修复只允许 `completed`、`blocked`、`paused` 通过目标检查；活动和未知状态继续拒绝。轮次、输入队列、进程身份、面板、草稿和二次检查不变，不修改原生目标数据库。
- 新增 4 项回归测试；完整飞书入口离线测试 52 项通过。该结果不等于真实投递或真实回复成功。
- 用户已明确批准只重启 `feishu-huawei-project-bot.service`；不重启工作台、模型服务或当前会话，不清空或手动重发队列。具体执行结果见 `docs/evidence/FEISHU_GOAL_QUEUE_FIX_2026-11-10.md`。
