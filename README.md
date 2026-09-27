# AI Meeting Room

AI Meeting Room 是本地运行的多智能体 AI 会议室：GPT 负责主脑决策，Codex 通过 CLI Agent Runtime 执行任务。当前公开版本为 **V1.0.3**，以安全的手动 GPT 交接为默认路径，**不需要 OpenAI API Key**。

*English: A local, fail-closed multi-agent meeting room. GPT acts as the brain through an explicit manual handoff; Codex executes tasks through CAO. The default V1.0.3 workflow does not automate the ChatGPT website.*

## 当前能力

- 会议创建、开始、暂停、受控恢复和独立完成；成员加入与健康状态可见。
- Codex 通过 [CLI Agent Orchestrator（CAO）](https://github.com/awslabs/cli-agent-orchestrator) 与 tmux 运行；本机 CAO 健康服务可复用，缺失时由产品拥有的恢复流程安全启动。不会安装依赖、切换账号或接管未知进程。
- Task / Result 与 GPT Brain Packet；用户在自己的 ChatGPT 中手动获取 Brain Decision，再导入、校验、明确应用 ACCEPT 或 REWORK。接受任务 **不等于** 完成会议。
- 已加入会议的 Agent 故障触发全局 fail-closed 暂停；停止新派发，并尝试中断活跃执行。恢复须经过健康检查，不会因重启自动继续。
- CAO 终端 viewport 的中间 `Working (...)`、指令回显和非最终片段不能形成正式完成结果。
- SQLite 持久化、重启恢复、会议总结 Markdown 导出、本地备份/恢复、脱敏诊断和简体中文界面。

架构与安全边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 环境要求

目前面向 macOS。开发启动需要 Python 3.10+（见 [Python 项目配置](ai_meeting_room/pyproject.toml)）、Node.js/npm、Electron（由 [desktop/package.json](desktop/package.json) 声明依赖）和 Python Playwright。执行 Codex 任务还需要已安装的 `tmux`、`codex`、`cao-server`，以及用户本人已完成的 Codex ChatGPT 登录。CAO 和 Codex 的具体安装方式以各自上游文档为准；请确保命令在当前用户的 PATH 中可解析。应用不会替用户完成登录。

上游提供的安装方式示例（须先有 Homebrew、npm 和 uv；无需填写 API Key）：

    brew install tmux
    npm install -g @openai/codex
    uv tool install cli-agent-orchestrator
    codex login
    command -v tmux codex cao-server

CAO 的上游安装说明见 [CAO README](https://github.com/awslabs/cli-agent-orchestrator#install-cao)。正常产品启动时由 Product-owned Recovery 检查、复用或安全启动本地 CAO；不需要另外开一个终端常驻 `cao-server`。若当前 PATH 找不到它，请先修正当前用户的工具安装环境，产品不会自行安装。

## 从源码启动

在 macOS 终端中，从仓库根目录执行：

    python3 -m venv .venv
    .venv/bin/python3 -m pip install -e ./ai_meeting_room
    npm --prefix desktop ci
    AI_MEETING_ROOM_PYTHON="$PWD/.venv/bin/python3" npm --prefix desktop start

如果需要 Codex 执行，请先按上游说明安装 CAO、tmux、Codex CLI，并人工执行 `codex login`。启动后检查界面的运行时预检；依赖缺失时 Codex 应保持“不可用”，而不是伪装可加入。桌面启动使用单实例保护；本地 Product Shell 只绑定 loopback。

本地 macOS 未签名应用包可在上述依赖就绪后运行：

    .venv/bin/python3 scripts/build_v1_app.py

生成目录默认是 `dist/`。此源码包不包含签名、公证或可直接分发的二进制应用；如 macOS 阻止未签名应用，须按个人设备政策自行决定是否运行。不要将生成的应用包、运行数据库或浏览器 Profile 提交到仓库。

## 基本使用

1. 启动应用，创建会议并选择本机项目工作区。
2. 等待 CAO 自动健康检查/恢复；确认 Codex 显示可加入后，让它加入本次会议并开始会议。
3. 创建和派发任务，等待真实 Codex Result。中间终端进度不算结果。
4. 生成 Brain Packet，复制给自己已登录的 ChatGPT；将 GPT 返回的 Brain Decision 粘贴回应用，先校验再明确应用 ACCEPT 或 REWORK。
5. 故障或暂停后，只有健康检查全部通过才能受控恢复。任务接受后，可另行完成会议并导出会议总结。

默认本地数据保存在当前用户的应用数据目录（macOS Application Support）；实际位置由应用运行时配置确定。备份与诊断由用户主动生成，不随源码发布。

## 测试

    .venv/bin/python3 -m unittest discover -s ai_meeting_room/tests -p 'test_*.py'
    npm --prefix desktop run test:unit
    npm --prefix desktop run check

这些是无真实 Agent 派发的自动测试；需要真实 CAO/Codex 的 live 验收不由普通单元测试代替。贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 当前限制

- 正式 V1 Agent 是 Codex；MiniMax 等其他 Provider 尚未正式集成。
- GPT Brain 默认使用 **Manual GPT Handoff**。GPT 网页自动化是实验能力，可能被第三方网页验证阻断，不是默认稳定路径。
- OpenAI API Brain 不是 V1 默认依赖，也不作为网页登录失败的自动回退。
- 本仓库只发布源码，不包含用户数据库、日志、认证资料或 macOS 二进制。

## 开源许可证

AI Meeting Room 使用 **GNU Affero General Public License v3.0 only（AGPL-3.0-only）** 发布。Copyright (c) 2026 moliangjin。

你可以自由使用、研究、修改、分发和商业使用本项目。如果修改后向他人分发，需要按照 AGPL-3.0 的要求提供对应源码；如果修改后通过网络向用户提供服务，需要按其网络交互条款向这些用户提供对应源码。具体权利和义务以 [LICENSE](LICENSE) 中的 AGPL-3.0 正文为准。依赖项各自的许可证不因此改变。
