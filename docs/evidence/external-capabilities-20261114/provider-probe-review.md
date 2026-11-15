# 学校云真实供应商探针：独立只读质量审核

- 审核日期：2026-11-14；公开链接复核及本地证据检查完成于约 10:14-10:16 UTC（18:14-18:16 Asia/Shanghai）。
- 角色：原生 collaboration 只读证据审计，不是 Claude Code 实施 ST，不调整看板或产品验收状态。
- 审核输入：`provider-probe/` 中 4 份汇总、6 份供应商响应、初次脚本失败记录、3 类公开/合成输入，以及父目录 `probe_official.py`、`vendor-contract-audit.md`。
- 本次没有执行探针脚本、读取其引用的凭据文件、调用付费供应商、修改产品或探针原件。唯一新增文件为本报告。另对前 3 条公开结果的网页/图片进行受限 GET，在内存检查 MIME/魔数并查看两张可达图片，没有运行网页脚本或执行下载内容。

## 1. 审核结论

**这些证据支持“学校云单次真实供应商契约探针已取得有效 Search、Fetch、text_recognition OCR 和原生以图搜图响应”，不支持“四能力普遍可用/精度达标/成本已优化/腾讯云已验收”。** 本审计依据留存响应和执行者记录核对，不重新发起付费调用；也没有独立网络抓包或供应商账单证明。

关键质量结论：

1. 图片响应确有 `server_tool_use(kind=image_search)`，与结果块 ID 匹配，包含 11 个独立 `image_url`；计费计数为 2 次 image search、0 次普通搜索。它不是普通网页搜索换名。
2. PDF 的 `rich_layout_recognition` 虽为 HTTP 200、1 页、`finish_reason=stop`，但正文为空，五个字段全部缺失，应记为**内容质量失败**。不能用传输成功或 stop 宣称识别成功。
3. PNG 与同一 PDF 的 `text_recognition` 均返回五个正确字段和值，页结构完整；仅支持这一份干净英文单页样本的成功，不能外推扫描件/中文/表格/多页精度。
4. 前 3 个图片结果中，第 1 张确认是输入图的缩小版本；第 3 张是相关但不同的工业机械臂照片；第 2 张被站点拦截，无法判定图像相似性。原网页可访问性只有 1/3，图片字节可读取性为 2/3，仅限本次学校云观察。
5. bridge 的最终自然语言回答提到了两个微信来源，但它们不在可见的 11 条结构化结果中，且回答没有实际 URL 或 citation 支持。应用只应接纳经过验证的结构化结果，不把这些口头来源写入正式证据。
6. 单次 bridge 的 usage 为 `input_tokens=16142`、`cache_read_input_tokens=1024`、`output_tokens=222`、`tool_image_search=2`。这是有额外推理和工具用量的 bridge；没有对照实验，**不能宣称节省 token 或费用**。

## 2. 证据完整性与可重复性

### 2.1 样本及本地检查

| 样本 | 文件/字节 | SHA-256 | 审核结果 |
| --- | --- | --- | --- |
| 合成 PDF | `synthetic-inspection.pdf`，869 B | `9166fe4d9a5e73187939ec3c3ad6d1bf648a83cfc95fabdf83f8722a6df4197e` | 与两份相关 summary 的 fixture hash 一致；`pdfinfo` 确认 PDF 1.4、1 页、无加密、无 JavaScript；`pdftotext` 正确读出五字段 |
| 合成 PNG | `synthetic-inspection.png`，34085 B | `05ec58ab68331c5853ad25389fa7cd073541ae08ec6792b68b7d9a7eed881f00` | 与 summary 一致；人工查看是白底清晰英文检查记录，五字段均可见 |
| 公开 ABB 图 | `robot-public-original.jpg` / `robot-abb-1.jpg`，各 251747 B | `094464032f31a85d6f729edc087ab9c9c4ad77da13e631044f43501e2517f654` | 两文件字节一致，与 summary 一致；人工查看为橙色机器人末端、夹爪与气管近景 |

公开图来源页确认作者 Peter Potrowl、1824×1368、251747 字节，允许选择 CC BY 2.5（页面还列其他许可）。这支持 summary 的许可/作者记录；它是需署名的公开许可图片，并非公有领域。来源：[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Robot_ABB_1.jpg)，[CC BY 2.5](https://creativecommons.org/licenses/by/2.5/)。

本地可重复命令，无网络和凭据读取：

```bash
pdfinfo /media/data3/dengkw/tmp/oc-capabilities-completion-20261114/provider-probe/synthetic-inspection.pdf
pdftotext /media/data3/dengkw/tmp/oc-capabilities-completion-20261114/provider-probe/synthetic-inspection.pdf -
```

### 2.2 响应文件 hash 与原始 HTTP hash 不是同一个对象

`probe_official.py:93` 对 HTTP 原始 body 计算 `response_sha256`，`:97` 再通过 `json.dumps(..., indent=2)` 另存响应。原始传输字节未保留，**summary 中的 raw hash 不能直接用已格式化 JSON 文件复算**。两者 hash 不同符合脚本行为，不据此认定篡改；同时不能把 raw hash 当作已独立复核的传输证据。后续宜记录 `raw_body_sha256` 与 `artifact_sha256` 两个明确字段。

本次复算的落盘文件 hash：

| 响应文件 | artifact SHA-256 |
| --- | --- |
| `search-response.json` | `59ae67309eb02d593d7e83af24950e2df0e5086394a3c1330e57db1860a6b79f` |
| `fetch-response.json` | `f70d5464ccc8a792a5e2c2b3206acfda8462a3d857f675a912c07e3ce4e2506f` |
| `ocr-response.json` | `f07d5cf24cd9511a40e70c403dc5a68c6c8ad4fcd9ec4e46b897c8252e20e553` |
| `ocr-png-image-followup-response.json` | `1aabbfecbd28a63ce87a650f2ad4faf05e6b69352f702b9d8a65817c5fc580dc` |
| `ocr-pdf-text-pdf-text-only-response.json` | `cf3d291331dac9faccc043b730180e8e88cdeabb5e30939b5263481234e283cf` |
| `reverse-image-reverse-only-response.json` | `0de483f2bb30fe8598fad2937340f42e15e32e2eb10e51e4a8383011d9527200` |

脚本审核时 SHA-256 为 `4a7202409d94dc69b8c6d2a46bad108c7d742b96af35d12f3ff846fd6e4bf5b1`。当前脚本将 initial 模式命名为 `summary-initial.json` / `*-initial-response.json`，而现有初次文件为 `summary.json` / `*-response.json`；说明现存脚本命名与初次执行产物不完全一致。此次没有历史执行脚本快照或变更 diff，不能宣称当前脚本逐字就是全部探针执行版本。后续应把每次脚本 hash/模式和请求摘要写入相应 summary。

汇总中的结果数、content 长度、OCR 五字段与 finish_reason、图片块类型、image_results=11 及 usage 均与相应落盘 JSON 一致。耗时和 HTTP 状态只能依据 summary，落盘 JSON 不含 HTTP 响应头，无法离线独立复算。初次 `script-initial-failure.json` 记录 `AttributeError`、0.0 秒且没有 HTTP 状态，应视为脚本失败记录，不计为供应商拒绝或成功调用。

## 3. Search 与 Fetch

### Search

- 请求摘要：`query="ABB industrial robot IRB 140 official"`，`count=3`，`need_content=false`；独立 `/v1/tools/search`。
- summary 记录 2026-11-14 10:04:36 UTC 起该组探针，Search HTTP 200、0.618 秒、3 条结果。响应有预期 `results` 结构，正文 content 都为空，与请求相符。
- 三条均为 ABB 相关主机：`library.e.abb.com` 或 `search.abb.com`，包含机器人产品资料、线路图/零件文档。结果 #1 有明确产品目录相关性；#2/#3 也是机器人资料，但不能仅凭它们证明找到了最佳 IRB 140 官方产品页。
- 展示质量有缺陷：#2 title 带 HTML 表格片段，#3 title 是残缺的 `[www`；snippet/summary 也含表格标记，date 都缺失。生产 UI 应按纯文本安全渲染、清理展示噪声，不能把标题直接作为可信 HTML；空日期保持未知。
- URL 有重复 fragment，#3 有供应商返回的 `x-sign` 参数。本审核没有打开该结果或把带签名 URL 复制到报告。不能假定“供应商返回的公开结果”就适合原样进入日志/页面；公开链接、追踪参数与疑似签名参数仍需逐项处理。

结论：**独立网页搜索结构/连通性单例通过，检索质量有限，不是图片搜索证据。**

### Fetch

- 请求为 ABB 官方 articulated robots 页面；summary 为 HTTP 200、7.843 秒，返回标题和 1266 字符 Markdown 正文。
- 独立读取 [ABB 官方原页](https://www.abb.com/global/en/areas/robotics/products/robots/articulated-robots) 可见相同的小/中/大型机器人分组，以及 12/60/600 kg 载荷描述，支持正文确实围绕目标页而非空标题。
- 抽取结果包含 `Tab 1` 导航噪声，也没有当前页面全部内容；此次不要求页面全文字节一致，不能据此声明完整抓取或对动态页面的普遍完整性。

结论：**真实网页正文获取单例通过；尚未覆盖重定向、受限域名、登录页、空正文、超大页或生产错误处理。**

## 4. OCR：五字段与页完整性

五项要求按“标签和值正确配对”检查，强于只做数值子串包含：

| 字段 | 预期行 |
| --- | --- |
| 设备 | `Equipment: DEMO-PUMP-042` |
| 压力上限 | `Pressure limit: 0.80 MPa` |
| 实测压力 | `Measured pressure: 0.76 MPa` |
| 日期 | `Inspection date: 2026-11-14` |
| 结论 | `Decision: PASS` |

| 探针 | purpose | 页结构 | 正文/字段 | 结论 |
| --- | --- | --- | --- | --- |
| PDF 初次 | `rich_layout_recognition` | page_count=1，pages 长度1，index=[0]，stop | 0 字符；0/5；output_tokens=0 | **失败：非空样本得到空识别** |
| PNG 后续 | `text_recognition` | page_count=1，pages 长度1，index=[0]，stop | 215 字符；5/5 标签和值正确 | 此单页图片成功 |
| PDF 后续 | `text_recognition` | page_count=1，pages 长度1，index=[0]，stop | 215 字符；5/5 标签和值正确 | 此单页 PDF 成功 |

三个响应的 `object=ocr.result`、purpose、页数、页索引、usage 加和均一致，没有顶层 error 或 length。两个 text_recognition 文本相同，五字段的顺序和单位也正确；未发现缺页或截断迹象。但初次页面 stop 只表示生成结束，不能覆盖其正文为空的质量问题。

原 PDF 经本地工具成功解析，且内容不是空白；因此可以确认此次 rich_layout 输出不满足测试要求。仅凭单次响应不能确定上游模型、PDF 渲染、purpose 实现还是其他原因；不应写成“rich_layout 永远不可用”。

后续 PNG 是不同文件类型/样本 hash，PDF 后续是同一文件但明确更换 purpose。它们是人工选择的后续诊断，不是原失败被自动重试修复，不能覆盖或删除失败记录。脚本未设自动重试；成功/失败均有独立响应 ID。

本 PDF 自带可提取文本，PNG 是清晰的英文渲染文档。仍缺少真实扫描 PDF、中文、噪声/旋转、复杂表格、版面坐标、多页顺序、length 截断、密码/格式错误、超时取消计费等验收；本次不能宣称通用工业 OCR 准确率。

## 5. 图片工具真实性与回答可信度

响应文件中实际结构：

```text
model: deepseek-flash
server_tool_use:
  id: srvtoolu_is_00_9f1c932ece7948998ddebc2098a51dea
  name: deepseek_web_search
  kind: image_search
  input: {action: search, queries: []}
web_search_tool_result:
  tool_use_id: srvtoolu_is_00_9f1c932ece7948998ddebc2098a51dea
  kind: image_search
  content: 11 results, each with distinct page url and non-empty image_url
stop_reason: end_turn
usage.server_tool_use:
  web_search_requests: 1
  web_fetch_requests: 0
  tool_search: 0
  tool_fetch: 0
  tool_image_search: 2
```

调用与结果 ID 精确匹配，kind 和空 queries 符合 [官方 Web Tools 图片搜索合同](https://api-docs.deepseek.com/guides/web_tools/)。外层结果 type 仍叫 `web_search_result` 是供应商协议命名；这里结合 image_search kind、image_url 以及实际图像检查才认定为真实搜图，不能单看 type。

`web_search_requests=1` 是展示用结果块数，不是一次计费量；实际 `tool_image_search=2` 不能被汇总成“只计一次”。请求只有一张图；原始结果未暴露具体区域选择，无法进一步独立证明第二个计费单位对应哪个 crop。

该请求使用 Messages 协议、关闭 thinking、max_tokens=700。成功只证明该配置与这个输入实际触发了一次原生 image_search 调用，不证明 tool_choice 可强制、文字搜图可用、Chat/Responses 协议同样保留字段、其他模型跨模型工具闭环已完成。

此外，提示词已经给出图片名 `Robot ABB 1`、作者和许可证。这是带有名称提示的公开样本探针，不是无提示盲测；最终回答复述作者/许可不能当作模型独立溯源成功。

最终文本中的两个微信页面没有出现在可见结果 url/site_name 中，也没有 citation 或具体页面链接。加密内容不能由本审计解密，无法核实是否包含未展开信息；应用不得凭这种不可验证正文增加来源。已知 Commons 原始出处也未出现在 11 条结构化结果中：**此次找到了同图转载，未找回已知原始出处。**

## 6. 前 3 条结果：相关性、可访问性与 MIME

### 6.1 有界读取方法

仅从留存响应提取前 3 个 `url` 与 `image_url`；固定其原始主机集合。每次要求 HTTPS、无 userinfo、443/默认端口，DNS 解析结果必须全为公网 IP。关闭环境代理和自动重定向；最多 3 次显式重定向，每跳重新检查主机/DNS；单请求 socket timeout 为 12 秒，HTML 最多 512 KiB、图片最多 2 MiB，读取时不执行 JS、不带 Cookie/Authorization。

本次 6 个 URL 均为原地址直接响应，没有实际重定向，DNS 检查均为公网。这里是一次链接可达性/MIME 检查，不是全面恶意内容检测或 DNS rebinding 防护证明；读取上限和 socket timeout 也不等于完整客户端总 deadline。

### 6.2 逐条结论

| 排名 | 来源与图片 | 网页/图片响应 | 相关性判定 |
| --- | --- | --- | --- |
| 1 | [Foreign Trade 原网页](https://www.foreign-trade.com/wholesale/ABB-NTAC-02-58976008_280925.html) / [图片](https://www.foreign-trade.com/photo/ko/Robot%5FABB%5F1%2Ejpg) | 网页 200 text/html、27468 B，包含对应图片文件名；图片 200 image/jpeg，JPEG 魔数，33764 B | **同一照片的缩小版本**：夹爪位置、绿色/蓝色管线、FESTO 标记与背景一致；不是原始图字节，也不是原作者出处。页面售卖 ABB NTAC 零件，不宜用来证明照片中设备型号 |
| 2 | [Descopera 原网页](https://www.descopera.ro/stiinta/18178346-cercetatorii-au-creat-primul-brat-robotic-care-poate-fi-controlat-fara-ajutorul-unui-implant-cerebral) / 响应第 2 条 `image_url` | 网页与图片均 403、text/html；图片 body 为挑战页而非图像，4832 B | **未核实**：结果标题/摘要谈机器人臂，只有主题层面关联；不能判为同图或有效可见图片，不绕过挑战 |
| 3 | [NewElectronics 原网页](https://www.newelectronics.co.uk/content/features/reaching-for-a-better-grip-with-robots) / [图片](https://www.newelectronics.co.uk/media/sdpd5uzy/robot-gripper-image.jpg?width=1002&height=564&bgcolor=White&v=1db44e8d274c110) | 网页 403、text/html；图片 200 image/jpeg，JPEG 魔数，56224 B | **视觉相关但不同照片**：同属橙色工业机械臂末端夹爪场景；朝向、背景、管线/夹爪构图不同。不能确认确切 ABB 型号或原作者 |

第 2 条图片完整地址可从 `reverse-image-reverse-only-response.json` 的结果 index=1 复取，其主机为 `media.descopera.ro`。长路径中含 `format(webp)` 并不能证明返回 WebP；本次实际响应是 403 HTML，必须按状态/MIME/魔数拒绝图片渲染。

可达两图的字节 hash：

| 图片 | SHA-256 |
| --- | --- |
| rank 1 | `d07fcab1a202d11ebf7cfd28486e67081b9946cc516ac1633ff347a7d71727f4` |
| rank 3 | `41a736665fa93ba7f757c85a676b49341c526249b73efaf7b9edc97c6f44941a` |

HTML/拒绝响应本次 hash：rank 1 网页 `e54f5c573549b740894c54c8fbda5c7bf68535afa86f239a5c085674f6a98026`；rank 2 网页 `b7bf1bf47a11d518a4c7a6fbfca100cf4f430f99f07d9b4eb3748a8fadf8de0d`，图片请求 `386624e567bd0e00d5c7219b83e4ade2d8a5a9d1b3d8249201a7a8638a3fd583`；rank 3 网页 `fc1d21d894a64d00238c67ff5abc38b930fdc006b81dabab0355601d7e245b31`。动态 HTML/挑战页可能随时间改变，这些 hash 只标识本次响应，不保证复读一致。此次网络响应未额外落盘，不能声称有离线重放包。

不要把这组结果写为“前 3 张全部可用/都找到同图”或计算普遍 P@3。准确记录是：一个同图副本、一个相关但不同照片、一个因拦截未知。

## 7. 用量、成本与不能升级的验收范围

bridge 实际报告 16142 非缓存输入 token、1024 缓存输入 token、222 输出 token；按 Messages 合同输入两项分列，合计输入 17166。max_tokens=700 约束输出，不等于输入或总成本上限。[官方 usage 定义](https://api-docs.deepseek.com/guides/web_tools/)

本次没有“直接搜图”“其他模型”或“未经桥接”的同任务成本/效果对照，也未查账单；因此不报告节省比例、精确实扣费用或供应商内部输入来源。用户图片大小只有 251747 B，也不意味着整个 bridge 只支付图片对应 token：实际响应已给出更大的用量。

本轮探针在独立脚本中直调供应商，未通过产品工具执行器、细粒度策略、审批、来源 API、额度账本或 UI。由此不能证明：

- 任意主聊天模型实际调用工具并正确消费图片/OCR结果；
- 附件 ACL、外发授权、逐助手权限、撤权、域名限制及备用路由均生效；
- OCR 空白/部分结果、图像 403 HTML、模型无工具调用时的产品行为正确；
- 搜图三种 URL（原网页、原图、缩略图）的访问控制和来源展示已闭环；
- 生产密钥配置、持续健康、性能/并发、账户配额和账单验收完成；
- 腾讯云运行网络与部署已经验证。

可用于后续产品验收的下一步是把相同已许可/合成 fixture 及失败案例导入真实工具路径：text_recognition 成功要展示页级来源；rich_layout 空白要明确失败；bridge 无可见图片/遇到403要真实降级；真实 image_search 的结构化结果才可进入图片证据。对能力集成和正式发布的通过与否，仍由完整学校云验收及既定腾讯云发布流程决定。

## 8. 离线复核脚本（不含凭据、不发请求）

```python
from pathlib import Path
import hashlib
import json
import re

root = Path('/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/provider-probe')
expected = [
    'Equipment: DEMO-PUMP-042',
    'Pressure limit: 0.80 MPa',
    'Measured pressure: 0.76 MPa',
    'Inspection date: 2026-11-14',
    'Decision: PASS',
]
for name in ('ocr-response.json', 'ocr-png-image-followup-response.json',
             'ocr-pdf-text-pdf-text-only-response.json'):
    raw = (root / name).read_bytes()
    body = json.loads(raw)
    pages = body['pages']
    text = '\n'.join(page['text'] for page in pages)
    print(name, hashlib.sha256(raw).hexdigest(), {
        'page_count_matches': body['page_count'] == len(pages),
        'continuous_indices': [p['index'] for p in pages] == list(range(body['page_count'])),
        'finish_reasons': [p['finish_reason'] for p in pages],
        'nonempty': bool(text.strip()),
        'correct_fields': [bool(re.search('^' + re.escape(v) + '$', text, re.M)) for v in expected],
    })

body = json.loads((root / 'reverse-image-reverse-only-response.json').read_text())
calls = {b['id']: b for b in body['content'] if b.get('type') == 'server_tool_use'}
for block in body['content']:
    if block.get('type') != 'web_search_tool_result':
        continue
    call = calls[block['tool_use_id']]
    assert call['kind'] == block['kind'] == 'image_search'
    assert call['input'] == {'action': 'search', 'queries': []}
    rows = block['content']
    print('image_results', sum(bool(r.get('image_url')) for r in rows))
    for rank, row in enumerate(rows[:3], 1):
        print(rank, row['url'], row.get('image_url'))
print('usage', body['usage'])
```

该脚本复核内容结构与落盘 hash，不验证原始 HTTP 状态/延迟/供应商来源，不复发任何供应商调用。需复核公开链接时按第 6 节的固定 URL、边界与真实 MIME 重新检查，不读取或执行 `probe_official.py` 中的凭据发现函数。
