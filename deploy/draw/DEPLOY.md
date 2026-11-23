# whaledraw 部署计划（尚未部署；要老板点头才做）

目标：让鲸鱼娘在 VM 102 上用 whaledraw 画图。她的权限不变：不给 sudo，不改 `NoNewPrivileges`，不放宽防火墙、`IPAddressDeny` 或 `ProtectSystem`。所有东西都由维护帐号一次性装进 root 所有、她只能读的目录。

## 放哪、谁装

| 东西 | 位置 | 谁装 | 她的权限 |
|---|---|---|---|
| whaledraw 程式（`whaledraw/`、`halloween.py`）与测试（`test_whaledraw.py`） | `/opt/dsh/whaledraw-0.1.0/` | 维护帐号（`dsh-admin`，sudo），用 `install_whaledraw.sh` | 只读（root:root，目录 755、档案 644） |
| Pillow 13.0.0、numpy 2.5.4、scipy 1.18.1 | `/opt/dsh/whaledraw-0.1.0/lib/` | 同上：脚本照 `wheels-vm.txt` 下载 wheel、核对 sha256、解压。VM 系统 python 没有 pip，所以不用 pip | 只读 |
| 中文字型 | `/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc` | 已经装好（系统套件 `fonts-noto-cjk`，2026-10-29 装的，SIL OFL 授权） | 只读；`scene.find_font()` 会先找这个路径 |
| 她的工作档（抠好的人物、每张图的图层、检查图） | `/srv/dsh/discord/draw/` | 她自己写 | 可写（原本的工作目录） |

- 不用她自己 `pylibs` 里的 Pillow：那份她可以改，出图用的依赖要固定。
- numpy、scipy 的 manylinux wheel 自带需要的数学库，不需要装系统套件。
- 体积：wheel 约 59 MB，装好约 200 MB；VM 磁盘剩 66 GB。

## 步骤

以下 `$V` 是 `/opt/dsh/whaledraw-0.1.0`。指令都在 VM 102 上以维护帐号 `dsh-admin` 执行。

1. PVE 快照（在 pve2 上）：`qm snapshot 102 before-whaledraw`。
2. 把 repo 的 `deploy/draw/` 整个资料夹传到维护帐号家目录，例如 `~/stage-whaledraw/`。要包含 `whaledraw/`、`halloween.py`、`test_whaledraw.py`、`wheels-vm.txt`、`install_whaledraw.sh`。
3. 安装（新版本一律放新目录，不覆盖旧的）：

   ```sh
   sudo sh ~/stage-whaledraw/install_whaledraw.sh ~/stage-whaledraw 0.1.0
   ```

   脚本会先建 `$V.part`，下载三个 wheel、逐一核对 sha256（对不上就中止），解压到 `lib/`，复制程式与测试档，chown root:root、chmod 755/644，最后才改名成 `$V`。

4. 以 dsh 身分验证（不经过 Discord）。测试档就在 `$V/test_whaledraw.py`，要在 `$V` 里执行：

   ```sh
   V=/opt/dsh/whaledraw-0.1.0
   sudo -u dsh env -C $V PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$V/lib:$V \
     WHALEDRAW_SAMPLES=/srv/dsh/discord python3 -B -m unittest -v test_whaledraw
   ```

   预期结果是 `Ran 20 tests … OK`。如果 `/srv/dsh/discord/whale_sheet.png` 不在，会是 `OK (skipped=1)`，跳过的那条标「【未跑】真实样本测试」。

   再用她的设定图完整画一次，输出放进她的工作目录：

   ```sh
   sudo -u dsh env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$V/lib:$V \
     python3 -B $V/halloween.py /srv/dsh/discord/whale_sheet.png /srv/dsh/discord/draw/verify-0.1.0
   ```

   要印出 `"ok": true`，并产生 `halloween.png` 与 `check/` 底下的检查图。

5. 群聊 persona（`deploy/social/persona-group.md`）加一小段，写这几件事：
   - 画图用 whaledraw，一层一个函数，每次从头合成，不要在成品上改像素。
   - 人物先用 `cut_out` 抠一次存起来，之后重复用。
   - 出图一律用 `publish`。报 `CheckFailed` 就看 `.REJECTED.png` 和 `check/` 修，不要发。
   - 重叠检查偏保守：只要有一点看得见的覆盖就算压到，贴得很近的物件（间距 5 像素以内）也可能被拒，要拉开 6 像素以上。
   
   要升 presets 版本，照 e2e → `configure_social.py --upgrade` 的流程走。
6. 量用量（第 7 项）：部署后请老板在群里叫她画一张。用 session 日志对上这张图的回合时间，再用 `usage/calls.jsonl` 算回合数、模型调用数、token，和基线比（175 次调用、新输入约 45 万、快取约 1225 万 token、约 1.3–1.5 元）。

## 已知的保守行为

- 重叠检查用「透明度大于 0」当作看得见。缩小时的抗锯齿会让形状边缘多出约 2–3 像素淡淡的覆盖，所以两个物件间距 4–5 像素时，会因为几个透明度 1 的像素被判成重叠；间距 6 像素以上就不会。实测没有远距离误报。
- 这是故意偏保守：宁可要她拉开一点，也不放过真的压到人物的东西。

## 回滚

- `sudo rm -rf /opt/dsh/whaledraw-0.1.0`，并还原 persona 那一段（presets 退回上一版）；或直接回 PVE 快照 `before-whaledraw`。
- 她 `/srv/dsh/discord/` 里原来的东西都不会被动到；验证输出只放在 `/srv/dsh/discord/draw/verify-0.1.0`。

## 0.2.0（social.54）：封印零件、API 速查、道具库（2026-11-01 已部署，老板经 Iris 批准）

部署纪录（2026-11-01，UTC 07:51–08:00）：快照 `before-social54-whaledraw020-20261101`；`install_whaledraw.sh` 装到 `/opt/dsh/whaledraw-0.2.0`（0.1.0 保留）；root 用 `seal_part.py` 封印 `whale_front.png`（预期洞数 3741），parts 目录与档案都是 root 755/644，dsh 写不进；以 dsh 身分验证：测试 37 项 `OK (skipped=1)`、她自己那份被拒、halloween `"ok": true`、API 例子 `OK []`；e2e 19 项 OK；群里安静 37 分钟后 `--upgrade`（设定备份 `/var/backups/dsh/before-social-20261101T075438Z`）；升级后 active、NRestarts=0、gateway ready、无 error，离线对话测试回话正常。

改了什么：
- `whaledraw.parts`：`load_part()` 读零件和它的封印 json（sha256、预期洞数）。只认 `/opt/dsh/whaledraw-*/parts/` 里的零件（路径规范化后逐段比对），而且 png、json、parts 目录、版本目录、`/opt/dsh` 每一层都要 root 所有、群组与其他人不能写、不是连结。预期洞数要是 0 到图面积 10% 的整数，而且要等于从图上重新量到的数字（json 被改过就对不上）。png 被改过、没有封印、json 坏掉或型别不对，一律报 `PartError`，讯息里写「请找维护员」。
- 封印工具搬出套件：`deploy/draw/seal_part.py`，只能以 root 执行，**不装进安装目录**（不在她的 PYTHONPATH 里）。`whaledraw.parts` 里已经没有 `seal` / `save_part`。
- `scene.image()`：只收 `load_part()` 读的 `Part`（`isinstance` 加上 load_part 的记号，再重读一次比对图和预期洞数）或一般 PIL 图；鸭子型别物件、自己做的 `Part`、读进来后被改过的零件都会报错。传 `expected_holes=` 直接报错。要手动改只能写 `holes_override=(数字, "理由")`，会记进 check.json 的 `overrides`，而且自检会当成问题、不出图。人物不是零件，也算问题。
- 规则 fail closed：`forbid_overlap` / `forbid_overlap_within` 的任一边一层都没对到（例如 `candy-*` 对不上 `candy`）、`forbid_overlap_within` 只写一个名字、或一边对到超过 64 层，都算自检问题，记在 check.json 的 `unchecked_rules`，`render()` 和 `publish()` 都不出图。
- 重叠检查先比外框，碰得到才在交集那一小块比像素（结果跟整张比一样，测试有逐一比对）。
- `halloween.py`：`halloween.py cut <设定图> <png>` 只抠图；`halloween.py <封印过的零件> <输出资料夹>` 才画图。
- `whaledraw.props`：南瓜、糖果、蝙蝠、枯树（会自然收尖）、墓碑。
- `forbid_overlap` 可以用 `*`；新增 `forbid_overlap_within`（同一类的东西彼此不准叠）。
- `API.md`：速查加一个最小例子，装在 `$V/API.md`；测试会实际执行这个例子。
- persona：先看 API.md；用 `load_part`，不要改 expected_holes；不要自己封印、改 json、自己做 Part、改规则名字让自检过；自检没过又看不懂就停下来，回报「需要维护员」；会靠近的物件一个一层；树干不要用方块画。

步骤（新版本放新目录，0.1.0 留着当回滚）：

1. 拍 PVE 快照 `before-social54-whaledraw020-<日期>`。
2. 把 `deploy/draw/` 传到 `~/stage-whaledraw-020/`，然后执行 `sudo sh ~/stage-whaledraw-020/install_whaledraw.sh ~/stage-whaledraw-020 0.2.0`。先确认 `/opt/dsh` 是 root 所有、群组与其他人不能写（`stat -c '%U %G %a' /opt/dsh`）；不是的话 load_part 会全部拒绝，先停下回报。
3. 封印她的立绘，放进只读目录。用的是 0.1.0 验收过的那一份，也就是 `/srv/dsh/discord/draw/parts/whale_front.png`。封印工具从 stage 资料夹执行，不复制到 `$V`：

   ```sh
   V=/opt/dsh/whaledraw-0.2.0
   sudo python3 -I -B ~/stage-whaledraw-020/seal_part.py $V /srv/dsh/discord/draw/parts/whale_front.png
   ```

   它会建 `$V/parts`（root:root 755），复制 png、量洞数、写封印 json（两个都是 root:root 644），最后用 0.2.0 的 `load_part()` 读一次，印出 `{"sealed": …, "expected_hole_pixels": …}`。同名零件已经存在就拒绝。

4. 以 dsh 身分验证：照 0.1.0 的第 4 步跑测试（预期 `OK (skipped=1)`，跳过的是「seal_part.py 不在这里」，安装目录本来就不放它），再画一次图（参数改成封印过的零件）：

   ```sh
   sudo -u dsh env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$V/lib:$V \
     python3 -B $V/halloween.py $V/parts/whale_front.png /srv/dsh/discord/draw/verify-0.2.0
   ```

   要印出 `"ok": true`。再跑一次 API.md 的例子（要印出 `OK []`）。
5. persona 改好、版本升到 social.54，跑 e2e 后再 `--upgrade`，选群里安静的时候做。
6. 回滚：presets 退回 social.53（persona 指回 0.1.0 的路径），或回快照。`/opt/dsh/whaledraw-0.1.0` 一直保留。

### 残余风险（挡不住，要靠人看）

- 共用锁 `/opt/dsh/whaledraw-parts/requests.lock` 她能打开、能 flock，这是设计上必要的。所以她如果能执行任意程式，就可以一直占着锁，让维护员的 seal、reject 拿不到锁（维护被拒绝服务）。威胁模型是「她走捷径」，不防有意攻击，所以这一条不挡。发现锁一直被占着时：
  1. 看是谁拿着锁：`sudo lslocks -o PID,COMMAND,PATH | grep requests.lock`；或 `sudo fuser -v /opt/dsh/whaledraw-parts/requests.lock`。
  2. 确认那个程序是 dsh 的（`ps -o user,pid,etime,cmd -p <PID>`），而且不是一个正常、马上会做完的 candidate 指令。
  3. 停掉那个程序：`sudo kill <PID>`，必要时 `sudo kill -9 <PID>`。不用重启 dsh 服务；只有它一再占锁时，才回报并考虑停掉 dsh。
  4. 再跑一次 `seal_part.py $V recover` 确认状态一致，然后照常 seal 或 reject。

威胁模型是「她走捷径」，不是蓄意对手。上面的检查让捷径会报错、会留在 check.json，但挡不住有意绕过：

- 她的工作目录 `/srv/dsh/discord/draw/` 可写，`draw.py` 是她自己写的 Python：她可以 monkeypatch `whaledraw`（例如改 `parts.TRUSTED_PARTS`、仿造 `Part` 的内部记号）、不用 `publish` 自己存图、或直接用 Pillow 画。
- `check.json`、`check/` 底下的检查图、`.REJECTED.png` 都写在她可写的目录，事后可以被改或伪造。
- 封印只保证「零件是维护员放的那一张」，不保证成品好看。

所以真正的把关是维护员：看成品，并对照 `check.json`（`problems`、`overrides`、`unchecked_rules` 要是空的或没有；`character.expected_from` 要是 `seal`；`character.part` 要在 `/opt/dsh/whaledraw-0.2.0/parts/`）。看到她在 `draw.py` 里动 `whaledraw` 的内部，或反复封印、改 json 的纪录，要回报。

本机与容器的验证（2026-11-01，Rin 意见修正后）：

- ubuntu:24.04 容器照上面第 2–4 步做：`install_whaledraw.sh` 装 0.2.0；以 root 从 stage 执行 `seal_part.py` 封印设定图抠出的立绘（预期洞数 3741，原尺寸），parts 目录与档案 root 755/644；同名再封一次被拒；以 dsh 执行 `seal_part.py` 被拒（「只能由维护帐号以 root 执行」）；dsh 写不进 `parts/`；dsh 用 `load_part` 读她自己目录那张被拒（「不在 /opt/dsh/whaledraw-*/parts/ 里」）；以 dsh 跑 `halloween.py $V/parts/whale_front.png …` 印出 `"ok": true`（缩放后预期洞数 3270，实际 3125）；API.md 例子印出 `OK []`。
- `test_whaledraw.py` 37 项：以 root 从 stage 跑 `OK (skipped=1)`（跳过「非 root 执行封印工具」）；以 dsh 跑安装目录那份 `OK (skipped=1)`（跳过「seal_part.py 不在这里」）；以 dsh 从 stage 跑封印工具那两项 OK。真实样本测试有跑。

## 0.2.1（social.55）：新姿势候选流程、零件目录改版（2026-11-02 已部署：Rin 第 4 次复审通过，老板经 Iris 批准）

部署纪录（2026-11-02 UTC 07:32–07:40）：
- 快照 `before-social55-whaledraw021-20261102`。
- 装好 0.2.1，0.2.0 保留。
- 素材目录放入 whale_sheet.png（sha 前 16 码 539548dbdf78a648）。
- import 0.2.0 的正面立绘，建第一组 20261102-1，current 指过去，INDEX.md 也建了。requests.lock 是 root root 644，.seal.lock 是 root 600，decisions.log 是 root 644；recover 回报日志完好、没有需要修复的。
- dsh 删锁档、建档、改 current、写进零件组、删 decisions.log，全部被拒。
- 以 dsh 验证：
  - 安装目录的测试 68 项 `OK (skipped=15)`；
  - load_part 读正面立绘 OK（front，3741）；
  - halloween `"ok": true`；API 例子 `OK []`；
  - 候选流程（只检查，没有封印）：prepare、finalize、submit 成功，重交被拒，load_part 读候选回 PartError，withdraw 后清掉。
- e2e 19 项 OK。群里安静 35 分钟后 `--upgrade`（设定备份 `/var/backups/dsh/before-social-20261102T073551Z`）。
- 升级后：active、NRestarts=0、gateway ready、5 分钟内 0 个 error；离线回话正常；以 dsh 用正面立绘画一张，自检 `OK []`、expected_from=seal。

老板 11/2 定的流程是：她挑素材、裁图、选空隙，维护员封印。零件放哪选了方案 B（见 `POSES-PLAN.md`）。

改了什么：
- 零件目录：改成 `/opt/dsh/whaledraw-parts/<日期-序号>/`，root 的 `current` 连结指到其中一组。
  - `load_part` 只认这里：路径规范化后，必须在某一组目录里。
  - 经过的连结（current）要是 root 的，而且只能指到同一层的一组目录。
  - png、json、那一组目录、零件总目录、`/opt/dsh` 每一层都要 root 所有、群组与其他人不能写。
  - 0.2.0 的 `/opt/dsh/whaledraw-*/parts` 不再认。
- 封印格式 v2：多了 `pose` 和 `cut_edges_ok`（故意裁掉的边，例如半身特写的 bottom）。
  - 自检只把没写明的那几边的直线断面当问题。
  - 顺带修好一个旧问题：以前裁切边是在补了透明边之后才量，永远量到 0。现在在贴上去之前量。
- `whaledraw.candidate`（她用，在安装目录里）：sources / prepare / finalize / submit / withdraw / status。
  - 素材只收 `/opt/dsh/whaledraw-sources/` 里 root 的原图。
  - 空隙用编号挖，可以给阴影框。
  - 有问题的候选不能交；同名只能一份待审，总共最多 3 份；送出后锁住。
  - 退回会自动解锁；封印过的名字不能再用。
- `seal_part.py`（维护员用，不装进安装目录）：requests / review / seal / reject / import。
  - 一律从素材＋裁切框＋阴影框＋编号重算，跟她的 candidate.png 逐像素比对。封的是重算版。
  - 每次封印都建新的一组（旧零件逐一验证后复制过去），再原子地切换 current。
  - 预先放好的连结不会被跟着走；她目录里的申请档用 `O_NOFOLLOW` 读。
  - 决定写在 root 的 `decisions.log`，她读得到。
- 自检：
  - 不是零件、又占画面超过 10% 的图片层算问题（挡「直接把候选贴进画里」）。
  - `scene.image` 也接受 `part=`（她 11/2 那样写过）。
- API.md、persona：路径改成 0.2.1 与 `whaledraw-parts/current`，加一小段新姿势流程。

步骤（照之前的规矩：新版本放新目录，0.2.0 留着）：

1. 拍 PVE 快照 `before-social55-whaledraw021-<日期>`。
2. 把 `deploy/draw/` 传到 `~/stage-whaledraw-021/`，执行：

   ```sh
   sudo sh ~/stage-whaledraw-021/install_whaledraw.sh ~/stage-whaledraw-021 0.2.1
   ```

3. 素材目录：

   ```sh
   sudo install -d -o root -g root -m 755 /opt/dsh/whaledraw-sources
   sudo install -o root -g root -m 644 /srv/dsh/discord/whale_sheet.png /opt/dsh/whaledraw-sources/whale_sheet.png
   ```

   复制的是她现在那张设定图，sha256 前 16 码 `539548dbdf78a648`，装好后核对。
4. 把 0.2.0 的正面立绘搬进新的零件目录（会建第一组并设 current，也会建立共用锁 `requests.lock`；之后用 `stat -c '%U %G %a' /opt/dsh/whaledraw-parts/requests.lock` 确认是 `root root 644`）：

   ```sh
   sudo python3 -I -B ~/stage-whaledraw-021/seal_part.py /opt/dsh/whaledraw-0.2.1 import /opt/dsh/whaledraw-0.2.0/parts/whale_front.png --as whale_front.png --pose front
   ```

   第一次 import 中途当掉（意图档 `.pending-commit.json` 里 `previous` 是 `null`、而且没有 current）：recover 会停下，要人照下面处理。
   1. 看意图档记的新组 `S`（`/opt/dsh/whaledraw-parts/S`）。
   2. 完整（目录在；`whale_front.png`、`whale_front.json`、`INDEX.md` 都在；用 `sudo env PYTHONPATH=$V/lib:$V python3 -B -c 'from whaledraw.parts import load_part; load_part("/opt/dsh/whaledraw-parts/S/whale_front.png")'` 读得进来）：
      ```sh
      sudo ln -sfn S /opt/dsh/whaledraw-parts/current
      ```
      然后在 `decisions.log` 用意图档里的 request 补记一行：`{"request": "<意图的 request>", "decision": "imported", "set": "S", "file": "whale_front.png", "at": "<现在>", "recovered": true}`（要以换行结尾）。最后删掉意图档。
   3. 半成品（缺档或读不进来）：确认 current 不存在、`decisions.log` 里没有这个 request，再 `sudo rm -rf /opt/dsh/whaledraw-parts/S` 和意图档，然后重跑上面那行 import。
   4. 处理完都要跑一次 `sudo python3 -I -B ~/stage-whaledraw-021/seal_part.py $V recover`，确认回报「没有需要修复的、日志完好」。

5. 以 dsh 身分验证：
   - 测试：预期 `OK (skipped=6)`，跳过的是 seal_part.py 不在安装目录的那几项。
   - halloween：`$V/halloween.py /opt/dsh/whaledraw-parts/current/whale_front.png …` 要 `"ok": true`。
   - API.md 的例子要印出 `OK []`。
   - `python3 -B -m whaledraw.candidate sources` 要列出 whale_sheet.png。
6. persona 升到 social.55（0.2.1 路径＋新姿势那一段），先跑 e2e，群里安静 10 分钟以上才 `--upgrade`。
7. 回滚：presets 退回 social.54（persona 指回 0.2.0），或回快照。`/opt/dsh/whaledraw-0.2.0`、`/opt/dsh/whaledraw-parts` 都留着；零件回滚就是把 current 指回上一组。

日常（部署之后）：她交了申请，维护员：

```sh
V=/opt/dsh/whaledraw-0.2.1
sudo python3 -I -B ~/stage-whaledraw-021/seal_part.py $V requests
sudo python3 -I -B ~/stage-whaledraw-021/seal_part.py $V review <申请编号>     # 看 /var/lib/whaledraw-review/<编号>/ 的三张图
sudo python3 -I -B ~/stage-whaledraw-021/seal_part.py $V seal <申请编号> --as whale_side.png
#   或 reject <申请编号> --reason "围裙被挖掉了"
```

审阅时看（**一定要看图**：自检只量得到明显不透明的问题，例如裁切边以透明度大于 128 计，很淡但看得见的断面会漏掉，所以审阅总览图是必要的关卡，不能只看 review 印出来的数字）：
- 红色的挖掉区域都是背景（头发之间、两腿之间），不是围裙、蕾丝、眼睛高光；
- 深浅两种底上都没有白边、残渣；
- 裁切边只有申请里写明的那几边；
- 姿势名合理。

并发与中断（Rin 第 1 轮意见修正）：
- 共用锁（Rin 第 2 轮修正）：`/opt/dsh/whaledraw-parts/requests.lock`，root 644，放在 root 755 的零件总目录里。她只能打开来 flock，不能删除或换掉。由维护员第一次执行 seal_part.py（例如 import）时建立；还没建立时，她的工具会拒绝。双方每次都核对：拥有者、权限、一般档、不是连结、所在目录；拿到锁后、重要写入前再确认路径上还是同一个 inode。不对就停下并警告。
- 她这一端：prepare、finalize、submit、withdraw、status 都在这把共用锁里做；拿不到最多等 15 秒。
  - submit 的顺序：先写暂存档，再写候选的锁标记，最后把暂存档改名成正式申请（原子）。失败会当场清掉。
  - 中途被杀：下次任何动作都会清掉暂存档，以及指到不存在申请的锁标记。
  - 孤立的申请（申请在、候选却没有指到它）：status 会列成 `orphan request`，她用 withdraw、或维护员用 reject 清掉。
- 维护员这一端：seal、reject、import、recover 要先拿 `/opt/dsh/whaledraw-parts/.seal.lock`（root 600），拿不到就拒绝；再拿同一把共用锁，并在锁里重读申请。拿锁的顺序固定（先 seal.lock 再共用锁，她只拿共用锁），不会互相卡死。
  - seal 照「意图 → 建组 → 切换 current → 记决定 → 删意图」的顺序做。
  - 每个会改东西的指令开头都先 recover。先严格检查意图档：只有 set、request、decision、file、previous 五个栏位，型别对，组名与档名格式对，组目录落在零件总目录里；不对就什么都不动，停下要人处理。之后只有两种情况会自动处理：
    - current 指到意图里的新组，而且这一组逐件验证过（目录在、每个零件都通过 load_part、原本那一组的零件都在、INDEX.md 在），就补记决定；
    - current 指到意图里记的原本那一组（也逐件验证过），就删掉做到一半的新组。
  - 其他所有情况（current 不存在、指到第三个组、指到的组不完整）什么都不动，保留意图档，停下要人处理。第一次封印（原本没有 current）中途当掉也属于这一类。处理方法见上面部署第 4 步的「第一次 import 中途当掉」：完整的新组就设 current 并补记决定；半成品就安全清理后重跑 import。
  - 人处理的方法：看 `.pending-commit.json` 记的新组和原本那一组，各自用 load_part 验过，把 current 指到完整的那一组；需要的话补记或不记决定，最后删掉意图档，再跑一次 `recover`，确认回报「没有需要修复的」。
  - decisions.log 的坏尾巴备份（`decisions.log.corrupt-*`）最多 10 份、总共 50 MB。超过就不再自动修复，也不会删旧备份，而是停下警告。维护员看过后决定保留哪些，并查一下为什么一直写到一半（磁碟满？一再被杀？）。
  - decisions.log 严格读：她那边读到坏行就拒绝。维护员写之前检查：只有「最后一行写到一半」会自动修（整份原档备份成 `decisions.log.corrupt-<时间>`，再去掉那半行）；中间有坏行就停下。写完读回确认。
  - 也可以单独跑 `seal_part.py $V recover`。

本机与容器的验证（2026-11-02）：
- `test_whaledraw.py` 51 项：
  - root 跑 `OK (skipped=1)`；
  - dsh 跑 `OK (skipped=5)`：封印工具那几项要 root；dsh 执行 seal_part.py 被拒。
- 用真实设定图走完一遍：
  - root `import` 正面立绘，建第一组；
  - dsh `prepare` 侧面（`--crop 548 0 960 624 --shadow-box 0 585 412 624`，63 个编号），`finalize --remove 14,16,19`，`submit`；
  - root `review`：重算与候选一致，产出总览图；
  - root `seal --as whale_side.png`：建第二组、切换 current、旧的一组保留，INDEX.md 列出两个姿势；
  - dsh 用两个零件画一张：自检 `OK []`。
- 她的捷径都被挡：
  - 候选用 `load_part` 读：PartError；
  - 把候选复制进零件目录、改封印 json、改 current：Permission denied；
  - 封印后重交：被拒，提示换名字。
- Rin 第 1 轮修正后（2026-11-02）：`test_whaledraw.py` 59 项。
  - root 跑 `OK (skipped=1)`；dsh 跑 `OK (skipped=8)`（要 root 的封印工具测试）。
  - 新加的测试：两个并发 submit（第 3、4 份，以及同名两份）、两个并发 seal（第二个被拒、之后补封，三个零件都在）、submit 和 withdraw 中途当掉、seal 在「意图／建组之后／切换 current 之后／记决定之后」四个点当掉，recover 后状态一致。
  - 并发与封印的 13 项连跑 5 次都过。把 submit 的锁换成空的再跑，并发上限那项会失败，所以测试确实抓得到原本的问题。
- Rin 第 2 轮修正后（2026-11-02）：`test_whaledraw.py` 65 项。
  - root 跑 `OK (skipped=1)`；dsh 跑 `OK (skipped=11)`。
  - 新加的测试：共用锁不见、是连结、权限 666、拥有者不对都会拒绝；拿着锁时锁档被换掉，送出前会停下，也不留锁标记；她拿着锁时维护员 seal 被拒、维护员拿着锁时她 withdraw 被拒（确定性的交错）；recover 遇到悬空的 current、或零件被改过的组，什么都不动、保留意图档；decisions.log 最后一行写到一半：她那边拒绝，维护员 recover 隔离并备份后可以正常追加；中间有坏行就不修。
  - 并发与封印的 19 项连跑 5 次都过。
  - 真实设定图的命令行流程：root 建的 requests.lock 是 root 644；她用这把锁照常 prepare、finalize、submit；维护员 seal 成功；dsh 删除或覆写锁档都是 Permission denied；最后 recover 回报日志完好、没有需要修复的。
- Rin 第 3 轮修正后（2026-11-02）：`test_whaledraw.py` 68 项。
  - root 跑 `OK (skipped=1)`；dsh 跑 `OK (skipped=14)`。
  - 新加的测试：
    - current 不存在、或指到第三个组：意图档、新组、第三个组都不动，也不补记决定。指回原本那一组之后，才正常回滚。
    - 17 种坏意图都拒绝，什么都不动：缺栏位、多栏位、型别错、组名是 `../../srv` 或 `current`、档名带 `../`、decision 不对、request 空或含 `/`，另外还有非物件、非 JSON。
    - decisions.log 坏尾巴的备份超过上限：停下警告，旧备份一份都没删。
  - 封印与并发那 22 项连跑 3 次都过。
  - 真实设定图的命令行流程照常，最后 recover 回报「日志完好、没有需要修复的」。

## 自检新增「没设规则的重叠」提醒（2026-11-06，只在本机改，**尚未部署**：等 Rin 审、老板批准）

- 人物层（用 `load_part` 读的零件层，加上自检指定的 `character` 层）跟别的层重叠、却没有为这一对设规则时，`check.json` 的 `warnings` 会列出来，`check-overlap.png` 上用橙色标出；**只是提醒，不挡出图**。
- 重叠的定义跟 `forbid_overlap` 同一个函式（alpha > 0）。一对层符合下面任一条就不提醒：`forbid_overlap` 系的规则（展开后的名字比对，沿用同一套逻辑）管着这一对；用新的 `scene.allow_overlap(a, b, "理由")` 声明过；另一层是超过画面 10% 的大场景层。
- `allow_overlap` 只消提醒，不解除 `forbid_overlap`；理由一定要写；名字可用万用字元，两边不能都是；名字写错（一层都没对到）、或一边对到超过 64 层，不生效，并多一条提醒。
- `check.json` 新增的栏位：`warnings`（一律有，可能是空的）；有内容才出现：`unruled_overlaps`、`allowed_overlaps`（她声明的故意重叠和理由，审阅时看合不合理）、`skipped_large_layers`（没检查的大场景层）。原本的栏位与封印格式都没动。
- 审阅时多看：`allowed_overlaps` 的理由，和 `skipped_large_layers` 里有没有不该略过的层。
- `halloween.py` 对脚下的影子声明了 `allow_overlap`。
- 本机与容器验证：`test_whaledraw.py` 79 项（新增 11 项），root 跑 `OK (skipped=1)`，dsh 跑 `OK (skipped=14)`；11/2 的心情图脚本原样重跑（零件路径改成新路径），自检回 `OK []`，`warnings` 正好 1 条（bubble，2075 像素），大层 ground、rays、sky、sun 列在 `skipped_large_layers`。

### 0.2.2 的安装与回滚（2026-11-06 补；只写步骤，**尚未部署**：Rin 审过、老板批准才做）

0.2.2 = 0.2.1 + 「没设规则的重叠」提醒 + 入口成功时也印出提醒。零件（`/opt/dsh/whaledraw-parts`）、素材目录、`requests.lock`、封印格式都跟版本无关，**不用重做**。persona 里的执行路径带着版本号，所以上线要一起升 social（persona 改指 0.2.2 与新增「成功也要看提醒」那一句）。

安装（新版本放新目录，0.2.1 保留当回滚）：

1. 拍 PVE 快照 `before-social56-whaledraw022-<日期>`，`qm listsnapshot 102` 确认。
2. 建 `~/stage-whaledraw-022/`（stage 只是维护员帐号家目录里的暂放处，拥有者是维护员本人，不是 root，之后会删；真正装进去的是下一步 `install_whaledraw.sh` 做的事）：

   - 里面放什么：本机 repo 的 `deploy/draw/` 整个目录的内容（`whaledraw/`、`halloween.py`、`test_whaledraw.py`、`API.md`、`seal_part.py`、`install_whaledraw.sh`、`wheels-vm.txt`、`DEPLOY.md` 等），**不放** `__pycache__`、`*.pyc`、`test-container/`（容器测试用的，VM 不需要）。
   - 怎么建（在本机 repo 根目录。打包用 repo 里的 `deploy/dsh/pack_stage.py`：每次打出来的 tar 完全一样，并附一份 `MANIFEST.sha256`（每个档案的 sha256）；有单元测试 `test_pack_stage.py`。`$SSH_VM` 代表维护员平常连 VM 的 ssh 指令，含跳板与金钥，只在维护员自己的环境里，不写进档案）：

     ```sh
     # 本机：打包。会印出档案数、tar 的 sha256、MANIFEST 的 sha256——把 MANIFEST 那一行留着，下面要对。
     # 打包失败（拒绝重复路径、符号链接、奇怪的档名等）就不会上传：两条用 && 接起来，前一条非 0 后一条不执行
     python deploy/dsh/pack_stage.py stage-whaledraw-022.tar --add .=deploy/draw --exclude test-container \
       && $SSH_VM 'rm -rf ~/stage-whaledraw-022 && mkdir ~/stage-whaledraw-022 && tar -xf - -C ~/stage-whaledraw-022 --no-same-owner' < stage-whaledraw-022.tar
     ```

     在 VM 上核对。**任何一步不过，整段就以非 0 结束，后面的步骤不执行**（`bash -euo pipefail`；这段是整个指令的最后一件事，它的结束码就是指令的结束码，后面不要再接别的指令）。把 `<MANIFEST_SHA256>` 换成本机打包时印出的 "MANIFEST sha256"（64 位小写十六进位）：

     ```sh
     bash -euo pipefail -s -- <MANIFEST_SHA256> <<'EOF'
     expect=${1-}
     case $expect in *[!0-9a-f]*|"") echo "第一个参数要是本机印出的 MANIFEST sha256（64 位小写十六进位）" >&2; exit 2;; esac
     if [ "${#expect}" -ne 64 ]; then echo "MANIFEST sha256 要 64 位" >&2; exit 2; fi
     cd ~/stage-whaledraw-022
     owner=$(stat -c %U .)
     me=$(id -un)
     if [ -z "$owner" ] || [ "$owner" != "$me" ]; then echo "stage 的拥有者不是维护员本人" >&2; exit 1; fi
     tmp=$(mktemp -d)
     trap 'rm -rf "$tmp"' EXIT
     # 不用进程替换（尖括号加括号那种写法）：它里面的失败 pipefail 管不到。每一步都写进暂存档，失败就由 set -e／pipefail 停下
     sha256sum -c --quiet MANIFEST.sha256
     find . ! -type d ! -name MANIFEST.sha256 > "$tmp/found"
     sed 's,^\./,,' "$tmp/found" | sort > "$tmp/found.sorted"
     awk '{print $2}' MANIFEST.sha256 | sort > "$tmp/listed.sorted"
     diff "$tmp/found.sorted" "$tmp/listed.sorted"
     actual=$(sha256sum MANIFEST.sha256)
     actual=${actual%% *}
     if [ "$actual" != "$expect" ]; then echo "MANIFEST 的 sha256 跟本机印出的不一样（清单被换过？）" >&2; exit 1; fi
     echo "stage 完整性核对全部通过"
     EOF
     ```

   - 然后装（`install_whaledraw.sh` 自己会：下载 wheel 并核对 sha256、复制到 `/opt/dsh/whaledraw-0.2.2.part`、`chown -R root:root`、目录 755、档案 644，再改名成 `/opt/dsh/whaledraw-0.2.2`；已存在就拒绝，不覆盖）：

   ```sh
   bash -eu <<'EOF'
   V=/opt/dsh/whaledraw-0.2.2
   sudo -n sh ~/stage-whaledraw-022/install_whaledraw.sh ~/stage-whaledraw-022 0.2.2
   # 用新版本的程式验一次零件目录：要回报「没有需要修复的、日志完好」，否则整段非 0
   out=$(sudo -n python3 -I -B ~/stage-whaledraw-022/seal_part.py $V recover)
   echo "$out"
   case $out in *'"recovered": "没有需要修复的"'*'"log": "完好"'*) ;; *) echo "recover 的回报不是「没有需要修复的、日志完好」" >&2; exit 1;; esac
   EOF
   ```

   之后几步用到的 `$V`，在你自己的 shell 里设：`V=/opt/dsh/whaledraw-0.2.2`。

3. 以 dsh 身分验证：
   - 测试：`sudo -u dsh env -C $V PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$V/lib:$V WHALEDRAW_SAMPLES=/srv/dsh/discord python3 -B -m unittest test_whaledraw`，要 `OK`（跳过的是要 root 或要 seal_part.py 的那几项）；
   - `sudo -u dsh env PYTHONPATH=$V/lib:$V python3 -B $V/halloween.py /opt/dsh/whaledraw-parts/current/whale_front.png /srv/dsh/discord/draw/verify-0.2.2`：要印出 `"ok": true`、`"warnings": []`，并有 `allowed_overlaps`（shadows）和 `skipped_large_layers`；
   - 跑一次 API.md 的例子：印出 `OK []`，再印一行 `allowed_overlaps … skipped_large_layers …`。
4. social.56（persona 改指 0.2.2、加「成功也要看提醒」）。版本已经在 repo 里定好：`discord-0.6.0-social.56`、`social-0.1.50`、`social-presets-0.53.0`（`configure_social.py` 的 `DEFAULT_BUNDLE`、`prepare_*.py`、`deploy/social/package.json` 一起改好了，`test_configure_social.py` 直接断言这个目标版本，并检查几处一致）。步骤：

   1. **建 `~/stage-social56/`**（维护员家目录里的暂放处，拥有者是维护员本人；之后要放进 `/opt` 的东西都是从这里复制过去并改成 root 所有，stage 本身不会被任何服务读取）。

      | stage 里的位置 | 来自本机 | 说明 |
      | --- | --- | --- |
      | `deploy/dsh/configure_social.py`、`prepare_social_materials.py`、`social-e2e.mjs`、`run_social_e2e.py` | repo `deploy/dsh/` 同名档案 | 脚本与 e2e |
      | `deploy/social/`（整个目录，不含 `node_modules`、`*.test.mjs`） | repo `deploy/social/` | social-0.1.50 与预设 0.53.0 的原始档，`prepare_social_materials.py` 从自己旁边的 `../social` 读它们 |
      | `inputs/fish` | `.cache/dsh-fish-memory` | 已审阅的鱼记忆套件，脚本会核对版本与入口 SHA256 |
      | `inputs/persona` | `.cache/dsh-whale-persona` | 角色设定 |
      | `inputs/opencc` | 已审阅的 opencc-js 1.4.2 套件目录 | 脚本核对版本与两个档案的 SHA256 |
      | `built/discord-0.6.0-social.56` | `.cache/dsh-social-bridge-56` | **在本机已产出**：`prepare_discord_plugin.py --social` 对已审阅的上游桥接原始码跑出来的结果（核对上游入口 SHA256 与唯一替换锚点，见 `deploy/dsh/README.md`）；这个不在 VM 上重做，直接搬 |

      `nekowhale-1.1.1`、`dsh-meme-0.1.64`、`huanqi-1.1.0` 这次**不用**放进 stage：它们已经在 `/opt/dsh/vendor/`（social.55 那次装好的，核对过），升级指令直接指过去。

      打包与上传（本机，在 repo 根目录；`$OPENCC_DIR` 是本机上已审阅的 opencc-js 1.4.2 套件目录，位置自己填；`pack_stage.py` 的说明见上面 whaledraw 那一步）：

      ```sh
      python deploy/dsh/pack_stage.py stage-social56.tar \
        --add deploy/dsh/configure_social.py=deploy/dsh/configure_social.py \
        --add deploy/dsh/prepare_social_materials.py=deploy/dsh/prepare_social_materials.py \
        --add deploy/dsh/social-e2e.mjs=deploy/dsh/social-e2e.mjs \
        --add deploy/dsh/run_social_e2e.py=deploy/dsh/run_social_e2e.py \
        --add deploy/social=deploy/social \
        --add inputs/fish=.cache/dsh-fish-memory \
        --add inputs/persona=.cache/dsh-whale-persona \
        --add inputs/opencc="$OPENCC_DIR" \
        --add built/discord-0.6.0-social.56=.cache/dsh-social-bridge-56 \
        --exclude '*.test.mjs' \
        && $SSH_VM 'rm -rf ~/stage-social56 && mkdir ~/stage-social56 && tar -xf - -C ~/stage-social56 --no-same-owner' < stage-social56.tar
      # 打包印出 "MANIFEST sha256"：留着，下面要对。打包失败就不会上传（&&）
      ```

      在 VM 上核对。同样**任何一步不过，整段以非 0 结束，后面不执行**，而且它必须是这个指令的最后一件事。每个档案都由清单管着，清单又由本机印出的指纹管着，所以 `configure_social.py` 等脚本也在其中，不用另外比：

      ```sh
      bash -euo pipefail -s -- <MANIFEST_SHA256> <<'EOF'
      expect=${1-}
      case $expect in *[!0-9a-f]*|"") echo "第一个参数要是本机印出的 MANIFEST sha256（64 位小写十六进位）" >&2; exit 2;; esac
      if [ "${#expect}" -ne 64 ]; then echo "MANIFEST sha256 要 64 位" >&2; exit 2; fi
      cd ~/stage-social56
      owner=$(stat -c %U .)
      me=$(id -un)
      if [ -z "$owner" ] || [ "$owner" != "$me" ]; then echo "stage 的拥有者不是维护员本人" >&2; exit 1; fi
      tmp=$(mktemp -d)
      trap 'rm -rf "$tmp"' EXIT
      # 不用进程替换（尖括号加括号那种写法）：它里面的失败 pipefail 管不到。每一步都写进暂存档，失败就由 set -e／pipefail 停下
      sha256sum -c --quiet MANIFEST.sha256
      find . ! -type d ! -name MANIFEST.sha256 > "$tmp/found"
      sed 's,^\./,,' "$tmp/found" | sort > "$tmp/found.sorted"
      awk '{print $2}' MANIFEST.sha256 | sort > "$tmp/listed.sorted"
      diff "$tmp/found.sorted" "$tmp/listed.sorted"
      actual=$(sha256sum MANIFEST.sha256)
      actual=${actual%% *}
      if [ "$actual" != "$expect" ]; then echo "MANIFEST 的 sha256 跟本机印出的不一样（清单被换过？）" >&2; exit 1; fi
      count=$(grep -c '"version": "0.6.0-social.56"' built/discord-0.6.0-social.56/package.json)
      if [ "$count" != 1 ]; then echo "discord 套件的版本不是 0.6.0-social.56" >&2; exit 1; fi
      echo "stage 完整性核对全部通过"
      EOF
      ```

      （SSH 凭据与连线参数只在本机，不进 repo。打包用的是 repo 里这支固定的程序，所以「上传了什么」可以被别人用同一个指令在同一个 commit 上重做、比对 sha256。`pack_stage.py` 对输入也是严格的：同一个目的路径出现两次、一个路径同时当档案又当目录、符号链接、管线等特殊档案、路径里有空白／控制字元／反斜线，一律拒绝，不会静默只留最后一项。）

   2. **在 VM 上、用维护员帐号（不用 sudo）产出另两个套件**。`prepare_social_materials.py` 需要 python3 与 PyYAML（social.55 那次就是在 VM 上这样跑的），它只写 `built/`，不碰 `/opt`：

      ```sh
      bash -eu <<'EOF'
      cd ~/stage-social56
      python3 -B deploy/dsh/prepare_social_materials.py inputs/fish inputs/persona built inputs/opencc
      for d in discord-0.6.0-social.56 social-0.1.50 social-presets-0.53.0; do      # built 里要有这三个（还会有 fish-1.3.7，不装）
        [ -f "built/$d/package.json" ] || { echo "built/$d 没有产出" >&2; exit 1; }
      done
      [ "$(grep -c whaledraw-0.2.2 built/social-presets-0.53.0/cordis.patch.yml)" -ge 1 ] || { echo "persona 没有指到 0.2.2" >&2; exit 1; }
      if grep -q whaledraw-0.2.1 built/social-presets-0.53.0/cordis.patch.yml; then echo "persona 还指着 0.2.1" >&2; exit 1; fi
      echo "built 里的三个套件齐全，persona 指到 0.2.2"
      EOF
      ```

   3. **搬进 `/opt/dsh/vendor/`**（拥有者 root:root，目录 755、档案 644；新版本写新目录，不覆盖）。整段用 `sh -eu` 跑：**先预检，三个新目录在 stage 里都有、在 vendor 里都还不存在才开始复制；只要有一个不对，整段以非 0 结束，一个都不复制。** 复制、chown、chmod 任何一步失败也立刻中止，并印出「这次已经开始建立的目录」。

      ```sh
      sh -eu <<'EOF'
      V=/opt/dsh/vendor
      S="$HOME/stage-social56/built"
      NEW="discord-0.6.0-social.56 social-0.1.50 social-presets-0.53.0"
      started=""                           # 这次实际开始复制的目录；失败时印出来，清理只清这几个
      finish() {
        status=$?
        if [ "$status" -ne 0 ]; then
          echo "中止（结束码 $status）。这次已经开始建立的目录：${started:-（无，预检就停了，什么都没复制）}" >&2
          echo "开始时间：$begin。请照 DEPLOY.md 的「中止后的清理」处理，不要凭印象删。" >&2
        fi
      }
      trap finish EXIT
      begin=$(date '+%F %T')
      # 预检：全部通过才继续
      for d in $NEW; do
        [ -d "$S/$d" ] || { echo "stage 里没有 $d" >&2; exit 1; }
        { [ ! -e "$V/$d" ] && [ ! -L "$V/$d" ]; } || { echo "vendor 里已经有 $d（可能是别人放的，不覆盖，也不删）" >&2; exit 1; }
      done
      # 复制：每一步失败都会停（sh -e）
      for d in $NEW; do
        started="$started $d"              # 先记下再复制：复制到一半失败，也知道是哪个
        sudo -n cp -r "$S/$d" "$V/$d"
        sudo -n chown -R root:root "$V/$d"
        sudo -n find "$V/$d" -type d -exec chmod 755 {} +
        sudo -n find "$V/$d" -type f -exec chmod 644 {} +
        bad=$(sudo -n find "$V/$d" \( ! -user root -o ! -group root -o -perm /022 -o -type l \))      # 不接 | wc -l：find 失败时输出是空的，会被当成「没有问题」
        [ -z "$bad" ] || { echo "$d 的拥有者或权限不对" >&2; exit 1; }
        stat -c '%U:%G %a %n' "$V/$d" "$V/$d/package.json"
      done
      echo "三个目录都已复制并改好拥有者与权限"
      EOF
      ```

      这段 `sh -eu` 是整个指令的最后一件事，**它的结束码就是整个指令的结束码**（里面任何一步失败，外面一定是非 0）；所以后面不要再接 `echo "$?"` 之类的指令——那会把失败码换成 0。想看结束码就在下一个提示符单独执行 `echo $?`，要是 0 以外的数字就不要往下做（中止时里面已经印出了原因）。

      `cp -r` 复制出来的档案拥有者是维护员、权限看 umask，所以 `chown` 与 `chmod` 缺一不可；下面的 `--show-bundle` 会再核对一次（拥有者不是 root、群组或其他人可写、dsh 读不到、package.json 与目录名对不上，都会被拒绝）。

      **中止后的清理**（只有中止时才做；每一步都先看清楚再动）：

      1. 看中止信息里「这次已经开始建立的目录」和开始时间。印出「预检就停了，什么都没复制」，或「vendor 里已经有 …」：这次**没有建立任何东西**，什么都不要删；那个已存在的目录不是这次建的，**停下来，回报给 Sora／老板，查是谁放的**。
      2. 列出的每个目录，逐个确认是这次建的：`sudo stat -c '%U %w %y %n' /opt/dsh/vendor/<名字>`（`%w` 建立时间要晚于「开始时间」；预检保证了动手前它不存在）。时间对不上、或名字不在列表里：**不是这次建的，不删，停下来找人**。列表里的目录若根本不存在（复制一开始就失败），不用删。
      3. 确认之后，一个一个用完整的名字删，不用通配符、不用变量：`sudo rm -rf -- /opt/dsh/vendor/discord-0.6.0-social.56`（另两个同理）；删完 `ls /opt/dsh/vendor` 确认只剩原来的。
      4. 找出失败的原因（磁盘满？sudo 不能用？stage 不完整？）、修好，再从第 1 步重做。已经成功复制完的目录也一起删掉重做，不要留一半新一半旧。
   4. 把新版 `configure_social.py` 装进 `/opt/dsh/`，**旧版先留着**（旧版路径写死 social.55，万一要紧急退回它本身就是一条退路）：

      ```sh
      bash -eu <<'EOF'
      NEW=~/stage-social56/deploy/dsh/configure_social.py
      CUR=/opt/dsh/configure_social.py
      BAK=$CUR.social55
      # 备份已经存在就停：重跑一次会把「新脚本」当成 social.55 的备份覆盖掉
      [ ! -e "$BAK" ] || { echo "备份 $BAK 已经存在，不覆盖（可能已经装过一次；先查清楚）" >&2; exit 1; }
      # 现在装着的必须真的是 social.55 那一版（写死 social.55、还没有 --show-bundle），才值得备份
      [ "$(grep -c 'discord-0.6.0-social.55' "$CUR")" -ge 1 ] || { echo "现在装着的不像 social.55 那一版，停下" >&2; exit 1; }
      if grep -q -e '--show-bundle' "$CUR"; then echo "现在装着的已经有 --show-bundle，不备份" >&2; exit 1; fi
      sudo -n cp -p "$CUR" "$BAK"
      sha256sum "$BAK" > ~/configure_social.social55.sha256          # 记下备份的指纹，还原时用来核对
      sudo -n install -o root -g root -m 644 "$NEW" "$CUR"
      [ "$(stat -c '%U %G %a' "$CUR")" = "root root 644" ] || { echo "新脚本的拥有者或权限不是 root root 644" >&2; exit 1; }
      [ "$(stat -c '%U %G %a' "$BAK")" = "root root 644" ] || { echo "备份的拥有者或权限不是 root root 644" >&2; exit 1; }
      cmp "$NEW" "$CUR"                                              # 新脚本与 stage 一致
      # e2e 的两支（run_social_e2e.py 由 root 执行并写证明，social-e2e.mjs 是它跑的验收；两支都放在 root 拥有的 /opt/dsh）
      for f in run_social_e2e.py social-e2e.mjs; do
        sudo -n install -o root -g root -m 644 ~/stage-social56/deploy/dsh/$f /opt/dsh/$f
        cmp ~/stage-social56/deploy/dsh/$f /opt/dsh/$f
      done
      echo "新脚本已装好；旧版备份在 $BAK"
      EOF
      ```

   5. **必经：快照之后、升级之前，跑 `--show-bundle` 核对**。它做的检查跟真的升级一模一样：名字与位置、路径上没有符号链接；**从 `/` 一路到套件目录的每一层**（`/`、`/opt`、`/opt/dsh`、`vendor`、套件目录）都是 root 所有、群组与其他人不能写、dsh 走得进去（other 有 `x`）；套件里每个目录与档案不是符号链接、root 所有、群组与其他人不能写、**dsh 读得到**（档案 other 有 `r`、目录 other 有 `rx`）；有 `package.json`，而且它是合法的小 JSON（不超过 64 KiB），`name` 与 `version` 跟目录名对得上（见下面「套件内容与目录名」）。所以它过了才代表升级和回滚拿得到这些目录，装进去的是对的版本。要跑两次：

      ```sh
      bash -eu <<'EOF'
      # (a) 升级目标：不给参数，就是脚本预设的 social.56 这一组（连同这次要一起装的三个套件）
      python3 -B /opt/dsh/configure_social.py --show-bundle \
        --stickers /opt/dsh/vendor/nekowhale-1.1.1 --memes /opt/dsh/vendor/dsh-meme-0.1.64 --huanqi /opt/dsh/vendor/huanqi-1.1.0
      # (b) 回滚目标：social.55 那一组，连同回滚指令一模一样的三个额外套件，确认它们现在也还合格
      python3 -B /opt/dsh/configure_social.py --show-bundle --rollback --discord discord-0.6.0-social.55 --social social-0.1.49 --presets social-presets-0.52.0 \
        --stickers /opt/dsh/vendor/nekowhale-1.1.1 --memes /opt/dsh/vendor/dsh-meme-0.1.64 --huanqi /opt/dsh/vendor/huanqi-1.1.0
      echo "两次核对都通过"
      EOF
      ```

      **算过的标准**：两次结束码都是 0；(a) 印出的是 `discord-0.6.0-social.56`、`social-0.1.50`、`social-presets-0.53.0`（加上三个额外套件）的完整路径，(b) 印出的是 `discord-0.6.0-social.55`、`social-0.1.49`、`social-presets-0.52.0`；印出的路径跟你要的一模一样（不是别处）。只要任何一次印出「拒绝：…」或结束码不是 0，就**停下**，不要升级，把拒绝的原因回报。
   6. **e2e（由 root 跑、root 写证明）**，通过之后才升级。

      **先做探针**：确认这台机器的 `systemd-run --wait` 会把单元的结束码原样传出来（不影响任何服务）。用一个会回特定结束码 **7** 的单元，必须得到 7 才继续。不用会回 1 的单元（例如 false 那样的）的理由：1 也是 `systemd-run` 自己出错时的结束码，分不出是单元真的跑了、结束码被传出来，还是根本没跑成；7 不会是 `systemd-run` 的错误码，只可能来自单元。

      ```sh
      bash -eu <<'EOF'
      set +e
      sudo -n systemd-run --quiet --wait --collect /bin/sh -c 'exit 7'
      code=$?
      set -e
      if [ "$code" != 7 ]; then echo "systemd-run --wait 没有传出单元的结束码 7（得到 $code），停下，不要靠它当闸门" >&2; exit 1; fi
      echo "systemd-run --wait 正确地传出了单元的结束码 7"
      EOF
      ```

      探针过了，再把这一组套件 plugin add 进 e2e 的家（dsh 帐号，跟 social.55 那次一样；e2e 家里原本就装了 huanqi，所以也带上）：

      ```sh
      bash -eu <<'EOF'
      sudo -n -u dsh env -C /srv/dsh HOME=/var/lib/dsh DSH_HOME=/var/lib/dsh/.dsh-e2e DSH_HOST_MODULES=/opt/dsh/app/node_modules/@deepseek-ai PATH=/opt/dsh/node/bin:/usr/bin:/bin \
        /opt/dsh/node/bin/node /opt/dsh/app/node_modules/@deepseek-ai/dsh/lib/bin.js plugin --profile web add \
        file:/opt/dsh/vendor/discord-0.6.0-social.56 file:/opt/dsh/vendor/social-0.1.50 file:/opt/dsh/vendor/social-presets-0.53.0 file:/opt/dsh/vendor/huanqi-1.1.0 \
        --config.auto-install-peers=false --ignore-scripts
      # 跑 e2e：会等到结束；结束码 0 才表示通过并写下 /opt/dsh/e2e-attest.json
      sudo -n python3 -B /opt/dsh/run_social_e2e.py --huanqi huanqi-1.1.0
      EOF
      ```

      整段结束码是 0 才继续。然后群里安静 10 分钟以上才 `--upgrade`（套件旗标不用写，预设就是 social.56；`--stickers`／`--memes`／`--huanqi` 照旧）；`configure_social.py` 动任何东西之前会先核对这份证明（规则见「e2e 闸门」），不过就拒绝。升级后看 active、NRestarts=0、gateway ready、日志无 error，离线测她回话，并自己以 dsh 用正面立绘画一张确认。

### e2e 闸门（Rin 第 7 轮）：`result.json` 不是 root 可以信的东西

`/var/lib/dsh/.dsh-e2e/result.json` 是 dsh 帐号写的（目录也是它的）。以前（**旧规则，已废除**）`configure_social.py` 只看 `ok` 和 `checks` 的长度，所以 `checks=[false]*13` 或者 dsh 自己写一份全绿的都能让 root 去动正式服务；把它换成 FIFO 还能让 root 永远卡住。现在：

- **做法（我选的方案）**：由 root 执行的 `run_social_e2e.py` 自己启动 e2e 单元（`systemd-run --wait`）、等到它结束、亲眼看到结束码 0，再用安全读取（见下）读 `result.json`，**逐项**核对它：`ok` 正好是 `true`、`checks` 依序正好是 e2e 脚本会 push 的 19 个名称（逐字，repo 里有测试拿 `social-e2e.mjs` 比对）、`modelRequests` 是正整数、没有别的栏位。全部过了才在 root 拥有的位置写下 `/opt/dsh/e2e-attest.json`（root:root 644；`/`、`/opt`、`/opt/dsh` 本来就受 `verify_dir` 同一条信任链保护），内容：完成时间、单元结束码、`result.json` 的 sha256、`social-e2e.mjs` 的 sha256、每个要装的套件的内容指纹。
- **绑定**：`configure_social.py` 动任何东西（停 dsh、改设定）之前重新核对：证明本身可信（root 拥有、群组其他人不可写、普通档案）；不超过 12 小时、不在未来；单元结束码 0；现在的 `social-e2e.mjs` 就是当时跑的那份；`result.json` 的 sha256 跟证明里的一样（事后被改或换掉就对不上）并且内容通过上面的逐项核对；**每个要装的套件**的内容指纹（逐档 sha256）跟证明里的一样，位置也相同——所以 e2e 验的就是要装的这几个字节，不是别的版本。额外套件（`--stickers`／`--memes`／`--huanqi`）若 e2e 没验过，就必须跟正式服务现在装着的逐档相同（也就是只是重装已经在跑的版本）。
- **为什么选这个**：另外两种做法的比较——(1) 在 root 拥有的位置记 nonce、要 `result.json` 带同一个 nonce：nonce 得传给 dsh 帐号跑的 e2e 进程，同一个 uid 的东西读得到 `/proc/<pid>/environ`，防不了恶意，只防得了过期；(2) 只看 e2e 的结束码：没有跟套件内容、跟 `result.json` 绑在一起，旧的、别的版本的结果都能顶替。root 亲眼等单元结束并记下指纹，再在升级时重算，才把「结果」「脚本」「套件内容」「时间」四件事绑在一起。
- **e2e 家里装的跟要装的必须一样**：`run_social_e2e.py` 在跑之前和跑完之后，各把 e2e 家的 `node_modules/<包>`（略过它自己的 `node_modules`）跟 `/opt/dsh/vendor` 的套件逐档比对（档案集合与 sha256 都要相同，多一个档案、少一个、符号链接都拒绝）；部署前在 VM 上只读比过 4 个套件，完全一致，没有多余的档案。跑的当中被换掉，跑完那次比对看得出来。
- **剩下的风险（挡不住的）**：(a) e2e 是在 dsh 帐号拥有的沙盒里跑的，一个已经被攻陷的 dsh 帐号可以在 e2e 进行当中动手脚（例如改它能写的其他输入），只有套件目录的被换掉会被抓到；这道闸门挡的是意外、过期、手改、错版、事后篡改，不是完全被攻陷的 dsh。(b) 单元结束到 root 读 `result.json` 之间有一个很短的空档，dsh 可以换掉档案；但单元结束码已经是 0，换的内容也得通过同样的逐项核对，等于声称了一样的事。(c) 结束码靠 `systemd-run --wait` 传出；部署当天用上面的「结束码 7」探针实际确认一次（没有在 VM 上做过，因为这次只读）。(d) 12 小时的期限与系统时钟有关；时钟被改的话期限失准。(e) root 本人可以伪造证明，这是前提：root 是受信任的。
- **已知、另案（B 级，这一版不修，等老板决定要不要另开一案）**：只有在 dsh 帐号「有意地、而且同时」动手脚才会出事的竞态——
  1. `configure_social.py`：`check_profile_dir` 检查完 profile 目录之后，才用路径名去打开暂存档、再 `rename`。`O_NOFOLLOW` 只保护最后一段，dsh 若恶意、并且在检查和使用之间替换它能写的父目录，仍有竞态。要根治得改成用目录的 fd（`openat`／`dir_fd`）一路操作，或者降权成 dsh 再写。
  2. `pack_stage.py`：两次符号链接检查和 `write_bytes` 之间还有一个可被替换的窗口（这支程序在维护员本机跑，不是 root；要根治得用 `O_NOFOLLOW|O_EXCL` 建档再写）。
  这两项不影响正常使用，也不影响 dsh 的一般行为；列在这里是为了让部署的人知道它们存在、没有被遗漏。
- **回滚**：`--rollback` 略过这道闸门，条件是 `--discord`、`--social`、`--presets` 三个都要明讲，并且正好是 `configure_social.py` 里 `ROLLBACK_GROUPS` 白名单中的**同一整组**（不能是这一版的预设，也不能各挑各的版本拼起来：混搭的组合、没在名单里的完整组一律拒绝）；会在 stderr 印警告。理由：回滚到已经部署过、跑过 e2e 的旧版本，不该因为没有它的新 e2e 证明就退不回去；但「略过闸门」不能变成「随便什么版本组合都能装」。`--show-bundle --rollback` 会做同样的参数检查。目前名单里只有 social.55（`discord-0.6.0-social.55`／`social-0.1.49`／`social-presets-0.52.0`）。
  - **白名单怎么维护**：写在代码里，不放在 root 可写的状态档（退路是安全决定，要经过审阅的变更才能改，而不是靠一个会被不小心改到的档案）。每次新版本部署成功、观察期过后，下一版的代码把「刚被取代的那一组」加进 `ROLLBACK_GROUPS`（例如部署 social.57 的那版代码把 social.56 加进来）；加之前要确认：那一组的三个目录还留在 `/opt/dsh/vendor`、当时是整组部署并跑过 e2e。`test_rollback_groups_are_well_formed` 检查每一组的名称合规、三项齐全、不含这一版的预设。
  - 想退回名单外的版本（例如更早的 social.54）：先审阅并把那一整组加进名单，或者改走正常的 e2e 验收。
- **root 读 dsh 可写路径的原则**（同一类问题的整体检查，结果见回报）：一律走 `read_regular_file`——`O_NOFOLLOW|O_NONBLOCK` 打开（符号链接拒绝，FIFO 也不会卡在 open），对打开的 fd 做 `fstat`：必须是普通档案、拥有者在名单里（root 或 dsh）、大小不超过上限，读的时候也有上限；检查和读用同一个 fd，没有「检查后被换掉」的空档。适用于：`result.json`、`cordis.patch.yml`、备份的 7 个档案、e2e 家里的套件目录。root 写 dsh 的目录（`cordis.patch.yml`）：先确认路径上没有符号链接、目录拥有者合理，再写一个新的暂存档（写完读回核对）并 `rename` 盖过去（目标若是符号链接，换掉的是链接本身，不会写到它指向的地方），`fchown`／`fchmod` 在暂存档上做。
- **写入一律写完并读回核对**（Rin 第 8 轮）：`os.write` 可以只写一部分，也可能写不进去。`write_all` 用循环写完，写进 0 字节就报错（不会无限循环）；写完 `fsync`，再从头读回来核对大小和 sha256，不对就丢 `ShortWrite`、不 rename、清掉暂存档。`cordis.patch.yml` 的新内容先写成暂存档并核对，**然后才**停 dsh、再 rename 换上去——所以写不完整时 dsh 还没停、原来的设定一个字节没动。备份档案也走同一条路（写不完整就不留下备份）。
- 测试：`test_configure_social.py` 的 `SafeReadTest`（FIFO、有写入端的 FIFO、符号链接、悬空链接、目录、装置、过大、拥有者、群组可写、不会卡住——测试用计时器，卡住就失败而不是跑不完）和 `E2eGateTest`（见回报的清单）。


回滚：

- 只退 persona（**可以照着执行**）：`configure_social.py` 现在接受 `--discord`、`--social`、`--presets` 三个参数，指到旧版的套件目录；不给就用脚本这一版的预设。回滚就是**照升级的同一条指令**，其余旗标一个字不改，只多加这三个套件参数和 `--rollback`（略过 e2e 闸门；要退回的必须是白名单里的整组，规则见下面「e2e 闸门」）：

  ```sh
  bash -eu <<'EOF'
  # 1. 先核对（必经；不动任何东西，不需要 root；做跟升级一样的检查，不合格会印「拒绝：…」并以非 0 结束，后面的步骤就不会执行）
  #    参数要跟下面第 3 步的回滚指令一模一样：三个套件加三个额外套件，这样核对的就是真的会装的那一组
  python3 -B /opt/dsh/configure_social.py --show-bundle --rollback \
    --stickers /opt/dsh/vendor/nekowhale-1.1.1 --memes /opt/dsh/vendor/dsh-meme-0.1.64 --huanqi /opt/dsh/vendor/huanqi-1.1.0 \
    --discord discord-0.6.0-social.55 --social social-0.1.49 --presets social-presets-0.52.0
  # 2. 确认旧版目录都还在（缺一个就非 0 停下）
  ls -d /opt/dsh/vendor/discord-0.6.0-social.55 /opt/dsh/vendor/social-0.1.49 /opt/dsh/vendor/social-presets-0.52.0
  # 3. 退回（脚本会先停 dsh、备份设定、装套件、再启动；`--rollback` 略过 e2e 闸门，但只收白名单里的整组）
  sudo -n python3 -B /opt/dsh/configure_social.py --upgrade --rollback --group-tools full --no-daily-limits --all-channels \
    --stickers /opt/dsh/vendor/nekowhale-1.1.1 --memes /opt/dsh/vendor/dsh-meme-0.1.64 --huanqi /opt/dsh/vendor/huanqi-1.1.0 \
    --owner-title 生物爹 \
    --discord discord-0.6.0-social.55 --social social-0.1.49 --presets social-presets-0.52.0
  EOF
  # 4. 上面整段结束码是 0 才算退回了；然后确认：package.json 的三个套件是 social.55 / 0.1.49 / presets-0.52.0；四个套件跟 vendor 一致；active、NRestarts=0、gateway ready、日志无 error
  ```

  - 套件目录的检查（升级和回滚都一样，动任何东西之前先做）：名字要符合 `discord-0.6.0-social.N`、`social-0.1.N`、`social-presets-0.N.0`，而且是 `/opt/dsh/vendor/` 的直接子目录；解析（realpath）之后要跟原样一模一样，所以路径上任何一层都不能是符号链接，也不能指到 vendor 外面或另一个套件。**从 `/` 到套件目录的每一层**（`/`、`/opt`、`/opt/dsh`、vendor、套件目录）都要 root 所有、群组与其他人不能写、是真目录；套件里每个目录与档案同样要 root 所有、群组与其他人不能写、不是符号链接。`--stickers`、`--memes`、`--huanqi` 的目录也一样。
    - 为什么连 `/` 也查：`/` 若被别人写得进去，别人就能把 `/opt` 整个换掉，后面查得再严也是白查；真正的 VM 上 `/`、`/opt`、`/opt/dsh`、vendor 都是 root:root 755（2026-11-06 只读核对过），所以不会误拒。
    - dsh 读不读得到（动任何东西之前确认，因为 `plugin add` 是以 dsh 的身分去读套件，等停了服务才发现读不到就晚了）：**只看 other 位**——套件里档案要 `o+r`、目录要 `o+rx`，套件目录往上每一层要 `o+x`。选 other 位而不去查 dsh 属于哪些群组，理由是：所有这些档案的群组都是 root，dsh 不在里面，决定权就在 other 位；不依赖 VM 上帐号怎么设，检查在哪台机器上结果都一样。群组读位（例如 0640）因此也算不合格。
    - **套件内容与目录名**（Rin 第 5 轮）：光有 `package.json` 不够，错版的目录（名字叫 `social-0.1.50`、里面其实是别的版本）以前也会过。现在读 `package.json`（不超过 64 KiB、UTF-8、合法 JSON、最外层是物件、`name` 与 `version` 都是非空字串，否则拒绝），并要求跟目录名一致。规则取自 VM 上现有 9 个目录的实际内容（2026-11-06 只读核对）：

      | 目录 | `name` | `version` |
      | --- | --- | --- |
      | `discord-0.6.0-social.N` | `dsh-plugin-discord` | `0.6.0-social.N` |
      | `social-0.1.N` | `dsh-social-runtime` | `0.1.N` |
      | `social-presets-0.N.0` | `dsh-social-presets` | `0.N.0` |
      | `--stickers`（如 `nekowhale-1.1.1`） | `dsh-nekowhale-stickers` | 目录名最后一个 `-` 后面的 `x.y.z` |
      | `--memes`（如 `dsh-meme-0.1.64`） | `dsh-meme` | 同上 |
      | `--huanqi`（如 `huanqi-1.1.0`） | `dsh-huanqi` | 同上 |

      额外套件的目录名看不出版本、或这三个旗标换成了别种套件，都会拒绝；要换套件，先来改 `configure_social.py` 里的这张表。
    - **读外部输入一律严格**（Rin 第 6 轮；这是同一类问题：输入解析太宽松）：
      - `package.json` 与隔离验收的 `result.json` 用 `strict_json` 读：只收严格 UTF-8（不吃 BOM）；`NaN`、`Infinity`、`-Infinity` 拒绝（Python 预设的 `json.loads` 会收，JavaScript 的 `JSON.parse` 不收，两边看到的就不是同一份）；`1e999` 这种读进来会变无限大的数字也拒绝；嵌套太深拒绝。
      - **重复的键一律拒绝**（用 `object_pairs_hook`）。理由：同一个键出现两次时，各家解析器取第一个还是最后一个并不一致，我们核对的 `name`／`version` 和 npm／pnpm 实际用到的可能不是同一个；正常的 `package.json` 不会有重复键，放行没有好处。
      - `result.json`（dsh 帐号写的，不当成可信的）：最大 1 MiB；最外层要是物件，`ok` 要**正好是** `true`（不收 `"yes"`、`1`），`checks` 要是列表，而且依序正好是 e2e 脚本会 push 的 19 个名称（逐字；上面「e2e 闸门」那一节是现行规则，旧的、只看项目数量的规则已经废除）；多出别的栏位也拒绝；不然拒绝。
      - profile 的 `cordis.patch.yml`：用 YAML 的安全子集，同一层有重复的键拒绝（PyYAML 预设会静默留最后一个；`<<` 合并键盖掉原值是 YAML 允许的，不算重复），最外层要是一串物件，找不到 `discord-bridge` 设定就明讲拒绝（以前是 `StopIteration` 的堆栈）。
      - prepare_discord_plugin.py、prepare_social_materials.py 读上游（桥接、鱼记忆、opencc）的 package.json 也改用同一个 strict_json（上游的东西同样不当成可信的）；用真实的输入重跑过，产物照常。pack_stage.py 对命令列和档案路径同样严格（见上面）。--discord／--social／--presets 与额外套件的目录名用 \Z 结尾的正则、只收 ASCII 数字，名字里带换行或 Unicode 数字都拒绝。
      - 只读核对（2026-11-06，VM 上用新代码对真实资料跑，什么都没写）：9 个套件目录、`result.json`（19 项）、`cordis.patch.yml`（7 行，含 `discord-bridge`）全部通过。
      - 对应的测试：`test_nonstandard_json_constants_are_refused`（NaN、Infinity、-Infinity、1e999）、`test_duplicate_keys_are_refused`（包含「第一个对最后一个错」与相反两种、嵌套里的）、`test_deeply_nested_json_is_refused_not_a_traceback`、`StrictInputTest`（`strict_json` 逐项、`result.json`、YAML）。
    - 检查到使用之间的空档：之后一律用检查回传、解析过的路径；这条路径上每一层都是 root 所有、别人不能写，所以非 root 的人没办法在检查之后换掉它，能改的只有 root 自己。
    - 这些检查有 85 个单元测试（`test_configure_social.py`；测试映像已内建固定版本的 PyYAML，YAML 那一项不会被跳过）。除了符号链接（指到 vendor 外面、指到 vendor 里另一个目录、路径中间、套件里面）、拥有者与群组其他人可写（每一层）、缺 `package.json`，第 4 轮新增：`/opt` 为 0777 或 1777 拒绝、`/opt` 拥有者不是 root 拒绝、从起点往下每一层都查、「`/`」本身也查（用替身 lstat）、`package.json` 为 0600 拒绝、只有群组可读（0640）拒绝、子目录 0700（以及 0711、0744、0750）拒绝、套件目录自己或上层少了搜寻权限拒绝、全部只读（0444/0555）仍然通过。第 5 轮新增：错版（版本不对、包名不对，主要三个套件与三种额外套件都测）拒绝；坏 JSON（语法错、空、不是物件、缺栏位、栏位不是字串、不是 UTF-8、带 BOM）拒绝；超过 64 KiB 拒绝；正确的三类套件与额外套件通过。第 6 轮新增：见下面「读外部输入一律严格」。
  - 为什么选「让脚本接受版本参数」：升级和回滚走同一条路（同样的备份、同样的 e2e 门槛、同样的安装指令），不用另外保管一份旧脚本（旧脚本会跟新脚本的其他选项慢慢不一样），也不用手动还原设定档（要重装 node_modules，容易出错）。参数只收 `/opt/dsh/vendor/` 底下、名字符合 `discord-0.6.0-social.N`、`social-0.1.N`、`social-presets-0.N.0` 的目录，名字不对就在动任何东西之前拒绝；目录不存在也拒绝。
  - 部署 social.56 的那一步会把新版 `configure_social.py` 装到 `/opt/dsh/configure_social.py`（旧版的路径写死，不能指定版本）；所以这个回滚要在新脚本装上之后才可用。要是新脚本根本没装上，VM 上还是 social.55 的脚本，就没有要退的东西。
  - 这条回滚指令的参数检查有单元测试（`deploy/dsh/test_configure_social.py`），指令本身没有在 VM 上演练过，演练要等部署那天（可以在拍快照之后、升级之前用 `--show-bundle` 先看）。
  - persona 一退回，她用的又是 0.2.1 的路径，`/opt/dsh/whaledraw-0.2.1` 一直留着。
- **还原 `configure_social.py` 本身**（只有新脚本本身出问题、连 `--show-bundle` 都跑不出来时才需要；平常的回滚用上面那条就够了）：

  ```sh
  bash -eu <<'EOF'
  # 旧版备份在部署步骤里留好了：/opt/dsh/configure_social.py.social55（旧版路径写死 social.55）
  CUR=/opt/dsh/configure_social.py
  BAK=$CUR.social55
  [ -f "$BAK" ] || { echo "找不到备份 $BAK" >&2; exit 1; }
  sudo -n cp -p "$BAK" "$CUR"
  sudo -n chown root:root "$CUR"
  sudo -n chmod 644 "$CUR"
  # 核对（任何一项不过就非 0 停下）：拥有者与权限、内容跟备份一模一样、确实是 social.55 那一版、跟部署前记下的指纹一致
  [ "$(stat -c '%U %G %a' "$CUR")" = "root root 644" ] || { echo "拥有者或权限不是 root root 644" >&2; exit 1; }
  cmp "$CUR" "$BAK"
  [ "$(grep -c 'discord-0.6.0-social.55' "$CUR")" -ge 1 ] || { echo "还原后的不像 social.55 那一版" >&2; exit 1; }
  if grep -q -e '--show-bundle' "$CUR"; then echo "还原后的还有 --show-bundle，不是旧版" >&2; exit 1; fi
  now=$(sha256sum "$CUR")
  was=$(cat ~/configure_social.social55.sha256)       # 读不到就在这里停下（不会拿两个空字串去比、比出「一致」）
  now=${now%% *}
  was=${was%% *}
  if [ -z "$was" ] || [ "$now" != "$was" ]; then echo "指纹跟部署前记下的不一致（或没有记下）" >&2; exit 1; fi
  echo "已还原成部署前的 social.55 版，内容与部署前记下的指纹一致"
  EOF
  ```

  还原脚本只是换回旧的配置工具，不会动已经装好的套件与 dsh；真要把服务退回 social.55，仍然用上面那条「同一条指令加三个参数」的回滚（旧脚本不接受这些参数，要用新脚本才行），或者整个回 PVE 快照。
- 零件完全不受影响，不用回滚（要退也只是 `current` 指回上一组）。
- `/opt/dsh/whaledraw-0.2.2` 可以留着，也可以 `sudo rm -rf` 掉；要整个退到部署前，就回 PVE 快照。

### DEPLOY.md 里的 shell 片段怎么测

会真的拿去执行的片段（vendor 复制、stage 完整性核对、安装与还原 `configure_social.py`）由 `deploy/dsh/test_deploy_snippets.py` 从本文件里把原文抽出来跑（只把 `/opt/dsh` 换成临时目录、`sudo` 换成直接执行的替身），要 root 与 bash，在容器里跑：

- vendor 复制那段最后一行必须是 heredoc 的结束记号（外面不能再接 `echo "$?"`）；目标已存在、stage 缺目录、复制失败时，整段的结束码是 1，且复制失败时会列出已开始建立的目录。
- stage 核对：好的 stage 过；改一个档、删一个档、多一个档、多一个符号链接、清单被换、指纹不对或格式不对，全部非 0 且不印「全部通过」。
- 安装 `configure_social.py`：备份已存在、现有的不像 social.55 那一版、已经是新版，都拒绝，且不覆盖备份；安装后还原能回到原内容；备份被改过，还原的指纹核对会失败。
- 另有两条全篇规矩的测试（不需要 root）：VM 上跑的 `python3` 一律带 `-B`；任何片段里不准用 `echo` 印 `$?`。

### 给 Rin 复跑 Linux 测试的方式（不用再下载依赖）

映像 `whaledraw-test:0.2.2` 已建好，内含 Pillow/numpy/scipy（照 `wheels-vm.txt` 核对过 sha256）、系统中文字型、dsh 帐号。在本机 Docker 里：

```sh
# Git Bash / WSL / Linux
sh deploy/draw/test-container/run_linux_tests.sh C:\Users\USER\OpenBot\Shared\whale-draw-acceptance-20261101\samples
# PowerShell
powershell -File deploy\draw\test-container\run_linux_tests.ps1 -Samples C:\Users\USER\OpenBot\Shared\whale-draw-acceptance-20261101\samples
```

- 样本资料夹是选配（有 `whale_sheet.png` 才会跑真实样本那几项）。输出是 root、dsh 各一次的结果，约 2 分钟，不会下载任何东西。
- **结束码**跟结果一致：两个身分都通过才是 0；任何一个有测试失败、出错、或没跑起来，就是 1。
- 反证：`sh deploy/draw/test-container/selftest_runner.sh` 会放故意失败的夹具来测 runner 自己（只有 root 失败、只有 dsh 失败、两个都失败、测试没跑起来：都必须非 0；夹具都通过：必须 0），几十秒。
- Git Bash 下路径会自动转成 Windows 路径（用 `cygpath`），不用自己处理；还是不行就用 PowerShell 那支。
- 映像也导成了 `C:\Users\USER\OpenBot\Shared\whaledraw-test\whaledraw-test-0.2.2.tar.gz`；Docker 里没有映像时先 `docker load -i` 它。要自己建：见 `test-container/Dockerfile` 开头的指令。
- 容器只挂载 `deploy/draw`，所以 persona 的那一项会跳过；它在本机完整 repo 里（`deploy/social/persona-group.md` 在旁边）才会跑，已另外验证过。
- **deploy/dsh 的测试**（`configure_social.py`、`pack_stage.py`、本文件的 shell 片段、`prepare_discord_plugin.py`）：映像里另有一支 `run-dsh-tests.sh`，挂载整个 `deploy` 目录，root、dsh 各跑一次，两个都过才是 0：

  ```sh
  docker run --rm -v <repo>/deploy:/deploy:ro whaledraw-test:0.2.2 /usr/local/bin/run-dsh-tests.sh
  ```

  映像里内建固定版本加 sha256 的 PyYAML（`test-container/wheels-test.txt`，只在测试映像里，不装到 VM），所以 YAML 严格测试在 root 身分下不会被跳过；脚本开头会印出 PyYAML 版本，没有 PyYAML 就直接失败。dsh 身分下要 root 的片段测试会跳过（这是预期的）。
