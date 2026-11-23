# 新姿势零件：设计（只是计划，未实作、未部署）

老板定的流程：**她挑素材、裁图、选空隙；维护员封印。** 2026-11-02 起草，等 Iris／老板决定第 4 节的目录方案、Rin 审第 5 节。

**状态（2026-11-02）**：老板选了方案 B。本机实作完成，等 Rin 审；部署步骤见 `DEPLOY.md` 的 0.2.1 段，还没部署。

| 第 6 节的捷径 | 实作在哪 | 测试 |
|---|---|---|
| 候选放进 parts | 零件目录 root 755/644 | 容器里以 dsh 复制、改 json、改 current 都是 Permission denied |
| 自己写／改 sidecar | `load_part` 只认 `whaledraw-parts/<组>/`，洞数要等于重新量到的 | `test_candidate_is_never_loadable`、`test_bad_seal_contents…` |
| 复制／连结到读得到的路径 | realpath 检查＋零件总目录里的连结要 root 的、只指同层 | `test_current_link_must_be_roots…`、`test_self_sealed_part…` |
| 交了之后改 candidate.png | `seal_part.py` 从素材重算、逐像素比对，封的是重算版 | `test_candidate_changed_after_submit_is_refused` |
| 先改素材再裁 | 素材只收 `/opt/dsh/whaledraw-sources/` 里 root 的档 | `test_sources_only_from_the_maintainers_folder` |
| 把不该挖的挖掉 | 审阅总览图把挖掉的区域标红并写编号 | 人工；真实样本的总览图实测过 |
| 不等封印直接贴候选 | 自检：不是零件、又超过画面 10% 的图片层算问题 | `test_large_loose_image_layer_is_a_problem` |
| 重复申请、催 | 同名一份待审、最多 3 份、送出锁住；封印后名字不能再用 | `test_no_resubmit…`、`test_reject_is_logged…`、`test_review_seal…` |
| 自己跑 seal_part.py | 不装进安装目录；非 root 拒绝 | `test_seal_tool_refuses_non_root`、`test_seal_tool_is_not_in_the_package` |
| 刻意 monkeypatch | 挡不住，靠人工审 | — |

## 1. 素材来源（2026-11-02 只读确认）

- 她能读的设定图只有一张：`/srv/dsh/discord/whale_sheet.png`（1448×1086，dsh 600，sha256 前 16 码 `539548dbdf78a648`，跟本机共享样本同一张）。
- 图上有的：
  - 全身三视图：正面站姿（已封印成 `whale_front`）、**侧面站姿（朝右）**、**背面站姿**；
  - 特写格（带圆角灰框）：脸部半身、呆毛、耳朵加蝴蝶结、眼睛、胸前领结、围裙、袖口、鞋子。
- **没有**坐姿、躺姿、其他表情或动作。要画「躺着晒太阳」这类姿势，目前没有素材，要另外取得设定图（谁来提供、版权，由老板决定）。
- 她目录里其他的 `whale_*.png`／`.gif` 是她以前做的图或下载的图，来源没有逐一确认，**不当素材**。
- 素材放哪：建议维护员把核可过的原图放进 root 所有、她只能读的 `/opt/dsh/whaledraw-sources/`（档名＋sha256 清单）。她只能从这里挑。新增素材也走维护员。
  - 理由：如果任何图都能当来源，她可以先自己补色、补洞再拿来「裁」，审阅时很难看出来。
  - 要更宽松也可以：允许任何来源，但报告里记 sha256，由维护员审阅时把关。

## 2. 她这一端的工具（放进 whaledraw：`python3 -m whaledraw.candidate`，以 dsh 身分可跑）

1. `prepare --source <素材> --crop x0 y0 x1 y1 --name <姿势名>`
   - 只接受第 1 节来源目录里的档（或照宽松方案）。
   - 裁图后用现有 `cut_out` 抠图（连通背景＋去白边），**先不挖任何空隙**。
   - 输出到 `/srv/dsh/discord/draw/candidates/<姿势名>/`：
     - `enclosed.png`：被围住的空隙编号图（沿用 `enclosed_sheet`）；
     - `preview.png`：深色、浅色两种底；
     - `prepare.json`：每个空隙的编号、范围、面积。
2. `finalize <姿势名> --remove 3,5,8 [--cut-edges bottom]`
   - 用**编号**指定要挖哪几块，不用座标，所以每次结果都一样。
   - 输出：
     - `candidate.png`；
     - `check-removed.png`：挖掉的区域标红，叠在原图上；
     - `check-edges.png`：沿用 `edge_sheet`，深浅两种底；
     - `report.json`：白边、残渣、洞数、裁切边、挖掉的编号、来源 sha256、裁切框、whaledraw 版本、candidate 的 sha256。
   - `--cut-edges bottom` 是给半身特写用的（故意从下面切掉），会写进报告，由维护员决定接不接受。
3. 候选零件 `load_part` 本来就不收：它只收 `/opt/dsh/whaledraw-*/parts/` 底下、root 所有的档。另外：
   - 候选档名固定叫 `candidate.png`，旁边的报告叫 `report.json`，不是同名的 `.json`；
   - 测试要确认：候选目录里的任何档，用 `load_part` 都会被拒。

## 3. 申请封印

- `python3 -m whaledraw.candidate submit <姿势名> --pose "侧面站姿" --note "<一句说明>"`
  - 写一份申请到 `/srv/dsh/discord/draw/requests/<时间>-<姿势名>.json`：候选路径、candidate 与来源的 sha256、裁切框、挖掉的编号、说明、版本。
  - 印出她要在群里说的那一句：「我做好了新姿势候选『侧面站姿』，等维护员封印」。
- 防重复：
  - 同一个姿势名已经有待审的申请，就拒绝；待审最多 3 份。
  - 送出后，候选目录在工具里标为「已申请」，不能再 `finalize`。要改，得先 `withdraw`，之后重新申请。
- 维护员怎么知道：
  - 她在群里那一句（老板看得到）；
  - 维护员 `sudo python3 seal_part.py requests` 列出所有待审申请。
- 维护员的决定写在 root 所有、她能读的 `/opt/dsh/whaledraw-parts/decisions.log`。root 不写进她的目录，免得碰到连结。

## 4. 维护员：审阅与封印

1. `sudo python3 -I seal_part.py review <申请>`
   - 用**安装版本自己的** whaledraw，从「来源原图＋裁切框＋编号」重新算一次零件，跟她交的 `candidate.png` 逐像素比对。不一样就停（代表候选被改过）。
   - 在 root 的审阅目录产出一张总览图：来源裁切、编号图、挖掉的区域（红）、深浅底的成品、边缘放大。另外印出洞数、白边、残渣、裁切边。
2. 维护员看图确认：
   - 挖掉的是背景，不是围裙、眼睛高光这类；
   - 没有白边、残渣；
   - 姿势名合理。
3. `sudo python3 -I seal_part.py seal <申请> --as <名字>.png`
   - 封印的是**重算出来的那一份**，不是她的档。会把裁切边、姿势名一起写进封印。
   - 写进 decisions.log。
   - 驳回用 `reject <申请> --reason "..."`，她能读到理由。

### 封印放哪：三个方案（请 Iris／老板选）

| 方案 | 做法 | 好处 | 代价 |
|---|---|---|---|
| A. 每加一个姿势出一个小版本 0.2.x | 整个 whaledraw 重装到新目录，所有零件重新封印，persona 路径改成新版本 | 跟现在的「新版本放新目录」规矩一致；回滚就是换回旧版本 | 每个姿势都要升 social 版本、跑 e2e、`--upgrade`；她用的路径一直变 |
| B. 零件单独做版本化目录 | `/opt/dsh/whaledraw-parts/<日期-序号>/` 放全部零件（旧的复制过来，再加新的），root 的 `current` 连结一次切换；旧目录保留 | 加姿势不用动程式和 persona（路径固定 `/opt/dsh/whaledraw-parts/current/…`）；切换和回滚都是换连结；`INDEX.md` 列出有哪些姿势 | 要改一次程式（`load_part` 的信任路径改成这里，出 0.2.1，要 Rin 审一次）；要多一个目录规矩 |
| C. 直接加进现有 0.2.0/parts | 封印到原本的目录 | 最简单 | 改动一个已发行的只读目录；没有原子切换；回滚只能删档；版本跟内容对不上 |

- 我的看法（供参考，不替你选）：姿势会陆续增加的话，B 的长期成本最低；只加一两个就不再加的话，A 不用改程式。C 不建议。
- B 的步骤：
  1. 出 0.2.1：信任路径改成 `/opt/dsh/whaledraw-parts/*/`，`current` 用 realpath 解开后逐段比对；
  2. 建 `/opt/dsh/whaledraw-parts/20261102-1/`，把 whale_front 重新封印进去；
  3. 切 `current`；
  4. persona 改指 `current`（只改这一次）；
  5. 之后每个新姿势：新日期目录、复制旧零件、封印新的、切 `current`。

## 5. 一定要 Rin 审的

- 候选工具：
  - 只读来源目录；
  - 写入只在 `candidates/<名字>/`（姿势名限制字元、路径规范化、不跟随连结）；
  - 不会写到 `parts`；
  - 送出申请的防重复逻辑。
- 候选区：任何档都过不了 `load_part`（测试要涵盖：复制、连结、改名成同名 `.json`）。
- 检查图够不够让维护员发现抠图错误：
  - 挖错的区域要显眼；
  - 白边、残渣在深浅两种底都看得到；
  - 半身特写的裁切边要标出来。
- `seal_part.py review/seal`：
  - 重算逐像素比对；
  - 只封重算出来的那一份；
  - root 不写她的目录；
  - decisions.log 的权限。
- 方案 B 的话，`load_part` 新信任路径与 `current` 连结的检查（连结本身 root 所有、目标在同一层）。

## 6. 她可能当成捷径的地方，与对策

| 捷径 | 对策 |
|---|---|
| 把候选直接放进 parts | parts 是 root 755／644，她写不进去 |
| 自己写或改封印 sidecar | `load_part` 只认信任目录，她目录里的 sidecar 一律不认；洞数还要跟图上重新量到的一致 |
| 把候选复制或连结到 `load_part` 读得到的路径 | 没写入权；连结用 realpath 解开后还是在她的目录，拒绝 |
| 交了申请之后再改 candidate.png 的像素 | 封印时从来源重算并逐像素比对，不一样就拒；封的是重算版 |
| 先在来源图上补色、补洞，再拿来裁 | 只收来源目录（root 只读）里的原图；宽松方案下维护员看总览图里的来源 |
| 把围裙、眼睛高光一起挖掉，让洞数「对」 | 审阅总览图把挖掉的区域标红，维护员要逐块看 |
| 不等封印，直接把候选当一般 PIL 图贴进画面当人物 | `self_check` 已经会把「人物不是 load_part 的零件」当问题；再加一条：面积超过画面某个比例、又不是零件的图片层，也算问题 |
| 一直重送申请、在群里催 | 同名待审只能一份；persona 写清楚送一次、说一句就好 |
| 自己执行 seal_part.py | 不在她的路径里，而且非 root 会拒绝（已经做了） |
| 刻意 monkeypatch whaledraw、不用 publish | 挡不住（DEPLOY.md「残余风险」）；靠维护员看成品和 check.json |

## 7. 人设（只加一小段）

> 想要新姿勢的零件：從 `/opt/dsh/whaledraw-sources/` 選素材，用 `python3 -m whaledraw.candidate` 裁圖、看編號圖決定挖哪幾塊，做好後 `submit` 一次，然後在群裡說一句「我做好了新姿勢候選『…』，等維護員封印」。封印前照常用現有的零件畫；不要重複申請、不要催、不要自己封印或改檔。

## 8. 工作量与先后

- 工作量（估计，含测试、不含审阅来回）：
  - 候选工具：约 1 天；
  - `seal_part.py` 的 review、seal、reject、requests：约 0.5 天；
  - 方案 B 的程式改动、persona、部署：约 0.5 天；方案 A 每个姿势约 0.5 天部署；
  - Rin 审阅通常 1–2 轮。
- 素材：现在能做的新姿势只有侧面和背面（另外还有半身特写）。坐、躺等姿势要先有新的设定图。
- 先后建议（请决定）：
  1. Krita 阶段 1 发现的两个 unit 修正，可以先送 Rin 审（很小），和这件事平行；
  2. 新姿势这条线只动 whaledraw，不碰 Krita，老板确认之后可以接着做；
  3. Krita 阶段 2 等老板批准；
  4. 批量工具、作品交接，我手上没有它们的具体需求，等你给范围再排。
