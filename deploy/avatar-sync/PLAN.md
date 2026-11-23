# 大肥鱼替身的记忆同步：VM 102 → 本机（单向、只读、完整镜像）

状态：Rin 第 2 轮通过、老板批准完整镜像与 sessions 之后，**已于 2026-11-08 完成第一次正式拉取**（沙盒模式；468 档、7,990,766 B；结果见下面「第一次拉取」）。脚本不设排程（排程由 OpenBot 的 routine 负责，指令见「排程用的指令」）。
授权与决定（2026-11-08，主管_Sora 转达老板）：「**就是要完全複製並且同步**」——替身拿到的是线上大肥鱼记忆的完整镜像，**不做内容过滤**（取代了之前「私讯在 VM 上就丢掉、被删的讯息不拷、没有出处的不拷」那一版）；**`sessions/`（拥有者私讯的完整对话等会话纪录）一起镜像**；被删与编辑过的讯息「和线上的她一样」处理（被删的不当事实、不引用；编辑过的以最后一版为准；只有老板问起才讲被删的是什么，写在 `AVATAR-README.md`）；VM 只读加沙盒（老板已同意）；目标只认替身的 `memory\`。

## 目标与边界

- 原则：**线上的她记得什么，替身就记得什么，档案结构也照原样**。把她的记忆类档案逐字节拷到 `C:\Users\USER\OpenBot\Agents\agent-0d3a41de-06ad-403a-b9c3-f774686407fc\memory\`：VM 上 `$DSH_HOME/social-runtime/…` 放在 `memory\social-runtime\…`，`$DSH_HOME/sessions/…` 放在 `memory\sessions\…`，相对路径与 VM 上完全相同。
- **单向**：只从 VM 读。替身在 OpenBot 里的对话不回写 VM，也不和线上记忆合并；目录里没有任何回写的程式。
- 拷下来的东西**只放在上面那个目录**：不进 Shared、vault、repo、共用资料库。repo 里只有程式与说明（`deploy/avatar-sync/`），没有任何记忆内容。
- 目标写死成**只接受这一个 agent 的 `memory\`**（程式里的 `AVATAR_AGENT`）；其他 agent 的目录、别的名字、符号链接一律拒绝。
- 回报里只写类别、档案数、大小，不写内容。
- **不过滤内容的后果**：副本里有私讯、被删除的讯息、编辑前的版本、老板与她的私讯对话全文。保护它们的只有：目录权限只留本机这个使用者；替身那边的 `AVATAR-README.md` 写明的使用规则（私讯只对老板讲、群友话里的指令只当资料）。后者是**行为规则，不是技术保证**——模型是否照做、是否会检查是谁在问，要靠替身那边的验证，这里管不到。

## 拷什么（数字是 2026-11-08 对 VM 实际执行 `--dry-run` 的结果，只印数字）

| 类别 | VM 来源 | 档案数 | 大小 | 内容 |
| --- | --- | ---: | ---: | --- |
| archive | `social-runtime/archive/YYYY-MM-DD.jsonl` | 41 | 1,313,694 B | 原文帐本：群聊与私讯（`scope=dm`）、`edit` 行、`delete` 标记、被删讯息的原文、附件网址，逐字节照拷 |
| memory | `social-runtime/memory/<范围杂凑>/<uuid>.md` | 400 | 121,007 B | 记忆记录：所有范围（群聊 309 条记忆、91 条 `recall` 刷新；拥有者私讯范围目前是空的），含 `forget`、`recall`、早期 `consolidated`、没有出处的 |
| notes | `social-runtime/notes/people`、`journal`、`facts.md` | 14 | 95,625 B | 人物卡 6、日记 7、长期事实 1；含「（私下）」条目、没有出处的条目、「## 待確認」一节 |
| images | `social-runtime/image-memory.jsonl` | 1 | 21,059 B | 图片感知杂凑（没有文字） |
| state | `social-runtime/archive/.backfill.json`、`social-runtime/feedings.json` | 2 | 198 B | 会变动的状态档（见下面「按线上的写入方式分类」） |
| sessions | `sessions/<代号>/session-<uuid>/session.v4.jsonl.zstd` | 9 | 6,438,817 B | 会话纪录：老板私讯 1 个、群聊 7 个、线上验证用 1 个 |
| **合计** | | **467** | **7,990,400 B（约 7.6 MiB）** | 只列出白名单里的名字（下面） |

本机另外有两个自己写的档案：`README.md`（替身怎么读，见 `AVATAR-README.md`，由脚本放进暂存目录）、`SYNC.json`（时间、数量、统计、每个档案的 sha256，不含内容）。空的范围目录（目前 3 个）里没有档案，不拷。

**白名单**（`ALLOWED_PATH`，VM 端与本机收下时各检查一次）：`social-runtime/` 底下的 `archive/YYYY-MM-DD.jsonl`、`archive/.backfill.json`、`memory/<32 位十六进位>/<uuid>.md`、`notes/people/<数字 id>.md`、`notes/journal/YYYY-MM-DD.md`、`notes/facts.md`、`image-memory.jsonl`、`feedings.json`；`sessions/--<代号>--/session-<uuid>/session.v<数字>.jsonl.zstd`。

**每个名字都要有去处**（Rin 第 1 轮 B）：`social-runtime/` 根目录里的每个名字，不是拷（上面的 5 项）、就是刻意不拷（`excluded_by_design`，回报名字）、`.part`／`.tmp` 暂存档（`transient_names`，计数），**其他一律算「未知」**，进 `skipped_names`／`skipped_examples`（最多 40 个名字）。各目录里不符合白名单的名字也一样（例如 `.bak`、`.tmp`、未来新增的档案种类）。所以 `skipped_names` 大于 0 就代表线上出现了没人评估过的新东西，例行同步看到就该来看一眼；现在是 0。

## 不拷什么（最终清单，每一项一句理由）

| VM 上的东西 | 为什么不拷 |
| --- | --- |
| `.credentials.yaml`、`profiles/*/cordis.patch.yml`、token、Doppler、`.anonymous-user-id`、`dsh-expression.json` | 凭证、密钥、设定：不是记忆，拷了只会多一个泄露点（这些也在 `social-runtime\` 与 `sessions\` 之外，脚本根本不会去读） |
| `social-runtime/usage/`（5 档 957,950 B）、`spend-state.json`（11,195 B） | 我判断**不算记忆**：是模型呼叫的用量与花费记帐，她回话时不读回来；每轮提示里的「飯錢」那行是从这里算出的数字，不是她记得的东西。真要让替身知道余额，可以单独加 `spend-state.json`（11 KB），不建议 |
| `discord-actions.jsonl`（24,122 B） | 她操作 Discord 的审计纪录（含参数），只写不读，不会回到她的上下文 |
| `session-scopes.json`、`state/`、`storages/` | 运行状态（会话与范围的绑定等），不是记忆 |
| `sessions/**/session.lock`（9 个，都是空档） | 运行时的锁档，没有内容；只计数（`session_locks_not_copied`） |
| `doctor.json`、`vital-events.jsonl`、`model-migration-probe.json`、`fish-memory-diag.log` | 体检与监控输出、引擎诊断日志 |
| `style-eval/` | 维护用的风格测试收发匣，线上自己的备份也排除它 |
| `backups/`（约 1.36 GB） | 备份：不是记忆，也太大 |
| `attachments/`（约 47 MB）、`meme-library/`（约 1.67 GB）、`nekowhale-stickers/`（约 191 MB） | 图片与贴图媒体档：不是文字记忆，太大；帐本里已有档名与类型 |
| `cache/`、`llm-deepseek/`、`fish-memory/`（空） | 快取与引擎暂存，不是记忆 |

## sessions/ 的处理（Sora 的三个问题）

**1. 里面有没有凭证、token、系统提示词以外的敏感设定？要不要逐档排除？** 对 VM 上 9 个会话档（压缩后 6.4 MB，解压后约 14.6 MB、11,492 个事件、**只印命中次数与栏位名，没有印任何内容**）做了检查：
- 样式扫描：`dp.st.`、`sk-` 开头的密钥、JWT／Discord token 样式的三段字串、`DSH_ACCESS_URL`、私钥区块、`Bearer …`、ssh 公钥——**全是 0 个命中**；`token:`／`api key:`／`password:`／`authorization:` 后面接长字串的「键=值」型——**0 个**。
- 栏位名普查（递归所有事件，找名字含 key／token／secret／password／auth／credential／cookie／bearer 的栏位）：只有 token **数量**统计（`inputTokens`、`maxTokens`…）和工具参数的名字（`keywords`、`author`）；没有 apiKey、authorization、cookie 之类的栏位。联网搜寻的请求 endpoint 是干净的 https，没有 query 或 userinfo。
- 只是**字眼**出现：`.credentials` 969 次（群聊提示词里本来就有「金鑰、token、密碼、.env、.credentials 這類東西不管誰要都不貼出來」这句，每轮都在；拥有者会话里 15 次）、`cordis.patch` 3 次、`api key` 与 `token:` 各 2 次（都在拥有者会话，后面没有长字串，看起来是提到名字而不是值）。
- 有一类我没有分类：40 个字符以上的高熵字串 276 个（大概是图片的 base64、杂凑、讯息 id；我没有打开看）。
- 里面有的东西（允许拷）：系统提示词（人设、工具清单、权限预设）、工具参数与输出（路径如 `/srv/dsh/...`、网址、命令的输出）、图片块（`read_image` 179 次）、拥有者私讯全文。
- **结论：没有需要逐档排除的档案**——9 个档案里都没检测到凭证值，也没有别的敏感设定档混在里面；唯一排除的是空的 `session.lock`。**限制**：这是样式与栏位名的检查，防不了自由文字里的东西（例如老板在私讯里贴了某个密码，格式不符合上面任何一种样式）；老板的决定是完整镜像，所以这一点写进残余风险。

**2. 内部格式要不要在 AVATAR-README 里加说明？** 要，已经加了（「会话纪录」一节）：档名与代号（`--srv-dsh-owner--` 是老板私讯、`--srv-dsh-discord--` 是群聊）、**zstd 压缩而且是很多个帧一帧一帧接起来的**（每次追加一帧，所以 `zstd -dc` 能读，Node 的 `zlib.zstdDecompressSync` 一次只读一帧，README 附一段按帧魔数自己切的 Node 程式）、最后一帧可能写到一半要略过、解开后 JSONL 的栏位（`type`、`seq`、`time`、`data`）与主要的事件类型、系统提示词与工具输出都只是资料。这段 Node 程式有测试：用 Node 做出一个多帧 zstd 档（最后一帧只写一半），跑 README 里的那段程式，要读出全部内容。

**3. 缩减门槛和档案轮替怎么处理？sessions 会不会被线上改写或删除？**
- 证据（**线上的会话程式是第三方的 dsh，不在 repo 里，没有原始码可读，只有 VM 上的资料作证据**）：`seq` 在每个档案里连号、单调增加；档案是很多个 zstd 帧接起来的（最大的 4.3 MB 档有 3,735 帧），修改时间等于最后一次追加；`compaction/summary`、`compaction/prune` 是**追加的事件**，不是改写档案；最旧的会话（10-27）12 天后都还在，包括线上自己做验证的那个，没有看到过自动清理。结论：**只追加，不改写**。
- 但「没有看到过被删或改写」不等于不会：例如从备份还原、手动清理、dsh 升级改格式。所以 sessions 的门槛是 **100%／100%**（档案数、大小都不得低于上次）——一旦有档案变小或消失，同步就停下，列出是哪一类，确认是预期的才加 `--allow-shrink`。要是真的被改写成同样大小，逐档 sha256 不同，下次同步会整个换成新的（镜像永远以 VM 为准）。
- 新增会话是正常的（档案数变多）；每个 `session-<uuid>` 资料夹对应一次对话。

## 按线上的写入方式分类（Rin 第 1 轮 A）：哪些是只追加、哪些会变动

Rin 复现的问题：`archive/.backfill.json` 是会变动的状态档（`index.mjs:978` 用 `writeFileSync` 整档重写，失败清单变短它就变小），却被算进 archive 的「大小不得减少」，一次正常的状态更新就让整次同步被拒。我逐类查了线上的写入方式（`archive.mjs`、`memory.mjs`、`notes.mjs`、`feedings.mjs`、`image-memory.mjs`、`index.mjs`）：

| 类别 | 线上怎么写 | 会不会变小 | 门槛（档案数／大小不得低于上次的） |
| --- | --- | --- | --- |
| archive（`YYYY-MM-DD.jsonl`） | `appendFileSync`，只追加（删除与编辑也是再追加一行） | 不会 | 100%／100% |
| memory（`<uuid>.md`） | 暂存档写好再 `renameSync`，一档只写一次；forget／recall 也是新档案 | 不会（没有任何删除或改写的程式） | 100%／100% |
| images（`image-memory.jsonl`） | `appendFile`，只追加 | 不会 | 100%／100% |
| sessions（会话纪录） | 帧一帧一帧追加（见上面的证据） | 没有证据会 | 100%／100% |
| notes（人物卡、日记、事实） | 整档重写（暂存档＋`renameSync`），内容是「原有＋新增」，**但站长可以直接手改**（档案开头就这样写） | 可能（手动删一条、删一张卡） | 95%／90% |
| **state**（`archive/.backfill.json`、`feedings.json`） | **整档重写**：`.backfill.json` 每次补抓都重写，失败清单可长可短；`feedings.json` 的「`/投餵 删 编号`」会删掉一笔 | **会**，而且是正常的 | **不看档案数与大小**；存在时必须是合法 JSON（来源端检查，写到一半或坏掉就中止，下次再来）；不存在也可以 |

「会变动却被当成只追加」的其他可能：我逐项看过，memory／images／archive 的日档／sessions 都没有（上表），notes 本来就有容许度，state 就是这次分出去的两个。`archive/.backfill.json` 与 `feedings.json` 还有一点：`feedings.json.part` 是它的暂存档，可能刚好留在根目录，已归入 `transient_names`，不算未知。
Rin 复现的情形（合成资料）：`.backfill.json` 从约 1.1 KB（20 笔失败）变成 30 B（空清单），同步照常通过（测试 `test_a_shorter_backfill_file_does_not_stop_the_sync`，输出附在回报里）。

## 对 VM「只读」的保证（四层，不变）

1. **程式只读**：在 VM 上跑的是 `pull_memory.py` 里的 `REMOTE`（约 215 行，没有任何内容判断），经 ssh 标准输入送过去，VM 上不落地任何档案。它只用 `os.open(…, O_RDONLY|O_NOFOLLOW)` 读，没有写入、删除、更名、子进程、网络。`test_pull_memory.py` 用 AST 逐项检查（只能 import 8 个标准模块；没有任何改动类呼叫；每个 `os.open` 都只带 `O_RDONLY`；唯一的输出是 stdout 的 tar），改了 REMOTE 就会失败。另有一项测试在假的 VM 树上跑前后比对，所有档案的大小与修改时间不变、不产生 `.pyc`；一项测试确认 REMOTE 里已经没有任何内容过滤的字样。
2. **Python 带 `-B -I`**：不写 `__pycache__`，不读环境变数与当前目录的模组。
3. **沙盒（默认，老板已同意）**：`sudo -n systemd-run --pipe --wait -p User=dsh -p ReadOnlyPaths=/var/lib/dsh -p ProtectSystem=strict -p PrivateNetwork=yes -p NoNewPrivileges=yes -p CapabilityBoundingSet= …`。以 dsh 帐号（不是 root）跑，`/var/lib/dsh` 在那个单元里是唯读挂载（`sessions/` 也在里面），没有网络、不能提权：就算程式有 bug，写入也会被核心拒绝。这会在 VM 的 `/run` 里建一个暂时的 systemd 单元（跑完自动消失，不写任何资料目录）。拉取后要确认单元已消失。
4. **不停服务、不碰设定**：整个过程不 `systemctl`、不改 profile、不 `plugin add`。她的运行与这个读取互不影响。**逐档读取，不是同一时间点的快照**（Rin 第 1 轮 B）：线上程式可能正在追加帐本、写新记忆或追加会话。读的时候档案变长了就重读（最多三次，`read_retries`；三次都在变就用最后一次读到的并记 `unstable_files`）；读到一半的最后一行／最后一帧**照拷**（`ledger_files_without_final_newline` 计数，README 告诉替身怎么略过）；列出来之后才消失的档案略过并计数（`vanished_files`）。所以某条记忆的出处讯息可能还没出现在帐本副本里，下一次同步就会补上。

**符号链接**（Rin 第 1 轮 B）：来源里**任何一层**目录（`social-runtime` 本身、`archive`、`memory`、各范围目录、`notes`、`notes/people`、`notes/journal`、`sessions`、各代号目录、各 `session-<uuid>`）和**每一个名字**（包括会被略过的杂项档名）都用 `lstat` 看过，是符号链接就中止（`symlink in source: …`），绝不跟进去。以前只有档案本身用 `O_NOFOLLOW` 挡下，目录是符号链接时 `listdir` 会照样跟进去。

`--no-sandbox` **只在紧急时用**（例如沙盒单元起不来又急着要拉）：它以 root 直接读，只剩第 1、2 层；`O_NOFOLLOW`／`lstat` 只检查路径的各层，不挡 root 读到别的挂载。每次使用会在 stderr 印警告。

## 来源缺失、空的、或变少

- VM 端：`social-runtime/archive`、`memory`、`notes`、`notes/people`、`notes/journal`、`notes/facts.md`、`sessions` 任何一个不存在，REMOTE 就以非 0 结束并且不输出任何东西。`image-memory.jsonl`、`feedings.json`、`archive/.backfill.json` 可以不存在（还没有人投餵、没看过图、没补抓过），回报在 `absent_optional`。
- 本机：archive／memory／notes／sessions 任何一类是 0 个档案，就中止，旧资料不动（即使加了 `--allow-shrink` 也一样）。
- **比上次少就停**：各类的门槛见上面「按线上的写入方式分类」那张表（`SHRINK_LIMITS`）。少了的正常原因只有从备份还原或手动清理（那时确认之后加 `--allow-shrink`）。state 类不看。第一次拉取没有「上次」，只检查四个必须有的类别不是空的。
- 本机还有两道：`memory\` 已存在时必须**完整**（见下），SYNC.json 缺失或坏掉**不算第一次拉取**、一律停下要人处理；只有 `memory\` 本身不存在、旁边也没有旧副本，才算第一次。

## 来源身分与来源连续性（2026-11-10 事故之后）

**事故**：2026-11-10 台北早上，线上的她（pve2 上的 VM 102）的网卡反覆断线，同步经跳板连不到它；有人把 pve 上 VM 102 的**旧副本**启动了（十几天前的磁碟，machine-id、MAC、IP 全都和线上的一样），同一个 IP 改由它回应，同步就连到了它。那一次是「档案变少」的门槛（archive 43→32）挡住的——但那是运气：门槛只看档案数和大小，**旧副本的档案若刚好更多、或差不多大，就挡不住**；而因为是克隆，machine-id 也分辨不出来。

**新增两道检查**（本机，`pull()` 里，抓到资料之后、换上去之前；任何一道不过就中止，`memory\` 不动）。顺序：空的 → 来源身分 → 来源连续性 → 变少门槛（这样「空的」就说空的，分岔就说分岔）。

1. **来源身分**（`check_source`）。REMOTE 多回报 `source`：`/etc/machine-id` 与 `/proc/sys/kernel/random/boot_id` 各取 sha256 前 16 码（不传原值；读法和其他档案一样只用 `O_RDONLY`，AST 测试照样通过）。本机把它和上次 SYNC.json 里的 `source` 比：
   - 读不到身分 → 中止（无从确认）；
   - **machine-id 和上次不同** → 不是同一台机器，中止；
   - **开机识别码不同** → 正常重开机就会变，放行并在输出标示 `source.boot_changed: true`（要人看一眼）；
   - **开机识别码「回到较早的一次」** → 一台机器不可能，代表同一个 IP 背后有两台在轮流回应，中止。SYNC.json 记着最近 50 次不同的开机（`source.boots`）；
   - 上次的 SYNC.json 还没有 `source`（这个检查上线之前的同步）→ 只记录（`source.first_seen: true`），不比对。
2. **来源连续性**（`check_continuity`）。上次同步里已有的、「只增不改」的档案——archive 日档、sessions、images：这次必须还在，而且**旧内容是新内容的开头**（只追加）；memory 记录（一档一次写好）：有就必须一模一样。notes（会被整档重写）与 state 不在其中。不对的会列出前 5 个（档名＋原因：不见了／变短了／开头对不上／被改过，不含档案内容）。这一道**不看档案数和大小**，所以挡得住「档案更多」或「一样大但内容不同」的分岔。

放行不隐瞒：输出多了 `source`（`machine_id`、`boot_id`、`boot_changed`、`first_seen`）与 `continuity`（`checked`、`unchanged`、`appended`、`violations`）；SYNC.json 记 `source`（含开机历史）。人确认来源变了是预期的（例如老板决定改认另一份资料）之后，手动加 **`--accept-source-change`**——**排程永远不加**，和 `--allow-shrink`、`--no-sandbox` 一样；它只放行来源身分与连续性，**不放行**「空的」与「变少」（要两个旗标都明讲）。

**挡不挡得住这次的旧副本？挡得住，而且不是靠档案数**（用真实数据验证，没有写任何东西）：
- 我把旧副本的磁碟（VM 已关机，`qemu-nbd -r` 只读离线挂载，用同一支 REMOTE 读出「那次同步若成功会拿到什么」）重放给检查：旧副本 126 个档案对镜像的 480 个。**连续性检查拦下，356 个只增不改的档案对不上**：5 个会话档变短（例如 4,333,430→511,087 位元组）、`archive/2026-10-29.jsonl` 变短、**`archive/2026-11-10.jsonl` 开头对不上**（同一天，旧副本和线上是两条不同的线）、300 多个 memory 记录来源上没有。旧的门槛也拦下（archive 43→32）；两道各自都挡得住。
- 同一套检查对线上的完整版（pve2 的 VM 102，当时的真实资料）：464 个只增不改的档案，462 个不变、2 个只追加、**0 个违规**；档案数 480→481，没有误拦。
- 身分这一层对这次的旧副本帮不上第一步（克隆，machine-id 相同；开机识别码是新的，看起来像重开机），但它的第二个作用成立：旧副本若曾被接受，线上一回来，开机识别码就是「回到较早的一次」→ 拦下。

**限制（诚实写出来）**：
- 克隆**刚出现、内容还和线上完全一样**时，没有任何办法分辨（测试里有这个情境）；等两边各自长出不同的内容，下一次同步就拦下。也就是说检测会晚一轮，不是即时的。
- 只比对上次已经在镜像里的档案；新出现的档案（新的一天）没有东西可比。
- memory 记录要求逐字节相同：如果线上程式其实会改写记录（我没有看到会），会被误拦——拦下是安全的一侧，要人确认。
- notes 与 state 不检查（它们本来就会被整档重写或变短）。
- 身分是 VM 自己回报的，不是防伪造的；假设 VM 本身没被恶意控制。
- 开机历史只在真的换上去时更新；内容没变的那一小时不写 SYNC.json（`memory\` 不动），只在输出里标示 `boot_changed`。

## 本机的目录、权限与换上去的方式

```
memory\
  README.md          替身怎么读（来自 AVATAR-README.md）
  SYNC.json          上次内容有变化的时间、数量、统计、每个档案的 sha256（没有内容）
  social-runtime\    镜像，路径照 VM 上的原样
    archive\  memory\<范围>\  notes\{people,journal,facts.md}  image-memory.jsonl  feedings.json
  sessions\          会话纪录，路径照 VM 上的原样
    <代号>\session-<uuid>\session.v4.jsonl.zstd
```

- 收到的档案**路径只认白名单**、逐档核对 sha256 与清单、有大小上限（单档 64 MiB、合计 256 MiB；现在合计 7.6 MiB，见下面的增长估计）。
- **整个 `memory\`** 先在隔壁的 `.memory-incoming-<时间>\` 建好，**权限也先在暂存目录收紧**，然后一次换上去：先把旧的 `memory\` 改名成 `.memory-old-<时间>`，再把新目录改名成 `memory\`；第二步失败就把旧的改回来；任何一步失败，旧内容都不动，暂存目录会被清掉。换好后才删旧目录（删不掉不算失败，下次拉取在确认 `memory\` 完整之后会清掉遗留的）。
- **内容和上次完全一样就不换**：这次抓到的每个档案 sha256 与 SYNC.json 记的相同、README 也和模板相同时，什么都不动（也不更新 `synced_at_utc`，它表示「上一次内容有变化的时间」），回报 `changed: false`。好处是整点同步大多数时候不碰 `memory\`，替身正开着里面档案的机会就少。
- **memory\ 顶层只能有四样**（Rin 第 3 轮 A）：`social-runtime`、`sessions`（目录）、`README.md`、`SYNC.json`（档案）。多出任何东西——不明的档案、目录、符号链接，或这四个名字的类型不对——就**中止、不替换、不清理**，错误讯息列出这些项目的名称（最多 20 个，之后写总数）。镜像里面也一样：SYNC.json 没登记的档案、没有登记档案的目录（含空目录）、任何符号链接，都中止并列名。这个检查在**碰 VM 之前**做一次（`preflight` → `verify_complete`），**换上去之前**（抓资料的几秒里有人放了东西）再做一次。
- **同一时间只允许一个拉取**：`memory\` 隔壁的 `.memory-sync.lock`（独占建立），第二个拉取直接拒绝，也不会碰对方的暂存目录与旧目录。程式被强制结束时会留下锁档，**不会自动清**：确认没有别的拉取之后手动删除。
- **动手前先看现状**（在碰 VM 之前）：`memory\` 存在 → 顶层检查、SYNC.json 在而且格式对、档案集合与每个档案的 sha256 跟 SYNC.json 记的一模一样、README.md 在；任何一项不对就停下要人处理，**不覆盖、不清理**。确认完整之后，才清掉遗留的暂存目录与旧副本。`memory\` 不存在而旁边有 `.memory-old-*` → **不清理**，停下要人处理（确认后把它改名成 `memory` 即可恢复）。
- 换上去失败**而且还原也失败**时（`SwapFailed`）：旧资料留在 `.memory-old-*`、新资料留在 `.memory-incoming-*`，两份都不删，错误信息写明恢复方法。
- 目录权限：`icacls … /inheritance:r /grant:r <本机使用者>:(OI)(CI)F`，然后读回来确认只剩这一个使用者。
- Windows 上如果替身正好打开着 `memory\` 里的档案，改名会失败：脚本停在第一步、什么都没改，下次再跑即可（排程的话就是下一个整点）。

## 执行步骤（Rin 通过之后）

1. 先 `--dry-run`（沙盒），只印数字，预期和上面的表差不多。
2. `python deploy\avatar-sync\pull_memory.py --target C:\Users\USER\OpenBot\Agents\agent-0d3a41de-06ad-403a-b9c3-f774686407fc\memory`
3. 核对并回报：档案数与大小、sha256 核对结果（脚本已逐档核对）、`icacls` 读回只剩本机使用者、`memory\` 隔壁没有 `.memory-incoming-*`／`.memory-old-*`／`.memory-sync.lock`、**VM 上的临时单元已消失**（`systemctl list-units 'run-*'` 之类的只读检查）、没有 `.credentials`／`token`／`cordis`／`usage`／`spend` 字样的档案名、`skipped_names` 是 0、`unstable_files` 是 0。
4. 请替身读 `README.md`，用合成问题或老板指定的问题，确认它会「先查索引、再按出处读原文」，并且确认私讯规则有被遵守（一个以非老板身分问私讯的合成问题）；也请它读一个会话档（用 README 里的 Node 片段）。不把内容贴到回报。
5. 排程：主管_Sora 之后用 OpenBot 的 routine 排；**这里不设**。

## 第一次拉取（2026-11-08，Rin 第 2 轮通过、老板批准之后）

沙盒模式（默认），没有加 `--no-sandbox`／`--allow-shrink`；VM 上没有写任何东西、没有停服务。结果（只有数字）：
- 结束码 0，stderr 空；`ok: true`，`changed: true`，`fetch_seconds` 8.6（整个指令 9.3 秒）。
- 每一类（档案数／大小）：archive 41／1,313,694 B；memory 401／121,279 B；notes 14／95,719 B；images 1／21,059 B；state 2／198 B；sessions 9／6,438,817 B；合计 **468／7,990,766 B**（比 dry-run 多的 1 个档案、几百位元组是线上在这之间新写的一条记忆与人物卡更新）。
- `skipped_names` 0、`unstable_files` 0、`read_retries` 0、`vanished_files` 0、`session_locks_not_copied` 9、`excluded_by_design` 8 个名字。
- 独立重算：我另外把 468 个档案各算了一次 sha256，与 `SYNC.json` 逐档比对——**468 个全部吻合**，没有缺少、没有多出、没有符号链接；`README.md` 与模板逐字节相同；`memory\` 顶层只有 `README.md`、`SYNC.json`、`sessions`、`social-runtime`。
- 权限（`icacls` 读回）：`memory\` 只有 `MSI\USER:(OI)(CI)(F)`，里面的档案只继承这一个（`MSI\USER:(I)(F)`）。
- 旁边没有 `.memory-incoming-*`、`.memory-old-*`、`.memory-sync.lock`（工作区里只有 `.agents`、`.claude`、`memory`）；档名里没有 credential／token／cordis／usage／spend／doppler／.env 字样。
- VM：`systemctl list-units --all 'run-*'` 只有 9 个本来就有的 `run-credentials-systemd…mount`（not-found／inactive），**没有这次的暂时单元**；没有 failed 的单元；`dsh` 服务 active（没碰）；`/run`、`/tmp` 没有残留；sudo 纪录里是 `systemd-run --quiet --pipe --wait --collect -p User=dsh … -p PrivateNetwork=yes …` 这条沙盒指令。
- 紧接着又跑了一次同样的指令：结束码 0，`changed: false`（两分钟内 VM 上没有新内容），没有换目录、没有残留——验证「没变化不换」在真实环境也成立。

## 排程用的指令（给 OpenBot routine）

- Shell：PowerShell（Windows PowerShell 5.1 或 pwsh 皆可）；工作目录：`C:\Users\USER\Documents\discord-pre-agi`。
- 指令（单行，正好这样）：
  `python -B deploy\avatar-sync\pull_memory.py --target C:\Users\USER\OpenBot\Agents\agent-0d3a41de-06ad-403a-b9c3-f774686407fc\memory`
  不加任何其他参数——**尤其不加 `--no-sandbox`、`--allow-shrink`**。预设值就是沙盒、严格门槛。
- 怎么判断：
  - **成功**：结束码 0，stdout 是一个 JSON，`"ok": true`。`"changed": true` ＝ 有新内容、已整个换上去；`"changed": false` ＝ 和上次一样、什么都没动（`last_change_utc` 是上一次真正有变化的时间）。
  - **成功但要人看一眼**：JSON 里 `stats.skipped_names` 或 `stats.unstable_files` 不是 0（前者是 VM 上出现了白名单之外、没人评估过的东西；后者是有档案读了三次都还在变）。同步本身是成功的。
  - **成功但要人看一眼（再补一项）**：JSON 里 `source.boot_changed` 为 `true`（VM 重开过机；同步本身成功，运行状态仍算成功）。**回报里要写明，并发信给主管_Sora 转给人看**（写开机识别码前 16 码的变化即可，不必升级处理）。`continuity.violations` 在没加 `--accept-source-change` 时一定是 0。
  - **被拦下**：结束码 1，stdout 没有 `"ok": true`，stderr 第一行以 `拒绝／失败：` 开头，后面是原因。**不要重试，不要加参数**，把那一行回报给人。常见的几种：`比上次少`（门槛拦下——线上可能轮替了或从备份还原；人确认上游之后才可以手动加 `--allow-shrink`）、`memory\ 顶层有不在预期名单里的东西`／`里有 SYNC.json 没登记的东西`（有人往 `memory\` 放了东西，列出名字）、`另一个拉取正在进行，或上一次被强制结束留下了锁档`（确认没有别的拉取之后手动删 `.memory-sync.lock`）、`VM 端失败`（ssh、sudo、沙盒、来源缺失、符号链接、状态档不是合法 JSON；2026-11-10 起，ssh 在收到程序之前就结束时，后面会直接带 ssh 自己的错误，例如 `No route to host`，不再是 `Broken pipe`——单次多半下一个小时就好，**但见下面「连续 2 次」**）、`换上去失败`（Windows 上替身正开着 `memory\` 里的档案；脚本什么都没改，下个小时再来）、`SwapFailed`（极少见：换上去和还原都失败，旧资料在 `.memory-old-*`、新资料在 `.memory-incoming-*`，必须人处理）、**`来源和上次同步的不是同一条线`**（来源连续性：档案变短／开头对不上／不见了——可能连到了旧副本、备份还原、分岔的副本）、**`来源的 machine-id 和上次同步的不同`**、**`来源回到了较早的一次开机`**（两台机器轮流回应同一个 IP）、`来源没有回报身分`。这几个（`--accept-source-change` 才放得过的）**一次就升级**，不要等。
  - **连续 2 次就升级处理**（2026-11-10 的事故：同步连续失败好几个小时才有人去查原因）：同一个原因（尤其 `VM 端失败`，含 `ssh 在收到程序之前就结束了`）**连续 2 次**被拦下，就升级给主管_Sora——信里写结束码、stderr 那一行、连续几次、上一次成功是几点；之后每一次仍失败不必重复写长信，但要在回报里写「连续第 N 次」。升级之后仍然不重试、不加参数、不碰 VM。
  - `--accept-source-change` 只有人确认来源之后才手动加一次，**排程里永远不加**。
  - 一次执行大约 9～10 秒（见下）；排程的超时设 120 秒以上。
- **没变化时的输出也带 `stats`**（第一次拉取之后补的一行，只改输出，不改行为；测试 `test_identical_content_is_not_swapped_and_the_directory_is_left_alone` 加了对应检查），不然内容没变的那些小时，排程看不到 `skipped_names`／`unstable_files`。
- 不要自己设别的排程（例如另一个同时跑的拉取）；锁档会挡下第二个，但被拦下的那次会算作失败。

## 同步频率与增量评估

- **一次完整同步的成本（第一次正式拉取实测）**：`fetch_seconds` **8.6**，整个指令 **9～9.3 秒**（468 个档案、7.99 MB，从本机 ssh 经跳板到 VM、起沙盒单元、读完、把 tar 传回来、逐档核对、建暂存目录、收紧权限、换上去）。比我事前估的 2～5 秒慢：传输大约 1 MB／秒，成本几乎全在经跳板的 ssh 传输，VM 端读 8 MB 的 CPU 与 IO 可以忽略。内容没变时的那次也是 8.6 秒（没变化也要把全部内容抓回来才知道）。
- **增长**：sessions 是大头：6.4 MB 是约 12 天累积的（平均约 0.5 MB／天，很不均匀：一个 4.3 MB 的群聊会话占了大头，它在 11-06 之后没有再增长），帐本约 32 KB／天，记忆记录很小。照这个速度每年约增加 200 MB，**256 MiB 的总量上限约一年半**会碰到；`REMOTE` 目前把所有档案读进记忆体再一次送出，到 100 MB 以上就该改成串流。按 1 MB／秒算，每天约 +0.5 秒。
- **建议：现在每小时一次完整镜像仍可以**（9 秒、8 MB／次，一天约 190 MB 的传输，对 VM 与本机都不构成压力），**不做增量**。我之前写的「单次超过约 10 秒就改增量」触发点其实已经快到了（9 秒）——这个门槛是我自己拍的，没有硬性理由：我把它改成 **单次超过约 30 秒、或总量超过约 40 MB** 再改增量（约 2 个月后）。要做的话：会话档只追加，本机先报每个档案的大小与前缀 sha256，VM 只回传有变的档案的新增位元组，本机接上去并核对整档 sha256；帐本日档同理；换上去的流程不变。
- 更频繁（例如 15 分钟）不建议：每次 9 秒的传输，一天 96 次，Windows 上替身开着档案时换上去失败的机会也更多（失败只是下一次再来）。
- 排程时请看每次输出：`ok`、`changed`、`fetch_seconds`、`stats.skipped_names`、`stats.unstable_files`；非 0 结束码＝被拦下，原因在 stderr。

## 失败时

- ssh、sudo、沙盒任何一步失败：脚本以非 0 结束，本机目录不动，VM 没有留下东西（单元会自动清掉）。**ssh 在收到程序之前就结束**（连不上跳板／VM、认证或主机金钥不对）时，会先读出 ssh 的 stderr 再报「VM 端失败（ssh 在收到程序之前就结束了，结束码 N）：…」，连线设定的值与它的每一段（使用者、主机名称、跳板、路径各段）和 IPv4 位址会被遮成 `<连线设定>`／`<ip>`（这条讯息会被排程转寄）；以前这种情况只看得到 `[Errno 32] Broken pipe`。
- 来源身分不对、或和上次同步的内容不连续：拒绝，不换（见上面「来源身分与来源连续性」）。
- 收到的东西不合格（路径、sha256、清单、大小、空的、变少）：拒绝，不换。
- 来源里有符号链接、状态档不是合法 JSON：VM 端中止，不输出任何东西。
- `memory\` 顶层或镜像里有不明项目：拒绝，列出名称，不换不清。
- 换上去的中途失败：自动还原旧目录（有故障注入测试）。
- 想撤销：删掉 `memory\` 整个目录即可（里面只有拷贝，没有别的）。

## 残余风险（已知，这一版不处理）

- **`--no-sandbox` 以 root 执行**：见上，只在紧急时用；正式拉取一律走沙盒。
- **副本里有私讯、已删除的内容、老板私讯的完整会话**：这是老板的决定。技术上的保护只有目录权限（只留本机使用者）；本机备份或同步软件若涵盖 `C:\Users\USER\OpenBot`，副本会跟着被备份（没有确认过）。**私讯规则只是行为约束，没有任何技术检查「是谁在问」**——替身会不会照 `AVATAR-README.md` 的规则讲话（以及能不能分辨问的人是不是老板），要靠替身那边的测试（Sora 之后会直接测替身）。
- **sessions 里的自由文字**：样式与栏位名检查没有发现凭证，但防不了格式不符的东西（例如老板在私讯里贴的某个密码）。
- **不是同一时间点的快照**：逐档读取（见上），读的时候有重读与计数，但档案之间可能差几百毫秒；会话档的最后一帧可能写到一半。
- **REMOTE 把所有档案读进记忆体**：现在 8 MB 没问题，到 100 MB 以上要改成串流（见增长估计）。
- **白名单之外的新种类**不会自动镜像，只会在 `skipped_names` 里被回报。
- **状态档写到一半**：`.backfill.json`／`feedings.json` 不是原子写入（`.backfill.json` 直接 `writeFileSync`），刚好被读到半截时 REMOTE 会因为 JSON 不合法而中止，下一次同步再来。
- 线上的会话程式（dsh）是第三方的、没有原始码可查：「只追加」是根据资料推断的，门槛设成 100% 就是为了让推断错了的时候停下来。
- 锁档遗留需要人手动删（见上）。
- **以下两点都是推断，尚未证实**（Rin／Sora 要求写明）：(1) 前面说的「40 字符以上的高熵字串 276 个」我没有分类，当成图片 base64、杂凑、讯息 id 是猜的；(2)「sessions 只追加、不改写」是根据 `seq` 连号、zstd 帧一帧追加、修改时间、最旧的会话 12 天还在推出来的，线上的会话程式是第三方的、没有原始码可查——所以门槛设 100%，推断错了会停下来。
- **重读时只比较长度**：读到一半档案变长就重读（`read_retries`），但如果线上在我们读的时候把档案**改写成同样大小**，这个检测看不到，拷到的可能是新旧混合的内容。帐本、记忆记录、会话纪录都是只追加或一次写好的，所以这在正常情况下不会发生；逐档 sha256 只能证明「传输没出错」，不能证明「读的那一刻内容一致」。
- **每次同步都要有人（或排程的回报）核对 `skipped_names` 和 `unstable_files`**：前者不是 0 就代表线上出现了没人评估过的东西；后者不是 0 就代表有档案读了三次都在变，那次的副本可能不一致。
- **上游如果轮替（会话被清理、帐本被还原），先确认过，才加 `--allow-shrink`**：被门槛拦下的那一次不要重试、不要直接加参数；先去看是哪一类变少了、为什么，确认是预期的之后手动加一次。排程里永远不加这个参数。
- 合成问题以外，没有用真实内容做过任何测试（测试全用合成资料）。**例外**：来源连续性另外用真实资料验证过两次（线上完整版放行、旧副本磁碟重放被拦下，见上面那一节），结果不进测试（含私讯，不放进 repo）。
- 来源检查的限制见「来源身分与来源连续性」那一节最后：克隆刚出现、内容还一样时分辨不出，会晚一轮才拦下。

## 测试与证据

`python -B -m unittest test_pull_memory`（在 `deploy/avatar-sync/`）：119 项（2026-11-10 来源检查之前是 77 项，新增 42 项）。**本机**：114 项通过、5 项因 Windows 不能建符号链接而跳过。**Linux 容器**（python:3.12-slim，root 与 uid 1000 各一次）：各 118 项通过、1 项跳过——跳过的是 README 的 Node 片段那项（镜像里没有 node；它在本机有 node 的环境通过），而本机跳过的 5 项符号链接测试在容器里**真的执行并通过**。涵盖：
- REMOTE 的只读检查（5 项）与「REMOTE 里已经没有内容过滤字样」；
- **镜像**：每个记忆类档案（含 sessions）逐字节照拷、路径相同；私讯、被删讯息及其删除标记、编辑行与编辑前的版本、没有出处的记录／事实、`recall`、拥有者范围、待確認一节、附件网址**全部都在**；凭证、用量、花费、运行状态、监控、风格测试、`session.lock` **都不在**；白名单之外的名字被略过并按名回报（含 sessions 里的）；**根目录的每个名字都有去处**（未知的被回报、刻意不拷的列名字、暂存档计数）；写到一半的最后一行照拷并计数；可以不存在的档案；列出后才消失的档案略过；**读的时候变长的档案会重读**；**状态档存在时必须是合法 JSON**；来源缺失（7 种，含 sessions）与空的中止；dry-run 只印数字；两边（VM 端与本机）对类别的看法一致；
- **符号链接**：来源里任何一层目录（9 个层级）是符号链接都中止、什么都不输出；被略过的杂项名字是符号链接也中止；
- 接收端的白名单与清单核对（含每一种镜像路径、各种不该收的路径，含 sessions 的假路径）、**目标只接受替身**；
- **缩减门槛**：archive／memory／images／sessions 一个字节、一个档案都不许少；notes 容许 5%／10%；**state 类可以变短甚至消失**（Rin 的复现）；空的永远不放行；
- **原子换上去与故障注入**（建暂存、权限、第一步改名、第二步改名、还原也失败）、遗留目录下次清掉、锁、SYNC.json 缺失／坏掉／内容不符、清理只在确认完整之后；
- **顶层与镜像内的不明项目**：不明档案、目录、符号链接、类型不对、抓取期间出现的东西，包括 `sessions\` 里的；
- **没变化不换**；`fetch_seconds` 有记录；
- **README**：给替身的会话读法用 Node 实测（多帧、最后一帧写到一半）；老板要求的规则字句都在。
- **来源身分与来源连续性（新增 40 项）**：REMOTE 的 `fingerprint` 只读只杂凑、不传原值、读不到就是 `None`；`check_source` 的每一种情形（读不到身分、第一次、旧 SYNC.json 没有 source、同一次开机、重开机、machine-id 变了、**回到较早的一次开机**、`--accept-source-change`、只记最近 50 次）；`check_continuity`（只增不改的档案：不变／只追加通过，变短、开头对不上、同样大小内容不同、sessions、不见了、memory 被改都拦；notes／state 可以重写或变短；讯息不含档案内容）；**整条 `pull()`**：第一次记录来源、正常增长加重开机通过并回报、**事故重演（档案数与大小都比镜像多、只有同一天的帐本内容对不上，旧门槛看不出来，连续性拦下）**、事故原样（缺天数、档案少）、接受过另一台之后退回第一台、machine-id 不同与 `--accept-source-change`、**接受旗标不放行「变少」与「空的」**、空的来源讲空的、内容没变也回报重开机且不动 `memory\`、VM 没回报 source、**「克隆刚出现、内容一样」的限制情境（晚一轮拦下）**；**Broken pipe**：真的子进程（ssh 提前结束）、假的 Popen（写入时 BrokenPipeError）、ssh 没留下错误输出、串流断在中途、dry-run 的错误讯息、`main()` 印在 stderr 第一行；讯息里连线设定的值、**它的每一个组成部分**（user@host 的使用者与主机名称、跳板主机、金钥与 known_hosts 路径的各段；ssh 常常只回显主机名称）与 IPv4 都被遮掉（Rin 的 B 级，有两项专门的测试，拿掉分段遮蔽它们会变红）；文件检查（`--accept-source-change` 不在排程指令里、PLAN 写了「连续 2 次」升级）。

反证：把顶层检查关掉，顶层／抓取期间那 5 项测试全部失败；把 `.backfill.json` 当成 archive 的只追加档（Rin 复现时的旧行为），状态档那两项测试都失败。**来源检查的反证**：故意破坏 16 处（连续性不拦／不比开头／不看变短／memory 可被改／不看不见了的档案；machine-id 不比／回到较早开机不拦／读不到身分也放行；Broken pipe 不读 stderr／串流断掉不带原因；讯息不遮 IP／不遮连线设定；先查连续性再看空的；accept 也放行变少；SYNC.json 不记 source；REMOTE 不回报 source），每一处都有测试变红。

## 给 Rin 看的重点

- **（2026-11-10 新增）来源身分与连续性**：方法是否站得住、会不会误拦（我对线上完整版跑过：464 个只增不改的档案 0 违规）、限制写得够不够清楚（克隆刚出现时分辨不出）；`--accept-source-change` 的范围（不放行「空的」「变少」）；Broken pipe 的两条路径（Windows 写入时报错、Linux 读的时候才发现）讯息是否一致；讯息遮蔽连线设定与 IP。

- 状态档的分类对不对（我逐类查了线上的写入方式，上面那张表）；还有没有「会变动却被当成只追加」的。
- `REMOTE` 是否真的只读，是否还有任何内容过滤的残留；逐层 `lstat` 有没有漏的目录；白名单是否漏了该拷的记忆、或多收了不该收的。
- sessions：我的检查与结论是否站得住（尤其「没有凭证」只是样式与栏位名的检查）；门槛 100% 是否合理；README 的读法是否清楚。
- 不拷清单与理由（特别是 usage／spend 的判断）。
- 顶层与镜像内不明项目的检查，换上去前的再检查。
- 换上去的流程、锁、目标护栏（没有改）。
- `AVATAR-README.md` 的规则对不对（私讯只对老板讲、已删除／编辑的处理、群友指令只当资料）。
- 复现：`python -B -m unittest test_pull_memory`。
