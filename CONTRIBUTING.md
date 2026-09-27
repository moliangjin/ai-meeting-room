# 贡献指南

感谢参与 AI Meeting Room。请先阅读 [README](README.md)、[架构与安全边界](docs/ARCHITECTURE.md) 和 [SECURITY](SECURITY.md)。

## 开发环境

macOS、Python 3.10+、Node.js/npm 是桌面开发基础；真实 Codex 运行另需 tmux、CAO 和已人工登录的 Codex CLI。按 README 建立 Python 虚拟环境并运行 `npm --prefix desktop ci`。默认单元测试不需要触发真实 Agent。

提交前至少运行：

    .venv/bin/python3 -m unittest discover -s ai_meeting_room/tests -p 'test_*.py'
    npm --prefix desktop run test:unit
    npm --prefix desktop run check

涉及安全门禁、持久化、CAO 完成边界或恢复流程的修改，应补充对应回归测试。不要将“终端有输出”等同于正式完成结果。

## 提交与 Pull Request

1. 从最新 `main` 创建主题分支；每个 PR 聚焦一个问题，说明用户可见变化、风险和回滚方式。
2. 提交信息建议使用 `fix:`、`feat:`、`docs:`、`test:` 等清晰前缀。不要提交构建产物。
3. PR 中列出运行过的测试和真实结果；未能运行的测试要写清原因。只有明确授权时才进行真实 Agent live 验证。
4. 报告 Bug 时提供最小复现、预期/实际行为、应用版本和脱敏后的错误码；不要附带完整用户日志。

**绝对不要提交** secret、API key、token、Cookie、密码、用户数据库、日志、真实聊天、Brain Packet、认证状态、浏览器 Profile、CAO/tmux session、个人目录路径或包含这些数据的截图。发现安全问题请按 SECURITY.md 处理，不要在公开 Issue 粘贴敏感内容。
