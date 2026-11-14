# 业务 MCP 请求约束

助手可按工具配置 `argument_constraints`，对已授权 MCP 请求再加一层确定性校验。
它为“只取所需字段”的要求提供可配置请求边界；只有实际配置并验证后才能约束该工具。真实字段名和含义必须来自已发现的工具定义，不能由平台猜测。

## 配置与行为

配置位于助手 `connections` 的每个绑定内：

```json
{
  "connection_id": "existing-connection-id",
  "allowed_tools": ["read_orders"],
  "auto_approved_tools": ["read_orders"],
  "argument_constraints": {
    "read_orders": {
      "type": "object",
      "required": ["fields", "limit", "factory"],
      "properties": {
        "fields": {
          "type": "array",
          "items": {"type": "string", "enum": ["id", "status"]},
          "minItems": 1,
          "maxItems": 2,
          "uniqueItems": true
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        "factory": {"const": "SYNTHETIC-F01"}
      }
    }
  }
}
```

以上仅为合成测试示例，未对应华微的真实工具 Schema。

- 空或缺省 `argument_constraints` 保留旧绑定行为。
- 只允许约束已绑定的工具；保存时连接须启用且已成功测试。
- 约束引用的参数必须在远端已发现 Schema 中明确声明。远端 Schema 缺失、复杂到无法安全验证或状态未核验时，拒绝启用。
- 每次请求必须同时满足远端已发现 Schema 与本地附加约束。原始参数不被改写：不补默认值、不裁剪字段、不自动改限额。
- 非约束字段仍由远端 Schema 决定是否合法。若需拒绝所有未列字段，应明确配置 `additionalProperties: false`；若需保证范围存在，应明确列入 `required`。
- `format` 仅支持 `date` 和 `date-time`，且启用格式验证；它不表示验证日期先后或时间跨度。固定业务窗口应使用已确认的值或另行实现有依据的规则。

## 有界配置和执行

附加约束采用 Draft 2020-12 的有限子集：`type`、`properties`、`required`、布尔 `additionalProperties`、`items`、标量 `enum`/`const`、数值大小与倍数、字符串长度、日期格式、数组数量与唯一性、对象字段数，以及 `allOf`/`anyOf`/`oneOf`/`not`。允许 `title`、`description`；不允许 `$ref`、正则表达式或 `default`。

- 单工具约束最大 32 KB；单连接绑定最多 64 个有约束工具，全部约束合计最大 128 KB。
- 约束 Schema 最多 128 个节点、8 层，每个组合规则最多 8 个分支；`uniqueItems: true` 必须同时明确 `maxItems <= 100`。
- 启用本地约束的远端 Schema 同样使用受支持的 Draft 2020-12 子集；引用、正则和旧方言不进入本地验证器。未启用约束的旧工具不因此改变行为。
- 请求在进入任一 Schema 验证器前，限制为 512 KB、最多 2,048 个值节点、12 层、每个数组/对象最多 100 项。超出返回固定错误，不静默截断。
- 以不可变规范 JSON 为键，用最多 64 项 LRU 复用 Schema 编译。权限、连接状态、凭据、请求参数与审批结果均不缓存。
- 已知连接凭据若出现在约束或远端 Schema 中，保存/调用会被拒绝，不把具体拒绝值写入错误。

## 两种运行时与工作流

`Service.authorize_tool` 是 SDK、Harness 与实时工作流的共享请求校验入口。
SDK 的自动批准 `PreToolUse` 与手动权限回调都调用该入口；Harness 在调用前和异步边界后重新授权。

任务创建时固定约束配置及远端已发现输入 Schema 的完整摘要。任务运行期间新增、删除、放宽或收紧约束，或已发现 Schema 变化，都会拒绝旧任务继续调用。

工作流用独立的权限检查方法准备工具清单，不伪造空参数请求。真正调用前仍须验证完整参数；工具范围摘要进入持久化审批快照，审批恢复时发生变化会要求重新运行。该检查方法不会执行任何工具，也没有跳过参数校验的执行开关。

## 不能由本功能证明的事项

这是一层**请求边界**，不替代业务系统身份权限，也不证明远端真的只返回这些字段、行数、工厂或时间范围，更不证明远端执行了聚合。
返回合同、实际字段/行数、业务正确性及 Token/耗时仍需单独验证。

约束不会把写工具变为只读工具。当前工作流历史实现以已绑定且自动批准的工具作为实时 `mcp_read` 候选；发现流程未保留 `readOnlyHint`，因此仅凭此标签不能宣称真实只读。

SDK 仍直接挂载远端 MCP Server，本功能拦截请求，但不能仅凭本地配置确认尚未重新发现的远端 Schema 变化；Harness 的传输路径另有实时 Schema 比对。若要求同等级的返回体治理、实时 Schema 验证和工具定义压缩，应另行统一受控代理入口。

本轮未把附加约束自动合入模型所见的工具定义，也没有完成业务域路由或自动选择聚合。当前验证的是越界请求被拒绝；模型能否首次生成正确、精简的请求，需要后续提示上下文、受控工具定义及真实任务联合验收。
