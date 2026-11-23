"""单向、只读地把 VM 102 上大肥鱼的记忆「完整镜像」到大肥鱼替身的工作区（memory 目录）。

  python pull_memory.py --target <替身工作区>\\memory --dry-run     只数数量与大小，什么都不拷
  python pull_memory.py --target <替身工作区>\\memory               同步一次（整个 memory\\ 一次原子换上去；没有变化就不动它）

设计（老板 2026-11-08：「就是要完全複製並且同步」；详见 PLAN.md）：
- 完整镜像，不做内容过滤：线上的她记得什么，替身就记得什么，路径照原样放在 memory\\social-runtime\\ 底下——
  原文帐本（含私讯、被删除的讯息与它的删除标记、编辑纪录）、记忆记录（含 recall／forget、所有范围）、人物卡、日记、长期事实、
  image-memory.jsonl、feedings.json。档案逐字节照拷。
- 不拷的只有不属于记忆的东西：凭证与设定、费用与用量、会话与内部状态、监控、备份、媒体库（清单与理由在 PLAN.md）。只认白名单路径，
  白名单之外的名字不拷、只回报数量与名字。
- 对 VM 只读：VM 上跑的是下面的 REMOTE（经 ssh 的标准输入送过去，VM 上不落地任何档案），只用 os.open(O_RDONLY|O_NOFOLLOW) 读，
  没有任何写入、删除、更名、子进程、网络；test_pull_memory.py 用 AST 逐项检查，改了 REMOTE 就会失败。
  默认再套一层 systemd 沙盒（以 dsh 帐号、/var/lib/dsh 以 ReadOnlyPaths 挂成唯读、无网络、无提权）：就算程式有 bug，写入也会被核心拒绝。
- 来源缺失，或 archive／memory／notes 任何一类是空的：中止，不替换。档案数／大小比上次少：archive 与 memory 一点都不许少
  （只追加的资料），notes 留 5%／10% 的空间，其余也不许少档案；确认是预期的（例如从备份还原）才加 --allow-shrink。
- 本机这边：同一时间只允许一个拉取（独占锁）；动手前先确认现有的 memory\\ 完整（顶层只能有 social-runtime、README.md、SYNC.json，
  SYNC.json 在、每个档案的 sha256 都对得上、没有任何不明的档案／目录／符号链接）；不完整就停下要人处理，并列出不明项目的名称；
  memory\\ 不存在而旧副本存在也停下；收到的东西逐档核对 sha256 与清单、白名单路径之外一律拒绝；
  整个 memory\\（含 SYNC.json 与 README）先在隔壁的暂存目录建好、权限收紧，再一次换上去（旧目录改名、新目录改名到位，失败就还原；
  还原也失败时旧资料与新资料都留着，不会被任何清理删掉）；内容和上次完全一样就不换（也不动 memory\\）；目标只接受替身 agent 的 memory 目录。
- 来源要是「同一条线」（2026-11-10 的事故：同一个 IP 背后出现了两台一样的机器，一台是十几天前的旧副本）：每次同步 VM 回报 machine-id 与开机识别码
  （各取杂凑前 16 码，不传原值）。machine-id 变了、或开机识别码回到较早的一次（两台机器在轮流回应），就中止；另外「来源连续性」检查：上次同步的
  archive／sessions／images 每个档案，这次必须还在、而且旧内容是新内容的开头（只追加）；memory 记录有就必须一模一样。出现倒退或分岔就中止，
  不替换（档案数／大小的门槛挡不住「旧副本档案刚好更多」或「同样多但内容不同」，这个检查可以）。人确认之后才能加 --accept-source-change。
- 不设排程、不自己重试；要同步就手动执行（排程由 OpenBot 的 routine 负责）。

连线设定与 vmssh 用的是同一份 .env（DSH_SSH_*），不印出来、不写进任何输出。
"""

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
SOURCE_ROOT = "/var/lib/dsh/.dsh/social-runtime"
README_TEMPLATE = HERE / "AVATAR-README.md"
AVATAR_AGENT = "agent-0d3a41de-06ad-403a-b9c3-f774686407fc"      # 大肥鱼替身；只接受它的 memory 目录
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MIRROR = "social-runtime"                                          # VM 上 social-runtime 的镜像，路径照 VM 上的原样
SESSIONS = "sessions"                                              # VM 上 $DSH_HOME/sessions 的镜像（拥有者私讯与群聊的完整会话纪录）
MIRROR_DIRS = (MIRROR, SESSIONS)
EXPECTED_TOP = (*MIRROR_DIRS, "README.md", "SYNC.json")            # memory\ 顶层只能有这几样，多出任何东西都中止
REQUIRED_PARTS = ("archive", "memory", "notes", "sessions")        # 这几类任何一类是空的，都当成来源出了问题
PARTS = (*REQUIRED_PARTS, "images", "state")
# 每一类允许比上次少多少（档案数比例, 大小比例）。依线上的写入方式分类（Rin 第 1 轮 A）：
# - 只追加、不会变小：archive（帐本日档，appendFileSync）、memory（记忆记录，一档一次写好）、images（image-memory.jsonl，appendFile）、
#   sessions（会话纪录，zstd 帧一帧一帧接上去，seq 连号）→ 一点都不许少；
# - 整档重写、会变短也可能被人手改：notes（人物卡、日记、事实，站长可以直接改）→ 留 5%／10%；
# - 会变动的状态档：state（archive/.backfill.json 的失败清单变短、feedings.json 的 `/投餵 删`）→ 不看档案数与大小，只要求存在时是合法 JSON。
SHRINK_LIMITS = {"archive": (1.0, 1.0), "memory": (1.0, 1.0), "notes": (0.95, 0.90), "images": (1.0, 1.0), "sessions": (1.0, 1.0), "state": (0.0, 0.0)}
# 来源连续性（只看上次同步里已有的档案）：只追加的几类，旧内容必须是新内容的开头；memory 记录一档一次写好，有就必须一模一样。
# notes 会被整档重写（人物卡、日记、事实）、state 会变短，不在其中。
APPEND_ONLY_PARTS = ("archive", "sessions", "images")
WRITE_ONCE_PARTS = ("memory",)
BOOT_HISTORY = 50                                                  # SYNC.json 里记住最近多少次不同的开机

# ---------------------------------------------------------------------------
# 跑在 VM 上的程式。只读。任何修改都要过 test_pull_memory.py 的静态检查。
# ---------------------------------------------------------------------------
REMOTE = r'''
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile

MODE, ROOT = sys.argv[1], sys.argv[2].rstrip("/")
DSH = os.path.dirname(ROOT)
SESSIONS = DSH + "/sessions"
SCOPE = re.compile(r"[0-9a-f]{32}\Z")
UUID_MD = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md\Z")
DAY_JSONL = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl\Z")
DAY_MD = re.compile(r"\d{4}-\d{2}-\d{2}\.md\Z")
CARD_MD = re.compile(r"\d{5,25}\.md\Z")
BACKFILL = re.compile(r"\.backfill\.json\Z")
SLUG = re.compile(r"--[A-Za-z0-9._-]+--\Z")
SESSION_DIR = re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
SESSION_LOG = re.compile(r"session\.v\d+\.jsonl\.zstd\Z")
LIMIT = 64 * 1024 * 1024
HANDLED_ROOT = ("archive", "memory", "notes", "image-memory.jsonl", "feedings.json")
# 刻意不拷的东西（不是记忆：费用用量、运行状态、监控、测试收发匣）：回报名字，不算「未知」。
EXCLUDED_ROOT = ("usage", "spend-state.json", "discord-actions.jsonl", "session-scopes.json", "doctor.json", "vital-events.jsonl", "model-migration-probe.json", "style-eval")
STATE_FILES = ("social-runtime/archive/.backfill.json", "social-runtime/feedings.json")
files = {}
stats = {"skipped_names": 0, "skipped_examples": [], "excluded_by_design": [], "transient_names": 0, "session_locks_not_copied": 0,
         "vanished_files": 0, "read_retries": 0, "unstable_files": 0, "absent_optional": [], "ledger_files_without_final_newline": 0}


def shown(path):
    return path[len(DSH) + 1:]


def fingerprint(path):
    # 来源身分：machine-id 与开机识别码（boot_id）各取杂凑的前 16 码，不传原值。这是几十个位元组的 etc／proc 小档，size 报 0，不走 read()。
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        raw = os.read(fd, 256).strip()
    finally:
        os.close(fd)
    return hashlib.sha256(raw).hexdigest()[:16] if raw else None


SOURCE = {"machine_id": fingerprint("/etc/machine-id"), "boot_id": fingerprint("/proc/sys/kernel/random/boot_id")}


def read(path):
    # 档案在列出之后、读取之前被线上程式换掉或删掉是正常的（暂存档改名、整理）：略过并计数，不是错误。
    # 读的时候档案变长了（线上正在追加）就重读，最多三次；三次都在变就用最后一次读到的并计数。
    data = b""
    for attempt in range(3):
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            stats["vanished_files"] += 1
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                raise ValueError("not a regular file or too large: " + os.path.basename(path))
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            if os.fstat(fd).st_size == len(data):
                return data
        finally:
            os.close(fd)
        stats["read_retries"] += 1
    stats["unstable_files"] += 1
    return data


def names(path):
    # 来源目录不存在就是出了问题（改了位置、权限、还没建好）：中止，不要当成「没有资料」。
    # 每一层目录、目录里的每个名字都用 lstat 看过：符号链接一律中止，绝不跟进去（也不只是略过）。
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            sys.exit("symlink in source: " + shown(path))
        listing = sorted(os.listdir(path))
        for name in listing:
            try:
                if stat.S_ISLNK(os.lstat(path + "/" + name).st_mode):
                    sys.exit("symlink in source: " + shown(path + "/" + name))
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        sys.exit("source directory missing: " + shown(path))
    except NotADirectoryError:
        sys.exit("source is not a directory: " + shown(path))
    return listing


def skip(rel):
    stats["skipped_names"] += 1
    if len(stats["skipped_examples"]) < 40:
        stats["skipped_examples"].append(rel)


def keep(rel, data):
    if rel in STATE_FILES:
        # 会变动的状态档：大小不管，但存在时必须解析得了（写到一半或坏掉就中止，下次再来）。
        try:
            json.loads(data.decode("utf-8"))
        except ValueError:
            sys.exit("state file is not valid JSON: " + rel)
    files[rel] = data


def take(folder, pattern, prefix):
    for name in names(folder):
        if not pattern.match(name):
            skip(prefix + "/" + name)
            continue
        data = read(folder + "/" + name)
        if data is not None:
            keep(prefix + "/" + name, data)


# social-runtime 根目录：每个名字都要有去处——拷、刻意不拷（回报名字）、或「未知」（回报，以后线上新增的类别不会被默默漏掉）。
root_names = names(ROOT)
for name in root_names:
    if name in HANDLED_ROOT:
        continue
    if name in EXCLUDED_ROOT:
        stats["excluded_by_design"].append(name)
    elif name.endswith((".part", ".tmp")):
        stats["transient_names"] += 1
    else:
        skip("social-runtime/" + name)
for name in HANDLED_ROOT:
    if name not in root_names:
        if name in ("archive", "memory", "notes"):
            sys.exit("source directory missing: social-runtime/" + name)
        stats["absent_optional"].append(name)
# 原文帐本：每一天一个档案，一行一个 JSON；逐字节照拷（含私讯、被删除的讯息、删除标记、编辑纪录）。
for name in names(ROOT + "/archive"):
    if DAY_JSONL.match(name) or BACKFILL.match(name):
        data = read(ROOT + "/archive/" + name)
        if data is not None:
            keep("social-runtime/archive/" + name, data)
            if DAY_JSONL.match(name) and data and not data.endswith(b"\n"):
                stats["ledger_files_without_final_newline"] += 1
    else:
        skip("social-runtime/archive/" + name)
# 记忆记录：所有范围（群聊、拥有者私讯…），每个范围一个以杂凑命名的目录；recall／forget 也是记录，一起照拷。
for scope in names(ROOT + "/memory"):
    if SCOPE.match(scope) and stat.S_ISDIR(os.lstat(ROOT + "/memory/" + scope).st_mode):
        take(ROOT + "/memory/" + scope, UUID_MD, "social-runtime/memory/" + scope)
    else:
        skip("social-runtime/memory/" + scope)
# 人物卡、日记、长期事实（整个档案，每一节都在）。
notes_names = names(ROOT + "/notes")
for required in ("people", "journal", "facts.md"):
    if required not in notes_names:
        sys.exit("source missing: social-runtime/notes/" + required)
for name in notes_names:
    if name == "people":
        take(ROOT + "/notes/people", CARD_MD, "social-runtime/notes/people")
    elif name == "journal":
        take(ROOT + "/notes/journal", DAY_MD, "social-runtime/notes/journal")
    elif name == "facts.md":
        data = read(ROOT + "/notes/facts.md")
        if data is None:
            sys.exit("source file missing: social-runtime/notes/facts.md")
        keep("social-runtime/notes/facts.md", data)
    else:
        skip("social-runtime/notes/" + name)
# 图片记忆与投餵名单；这两个可能还不存在（没看过图、没人投餵），不存在不是错误。
for name in ("image-memory.jsonl", "feedings.json"):
    if name in root_names:
        data = read(ROOT + "/" + name)
        if data is None:
            stats["absent_optional"].append(name)
        else:
            keep("social-runtime/" + name, data)
# 会话纪录：sessions/<cwd 代号>/session-<uuid>/session.v4.jsonl.zstd（拥有者私讯与群聊的完整对话，zstd 帧一帧一帧接上去，只追加）；
# 空的 session.lock 是运行时的锁，不拷。
for slug in names(SESSIONS):
    if not SLUG.match(slug):
        skip("sessions/" + slug)
        continue
    for session in names(SESSIONS + "/" + slug):
        if not SESSION_DIR.match(session):
            skip("sessions/" + slug + "/" + session)
            continue
        for name in names(SESSIONS + "/" + slug + "/" + session):
            if SESSION_LOG.match(name):
                data = read(SESSIONS + "/" + slug + "/" + session + "/" + name)
                if data is not None:
                    keep("sessions/" + slug + "/" + session + "/" + name, data)
            elif name == "session.lock":
                stats["session_locks_not_copied"] += 1
            else:
                skip("sessions/" + slug + "/" + session + "/" + name)


def part_of(path):
    if path.startswith("sessions/"):
        return "sessions"
    if path in STATE_FILES:
        return "state"
    first = path.split("/")[1]
    if first == "image-memory.jsonl":
        return "images"
    return first if first in ("archive", "memory", "notes") else "state"


by_part = {}
for path, data in files.items():
    row = by_part.setdefault(part_of(path), {"files": 0, "bytes": 0})
    row["files"] += 1
    row["bytes"] += len(data)
stats["excluded_by_design"].sort()
summary = {"files": len(files), "bytes": sum(len(v) for v in files.values()), "by_part": by_part}
if MODE == "dry":
    sys.stdout.write(json.dumps({"summary": summary, "stats": stats, "source": SOURCE}, sort_keys=True) + "\n")
else:
    manifest = {"format": 1, "files": {k: {"sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} for k, v in sorted(files.items())}, "summary": summary, "stats": stats, "source": SOURCE}
    buffer = sys.stdout.buffer
    with tarfile.open(fileobj=buffer, mode="w|") as archive:
        for path, data in list(sorted(files.items())) + [("MANIFEST.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))]:
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            archive.addfile(info, io.BytesIO(data))
'''

ALLOWED_PATH = re.compile(
    r"(social-runtime/(archive/\d{4}-\d{2}-\d{2}\.jsonl"
    r"|archive/\.backfill\.json"
    r"|memory/[0-9a-f]{32}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md"
    r"|notes/people/\d{5,25}\.md"
    r"|notes/journal/\d{4}-\d{2}-\d{2}\.md"
    r"|notes/facts\.md"
    r"|image-memory\.jsonl"
    r"|feedings\.json)"
    r"|sessions/--[A-Za-z0-9._-]+--/session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/session\.v\d+\.jsonl\.zstd)\Z"
)


class PullError(RuntimeError):
    pass


class SwapFailed(PullError):
    """换上去失败、而且还原也失败：旧资料与新资料都还在原处，必须由人处理，不能被任何清理删掉。"""


# ---------------------------------------------------------------------------
# 连线
# ---------------------------------------------------------------------------
def load_env(path):
    config = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    return config


def remote_command(mode, sandbox=True, root=SOURCE_ROOT):
    inner = f"python3 -B -I - {mode} {root}"
    if not sandbox:
        # 紧急用：以 root 直接读，只剩「程式只读」那一层保证，O_NOFOLLOW 也只保护路径的最后一段。
        return "sudo -n " + inner
    # 以 dsh 帐号跑；/var/lib/dsh 在这个单元里是唯读挂载，写入会被核心拒绝；没有网络、不能提权、没有任何额外权限。
    return ("sudo -n systemd-run --quiet --pipe --wait --collect -p User=dsh -p NoNewPrivileges=yes -p ProtectSystem=strict "
            "-p ProtectHome=yes -p PrivateTmp=yes -p PrivateNetwork=yes -p CapabilityBoundingSet= -p ReadOnlyPaths=/var/lib/dsh " + inner)


def ssh_argv(config, command, repo=REPO):
    known = pathlib.Path(config["DSH_KNOWN_HOSTS"])
    if not known.is_absolute():
        known = repo / known
    return ["ssh", "-J", config["DSH_SSH_JUMP"], "-i", config["DSH_SSH_KEY"], "-o", "ForwardAgent=no", "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known),
            config["DSH_SSH_TARGET"], command]


# ---------------------------------------------------------------------------
# 收下来的东西：只认白名单路径，逐档核对
# ---------------------------------------------------------------------------
def receive(stream, limit_file=MAX_FILE, limit_total=MAX_TOTAL):
    files, manifest, total = {}, None, 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if not member.isreg():
                raise PullError(f"收到的不是一般档案：{member.name!r}")
            if member.size > limit_file:
                raise PullError(f"档案太大：{member.name!r}")
            if manifest is not None:
                raise PullError("清单之后还有档案")
            data = archive.extractfile(member).read()
            total += len(data)
            if total > limit_total:
                raise PullError("总量超过上限")
            if member.name == "MANIFEST.json":
                manifest = json.loads(data.decode("utf-8"))
            elif ALLOWED_PATH.match(member.name):
                if member.name in files:
                    raise PullError(f"重复的路径：{member.name!r}")
                files[member.name] = data
            else:
                raise PullError(f"不在白名单里的路径：{member.name!r}")
    if manifest is None or manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise PullError("没有收到清单，或清单格式不对")
    listed = manifest["files"]
    if set(listed) != set(files):
        raise PullError("清单和实际收到的档案不一致")
    for path, data in files.items():
        entry = listed[path]
        if entry.get("bytes") != len(data) or entry.get("sha256") != hashlib.sha256(data).hexdigest():
            raise PullError(f"sha256 对不上：{path!r}")
    return files, manifest


def part_of(path):
    """和 REMOTE 里的 part_of 完全一样（有测试比对两边的结果）。"""
    if path.startswith("sessions/"):
        return "sessions"
    if path in ("social-runtime/archive/.backfill.json", "social-runtime/feedings.json"):
        return "state"
    first = path.split("/")[1]
    if first == "image-memory.jsonl":
        return "images"
    return first if first in ("archive", "memory", "notes") else "state"


def part_summary(files):
    rows = {part: {"files": 0, "bytes": 0} for part in PARTS}
    for path, data in files.items():
        rows[part_of(path)]["files"] += 1
        rows[part_of(path)]["bytes"] += len(data)
    return rows


def scrub(text, config=None):
    """VM 端回来的错误文字要印出来、也会被排程转寄：连线设定的值、它的每一个组成部分（user@host 的使用者与主机名称、跳板主机、
    金钥与 known_hosts 的路径各段）和 IPv4 位址都不能出现在里面。ssh 常常只回显主机名称，不会带 user@ 前缀。"""
    tokens = set()
    for value in (config or {}).values():
        if isinstance(value, str):
            tokens.add(value)
            tokens.update(part for part in re.split(r"[@:,\s/\\]+", value))
    # 先遮长的（完整的值），再遮短的（各段）；太短的段（1～2 个字元）会误伤正常文字，不遮。
    for token in sorted((t for t in tokens if len(t) >= 3), key=len, reverse=True):
        text = re.sub(re.escape(token), "<连线设定>", text, flags=re.IGNORECASE)
    return re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", text)


def check_source(previous, source, accept=False, now=None):
    """来源是不是上次的那一台。回传这次的身分（含是否换了一次开机），以及要写进 SYNC.json 的 source 记录。
    - 读不到身分：中止（无从确认）。
    - machine-id 和上次不同：不是同一台机器，中止。
    - 开机识别码不同：正常重开机就会变，放行并标示 boot_changed；但「回到较早的一次开机」不会发生在一台机器上，那代表两台机器在轮流回应，中止。
    - 上次的 SYNC.json 还没有 source（这个检查上线之前的同步）：只记录，不比对。"""
    ok = isinstance(source, dict) and all(isinstance(source.get(key), str) and re.fullmatch(r"[0-9a-f]{16}", source[key]) for key in ("machine_id", "boot_id"))
    if not ok:
        raise PullError("来源没有回报身分（machine-id 或开机识别码读不到）：无法确认连到的是同一台机器，中止，本机的旧资料不动")
    stamp = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    info = {"machine_id": source["machine_id"], "boot_id": source["boot_id"], "boot_changed": False, "first_seen": False}
    old = (previous or {}).get("source") if previous else None
    boots = []
    if isinstance(old, dict) and isinstance(old.get("machine_id"), str) and isinstance(old.get("boot_id"), str):
        boots = [row for row in old.get("boots", []) if isinstance(row, dict) and isinstance(row.get("boot_id"), str)]
        if not boots:
            boots = [{"boot_id": old["boot_id"], "first_seen_utc": old.get("recorded_utc")}]
        if old["machine_id"] != source["machine_id"] and not accept:
            raise PullError(f"来源的 machine-id 和上次同步的不同（上次 {old['machine_id']}，这次 {source['machine_id']}）：不是同一台机器，中止，不替换。"
                            "人确认来源之后才能加 --accept-source-change")
        if source["boot_id"] != old["boot_id"]:
            if source["boot_id"] in {row["boot_id"] for row in boots} and not accept:
                raise PullError(f"来源回到了较早的一次开机（开机识别码 {source['boot_id']} 以前见过，上次是 {old['boot_id']}）：一台机器不会这样，"
                                "多半是同一个 IP 背后有两台机器在轮流回应。中止，不替换。人确认之后才能加 --accept-source-change")
            info["boot_changed"] = True
    else:
        info["first_seen"] = True
    if not boots or boots[-1]["boot_id"] != source["boot_id"]:
        boots.append({"boot_id": source["boot_id"], "first_seen_utc": stamp})
    record = {"machine_id": source["machine_id"], "boot_id": source["boot_id"], "boots": boots[-BOOT_HISTORY:], "recorded_utc": stamp}
    return info, record


def check_continuity(target, previous, files, accept=False):
    """来源连续性：上次同步里已有的、「只增不改」的档案，这次必须还在，而且旧内容是新内容的开头（memory 记录则必须一模一样）。
    挡的是「连到了另一份资料」——例如十几天前的旧副本（档案少、或同一天的帐本内容对不上）、从备份还原的、分岔出去的副本。
    回传统计；有对不上的就中止（--accept-source-change 只放行，不隐瞒：violations 照样回报）。"""
    stats = {"checked": 0, "unchanged": 0, "appended": 0, "violations": 0}
    if previous is None:
        return stats
    problems = []
    for path, old_sha in sorted(previous["files"].items()):
        part = part_of(path)
        if part not in APPEND_ONLY_PARTS and part not in WRITE_ONCE_PARTS:
            continue
        stats["checked"] += 1
        new = files.get(path)
        if new is None:
            problems.append((path, "来源上没有这个档案了"))
        elif hashlib.sha256(new).hexdigest() == old_sha:
            stats["unchanged"] += 1
        elif part in WRITE_ONCE_PARTS:
            problems.append((path, "记录的内容被改过"))
        else:
            old = (target / path).read_bytes()               # preflight 已确认本机的 sha256 都对得上
            if len(new) < len(old):
                problems.append((path, f"变短了（{len(old)}→{len(new)} 位元组）"))
            elif new[:len(old)] != old:
                problems.append((path, "不是原来内容的延续（开头的位元组对不上）"))
            else:
                stats["appended"] += 1
    stats["violations"] = len(problems)
    if problems and not accept:
        shown = "；".join(f"{path}：{why}" for path, why in problems[:5])
        raise PullError(f"来源和上次同步的不是同一条线：{len(problems)} 个应该只增不改的档案对不上（例：{shown}"
                        f"{'…' if len(problems) > 5 else ''}）。可能连到了另一份资料（旧副本、备份还原、分岔的副本），中止，不替换。"
                        "人确认来源之后才能加 --accept-source-change")
    return stats


def check_not_empty_or_shrunk(files, previous=None, allow_shrink=False):
    """空的一律中止（来源出了问题，不是『没有资料』）；比上次少也中止（各类的容许度见 SHRINK_LIMITS），除非明讲 --allow-shrink。"""
    now = part_summary(files)
    for part in REQUIRED_PARTS:
        if now[part]["files"] == 0:
            raise PullError(f"{part} 是空的：来源可能缺失或位置变了，中止，本机的旧资料不动")
    if previous:
        before = previous.get("summary", {}).get("by_part", {})
        for part, row in now.items():
            old = before.get(part)
            if not old:
                continue
            files_ratio, bytes_ratio = SHRINK_LIMITS[part]
            if (row["files"] < old["files"] * files_ratio or row["bytes"] < old["bytes"] * bytes_ratio) and not allow_shrink:
                raise PullError(f"{part} 比上次少（档案 {old['files']}→{row['files']}，大小 {old['bytes']}→{row['bytes']}；"
                                f"门槛 {files_ratio:.0%}／{bytes_ratio:.0%}）：中止。这类资料只会增加，少了通常是出问题（例如从备份还原就会这样）。"
                                f"确认是预期的话加 --allow-shrink")
    return now


# ---------------------------------------------------------------------------
# 目标、锁、现状检查、原子换上去
# ---------------------------------------------------------------------------
def check_target(target):
    """只接受大肥鱼替身的 memory 目录：…\\OpenBot\\Agents\\<替身 agent>\\memory，其他一律拒绝（包括别的 agent）。"""
    target = pathlib.Path(target)
    resolved = target.resolve()
    same = os.path.normcase(str(resolved)) == os.path.normcase(os.path.abspath(target))     # 路径上有符号链接或接合点就对不上
    if not same or tuple(resolved.parts[-4:]) != ("OpenBot", "Agents", AVATAR_AGENT, "memory"):
        raise PullError("目标必须正好是替身工作区的 memory 目录（…\\OpenBot\\Agents\\" + AVATAR_AGENT + "\\memory）：" + str(target))
    if not resolved.parent.is_dir():
        raise PullError("替身的工作区不存在：" + str(resolved.parent))
    if resolved.exists() and (not resolved.is_dir() or os.path.islink(resolved)):
        raise PullError("目标不是一般目录（符号链接或档案）：" + str(resolved))
    return resolved


@contextlib.contextmanager
def sync_lock(target):
    """独占锁：同一时间只允许一个拉取。锁档不会被自动清掉（程式被强制结束时会留下），那时人确认没有别的拉取后手动删。"""
    path = target.with_name(".memory-sync.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise PullError(f"另一个拉取正在进行，或上一次被强制结束留下了锁档：{path}。确认没有别的拉取在跑之后，手动删除这个档案再试") from None
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode())
        os.close(fd)
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def restrict_to_current_user(path):
    """目录只留本机这个使用者。回传一行说明。"""
    if os.name == "nt":
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ["USERNAME"]
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"], check=True, capture_output=True)
        listing = subprocess.run(["icacls", str(path)], check=True, capture_output=True, text=True).stdout
        others = [line for line in listing.splitlines() if ":(" in line and user.lower() not in line.lower()]
        if others:
            raise PullError("权限没有限缩成只剩本机使用者")
        return "ACL 只剩本机使用者"
    os.chmod(path, 0o700)
    return "权限 700"


def write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:
        handle.write(data)


def build_staging(files, manifest, staging, sync_extra):
    """把整个 memory\\ 在暂存目录里建好：镜像（social-runtime\\、sessions\\）、README、SYNC.json（含每个档案的 sha256，之后用来确认它完整）。"""
    staging.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        write_file(staging / path, data)
    sync = {"synced_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "direction": "VM 102 -> 本机替身工作区（单向、只读的完整镜像）",
            "summary": {**manifest["summary"], "by_part": part_summary(files)}, "stats": manifest["stats"],
            "files": {path: entry["sha256"] for path, entry in sorted(manifest["files"].items())}, **sync_extra}
    write_file(staging / "SYNC.json", json.dumps(sync, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
    if README_TEMPLATE.exists():
        write_file(staging / "README.md", README_TEMPLATE.read_bytes())
    return sync


def check_top_level(target):
    """memory\\ 顶层只能有 social-runtime（目录）、README.md、SYNC.json（档案）。多出任何东西（不明的档案、目录、符号链接）就中止，
    列出它们的名称：整个目录会被换掉，放在这里的东西会被悄悄丢掉。"""
    try:
        items = sorted(target.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise PullError(f"memory\\ 读不了：{error.strerror}。请人处理") from None

    def kind(item):
        return "符号链接" if item.is_symlink() else "目录" if item.is_dir() else "档案" if item.is_file() else "其他"
    unknown = [f"{item.name}（{kind(item)}）" for item in items if item.name not in EXPECTED_TOP]
    wrong = [f"{item.name}（应该是{'目录' if item.name in MIRROR_DIRS else '档案'}，实际是{kind(item)}）" for item in items
             if item.name in EXPECTED_TOP and (item.is_symlink() or item.is_dir() != (item.name in MIRROR_DIRS))]
    if unknown or wrong:
        shown = unknown + wrong
        raise PullError(f"memory\\ 顶层有不在预期名单里的东西（预期只有 {'、'.join(EXPECTED_TOP)}）：{'、'.join(shown[:20])}"
                        f"{'…共 ' + str(len(shown)) + ' 项' if len(shown) > 20 else ''}。"
                        "换上去会把它们丢掉：中止，不替换也不清理。请人把它们搬走或确认之后再同步")


def verify_complete(target):
    """现有的 memory\\ 是不是完整、可读：顶层只有预期的三样，SYNC.json 在而且格式对，档案集合与 sha256 跟 SYNC.json 记的一模一样，
    镜像里没有任何不明的档案、目录、符号链接。回传 SYNC.json 的内容；不对就丢 PullError（要人处理，脚本不会覆盖一个看不出是什么状态的目录）。"""
    check_top_level(target)
    try:
        sync = json.loads((target / "SYNC.json").read_text(encoding="utf-8"))
        listed = sync["files"]
        by_part = sync["summary"]["by_part"]
        if not isinstance(listed, dict) or not isinstance(by_part, dict):
            raise ValueError("格式不对")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PullError(f"memory\\ 已经存在，但 SYNC.json 缺失或坏掉（{type(error).__name__}）：不当成第一次拉取，也不覆盖。请人确认这个目录的状态") from None
    present, unknown = {}, []
    expected_dirs = {*MIRROR_DIRS, *(pathlib.PurePosixPath(path).parent.as_posix() for path in listed)}
    expected_dirs |= {parent.as_posix() for path in listed for parent in pathlib.PurePosixPath(path).parents if parent.as_posix() != "."}
    for top in MIRROR_DIRS:
        for folder, dirs, names in os.walk(target / top, followlinks=False):
            here = pathlib.Path(folder)
            for name in dirs[:]:
                item = here / name
                relative = item.relative_to(target).as_posix()
                if item.is_symlink():
                    unknown.append(f"{relative}（符号链接）")
                    dirs.remove(name)
                elif relative not in expected_dirs:
                    unknown.append(f"{relative}（目录）")
            for name in names:
                item = here / name
                relative = item.relative_to(target).as_posix()
                if item.is_symlink() or not item.is_file():
                    unknown.append(f"{relative}（{'符号链接' if item.is_symlink() else '其他'}）")
                    continue
                try:
                    present[relative] = hashlib.sha256(item.read_bytes()).hexdigest()
                except OSError as error:
                    raise PullError(f"memory\\ 里有读不了的档案：{relative}（{error.strerror}）。请人处理") from None
    extra = sorted(set(present) - set(listed))
    unknown += [f"{path}（档案）" for path in extra]
    if unknown:
        raise PullError(f"memory\\ 里有 SYNC.json 没登记的东西：{'、'.join(unknown[:20])}{'…共 ' + str(len(unknown)) + ' 项' if len(unknown) > 20 else ''}。"
                        "换上去会把它们丢掉：中止，不替换也不清理。请人处理")
    if present != listed:
        missing = sorted(set(listed) - set(present))
        changed = sorted(k for k in set(present) & set(listed) if present[k] != listed[k])
        raise PullError(f"memory\\ 的内容和 SYNC.json 记的不一致（缺少 {len(missing)}、被改过 {len(changed)}；例：{(missing or changed)[:3]}）。请人处理，脚本不会覆盖")
    if not (target / "README.md").is_file():
        raise PullError("memory\\ 不完整：缺少 README.md。请人处理")
    return sync


def stale_dirs(target, pattern):
    return [path for path in target.parent.glob(pattern) if path.is_dir() and not os.path.islink(path)]


def preflight(target):
    """动手前先看现状。回传上一次的 SYNC.json（第一次拉取回传 None）。
    - memory\\ 不存在：只有「旁边也没有旧副本」才算第一次；有 .memory-old-* 就停下（可能是上次换上去失败、还原也失败）。
    - memory\\ 存在：必须完整可读（见 verify_complete），SYNC.json 缺失或坏掉不算第一次，一律停下。
    - 确认现有的完整之后，才清掉遗留的暂存目录与旧副本。"""
    if not target.exists():
        olds = stale_dirs(target, ".memory-old-*")
        if olds:
            newest = sorted(olds)[-1]
            raise PullError(f"memory\\ 不存在，但旁边有旧副本 {newest.name}（可能是上次换上去失败、还原也失败）：不清理，停下要人处理。"
                            f"确认之后把它改名成 memory 即可恢复；{'也还有一个暂存目录' if stale_dirs(target, '.memory-incoming-*') else ''}")
        for stale in stale_dirs(target, ".memory-incoming-*"):
            shutil.rmtree(stale, ignore_errors=True)
        return None
    sync = verify_complete(target)
    for stale in stale_dirs(target, ".memory-incoming-*") + stale_dirs(target, ".memory-old-*"):
        shutil.rmtree(stale, ignore_errors=True)
    return sync


def rename(source, destination):
    os.rename(source, destination)


def swap_in(staging, target, stamp):
    """一次换上去：旧目录先改名、新目录改名到位；失败就把旧的改回来，旧内容不动。回传旧目录的路径（要不要删由呼叫端决定）。
    还原也失败时丢 SwapFailed：旧资料在 .memory-old-*，新资料在暂存目录，两个都留着。"""
    old = None
    if target.exists():
        old = target.with_name(f".memory-old-{stamp}")
        rename(target, old)
    try:
        rename(staging, target)
    except BaseException as error:
        if old is not None:
            try:
                rename(old, target)
            except BaseException:
                raise SwapFailed(f"换上去失败，还原也失败：旧资料在 {old.name}，新资料在 {staging.name}，都保留着。"
                                 f"请人处理（确认后把 {old.name} 改名成 memory 即可恢复旧资料）") from error
        raise
    return old


def fetch(args, config):
    """向 VM 要资料（--dry-run 只要数字）。回传 (files, manifest) 或 dry-run 的报告。"""
    mode = "dry" if args.dry_run else "pull"
    argv = ssh_argv(config, remote_command(mode, sandbox=not args.no_sandbox))
    if args.no_sandbox:
        print("警告：--no-sandbox 以 root 直接读，只剩『程式只读』这一层保证；只在紧急时用。", file=sys.stderr)

    def stderr_text(errors):
        errors.seek(0)
        return scrub(errors.read(500).decode("utf-8", "replace").strip(), config)

    with tempfile.TemporaryFile() as errors:
        if args.dry_run:
            process = subprocess.run(argv, input=REMOTE.encode("utf-8"), stdout=subprocess.PIPE, stderr=errors, timeout=180)
            if process.returncode:
                raise PullError("VM 端失败：" + stderr_text(errors))
            return json.loads(process.stdout.decode("utf-8"))
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)

        def died(why):
            """ssh 提前结束或收到的东西读不下来：等它结束（给 5 秒，不结束就杀掉）、读出它自己的 stderr，组成一条带原因与结束码的讯息。"""
            with contextlib.suppress(OSError):
                process.stdin.close()
            try:
                status = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                status = process.wait()
            with contextlib.suppress(OSError):
                process.stdout.close()
            return PullError(f"VM 端失败（{why}，结束码 {status}）：{stderr_text(errors) or 'ssh 没有留下任何错误输出'}")

        try:
            process.stdin.write(REMOTE.encode("utf-8"))
            process.stdin.close()
        except OSError:
            # BrokenPipeError 也是 OSError：ssh 在收到程序之前就结束了（连不上跳板或 VM、认证或主机金钥不对…）。
            # 真正的原因在 ssh 的 stderr；不先读出来，只会看到莫名其妙的 Broken pipe（2026-11-10 的事故就是这样）。
            raise died("ssh 在收到程序之前就结束了") from None
        try:
            result = receive(process.stdout)
        except PullError:
            process.kill()
            raise
        except Exception as error:
            # 管道缓冲区够大时，程序写得进去、ssh 才死：这里收到的是一个空的或断掉的串流（tarfile.ReadError 等），原因同样在 ssh 的 stderr。
            raise died(f"收到的资料读不下来：{type(error).__name__}") from None
        finally:
            with contextlib.suppress(OSError):
                process.stdout.close()
            status = process.wait()
        if status:
            raise PullError("VM 端失败：" + stderr_text(errors))
        return result


def unchanged(target, previous, files):
    """这次抓到的内容和现有的 memory\\ 完全一样（档案集合与 sha256 都相同，README 也和模板相同）吗？"""
    now = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
    if now != previous.get("files"):
        return False
    template = README_TEMPLATE.read_bytes() if README_TEMPLATE.exists() else None
    return template is None or (target / "README.md").read_bytes() == template


def pull(args, config=None):
    target = check_target(args.target)
    config = config if config is not None else load_env(args.env)
    if args.dry_run:
        print(json.dumps(fetch(args, config), ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    with sync_lock(target):
        previous = preflight(target)                 # 本机现状先查清楚，再去碰 VM
        started = time.perf_counter()
        files, manifest = fetch(args, config)
        fetch_seconds = round(time.perf_counter() - started, 1)
        accept = getattr(args, "accept_source_change", False)
        check_not_empty_or_shrunk(files)                                  # 先看是不是空的：空的就说空的，不要说成「不是同一条线」
        source, source_record = check_source(previous, manifest.get("source"), accept)
        continuity = check_continuity(target, previous, files, accept)
        check_not_empty_or_shrunk(files, previous, args.allow_shrink)
        if accept and (continuity["violations"] or source["boot_changed"]):
            print("警告：--accept-source-change 放行了来源的变化（violations 照常回报在输出里）。", file=sys.stderr)
        if previous is not None and unchanged(target, previous, files):
            # 和上次一模一样：不换、不动 memory\（替身可能正开着里面的档案，换了反而会失败）。
            # stats 是「这一次」在 VM 上看到的（skipped_names、unstable_files 等）：内容没变也要让排程看得到有没有新出现的未知名字。
            # source.boot_changed 为真＝VM 重开过机：同步是成功的，但要让人知道（这次没有换，所以开机记录要等下一次真正换上去才会更新）。
            print(json.dumps({"ok": True, "changed": False, "last_change_utc": previous["synced_at_utc"], "summary": previous["summary"],
                              "stats": manifest["stats"], "fetch_seconds": fetch_seconds, "source": source, "continuity": continuity},
                             ensure_ascii=False, indent=1, sort_keys=True))
            return 0
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{os.getpid()}"
        staging = target.with_name(f".memory-incoming-{stamp}")
        old = None
        try:
            build_staging(files, manifest, staging, {"fetch_seconds": fetch_seconds, "source": source_record})
            acl = restrict_to_current_user(staging)
            if target.exists():
                check_top_level(target)              # 抓资料的这几秒里有人放了东西进去的话，换上去就会丢掉它：再看一次
            old = swap_in(staging, target, stamp)
        except SwapFailed:
            raise                                    # 两份资料都留着，不清理
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)   # 删不掉不算失败，下次拉取在确认 memory\ 完整之后会再清
    sync = json.loads((target / "SYNC.json").read_text(encoding="utf-8"))
    print(json.dumps({"ok": True, "changed": True, "acl": acl, "fetch_seconds": fetch_seconds, "source": source, "continuity": continuity,
                      **{k: sync[k] for k in ("synced_at_utc", "summary", "stats")}}, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, help="替身工作区底下的 memory 目录（只接受大肥鱼替身）")
    parser.add_argument("--env", default=str(REPO / ".env"), help="含 DSH_SSH_* 的设定档（预设：repo 根目录的 .env）")
    parser.add_argument("--dry-run", action="store_true", help="只数数量与大小，不拷任何档案")
    parser.add_argument("--no-sandbox", action="store_true", help="紧急用：不套 systemd 唯读沙盒（以 root 读，只剩『程式只读』这一层保证）")
    parser.add_argument("--allow-shrink", action="store_true", help="明讲：这次档案数或大小比上次少很多是预期的")
    parser.add_argument("--accept-source-change", action="store_true",
                        help="人手动用，排程永远不加：明讲来源换了一台机器／回到较早的开机／和上次同步的内容分岔是预期的（例如人确认过后要改认另一份资料）")
    args = parser.parse_args(argv)
    try:
        return pull(args)
    except (PullError, subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        print(f"拒绝／失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
