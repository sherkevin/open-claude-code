# D2.3b｜Mem0 固定供应商 I/O 超时与重试边界

基线 0020102，独立工作树 kaiwu-mem0-provider-io-20261126。仅限已有隔离 pilot：Mem0 2.2.1、OpenAI SDK 3.59.0、本地 Qdrant client 1.20.0。未接产品或生产，不使用客户数据。核对固定 SDK 源码：Mem0 OpenAI embedding 与 LLM 构造未显式传 timeout/max_retries；OpenAI SDK 默认 600 秒、连接 5 秒、2 次重试。远程 Qdrant 适配器未透传 timeout，不纳入本次准入。

薄适配：构造后、实际调用前设置固定 SDK 客户端的 request timeout（默认 10 秒，允许 0.1–60 秒）与 max_retries=0；拒绝未验证的供应商、远程向量库、额外 reranker 或环境覆盖路由。沿用同步调用和跨进程单写锁，不使用线程超时提前返回。仅证明传输层 inactivity timeout，不能承诺整个同步批次时限、DNS/本地磁盘硬截止、远端取消、硬费用预算或未知写入结果回滚。实际产品接线前还需经批准的供应商线路、出站隔离、清理与资源生命周期。

测试证据目录：/media/data3/dengkw/acceptance/kaiwu-mem0-provider-io-20261126/。真实 SDK 8/8，退出码 0（104.45 秒）；后端 pytest 50/50，退出码 0（68.60 秒）。真实 SDK 以回环合成 HTTP 503 和停滞响应验证单次请求不重试、锁保留至本地调用实际返回后释放；100 份记忆回归通过。208 次正常回环 embedding、2 次故障 embedding、0 次提取请求。后端覆盖非法超时和未验证路由的拒绝。测试期间服务端停滞响应可以继续存在，不代表远端取消。

下一步：D2.3c 设计未知回执残留的显式对账、撤权后不依赖仍有读取授权的清理凭证及 SDK 生命周期关闭。继续保留生产发布闸门，不改变客户数据。
