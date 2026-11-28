# 「正在输入不停」修正与背景画室：移植到 VM 现行 release

现行 release（`social64-trust-krita53-books-cef077-discord-bounded-1126`）有仓库里没有的补丁：gatewayScopes、history-view、drain、承诺、Krita 对帐。所以**不能用仓库重建、也不能整档覆盖**。`port_typing_fix.py` 只替换本次修正需要的接点，其余原样保留；任何一个接点对不上就整个停下，一个档案都不写；写到一半失败（例如第二个档案没有写入权限）会把已经写的还原、删掉新增的，不留半套。

## 这一版做了什么

- **画图搬到背景画室**（`studio.mjs`）：聊天回合不再自己画。她用 `draw_request` 把要画什么交给画室、回一句「好，我去畫」就结束，「正在输入」只亮那几秒；画室在另一个会话里画，画好由 Krita 工具直接回覆原本那则请求，画的时候不亮「正在输入」。同一时间只画一张（Krita 的批次锁不等待，第二批会被拒），第二个请求排队并回「前面還有一張，畫完就畫你的」。没画成、只画到预览、超时（预设 10 分钟，`DISCORD_DRAW_DEADLINE_MS`）、判断不该画，都回一句固定的短话。/stop 会停掉这个频道的画室。
- **聊天回合的期限与停止**（`turn-guard.mjs`）：同一则讯息自动续做时沿用第一轮的期限与停止讯号；/stop 停掉正在跑与排队中的讯息，之后不再送进模型、不再送答。期限仍是 280 秒（`DISCORD_TURN_DEADLINE_MS`）。
- **归帐**：工具送讯照「是哪则讯息送出的」算：挂了回复就是被回复的那则；没挂回复时，看工具**一开始执行**时记下的来源（哪个会话、为哪则讯息在跑；runtime 注册每个工具时都包了这一层），不看真的送出时轮到谁。没有来源也没挂回复的送讯不归给任何一则。桥接每次成功送出文字就记一笔，后面的贴图附件失败只记日志，不再补「卡住了」。
- **闸道租约**：期限到或 /stop 以后才收尾的那一轮不再去拿闸道租约，也只结束自己的租约，不会把已经轮到的下一则的租约清掉。
- **画室的隔离与期限**（第 3 轮审查）：画室会话不带记忆、人物卡与相关旧讯息（群聊会话本来会带拥有者私讯的记忆，标成「私下」；画室只需要「画什么、回覆哪则」，都在它收到的那则讯息里）。画室会话不管 `groupTools` 是 `chat` 还是 `full` 都能用 `krita_batch`（而且只能用它），聊天会话一律不能用。画室的每一段等待都有期限：期限到或被叫停（取消、/stop、请求被删）以后，画室会话 30 秒内不回来就换下一张；没画成时补的那句 30 秒送不出去就放弃（只记日志 `note=timeout`）。
- **请求被删**：桥接收到讯息被删（含批次删除）时，那张在画或排队的安静停掉，不补话（日志 `status=deleted`），之后再交代同一则也不画。Krita 交付与画室补的那句都改成 `fail_if_not_exists: true`：就算删除事件漏了，Discord 也会拒绝，图和那句话都不会掉成一般群讯（只记日志）。
- **其他**：「正在输入」被限流时不在 REST 里重送，熄灯后送到一半的也取消；附件下载失败的日志不写错误内文（可能带档名或网址）；只 @ 的判断不把「附档案、Discord 贴图、embed、挂了回复（引用抓不到也一样）、她一分钟内刚说过话」当成只 @。

## 会动到的档案

| 档案                                               | 改动                                                                                                                                                                                                                                                                                                                                                                               |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dsh-plugin-discord/lib/index.js`                  | `enqueuePrompt` 由 `runSocialTurn` 包住（记进队列时间）；`runPrompt` 的两段等待换成 `awaitSocialTurn`；附件与 flush 有期限；拿租约前先查期限、只结束自己的租约；送讯每次成功就记帐；`commandStop` 停讯息与画室；「正在输入」改用 `socialTyping`；登入后建画室（`createSocialStudio`）；闸道附上的工具说明改成「交给画室」；`onMessageDelete` 通知画室（`noteSocialStudioDeleted`） |
| `dsh-plugin-discord/lib/natural-participation.mjs` | 把本分支对 develop（`a86bd2c`）的改动逐段套上（承诺那几行保留）                                                                                                                                                                                                                                                                                                                    |
| `dsh-social-runtime/bridge.mjs`                    | `extractSocialReply` 本体改呼叫 `foldSocialReply`（结尾 Krita 对帐保留）；汇出 turn-guard、studio、`createSocialStudio`、`stopSocialStudio`、`noteSocialStudioDeleted`                                                                                                                                                                                                             |
| `dsh-social-runtime/index.mjs`                     | 注册 `draw_request`；画室会话只给 `krita_batch` 与 Krita 说明（chat／full 都放行），不带记忆与人物卡；聊天会话只给 `draw_request` 与「交给画室」的说明，用 `krita_batch` 会被挡；画室的模型请求另外计数（上限 1），画图时聊天不会被「目前有点忙」挡下；`apply` 里每个工具都包上送讯来源（`sendOrigin`）                                                                            |
| `dsh-social-runtime/krita-tools.mjs`               | 画室画的图仍归原本的聊天会话（续改、`recent` 找得到）；记下预览张数；新增 `kritaDeliveries`；交付改成 `fail_if_not_exists: true`（请求被删就不贴）                                                                                                                                                                                                                                 |
| `dsh-social-runtime/turn-guard.mjs`、`studio.mjs`  | 新增                                                                                                                                                                                                                                                                                                                                                                               |

画室会话**不设 `parentSession`、也不设 `origin`**：现行的 history-view 会把子会话的模型请求绑到父会话当轮的闸道租约，聊天回合一结束租约就没了，画室的请求会被拒；宿主也只接受 `origin: "subagent"`。画室会话靠 runtime 的 `studios` 登记辨认（建立以前就登记，runtime 每秒 mount 一次所有会话），prepare 会写进 bindings，所以心跳不会把它当成目标。每个聊天会话配一个画室会话，重启后会建新的。

## 步骤

1. **取副本（唯读）**：把 VM 上 `current` 指向的 release 里，`web/node_modules/dsh-plugin-discord` 与 `web/node_modules/dsh-social-runtime` 两个目录复制到本机一个空目录（例如 `rel/`），保持这两个名字。release 里的档案是唯读的，复制后先 `chmod -R u+w rel`。另外留一份没动过的副本给第 3 步对照。
2. **移植**（在仓库根目录，已提交的版本）：

   ```sh
   python deploy/dsh/port_typing_fix.py rel
   ```

   印出「已移植：index.js、natural-participation.mjs、bridge.mjs、index.mjs、krita-tools.mjs，新增 turn-guard.mjs、studio.mjs」才算成功；失败时印「移植中止，没有写入任何档案：…」。同一份副本跑第二次会被拒绝。

3. **本机验收**：

   ```sh
   node deploy/dsh/typing-hang-repro.mjs --release rel          # 移植后：33 项全部通过，代码 0
   node deploy/dsh/typing-hang-repro.mjs --release rel-原样副本  # 现行版本：32 项失败（宿主规则那项通过），代码 1
   DSH_RELEASE_COPY=rel-原样副本 python -m unittest discover -s deploy/dsh -p test_port_typing_fix.py
   ```

   `typing-hang-repro.mjs` 拿副本里真正的 `enqueuePrompt`／`runPrompt`／`saveIncoming`／`commandStop`、`DiscordRest`、`beginTypingFor` 与 `deliverMessage`，接上副本的群聊行为处理、turn-guard 与画室（真的 `Studio`／`connectStudio`／`draw_request`，画室会话是假的），用假 Discord、假模型、假时钟跑，逐则讯息 ID 判定「每一则 @ 都有正确、及时的回应」。情境包括：事故当天的时间线、画图期间同频道另一则 @、第二张排队、画室失败与超时、交给画室后没说话、A 送讯慢 B 先到、A 的工具讯息在 B 那轮才送达、A 被放弃后才收尾、附件卡住／失败／下载中 /stop、flush 卡住、自动续做（含不重新计时）、20 秒先说一句、到期限后排队的 @、答完停掉补救轮、送讯出错、贴图上传失败、/stop、只 @ 的四种误判、typing 被限流；第 3 轮审查加的：画室补的那句一直送不出去、请求讯息被删（用副本真的 `onMessageDelete` 与 `noteSocialStudioDeleted`）、A 的工具跨轮才发出且没挂回复。

   另外 5 项直接用副本真的 social runtime（`index.mjs`）跑。第一项照正式宿主（dsh-tools、dsh-system-prompt）的规则（`deploy/social/host-contract.mjs`，照宿主的错误讯息自己写的，不含宿主原始码）：release 里全部工具都要能登记（要有 `output { schema, render }`、output schema 只能用 type/oneOf/properties/required/additionalProperties/items/enum/const 加注解、`timeoutMs` 是正数），挂上聊天与画室会话时 `tools.restrict` 只能点名已登记的全域工具、`presentAs` 每个作用域一次、系统提示段落 order 是有限数且同名不重复、没有没登记的 `{{变数}}`，`draw_request` 的回传值要合 output schema。2026-11-28 第一次部署时宿主就是因为 `draw_request` 少了 output 拒绝整个 social runtime（只连得上闸道，不会回话），假宿主没抓到；其余检查也都改用这套规则。其余 4 项：群聊画室不带私讯记忆与拥有者人物卡（合成一段私讯记忆和人物卡上的私下条目，最坏的模型把看到的全部贴出来，上下文与输出都不能有那段字）；请求被删时真的 `krita_batch` 交付与画室补的那句（真的 `DiscordRest`，假 fetch 照 Discord 的 `fail_if_not_exists` 规则）都不掉成一般群讯；画室会话在 `groupTools=chat` 与 `full` 都能用 `krita_batch`、聊天会话都不能；`sendOrigin` 与 apply 里每个工具都包了送讯来源。

4. **真宿主启动（部署前）**：在 VM 上用 nobody 身分、临时目录、另一个连接埠、没有 Discord token 与模型金钥，把新 release 的副本走真的 `trusted-entry.mjs` 启动一次（设定里所有正式路径与 socket 都换成临时目录），日志不能有 `entry did not activate` 或任何注册错误；再用一个只在沙盒载入的探针外挂，以真宿主的 `agents.create` 建聊天会话、送一则讯息（要走到组系统提示，只因没有金钥而停），并走真的 `connectStudio`／`studio.request` 让宿主建出画室会话。
5. **部署（要主人同意才做）**：在 VM 上把现行 release 目录复制成新的 release（名字带日期，例如 `…-studio-1128`），用第 2 步的 7 个档案替换／新增到新 release 里对应的位置，`node --check` 这 7 个档案，再把 `current` 指到新 release、重启 `dsh.service`。回滚就是把 `current` 指回原本的 release 再重启。
6. **设定（可不设）**：`DISCORD_TURN_DEADLINE_MS`（聊天回合，预设 280000，30 秒～30 分钟）；`DISCORD_DRAW_DEADLINE_MS`（一张图，预设 600000，1～60 分钟）；`SLOW_TURN_NOTICE_SECONDS`（预设 20）。
7. **真频道验收（要主人同意才做）**：在测试频道请她画一张，画的期间另一个人 @ 她聊天，再请第三个人也画一张。`journalctl -u dsh.service | grep -E '直接呼叫|畫室'`：每一则 @ 都要有「收到 #ID」与「结果 #ID status=…」；画图那则 status 是 `answered` 或 `draw-ack-sent`，画室有「畫室 收到／开始／结果 #ID status=delivered」；第二张是 `status=queued`。「正在输入」只在聊天回合那几秒亮。日志里不能出现任何讯息内文。另外请人画到一半把请求那则删掉：群里不能出现那张图或「卡住了」，日志有 `畫室 结果 #ID status=deleted`。正式环境是 `groupTools: full`（2026-11-28 唯读确认），要看画室真的叫得动 `krita_batch`。

## 版本相依

- 移植脚本以 `a86bd2c` 为现行 release 的 `natural-participation.mjs`、`bridge.mjs` 的来源（`--base` 可改）。现行 release 之后若又改过这些档案或接点，脚本会停下，要重新审阅接点再改脚本。
- 只在本机副本上写档；脚本不连 VM。
