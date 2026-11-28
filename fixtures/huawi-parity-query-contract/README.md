# 合成只读查询协议测试夹具（HW-029a）

状态：`synthetic_contract_fixture_not_product_support`。本目录只验证夹具自身的形状与合成数据的算术真值，不证明产品支持、客户输入协议或真实数据正确性。作者未执行测试，由主控独立运行。

## 核心判断

已观察到的原生结果形状与当前 `business-result/v1` 适配器不是同一种数据表示：原生用 `data` 位置矩阵、`columns[].name`、`rows=页大小`、`page`、`has_more` 和可选 `total`；当前适配器要 `rows/records` 对象数组、`columns[].key` 和 `total_rows`。因此当前基线不能无损表示分页、总量口径（记录还是分组）、数据集身份，也不能区分有效零、空页、空结果、缺失总量、失败、拒绝、未查询和畸形回包。

```text
describe 关键字/类型搜索 ──> 候选数据集（不是字段明细）
describe 精确 code     ──> 实际字段名 name + 显示 label + type
query  select/filter/sort/group/count/count_distinct/sum
        │
        └──> structuredContent
             dataset
             columns [{name, label, type}]
             data    [[cell, ...], ...]        位置矩阵
             rows    页大小（不是返回行数）
             page    页码（夹具假设 1-based）
             has_more
             total   可选；输出行/组数，不是聚合单元格
```

## 证据分层

| 文件 | 内容 | 证据等级 |
|---|---|---|
| `observed/native-envelope.json` | 原生结果字段与已观察语义 | 观察到的有界契约；明确列出未观察项 |
| `hypotheses/request-and-type-encoding.json` | 页码基准、类型词表、eq 操作、排序方向、别名等 | 合成假设；不是客户输入 schema |
| `dataset/synthetic_sales.json` | 9 行合成销售数据、2 个合成目录条目、发布/未发布字段 | 纯合数据；无客户标识 |
| `cases/*.json` | 81 条机器可读用例 | 请求为规范化合成操作；回复使用观察到的原生结果形状 |
| `adapter-gaps.json` | 当前基线适配器缺口与 HW-026 后最小跟进项 | 基线行为表征；不是产品验收 |
| `verify_fixture.py` | 仅标准库的夹具形状与算术真值检查 | 不导入产品运行时、不联网 |

`rows` 是页大小这一点在用例中明确验证：`query.pagination.page3_size4_short_last` 的 `rows=4`、`data` 只有 1 行、`total=9`，因此返回行数只能取 `len(data)=1`。

## 用例覆盖

| 文件 | 用例数 | 覆盖 |
|---|---:|---|
| `cases/describe.json` | 8 | 关键字搜索 vs 精确 code；搜索多候选、无匹配、精确命中、目录无字段、未知 code、用标题/标签冒充 code、搜索结果不能当字段明细 |
| `cases/query_fields_filters_order.json` | 12 | 发布字段选择；`*` 只用于 count；标签不是字段名；已发布字段过滤；未发布字段拒绝；未观察操作不认证；已发布字段排序；无排序不宣称顺序 |
| `cases/query_aggregates.json` | 12 | 未分组 COUNT / COUNT DISTINCT（已知安全、显式别名）；SUM（需独立证明）；按区域分组 count/sum（需独立证明）；未发布 group_by 拒绝；group 字段不匹配；缺 group_by；缺显式别名 |
| `cases/query_pagination.json` | 15 | 第 1/2/3 页；越界空页；短末页；缺 total；过滤后分页；`has_more`/`total` 矛盾；total 是聚合单元格的歧义；分组 total=组数；不完整分组第 1/2 页；不完整分组求和 |
| `cases/zero_empty.json` | 8 | 有效零计数；有效零去重计数；空结果；缺 total 未知；失败；拒绝；未查询；畸形回包 |
| `cases/malformed.json` | 26 | 非对象、错误标志带数据、缺字段、列不是数组、空列、列非对象、缺 name/label/type、重复列名、data 非数组、行非数组、行宽短/长、单元格非标量、非有限数、rows/page/has_more/total 类型错误、页大小为 0、data 超过页大小、total 小于返回行 |
| **合计** | **81** | |

## 合成数据真值

| 指标 | 值 |
|---|---:|
| 记录数 | 9 |
| `amount_cents` 合计 | 82500 |
| `quantity` 合计 | 58 |
| 区域去重数 | 3 |
| 产品去重数 | 3 |
| 区域分组计数 | SYNTH-R1=4，SYNTH-R2=3，SYNTH-R3=2 |
| 区域分组金额 | SYNTH-R1=31000，SYNTH-R2=24500，SYNTH-R3=27000 |
| 不完整分组第 1 页（2 组）金额 | 55500 / 全量 82500 |
| 不完整分组第 1 页（2 组）计数 | 7 / 全量 9 |

这些数值由 `verify_fixture.py` 从 `dataset/synthetic_sales.json` 重新计算，并同时与用例 `expected` 对比；不是产品返回值的声明。

## 当前适配器缺口

| 缺口 | 原生观察 | 当前适配器 | 后果 |
|---|---|---|---|
| 位置矩阵 | `data` 是列表的列表 | 要 `rows/records` 对象数组 | 原生回包直接不投影 |
| 列名 | `columns[].name` | 要 `columns[].key` | 即使换成对象行也因缺 key 被拒 |
| `rows` 语义 | 页大小整数 | 当成行数组 | 整数 `rows` 导致无表格；误映射会把页大小当匹配行数 |
| `total` 口径 | 输出行/组数 | 只有 `total_rows`，无口径 | 3 个分组可能被读成 3 条记录 |
| 分页状态 | `page`、`has_more`、页大小 | 无这些字段 | 无法表示部分页、越界空页或后续页 |
| 数据集身份 | `dataset` | 无 | 结果卡丢失数据集 code |
| 聚合单元格 | COUNT / COUNT DISTINCT 单元格 | 只有表格和返回行 SUM 声明 | 有效计数无法安全表示 |
| SUM/分组 | 不在已知安全集合 | 只认证显式返回行 SUM | 算术真值不能变成产品认证合计 |
| 缺单元格 | 行宽必须等于列数 | 对象行缺 key 会补 null | 有损转换可能伪造 null |
| 结果事实 | 返回行数取 `len(data)` | 读 `row_count`、`page_size` | 原生行数与页大小变 unknown |

`adapter-gaps.json` 是机器可读版本，每个缺口绑定具体 `case_id`。

## HW-026 后的最小跟进

1. 增加有界原生结果归一化：只接受已观察的 `dataset/columns/data/rows/page/has_more/可选 total` 形状，映射为 `business-result/v1`，不改授权或审批。
2. `columns[].name -> key`，严格按列位置 zip 生成对象行；拒绝行宽不一致、重复/缺 name/label/type、非标量、非有限数。
3. `returned_rows = len(data)`，绝不用 `rows`；单独保留 `page`、`page_size`、`has_more`。
4. `total` 必须带 `total_basis`（records/groups）和来源；口径未声明时标为 ambiguous/unknown，不能当记录数或聚合单元格。
5. 未分组 COUNT / COUNT DISTINCT 以显式别名表示为聚合结果，保留现有“非独立真值/非全量”措辞。
6. 不认证 SUM、分组 SUM、分组 COUNT 或分页完整性；`host_verified_aggregates` 仍只限既有“显式返回行 SUM”合同。
7. 输出明确结果分类：有效零、空页、空结果、缺 total、失败、拒绝、未查询、畸形。畸形必须失败关闭。
8. 更新 `result_fact_metadata` 使用归一化后的 returned_rows、page、page_size、total_basis，不编造 `row_count`。
9. HW-026 后重跑本夹具，并更新当前适配器表征测试；本夹具只证明算术和形状一致，不证明客户支持或生产验收。

## 运行方式

```bash
python3 fixtures/huawi-parity-query-contract/verify_fixture.py
python3 -m pytest server/tests/test_huawi_parity_query_contract_fixture.py -q
```

若从 `server/` 目录运行 pytest，用：

```bash
python3 -m pytest tests/test_huawi_parity_query_contract_fixture.py -q
```

## 限制与交接

- 未观察原生请求/输入 schema；用例中的请求是规范化合成操作，不是客户 schema。
- 未观察原生 describe 回复形状和错误回包；describe 用例是规范化语义观察。
- 页码 1-based、类型词表、`eq` 操作、排序方向、聚合别名拼写均为夹具假设。
- `malformed.cell_non_finite` 使用 JSON 数字 `1e999`，Python `json` 会读成 `inf`；若改用严格拒绝该数字的解析器，应把该用例视为解析器可移植性缺口，而不是通过。
- SUM、分组聚合、分页完整性只给算术真值，必须另行取得契约证明。
- 作者未运行测试；主控负责独立命令、结果记录和后续合并判断。
- 不接触客户数据、生产、`.runtime`、凭据或网络。
