# 服务端回归证据（2026-11-06）

## 环境

- 项目：open-claude-code（产品“知行工作台”）
- 服务器：腾讯云
- 工作目录：`/data/dengkw/workspace/open-claude-code/server`
- Python：项目 `.venv`，Python 3.10.12
- 测试依赖：pytest 8.4.2（来自项目虚拟环境）
- 运行边界：未重启 systemd，未读取或修改 `.runtime/data/`、`.runtime/backups/`、模型配置、管理员密码或客户凭据。

## 命令与结果

```bash
cd /data/dengkw/workspace/open-claude-code/server
../.venv/bin/python -m pytest -q --basetemp=/tmp/zhixing-server-regression-20261106 -p no:cacheprovider
../.venv/bin/python -m compileall app
```

结果：

- `pytest`：**322 passed, 1 warning in 40.16s，退出码 0**。
- 唯一警告来自第三方 `fastapi/testclient.py` 的 `StarletteDeprecationWarning`，提示 `httpx` 与 Starlette 测试客户端的未来兼容变化；不是项目源码警告。
- `compileall app`：**退出码 0**。
- pytest 输出日志副本：`/tmp/zhixing-server-pytest-20261106.log`（临时路径，不作为项目交付证据）。
- 编译输出日志副本：`/tmp/zhixing-server-compileall-20261106.log`（临时路径）。

## 本轮修正的测试前提

`server/tests/test_model_catalog.py::test_sdk_receives_selection_and_all_model_aliases` 的三个参数（`light-low`、`high-high`、`max-max`）原先在替换 provider 环境后直接调用 `runtime_config`，按产品契约应触发 `409 MODEL_SELECTION_REQUIRED`。本轮保留该产品行为，只在测试中通过 `service.patch_conversation(..., model_id="dano-v2.1", reasoning_level=level)` 明确重新选择同一公共模型，重新固定 provider 指纹，再断言 SDK 模型和 effort 映射。修正后的定向测试为 **3 passed, 1 warning**，随后全套 322 项通过。

## 版本口径

本证据针对 2026-11-06 当前开发树。2026-11-05 0.4.2 发布树的历史基线为 316 passed、3 failed；当前工作树已包含后续模型目录、推荐路由和客户端改动，不能把两套数字混成同一版本结论。
