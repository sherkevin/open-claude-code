# Huawi 原生矩阵投影候选（HW-029b）

日期：2026-11-28
分支：`feat/huawi-matrix-projection-20261128-2158`
固定基线：`b2b7347570d4be33f93d4e13dd90c546a7953da0`
状态：`synthetic_candidate_not_product_support`

## 核心判断

新增一个**纯函数、未接运行时**的投影模块，只接受观察到的有界原生矩阵回包，并与同一次已批准查询的顶层参数严格绑定。关键修复是 **select 到列的一一绑定**：`select` 只接受已观察的结构化对象，每个返回列名必须等于被选字段或显式 alias，顺序一致，不能多、缺、重排或重复。

```text
已批准 query 参数                         原生 structuredContent
 dataset                                    dataset
 select[{field} | {field,aggregate,alias}]   columns[{name,label,type}]
 group_by / order_by                 +       data[[cell,...],...]
 page / rows / include_total                 rows=页大小 / page / has_more / 可选 total
        │                                          │
        └──────────────┬───────────────────────────┘
                       ▼
    select 逐项解析 → 期望输出列名（字段或 alias）
                       │
                       ▼
    返回 columns[].name 必须与期望列名逐项相等
        │                 │                 │
        ▼                 ▼                 ▼
   完全匹配            多余/缺失/重排/重复      其他结构或一致性矛盾
        │                 │                 │
        ▼                 ▼                 ▼
   投影成功             None               None（fail closed）
```

本模块**不修改**现有 `server/app/huawi_aggregate_receipt.py` 的严格计数投影，也不接入 runtime、bridge、路由、审批或前端。没有客户数据、真实账号、网络或生产调用。

## 接受的 select 线格式

只接受下列两种已观察的 select 项；其他形状一律 `None`。

| select 项 | 要求 | 输出列名 |
|---|---|---|
| `{"field": "synthetic_field"}` | 恰好一个 `field` 键；安全字段名；不能是 `*` | `synthetic_field` |
| `{"field": "*", "aggregate": "count", "alias": "record_count"}` | 恰好三个键；aggregate 只能是 `count`；field 必须是 `*`；alias 必须是安全字段名 | `record_count` |
| `{"field": "synthetic_field", "aggregate": "count_distinct", "alias": "distinct_count"}` | 恰好三个键；aggregate 只能是 `count_distinct`；field 必须是安全字段名、不能是 `*`；alias 必须是安全字段名 | `distinct_count` |

拒绝：SQL 字符串或任何非对象项、缺失 `field`、缺失 `aggregate`、缺失 `alias`、多余键、`*` 用于非 count、count 的 field 不是 `*`、count_distinct 的 field 不是普通字段、未知 aggregate（含 `sum`）、不安全 alias/字段名、重复输出列名（含 alias 与已选字段同名）、超过 24 项。`select` 必须是 1..24 项的非空数组。

## 参数绑定

| 已批准 query 参数 | 绑定规则 | 证据边界 |
|---|---|---|
| `dataset` | 必须与回包 `dataset` 相等 | 观察到的字段语义 |
| `select` | 逐项解析为期望输出列名；回包 `columns[].name` 必须按顺序完全相等 | 已观察的结构化 select 线格式；只接受 COUNT / COUNT DISTINCT 聚合 |
| `group_by` | 每项必须是安全字段名、唯一，并且是**已选中的普通字段**；引用聚合 alias、未选字段或重复项都拒绝 | 分组字段必须匹配 `group_by` 的观察规则 |
| `order_by` | 必须是对象数组；每项含 `field`（必须是输出名或 alias），可选 `direction` 仅检查为 ≤32 字符串，**不解释**方向值 | 排序使用输出名/alias；方向词表未证明 |
| `filters` | 只检查是数组并标记 `filtered`；**不解析**字段、操作符或值 | 嵌套过滤语义未观察 |
| `page` | 参数存在时必须与回包 `page` 相等；缺省时不假设默认值 | 页码基准未证明 |
| `rows` | 参数存在时必须与回包 `rows` 相等；缺省时不假设默认值 | `rows=页大小` 已观察 |
| `include_total` | 为 `false` 时回包不得带 `total`；为 `true` 时 `total` 仍可缺失（未知，不是 0） | `total` 为可选输出行/组数 |

投影输出新增：

- `select_column_binding = "exact_selected_field_or_alias"`
- `selected_output_columns`：按 select 顺序解析出的列名
- `aggregate_columns`：已接受的聚合项 `{field, aggregate, alias}`
- `has_aggregate`：是否存在已接受聚合项
- `aggregate_value_verified = false`：本模块只绑定列名，不核验聚合值

## 接受的原生结果范围

只接受下列形状的 `structuredContent`，且结果信封只允许 `content / structuredContent / isError / is_error`；不读取文本 `content`，不回退到 `structured_content`。

| 字段 | 约束 |
|---|---|
| `dataset` | 字符串代码；必须与同一已批准 query 的 `dataset` 完全一致 |
| `columns` | 1..24 项；每项恰好 `{name,label,type}`；`name` 唯一且为安全字段名；`label` 为 ≤200 字符字符串；`type` 为非空字符串，但词表不作解释；列名序列必须与 select 期望完全一致 |
| `data` | 位置矩阵；每个单元格必须是字符串/布尔/安全整数/有限浮点/`null`；每行宽度必须等于列数；最多 200 行 |
| `rows` | 1..1000 的整数，表示**页大小**，不是 `len(data)` |
| `page` | ≥1 的安全整数；页码基准未被观察证明 |
| `has_more` | 严格布尔值 |
| `total` | 可选，0..2^53-1 的整数；只按输出行/组数解释，绝不作为聚合单元格或底层记录数 |

额外拒绝：未知信封/载荷字段、错误标志为真或非布尔、列声明缺字段或重复、行宽不一致、非标量/非有限数/超长字符串、`len(data) > rows`、超过 24 列或 200 行、序列化后超过 96,000 字节。

## 分类与零/空区分

| 分类 | 条件 | 关键输出 |
|---|---|---|
| `valid_table` | `data` 非空且所有检查通过 | `returned_rows=len(data)`；有 0 单元格时 `zero_kind=zero_cells_present_not_empty` |
| `valid_empty_result` | `data` 为空且 `total=0`，或首页无 `total` | `empty=true`；后者 `empty_kind=empty_first_page_total_unknown` |
| `valid_empty_page` | `data` 为空且页码大于 1（`total` 未知或大于 0） | `empty_kind=beyond_last_page_not_dataset_zero` 或 `empty_page_total_unknown` |
| `None` | 畸形、未知 schema、select 列不匹配、矛盾 total/页码、空页却 `has_more=true` 等 | 不生成任何“零/空/完成”结论 |

数值 `0` 单元格与空结果是两条路径：`[[0]]` 是 `valid_table`、`empty=false`；`data=[]` 才是空页/空结果。空结果只说明本次查询未返回行，不说明数据集为空或业务为零。

## 分页与 total 口径

| 情况 | 输出 |
|---|---|
| `rows` | 始终单独保留为 `page_size`，不参与 `returned_rows` 计算 |
| `returned_rows` | 始终取 `len(data)` |
| `has_more=true` | `page_coverage=more_pages_not_returned`，本页不是全量 |
| `has_more=false` | 只表示本次查询没有后续页；不证明数据源完整或权限范围 |
| `total` 缺失 | `total=null`、`total_basis=unknown_not_zero`；未知不是 0 |
| 无 `group_by` 且有 `total` | `total_basis=output_rows`，只表示输出行数，不表示底层记录数 |
| 有 `group_by` 且有 `total` | `total_basis=groups_consistent_but_not_declared`、`total_basis_ambiguous=true`；可能按输出组数或记录数计数，口径未声明，不能直接当作记录数或完整组数，也不是分组聚合单元格 |
| 任意结果 | `total_is_aggregate_cell=false`、`total_is_record_count=false`、`global_coverage=false`、`scope_verified=false`、`complete=null` |

一致性检查只使用不依赖页码基准的安全边界：`total < returned` 拒绝；`has_more=true` 时 `total <= returned` 拒绝；首页空页却报告正 `total` 拒绝。对**非分组**结果，首页 `has_more=false` 时要求 `total == returned`，后续页 `has_more=false` 时要求 `returned < total <= returned + page*page_size`。对**分组**结果，`total` 可能按组数或记录数计，因此只保留在两种口径下都成立的 `total >= returned` 与 `has_more` 下 `total > returned` 检查，不用页码/页大小推断分组完整性。更完整的分页完整性仍需独立契约证明，因此固定 `pagination_completeness=requires_separate_proof`。

## 合成夹具交叉检查

测试读取仓库内已提交的 81 条合成用例：

- 8 条 describe 用例不是 query 结果信封，不进入本投影交叉检查；
- 其余 73 条：预期 `valid_table / valid_empty_result / valid_empty_page / valid_zero_aggregate` 的回包必须成功投影；预期 `failure / denied / malformed / inconsistent / rejected / unverified / not_queried` 或 `reply=null` 的用例必须返回 `None`；
- 夹具请求使用规范化的合成字段（`fields/page_size/sort/operation`），不是原生输入 schema。测试把每条请求翻译成已观察的结构化 select：`select → [{"field": f}]`、`count → [{"field":"*","aggregate":"count","alias":a}]`、`count_distinct → [{"field":f,"aggregate":"count_distinct","alias":a}]`、`group → [{"field":group_by}, 聚合项]`。分组请求不带聚合 alias，测试从回包的非分组列名派生一个**合成 alias**，仅用于跑通绑定，不构成客户或原生 schema 声明。
- `sum` 不在已知安全聚合集合内，模块按未知 aggregate 拒绝。夹具中的 4 条 SUM 用例（2 条未分组、2 条分组）在测试中断言为 `None`，不把算术真值包装成产品能力。
- 夹具中 `valid_zero_aggregate`（COUNT=0）在本模块中投影为 `valid_table`，并以 `zero_cell_count=1`、`empty=false` 区分；未分组计数仍由既有严格计数投影负责，矩阵模块只绑定 alias 列名，不重复认证聚合语义。

## 精确测试命令

从本工作树根目录：

```bash
cd server && python3 -m pytest tests/test_huawi_matrix_projection.py -q
```

等价形式（测试文件自行把 `server/` 加入 `sys.path`）：

```bash
python3 -m pytest server/tests/test_huawi_matrix_projection.py -q
```

**作者未执行上述命令。** 测试文件共有 46 个测试方法（含大量 `subTest` 参数变体），预期结果为 `46 passed`；若有失败，以主控独立运行的原始输出为准。不需要新增依赖，仅使用标准库与仓库已固定的 pytest。

## 预期逐项结果

| 覆盖 | 预期 |
|---|---|
| 合法分页表 / 短末页 | 成功；`returned_rows=len(data)` 与 `page_size=rows` 可不同 |
| 普通字段 select | 成功；`selected_output_columns` 与返回列名逐项相等 |
| COUNT / COUNT DISTINCT | 成功；列名必须等于显式 alias；`aggregate_value_verified=false` |
| 列多余/缺失/重排/重复 | 全部 `None` |
| alias 冲突、未知 aggregate、SQL 字符串、缺 field/alias | 全部 `None` |
| 分组字段不是已选普通字段（含引用 alias） | `None` |
| `order_by` 引用非输出名或畸形 | `None` |
| 首页空 + `total=0` | `valid_empty_result` |
| 首页空 + 缺 `total` | `valid_empty_result`，`total_basis=unknown_not_zero` |
| 越界空页 + `total>0` | `valid_empty_page`，不是数据集为零 |
| 0 单元格 | `valid_table`，`empty=false`、`zero_cell_count>0` |
| 分组页 | `total_basis=groups_consistent_but_not_declared`，组覆盖可能不完整，聚合值未独立核验 |
| 过滤/排序页 | 成功但标记 `filtered/ordered`，仍 `scope_verified=false` |
| SUM（未分组或分组） | `None`；不在已知安全聚合集合 |
| 错误/拒绝/畸形/矛盾 total | 全部返回 `None` |
| 未知字段、重复列、行宽不等、非标量、超长/超大 | 全部返回 `None` |
| 81 条夹具 | 总数 81；73 条非 describe 用例按预期成功或拒绝 |

## 限制与证据边界

- 原生请求/输入 schema 未观察；本模块只实现已观察的结构化 select 子集，过滤操作符、排序方向、页码基准均未证明。`filters` 内容完全不解释；`order_by` 只做输出名与字符串类型检查，不解释方向。
- 只接受 COUNT 和 COUNT DISTINCT 聚合。SUM、分组 SUM 及其他未知 aggregate 一律失败关闭；既有严格计数投影 `huawi_aggregate_receipt.py` 保持不变，本模块不认证任何聚合值；COUNT/COUNT DISTINCT 单元格必须为非负安全整数，不接受布尔、字符串、空值或浮点数。
- `columns[].type` 的原生词表未证明；模块只检查其为非空字符串，不按类型转换或解释单元格。
- `total` 一致性检查只覆盖不依赖页码基准的必要条件，不证明分页完整；`has_more=false` 也不证明数据源完整或权限范围。
- 分组（COUNT）、过滤、分页输出全部标记 `support_status=synthetic_candidate_not_product_support`；没有客户支持、生产验收或真实数据正确性声明。
- 本模块不产生 `business-result/v1` 结果卡，不接 runtime/bridge；后续若接入，仍需单独实现卡片映射、审批绑定和运行时测试。
- 上限为 200 行、24 列、单元格 2,000 字符、投影 JSON 96,000 字节；超限返回 `None`，不做截断。
- 测试全部为合成数据；作者未执行测试，主控负责独立运行与记录。

## 变更文件

- 新增：`server/app/huawi_matrix_projection.py`
- 新增：`server/tests/test_huawi_matrix_projection.py`
- 新增：`docs/huawi-parity/matrix-projection.md`
- 未修改：`server/app/huawi_aggregate_receipt.py`、`server/app/business_results.py`、运行时、路由、前端、夹具和既有测试。
