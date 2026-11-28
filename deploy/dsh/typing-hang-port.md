# 「正在输入不停」修正与背景画室：移植到 VM 现行 release

现行 release（`social64-trust-krita53-books-cef077-discord-bounded-1126`）有仓库里没有的补丁：gatewayScopes、history-view、drain、承诺、Krita 对帐。所以**不能用仓库重建、也不能整档覆盖**。`port_typing_fix.py` 只替换本次修正需要的接点，其余原样保留；任何一个接点对不上就整个停下，一个档案都不写；写到一半失败（例如第二个档案没有写入权限）会把已经写的还原、删掉新增的，不留半套。

## 这一版做了什么

- **画图搬到背景画室**（`studio.mjs`）：聊天回合不再自己画。她用 `draw_request` 把要画什么交给画室、回一句「好，我去畫」就结束，「正在输入」只亮那几秒；画室在另一个会话里画，画好由 Krita 工具直接回覆原本那则请求，画的时候不亮「正在输入」。同一时间只画一张（Krita 的批次锁不等待，第二批会被拒），第二个请求排队并回「前面還有一張，畫完就畫你的」。没画成、只画到预览、超时（预设 10 分钟，`DISCORD_DRAW_DEADLINE_MS`）、判断不该画，都回一句固定的短话。/stop 会停掉这个频道的画室。
- **聊天回合的期限与停止**（`turn-guard.mjs`）：同一则讯息自动续做时沿用第一轮的期限与停止讯号；/stop 停掉正在跑与排队中的讯息，之后不再送进模型、不再送答。期限仍是 280 秒（`DISCORD_TURN_DEADLINE_MS`）。
- **归帐**：工具送讯照「是哪则讯息送出的」算：挂了回复就是被回复的那则；没挂回复时，看工具**一开始执行**时记下的来源（哪个会话、为哪则讯息在跑；runtime 注册每个工具时都包了这一层），不看真的送出时轮到谁。没有来源也没挂回复的送讯不归给任何一则。桥接每次成功送出文字就记一笔，后面的贴图附件失败只记日志，不再补「卡住了」。
- **闸道租约**：期限到或 /stop 以后才收尾的那一轮不再去拿闸道租约，也只结束自己的租约，不会把已经轮到的下一则的租约清掉。
- **画室的隔离与期限**（第 3 轮审查）：画室会话不带记忆、人物卡与相关旧讯息（群聊会话本来会带拥有者私讯的记忆，标成「私下」；画室只需要「画什么、回覆哪则」，都在它收到的那则讯息里）。画室会话不管 `groupTools` 是 `chat` 还是 `full` 都能用 `krita_batch`（而且只能用它），聊天会话一律不能用。画室的每一段等待都有期限：期限到或被叫停（取消、/stop、请求被删）以后，画室会话 30 秒内不回来就换下一张；没画成时补的那句 30 秒送不出去就放弃（只记日志 `note=timeout`）。
- **请求被删**：桥接收到讯息被删（含批次删除）时，那张在画或排队的安静停掉，不补话（日志 `status=deleted`），之后再交代同一则也不画。Krita 交付与画室补的那句都改成 `fail_if_not_exists: true`：就算删除事件漏了，Discord 也会拒绝，图和那句话都不会掉成一般群讯（只记日志）。
- **2026-11-28 真频道验收后的修正**：
  - 画室不再把「第一个有文字的回合」当成结果：等画室会话整个闲下来（含换气的补救轮，`awaitSocialTurn` 的 `untilIdle`，不再用聊天回合的 `discord-turn-answered` 停掉补救轮），图还没交付就在期限内「接着画」（`runStudioJob`，最多 `STUDIO_MAX_ROUNDS`=4 轮，每轮都绑回原本那则请求）。画室提示词要她一步只想下一笔、想好就叫 `krita_batch`，并说明〔换气〕要她落盘、请人压缩的提示在画室不用照做。
  - 换气的提示**不从画室拿掉**：拿掉以后那一轮会变成完全空白，换气额度 0 时它处理空白回合会在会话事件里同步写入，宿主当成重入、整个 dsh 停掉（沙盒实测）。
  - 画室思考深度：试过 off 与 low（沙盒、真模型），这条路线（opencode-go 的 deepseek-v4.1-flash，手动宣告的模型）off 会被当成「不指定」，每步照样想约 1,500 字，跟 low 一样，所以不采用。
  - `draw_request` 加程式检查：这一则（叫她的那则）要明确要她画，不然不收，并告诉她之前的画图请求画室都已经回报过、不要补画（第 4 轮起改由守门员判断，见下面）。画室的结果（`studio.recent`）附在聊天会话的工具说明里（`studioRecentNote`），画室补的那句也记进群聊行为处理的纪录。
  - 送答前的核对（闸道 `historyCanDeliver`，要向 Discord 重新取原讯息、频道、成员与身分组，3 秒期限）没过就隔 1.5 秒再核对，最多 3 次（`socialCanDeliver`）；讯息已被删或改掉就不再试。日志 `送答核对没过 #ID：第 N 次`。
  - 聊天回放（`history-view.mjs`）：只回放 6 小时以内的讯息；每则标上时间（台北）与说话的人（都取自 Discord 核对过的原讯息），这次的那则标明「這則在叫你／沒人叫你…」，回放的旧讯息后面接上她当时的回答（只取文字）。
  - 「等我一下，我弄一下」不再按时间先说；只有交给画室以后、20 秒还没说话才说。群聊纪录不是今天的讯息连日期一起写；会话里离上一则群友讯息超过 3 小时，就附一句「之前的话题都过去了」（`socialRecencyNote`，没有闸道回放的设定才看得到）。
- **部署前审查（2026-11-28）的修正**：
  - 要不要画先改成逐个子句的关键字规则（`drawIntent`）；复验仍误判一半难句，第 4 轮整个拿掉（见下面）。
  - 改作品（`source_job`）必须是这个频道画室画过的：Krita 这则请求实际碰过的作品编号（闸道收下的才算）由画室记成这个频道的（`Studio.noteWork`／`workChannel`）；别的频道的、不知道的（含重启以前画的）都不收。
  - 聊天回放接上的「她当时的回答」只用这个频道实际送出的文字：桥接每次成功送出（她的回答、补的那句、画室的那句）都记到那则讯息名下（`historyView.sent`），不再从会话里抓（会话里有心跳另开一轮的定时输出、没送出去的草稿）。只记这次启动以后送出的。回放的 6 小时范围照 Discord 讯息编号里的时间，不照进会话的时间。
  - 送答前的核对过了，但核对的这段时间里被中止（期限、/stop）或讯息被撤销，就不送；进来之前就到期限的（例如 flush 卡到期限）照旧核对过就送。讯息被撤销时，送答核对与闸道租约都回「⏹️ 回合被取消（訊息已撤回）」（`TURN_REVOKED_NOTICE`），群聊行为处理不发出去、也不补「卡住了」（`status=revoked`）；核对没过又到期限时回期限的提示。
  - 画室这张结束了（画好、没画成、被删、被取消）就先发 `finishing` 事件再送补的那句；排队中被删的也发 `end`。群聊行为处理收到后不再替它说「等我一下」或「好，我去畫」（`status=draw-done`）。
- **第 4 轮（复验后）**：
  - **要不要画不再靠关键字规则**：`draw_request(start)` 被叫时另问一次模型当守门员（`studio.mjs` 的 `createDrawGate`）。守门员用聊天同一个模型、单轮、`reasoningEffort: low`、`temperature: 0`，不带人设、聊天回放与画室旧结果，也不看聊天模型写的 brief；只看叫她的那则原文和客观事实（可参考的图几张、回覆的是谁、这个频道画好的作品、这则之前十分钟的几句对话，`drawGateFacts`），群友的字关在带随机码的不可信标签里（`drawGatePrompt`）。回 `{"reason","intent","allow"}`；20 秒没回、出错、不是 JSON 都当成不画，照原因告诉她怎么回（`gateRefusal`）。程式只留不靠语意的事实检查：`source_job` 是这个频道画过的、`attachment_ids` 是这则（或它回覆的那则）带的图、brief 不能空、同一则已经在画室就不重画（`Studio.statusOf`）。日志 `畫室 判断 #ID allow=… intent=… secs=… in=… out=…`（不记内文与理由）。runtime 的 ingress 多记被回覆的那则（`quotedOf`）。正反例 93 句在 `deploy/social/draw-intent-cases.mjs`（`--legacy` 跑旧规则对照，真模型在 VM 沙盒的 `gate` 情境跑、`--report` 整理）。
  - **换气（huanqi 1.1.0）空白回合让整个 dsh 结束**：额度 0 时，换气在 turn/end 的会话事件里同步 inject／steer，宿主拒绝重入（`session append cannot reenter while another append is being published`）；熔断那段包在没有 catch 的 async IIFE 里，变成没人接的 rejection，dsh 以 `fatal load failure` 结束。移植脚本改 release 里的 `dsh-huanqi/lib/index.js`：这些写入都等这一轮结束（`whenIdle`）再做、失败只记日志，两个 IIFE 都接住错误；额度、判词、只 steer 一次都不变。
  - 回放记的「她实际送出的字」改成过滤后的（群聊的 `prepared.text`、控制回覆的 `shown`、画室那句用 Discord 回的 content），不再接回被拿掉的 `[sticker:…]` 与遮掉的机密。
  - 讯息被删或改掉：这一轮不管怎么结束（含期限到、被放弃）都只回「訊息已撤回」（`runSocialTurn` 的 `revoked`）；送答前的核对向 Discord 取原讯息回 404 Unknown Message（删除事件还没到）也记成撤销（`gateway-scopes.mjs` 的 `current`）。
  - 「等我一下」还在送（Discord 慢）时画室结束了：画室等它送完（最多 2 分钟）再送补的那句，顺序不会反（`finishing` 事件的 `wait`）。
- **所有回覆都用严格的讯息参照（Sol 部署前审查）**：送答前的核对过了，但在 POST 等待或重试的期间原讯息才被删，以前一般回覆用 `fail_if_not_exists: false`，回答会变成一则没回覆任何人的群讯。现在 `DiscordRest.createMessage`（她的回答、补的那句、控制回覆）、只有贴图时的附件（`createMessageWithFiles` 多一个回覆对象）、`discord_send_message` 的 `reply_to`、Krita 交付与画室补的那句全都是 `fail_if_not_exists: true`。Discord 拒绝、说那则不在（Unknown Message 10008，或表单错误里的 `REPLIES_UNKNOWN_MESSAGE`；官方文件没写确切错误码，两种都认，`socialMessageGone`）时，`DiscordRest.request` 交给 `rest.onGone`（`socialNoteGone`），当成对方撤回：群聊行为处理把那则呼叫记成撤回（`messageGone`：其余段落与附件不送、不补「卡住了」「好，我去畫」，日志 `status=revoked`），闸道记成撤销，画室那张安静停掉（`status=deleted`，不接着画）；画室补的那句送出时才被拒也记成 `deleted`。她自己选的回覆对象（`[回覆 #編號]`）不在了、叫她的那则还在，就改回覆叫她的那则（`socialReply`）。没挂回覆的一般讯息（她没写 `[回覆 #編號]`）本来就不是回覆，Discord 没办法替它挡。
- **其他**：「正在输入」被限流时不在 REST 里重送，熄灯后送到一半的也取消；附件下载失败的日志不写错误内文（可能带档名或网址）；只 @ 的判断不把「附档案、Discord 贴图、embed、挂了回复（引用抓不到也一样）、她一分钟内刚说过话」当成只 @。

## 会动到的档案

| 档案                                               | 改动                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dsh-plugin-discord/lib/index.js`                  | `enqueuePrompt` 由 `runSocialTurn` 包住（记进队列时间）；`runPrompt` 的两段等待换成 `awaitSocialTurn`；附件与 flush 有期限；拿租约前先查期限、只结束自己的租约；送讯每次成功就记帐；`commandStop` 停讯息与画室；「正在输入」改用 `socialTyping`；登入后建画室（`createSocialStudio`）；闸道附上的工具说明改成「交给画室」；`onMessageDelete` 通知画室（`noteSocialStudioDeleted`）；`DiscordRest` 的回覆一律严格参照，被拒时交给 `rest.onGone` 记成撤回 |
| `dsh-plugin-discord/lib/history-view.mjs`          | 聊天回放只放 6 小时内的讯息；每则标时间、说话的人与「叫你／没人叫你」；回放的旧讯息后面接上她当时的回答                                                                                                                                                                                                                                                                                                                                                 |
| `dsh-plugin-discord/lib/gateway-scopes.mjs`        | `historySource` 多回传说话的人的名字与讯息时间（取自 Discord 原讯息）                                                                                                                                                                                                                                                                                                                                                                                   |
| `dsh-plugin-discord/lib/natural-participation.mjs` | 把本分支对 develop（`a86bd2c`）的改动逐段套上（承诺那几行保留）                                                                                                                                                                                                                                                                                                                                                                                         |
| `dsh-social-runtime/bridge.mjs`                    | `extractSocialReply` 本体改呼叫 `foldSocialReply`（结尾 Krita 对帐保留）；汇出 turn-guard、studio、`createSocialStudio`、`stopSocialStudio`、`noteSocialStudioDeleted`                                                                                                                                                                                                                                                                                  |
| `dsh-social-runtime/index.mjs`                     | 注册 `draw_request`；画室会话只给 `krita_batch` 与 Krita 说明（chat／full 都放行），不带记忆与人物卡；聊天会话只给 `draw_request` 与「交给画室」的说明，用 `krita_batch` 会被挡；画室的模型请求另外计数（上限 1），画图时聊天不会被「目前有点忙」挡下；`apply` 里每个工具都包上送讯来源（`sendOrigin`）                                                                                                                                                 |
| `dsh-social-runtime/krita-tools.mjs`               | 画室画的图仍归原本的聊天会话（续改、`recent` 找得到）；记下预览张数；新增 `kritaDeliveries`；交付改成 `fail_if_not_exists: true`（请求被删就不贴）                                                                                                                                                                                                                                                                                                      |
| `dsh-social-runtime/discord-tools.mjs`             | `discord_send_message` 的 `reply_to` 改成 `fail_if_not_exists: true`（Sol 部署前审查）                                                                                                                                                                                                                                                                                                                                                                  |
| `dsh-huanqi/lib/index.js`                          | 换气的空白回合补救／熔断：会话事件里不再同步写会话，等这一轮结束再补提示；async 的错误都接住（第 4 轮）                                                                                                                                                                                                                                                                                                                                                 |
| `dsh-social-runtime/turn-guard.mjs`、`studio.mjs`  | 新增                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

画室会话**不设 `parentSession`、也不设 `origin`**：现行的 history-view 会把子会话的模型请求绑到父会话当轮的闸道租约，聊天回合一结束租约就没了，画室的请求会被拒；宿主也只接受 `origin: "subagent"`。画室会话靠 runtime 的 `studios` 登记辨认（建立以前就登记，runtime 每秒 mount 一次所有会话），prepare 会写进 bindings，所以心跳不会把它当成目标。每个聊天会话配一个画室会话，重启后会建新的。

## 步骤

1. **取副本（唯读）**：把 VM 上 `current` 指向的 release 里，`web/node_modules/dsh-plugin-discord`、`web/node_modules/dsh-social-runtime` 与 `web/node_modules/dsh-huanqi` 三个目录复制到本机一个空目录（例如 `rel/`），保持这三个名字。release 里的档案是唯读的，复制后先 `chmod -R u+w rel`。另外留一份没动过的副本给第 3 步对照。
2. **移植**（在仓库根目录，已提交的版本）：

   ```sh
   python deploy/dsh/port_typing_fix.py rel
   ```

   印出「已移植：index.js、natural-participation.mjs、history-view.mjs、gateway-scopes.mjs、bridge.mjs、index.mjs、krita-tools.mjs、discord-tools.mjs、dsh-huanqi/lib/index.js，新增 turn-guard.mjs、studio.mjs」才算成功；失败时印「移植中止，没有写入任何档案：…」。同一份副本跑第二次会被拒绝。

3. **本机验收**：

   ```sh
   node deploy/dsh/typing-hang-repro.mjs --release rel          # 移植后：52 项全部通过，代码 0
   node deploy/dsh/typing-hang-repro.mjs --release rel-原样副本  # 现行版本：51 项失败（宿主规则那项通过），代码 1
   DSH_RELEASE_COPY=rel-原样副本 python -m unittest discover -s deploy/dsh -p test_port_typing_fix.py
   ```

   `typing-hang-repro.mjs` 拿副本里真正的 `enqueuePrompt`／`runPrompt`／`saveIncoming`／`commandStop`、`DiscordRest`、`beginTypingFor` 与 `deliverMessage`，接上副本的群聊行为处理、turn-guard 与画室（真的 `Studio`／`connectStudio`／`draw_request`，画室会话是假的），用假 Discord、假模型、假时钟跑，逐则讯息 ID 判定「每一则 @ 都有正确、及时的回应」。情境包括：事故当天的时间线、画图期间同频道另一则 @、第二张排队、画室失败与超时、交给画室后没说话、A 送讯慢 B 先到、A 的工具讯息在 B 那轮才送达、A 被放弃后才收尾、附件卡住／失败／下载中 /stop、flush 卡住、自动续做（含不重新计时）、20 秒先说一句、到期限后排队的 @、答完停掉补救轮、送讯出错、贴图上传失败、/stop、只 @ 的四种误判、typing 被限流；第 3 轮审查加的：画室补的那句一直送不出去、请求讯息被删（用副本真的 `onMessageDelete` 与 `noteSocialStudioDeleted`）、A 的工具跨轮才发出且没挂回复。

   另外 5 项直接用副本真的 social runtime（`index.mjs`）跑。第一项照正式宿主（dsh-tools、dsh-system-prompt）的规则（`deploy/social/host-contract.mjs`，照宿主的错误讯息自己写的，不含宿主原始码）：release 里全部工具都要能登记（要有 `output { schema, render }`、output schema 只能用 type/oneOf/properties/required/additionalProperties/items/enum/const 加注解、`timeoutMs` 是正数），挂上聊天与画室会话时 `tools.restrict` 只能点名已登记的全域工具、`presentAs` 每个作用域一次、系统提示段落 order 是有限数且同名不重复、没有没登记的 `{{变数}}`，`draw_request` 的回传值要合 output schema。2026-11-28 第一次部署时宿主就是因为 `draw_request` 少了 output 拒绝整个 social runtime（只连得上闸道，不会回话），假宿主没抓到；其余检查也都改用这套规则。其余 4 项：群聊画室不带私讯记忆与拥有者人物卡（合成一段私讯记忆和人物卡上的私下条目，最坏的模型把看到的全部贴出来，上下文与输出都不能有那段字）；请求被删时真的 `krita_batch` 交付与画室补的那句（真的 `DiscordRest`，假 fetch 照 Discord 的 `fail_if_not_exists` 规则）都不掉成一般群讯；画室会话在 `groupTools=chat` 与 `full` 都能用 `krita_batch`、聊天会话都不能；`sendOrigin` 与 apply 里每个工具都包了送讯来源。

4. **真宿主、真模型跑一次画室与聊天（部署前，在 VM 上）**：`studio-sandbox.sh <11 个新档的目录> <沙盒目录> [秒数] [情境]`，`OPENCODE_GO_API_KEY` 只放环境变数（从 Doppler 的 dev 取，不写进档案或指令列），沙盒放进记忆体上限 1G 的 scope、可用记忆体不到 1.6G 就不跑。nobody 身分（多 kritaio 群组）、临时目录、另一个连接埠、没有 Discord token；正式的 Krita 闸道只收正式 dsh.service 的 node，所以另开一个临时闸道（`studio-sandbox-gate.py`：同一个 Krita，作品登记用沙盒自己的，跑完删掉沙盒画的作品目录）。换气额度从 0 开始（跟 2026-11-28 正式环境一样）。情境（`studio-sandbox-probe.mjs`）：`draw` 画一张小鯨魚并交出图；`draw-huanqi` 画到一半塞进换气熔断的提示（inject＋steer）再用 `discord-turn-answered` 中止一次，仍要画完；`after-fail` 让一张画不完（期限 15 秒）以后聊「你最喜歡什麼顏色」「你畫畫的時候還能聊天嗎」，不能交给画室；`double` 连 @ 两则都要有回答；`intent`（部署前审查）说「先不要畫」「請問你是怎麼畫畫的」不能交给画室，「幫我做一張頭像」要交给画室并画完（她先问要什么样的就再说清楚一次）。`gate`（第 4 轮）把 `draw-intent-cases.mjs` 的正反例逐句走正式的宿主与模型路线问守门员（不画图），结果写进 `out/gate.jsonl`，`SBX_GATE_RUNS` 设几轮（预设 2）。**一个沙盒只画一张**（画图情境分开跑），跑的时候另外盯 `/var/log/krita-guard.log`，`psi_mem` 到 10% 就先停沙盒。图存在沙盒的 `out/`。注意：沙盒没有闸道（gatewayScopes），聊天的回放（history-view）不会套用，那部分只在重现脚本里用副本真的 history-view 检查。
5. **换气空白回合（部署前，在 VM 上，不用金钥）**：`PROBE=huanqi-sandbox-probe.mjs HUANQI_CREDIT=0 bash host-sandbox.sh <11 个新档的目录> 70`。探针把一个群聊会话的模型串流换成「只有思考、没有正文」，换气额度 0：现行版本整个程序以 `fatal load failure: Error: session append cannot reenter…` 结束（exit=1）；新版本要印出 `ALIVE`，熔断提示与「落盘」那句在这一轮结束以后补上（`huanqiNotices`）。
6. **真宿主启动（部署前）**：在 VM 上用 nobody 身分、临时目录、另一个连接埠、没有 Discord token 与模型金钥，把新 release 的副本走真的 `trusted-entry.mjs` 启动一次（设定里所有正式路径与 socket 都换成临时目录），日志不能有 `entry did not activate` 或任何注册错误；再用一个只在沙盒载入的探针外挂，以真宿主的 `agents.create` 建聊天会话、送一则讯息（要走到组系统提示，只因没有金钥而停），并走真的 `connectStudio`／`studio.request` 让宿主建出画室会话。
7. **部署（要主人同意才做）**：在 VM 上把现行 release 目录复制成新的 release（名字带日期，例如 `…-studio-1128`），用第 2 步的 11 个档案替换／新增到新 release 里对应的位置（含 `web/node_modules/dsh-huanqi/lib/index.js`），`node --check` 这 11 个档案，再把 `current` 指到新 release、重启 `dsh.service`。原本的 release 目录不动、只换 `current` 指标，回滚就是把 `current` 指回原本的 release 再重启。
8. **设定（可不设）**：`DISCORD_TURN_DEADLINE_MS`（聊天回合，预设 280000，30 秒～30 分钟）；`DISCORD_DRAW_DEADLINE_MS`（一张图，预设 600000，1～60 分钟）；`SLOW_TURN_NOTICE_SECONDS`（预设 20）。
9. **真频道验收（要主人同意才做）**：在测试频道（之后改用主人指定的另一个测试频道，不再用原本的群聊频道）请她画一张，画的期间另一个人 @ 她聊天，再请第三个人也画一张。`journalctl -u dsh.service | grep -E '直接呼叫|畫室'`：每一则 @ 都要有「收到 #ID」与「结果 #ID status=…」；画图那则 status 是 `answered` 或 `draw-ack-sent`，画室有「畫室 收到／开始／结果 #ID status=delivered」；第二张是 `status=queued`。「正在输入」只在聊天回合那几秒亮。日志里不能出现任何讯息内文。另外请人画到一半把请求那则删掉：群里不能出现那张图或「卡住了」，日志有 `畫室 结果 #ID status=deleted`。正式环境是 `groupTools: full`（2026-11-28 唯读确认），要看画室真的叫得动 `krita_batch`。

## 版本相依

- 移植脚本以 `a86bd2c` 为现行 release 的 `natural-participation.mjs`、`bridge.mjs` 的来源（`--base` 可改）。现行 release 之后若又改过这些档案或接点，脚本会停下，要重新审阅接点再改脚本。
- 只在本机副本上写档；脚本不连 VM。
