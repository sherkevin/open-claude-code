# 华微世纪独立飞书机器人接入契约

核验日期：2026-11-10。本文是只读调研和实现约束，不代表应用已改名、发布或入群；没有发送飞书消息，也没有读取或输出凭据。

## 目标与隔离

用户要求将指定的“邓凯文的飞书 CLI”应用改为 `dengkw-华微世纪负责人`，接入华微世纪群。只有有人明确 @ 本机器人时才执行任务和回复；回复前读取相关群聊；回复可以原生 @ 群成员。

主代理已核实目标群是 `oc_0e594fd71ee1b3e2e0b46365e4f42a7f`。当前默认 CLI profile 名字不能用于推断应用身份：默认应用实际属于学校大管家，不能把它改成此项目机器人。必须先核实目标应用的 App ID，再建立独立 profile、监听进程、群白名单、消息去重存储和会话路由。

## 最小权限

全部日常运行操作显式使用 `--as bot`。

| 用途 | 应用权限 | 说明 |
| --- | --- | --- |
| 接收群里 @ 本机器人的事件 | `im:message.group_at_msg:readonly` | 订阅 `im.message.receive_v1`；有群全量权限时仍由程序判断是否 @ |
| 读取群历史和线程 | `im:message:readonly` 与 `im:message.group_msg` | 两者都需要；后者是敏感权限。仅 @ 权限不足以读非 @ 的上下文 |
| 发消息和回复、原生 @ 人 | `im:message:send_as_bot` | 不需要额外的 @ 人权限 |
| 读取群名和群设置 | `im:chat:read` | 推荐用于启动核验，不是已知群 ID 下收发的必需条件 |
| 查群成员以识别任意被 @ 对象 | `im:chat.members:read` | 仅回复当前发言人时，可直接用事件里的 open_id，无需通讯录权限 |
| 将机器人加入群 | `im:chat.members:write_only` | 部署时由已在群中的操作者调用；机器人尚未入群时不能以自身身份邀请自己 |

本机 `lark-cli event schema im.message.receive_v1 --json` 的 `scopes` 仅列出 `im:message.p2p_msg:readonly`，而飞书官方按消息场景区分权限。此 CLI 元数据不等于群聊的完整权限要求。如果该版本 CLI 在启动时强校验单聊权限，须核实实际错误，或使用官方 SDK 监听；不要为猜测而扩权。

群历史权限依据：[获取会话历史消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)。事件及发送权限依据：[消息开放能力列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)。

## 事件和触发规则

本机 CLI 已核验的事件输出为平铺对象，`jq_root_path` 是 `.`。字段包括：

```text
chat_id, chat_type
message_id, event_id, create_time, timestamp
sender_id, sender_type
content, message_type
mentions: [{ id, key, name }]
reply_to, root_id, thread_id
```

`mentions[].id` 和 `sender_id` 使用应用范围内的 open_id。`content` 对文本等类型已经转换为可读文本，不能再执行 `JSON.parse` / `fromjson`；只有 interactive 卡片保留原始 JSON 字符串。CLI 的格式与原始开放接口事件不同，后者的 mention ID 位于 `event.message.mentions[].id.open_id`。

触发条件必须同时满足：

1. 消息属于目标群白名单，且 `chat_type === "group"`。
2. `sender_type === "user"`，并且不是本机器人自己。
3. `mentions` 内有与启动时核实的本机器人 open_id 完全一致的 ID。
4. 此 `message_id` 尚未进入持久化处理记录。

显示名、文本中的 `@dengkw`、`@所有人`、引用旧消息和 @ 其他机器人都不替代第 3 条。群历史只作为上下文，不能把其中旧 @ 消息重新执行。其他机器人的消息可以出现在上下文里，但不成为新任务触发器。

## 读取上下文

优先获取触发消息、被回复的父消息、根消息、相关线程，再补充触发时间前后的群消息。默认最近 50 条、必要时分页至 100 条是实现选择，不能标成平台限制。提交任务前按时间正序组织上下文，附消息 ID、时间、发送者身份和来源群。

本机 CLI 支持：

```bash
lark-cli im +chat-messages-list --profile PROJECT_PROFILE --as bot \
  --chat-id TARGET_CHAT --order desc --page-size 50 --no-reactions \
  --start START_ISO --end END_ISO --page-all --page-limit 2

lark-cli im +threads-messages-list --profile PROJECT_PROFILE --as bot \
  --thread THREAD_ID --order desc --page-size 50 --no-reactions \
  --page-all --page-limit 2
```

`--no-reactions` 避免额外的表情读取权限。JSON 成功信封看 `ok === true`，历史消息在 `data.messages`，分页看 `has_more`，不能仅判断是否存在 `page_token`。

原生历史接口：`GET /open-apis/im/v1/messages`。参数为 `container_id_type=chat|thread`、`container_id`、`sort_type=ByCreateTimeAsc|ByCreateTimeDesc`、`page_size`（1—50）及 `page_token`。群历史 `start_time/end_time` 是秒级时间戳；事件 `create_time` 是毫秒级。线程容器不支持时间范围，群容器只列话题根消息，不能代替线程读取。分页时保持排序和其他查询条件一致。[官方接口说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)

读取失败时必须把缺少上下文的状态反馈给当前任务处理器，不能宣称已看过群聊。保密群设置、新成员历史可见范围以及机器人入群状态均可能限制可读取内容；不能承诺能看到加入前所有记录。

## 回复与 @ 人

回复接口为 `POST /open-apis/im/v1/messages/:message_id/reply`，消息体使用 `msg_type: "text"`、字符串化的 `content`，可设置 `reply_in_thread`、幂等 `uuid`。CLI 对应：

```bash
lark-cli im +messages-reply --profile PROJECT_PROFILE --as bot \
  --message-id SOURCE_MESSAGE --msg-type text --content - \
  --idempotency-key STABLE_REPLY_KEY
```

通过标准输入传入 JSON，避免把群消息直接插入 shell。需要在话题流回复时增加 `--reply-in-thread`。同一回复重试必须复用不超过 50 字符的幂等键。

原生文本 mention 内容为 `<at user_id="ou_xxx">姓名</at>`，用当前应用获得的成员 ID，不靠姓名猜测，也不跨应用复用其他机器人的 open_id。普通 `@姓名` 文本不是可靠的飞书通知。来源：[发送消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json?lang=zh-CN)。

## 可靠性与常驻监听

建议事件接收端先持久化入队再返回，模型执行与消息发送由后台队列处理。以 `(app_id, message_id)` 为唯一键记录接收、处理中、完成、回复消息 ID 和错误状态；任务转交和回复分别幂等。同一目标会话串行提交，避免两个群成员同时 @ 造成回复归属错位。进程重启恢复未完成任务，而非重跑已完成消息。

官方长连接收到事件后须在 3 秒内完成事件处理，否则可能重推；同一应用最多 50 个连接，多个客户端是随机单点投递，不是广播。因此一个应用只设一个明确的入口分发器，避免独立服务争抢事件。[使用长连接接收事件](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)

CLI 常驻的额外约束来自本机技能和 `--help`：

- `event consume im.message.receive_v1 --as bot --profile PROJECT_PROFILE` 输出逐行 JSON。
- 以 stderr 的 `[event] ready event_key=im.message.receive_v1` 作为就绪证据。
- 未设置 `--timeout` / `--max-events` 时 stdin EOF 会让进程正常退出；守护程序应保持 stdin 打开，或使用有界订阅并正常续接。
- 不使用 `--quiet` 隐藏丢弃/错误信号；使用 SIGTERM 正常退出。
- 消息去重使用 `message_id`，不能只用 `event_id`。

## 后台配置、改名与发布

配置流程：定位原应用并核对 App ID → 在基础信息修改显示名 → 开启应用机器人能力 → 申请上述权限 → 启动独立长连接 → 在事件配置选择长连接 → 订阅 `im.message.receive_v1` → 配置可用范围 → 创建版本、申请发布并等待必要的管理员审核。已在线的长连接是保存长连接配置的前提。[机器人概述](https://open.feishu.cn/document/client-docs/bot-v3/bot-overview?from=home)、[长连接配置](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)

本机 CLI `application --help` 只有 slash command 管理，没有应用改名的 typed command。官方 Python SDK 已核实以下 v7 接口和参数，必要时可用 `lark-cli api` 调用：

| 操作 | 接口 | 已核实请求字段 |
| --- | --- | --- |
| 改名称 | `PATCH /open-apis/application/v7/applications/:app_id/base` | `i18ns: [{i18n_key, name, description, help_use}]`；`avatar_url`、`homepage_url` 可选 |
| 开启机器人 | `PATCH /open-apis/application/v7/applications/:app_id/ability` | `bot.enable`；是否允许其他应用邀请入群为 `bot.allow_invited_to_group_by_other_app_switch_open` |
| 权限/事件配置 | `PATCH /open-apis/application/v7/applications/:app_id/config` | `scope.add_scopes: [{scope_name,token_type}]`；`event.subscription_type/add_events/remove_events/request_url` |
| 发布 | `POST /open-apis/application/v7/applications/:app_id/publish` | `mobile_default_ability`, `pc_default_ability`, `remark`, `changelog`, `version` |

v7 改名和发布请求都支持应用凭证及用户凭证。请求格式依据：[官方 SDK 改名请求](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/application/v7/model/patch_application_base_request.py)、[改名请求体](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/application/v7/model/patch_application_base_request_body.py)、[发布请求](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/application/v7/model/create_application_publish_request.py)。

尚未从可读取的官方文档正文核实 v7 管理接口的准确 scope 和 `subscription_type/token_type` 枚举；不要依据猜测写入。第三方索引给出的 `application:application:patch` 只能作下一步查证线索。通过已登录的开发者后台操作或读取官方 API Explorer schema 后再执行。名称修改成功也不等于线上已生效，应重新读取机器人基础信息和群成员列表验收。

## 加入已有群

`POST /open-apis/im/v1/chats/:chat_id/members?member_id_type=app_id`，请求体 `{"id_list":["TARGET_APP_ID"]}`。本机 schema 已核实：

```bash
lark-cli im chat.members create --profile INVITER_PROFILE --as user \
  --params '{"chat_id":"TARGET_CHAT","member_id_type":"app_id","succeed_type":1}' \
  --data '{"id_list":["TARGET_APP_ID"]}'
```

此命令仅为格式示例，没有执行。邀请操作者须已在群内；群仅允许管理员邀请时须使用群主/管理员身份。新增机器人还须处于可用状态和范围内。应核对返回的无效、不可见、待审批 ID 列表，退出成功不等于每个成员已加入。依据：[添加群成员接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat-members/create)及本机 `lark-cli schema im.chat.members.create`。

## 验收场景

1. @ 本机器人且附任务：接收一次，先取上下文，再提交到指定会话，回复对应消息。
2. 不 @、仅输入名字、@其他机器人、@所有人：无回复、无任务执行。
3. 一个群成员 @ 本机器人并 @ 另一成员：回复可以生成正确的原生 mention。
4. 线程内 @：包含父消息和线程讨论，回复归属正确。
5. 同消息重复投递、监听重连、服务重启：没有重复执行和重复回复。
6. 其他机器人或自己发出的消息：不会循环触发。
7. 历史消息读取失败：明确标记上下文缺失，不假装已读取。
8. 机器人和网页均向同一项目会话发送消息时：路由记录标明来源与发言人，群回复只包含与群任务有关的信息，不把私人会话全文转发到群。
