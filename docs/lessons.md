# 开发与部署经验教训

这份是从团队工作日志的教训表搬进来的（2026-11-22），只收跟本仓库有关的技术笔记。每条保留原编号、日期、记录者与证据；证据路径相对本仓库，标「内部纪录」的不在本仓库。

## Discord 接入与费用上限

- **L4**（2026-10-27，codex）Discord 固定回复的验收只依赖 Discord 设定，要和完整的模型凭证检查拆开。遇到 HTTP 401 先查官方 API 状态与 `.env` 的存档时间，确认 token 已存档再重试，不印出凭证。证据：`src/config.ts`、`src/index.ts`、`docs/plans/v1.md`
- **L5**（2026-10-27，codex）群聊的费用上限要在 HTTP 呼叫前用 SQLite immediate 交易预留，避免多个频道同时花掉剩余预算；逾时、用量不明与程序中断的预留要保留，不能当成零费用。保守估价和供应商帐单分开记。证据：`src/ledger.ts`、`tests/ledger.test.ts`、`tests/chat-http.test.ts`

## dsh 部署与隔离

- **L8**（2026-10-27，codex）要求 dsh 和个人电脑隔离时，部署模型是服务器 VM 承载代理与工具，Discord／浏览器只当客户端。建议资源前先查既有基础设施纪录与主机实际余量；官方桌面本机 Host 与远端 Web 的能力要分别验证。证据：`docs/plans/v1.md`（隔离需求补充）
- **L12**（2026-10-27，codex）独立的 dsh VM 要分开 PVE 控制面、维护使用者与无 sudo 的代理使用者；不为单一新 VM 改动全局 PVE 防火墙而影响其他客体，先在新客体用 UFW 与 systemd cgroup 限制并实测。部署前固定版本并校验映像；启动 URL 带凭证时要遮蔽并存进私人 `.env`，不直接印出完整服务日志。证据：`deploy/dsh/dsh.service`、`deploy/dsh/run_dsh.py`、`deploy/pve/provision_vm.py`
- **L14**（2026-10-27，codex）验证 systemd `IPAddressDeny` 时，TCP connect 可能只是超时；改用 UDP `sendto`，只有 EPERM／EACCES 才判定为权限拒绝，并在真实服务的 `ExecStartPre` 里验证。另用一次公开 HTTPS 成功确认不是整个网络中断。证据：`deploy/dsh/verify_isolation.py`

## PVE 资源评估与 VM 维护

- **L9**（2026-10-27，codex）评估 PVE 新 VM 时，不能把 guest 的 MemAvailable 当成宿主可分配量；balloon=0 的固定配额不会因为停掉容器就自动释放。先用只读指标列出各资源的配置、目前用量与峰值；环境标签可能不准，要合并资源名称与相依性判读，不能只按标签停服务。缩配与停用要另外取得授权。证据：`deploy/pve/README.md`、`deploy/pve/audit_memory.py`
- **L10**（2026-10-27，codex）回答 VM 真实用量要同时交代配置额、guest MemTotal 与 MemAvailable；不重复计算快取，也不把 MemTotal − MemAvailable 称为纯程序 RSS。缩配后的余量只是当下样本的推估，要保留建置峰值与核心余量。证据：guest `/proc/meminfo` 只读核对；`deploy/pve/README.md`
- **L11**（2026-10-27，codex）维护既有 VM 前，先查即时的 guest agent、部署队列、容器与 HTTP 基准（旧纪录可能把 agent 状态记错）。拍快照后正常关机调配额，开机后比对原有 running／oneshot 状态、健康检查、OOM 与受保护的转址；短期通过不等于建置峰值已验证，过时的配置纪录要回写更正。证据：`deploy/pve/README.md`；内部基础设施维护纪录（2026-10-27）

## 自然参与与社交模块

- **L17**（2026-10-27，codex）自然群友不能停在「必须 @ 才回」：按实际成功回复的对象开短暂的续聊窗口，冷却只限制主动插话，直接呼叫与正常续聊不受阻。较早的「先加提及过滤」只适合初期连线验收，不是最终产品规则。证据：`deploy/dsh/natural-participation.mjs`
- **L18**（2026-10-28，codex）dsh 准备动态上下文时，当轮讯息还没进历史，召回查询要用桥接收到的当前原讯息；记忆的作者要从实际执行回合的事件验证，不能借用排队讯息或 Web 输入前的 Discord 身份快取。这个问题是用「真 dsh 配假模型」的跨会话记忆验收抓到的。证据：`deploy/social/index.mjs`
- **L19**（2026-10-28，codex）验证工具预设迁移时，要覆盖真实供应方的工具宣告约束。dsh 的 addition-only 会删掉旧工具并把新工具延后宣告；旧工具全删时，只把当前可用、且不要求延后的工具立即宣告，并去掉重复的添加块。OpenAI 相容的假模型通过，不能证明 DeepSeek 协议相容，要用实际宿主投影与真实模型的独立会话补验。证据：`deploy/dsh/patch_tool_projection.py`
- **L135**（2026-11-13，codex）主动搭话的 dry-run 如果在 followup 之前就 return，只能验证触发节奏，不能评价台词；要另做一份不真发送、不写工具的生成副本。计时器不能复用 engaged 豁免，bot 自己的发言不应成为下一次开口的依据。证据：<https://github.com/Yuuyuko-uu/dsh-speak-up/blob/main/lib/host.js#L804>；`deploy/dsh/natural-participation.mjs:146`
- **L805**（2026-11-14，codex）插件标成 observe 不保证零行为：评估的插件在 `engine.open` 时仍会挂上既有适配，另一个入口也会先建目录。先静态审入口与状态载入，再选纯离线的侦测方式；日志输出的证据要私有隔离。证据：内部评估纪录（2026-11-14）
