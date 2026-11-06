# Project TODO

## 产品主线状态（Product mainline status，2026-11-06）

- **0.4.2 已发布。** 客户端、服务端、部署包三个正式归档及 `manifest.json` 位于 `dist/releases/0.4.2/`；0.1.0—0.4.2 六个正式版本的归档摘要都与各自清单匹配。0.4.2 的发布内容是会话并发隔离、会话独立记忆，以及切换模型后的身份查询修正。发布结论见 [交付验证](docs/DELIVERY_VERIFICATION.md)，工程明细见 [ENGINEER_TODO](docs/ENGINEER_TODO.md)，私有证据的当前位置见 [证据索引](docs/evidence/EVIDENCE_INDEX_2026-11-06.md)。
- **当前工作树已经超出 0.4.2。** 2026-11-06 远程交接同步了新的需求文档、模型目录/推荐路由和客户端改动；当前服务端可收集 322 项测试，客户端源码有 53 项测试，`client/node_modules` 已恢复。当前树不能用来重打 0.4.2 归档：`python3 scripts/build_release.py --version 0.4.2 --stage final --check-only` 当前给出 client=88、server=79、deployment=57，而 0.4.2 正式包是 57/56/57。0.4.2 归档保持冻结，下一版本应使用新的版本号和新的 FINAL 验证记录。
- **本轮需求审计与回归已完成（2026-11-06）。** 逐项审计见 [REQUIREMENTS_AUDIT_NOTES](docs/verification/REQUIREMENTS_AUDIT_NOTES.md)：27 个编号中 22 项已实现、4 项部分实现、1 项设计中；多模态知识图谱与生产 Sub-agent 仍明确为设计中。服务端当前开发树 322 项测试全部通过，客户端 53 项测试通过，TypeScript 检查和独立目录 Vite 构建通过；证据见 [服务端回归](docs/evidence/SERVER_REGRESSION_2026-11-06.md) 与 [客户端回归](docs/evidence/CLIENT_REGRESSION_2026-11-06.md)。
- **本轮已修复的最小不一致。** `server/tests/test_model_catalog.py` 在替换 provider 环境后显式重新选择同一公共模型，保留配置变化必须返回 `409 MODEL_SELECTION_REQUIRED` 的产品行为；README 已补齐混元 4 和 Kimi 3.1 PRO 的公开目录说明，并注明未开通时不可用。一次授权的腾讯云内网 `npm ci` 已完成，`package-lock.json` 未变化。
- **下一阶段待办（尚未开始）：**
  1. **补齐 0.4.2 发布树的独立回归证据。** 当前开发树的 322 项通过不能替代 0.4.2 发布树历史记录中 319 项收集、316 通过和 3 失败的口径；需要在不改正式归档的临时副本中确认发布树最终测试数量，并解释 317/319 与当前 322 的差异。
  2. **真实客户与模型开通验收。** 取得华微世纪真实 MCP 地址、认证方式、工具清单、业务样本、标准答案和原方案基线后，分别验收数据来源、知识来源、网页来源、质量、耗时和成功任务成本；确认混元 4、Kimi 3.1 PRO 的 TokenHub 上游与额度。
  3. **下一版本规划。** 多模态图谱和 Sub-agent 仍处于设计阶段；生产部署、systemd 重启、容器验收、生产负载和灾备演练保持独立待批准事项。
- **独立线程，不属于产品主线，保持原状：** Better AI PPT 截图理解（下方 2026-11-05 条目）、Linux Mint Live USB（下方 2026-11-04 条目）、旧 Intel Mac 启动（下方 2026-11-05 条目）继续各自独立记录。本轮不替用户决定它们是否继续由本项目跟踪，也不把它们排进产品版本计划。
- **仍待用户或商务决策（本轮未完成）：** 商业化、计费与转售授权；客户真实 MCP、业务样本和原方案基线；外部界面最终品牌；是否允许生产重启或部署下一版本。

## 2026-11-05 — Design multimodal RAG and knowledge graph extension

- [DONE] Inspect existing knowledge, evidence, permission, version, and run-snapshot contracts; architecture maps to implemented interfaces and calls out gaps.
- [DONE] Verify primary/open-source references for hybrid/multimodal retrieval, OCR/layout/table handling, graph-enhanced RAG, provenance, and access control; links and evidence boundaries are recorded in the design doc.
- [DONE] Write `docs/design/RAG_MULTIMODAL_GRAPH.md` covering ingestion, retrieval, graph, provenance, permissions, incremental updates, phased delivery, and acceptance criteria; interfaces are explicitly future proposals where not implemented.
- [DONE] Review design against current API; artifact: [RAG multimodal graph design](docs/design/RAG_MULTIMODAL_GRAPH.md). Validation is documentation inspection (`Get-Content -Raw docs/design/RAG_MULTIMODAL_GRAPH.md`, `Get-Content -Raw docs/API_CONTRACT.md`, `Get-Content -Raw docs/KNOWLEDGE_AND_SOURCES.md`) plus relative-link existence checks. No production code/deploy performed; implementation and benchmark evidence remain future work.

## 2026-11-05 — Understand Better AI from the supplied PPT screenshots

- [RUNNING] Read both attached images and extract company mission, vision, audience, product and values. Acceptance: distinguish explicit slide text from interpretation and ambiguous handwriting.
- [TODO] Independently review the company positioning against the image evidence. Acceptance: do not infer a hardware-manufacturing business or unshown product capabilities from the mission alone.
- [TODO] Record the grounded company understanding in `docs/BETTER_AI_COMPANY_CONTEXT.md` and explain it in Chinese. Acceptance: preserve the AI hardware industry-chain focus, enterprise-agent product direction and human-centered values; state the evidence boundary.

## 2026-11-04 — Deliver enterprise Agent client and server

- [DONE] Long-term client/server delivery completed2026-11-05: independent0.1.0 archives and SHA256 manifest in `dist/releases/0.1.0/`; verified native Tencent deployment relocated to `/data/dengkw/workspace/open-claude-code`, private browser preview available. Requirement evidence, tests, artifact links and customer-data boundaries are maintained in [ENGINEER_TODO](docs/ENGINEER_TODO.md) and [delivery verification](docs/DELIVERY_VERIFICATION.md).

## 2026-11-04 — Define the customer deliverable and internal runtime boundary

Context: The user is considering a branded enterprise agent service whose execution uses the official Claude SDK internally. The development environment is the remote Tencent Cloud server `open-claude-code`. This local note does not claim to describe or modify that remote repository.

- [SUPERSEDED] The API-first deliverable omitted a production user client and MCP administration. Replaced by the product/configuration flow task below after user correction.
- [DONE] Verify official SDK integration and branding guidance using the Agent SDK overview, hosting guide, Claude Code legal guidance, and commercial terms. Branding is optional; SDK hosting is stateful. Unified billing/resale authorization is not established by branding permission.
- [DONE] Define what stays internal versus what appears in the customer contract. See [delivery boundary](docs/customer-delivery-boundary.md): proposed API/event contract, tenant ownership, credentials, hosted versus private deployment, and disclosure limits.
- [DONE] Prepare the Chinese explanation and linked local decision note. Evidence: official documentation reviewed on 2026-11-04 and an independent product-boundary review. Documentation-only validation: `Get-Content -Raw TODO.md` and `Get-Content -Raw docs/customer-delivery-boundary.md`. No application tests are applicable; no remote inspection, implementation, deployment, or commercial authorization was performed. This completes the scope-definition task, not the product implementation.

## 2026-11-04 — Define the usable client and MCP configuration flow

- [DONE] Replace the API-only proposal with a concrete end-user and administrator product: a production Web workspace, business connections page, simple assistant settings, and application/execution services. Hosted access versus container deployment is explicit.
- [DONE] Establish MCP configuration, protected credential storage, authorization, testing from the execution node, and binding to an assistant/session. The revised note includes a complete QA onboarding flow without manual SDK configuration.
- [DONE] Distinguish the user-facing Web client from the runtime's protocol MCP client; document remote HTTP, private network, server stdio, and employee-local execution cases. Verified against official MCP architecture and SDK MCP/authentication documentation.
- [DONE] Update [delivery note, revision 2](docs/customer-delivery-boundary.md) and independently review the user/admin flow. Validation: `Get-Content -Raw docs/customer-delivery-boundary.md` and `Get-Content -Raw TODO.md`. Evidence limit: design and documentation only; actual customer MCP/network and remote repository remain uninspected, no code or deployment changes. Product implementation is not complete.

## 2026-11-04 — Create a verified Linux Mint live USB

- [DONE] Windows 11 build 26200 x64. E: KINGSTON maps to disk 1 Kingston DataTraveler 3.0, USB, 123730388992 bytes, USB instance E0D55EA574D9F621882D03CD. Disk 0 is the system NVMe. D: Data is fixed with 313787813888 bytes free. No matching ISO or tool found in the checked locations.
- [DONE] Downloaded the 3033710592-byte ISO from the listed kernel.org mirror into D:\open-claude-code\.mint-live-usb\linuxmint-22.3-xfce-64bit.iso. Full SHA-256 from resume_iso.py matched the official checksum and the user record: 45a835b5dddaf40e84d776549e0b19b3fbd49673b6cc6434ebddbfcd217df776.
- [DONE] Etcher official Windows x64 ZIP was too slow on this connection. Downloaded official Rufus 4.15 portable x64, verified release SHA-256 84c8a437f8af89257524478489e5c85f1edf25f761d299e2bcde46ac0afbe106 and valid Akeo Consulting Authenticode, then launched it in the authorized elevated verification session.
- [DONE] Full write verification investigated the raw SHA-256 difference with a complete 3033710592-byte comparison. Exactly 108 changed bytes add System Volume Information in formerly unused EFI FAT12 metadata/space. Independent reconstruction confirmed all four original EFI files, sizes, cluster chains and SHA-256 values unchanged; no other image bytes differ. Whole raw hash does not match and is explicitly documented. See [.mint-live-usb/verification-summary.md](.mint-live-usb/verification-summary.md), raw-comparison.json, and raw-comparison-status.log.
- [DONE] Windows safe-eject API returned CR_SUCCESS for the exact Kingston USB parent. Independent check at 2026-11-05 16:41:15 found physical disk and disk PnP node absent, USB removal code 47. Evidence: .mint-live-usb/final-safe-eject.log and final-device-state.json. Verification commands and evidence boundaries are in verification-summary.md. Stop at the verified live USB; actual Mac boot and Wi-Fi/audio tests remain for the user.


- Final evidence boundary: downloaded ISO SHA-256 matches official source; full written-content comparison confirms original system data and EFI boot files, with explained additional filesystem metadata. USB safely ejected. Actual boot on the Mac and its hardware compatibility are not yet tested.



























## 2026-11-05 — Boot the verified Mint USB on the old Intel Mac

- [DONE] Photo shows macOS unreadable-disk prompt. Official Canonical USB guide explicitly documents this warning after writing a Linux image and says not to initialize. Apple and Mint documentation confirm Option/Alt startup and EFI boot. Guidance: Ignore, retain USB, boot via Option, choose EFI Boot. The warning alone does not prove failure or successful boot.
- [WAITING] User boots the USB into the Mint live desktop and checks Wi-Fi, microphone and speakers. Actual Mac UI is only available through user-provided observations; no startup-manager/live-desktop result observed yet.
- [TODO] Once the live environment works, identify the internal Mac SSD separately from the similarly sized Kingston USB before any requested installation. No internal-drive erase has been performed or instructed in this troubleshooting step.

## 2026-11-06 — Remote development handoff to Tencent Cloud

- [DONE] Synced the latest source and documentation to `/data/dengkw/workspace/open-claude-code` without touching `.runtime/data`, `.venv`, production services, or the root-owned `l2-spec.md`; 27 content files changed and 17 overwritten files were backed up under `.runtime/sync-backups/20261106-remote-handoff/`. Key-file SHA-256 values match the local source; production services stayed active and `/api/v1/health/ready` returned ready.
- [DONE] Remote regression baseline: server `322 passed`, client `53 passed`, production client build succeeded; `client/node_modules` ownership/execute bits were repaired for the `ubuntu` account.
- [DONE] The client build initially replaced the live Nginx root `client/dist`; restored the exact 0.4.2 build from `dist/releases/0.4.2/zhixing-client-0.4.2.zip`, verified all four hashes against `remote-final.json`, and backed up the new build under `.runtime/sync-backups/20261106-remote-handoff/new-client-dist/`.
- [DONE] Remote agent completed `docs/verification/REQUIREMENTS_AUDIT_NOTES.md`, fixed the provider-fingerprint test fixture and README model-list inconsistency, and updated the requirements-alignment checklist with evidence links.
- [TODO] Production restart or deployment is a separate step and requires explicit user approval; keep the current systemd services and production data intact until then.

