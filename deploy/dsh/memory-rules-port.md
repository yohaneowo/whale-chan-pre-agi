# 翻旧事的规则（小鲸鱼记忆第一阶段）：移植到 VM 现行 release

现行 release 是 `social64-trust-krita53-books-cef077-discord-bounded-1126-studio-1128c`（已经套过 [`port_typing_fix.py`](typing-hang-port.md)）。跟上一次一样，release 有仓库里没有的补丁，所以**不能用仓库重建、也不能整档覆盖**：`port_memory_rules.py` 只替换这次需要的接点，任何一个接点对不上就整个停下，一个档案都不写；写到一半失败会还原，不留半套。

背景：2026-11-28 14:46 #general 有人在聊眼前的事，她却接了四天前同一个频道的「送货」。研究（四份报告与整理后的方案）认定要先照 dsh-memory-keeper 的运行规则修好「什么时候翻旧事、可以翻哪里」，搜寻方法留到第 2 步再换。主人的决定：私讯可以想起群聊的事，群聊看不到私讯；公开频道之间可以互相提起，但只在相关或被问到时才提，并说出来源；私讯和不是所有人都看得到的频道不拿到别处讲；平常翻 6 个月内的事，有提到日期才查更早。

## 这一版做了什么

规则写在 `deploy/social/recall-rules.mjs`（新增，release 里是 `dsh-social-runtime/recall-rules.mjs`），runtime 的 `recallFor(source)` 每则讯息算一次，**自动想起、长期记忆、人物卡、翻纪录的工具（`chat_search`／`chat_open`）、闸道附上的「本頻道有原始來源可核對的記憶」全部用同一份**：

- **有线索才翻**：打招呼、闲聊、只 @ 不翻长期记忆、人物卡、旧讯息，工具也回「這句話沒有要翻舊事的線索…先回眼前這句」。线索是：明确在问以前的事（上次、之前、还记得、到了吗、说过……）、提到日期、问时间或说到之后的安排（几点、几号、什么时候、下个月）、提到人（@ 群友、说出在这里说过话的人的名字、问「谁」）、情绪词、点名某个频道，或者范围内的搜寻撞上少见的东西或话题。
- **先按范围过滤再搜寻**（`placeOf`）：群聊只翻这个频道；同服务器的**公开**频道（@everyone 看得到，`ChannelVisibility` 向 Discord 问频道权限，快取 10 分钟，还不知道的当成不公开）要有人点名那个频道或明确在问以前的事才翻，而且标出「#频道名（在那裡聽到的）」；私讯、不公开的频道、私人讨论串、别的服务器一律不拿到别处。私讯里可以想起公开频道的事（标来源），想不起不公开频道的。原文帐本的 `related`／`search`／`around` 多了 `filter`，连「多常见」的权重都只算范围内的讯息。
- **平常只翻 6 个月内**（183 天）；话里有日期（2026-05-01、5/1、5月1日、3月、去年、三个月前……）就翻那段时间，可以更早。这一则本身不算旧事。
- **找不到就说找不到**：明确在问却什么都没找到，系统提示附「…沒找到可靠的紀錄：就老實說沒找到或不記得了…不要編」；`chat_search` 没找到也这样回。找到了附「跟現在聊的有關才提…以前記的跟對方現在說的對不上，以現在說的為準」。明确在问时范围已经先缩小，搜寻门槛从 9 放低到 3（「上次那个包裹」只撞上「包裹」也要找得到）。
- **群聊看不到私讯**：群聊只找群聊范围的记忆（以前会把拥有者私讯的记忆一起找、标成「私下跟你說的，群裡別提」）；人物卡上「（私下）」的条目只在私讯带；人物卡的条目、记忆都看出处那则讯息在哪个频道、什么时候，出处不明的旧条目不带（留在档案里待确认）。
- **「事实」降成「事件」**：整理出的笔记一律记成事件；召回时只有标了 `pinned: true` 的记录才常驻（目前没有），以前标成 fact 的 559 笔都当事件、用到才找。站长确认过的长期事实仍在 `notes/facts.md` 的「事實」一节，每轮都带。旧档一个都不删、不改。
- **她的行为规则**（`MEMORY_MANNERS`，系统提示 `social-memory-manners`，群聊与私讯都带、画室不带）：记得不等于要说、打招呼闲聊不翻旧帐、照原话和日期讲并说出来源、翻不到就直说不要编、旧日记是当时写的故事不要装熟、以现在说的为准、记人记具体的那一刻、私讯与不公开频道的事不拿到别处。
- **背景整理修好**：思考深度从 `off`（这条模型路线不接受，2026-11-25 起每批都失败）改成 `low`，`maxTokens` 800 → 2000。每批先存进批次帐（`notes/batches/`，`deploy/social/notes-batches.mjs`），成功才收掉；失败记批次编号、群聊或私讯、失败在哪一步（`model`／`parse`／`save`／`write`）、原因类别（`http-500`、`bad-json`、`fs-EACCES`、`model-error`……，不记错误内文与聊天内容），日志 `背景整理失败 批次 … ：write 这一步，原因 fs-EACCES，第 1 次，稍后重试`。之后最多重试 3 次（5、10 分钟后），超过就移到 `failed/`（不删）。每一步做完就记下来（模型的结果、哪几个人的卡写好了、日记、事实），重试时做过的不再做，人物卡、日记、召回记录都不会重复；重试前再核对原文帐本，被删的讯息不整理、改过的用改过的字。写进召回记录的笔记带上出处那则讯息的频道。
- **fish-memory 的设定档**：`fish-memory-config.json` 不存在就用预设值，这个程序只提示一次（以前每一步都写一行 ENOENT）；档案损坏或没有权限才每次报警，报警只记错误码，不记路径以外的内文。
- 不变的：聊天回放（history-view）仍是同一个频道、6 小时内、标日期与说话的人，只 @ 仍走 `bareCall`，这些与新规则一致（回放是「眼前的对话」，不是翻旧事），没有改。

## 会动到的档案

| 档案（release 副本里）                                     | 改动                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dsh-social-runtime/index.mjs`                             | `recallFor`／`computeRecall`／`placeLabel`／`recordPlace`／`noteLabel`／`recallScope`／`recallNote`；`relatedLines` 改用 `recallFor`；`social-memory` 那段照规则组；`groupContext` 多收 `recall`、人物卡照范围、拿掉「私下」那两句；私讯会话的说明改成「想得起公开频道的事、私讯只留在私讯」；新增 `social-memory-manners`；`admit` 时载入频道可见性；`consolidate` 整段换成批次帐版本（`runNoteBatch`／`retryNotes`／`noteLog`／`reviseNoteLines`／`consolidationText`） |
| `dsh-social-runtime/memory.mjs`                            | `recallAcross` 多一个 `place`（先按范围过滤）；常驻只看 `pinned`                                                                                                                                                                                                                                                                                                                                                                                                          |
| `dsh-social-runtime/notes.mjs`                             | `context(people, { admit, shown })`：人物卡只带看得到的条目                                                                                                                                                                                                                                                                                                                                                                                                               |
| `dsh-social-runtime/archive.mjs`                           | `related`／`search`／`around` 多一个 `filter`                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `dsh-social-runtime/chat-tools.mjs`                        | 没有线索就不翻；只翻范围内；没找到就说没找到                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `dsh-plugin-discord/lib/gateway-scopes.mjs`                | `historyMemories`：没有线索就不附；只附 6 个月内（或话里那段日期）的                                                                                                                                                                                                                                                                                                                                                                                                      |
| `dsh-plugin-discord/lib/natural-participation.mjs`         | 整理失败的警告改成「这批留着稍后重试」                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `dsh-fish-memory/lib/index.js`                             | `loadCfg`：设定档不存在只提示一次                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `dsh-social-runtime/recall-rules.mjs`、`notes-batches.mjs` | 新增（来自仓库 `deploy/social/`）                                                                                                                                                                                                                                                                                                                                                                                                                                         |

## 步骤

1. **取副本（唯读）**：VM 上 `current` 指向的 release 里，`web/node_modules/` 底下的 `dsh-social-runtime`、`dsh-plugin-discord`、`dsh-fish-memory` 复制到本机空目录（例如 `rel/`），`chmod -R u+w rel`；另留一份没动过的给第 3 步对照。
2. **移植**（仓库根目录，已提交的版本）：`python deploy/dsh/port_memory_rules.py rel`。印出「已移植：index.mjs、memory.mjs、notes.mjs、archive.mjs、chat-tools.mjs、gateway-scopes.mjs、natural-participation.mjs、index.js，新增 recall-rules.mjs、notes-batches.mjs」才算成功；同一份副本跑第二次会被拒绝。
3. **本机验收**：
   ```sh
   node deploy/dsh/memory-rules-repro.mjs --release rel           # 移植后：12 项全部通过，代码 0
   node deploy/dsh/memory-rules-repro.mjs --release rel-原样副本  # 现行版本：12 项全部失败，代码 1
   node deploy/dsh/typing-hang-repro.mjs --release rel             # 上一轮的 52 项仍全部通过
   DSH_RELEASE_COPY=rel-原样副本 python -m unittest discover -s deploy/dsh -p test_port_memory_rules.py
   ```
   `memory-rules-repro.mjs` 用副本里真的 social runtime、fish-memory 召回引擎与 Discord 外挂的 `GatewayScopes`，假 Discord（频道权限、讯息）、几百则平常的群聊当背景，看**实际送进模型的内容**：系统提示每一段展开、闸道附上的记忆，加上最坏的模型（每轮都拿暗号叫 `chat_search`、`chat_open`）拿到的工具输出。情境：#general 闲聊与只 @ 不带四天前的送货（三条路：remember 的原话、整理成「事实」的笔记、人物卡）；两个公开频道、一个不公开频道、一则私讯各放一个暗号（A 频道闲聊都不出现、明确问旧事不出现私讯与不公开频道的、点名 B 频道可以提且标来源、私讯想得起公开频道的且不出现不公开频道的）；明确问「上次那個包裹到了嗎」带同频道原文与日期、问不存在的事要她说没找到；8 个多月前的事没提日期不翻、提了月份才翻；行为规则在群聊与私讯都有；背景整理的模型失败、格式错、写入失败（人物卡写好、日记写不进去）各自留下步骤与类别、不含机密与内文、重试后人物卡／日记／召回记录各一条、不再是「事实」、参数是 low 与 2000；fish-memory 设定档不存在只提示一次、损坏才报警。
4. **回想题库**（`deploy/eval/recall`，36 题，不呼叫模型）：`node run.mjs --social rel/dsh-social-runtime --fish <fish-memory 1.3.7> --opencc <opencc 1.4.2>`（Windows 要让 `C:\Windows\System32\tar.exe` 排在 PATH 前面，Git 的 tar 不认 `C:\` 路径）。题库镜像 index.mjs 的接线，移植后的 release 走新的接线（`composeRecall`，`guardMirror` 换一组接线词），工具题没有问题句时当成站长在问以前的事。题目是照旧政策（所有群聊共用一份记忆）出的，所以有几题照新规则本来就会不同，见下面「题库对照」。
5. **真模型跑一次背景整理（部署前，在 VM 上）**：`bash notes-sandbox.sh <old|new> <新版档案目录> <沙盒目录> [秒数]`，`OPENCODE_GO_API_KEY` 只放环境变数。不画图、不开 Krita 闸道、没有 Discord token；nobody 身分、临时目录、另一个连接埠、记忆体上限 1G 的 scope，可用记忆体不到 1.6G 就不跑；一次只跑一个（old 跑完再跑 new），跑的时候盯 `/var/log/krita-guard.log`。探针（`notes-sandbox-probe.mjs`）先照旧参数与新参数各直接问一次模型（记思考与正文多长、怎么结束），再跑一次真的 `consolidate`，印出人物卡与日记写出来的条目、批次帐剩几批、召回记录的种类。pve2 的网路不稳，整段用 `nohup setsid` 在 VM 上独立跑，看 `old.out`／`new.out`。
6. **部署（要主人同意才做）**：同上一版——把现行 release 复制成新的 release（名字带日期），把第 2 步的 10 个档案替换／新增到 `web/node_modules/` 底下对应的位置，`node --check` 这 10 个档案，`freeze.py select` 到新 release、重启 `dsh.service`。回滚就是 select 回原本的 release 再重启。旧的记忆档、人物卡、日记都不用动（新规则只改「怎么读」，旧的 fact 记录照样留着，读的时候当事件）。
7. **真频道验收（要主人同意才做）**：在 #大肥魚的魚缸 用假内容试：闲聊与只 @（不接旧事）、明确问本频道旧事（带日期）、问不存在的事（说没找到）、点名另一个公开频道（标来源）；`journalctl -u dsh.service | grep 背景整理` 不再有失败，`notes/people/`、`notes/journal/` 有新的修改时间。

## 题库对照

2026-11-29 本机跑（不呼叫模型）：仓库 `deploy/social` 35/36、现行 release 27/36、移植后 25/36。移植后多通过 c9-3（来源被删的人物卡条目不再带出）；多失败的三题照新规则本来就会不同：c6-3（另一个服务器的旧话要想得起来——新规则不拿到别的服务器）、c5-3（私讯来源要标「私訊（私下）」——私讯的东西只留在私讯，私讯里不再加这个标记，两则说法都有带出）、c9-5（回滚题的前提「写坏的那条一定会被召回」靠的是旧的常驻名额；写坏的那条现在是事件，跟正确的那条重复被去掉，回滚本身的检查都通过）。其余失败的题（c2-3、c3-2、c3-3、c4-1、c4-2、c8-2、c8-3、c9-2）现行版本也失败，是第 2 步（换搜寻方法、出处维护）要处理的。

## 版本相依

- 移植脚本要求 release 已经套过 `port_typing_fix.py`（`drawRequestTool`、`this.studios.has(agent.id)` 要在），而且 `consolidate` 还是 `reasoningEffort: 'off'`、`maxTokens: 800` 那一版；对不上就停下。
- 只在本机副本上写档；脚本不连 VM。
