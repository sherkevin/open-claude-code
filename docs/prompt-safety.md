# 开物提示词审核

本分支选用本机 Qwen3Guard-Gen-0.6B 审核通用风险，并用独立 JSON 规则执行部署方的主题政策。审核开启后，新输入先经过审核，再进入自动选模、主模型或工具流程。

## 开源选型

以下核对基于 2026-11-22 可查的官方材料。自托管表示模型或规则可在隔离环境内运行。企业使用还要核对拟安装版本、发行方式和依赖许可。表中体积是权重或估算，吞吐随设备、上下文和并发变化。本轮延迟实测只针对部署规则与 Qwen。

| 方案 | 许可与自托管 | 中文和部署策略 | 接口、硬件和维护信号 |
| --- | --- | --- | --- |
| **Qwen3Guard-Gen-0.6B** | 公开 BF16 权重附 Apache-2.0 许可。GitHub 根目录未发现单独的代码许可文件，本实现不复制其评测代码。推理依赖 Transformers 或 vLLM，许可证见下文。 | 官方覆盖中文及多语言，返回固定风险类别及三级标签。内置政治类别有模型自己的定义，无法直接表达部署方的词表和例外。 | 下载权重后用 Transformers 原生模板推理，或用 vLLM 的原生 Chat Completions 服务。所测权重 `1,503,300,328` 字节。项目公告列出 2025-10-16 的评测发布，所用模型快照更新于 2025-11-07。离线运行时提示词只到本机。 [GitHub](https://github.com/QwenLM/Qwen3Guard) · [模型卡和原生接口](https://huggingface.co/Qwen/Qwen3Guard-Gen-0.6B) · [权重许可](https://huggingface.co/Qwen/Qwen3Guard-Gen-0.6B/blob/main/LICENSE) |
| **ShieldLM-14B-qwen** | 仓库代码 MIT，模型卡标注 MIT，原始 Qwen-14B-Chat 另有通义千问协议，包括再发行署名和大规模商用条件。企业须确认基座义务。 | 官方支持中英双语和追加审核规则，主要面向检查模型生成内容。中文输入审核、规则稳定性和政企样本无本项目实测。 | 项目提供 Transformers 推理脚本。官方加载例启用自定义代码，离线前需审查并固定版本。BF16 权重约 28 GB，另需推理内存。公开仓库只有少量提交，主要发布记录是 2024 年的研究版本。数据可保留在自管推理进程。 [GitHub 和许可](https://github.com/thu-coai/ShieldLM) · [模型卡](https://huggingface.co/thu-coai/ShieldLM-14B-qwen) · [基座许可](https://huggingface.co/Qwen/Qwen-14B-Chat/blob/main/LICENSE) |
| **Llama Guard 3-1B** | 可自托管的公开权重，Hugging Face 模型卡标注 Llama 3.2 社区许可。GitHub 文档和许可文件使用历史许可版本，采用时应以所下载分发物的条款为准，并核对可接受使用政策。 | 官方列出 8 种支持语言，未列中文。固定 MLCommons 风险类别，政企主题仍需另接规则。 | Transformers 模板可本地推理，常规 1B 权重约 2 GB，官方还介绍了 INT4 方案。系列公开发布于 2024 年。无本项目延迟实测。 [GitHub 和许可文件](https://github.com/meta-llama/PurpleLlama/tree/main/Llama-Guard3/1B) · [模型卡](https://huggingface.co/meta-llama/Llama-Guard-3-1B) · [原生格式](https://dev.meta.ai/llama/docs/model-cards-and-prompt-formats/llama-guard-3) |
| **NVIDIA NeMo Guardrails** | 框架代码 Apache-2.0，可部署 Python 服务。实际分类模型、Ollama 等推理后端以及可选 provider 依赖的许可单独计算。 | 可配置输入检查、规则及不同模型。中文表现取决于所选后端和策略验证。 | 使用 `nemoguardrails server` 或 Python 输入检查。v0.25.0 发布于 11-19，要求 Python 3.11–3.13，开物仍支持 3.10，需独立环境。规则只需要 CPU，模型资源和耗时取决于后端。内网部署应在启动前设置 `NEMO_GUARDRAILS_NO_USAGE_STATS=1`，并选本地后端。 [代码许可](https://github.com/NVIDIA-NeMo/Guardrails/blob/develop/LICENSE-Apache-2.0.txt) · [服务文档](https://docs.nvidia.com/nemo/guardrails/run-guardrailed-inference/using-fastapi-server/run-guardrails-server) · [发布记录](https://github.com/NVIDIA-NeMo/Guardrails/releases) · [遥测文档](https://docs.nvidia.com/nemo/guardrails/latest/resources/telemetry) |
| **Protect AI LLM Guard** | Python 扫描库和 API 代码 MIT。所查 BanTopics 默认线使用 DeBERTa zero-shot，模型卡标注 MIT；ONNX Runtime 为 MIT。每个扫描器的权重仍需分别核对。 | `BanSubstrings` 支持词表和全部命中，`BanTopics` 能挂分类器。所查默认 DeBERTa 模型卡标注英语，更换中文模型需另测。 | 原生 `POST /scan/prompt`，可从本地目录加载模型。词表 CPU 即可，分类器可用 CPU/ONNX，耗时取决于模型。仓库于 2026-07-09 归档，只读，未选为新依赖。 [代码许可](https://github.com/protectai/llm-guard/blob/main/LICENSE) · [扫描器配置](https://github.com/protectai/llm-guard/blob/main/llm_guard_api/config/scanners.yml) · [自托管 API](https://protectai.github.io/llm-guard/api/deployment/) · [模型卡](https://huggingface.co/MoritzLaurer/deberta-v3-base-zeroshot-v1.1-all-33) · [ONNX Runtime 许可](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) · [归档记录](https://github.com/protectai/llm-guard/releases) |
| **YARA-X 与轻量词表** | YARA-X 代码 BSD-3-Clause，Python wheel 含 Rust 原生库，无模型权重。分发需核对选用模块及 crate 许可。此分支的小词表复用 Python 标准库与现有 Pydantic，无新增运行时依赖。 | 对 UTF-8 文本可做确定性匹配和例外。两种方式均依赖维护词表，语义改写、同音词或未列出的繁简体没有通用识别保证。 | YARA-X 提供 `yara_x.compile` 和 `scan`，发布页列出 1.23.0。CPU 扫描、全程本地。当前规则数量和表达力适合直接使用 JSON 词表，无需部署另一个进程。 [YARA-X 许可](https://github.com/VirusTotal/yara-x/blob/main/LICENSE) · [规则文档](https://virustotal.github.io/yara-x/docs/writing_rules/text-patterns/) · [Python API](https://virustotal.github.io/yara-x/docs/api/python/) · [发布记录](https://github.com/VirusTotal/yara-x/releases) |

采用 Qwen 的主要依据是中文原生支持、公开的小体积权重、独立的本机接口，以及能用部署方配置控制类别。现有开物依赖 [HTTPX（BSD-3-Clause）](https://github.com/encode/httpx/blob/master/LICENSE.md) 和 [Pydantic（MIT）](https://github.com/pydantic/pydantic/blob/main/LICENSE) 用于接线和校验，不新增主服务运行时依赖。复跑使用 [Transformers（Apache-2.0）](https://github.com/huggingface/transformers/blob/main/LICENSE) 与 [PyTorch（BSD-3-Clause 及第三方声明）](https://github.com/pytorch/pytorch/blob/main/LICENSE)。原生服务可选 [vLLM（Apache-2.0，CPU 或 GPU 另有平台依赖）](https://github.com/vllm-project/vllm/blob/main/LICENSE)。商业部署应按实际选定的镜像或 wheel 版本留存许可证，特别是选用 GPU 驱动及其运行库时。论文指标与本次合成结果都不能替代客户语料验收。

## 运维配置

| 服务端变量 | 值与行为 |
| --- | --- |
| `AGENT_PROMPT_SAFETY_MODE` | 默认 `off`。`rules` 只执行主题规则。`qwen` 依次执行主题规则和通用审核。分类器故障时须恢复后重试，`rules` 是管理员可以明确选择的有限模式。 |
| `AGENT_PROMPT_SAFETY_POLICY` | 设置当前服务可读的 JSON 文件绝对路径。用 [示例策略](../server/prompt-safety.example.json) 起步，其中地名和主题都是虚构验收数据。生产策略可放在独立的只读配置目录。 |
| `AGENT_PROMPT_SAFETY_QWEN_URL` | 默认 `http://127.0.0.1:8091/v1`。只接受带端口的 `127.0.0.1` 或 `[::1]`，禁用代理、重定向和 URL 凭据。容器运行时审核进程须和开物服务共享网络命名空间，或同在原生宿主机。 |
| `AGENT_PROMPT_SAFETY_QWEN_MODEL` | 默认 `Qwen/Qwen3Guard-Gen-0.6B`。设置为本地服务为该权重暴露的模型名。Transformers 服务使用绝对权重路径作为模型名时，开物端配置相同路径。 |
| `AGENT_PROMPT_SAFETY_TIMEOUT_SECONDS` | 默认 `10` 秒，可设在 `(0,120]`。应按本机预热和请求延迟设置。取值无效时拒绝需要审核的请求。 |

策略文件每次审核读取。管理员可用临时文件和同文件系统的原子替换更新内容，无需重启。`category` 是固定日志标识。按数组顺序匹配启用规则，`terms` 至少命中一项，非空 `require_any` 还要求至少一项上下文短语命中。`except_exact` 只对本条规则的整条输入生效。匹配先做 Unicode NFKC、大小写折叠，忽略空白、标点和格式控制字符。繁简同义词和口语变体按需配置。规则不读取用户身份或立场。不要把“合同”“风险”“安全培训”等业务词作为没有上下文条件的拒绝词。`message` 和 `blocked_message` 是管理员编写的固定提示，不应包含内部术语或敏感词表。规则例外仍经过通用审核。

`qwen_block_levels` 默认只拒绝 `Unsafe`。`qwen_block_categories` 默认包含模型的 8 个通用类别。`Politically Sensitive Topics` 仅作为可解析的原生标签，部署主题由管理员规则决定。模型标签的组合无法代表所有企业风险。可用规则细化主题，或在合成用例验收后调整通用类别和风险级别。审核实发原始单条用户文本，没有对话历史、整套系统提示、附件、凭据和用户身份；主模型继续按现有的模型路由配置处理审核通过的请求。

## 本地部署与复跑

先按 [Server 安装说明](../server/README.md) 安装应用和测试依赖。规则模式可直接试用。由应用服务进程读取环境变量，Compose 另需按实际路径只读挂载策略文件。

```bash
export AGENT_PROMPT_SAFETY_MODE=rules
export AGENT_PROMPT_SAFETY_POLICY="$PWD/server/prompt-safety.example.json"
```

要使用 Qwen，先把下列公开权重和分发许可下载到可缓存的位置。隔离环境可由离线介质导入同一快照。安装阶段只下载依赖和模型，不发送提示词。

```bash
hf download Qwen/Qwen3Guard-Gen-0.6B \
  --revision fada3b2f655b89601929198343c94cd2f64d93cc \
  --local-dir .venv/qwen3guard
```

可用独立 Python 环境启动 [Transformers 的原生服务](https://huggingface.co/docs/transformers/v5.19.0/serve-cli/serving)。CPU 复跑使用 PyTorch `2.10.0+cpu`、Transformers `5.19.0`。原生服务使用 FastAPI（MIT）、Uvicorn（BSD-3-Clause）和 OpenAI Python 类型库（Apache-2.0），运行期间无需外部 API。 [FastAPI 许可](https://github.com/fastapi/fastapi/blob/master/LICENSE) · [Uvicorn 许可](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md) · [OpenAI Python 许可](https://github.com/openai/openai-python/blob/main/LICENSE)

```bash
python3 -m venv .venv/reviewer
.venv/reviewer/bin/pip install --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.10.0+cpu' 'torchaudio==2.10.0+cpu' 'torchvision==0.25.0+cpu'
.venv/reviewer/bin/pip install 'transformers[serving]==5.19.0'
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 \
  .venv/reviewer/bin/transformers serve "$PWD/.venv/qwen3guard" \
  --host 127.0.0.1 --port 18091 --device cpu --dtype float32 --log-level error
```

用应用测试环境执行以下命令，可通过实际审核模型拦截一条合成风险请求，并放行一条正常输入给合成主运行时。`120` 秒是本机 CPU 烟测的时限，目标环境需重新测量。

```bash
export AGENT_PROMPT_SAFETY_QWEN_URL=http://127.0.0.1:18091/v1
export AGENT_PROMPT_SAFETY_QWEN_MODEL="$PWD/.venv/qwen3guard"
export AGENT_PROMPT_SAFETY_TIMEOUT_SECONDS=120
KAIWU_TEST_QWEN_URL="$AGENT_PROMPT_SAFETY_QWEN_URL" \
  python -m pytest -q -c server/pyproject.toml server/tests/test_prompt_safety.py -k real_local_qwen --durations=3
# 可选全样本 HTTP 对照，CPU 上耗时更长。
python scripts/evaluate_prompt_safety.py --url "$AGENT_PROMPT_SAFETY_QWEN_URL"
```

Qwen 上游文档给出 vLLM 原生接口。下载后断开外网，在与应用同一网络命名空间中运行下述命令。安装按目标环境选 [vLLM CPU](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/) 或 [GPU](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/) 包。禁用请求体日志，服务只监听回环地址。`--max-model-len 32768` 包含 Qwen 原生审核模板和生成空间。该 vLLM 命令是部署路径，未在本机验证 vLLM GPU/CPU 引擎。

```bash
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 VLLM_NO_USAGE_STATS=1 \
  vllm serve "$PWD/.venv/qwen3guard" --host 127.0.0.1 --port 8091 \
  --served-model-name Qwen/Qwen3Guard-Gen-0.6B --max-model-len 32768 \
  --disable-log-requests
```

```bash
export AGENT_PROMPT_SAFETY_MODE=qwen
export AGENT_PROMPT_SAFETY_QWEN_URL=http://127.0.0.1:8091/v1
export AGENT_PROMPT_SAFETY_QWEN_MODEL=Qwen/Qwen3Guard-Gen-0.6B
```

以下命令均在仓库根目录运行。第一条运行隔离的 API 回归，Qwen HTTP 结果和主模型流程使用测试替身。第二条直接用同一权重、原生模板和解析器在 CPU 跑 24 条合成样本，需另外安装 `transformers==4.57.1`、`torch==2.5.1` CPU 发行包与 `safetensors`。已有本机原生兼容服务时，将 `--model-dir` 换成 `--url http://127.0.0.1:8091/v1` 即可验证 HTTP 路径。脚本只输出样本 ID、标签、判定和耗时，退出码非零表示分类服务或格式异常。策略效果以结果里的漏拦和误拦计数为准。

```bash
python -m pytest -q -c server/pyproject.toml server/tests/test_prompt_safety.py
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 \
  python scripts/evaluate_prompt_safety.py --model-dir .venv/qwen3guard
```

## 实测记录

合成中文样本 24 条，其中应放行 9 条、应拦截 15 条。已跑官方权重 `fada3b2f655b89601929198343c94cd2f64d93cc`，仅 CPU，Intel Xeon E5-2620 v4、PyTorch 2.5.1+cpu、Transformers 4.57.1、FP32、4 线程。首轮含加载与预热 11.19 秒，整个进程峰值 RSS 4,952,520 KiB。单请求延迟取预热后的短文本串行样本。逐条判定和耗时见[原始结果](evidence/prompt-safety/zh-CN-cpu.json)。

所测 `model.safetensors` 的 SHA-256 为 `4f3ce47ebd968cddb67de08d8764f8ede7c410a7d1fb9e08145a4c7a2f2e5c0f`。工作树内用 `npm --prefix client run build` 构建页面，再用无头 Chromium 访问本地应用 API 和合成运行时。实测直发 `403` 与 `503` 提示均可见，历史无新增消息，恢复策略后 `202` 显示正常回复，队列编辑被拒时提示在编辑窗口内。

同一权重的 Transformers 原生 HTTP 服务额外通过了 `-k real_local_qwen` 烟测。真实审核拦截一条合成诈骗请求，未触发主运行时。正常请求通过审核并由合成主运行时返回回复。两条请求的测试调用合计 24.32 秒。较早一次同口径请求在 30 秒超时后返回 `503`，单独观察到一次原生 HTTP 正常请求耗时 33.54 秒。下表延迟来自 4.57.1 直接加载权重的串行对照，HTTP 目前仅做双样本烟测。

| 方案 | 正确 | 漏拦 | 误拦 | 审核异常 | 延迟中位数 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 只用主题规则 | 18/24 | 6 | 0 | 0 | 4.25 / 5.6 ms |
| 只用 Qwen 通用类别 | 14/24 | 10 | 0 | 0 | 3,249.95 / 4,409.7 ms |
| 组合 | 23/24 | 1 | 0 | 0 | 规则命中时无需调用模型，其余请求需加一次模型审核 |

规则拦截 10 条部署主题中的 9 条。Qwen 通用类别拦截 5 条通用风险样本。组合漏掉 `election-no-keyword`，该口语改写没有命中明确的主题词，Qwen 返回 `Safe`。合成合同评估、安全培训、防诈骗咨询、公开政务清单和精确例外均放行。合成的两种相反立场均按同一主题规则处理。这里的 0 误拦只代表这 9 条放行样本。CPU 并发、长文本延迟、客户分类精度、真实政企语料和部署方策略效果尚待验证。

## 用户可见行为与范围

- `403 PROMPT_BLOCKED` 显示策略提示。`503 PROMPT_REVIEW_UNAVAILABLE` 显示暂不可用并提示重试。Qwen 模式下超过 `8,000` 字符返回 `422 PROMPT_REVIEW_LIMIT`，避免截断后的文本被误当作整条审核。启用后，配置失效、审核超时、异常 HTTP 状态或未知返回格式均停止提交。新拒绝不会创建消息或执行记录。已经被接收的请求 ID 仍按原有幂等规则返回原执行记录。
- 主对话直发、待发队列新增、编辑与发送、独立选模、Tab 补全、上下文压缩输入共用服务端策略。飞书桥提交走同一个消息准备入口，本轮只验证本地 Web 与 API。策略变化后，旧待发草稿发送时会复审，失败则暂停并在队列显示原因。草稿仍可编辑或取消。先前保存的草稿和历史消息保留原有读权限。
- 新审核日志只记录拒绝来源与类别，或固定的错误类别。正常放行不产生日志记录。审核服务无需凭据。任何模型、代理或容器日志也需关闭请求体日志，开物无法替该进程控制日志设置。
- 未纳入的边界包括附件正文和图片、Skill 与系统提示模板、检索和工具结果、已保存的历史文本、自动的后续问题生成、工作流和独立兼容网关。仅附件且正文为空时无可审核的提示词。本轮以输入为范围，正常回复沿原接口和 SSE 流式路径显示。
