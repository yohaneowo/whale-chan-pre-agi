# 每日自动化演化研究（Antigravity Daily Research）

本目录由 Antigravity 自动化排程（Paseo Schedule）每日定时生成，用于跟踪 `discord-pre-agi` 项目的最新进展、审查拟人性与工程架构，并提出前沿的 `dsh`（DeepSeek Harness）插件概念与实装方案。

---

## 1. 自动化排程设定

* **排程名称**：`daily-discord-pre-agi-research`
* **排程周期**：每天 09:00（Asia/Taipei 时间，Cron：`0 9 * * *`）
* **执行引擎**：Antigravity (`antigravity/gemini-3.8-flash`)
* **排程服务**：Paseo Daemon（排程 ID：`e37c1e90`）
* **工作目录**：`C:\Users\USER\Documents\discord-pre-agi`

---

## 2. 每日研究任务目标

每次定时触发时，Antigravity Agent 会自动执行以下任务：
1. **状态与进展审计**：检查 `git log`、`develop` 分支变动、`docs/plans/v1.md` 的打勾验收项。
2. **拟人性与架构改进审查**：根据 `docs/research/social-persona-and-naturalness-review.md` 与实际运行反馈，检视打字节奏、语气口癖、繁简适应、记忆召回及前缀缓存（prefix cache）最佳化空间。
3. **dsh 插件概念构思**：结合 deepseek-harness 插件生态（Cordis 事件生命周期、中间件拦截、工具动态注入等），构思可拓展的新玩法或辅助插件。
4. **生成每日报告**：落盘写入 `docs/research/daily/YYYY-MM-DD.md`，给出明确的优先级行动清单。

---

## 3. 手动控制与排程管理

你可以透过 Antigravity CLI 或 Paseo MCP 随时管理该排程：

### CLI 命令
```bash
# 查看所有排程状态
paseo schedule ls

# 立即手动触发一次研究
paseo schedule run-once e37c1e90

# 检视执行历史纪录与日志
paseo schedule logs e37c1e90

# 暂停每日排程
paseo schedule pause e37c1e90

# 恢复每日排程
paseo schedule resume e37c1e90
```

### 在对话中操作
也可以直接向 Antigravity 发送指令：
* 「帮我手动触发一次每日研究」
* 「把每日研究排程改成每天晚上 11 点」
* 「暂停每日自动化研究」
