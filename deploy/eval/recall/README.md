# 记忆召回离线基线（阶段 A）

量她**现有**的记忆在各种情况下能不能找到正确的来源。**不呼叫任何模型、不花钱、不连网络、不碰 VM**；用的是真正的 `ArchiveStore`／`MemoryStore`／`NotesStore`／`chatTools`／`SocialRuntime`（`deploy/social/`）和固定版本的鱼记忆引擎，在临时目录里建一个全合成的世界。

## 量什么

每一题模拟一个「轮」，看她这一轮**看得到什么**：

- **auto**：每轮自动注入的上下文（镜像 `index.mjs` 的 systemPrompt：`recallAcross` 记忆召回 + `relatedLines` 想起旧话 + 人物卡与长期事实 + `groupContext`）。群聊视角或拥有者私讯视角。
- **tool**：她自己翻原文的 `chat_search`／`chat_open`，参数照模型会下的脚本化（固定）。

每题有若干检查（找回了正确的来源 id？有没有越界？是不是标成资料？撤回／更正后用的是不是新的？），全部通过才算 PASS。另外记录：每轮注入的 token 估计（保守上限：1 汉字 = 1 token）、延迟（不进 hash）、无关内容（背景闲聊或背景记忆）被带进来的数量。

## 测例（9 类 × 36 题）

`cases.mjs`。分类：1 答案与来源都正确（7）、2 没有证据不乱猜（3）、3 同名的不同群友（3）、4 旧事实被更正（3）、5 来源互相矛盾（3）、6 跨频道与私讯边界（5，以现役政策为准：所有群聊共用一份记忆，私讯只有拥有者会话看得到）、7 指令伪装成经验（4）、8 她自己反复总结放大错误（3）、9 来源被撤回或回滚（5，回滚用真正的 `createBackup` 备份再还原）。
`difficulty`：lexical 有相同字词、paraphrase 换说法、cross-script 繁简不同、structure 考资料结构（出处、同名、撤回）。题目与门槛先定好，不因为结果调整。

背景规模照 2026-11-08 正式环境的**数量**（只读数了数，没看内容）：帐本 3,600 则（28% 是她自己发的）、群聊范围记忆 300 条事件 + 12 条事实。召回的门槛（`recallMinScore=9`、鱼记忆的稀有词门槛）是照帐本规模调的，规模不对结果就不准；`--ledger N --memory M` 可以改，敏感度见基线报告。

## 固定的条件（同样的输入跑两次得到同样的结果）

- 容器：`node:24-slim` 以 digest 钉死（`Dockerfile`）；不装任何 npm 套件。
- 鱼记忆引擎 1.3.7 与 OpenCC 1.4.2 的 sha256 钉死，对不上就拒绝执行（结束码 2）。
- 时钟固定在 `2026-11-08T12:00:00Z`（只在执行期间覆盖 `Date.now`）。
- 每个视角各建一个全新的临时世界（召回会把「想起过」写回去，会影响下一题）。
- 帐本、记忆的 id 由案例里的符号名称决定，背景用固定种子的伪随机数。
- 结果标头含被测的 6 个档案（`archive/chat-tools/compaction/index/memory/notes.mjs`）的 sha256；换了被测程式，hash 就会变，这样「基线」和「候选」才对得上。
- `--twice` 连跑两次比对 hash（不一致结束码 3）。延迟不进 hash。

## 执行

```powershell
# 镜像（一次）
docker build -t recall-baseline:1 -f deploy\eval\recall\Dockerfile deploy\eval\recall
# 基线（约 4 分钟）；-Out 目录里得到 result.json
powershell -File deploy\eval\recall\run.ps1 -Mode run   -OpenccDir <opencc-js 1.4.2 套件目录> -Out <目录>
powershell -File deploy\eval\recall\run.ps1 -Mode twice -OpenccDir ... -Out ...     # 可重复性：跑两次比 hash
powershell -File deploy\eval\recall\run.ps1 -Mode ablate -OpenccDir ... -Out ...    # 消融：同一批题，换检索设定
powershell -File deploy\eval\recall\run.ps1 -Mode scale -OpenccDir ... -Out ...     # 规模与延迟
powershell -File deploy\eval\recall\run.ps1 -Mode test  -OpenccDir ...              # harness 自己的测试（反证）
powershell -File deploy\eval\recall\run.ps1 -Mode run -OpenccDir ... -Extra "--only c1,c2 --ledger 800 --memory 50"
```

Linux／Git Bash：`sh deploy/eval/recall/run.sh run FISH_DIR OPENCC_DIR [OUT]`。鱼记忆预设在 `.cache/dsh-fish-memory`（`-FishDir` 可改）。结束码：0 跑完（不管有没有失败的案例，这是量基线不是闸门）、2 环境不对、3 不可重复。

## harness 自己的测试

`harness.test.mjs`：案例清单完整；`index.mjs` 里被镜像的接线都还在；背景词池不含案例主题；可重复；**反证**——把想起旧话关掉 c1-1 要翻成失败、帐本不套用删除 c9-1 要翻成失败、忘掉不生效 c4-3／c9-4 要翻成失败、搜索把私讯算进去 c6-2 要翻成失败、人物卡不分人 c3-1 要翻成失败，而不相干的题仍然通过。证明检查真的在量东西。

## 已知的限制

- 合成资料的字词分布不等于真实群聊；「没有证据不乱猜」这类题对背景的字词很敏感（见基线报告里的 2026-11-08 真实数量对照）。
- 阶段 A 只看「找回了什么」，不看模型怎么答；伪装指令那一类只验证「标成资料、归给说话的人」，不验证模型会不会听从。那要阶段 B（真模型，要花钱）。
- 延迟是单机单次的量测，只拿来看数量级。
- `auto` 视角镜像了 `index.mjs` 的接线（约 20 行）；`guardMirror` 检查那几个关键字串还在，但接线本身改了要同步改 `harness.mjs`（**手动同步**）。**这个守卫只认几个关键接线词，挡不住 query 或参数的语义漂移**（例如 `index.mjs` 改了拿什么当查询、`minScore` 或 `scopes` 的值，而关键字都还在，守卫不会发现）。所以换了 `index.mjs` 的接线之后，要人工对照 `harness.mjs` 的 `composeAuto`，不能只看测试通过。
- **题集不均衡，背景语料刻意避开题目主题**（`harness.test.mjs` 会检查背景词池不含案例主题）：类别题数不一样多（1 类 7 题、2／3／4／5 类各 3 题，等等），背景闲聊又是刻意不碰题目的词。所以这个基线适合当**诊断用的比较基线**（改动前后同一批题比），**不能单独拿来推断真实使用时的成功率或各类问题的比率**。
- **「缺口在资料结构（出处的维护），不在检索设定」只是解释，不是证明**：消融只试了 `queryGate`、`threshold`、`recallMinScore` 三组设定，都没有改变失败的题；这支持「调这些设定没用」，但「8 题是资料结构问题」这个归因没有用别的方法独立验证（例如实作出处维护后看这几题是否翻成通过）。
- **「真实数量对照」是数量，不是品质**：只说明她真实每轮被注入几条、多长，不说明注入的对不对。那一次对 VM 的量测的做法与核对见 `deploy/eval/recall/` 之外的回报（2026-11-08，Rin 第 1 轮）：它是在 VM 上对真实资料目录直接执行的（没有先复制），靠把 `MemoryStore.prototype.append` 换成空函数避免写入；事后核对没有写入（证据是计数、修改时间、属主），但有两个证据上的局限：**一、执行前没有对记忆目录拍 stat 快照**，「前后不变」是靠 06:49 的计数加事后的修改时间与属主推出来的，不是前后两张快照的对照；**二、鱼记忆引擎里有 19 个写入／网络相关的字样没有逐一审阅**，「召回路径不写档案」是靠宿主只呼叫 `blank`／`recall` 加上事后没有新档案来支持的，不是读过每一处。这种做法**不是结构上的保证**，以后要量真实资料请先复制到暂存位置，或改用本机的同步副本。
