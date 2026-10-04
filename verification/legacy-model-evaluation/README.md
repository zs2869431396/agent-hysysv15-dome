# 早期模型评估记录

这些文件从原 `_demo/` 目录迁入，保留供历史技术文档追溯：

- `llm-probe.json`：早期端点能力探测。
- `llm-eval.json`：早期模型评估。
- `smoke-agent.json`：修复前的智能体输出。

它们不是当前版本的测试结果或验收结论，不能用于判断当前功能是否通过。
当前验证请使用 `Run-Offline-Checks.cmd`、`scripts/run-all-tests.cmd` 和真机验收入口。

模型探测脚本之后生成的 `_demo/` 内容仅为本地运行产物，已整体排除出 Git。
