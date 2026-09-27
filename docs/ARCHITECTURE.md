# 架构与安全边界

AI Meeting Room V1.0.3 是本地应用。Electron Desktop Shell 启动只绑定 loopback 的 Product Shell；业务状态存于本地 SQLite。默认主脑是显式的 Manual GPT Handoff，正式执行 Agent 是经 CAO/tmux 管理的 Codex CLI。实验性 Browser Extension、网页自动化、MiniMax 和 API Brain 不属于默认稳定链路。

## 模块

```text
Electron UI / localhost Product Shell
                |
          Meeting Core ---- EventBus ---- SQLite
          /     |    \
 AgentRegistry TaskEngine SafetyEngine -- GlobalCircuitBreaker
          |         |          |
 RecoveryManager  RuntimeCoordinator
          |         |          |
 RuntimeToolResolver CAO adapter -- CAO HTTP -- tmux -- Codex CLI
          |
     WorkspaceManager

ManualGPTBrainTransport -- Brain Packet / Decision -- Meeting Core
```

- **Meeting Core** 持有会议生命周期和业务门禁；TaskEngine 负责任务状态、Result 与单飞派发。
- **AgentRegistry** 区分已加入与未加入的 Provider。未加入的 Provider 不阻塞会议；任何已加入成员的真实异常进入全局安全处理。
- **SafetyEngine / GlobalCircuitBreaker** 在 ERROR、LOST、UNKNOWN 等异常下开启全局暂停，停止新派发，尝试中断活跃 Agent，并保留触发原因。UNKNOWN 不视为健康。
- **RecoveryManager** 对 CAO、tmux、Codex 登录/运行时、工作区和持久化做健康门控。恢复需要显式确认；重启后不会自动续派。
- **RuntimeCoordinator / CAO adapter** 使用正式 CAO session 和终端生命周期。CAO `mode=last` 是终端 viewport，不是消息流；`Working (...)`、指令回显和部分输出不能越过完成边界成为正式 Result。真实最终响应须满足新鲜输出与生命周期稳定条件。
- **WorkspaceManager** 管理项目工作区；并行写入应使用隔离 worktree/任务分支，不把同一正式文件暴露给多个 Agent 同时写。
- **ManualGPTBrainTransport** 生成经安全检查的 Brain Packet，用户手动将它交给 GPT，再导入、校验和应用 Decision。它不自动读取网页 Cookie，也不直接调用 OpenAI API。
- **SQLite** 存储会议、任务、结果、事件与审计。备份、恢复、诊断和会议总结由产品的正式路径生成。

## 关键状态与原则

```text
健康执行:   READY -> RUNNING -> [Task Result] -> Brain Decision
故障:       joined Agent ERROR/LOST/UNKNOWN -> CircuitBreaker OPEN -> PAUSED
受控恢复:   PAUSED -> 全体健康检查 -> RECOVERING -> RUNNING
结束会议:   独立的 COMPLETE_MEETING 操作；ACCEPT != COMPLETE_MEETING
```

暂停期间 Meeting Core 拒绝新任务；不能仅依赖 UI 禁用按钮。恢复失败应保持暂停。运行状态和认证数据不属于源码仓库；详细使用与测试命令见 [README](../README.md)。
