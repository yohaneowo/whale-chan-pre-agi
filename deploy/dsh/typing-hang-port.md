# 「正在输入不停」修正：移植到 VM 现行 release

现行 release（`social64-trust-krita53-books-cef077-discord-bounded-1126`）有仓库里没有的补丁：gatewayScopes、history-view、drain、承诺、Krita 对帐。所以**不能用仓库重建、也不能整档覆盖**。`port_typing_fix.py` 只替换本次修正需要的接点，其余原样保留；任何一个接点对不上就整个停下，一个档案都不写。

会动到的只有 4 个档案：

| 档案                                               | 改动                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dsh-plugin-discord/lib/index.js`                  | `enqueuePrompt` 改由 `runSocialTurn` 包住（亮灯、期限、熄灯）；`runPrompt` 的两段等待（`whenIdle` 与 `settleSocialTurn`）一起换成 `awaitSocialTurn`；附件下载与 flush 有期限；取回答绑这次的 inbox 讯息；送讯每段凭 `naturalTicket` 记帐；工具送讯记帐；日志改用 `errorTrace`；参与层日志接 `logger.info` |
| `dsh-plugin-discord/lib/natural-participation.mjs` | 把本分支对 develop（`a86bd2c`）的改动逐段套上（承诺那几行保留）                                                                                                                                                                                                                                           |
| `dsh-social-runtime/bridge.mjs`                    | `extractSocialReply` 的本体改呼叫 `foldSocialReply`（结尾的 Krita 对帐保留），汇出 turn-guard                                                                                                                                                                                                             |
| `dsh-social-runtime/turn-guard.mjs`                | 新增                                                                                                                                                                                                                                                                                                      |

## 步骤

1. **取副本（唯读）**：把 VM 上 `current` 指向的 release 里，`web/node_modules/dsh-plugin-discord` 与 `web/node_modules/dsh-social-runtime` 两个目录复制到本机一个空目录（例如 `rel/`），保持这两个名字。release 里的档案是唯读的，复制后先 `chmod -R u+w rel`。另外留一份没动过的副本给第 3 步对照。
2. **移植**（在仓库根目录，已提交的版本）：

   ```sh
   python deploy/dsh/port_typing_fix.py rel
   ```

   印出「已移植：index.js、natural-participation.mjs、bridge.mjs，新增 turn-guard.mjs」才算成功；失败时印「移植中止，没有写入任何档案：…」。同一份副本跑第二次会被拒绝（防止重复套用）。

3. **本机验收**：

   ```sh
   node deploy/dsh/typing-hang-repro.mjs --release rel          # 移植后：全部通过，代码 0
   node deploy/dsh/typing-hang-repro.mjs --release rel-原样副本  # 现行版本：13 个情境失败，代码 1
   DSH_RELEASE_COPY=rel-原样副本 python -m unittest discover -s deploy/dsh -p test_port_typing_fix.py
   ```

   `typing-hang-repro.mjs` 拿副本里真正的 `enqueuePrompt`／`runPrompt`／`saveIncoming` 与 `deliverMessage`，接上副本的群聊行为处理与 turn-guard，用假 Discord、假模型、假时钟跑 13 个情境（事故当天的时间线、同频道 A 送讯慢 B 先送达、没正文、被取消、附件卡住、flush 卡住、自动续做、画图先说一句、20 秒先说一句、到期限后排队的 @、答完停掉补救轮、送讯出错、`/stop`），逐则讯息 ID 判定。

4. **部署（要主人同意才做）**：在 VM 上把现行 release 目录复制成新的 release（名字带日期，例如 `…-typing-guard-1128`），用第 2 步的 4 个档案替换新 release 里对应的档案，`node --check` 这 4 个档案，再把 `current` 指到新 release、重启 `dsh.service`。回滚就是把 `current` 指回原本的 release 再重启。
5. **设定（可不设）**：`DISCORD_TURN_DEADLINE_MS`（预设 280000，30 秒～30 分钟）；`SLOW_TURN_NOTICE_SECONDS`（预设 20，多久没东西送达就先说「等我一下」）。
6. **真频道验收（要主人同意才做）**：在测试频道 @ 她几次，包括只 @、连着 @ 两次、一则会画图的长请求。`journalctl -u dsh.service | grep 直接呼叫` 每一则都要有一行「收到 #ID」与一行「结果 #ID status=…」，status 是 `answered`／`bare-sent`／`notice-sent`／`fallback-sent` 之一；画图那则在动手前先说「好，我畫一下」，「正在输入」最晚 280 秒熄灯。日志里不能出现任何讯息内文。

## 版本相依

- 移植脚本以 `a86bd2c` 为现行 release 的 `natural-participation.mjs`、`bridge.mjs` 的来源（`--base` 可改）。现行 release 之后若又改过这两个档案或 `index.js` 的这些接点，脚本会停下，要重新审阅接点再改脚本。
- 只在本机副本上写档；脚本不连 VM。
