# 工作流搭建组件选型决策

- 日期：2026-11-07
- 项目：知行工作台
- 状态：已完成前端隔离验证，采用 FlowGram
- 触发原因：当前工作流搭建界面是自研的基础版本，节点、表单、变量和画布交互仍然粗糙；项目要求优先复用成熟开源能力，避免重复建设。

## 结论

工作流编辑器优先评估并复用 **FlowGram** 的画布、节点表单、变量和物料能力，在前端以适配器接入现有工作流接口。暂时不把整套 Coze Studio 或 Dify 替换进现有后端，也不把 n8n 或已停止维护的 Flowise 作为嵌入式编辑器。

这次先在前端独立工作树完成最小验证，不改变当前后端接口，也不阻塞后端工程师的未提交工作；验证通过后再合并画布实现。

## 本次实现与验收

- 前端使用 `@flowgram.ai/free-layout-editor@1.0.16`，许可证为 MIT；实现位于 `client/src/FlowGramWorkflowCanvas.tsx` 和 `client/src/workflowFlowgram.ts`。
- 适配器把现有 `WorkflowGraph` 转成 FlowGram 的 `WorkflowJSON`，并将画布位置保存在业务配置里的保留字段 `config.__ui.position`；节点类型、业务配置和服务端接口保持不变。
- FlowGram 只在独立前端工作树中接入，未修改 `server/`；主工作树已有的后端未提交改动继续保留。
- 已完成 TypeScript/Vite 构建、前端自动化测试和浏览器验收：17 个测试文件、74 个测试通过；工作流页面可加载节点、连线、缩放、适应画布、撤销重做和节点名称编辑。
- 回滚方式是恢复合并前的 `client/` 内容，服务端无需回滚；远程仓库不会因本次本地合并自动推送。

## 候选评估

### FlowGram：首选编辑器组件

FlowGram 是可扩展的工作流开发框架，提供画布、表单、变量和物料等编辑器基础能力，采用 MIT 许可证。它的边界更接近“复用工作流编辑器”，适合当前 React 和 TypeScript 前端；Coze Studio 也使用其工作流画布能力。

接入方式：保留现有 Python 后端和工作流数据模型，在前端增加 `WorkflowEditorAdapter`，把 FlowGram 的节点、连线、变量和表单转换成现有接口；通过功能开关逐步替换当前粗糙画布。

### Coze Studio（用户提到的“codz”按 Coze 理解）：整套平台候选

Coze Studio 是 Apache 2.0 许可证的开源智能体和应用开发平台，包含工作流、知识库、插件、数据库和模型配置，适合快速搭建完整平台或作为隔离服务评估。它包含 Go 后端和 React、TypeScript 前端，部署依赖比单独复用编辑器大，直接替换当前系统会带来身份、数据模型、运行时和运维整合成本。

结论：作为整套能力的对照实现或独立服务候选，不直接嵌入当前产品；先用同一批文档验证工作流、自动知识库处理和权限边界。

### Dify：强能力平台候选，但先做许可证审查

Dify 提供可视化工作流、智能体、模型管理和检索增强生成流水线，适合做独立平台或服务编排。它使用基于 Apache 2.0 的 Dify Open Source License，并附加多租户服务、前端品牌标识等条件，因此不能按“无条件 Apache 组件”直接放入本产品。

结论：可以作为独立部署的能力对照和接口集成候选，但在客户可见、多租户、品牌替换或商业分发场景使用前，必须完成许可证和商务确认。

### n8n：不作为嵌入式开源组件

n8n 的代码可见并允许部分内部使用，但其官方采用 Sustainable Use License / fair-code 条款，不是通常意义上的宽松开源许可证；向客户提供工作流编辑器或以服务形式开放相关能力可能需要商业协议。

结论：不作为当前产品的内嵌编辑器。只有在内部自动化场景且完成条款审查后，才单独评估。

### Flowise：不用于新建设计

Flowise 曾是 Apache 2.0 的可视化智能体和工作流项目，但官方已宣布代码冻结、仓库归档和终止维护，当前日期已经晚于其终止维护时间。

结论：不引入新系统；历史代码只作为交互参考，不作为依赖。

## 最小验证要求

1. 在独立分支和前端功能开关下接入 FlowGram，不修改服务器端接口。
2. 用现有工作流接口完成节点新增、连线、参数编辑、变量引用、保存、重新打开和错误提示。
3. 用上传文档触发当前自动处理链路，验证工作流节点能看到任务状态、证据来源和失败重试结果。
4. 对比当前版本的构建、前端测试、键盘操作、移动端布局和大图性能。
5. 记录包版本、许可证、构建产物、浏览器验证结果和回滚步骤；未达到门槛前保留当前编辑器。

## 官方来源

- FlowGram：<https://github.com/bytedance/flowgram.ai>；许可证：<https://github.com/bytedance/flowgram.ai/blob/main/LICENSE>
- Coze Studio：<https://github.com/coze-dev/coze-studio>
- Dify：<https://github.com/langgenius/dify>；许可证：<https://github.com/langgenius/dify/blob/main/LICENSE>
- n8n 许可证说明：<https://github.com/n8n-io/n8n-docs/blob/main/docs/n8n-community-license/README.md>
- Flowise 官方终止维护说明：<https://flowiseai.com/sunset>
