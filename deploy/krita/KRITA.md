# Krita 原型（VM 102）：部署、分阶段执行、停止与回滚

老板授权在 VM 102 装 Krita 6.0.4 AppImage、xvfb、固定 commit 的 krita6-mcp，做原型。

限制：
- 她不给 sudo，由维护帐号代装。
- 不改 `NoNewPrivileges`，不改防火墙，不用 nft。
- Krita 用独立帐号、独立服务。
- 先拍 PVE 快照。
- 一旦影响到 bot 本体，立刻停掉。

## 放了什么

| 东西 | 位置 | 拥有者／权限 |
|---|---|---|
| Krita 6.0.4（AppImage 解开，sha256 `4ec9a458…de9f`，KDE 公布值） | `/opt/krita/6.0.4`，`current` 连结 | root，唯读 |
| krita6-mcp，commit `36388b85d96f526c61d97a400989c09b96c5c30a` | `/opt/krita6-mcp/36388b8`，`current` 连结 | root，唯读 |
| uv 0.13.6（只给上游检查用，sha256 `f29c3e94…3645`） | `/opt/krita-tools/uv-0.13.6` | root，唯读 |
| bridge 外挂（已启用） | `/var/lib/krita/.local/share/krita/pykrita/` | root，唯读；`kritarc` 归 krita |
| krita-gate、监控、上游检查、量测脚本 | `/opt/krita-gate/` | root，唯读 |
| 系统帐号 `krita`（nologin），群组 `kritaio`（成员：krita、dsh） | — | — |
| 共用资料夹最上层 | `/srv/dsh/krita-io`；Krita 和 gate 看到的是 `/srv/krita-io` | root:kritaio 0750（谁都不能把 in／out 换成连结） |
| `in/`：她放要汇入的图 | `/srv/dsh/krita-io/in` | dsh:kritaio 2750（krita 只能读） |
| `out/`：Krita 存的 .kra、PNG 和预览 | `/srv/dsh/krita-io/out` | krita:kritaio 2750（dsh 只能读，没办法在里面放连结） |
| 服务 | `krita.service`、`krita-gate.service` | 装好但**不启用、不启动** |

系统套件只多装 `xvfb`。AppImage 需要系统的 `libEGL.so.1`（`libegl1`、`libgl1-mesa-dri`）；VM 102 已经有，安装脚本同一行列出来只是确认，不会改动。`xauth`、`dbus-run-session` 本来就有。

### 隔离方式

- **资源**：Krita 有自己的 cgroup，MemoryMax 1600M、MemoryHigh 1200M、不用 swap、CPU 200%。
  - 超过 1600M 时，只有 Krita 自己的 cgroup 会被 OOM，不会波及 dsh。这一点由 cgroup 保证。
  - `OOMScoreAdjust=800` 只是让整台 VM 内存不够（全域 OOM）时，Krita 比较可能先被杀。这**不保证**一定先杀 Krita、不会杀到 dsh。
  - 真正的保护是 cgroup 上限，加上 krita-guard 每 5 秒检查一次：VM 可用内存低于 450 MB、或内存／CPU 压力过高，就先停掉 Krita 和检查程式。
- **沙盒**：`ProtectSystem=strict`、`ProtectHome`、`PrivateTmp`、`PrivateDevices`。能写的只有 `/var/lib/krita` 和共用资料夹。bot 的私人位置都遮起来：`/srv/dsh`、`/var/lib/dsh`、`/opt/dsh`、`/root`，以及有的话 `/etc/dsh`、`/var/backups/dsh`（设定备份）、`/var/log/dsh`、`/run/dsh`。2026-11-01 查过，VM 上目前没有 `/etc/dsh`。
- **网路**：`IPAddressDeny=any` 加 `IPAddressAllow=localhost`，只能被本机连，自己也不能外连。

### bot 只能透过 krita-gate 碰 Krita

- gate 的 socket 在 `/run/krita-gate/gate.sock`，群组 kritaio、0660。
- Krita 的 bearer token 在 `/var/lib/krita/.local/state/krita6-mcp`（0700，归 krita），dsh 读不到，所以绕不过 gate。
- gate 白名单（详见 `krita_gate.py`）：
  - 收：读取类、笔刷绘图、图层与选区、汇入、存 .kra、汇出 PNG。
  - 参数的形状和范围都有限制：
    - 点是 `[x, y]` 数字对，座标在 ±16384 内，不能多包一层；`shape` 里只能有一层。
    - 每个数值参数都有语义范围，例如笔刷 0.1–1000、不透明度和压力 0–1、宽高（与 x、y）1–4096、预览边长 32–1024。不认得的数值参数一律拒。
    - 颜色只收 `#RRGGBB`，id 只收识别字。NaN、Infinity、超大整数、溢位都拒，不会让例外穿透。
  - 档案只收档名，路径由 gate 决定。
    - 存档：Krita 写 `out/<job>/`（归 krita，dsh 只能读）。
    - 汇入：gate 先用 `O_NOFOLLOW` 读 `in/<job>/<档名>`（检查是一般档案、PNG、16 MB 内），复制到只有 krita 能读写的 `/var/lib/krita/staging/<job>/<随机名>.png`，再让 Krita 读这份副本。Krita 的汇入根目录只设 staging，`in/` 对 Krita 本身是遮住的。所以就算 dsh 在 gate 检查完之后换掉 `in/` 里的档，Krita 读到的还是检查过的副本。
  - 预览由 gate 自己写进它建立、它拥有的 `out/<job>/`，开档用 `O_NOFOLLOW`＋`O_EXCL`，碰到符号连结、硬连结或别人拥有的资料夹都拒绝。
  - 不收：开任意档、档案图层、AI Diffusion、向量编辑、任何 exec。
- 上限：
  - 一批 50 个操作、20000 个点；单笔 2048 点；单个参数 64 KB；整个请求 1 MB。
  - 每个连线从接上开始，**10 秒内**要把请求送完（绝对期限，一个字节一个字节慢慢送也一样）。
  - 同时最多服务 6 个连线，超过的当场回「too many connections」并关掉，不另开执行绪。
  - 同时只跑一批，第二批立刻回「忙碌」，不排队。
  - JSON 里重复的键（例如 `command` 出现两次）整个请求直接拒绝，被审计到的就是实际执行的。
- 配额与清理（只动 krita 帐号自己建立的一般档案，不跟随连结）：
  - 每个 job 的 `out/<job>` 加 staging 合计最多 200 MB；`out/` 加 staging 总共最多 2 GB；磁盘剩不到 2 GB 就拒绝。
  - **每一个**会写档的操作送出前都检查一次，用当下磁碟上的实际用量，加上这个操作会写的量：
    - 汇入：档案的实际大小；
    - 预览：先预留 16 MB；
    - 存 .kra 或汇出 PNG：没有固定预留。gate 先向 Krita 要这份文件的 `inspect_document`（画布宽高、每个图层的范围），按最坏情况算：
      - PNG：每列原始像素加 1 个滤波位元组，再加 deflate 的最坏膨胀、每个 IDAT 区块 12 位元组、1 MB 的标头与色彩描述档余量。4096×4096 约 65 MB。
      - .kra：每个图层（含群组、遮罩）按它的范围算原始 RGBA 图块（64×64，不对齐时每边多算一块，范围可超出画布），每层再加 64 KB，加上合并图 PNG 和缩图 PNG 的最坏情况，整个再算一次 deflate 膨胀，加 1 MB。只会多算，不会少算。
      - 画布上限因此从 8192 降到 4096：8192×8192 的 PNG 光原始像素就约 256 MB，超过单一 job 的 200 MB 配额；4096×4096 一张满版 PNG 一定放得下。
      - Krita 的描述不是预期的样子（宽高不是 1–4096 的整数、没有图层清单、某层没有可用的范围），或拿描述的那个操作没在时限内结束，这次存档就拒绝，不送出。
  - 超过就停下这一批，回 `refused before sending`；之前已经做的操作照算。所以一批最多只会超出一次预留量，不会像以前那样一批 50 个汇入一口气写进 800 MB。
  - 单纯画图不受影响。
  - 每次要写档前和 gate 启动时清理：staging 里超过 1 天的副本、`out/` 里超过 7 天的预览图（`preview-*.png`），以及清完后变空、归 krita 的 job 资料夹。
  - 她存的 .kra 和汇出的 PNG 不会自动删。
- 时间与取消（实际行为）：
  - 每个操作最多等 `min(30 秒, 这批剩下的时间)`，一批最多 180 秒。没做完就请 Krita 取消它，再最多等 30 秒让 Krita 真的结束或丢掉它。
  - 在这个操作真的结束之前，批次锁不会放开，后面的操作也不会送出。
  - 如果 Krita 30 秒后还没结束，gate 会记住它；之后的请求一律回「忙碌」，直到 Krita 回报它结束。
  - `{"cancel": job}`：标记取消，并立刻请 Krita 取消正在跑的那个操作。之后的操作都不会执行。
  - 如果取消送到时已经是最后一个操作，而且它照样做完了，批次会回 `ok: false`，`stopped` 写「cancel arrived too late … nothing was skipped」。呼叫者看得出来取消其实没挡下任何东西。
  - 限制：Krita 无法中断一笔已经在画的原生笔触，取消只能保证「它之后的都不跑」，不能保证这一笔画一半就停。
  - 记号档 `/var/lib/krita/gate/stuck.json`（先写暂存档、fsync，再改名）：
    - **每个操作送出之前**先写一笔 `in-flight`，Krita 回报结束后才删掉。所以就算 gate 在「送出之后、记下来之前」当掉，重启后也看得到。
    - 逾时没结束会写成 `stuck`。送出时连线出错、不确定 Krita 有没有收到，也当作 `stuck`。请 Krita 取消时，取消请求本身出错，也一样：只有 Krita 确认结束才会清掉。
    - 内存里的状态跟记号档同进退（fail closed）：写记号档之前，内存就先设成忙碌；删记号档成功之后，内存才清掉。所以只要磁碟上有 `in-flight`、Krita 又还没确认结束，内存一定是忙碌；写或删记号档失败时，也维持忙碌。
    - gate 启动时只要看到记号档，就一律回「忙碌」。
  - 自动解除：Krita 回报那个操作结束，或那个 Krita 实例已经不在了（例如重启过 Krita），下一次请求时清掉。
  - **记号档坏掉（读不出来）时不会自动解除**，一直回忙碌，等人确认。手动步骤：
    1. 确认没有在画图：`journalctl -u krita-gate -n 50`、`/var/log/krita-guard.log`。
    2. `sudo systemctl stop krita-gate.service`
    3. `sudo systemctl restart krita.service`（确保 Krita 里没有还在跑的操作）
    4. `sudo cat /var/lib/krita/gate/stuck.json` 留底后，`sudo rm /var/lib/krita/gate/stuck.json`
    5. `sudo systemctl start krita-gate.service`，送一个只读请求（例如 `list_documents`）确认回 `ok`。

### 接受的风险（写明）

- **gate 和 Krita 用同一个 krita 帐号。** 原因：krita6-mcp 的连线程式只认「档案拥有者就是自己、权限 0600」的 discovery 档（`bridge_client.py:55–58`），换帐号就读不到 token；要分开得 fork 上游。
- **这代表 gate 的白名单只挡得住 bot，挡不住 Krita 自己。**
  - 如果 Krita 被攻破（例如被一张恶意图片或外挂漏洞控制），它就有 krita 帐号的权限，可以读到 token，直接对 bridge 下任何指令，完全绕过 gate 的白名单。
  - 反过来也一样：gate 被攻破，就能拿 token 直接操作 Krita。
  - 一旦 token 外泄，gate 的白名单就不再提供保护。
- 这时还剩下的保护，只有 krita 帐号外面那层沙盒：看不到 bot 的资料、不能外连、只能写 `/var/lib/krita`、`out/` 和 staging、内存和 CPU 有上限。
- 同样因为同一个帐号：被攻破的 Krita 也可以改写 staging 里的汇入副本，「先复制到 staging 再让 Krita 读」只防得了 dsh 在检查后换档，防不了 Krita 自己。这一点留到阶段 1、2 实测后再决定是否接受。
- 所以这个风险只有在阶段 1、2 把沙盒都实际验证过之后才接受，而且只限原型。正式长期使用前，要重新评估是否 fork 上游，让 gate 改用另一个帐号。

## 更新已安装的 gate 与工具档

阶段 0 装好之后，`deploy/krita/` 的 gate、检查脚本或 unit 档如果有改，用这个更新：

```sh
sudo sh ~/stage-krita/update_krita_files.sh ~/stage-krita
```

`~/stage-krita` 除了 `deploy/krita/` 的档案，还要有 `wheelhouse/` 和 `wheelhouse.sha256`（检查程式用的离线套件，在 VM 外用 `fetch_probe_wheels.sh` 下载，见阶段 1）。

- 只会替换 `/opt/krita-gate/*`（含 `probe-requirements.txt` 和 `wheelhouse/`）和两个 unit 档，并补上新版本需要的 `/var/lib/krita/gate`（krita 700）。不动 Krita 本体、krita6-mcp、帐号和共用资料夹。
- **先检查目标，全部合格才动手**（fail-closed）：
  - `/opt`、`/etc/systemd/system`、`/opt/krita-gate` 都是 root:root 755；已有的每个档案都必须是 root:root，权限正好是 644 或 755；资料夹里不能有清单以外的档案（`wheelhouse/` 里只能有 root 644 的 `.whl`）；两个 unit 档是 root:root 644，而且没有 drop-in；`/var/lib/krita` 是 krita 750，`/var/lib/krita/gate` 如果已存在要是 krita 700。
  - krita、krita-gate 或任何检查程式正在跑时也会拒绝。
- **冻结来源**：先把来源档（不跟随连结）复制到只有 root 能读的 `/var/backups/krita-update-<时间>/source/`，之后的检查和安装都只用这份。所以来源资料夹在检查之后被换掉也没用。检查内容：齐全、不是空档、不是连结；Python、shell 能解析；unit 档有 `[Service]`；每个 wheel 都对得上 `wheelhouse.sha256`，数量一致，而且 sha256 都钉在 `probe-requirements.txt` 里。来源检查没过，连备份资料夹也删掉，系统完全没变。
- 动手时：
  1. 把整个 `/opt/krita-gate` 和两个 unit 档备份到同一个 `/var/backups/krita-update-<时间>/`（root 700）。
  2. 在旁边建好新的资料夹，用改名的方式换上去；unit 档也是先写暂存档再改名。
  3. 最后逐档比对内容和权限。
- 回滚：
  - 每一步要做之前，就先记下「这一步可能做了」，所以不管在哪两步之间出错或被中断（包括 Ctrl-C、`kill`），都会还原。
  - 还原时某一步失败，不会中断，其他步骤照样做完，最后列出哪几步失败，并印出 `ROLLBACK INCOMPLETE`，请从备份手动还原。
  - 例如：删不掉新的 `/opt/krita-gate`，unit 档和 `/var/lib/krita/gate` 照样还原，旧版本留在 `/opt/krita-gate.old-<时间>`。
- 2026-11-01 在 systemd 容器跑 `test_update_krita_files.sh`：46 项都过。
  - 各种不合格的目标或来源（含被改过的 wheel、多出来的 wheel、没钉在需求档里的 wheel、连结）都会被拒，拒绝原因正确，系统状态完全没变，也不留备份资料夹。
  - 在 12 个步骤之间各自注入失败、再各自用 TERM 讯号中断（共 24 种），都还原到跟原本一模一样。
  - 故意让还原的某一步失败，其他步骤照样还原，并正确回报失败几步。
  - 正常更新，以及在已有 wheelhouse 上再更新一次，都成功。

## 升级 Krita 版本（换 AppImage）时

1. 新版本解开到新目录 `/opt/krita/<新版本>`，root 所有、唯读；旧版本目录留着当回滚。
2. **krita.service 里这两行要一起改成新版本的真实目录**，不能写 `/opt/krita/current`（阶段 2 实测连结路径会让内建 Python 找不到标准库）：
   - `Environment=APPDIR=/opt/krita/<新版本>`
   - `ExecStart=… /opt/krita/<新版本>/AppRun --nosplash`
   - `test_krita_gate.py` 会检查两行一致、而且不含 current。
3. 核对 `current` 连结：`readlink /opt/krita/current` 要指到新版本。`run_probe.sh` 的检查程式和 importer 路径都经过 `current`。
4. 用 `update_krita_files.sh` 装上新的 unit 档（先停服务）。
5. 重跑 `probe_krita`、`smoke_krita`：报告里的 `krita` 版本要是新版本，Python／PyQt 版本也记下来。krita6-mcp 上游测过的组合不一定包括新版本，有差异要写进这份文件。
6. 都通过了，才照「启动」那段起服务。

## 停止（一行）

```sh
sudo systemctl stop krita-gate.service krita.service 'krita-probe-*.service'
```

## 回滚（谁确认：老板。由我执行前先回报 Iris 转老板）

1. 停止，并移除服务：

   ```sh
   sudo systemctl stop krita-gate.service krita.service 'krita-probe-*.service'
   sudo systemctl disable krita-gate.service krita.service
   sudo rm /etc/systemd/system/krita.service /etc/systemd/system/krita-gate.service
   sudo systemctl daemon-reload
   ```

2. **先备份作品**。共用资料夹里可能有她的作品，不直接删：

   ```sh
   sudo tar -czf /var/backups/krita-io-$(date +%Y%m%d-%H%M).tar.gz -C /srv/dsh krita-io
   sudo chown -R dsh:dsh /srv/dsh/krita-io
   ```

   资料夹本身留着（改归 dsh）。要不要删、什么时候删，由老板看过备份后决定。

3. 移除软体与帐号，只删 Krita 自己的东西：

   ```sh
   sudo rm -rf /opt/krita /opt/krita6-mcp /opt/krita-tools /opt/krita-gate /srv/krita-io /var/lib/krita
   sudo gpasswd -d dsh kritaio; sudo userdel krita; sudo groupdel kritaio
   sudo apt-get remove -y xvfb
   ```

4. 或直接回 PVE 快照：阶段 0 之前是 `before-krita-20261101`，阶段 1 之前是 `before-krita-phase1-<日期>`。这会把 bot 一起退回快照当时的状态，只在上面的步骤不够时用，而且要老板确认。

## 阶段（每阶段结束都先回报 Iris，等她说才往下）

整个过程都开着监控：

```sh
sudo nohup /opt/krita-gate/krita_guard.sh >/dev/null 2>&1 &
```

- 每 5 秒在 `/var/log/krita-guard.log` 记一次可用内存、内存／CPU 压力，以及 dsh 和 Krita 的内存峰值。
- 出现以下任一情况，立刻停掉 Krita 和检查程式：dsh 重启或停止、dsh 被 OOM 杀、可用内存低于 450 MB、内存压力超过 20%、CPU 压力超过 60%。

### 阶段 0：快照与安装

```sh
# pve2
qm snapshot 102 before-krita-20261101 --description "2026-11-01 before Krita 6.0.4 + krita6-mcp phase 0 (install only, nothing started)"
# VM 102，维护帐号；先把 repo 的 deploy/krita/ 传到 ~/stage-krita/
sudo sh ~/stage-krita/install_krita.sh ~/stage-krita
```

- 安装脚本只做全新安装：以下任何一个已经存在（连失效的连结也算）就直接停，不改任何既有东西的拥有者、权限或连结：
  - `/opt/krita`、`/opt/krita6-mcp`、`/opt/krita-tools`、`/opt/krita-gate`
  - `/var/lib/krita`、`/srv/dsh/krita-io`、`/srv/krita-io`
  - 两个 unit 档
  - krita 帐号、krita 或 kritaio 群组
- 要重装，先照「回滚」清干净。

### 阶段 1：隔离验证与上游检查（不启动 krita.service）

开始前（照顺序做）：

1. 再拍一个「当前状态」快照：在 pve2 上执行 `qm snapshot 102 before-krita-phase1-<日期> --description "<日期> before Krita phase 1 (probes)"`，然后用 `qm listsnapshot 102` 确认快照在。
2. 用 `update_krita_files.sh` 把阶段 0 之后改过的档案装上（见上一节）。
3. 只读确认 krita.service 没有 drop-in：
   - `systemctl cat krita.service` 只能印出 `/etc/systemd/system/krita.service` 一个档，不能有 `# /etc/systemd/system/krita.service.d/...` 这类段落；
   - `systemctl show krita.service -p DropInPaths --value` 要是空的；
   - `ls -d /etc/systemd/system/krita.service.d /run/systemd/system/krita.service.d /usr/lib/systemd/system/krita.service.d` 三个都要不存在。
   - `run_probe.sh` 每次执行也会自动再查一次，有 drop-in、或 krita.service 不是从 `/etc/systemd/system/krita.service` 载入，就拒绝执行。

每一步都用 `run_probe.sh` 跑。它会开一个暂时性的 systemd **服务**（不是 scope），所有 systemd-run 属性都由 `unit_props.py` 直接从 `/etc/systemd/system/krita.service` 读出来，所以两边不会不一致：

- 照抄的：帐号与群组、UMask、环境变数、资源上限、所有沙盒设定、BindPaths、两条 InaccessiblePaths、IP 过滤、位址族限制。
- 刻意跳过的项目和理由（`unit_props.py` 的 `SKIP`）：
  - `ExecStart`、`ExecStartPre`、`ExecStartPost`、`ExecStop`、`ExecReload`：检查程式有自己的指令；
  - `Restart`、`RestartSec`：检查只跑一次，失败就回报，不能被自动重启；
  - `WorkingDirectory`：检查要在 `/opt/krita6-mcp/current` 里跑，由 `run_probe.sh` 另外设定；
  - `Type`、`RemainAfterExit`：只跟程式的生命周期有关，`systemd-run --wait` 要用自己的预设。
  - 这些都不影响程式能读、能写、能连到哪里或能用多少资源。krita.service 目前只设了其中的 ExecStart、Restart、RestartSec、WorkingDirectory。
- `sudo bash /opt/krita-gate/run_probe.sh props` 可以印出实际用的属性清单。
- 2026-11-01 在 systemd 容器里，用 `systemctl show` 逐项比对 krita.service 和暂时性服务的 28 个沙盒与资源属性：差异 0、空值 0。

```sh
sudo bash /opt/krita-gate/run_probe.sh verify               # 先验证隔离
sudo bash /opt/krita-gate/run_probe.sh setup                # 离线准备上游检查用的环境（完整沙盒，IP 过滤照开）
sudo bash /opt/krita-gate/run_probe.sh probe_plugin_import
sudo bash /opt/krita-gate/run_probe.sh probe_krita
sudo bash /opt/krita-gate/run_probe.sh smoke_krita
```

`setup` 是**离线**安装，krita 帐号从头到尾不能外连：

- 以前的做法是拿掉 IP 过滤跑 `uv sync`，等于让 krita 帐号可以连到任何地方；这个做法已经拿掉，不要再用。
- 现在：检查程式要的 28 个 wheel（约 9 MB）在 VM 外面、用一次性的 ubuntu:24.04 容器跑 `fetch_probe_wheels.sh` 下载。每个都核对 `probe-requirements.txt` 里钉的 sha256（从 krita6-mcp 钉住那个 commit 的 `uv.lock` 汇出）。下载好的 wheel 跟着 stage 资料夹传上 VM，由安装／更新脚本放进 root 所有、只读的 `/opt/krita-gate/wheelhouse/`。
- `setup` 在**完整沙盒**（IP 过滤照开）里做三件事：
  1. `uv venv --offline`；
  2. `uv pip install --offline --no-index --no-build --require-hashes --find-links /opt/krita-gate/wheelhouse -r probe-requirements.txt`：不在清单里、或 sha256 不对的都拒，也不会从原始码建置；
  3. 写一个 `.pth` 档，把 krita6-mcp 的 `src/`、`plugin/`（root 所有、只读）放进 venv 的路径。
- 最后确认 `import mcp, krita6_mcp.cli, krita6_bridge.discovery` 成功。
- venv 已经存在就拒绝（要重建先删掉 `/var/lib/krita/probe-venv`）；因为全程离线，重跑也不会打开对外连线。
- 2026-11-01 在断网的 ubuntu:24.04 容器以 krita 帐号实测：28 个 wheel 全部装好，三个检查工具的 `--help` 和 krita6-mcp 的模组都能载入。

过关标准：

1. `verify` 分两步，都用 IP 直连（不经过 DNS）：
   - **对照组**：同一套沙盒、只拿掉 IP 过滤，三个目标（1.1.1.1:443、9.9.9.9:443、8.8.8.8:53）都要 `OPEN`。这证明当下 VM 是连得出去的。对照组没过，整个 verify 就不算数。
   - **沙盒**：几秒后在同样的沙盒里连同样的目标，都要是 `DENIED … DROPPED` 或 `BLOCKED_BY_FILTER`。systemd 的 IPAddressDeny 是直接丢掉封包，所以看起来是逾时。2026-11-01 在 systemd 容器实测就是 DROPPED。
   - 只有两种结果算「被过滤器挡住」：`EPERM`／`EACCES`，或连线逾时（封包被丢）。其他所有失败都算 `FAIL`，不算挡住：DNS 错误、网路或主机不可达、被拒绝、连线重置、其他任何错误。
   - 目标写死成 IP，程式启动时会检查真的是 IP，所以不会去查 DNS。`test_krita_gate.py` 的 NetcheckTest 逐一测过这些情况，包括 DNS 失败、`ENETUNREACH`、`EHOSTUNREACH`、`ECONNREFUSED`、`ECONNRESET`。
   - 另外，`/etc/systemd/system/krita.service` 里每一条 `InaccessiblePaths=` 的路径都要印出 `DENIED`（清单由 `netcheck.py` 直接从 unit 档读出来，不是写死的）：`/srv/dsh`、`/var/lib/dsh`、`/opt/dsh`、`/root`、`/etc/dsh`、`/var/backups/dsh`、`/var/log/dsh`、`/run/dsh`、`/srv/krita-io/in`。读不懂 unit 档（续行、引号、`%` 代换、空值）就直接 `FAIL`。最后还要印出 `LOOPBACK OK`。
   - 只要有任何一行 `LEAK` 或 `FAIL`（结束码不是 0），就停下回报。
2. 真正跑起来的服务也要验证（阶段 2 启动后，全部是只读指令）：
   - `sudo nsenter -t $(systemctl show -p MainPID --value krita) -m ls /var/lib/dsh /srv/dsh /etc/dsh`：每个都要被拒或不存在；
   - `systemctl show krita -p IPAddressDeny -p IPAddressAllow`：要是 `any` 和 localhost；
   - 如果有 `bpftool`：`sudo bpftool cgroup show /sys/fs/cgroup/system.slice/krita.service`，要看到 ingress／egress 过滤程式。
3. 三个上游检查都通过；`probe_krita` 的报告显示原生笔触确实改到像素、undo 可用、.kra 存回再开后像素一致。

备注：

- 2026-11-01 在本机容器解开同一个 AppImage（sha256 相符）查过：
  - 内附 Python 3.13、PyQt6 6.8.1（`pyqt6_sip` 13.10.0）、`libxcb-cursor`，以及 xcb 平台外挂；
  - importer 在 `/opt/krita/current/usr/share/krita/pykrita/plugin_importer/plugin_importer.py`；
  - 上游测试用的是 PyQt 6.11、Python 3.14，版本差异会不会出问题要阶段 1 才知道。
- `probe_plugin_import` 是用外部 Python 载 Krita 的 importer。如果它因为外部环境没有 PyQt6 而失败，那是检查工具的问题，不是外挂不相容；外挂能不能载入，以 `probe_krita`、`smoke_krita` 为准。
- 两个服务都同时设了 `InaccessiblePaths=/srv/dsh` 和 `BindPaths=/srv/dsh/krita-io:/srv/krita-io`。如果 systemd 因此挂不上共用资料夹（阶段 2 会看到 `/srv/krita-io` 是空的或启动失败），就从 InaccessiblePaths 拿掉 `/srv/dsh`。krita 帐号本来就进不了 `/srv/dsh`（dsh:dsh 750），所以拿掉后隔离仍在；拿掉了要记下来。

#### 阶段 1 实测纪录（2026-11-01 UTC 08:17–08:31，VM 102）

- 群里安静 60 分钟后开始。快照 `before-krita-phase1-20261101`。`update_krita_files.sh` 成功，档案与来源一致，补上 `/var/lib/krita/gate`（krita 700）。没有 drop-in。
- `verify`：对照组三个目标都 `OPEN`；沙盒里 9 个 InaccessiblePaths 都 `DENIED`，三个外连都 `DROPPED`，`LOOPBACK OK`。
- `setup`：完整沙盒、离线，约 2 秒装好 28 个套件。
- 上游检查发现两个 **krita.service 的设定错误**（已在 repo 修好，VM 上还是旧档，要先审过、用 `update_krita_files.sh` 装上，再用正式的 `run_probe.sh` 重跑 `probe_krita` / `smoke_krita` 确认，才能进阶段 2）：
  1. 没设 `APPDIR`：解开的 AppImage 的 AppRun 只设 `LD_LIBRARY_PATH`，Krita 内建的 Python 3.13 找不到标准库（`No module named 'encodings'`），任何 Python 外挂都载不进来。修法：`Environment=APPDIR=/opt/krita/6.0.4`（阶段 2 发现 `/opt/krita/current` 这个连结路径不行，见阶段 2 纪录）。
  2. `Environment=KRITA6_MCP_INPUT_ROOTS={"in":…}` 的双引号被 systemd 去掉，桥接收到 `{in:/var/lib/krita/staging}`，回 `INVALID_CONFIGURATION` 后不启动（不写 discovery，log 里也没有讯息；是用一个只读的诊断外挂在 Krita 里读出 `_last_error` 才找到）。修法：整个指定用单引号包起来。`test_krita_gate.py` 加了回归测试；systemd 容器里 `systemd-run` 和 `systemctl show` 都确认收到的是正确 JSON。
- 用暂时的 run_probe 复本（只多加 `APPDIR` 和正确引号的两个变数，其他沙盒属性一样由 unit 档产生）跑：
  - `probe_krita`：通过。Krita 6.0.4、Qt 6.8.0、PyQt 6.8.1、Python 3.13.5；16 项全过：原生路径／压力线条会改像素、undo／redo、缩图、存 .kra、汇出 PNG、重开后图层与像素一致。
  - `smoke_krita`：通过。56 个工具；原生路径与线条、内嵌 PNG、分层存档与另外汇出、拒绝覆写、重复送出只执行一次。
  - `probe_plugin_import`：上游工具会把 ZIP 写进只读的 `<project>/dist` 而失败，不是外挂问题。改成写到报告资料夹后通过；`run_probe.sh` 已照这个改。
- 量测（**实测**，直接对桥接、不经过 gate，在同一套暂时性沙盒服务里；1200×800 文件，笔刷「b) Basic-5 Size」）：
  - Krita 启动到桥接可用：36.1 秒；建文件 787 ms。
  - 单笔：10 点 106 ms（8 px）／158 ms（60 px）；100 点 55／54 ms；500 点 58／109 ms。
  - 一批 30 笔（每笔 100 点、12 px）：共 2122 ms，平均 71、最长 112 ms。一批 50 笔：共 2751 ms，平均 55、最长 57 ms。
  - 预览（长边 1024）219 ms、358 KB；存 .kra 156 ms（625 KB）；汇出 PNG 96 ms（292 KB）。
  - 记忆体峰值（暂时性服务的 cgroup，含 Krita、Xvfb 和量测程式）：635 MB；四次 Krita 启动里最高 635 MB。krita.service 上限是 1600 MB。
  - `measure_krita.py` 本身有错：一批 50 笔时笔画会画到画布外（y 到 806），Krita 回 `OUT_OF_BOUNDS`。已改成 `70 + 13 * i`（最高 767）。
- 监控（krita_guard 每 5 秒，共 151 笔）：没有触发停止；可用记忆体最低 3719 MB；记忆体／CPU 压力都是 0%。dsh：前后都 active、NRestarts=0、OOM 0、记忆体 233 → 238 MB（这段时间最高 238 MB），这段时间群里没讯息，所以量不到回话延迟。
- 收尾：没有残留的 `krita-probe-*` 单元、krita 帐号的程序或 Xvfb；krita.service、krita-gate 都是 inactive／disabled。暂时的 run_probe 复本、诊断外挂和监控脚本都删了，报告留在 `/var/lib/krita/probe-reports/`。

#### 阶段 2 实测纪录（2026-11-02 UTC 05:03–05:13，VM 102）

- 群里安静 15 分钟后开始，期间群里 0 则讯息。快照 `before-krita-phase2-20261102`。
- 发现并修好两个问题（都在 repo，VM 上已用 `update_krita_files.sh` 装上；每次更新前先停服务）：
  1. `APPDIR=/opt/krita/current`（连结路径）不行，一样是 `No module named 'encodings'`；要真实目录 `/opt/krita/6.0.4`（阶段 1 实测过的值）。`ExecStart` 也改成同一个目录，升级 Krita 时两行一起改。测试加了「APPDIR 不能含 current」。
  2. gate 建 job 资料夹用 `mkdir(0o2750)`，在 `RestrictSUIDSGID=yes` 的服务里回 `EPERM`，第一批就失败。改成 `0o750`；上层 `out/` 是 setgid，新资料夹照样继承（实测 `2750`）。加了测试，容器里也重现过 EPERM。
- 环境变数：`systemctl show` 和 `unit_props.py` → `systemd-run -p` 收到的 JSON 都是对的（单引号写法）；用正式的 `run_probe.sh` 重跑 `probe_krita`（16 项全过）、`smoke_krita`（通过）、`probe_plugin_import`（通过）。
- 启动：krita.service 启动后约 34 秒桥接就绪（state 目录 krita 700）；krita-gate 起来，dsh 连得上 socket。（就绪一律以「经 gate 的只读请求回 ok」判断，见下面的启动步骤。）
- 量测（**实测**，经 gate、以 dsh 执行 `measure_krita.py`，1200×800，「b) Basic-5 Size」）：
  - 单笔（gate 回报的操作耗时）：10 点 59／71 ms（8／60 px）；100 点 60／73 ms；500 点 61／73 ms。
  - 一批 30 笔：2099 ms（平均 69、最长 111）；一批 50 笔：3535 ms（平均 70、最长 127）。
  - 预览 227 ms；存 .kra 187 ms；汇出 PNG 119 ms。
  - krita.service 记忆体峰值：量测后 493 MB；整个阶段 guard 看到的最高 614 MB（上限 1600 MB）。
- 实机权限：
  - staging 与 gate 资料夹都是 `krita:krita 700`；`sudo -u dsh test -w /var/lib/krita/staging` 回 1。
  - krita-gate：`ReadWritePaths=/var/lib/krita/staging /var/lib/krita/gate`，`ReadOnlyPaths=/var/lib/krita`；在它的命名空间里 staging 是 `rw`、`.local/state` 是 `ro`。
  - Krita 的命名空间里 staging 是 `rw`（已写明的接受风险）。
  - 在 Krita 的命名空间里以 krita 身分列 `/var/lib/dsh`、`/srv/dsh`、`/opt/dsh`、`/root`、`/srv/krita-io/in` 都被拒，`/etc/dsh` 不存在。
  - IP 过滤：`IPAddressAllow=127.0.0.0/8 ::1/128`，`IPAddressDeny=0.0.0.0/0 ::/0`；bpftool 看到 `sd_fw_ingress`／`sd_fw_egress`。
  - dsh 读不到 token 目录；写不进 `out/`；没带 token 打桥接的 `/v1/status`、`/v1/session` 都回 401。
- gate 白名单：open_document、run_python、create_file_layer、generate_diffusion、add_vector_shape、set_file_layer 都被拒；自带 path、`../` 档名、重复的键、8192×8192 画布也都被拒。
- 记号档：一批 40 个操作期间看到的都是 `in-flight`，结束后删掉；忙碌时第二批回 `busy`；取消一批 50 笔，做完 15 笔就停（`stopped=cancelled`），之后没有残留记号，下一批正常。
  - 指向不存在的 Krita 实例的旧记号：重启 gate 后第一个请求就自动清掉。
  - 坏掉的记号：一直回「a person must check…」；照上面的手动步骤（停 gate、重启 krita、删记号、启 gate）后恢复。
- 新发现与处理：
  - Krita 写出的 `.kra`、`.png` 权限是 `660`（当时 `UMask=0007`），dsh（在 kritaio 群组）能改写已经输出的档案；dsh 不能在资料夹里新建档（资料夹是 2750）。
    - 处理（Rin 同意）：krita.service 改成 `UMask=0027`，repo 已改；VM 上还是旧档，下次用 `update_krita_files.sh` 装上。staging 只有 krita 用，不受影响。
    - **待实测**（下次启动 Krita 时）：输出应为 `640`；`sudo -u dsh cat` 读得到；`sudo -u dsh sh -c 'echo x >> <档>'` 要被拒。实测结果写在这里。
  - Krita 停掉后，discovery 档还留着（停用时可以留着），所以「有没有 discovery 档」不能拿来判断就绪；一律以 gate 回 `ok` 判断（启动步骤已照这个改）。
- 监控（guard 每 5 秒，本阶段 104 笔）：没有触发停止；可用记忆体最低 3144 MB；CPU 压力最高 1%。dsh：前后都 active、NRestarts=0、OOM 0、记忆体 917 MB 没变（比阶段 1 高，是她今天画图、聊天后的快取，阶段开始前就是这个数）。这段时间群里没讯息，量不到回话延迟。
- 收尾：krita.service、krita-gate 都停掉、disabled；没有 krita 程序、Xvfb、`krita-probe-*` 单元；没有残留记号档。`out/measure-1102-051100/` 留着（量测的 .kra、PNG、预览）。

### 阶段 2：启动服务

```sh
sudo systemctl start krita.service krita-gate.service
```

**就绪怎么判断**：不要看 discovery 档。Krita 停掉后旧的 discovery 档会留着，有档不代表这一次的 Krita 已经起来。一律经 gate 送一个只读请求，回 `ok` 才算就绪（通常 30–40 秒）：

```sh
for i in $(seq 1 30); do
  sudo -u dsh python3 -I -c "import json,socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/krita-gate/gate.sock');s.sendall(b'{\"job\":\"ping-0001\",\"ops\":[{\"command\":\"list_documents\"}]}\n');print(s.makefile().readline().strip())" 2>/dev/null | grep -q '"ok": true' && { echo ready; break; }
  sleep 3
done
```

- 回 `expected exactly one running Krita, found 0`，或连不上 socket：还没就绪，继续等。
- 超过 90 秒还没 `ready`：看 `journalctl -u krita -n 50` 和 `journalctl -u krita-gate -n 50`，停下回报。

- 新加的群组对 `sudo -u dsh` 立即生效；dsh 服务本身要等下次重启才拿到群组，原型阶段用不到。
- 安全检查（都要被拒）：
  - `sudo -u dsh ls /var/lib/krita/.local/state/krita6-mcp`；
  - `sudo -u dsh touch /srv/dsh/krita-io/out/x`；
  - 没有 token 时，`sudo -u dsh curl -s http://127.0.0.1:<埠>/v1/status`。
- 再跑一次第 1 阶段过关标准第 2 项（对真正的服务做验证）。
- staging 的写入权限也要实机核对（全部是只读指令）：
  - `sudo stat -c '%U:%G %a' /var/lib/krita/staging /var/lib/krita/gate` 都要是 `krita:krita 700`；
  - `sudo -u dsh test -w /var/lib/krita/staging; echo $?` 要是 `1`（dsh 不能写）；
  - `systemctl show krita-gate -p ReadWritePaths -p ReadOnlyPaths`：可写的只有 `/var/lib/krita/staging /var/lib/krita/gate`，`/var/lib/krita` 是只读；
  - `sudo nsenter -t $(systemctl show -p MainPID --value krita-gate) -m findmnt -no OPTIONS -T /var/lib/krita/staging` 要含 `rw`，换成 `-T /var/lib/krita/.local/state` 要含 `ro`；
  - `sudo nsenter -t $(systemctl show -p MainPID --value krita) -m findmnt -no OPTIONS -T /var/lib/krita/staging` 要含 `rw`（Krita 要读 staging 的副本；它对 staging 有写入权，是 KRITA.md 写明的接受风险）。

### 阶段 3：量测

```sh
sudo -u dsh python3 -I /opt/krita-gate/measure_krita.py > /srv/dsh/discord/draw/krita-measure.json
```

量的是：

- 单笔（10、100、500 点，笔刷 8 和 60 像素）、一批 30 笔与 50 笔、预览、存 .kra 和 PNG 的毫秒数；
- Krita 的内存峰值。

最后把 `out/measure-*/measure.png` 拿来肉眼看，确认笔触是真笔刷。

## 退路（Iris 已授权直接用，用了要记下来）

- AppImage 载不了 PyQt6 外挂：改用上游测过的 6.0.3 组合（要另找可信的 6.0.3 版本与校验值）。
- Xvfb 不稳：在 `kritarc` 关掉画布 OpenGL 加速再试。
- 太吃内存，或监控一直触发：停掉 Krita，保留 whaledraw 当主力。
