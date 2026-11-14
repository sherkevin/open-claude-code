# 官方应用能力适配设计

## 范围

本分支增加一个默认关闭的服务端适配器，覆盖官方 Search、Fetch 和 OCR
接口。它是知识库与网页研究的可选外部能力，不替换知行工作台当前的
TokenHub 聊天模型线路，也不改变默认 Bing RSS 行为。

协议依据：

- Search/Fetch 指南：<https://api-docs.deepseek.com/guides/search/>
- OCR 指南：<https://api-docs.deepseek.com/guides/ocr/>
- Search 参考：<https://api-docs.deepseek.com/api/search/>
- OCR 参考：<https://api-docs.deepseek.com/api/ocr/>

## 安全边界

- `AGENT_DEEPSEEK_API_ENABLED` 和独立的 `AGENT_DEEPSEEK_API_KEY` 缺一不可；
  未满足时不创建网络客户端。
- 生产请求只允许官方 HTTPS 主机；测试使用 HTTP 模拟传输而不是切换到
  任意外部地址。
- Fetch 目标先执行现有公开地址和重定向防护；不向外部服务发送内部地址、
  用户名或密码。
- 错误正文不转发上游响应，公共结果使用统一托管标识，凭据只放请求头。
- 只对 408、429 和 5xx 做一次有限重试；余额不足、认证失败和其他 4xx
  不重试。取消会立即向调用方传播。

## 知识库 OCR

本地解析器仍是首选。只有本地解析明确报告 `OCR_REQUIRED`，且应用能力和
OCR 开关都开启、独立密钥存在时，才把已授权的文件发送到 OCR 接口。文件
大小、页数、返回页数和文本均受界限约束，页结果转换为现有知识索引格式，
并保留中性的 `managed-ocr` 提取标记。没有密钥、线路不可用或响应无效时不会把
失败包装成已完成的本地 OCR。

## 当前不包含

- 未进行真实付费 API 调用或生产部署验收。
- 官方 Search 尚不支持图片搜索；图片搜索仍由现有网页工具负责。
- OCR 使用量仅作为接口返回的参考字段；计费与速率限制需要后续在获批
  授权后做真实验收。
