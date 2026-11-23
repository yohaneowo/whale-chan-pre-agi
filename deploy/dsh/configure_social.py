"""隔离验收通过后，备份并启用群聊、私人管理与记忆用量护栏。"""

import argparse
import datetime
import hashlib
import json
import math
import os
import pathlib
import posixpath
import re
import shutil
import stat
import subprocess
import sys

# 这次要装的那一组套件（部署新版本时才改这三行）。回滚不用改档案：用 --discord / --social / --presets 指到旧版本的目录
# （每一版的目录都留在 /opt/dsh/vendor/），见 deploy/draw/DEPLOY.md 的回滚步骤。
VENDOR = "/opt/dsh/vendor"
ANCHOR = "/"                 # 从这一层往下（含）到套件目录，每一层都要通过检查
TRUSTED_UID = 0
DEFAULT_BUNDLE = {
    "discord": "discord-0.6.0-social.56",
    "social": "social-0.1.50",
    "presets": "social-presets-0.53.0",
}
BUNDLE_NAMES = {
    "discord": re.compile(r"discord-0\.6\.0-social\.[0-9]+\Z"),
    "social": re.compile(r"social-0\.1\.[0-9]+\Z"),
    "presets": re.compile(r"social-presets-0\.[0-9]+\.0\Z"),
}
EXTRA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")   # --stickers / --memes / --huanqi 的目录名

# package.json 要跟目录名对得上（Rin 第 5 轮：vendor 里若有「名字像、内容是别的版本」的目录，以前也能通过并被装进去）。
# 规则取自 VM 上现有 9 个目录的实际 package.json（2026-11-06 只读核对）：
#   discord-0.6.0-social.N  → name "dsh-plugin-discord"，  version "0.6.0-social.N"（目录名去掉 "discord-"）
#   social-0.1.N            → name "dsh-social-runtime"，  version "0.1.N"（去掉 "social-"）
#   social-presets-0.N.0    → name "dsh-social-presets"，  version "0.N.0"（去掉 "social-presets-"）
#   --stickers nekowhale-1.1.1 → name "dsh-nekowhale-stickers"；--memes dsh-meme-0.1.64 → "dsh-meme"；
#   --huanqi huanqi-1.1.0   → "dsh-huanqi"；额外套件的 version 是目录名最后一个「-」后面的 x.y.z。
# 目录名对不上包名的额外套件（例如以后换了别的贴图套件），要先来改这张表，改之前一律拒绝。
PACKAGE_NAMES = {"discord": "dsh-plugin-discord", "social": "dsh-social-runtime", "presets": "dsh-social-presets"}
VERSION_PREFIX = {"discord": "discord-", "social": "social-", "presets": "social-presets-"}
EXTRA_PACKAGE_NAMES = {"--stickers": "dsh-nekowhale-stickers", "--memes": "dsh-meme", "--huanqi": "dsh-huanqi"}
EXTRA_VERSION = re.compile(r".+-([0-9]+\.[0-9]+\.[0-9]+)\Z")
MAX_PACKAGE_BYTES = 65536     # 现有最大的 2919 字节；太大一律当成不对


class BundleError(ValueError):
    pass


def bundle_dir(kind, value=None, vendor=VENDOR):
    """某一类套件的目录（vendor 底下）。value 可以写完整路径或只写目录名；不给就用预设。
    这只是写法检查（名字、位置）；碰到档案系统的检查在 verify_dir。"""
    value = value or DEFAULT_BUNDLE[kind]
    return _direct_child(value, BUNDLE_NAMES[kind], f"--{kind}", vendor)


def extra_dir(value, label, vendor=VENDOR):
    """--stickers / --memes / --huanqi 的目录：同样只收 vendor 的直接子目录。"""
    return _direct_child(value, EXTRA_NAME, label, vendor)


def _direct_child(value, pattern, label, vendor):
    path = value if "/" in value else vendor + "/" + value
    if posixpath.dirname(posixpath.normpath(path)) != vendor or posixpath.normpath(path) != path:
        raise BundleError(f"{label} 要是 {vendor}/ 底下的目录：{value!r}")
    if not pattern.match(posixpath.basename(path)):
        raise BundleError(f"{label} 的目录名不对：{posixpath.basename(path)!r}")
    return path


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise BundleError(f"{path} 不存在") from None
    except OSError as error:
        raise BundleError(f"{path} 读不了（{error.strerror}）") from None


def chain(path, anchor=ANCHOR):
    """从 anchor 一路到 path 的每一层路径（含两端），例如 ['/', '/opt', '/opt/dsh', '/opt/dsh/vendor', '/opt/dsh/vendor/x']。"""
    if path != anchor and not path.startswith(anchor.rstrip("/") + "/"):
        raise BundleError(f"{path} 不在 {anchor} 底下")
    levels = [path]
    while levels[-1] != anchor:
        parent = posixpath.dirname(levels[-1])
        if parent == levels[-1]:
            break
        levels.append(parent)
    return list(reversed(levels))


# dsh 读不读得到：套件里的东西是 root 所有（检查过了），dsh 不是拥有者；它所在的群组也不是这些档案的群组
# （档案的群组是 root）。所以决定它能不能读的就是 other 位：档案要 o+r，目录要 o+rx，
# 套件目录往上的每一层要 o+x（能走进去）。不去查 dsh 在哪些群组：那样检查会依赖 VM 上的帐号设定，
# 而部署时 plugin add 是用 dsh 的身分去读这些档案的，other 位够用才是最稳的判断。
NEED_BITS = {"x": stat.S_IXOTH, "r": stat.S_IROTH, "rx": stat.S_IROTH | stat.S_IXOTH}


def _check_entry(path, info, uid, directory_ok=True, file_ok=True, need="r"):
    if stat.S_ISLNK(info.st_mode):
        raise BundleError(f"{path} 是符号链接；套件里不准有符号链接")
    if not ((directory_ok and stat.S_ISDIR(info.st_mode)) or (file_ok and stat.S_ISREG(info.st_mode))):
        raise BundleError(f"{path} 不是一般档案或目录")
    if info.st_uid != uid:
        raise BundleError(f"{path} 的拥有者不是 root（uid {info.st_uid}）")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise BundleError(f"{path} 的群组或其他人可写（{stat.S_IMODE(info.st_mode):o}）")
    if info.st_mode & NEED_BITS[need] != NEED_BITS[need]:
        raise BundleError(f"{path} 的权限是 {stat.S_IMODE(info.st_mode):o}：dsh 读不到、或走不进去"
                          f"（other 位要有 {need}；plugin add 是用 dsh 的身分读的，等到停服之后才失败就晚了）")


def _reject_constant(name):
    raise ValueError(f"不接受非标准的 JSON 常数 {name}")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"数字 {text} 超出范围（变成无限大）")
    return value


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"重复的键 {key!r}")
        seen[key] = value
    return seen


def strict_json(raw):
    """严格的 JSON 解析（RFC 8259 的 JSON，不收 Python 预设会放行的非标准写法）：
    - 只收严格 UTF-8（不吃 BOM）；
    - NaN、Infinity、-Infinity 一律拒绝（预设的 json.loads 会收，下游的 JavaScript JSON.parse 不收，两边看到的东西不一样）；
      1e999 这种读进来会变成无限大的数字也拒绝；
    - 重复的键一律拒绝：同一个键出现两次时，各家解析器取哪一个不一致（有的取第一个、有的取最后一个），
      我们检查的值和 npm／pnpm 实际用到的值可能不是同一个；正常的 package.json 不会有重复键，没有理由放行；
    - 嵌套太深（RecursionError）也算拒绝。
    不合格丢 ValueError。"""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float,
                          object_pairs_hook=_no_duplicate_keys)
    except RecursionError:
        raise ValueError("嵌套太深") from None
    except UnicodeDecodeError as error:
        raise ValueError(f"不是 UTF-8（{error.reason}）") from None


def read_package(path):
    """读套件的 package.json 并回传 (name, version)；读不了、太大、不是严格的 JSON、栏位不对都丢 BundleError。"""
    try:
        with open(path, "rb") as file:
            raw = file.read(MAX_PACKAGE_BYTES + 1)
    except OSError as error:
        raise BundleError(f"{path} 读不了（{error.strerror}）") from None
    if len(raw) > MAX_PACKAGE_BYTES:
        raise BundleError(f"{path} 太大（超过 {MAX_PACKAGE_BYTES} 字节），不像套件的 package.json")
    try:
        data = strict_json(raw)
    except ValueError as error:
        raise BundleError(f"{path} 不是合法的（严格）UTF-8 JSON（{error}）") from None
    if not isinstance(data, dict):
        raise BundleError(f"{path} 的最外层不是物件")
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
        raise BundleError(f"{path} 的 name 或 version 缺少、或不是字串")
    return name, version


def verify_dir(path, vendor=VENDOR, anchor=ANCHOR, uid=TRUSTED_UID, expect=None):
    """确认 path 是可以拿 root 去装、而且 dsh 读得到的套件目录；通过才回传「解析过的」路径，不通过就丢 BundleError。
    检查：
    - path 解析（realpath）之后跟原样一模一样，所以路径上任何一层都不是符号链接，而且就在 vendor 底下一层；
    - 从 anchor（预设是 /）到套件目录的每一层：root 所有、群组与其他人不能写、是真目录、dsh 走得进去（o+x）；
    - 套件目录里每一个目录与档案：不是符号链接、是一般档案或目录、root 所有、群组与其他人不能写，
      目录 dsh 要读得了也走得进去（o+rx）、档案 dsh 要读得了（o+r）；有 package.json；
    - expect=(name, version) 时，package.json 要是合法的小 JSON，而且 name、version 跟 expect 一模一样（套件的目录名与内容对得上）。
    关于「检查到使用之间」（TOCTOU）：之后一律用这里回传、解析过的路径去装。从 / 到套件里每个档案都是 root 所有、
    群组与其他人不能写的，所以非 root 的人没办法在检查之后把其中任何一层换成符号链接或别的东西；能改的只有 root 自己，
    前提是部署时没有别的 root 程序同时在改这些目录（部署步骤里就是只有维护员一个人在做）。
    脚本在安装前先做完所有检查，任何一项不过就在动任何东西（停 dsh、改设定）之前拒绝。"""
    real = os.path.realpath(path)
    if real != path or posixpath.dirname(real) != vendor:
        raise BundleError(f"{path} 解析之后是 {real}：路径上有符号链接，或不在 {vendor}/ 的直接底下；拒绝")
    levels = chain(path, anchor)
    for level in levels[:-1]:
        _check_entry(level, _lstat(level), uid, file_ok=False, need="x")
    _check_entry(path, _lstat(path), uid, file_ok=False, need="rx")
    for folder, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            full = os.path.join(folder, name)
            _check_entry(full, _lstat(full), uid, file_ok=False, need="rx")
        for name in files:
            full = os.path.join(folder, name)
            _check_entry(full, _lstat(full), uid, directory_ok=False, need="r")
    package = os.path.join(path, "package.json")
    if not os.path.isfile(package):
        raise BundleError(f"{path} 里没有 package.json，不是套件目录")
    name, version = read_package(package)
    if expect is not None and (name, version) != tuple(expect):
        raise BundleError(f"{package} 写的是 {name} {version}，但目录 {posixpath.basename(path)} 应该是 {expect[0]} {expect[1]}；"
                          "目录名与内容对不上（错版或放错位置），拒绝")
    return real


def expected_package(kind, directory):
    """主要三个套件：目录名 → (包名, 版本)。"""
    base = posixpath.basename(directory)
    return PACKAGE_NAMES[kind], base[len(VERSION_PREFIX[kind]):]


def expected_extra_package(directory, label):
    """--stickers / --memes / --huanqi 的目录名 → (包名, 版本)。"""
    base = posixpath.basename(directory)
    match = EXTRA_VERSION.match(base)
    if label not in EXTRA_PACKAGE_NAMES or not match:
        raise BundleError(f"{label} 的目录名 {base!r} 看不出版本（要像 xxx-1.2.3），或不是认得的额外套件")
    return EXTRA_PACKAGE_NAMES[label], match.group(1)


def checked_bundle_dir(kind, value=None, vendor=VENDOR, anchor=ANCHOR, uid=TRUSTED_UID):
    directory = bundle_dir(kind, value, vendor)
    return verify_dir(directory, vendor, anchor, uid, expected_package(kind, directory))


def checked_extra_dir(value, label, vendor=VENDOR, anchor=ANCHOR, uid=TRUSTED_UID):
    directory = extra_dir(value, label, vendor)
    return verify_dir(directory, vendor, anchor, uid, expected_extra_package(directory, label))


EXTRA_KINDS = (("stickers", "--stickers"), ("memes", "--memes"), ("huanqi", "--huanqi"))
KIND_PACKAGES = {**PACKAGE_NAMES, "stickers": EXTRA_PACKAGE_NAMES["--stickers"], "memes": EXTRA_PACKAGE_NAMES["--memes"],
                 "huanqi": EXTRA_PACKAGE_NAMES["--huanqi"]}


def resolve_bundles(discord=None, social=None, presets=None, stickers=None, memes=None, huanqi=None,
                    vendor=VENDOR, anchor=ANCHOR, uid=TRUSTED_UID):
    """要装的套件 → {种类: 检查过、解析过的路径}（顺序：discord、social、presets，然后给了哪些额外套件）。任何一个不合格丢 BundleError。"""
    bundles = {kind: checked_bundle_dir(kind, value, vendor, anchor, uid)
               for kind, value in (("discord", discord), ("social", social), ("presets", presets))}
    for (kind, label), value in zip(EXTRA_KINDS, (stickers, memes, huanqi)):
        if value:
            bundles[kind] = checked_extra_dir(value, label, vendor, anchor, uid)
    return bundles


def build_plugin_command(bundles):
    """dsh plugin add 的完整指令；bundles 是 resolve_bundles 的结果（只用检查回传、解析过的路径）。"""
    return ["sudo", "-u", "dsh", "env", "HOME=/var/lib/dsh", "DSH_HOME=/var/lib/dsh/.dsh",
            "DSH_HOST_MODULES=/opt/dsh/app/node_modules/@deepseek-ai", "PATH=/opt/dsh/node/bin:/usr/bin:/bin",
            "/opt/dsh/node/bin/node", "/opt/dsh/app/node_modules/@deepseek-ai/dsh/lib/bin.js", "plugin", "--profile", "web", "add",
            *("file:" + path for path in bundles.values()),
            "--config.auto-install-peers=false", "--ignore-scripts"]


def plugin_command(discord=None, social=None, presets=None, stickers=None, memes=None, huanqi=None,
                   verify=False, vendor=VENDOR, anchor=ANCHOR, uid=TRUSTED_UID):
    """dsh plugin add 的完整指令。升级与回滚走同一条路，只差套件目录。verify=True（configure 一律用）会先做档案系统检查，
    指令里放的是检查回传、解析过的路径。"""
    if verify:
        return build_plugin_command(resolve_bundles(discord, social, presets, stickers, memes, huanqi, vendor, anchor, uid))
    bundles = {kind: bundle_dir(kind, value, vendor) for kind, value in (("discord", discord), ("social", social), ("presets", presets))}
    bundles.update({kind: value for (kind, _), value in zip(EXTRA_KINDS, (stickers, memes, huanqi)) if value})
    return build_plugin_command(bundles)


# --rollback 只能退回「部署过的完整组」。这份白名单写在代码里（经过审阅的变更才能改），不放在 root 可写的状态档里：
# 退路是安全相关的决定，不该靠一个会被不小心改到的档案。维护方式：每次新版本部署成功、观察期过后，下一版的代码把
# 「刚被取代的那一组」加进来（例如部署 social.57 的那版代码把 social.56 加进来）；条目要同时满足：三个目录都还留在
# /opt/dsh/vendor、当时整组部署并跑过 e2e。三个套件必须同属一组，不能各挑各的版本拼起来。
ROLLBACK_GROUPS = {
    "social.55": {"discord": "discord-0.6.0-social.55", "social": "social-0.1.49", "presets": "social-presets-0.52.0"},
}


def validate_rollback(discord, social, presets):
    """--rollback 才能略过 e2e 闸门：三个套件都要明讲（不能不小心用预设），而且必须正好是 ROLLBACK_GROUPS 里的某一整组。"""
    if not (discord and social and presets):
        raise RuntimeError("--rollback 要同时明讲 --discord、--social、--presets，不能用预设。")
    chosen = {kind: posixpath.basename(bundle_dir(kind, value)) for kind, value in
              (("discord", discord), ("social", social), ("presets", presets))}
    if chosen == DEFAULT_BUNDLE:
        raise RuntimeError("--rollback 指到的就是这一版的预设套件；这不是回滚，请走正常的 e2e 验收。")
    if chosen in ROLLBACK_GROUPS.values():
        return
    owners = {kind: [label for label, group in ROLLBACK_GROUPS.items() if group[kind] == name] for kind, name in chosen.items()}
    if all(owners.values()):
        raise RuntimeError(f"--rollback 的三个套件来自不同的部署组（{chosen}），不能混搭；要退回的话三个都取自同一组：{sorted(ROLLBACK_GROUPS)}。")
    raise RuntimeError(f"--rollback 指到的组不在「部署过的完整组」白名单里（可退回：{sorted(ROLLBACK_GROUPS)}）；"
                       "要退回别的版本，请先审阅并把那一整组加进 ROLLBACK_GROUPS，或改走正常的 e2e 验收。")


# ---- root 读「别人（dsh）可能动手脚」的路径：一律走这里 ----
# 以前直接 open／read_text／copyfile：dsh 把档案换成 FIFO，root 会永远卡在 open 或 read；换成符号链接，root 会去读（或写）别处。
# 现在：用 O_NOFOLLOW（最后一段是符号链接就拒绝）加 O_NONBLOCK（FIFO 也不会卡住 open）打开，再对打开的 fd 做 fstat：
# 必须是一般档案、拥有者在允许的名单里、大小不超过上限，才读；读的时候也有上限。检查和读用的是同一个 fd，没有「检查后被换掉」的空档。
class UnsafeFile(RuntimeError):
    pass


OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
DSH_UID = 999            # 与 configure() 把设定档 chown 给的帐号一致
ROOT_AND_DSH = frozenset({TRUSTED_UID, DSH_UID})


def read_regular_file(path, limit, owners=None, trusted=False):
    """安全地读一个档案（见上）。owners：允许的拥有者 uid 集合（None＝不限）；trusted=True 另外要求群组与其他人不能写。"""
    try:
        fd = os.open(path, OPEN_FLAGS)
    except OSError as error:
        raise UnsafeFile(f"{path} 打不开（{error.strerror}；符号链接也会在这里被挡下）") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeFile(f"{path} 不是一般档案（管线、目录、装置、符号链接都不收）")
        if owners is not None and info.st_uid not in owners:
            raise UnsafeFile(f"{path} 的拥有者 uid {info.st_uid} 不在允许的名单里")
        if trusted and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise UnsafeFile(f"{path} 的群组或其他人可写（{stat.S_IMODE(info.st_mode):o}）")
        if info.st_size > limit:
            raise UnsafeFile(f"{path} 太大（{info.st_size} 字节，上限 {limit}）")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise UnsafeFile(f"{path} 读的时候超过上限 {limit}")
        return b"".join(chunks)
    finally:
        os.close(fd)


class ShortWrite(OSError):
    pass


def write_all(fd, data):
    """把 data 全部写进 fd。os.write 可以只写一部分：用循环写完；写进 0 字节（写不下去，例如磁盘满）就报错，不会无限循环。"""
    view, done = memoryview(data), 0
    while done < len(view):
        count = os.write(fd, view[done:])
        if count <= 0:
            raise ShortWrite(f"写入停住了：写了 {done}/{len(view)} 字节之后一个字节都写不下去")
        done += count


def write_and_verify(fd, data):
    """写完、fsync，然后从头读回来核对：大小和 sha256 都要跟 data 一样。任何一项不对就丢 ShortWrite（fd 必须是可读可写）。"""
    write_all(fd, data)
    os.fsync(fd)
    size = os.fstat(fd).st_size
    if size != len(data):
        raise ShortWrite(f"写完之后档案是 {size} 字节，应该是 {len(data)}")
    os.lseek(fd, 0, os.SEEK_SET)
    digest, total = hashlib.sha256(), 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    if total != len(data) or digest.hexdigest() != hashlib.sha256(data).hexdigest():
        raise ShortWrite("写完读回来的内容跟要写的不一样")


def stage_file(path, data, mode=0o644, uid=None, gid=None):
    """把 data 写进 path 旁边的新暂存档（O_EXCL＋O_NOFOLLOW，写完读回核对），回传暂存档的路径；失败就清掉暂存档再丢例外。
    之后用 commit_file 换上去；换上去之前原来的档案一个字节都没动。"""
    folder = os.path.dirname(path)
    temp = os.path.join(folder, f".{os.path.basename(path)}.tmp-{os.getpid()}")
    fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), mode)
    try:
        write_and_verify(fd, data)
        if uid is not None:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        discard_file(temp)
        raise
    os.close(fd)
    return temp


def commit_file(temp, path):
    """rename 盖过去（原子）。目标若是符号链接，换掉的是链接本身，不会写到链接指向的地方。"""
    os.replace(temp, path)


def discard_file(temp):
    try:
        os.unlink(temp)
    except FileNotFoundError:
        pass


def write_new_file(path, data, mode=0o644, uid=None, gid=None):
    """原子地写档案：暂存、核对、rename。任何一步失败，原来的档案不变、暂存档清掉。"""
    temp = stage_file(path, data, mode, uid, gid)
    try:
        commit_file(temp, path)
    except BaseException:
        discard_file(temp)
        raise


def check_profile_dir(folder, owners=ROOT_AND_DSH):
    """root 要往 dsh 的目录写东西之前：路径上不能有符号链接（realpath 与原样一致），目录本身是真目录、拥有者合理。"""
    folder = str(folder)
    if os.path.realpath(folder) != folder:
        raise UnsafeFile(f"{folder} 的路径上有符号链接，拒绝")
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners:
        raise UnsafeFile(f"{folder} 不是目录，或拥有者不对")


# ---- 隔离验收（e2e）的闸门 ----
# result.json 是 dsh 帐号写的（目录也是它的），不能只看里面写了什么就让 root 去动正式服务。做法（见 DEPLOY.md「e2e 闸门」）：
# 由 root 执行的 run_social_e2e.py 自己启动 e2e 单元并等它结束、亲眼看到结束码 0，再读 result.json，
# 全部通过才在 root 拥有的位置（/opt/dsh/e2e-attest.json）写下一份证明：时间、结束码、result.json 的 sha256、e2e 脚本的 sha256、
# 每个要装的套件的内容指纹。configure 时逐项重新核对，所以：手改或预先做好的 result.json、过期的结果、
# 别的版本的结果、e2e 脚本被换过、事后又被改动的 result.json，全部被挡下。
E2E_HOME = "/var/lib/dsh/.dsh-e2e"
E2E_RESULT = E2E_HOME + "/result.json"
E2E_MODULES = E2E_HOME + "/profiles/web/node_modules"
E2E_SCRIPT = "/opt/dsh/social-e2e.mjs"
E2E_ATTEST = "/opt/dsh/e2e-attest.json"
PROD_MODULES = "/var/lib/dsh/.dsh/profiles/web/node_modules"      # 正式服务现在装着的套件（root 可读）
EXTRA_ONLY_KINDS = ("stickers", "memes", "huanqi")
E2E_MAX_AGE = 12 * 3600        # 证明最多这么久；超过要重跑 e2e
E2E_SCHEMA = 1
# social-e2e.mjs 成功时依序 push 的 19 项检查名称（test_configure_social 会拿 repo 里的 social-e2e.mjs 逐字比对，改了一边没改另一边就会失败）
EXPECTED_E2E_CHECKS = (
    "普通群友无需提及也能自然接话",
    "收到的群聊讯息写进原文帐本",
    "翻原文工具从帐本搜到逐字原文并标明是资料",
    "两分钟内同一人的接话不被插话冷却挡住",
    "真实 remember 调用保存可信作者与 Markdown",
    "主人私讯具有独立完整工具预设",
    "群聊不能列出或绑定私人会话",
    "用量只向主人私讯显示",
    "普通群友管理指令与私讯受阻，伪装昵称无效",
    "群聊完整工具在自己的工作区实际执行",
    "送进群聊前遮掉金钥",
    "换气：空回合自动补救，补救轮的回答传到 Discord",
    "Discord 工具：群友能开频道，删频道只认站长",
    "Discord 工具：工作区里的图片能当附件发出去",
    "冷启动保留频道与会话绑定",
    "冷启动群聊记忆保留，私讯的事只以「私下」标记出现",
    "新会话仍召回同群记忆，私讯的事标成私下",
    "模型调用统一记账并按实际 usage 结算",
    "达到用量上限后真实流水线不再请求模型，重启不能绕过",
)


def tree_files(path, skip_node_modules=False, owners=None, limit=64 << 20):
    """目录底下每个档案 → sha256（相对路径为键）。遇到符号链接、管线等一律拒绝（UnsafeFile）。"""
    if os.path.islink(path) or not os.path.isdir(path):
        raise UnsafeFile(f"{path} 不是真正的目录（不存在，或是符号链接）")
    result = {}
    for folder, dirs, files in os.walk(path, followlinks=False):
        kept = []
        for name in sorted(dirs):
            full = os.path.join(folder, name)
            if os.path.islink(full):
                raise UnsafeFile(f"{full} 是符号链接，拒绝")
            if not (skip_node_modules and name == "node_modules"):
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            full = os.path.join(folder, name)
            result[os.path.relpath(full, path).replace(os.sep, "/")] = hashlib.sha256(read_regular_file(full, limit, owners)).hexdigest()
    return result


def tree_digest(files):
    return hashlib.sha256("".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items())).encode("utf-8")).hexdigest()


def check_e2e_installed(bundles, modules=E2E_MODULES):
    """e2e 的家（dsh 的）里实际装的套件，必须跟要装进正式服务的套件逐档一模一样（档案集合与 sha256 都相同）。"""
    for kind, path in bundles.items():
        installed = os.path.join(modules, KIND_PACKAGES[kind])
        if tree_files(path) != tree_files(installed, skip_node_modules=True):
            raise RuntimeError(f"e2e 家里装的 {KIND_PACKAGES[kind]} 跟要装的 {path} 不一致；请先把这一组套件 plugin add 进 e2e 家，再跑 e2e。")


def validate_e2e_result(raw):
    """result.json 的内容：严格 JSON；ok 正好是 true；checks 逐项等于 EXPECTED_E2E_CHECKS（名称与顺序，每一项都得是通过的那一项）；
    modelRequests 是正整数。多出别的栏位也拒绝（失败时 e2e 写的是别的栏位）。"""
    try:
        result = strict_json(raw)
    except ValueError as error:
        raise RuntimeError(f"验收结果档不是合法的 JSON（{error}）") from None
    if not isinstance(result, dict) or set(result) != {"ok", "checks", "modelRequests"}:
        raise RuntimeError("验收结果档的栏位不对（要正好是 ok、checks、modelRequests），拒绝。")
    if result["ok"] is not True:
        raise RuntimeError("验收结果 ok 不是 true，拒绝。")
    checks = result["checks"]
    if not isinstance(checks, list) or checks != list(EXPECTED_E2E_CHECKS):
        got = checks if isinstance(checks, list) else []
        missing = [name for name in EXPECTED_E2E_CHECKS if name not in got]
        raise RuntimeError(f"验收项目跟预期的 {len(EXPECTED_E2E_CHECKS)} 项不一致（缺：{missing[:3]}，共 {len(got)} 项），拒绝。")
    requests = result["modelRequests"]
    if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
        raise RuntimeError("验收结果的 modelRequests 不是正整数，拒绝。")
    return result


def _utc_stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_and_attest(bundles, runner, now=None, result_path=E2E_RESULT, script_path=E2E_SCRIPT, attest_path=E2E_ATTEST,
                   modules=E2E_MODULES, trusted_uid=TRUSTED_UID, owners=ROOT_AND_DSH):
    """由 root 执行：跑 e2e（runner 回传单元的结束码）、核对、写证明。前后各核对一次 e2e 家里装的套件，跑的当中被换掉也看得出来。
    任何一步不过都不会留下证明（开头先把旧的删掉）。"""
    check_e2e_installed(bundles, modules)
    script = read_regular_file(script_path, 2 << 20, {trusted_uid}, trusted=True)
    try:
        os.unlink(attest_path)
    except FileNotFoundError:
        pass
    code = runner()
    check_e2e_installed(bundles, modules)
    if code != 0:
        raise RuntimeError(f"e2e 单元的结束码是 {code}，没有通过。")
    raw = read_regular_file(result_path, 1 << 20, owners)
    validate_e2e_result(raw)
    attest = {"schema": E2E_SCHEMA, "finished_at": _utc_stamp(now or datetime.datetime.now(datetime.timezone.utc)), "unit_exit": 0,
              "result_sha256": hashlib.sha256(raw).hexdigest(), "script_sha256": hashlib.sha256(script).hexdigest(),
              "checks": list(EXPECTED_E2E_CHECKS),
              "bundles": {kind: {"dir": path, "digest": tree_digest(tree_files(path))} for kind, path in bundles.items()}}
    write_new_file(attest_path, (json.dumps(attest, ensure_ascii=False, indent=1) + "\n").encode("utf-8"), 0o644)
    return attest


def verify_e2e_gate(bundles, now=None, result_path=E2E_RESULT, script_path=E2E_SCRIPT, attest_path=E2E_ATTEST,
                    trusted_uid=TRUSTED_UID, owners=ROOT_AND_DSH, max_age=E2E_MAX_AGE, prod_modules=PROD_MODULES):
    """configure 动任何东西之前：确认「root 亲眼看过的 e2e 成功」跟现在要装的这一组套件对得上。不合格丢 RuntimeError。"""
    try:
        raw = read_regular_file(attest_path, 1 << 16, {trusted_uid}, trusted=True)
    except UnsafeFile as error:
        raise RuntimeError(f"找不到可信的 e2e 证明（{error}）；请先以 root 跑 run_social_e2e.py。") from None
    try:
        attest = strict_json(raw)
    except ValueError as error:
        raise RuntimeError(f"e2e 证明不是合法的 JSON（{error}）") from None
    keys = {"schema", "finished_at", "unit_exit", "result_sha256", "script_sha256", "checks", "bundles"}
    if not isinstance(attest, dict) or set(attest) != keys or attest["schema"] != E2E_SCHEMA:
        raise RuntimeError("e2e 证明的格式不对，拒绝。")
    unit_exit = attest["unit_exit"]
    if isinstance(unit_exit, bool) or not isinstance(unit_exit, int) or unit_exit != 0:
        raise RuntimeError("e2e 证明里的单元结束码不是 0，拒绝。")
    try:
        finished = datetime.datetime.strptime(attest["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        raise RuntimeError("e2e 证明的时间格式不对，拒绝。") from None
    age = ((now or datetime.datetime.now(datetime.timezone.utc)) - finished).total_seconds()
    if age < -300 or age > max_age:
        raise RuntimeError(f"e2e 证明已过期或时间不对（{int(age)} 秒前，上限 {max_age}）；请重跑 e2e。")
    if attest["checks"] != list(EXPECTED_E2E_CHECKS):
        raise RuntimeError("e2e 证明里的验收项目跟预期的不一致，拒绝。")
    script = read_regular_file(script_path, 2 << 20, {trusted_uid}, trusted=True)
    if hashlib.sha256(script).hexdigest() != attest["script_sha256"]:
        raise RuntimeError("现在的 social-e2e.mjs 跟当时跑 e2e 用的不是同一份；请重跑 e2e。")
    try:
        result_raw = read_regular_file(result_path, 1 << 20, owners)
    except UnsafeFile as error:
        raise RuntimeError(f"验收结果档不安全：{error}") from None
    if hashlib.sha256(result_raw).hexdigest() != attest["result_sha256"]:
        raise RuntimeError("result.json 跟 root 当时核对过的不一样（被改动或换掉），拒绝。")
    validate_e2e_result(result_raw)
    attested = attest["bundles"]
    if not isinstance(attested, dict):
        raise RuntimeError("e2e 证明里的套件清单格式不对，拒绝。")
    for kind, path in bundles.items():
        entry = attested.get(kind)
        if entry is None and kind in EXTRA_ONLY_KINDS:
            # 额外套件 e2e 没验过：只允许重装「正式服务现在已经在跑」的那一份（逐档相同）。换了内容的，要先放进 e2e 家重跑 e2e。
            try:
                same = tree_files(path) == tree_files(os.path.join(prod_modules, KIND_PACKAGES[kind]), skip_node_modules=True)
            except UnsafeFile as error:
                raise RuntimeError(f"额外套件 {kind} 读不了：{error}") from None
            if not same:
                raise RuntimeError(f"额外套件 {path} e2e 没验过，而且跟正式服务现在装的不一样；请放进 e2e 家、对它重跑 e2e。")
            continue
        if not isinstance(entry, dict) or entry.get("dir") != path:
            raise RuntimeError(f"e2e 验收的不是这一组套件：{kind} 没有对应的证明（或位置不同）；请对要装的套件重跑 e2e。")
        try:
            digest = tree_digest(tree_files(path))
        except UnsafeFile as error:
            raise RuntimeError(f"套件 {path} 读不了：{error}") from None
        if entry.get("digest") != digest:
            raise RuntimeError(f"{path} 的内容跟 e2e 验收时不一样（套件被改动过）；请重跑 e2e。")


# ---- profile（dsh 的目录）里的档案 ----
def copy_profile_file(source, target, owners=ROOT_AND_DSH):
    """备份：安全地读（见 read_regular_file）、写进 root 的备份目录。"""
    data = read_regular_file(source, 64 << 20, owners)
    if os.path.lexists(target):
        raise FileExistsError(f"备份目标已经存在：{target}")
    write_new_file(str(target), data, 0o600)


def load_profile_rows(text):
    """cordis.patch.yml：只收 YAML 的安全子集；同一层有重复的键就拒绝（PyYAML 预设会静默留最后一个，
    我们检查的跟 dsh 实际读到的可能不是同一个）；最外层要是一串物件。"""
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":      # "<<:" 合并键本来就允许被后面的键盖掉，不算重复
                continue
            key = loader.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    raise RuntimeError(f"cordis.patch.yml 有重复的键 {key!r}（第 {key_node.start_mark.line + 1} 行），拒绝。")
                seen.add(key)
            except TypeError:
                raise RuntimeError("cordis.patch.yml 的键不是一般的值，拒绝。") from None
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    try:
        rows = yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as error:
        raise RuntimeError(f"cordis.patch.yml 解析不了（{error}）") from None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("cordis.patch.yml 的最外层要是一串物件，拒绝。")
    return rows


# huanqi（换气）的设定：它自己的压缩与「用检索工具取回」提示都关掉——压缩之后改用带出处的压缩来做，
# 我们也没有它那套检索工具。其余照它的预设。
HUANQI_CONFIG = {"fullCompactOnBreaker": False, "regionCompactAfterCuts": 0, "postCompactRecallHint": False}


def configure(upgrade=False, group_tools=None, no_daily_limits=False, all_channels=False, stickers=None, memes=None, huanqi=None, owner_title=None,
              discord=None, social_dir=None, presets=None, rollback=False):
    if os.geteuid() != 0:
        raise RuntimeError("必须由 VM 维护帐号以 root 执行。")
    import yaml   # 只有真的要改设定时才需要（测试和 --show-bundle 不用装）
    # 名字、位置、符号链接、拥有者、权限都在动任何东西之前检查；之后只用检查回传、解析过的路径。
    bundles = resolve_bundles(discord, social_dir, presets, stickers, memes, huanqi)
    command = build_plugin_command(bundles)
    if rollback:
        validate_rollback(discord, social_dir, presets)
        print("警告：--rollback 略过 e2e 闸门（退回已部署过的旧版本）。", file=sys.stderr)
    else:
        verify_e2e_gate(bundles)
    if subprocess.run(["systemctl", "is-active", "--quiet", "dsh-social-e2e"]).returncode == 0:
        raise RuntimeError("e2e 单元还在跑，等它结束再来。")
    profile = pathlib.Path("/var/lib/dsh/.dsh/profiles/web")
    patch = profile / "cordis.patch.yml"
    check_profile_dir(profile)
    try:
        rows = load_profile_rows(read_regular_file(patch, 1 << 20, ROOT_AND_DSH).decode("utf-8"))
    except (UnsafeFile, UnicodeDecodeError) as error:
        raise RuntimeError(f"cordis.patch.yml 读不了：{error}") from None
    bridge_row = next((row for row in rows if row.get("id") == "discord-bridge"), None)
    if bridge_row is None or not isinstance(bridge_row.get("config"), dict):
        raise RuntimeError("找不到既有的 discord-bridge 设定，拒绝。")
    bridge = bridge_row["config"]
    existing = next((row for row in rows if row.get("id") == "social-runtime"), None)
    if existing is not None and not upgrade:
        raise RuntimeError("已经配置，拒绝重复覆盖。")
    if not bridge.get("allowedUsers") or not bridge.get("allowedChannels"):
        raise RuntimeError("缺少既有拥有者或频道白名单。")
    if bridge.get("gatewayUrl") or bridge.get("restBaseUrl") or bridge.get("token"):
        raise RuntimeError("正式桥接不得使用测试端点或内嵌凭证。")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = pathlib.Path("/var/backups/dsh") / ("before-social-" + stamp)
    backup.mkdir(mode=0o700, parents=True)
    for name in ("cordis.yml", "cordis.patch.yml", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "discord-bridge-state.json", "participation-state.json"):
        if os.path.lexists(profile / name):
            try:
                copy_profile_file(profile / name, backup / name)
            except UnsafeFile as error:
                raise RuntimeError(f"备份 {name} 时发现不安全的档案：{error}") from None
    social = {"id": "social-runtime", "config": {
        "ownerIds": bridge["allowedUsers"],
        "allowedChannels": bridge["allowedChannels"],
        "groupCwd": bridge["cwd"],
        "stateRoot": "/var/lib/dsh/.dsh/social-runtime",
        "limits": {"input": 2000000, "output": 50000, "calls": 200, "maxOutput": 8192, "timeZone": "Asia/Taipei"},
    }}
    if existing is None:
        rows.append(social)
        existing = social
    if all_channels:
        # 拥有者决定：bot 所在的所有服务器、所有频道都能说话（私讯仍只收拥有者）。
        bridge["allowedChannels"] = ["*"]
        existing.setdefault("config", {})["allowedChannels"] = ["*"]
    if no_daily_limits:
        # 拥有者暂时不要每日上限；护栏要求正整数，所以设成用不完的数。单次输出上限仍保留。
        existing.setdefault("config", {}).setdefault("limits", {}).update(input=10**12, output=10**12, calls=10**12)
    if group_tools is not None:
        # 群聊开放完整工具由拥有者决定并承担群友诱导的风险；送往 Discord 的文字仍会遮掉机密。
        existing.setdefault("config", {})["groupTools"] = group_tools
    if owner_title is not None:
        # 她在群里怎么称呼站长（预设「主人」）；空字串就只叫名字。
        existing.setdefault("config", {})["ownerTitle"] = owner_title.strip()
    if stickers:
        # 大肥鱼表情（dsh-nekowhale-stickers）：只用它的 stickers 服务与搜图、模板工具。
        # 网页桌宠与「双击查余额」（会用到 DeepSeek 登入）关掉；它自己的提示词关掉，由群聊人设说明用法。
        row = next((row for row in rows if row.get("id") == "nekowhale-stickers"), None)
        if row is None:
            row = {"id": "nekowhale-stickers"}
            rows.append(row)
        row["config"] = {"autoSync": "lazy", "promptMode": "off", "frequency": "rare", "persona": "self", "agentCuration": False, "returnImageToModel": False, "pet": False, "petBubbles": False, "petBalance": False}
    if huanqi:
        row = next((row for row in rows if row.get("id") == "huanqi"), None)
        if row is None:
            row = {"id": "huanqi"}
            rows.append(row)
        row["config"] = dict(HUANQI_CONFIG)
    bridge.update(preset="whale-social", notifyEnabled=False, imessageEnabled=False)
    account = next((row for row in rows if row.get("id") == "llm-deepseek-account"), None)
    if account is None:
        account = {"id": "llm-deepseek-account", "config": {}}
        rows.append(account)
    account.setdefault("config", {}).update(maxTokens=2048, retryPolicy={"mode": "normal", "maxRetries": 0})
    # 新设定先写成暂存档并读回核对（写不完整就在这里失败，这时 dsh 还没停、原来的设定一个字节没动），再停 dsh、再 rename 换上去。
    check_profile_dir(profile)
    staged = stage_file(str(patch), yaml.safe_dump(rows, allow_unicode=True, sort_keys=False).encode("utf-8"), 0o600, DSH_UID, DSH_UID)
    try:
        subprocess.run(["systemctl", "stop", "dsh"], check=True)
        check_profile_dir(profile)   # 停了 dsh 之后、换上去之前再确认一次：路径上没有符号链接
        commit_file(staged, str(patch))
    except BaseException:
        discard_file(staged)
        raise
    try:
        install = subprocess.run(command, cwd="/srv/dsh", capture_output=True, text=True, timeout=120)
        (backup / "install.log").write_text(install.stdout + install.stderr)
        (backup / "install.log").chmod(0o600)
        if install.returncode:
            raise RuntimeError("插件安装未完成，请查看 VM 私人安装日志并恢复备份。")
        dropin = pathlib.Path("/etc/systemd/system/dsh.service.d/social-runtime.conf")
        dropin.write_text('[Service]\nEnvironment="DSH_HOST_MODULES=/opt/dsh/app/node_modules/@deepseek-ai"\nEnvironment="TZ=Asia/Taipei"\n')
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "start", "dsh"], check=True)
    except Exception:
        # 安装失败保留停止状态，不让不完整的边界配置进入真实群聊。
        raise
    print(json.dumps({"configured": True, "backup": str(backup), "bundle": [c[len("file:"):] for c in command[11:14]]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="保留既有护栏设定，只升级已验收的固定套件")
    parser.add_argument("--group-tools", choices=("chat", "full"), help="群聊只给 remember（chat）或与私人会话一样的完整工具（full）；不给就保留现状")
    parser.add_argument("--no-daily-limits", action="store_true", help="取消每日调用次数与 token 上限（单次输出上限保留）")
    parser.add_argument("--all-channels", action="store_true", help="所有服务器的所有频道都能说话（频道白名单设为 *）")
    parser.add_argument("--stickers", help="已审阅的 dsh-nekowhale-stickers 套件目录（/opt/dsh/vendor 底下），一起装进 profile")
    parser.add_argument("--memes", help="已审阅的 dsh-meme 套件目录（/opt/dsh/vendor 底下），一起装进 profile；图库在 $HOME/.dsh/meme-packs")
    parser.add_argument("--owner-title", help="群聊里称呼站长的方式（预设「主人」，空字串＝只叫名字）；不给就保留现状")
    parser.add_argument("--huanqi", help="已审阅的 dsh-huanqi（换气）套件目录（/opt/dsh/vendor 底下），一起装进 profile")
    parser.add_argument("--discord", help="discord 桥接套件（预设：这个脚本这一版的）；回滚时指到旧版，例如 discord-0.6.0-social.55")
    parser.add_argument("--social", help="social 套件（预设同上）；例如 social-0.1.49")
    parser.add_argument("--presets", help="presets 套件（预设同上）；例如 social-presets-0.52.0")
    parser.add_argument("--rollback", action="store_true",
                        help="退回已部署过的旧版本：略过 e2e 闸门。要同时明讲 --discord、--social、--presets，而且不能就是这一版的预设")
    parser.add_argument("--show-bundle", action="store_true", help="只印出会装哪三个套件（检查用，不动任何东西，也不需要 root）")
    args = parser.parse_args()
    if args.show_bundle:
        # 跟真的升级做同一组检查（包括符号链接、拥有者、权限），所以「--show-bundle 过了」才代表升级或回滚拿得到这三个目录。
        try:
            shown = resolve_bundles(args.discord, args.social, args.presets, args.stickers, args.memes, args.huanqi)
            if args.rollback:
                validate_rollback(args.discord, args.social, args.presets)
        except (BundleError, RuntimeError) as error:
            print(f"拒绝：{error}")
            raise SystemExit(1)
        print(json.dumps(shown, indent=1))
        raise SystemExit(0)
    configure(args.upgrade, args.group_tools, args.no_daily_limits, args.all_channels, args.stickers, args.memes, args.huanqi, args.owner_title,
              args.discord, args.social, args.presets, args.rollback)
