"""新姿势的候选零件：她挑素材、裁图、选空隙；维护员审阅后封印。以 dsh 身分执行：

  python3 -m whaledraw.candidate sources
      列出能用的素材（/opt/dsh/whaledraw-sources/ 里维护员放的原图）。
  python3 -m whaledraw.candidate prepare <名字> --source <素材档名> --crop 左 上 右 下 [--shadow-box 左 上 右 下]
      裁图、抠背景（先不挖任何空隙），产出编号图 enclosed.png、深浅底预览 preview.png、prepare.json。
      --shadow-box（裁切后的座标）：脚下灰色地面阴影所在的框，跟背景连着的阴影一起去掉。
  python3 -m whaledraw.candidate finalize <名字> --remove 3,5,8 [--cut-edges bottom]
      照编号挖掉那几块（看 enclosed.png 决定；没写的一律保留），产出 candidate.png、
      check-removed.png（挖掉的地方标红）、check-edges.png（边缘放大，深浅两种底）、report.json。
      --cut-edges 只给故意裁掉一边的半身特写用（top/bottom/left/right）。
  python3 -m whaledraw.candidate submit <名字> --pose <姿势名> --note "<一句说明>"
      交申请给维护员（每个名字只能有一份待审，全部最多 3 份）。印出要在群里说的那一句。
  python3 -m whaledraw.candidate withdraw <名字>      撤回待审的申请（之后才能再改）
  python3 -m whaledraw.candidate status               看候选与申请的状态

候选零件放在她自己的目录 /srv/dsh/discord/draw/candidates/<名字>/，load_part() 不认（只认封印目录）。
维护员封印时会从「素材原图＋裁切框＋编号」自己重算一次，跟 candidate.png 逐像素比对；
封印的是重算出来的那一份，所以交申请之后改 candidate.png 没有用，只会被退回。
"""

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import stat
import sys
import time

try:
    import fcntl
except ImportError:          # Windows：这个工具只在 VM（Linux）上用
    fcntl = None

import numpy as np
from PIL import Image, ImageDraw

from . import __version__
from .check import edge_points, edge_sheet, enclosed_sheet
from .cutout import cut_edges, cut_out, edge_quality
from . import parts as _parts
from .parts import PARTS_ROOT, POSE_NAME, SIDES, PartError, _safe

SOURCES = "/opt/dsh/whaledraw-sources"
CANDIDATES = "/srv/dsh/discord/draw/candidates"
REQUESTS = "/srv/dsh/discord/draw/requests"
DECISIONS = PARTS_ROOT + "/decisions.log"
# 她与维护员共用的锁：放在 root 的零件总目录里，root 所有、0644。她只能「打开来读」（flock 用读的档案
# 描述元就能锁），不能删除或换掉。由维护员第一次执行 seal_part.py（import 或 recover）时建立。
SHARED_LOCK = PARTS_ROOT + "/requests.lock"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}\Z")
MAX_PENDING = 3
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MIN_SIDE, MAX_SIDE = 16, 4096


class CandidateError(Exception):
    pass


class Crash(BaseException):
    """测试用：模拟在某一步被杀掉（不跑任何清理）。正式执行时不会发生。"""


_CRASH_AT = None             # 测试才会设：在这个步骤「当掉」


def _crashpoint(name):
    if _CRASH_AT == name:
        raise Crash(name)


LOCK_WAIT = 15.0             # 等另一个 prepare/finalize/submit/withdraw 做完，最多等这么久


class _Lock:
    """她与维护员共用的单写者锁（SHARED_LOCK 上的 flock）。会改候选或申请状态的动作都在锁里做，
    所以「检查数量／同名」和「写入」之间不会有别人插进来；维护员的 seal / reject 也拿同一把锁。

    锁档必须：在 root 的零件总目录里（目录本身也要 root 所有、群组与其他人不能写）、root 所有、
    群组与其他人不能写、一般档、不是连结。不符就拒绝（fail closed）：锁档要是能被换掉，两边可能各锁
    各的。拿到锁之后再确认路径上的档案就是锁住的那一个（同一个 inode）；重要的写入前也再确认一次（check）。"""

    def __init__(self, create=False):
        self.create, self.fd = create, None   # create 只有 root（seal_part.py）会用

    def _verify(self, fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _parts.TRUSTED_UID
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise CandidateError(f"警告：共用锁档 {SHARED_LOCK} 的拥有者或权限不对；请找维护员（不要自己建或改）")
        try:
            now = os.lstat(SHARED_LOCK)
        except FileNotFoundError:
            raise CandidateError(f"警告：共用锁档 {SHARED_LOCK} 不见了；请找维护员") from None
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise CandidateError(f"警告：共用锁档 {SHARED_LOCK} 被换掉了；已停止，请找维护员")

    def __enter__(self):
        if fcntl is None:
            raise CandidateError("这个工具只在 Linux 上用")
        folder = os.path.dirname(SHARED_LOCK)
        try:
            _safe(folder, "dir")
        except PartError as error:
            raise CandidateError(f"警告：共用锁所在的目录不对：{error}") from None
        if self.create and not os.path.lexists(SHARED_LOCK):
            fd = os.open(SHARED_LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
            os.fchmod(fd, 0o644)
            os.close(fd)
        try:
            self.fd = os.open(SHARED_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            raise CandidateError(f"共用锁档 {SHARED_LOCK} 还没建立；请找维护员（seal_part.py recover 会建立）") from None
        except OSError as error:
            raise CandidateError(f"警告：打不开共用锁档 {SHARED_LOCK}（{error.strerror}；是连结？）") from None
        try:
            self._verify(self.fd)
            deadline = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CandidateError("另一个候选动作还在做（或维护员正在处理申请）；等一下再试") from None
                    time.sleep(0.05)
            self._verify(self.fd)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def check(self):
        """重要的写入之前再确认一次：锁档还在、没被换掉。"""
        self._verify(self.fd)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)          # 关档就释放 flock
        return False


# ---------- 素材与重算（维护员的封印工具也用这几个函式，两边算法只有一份） ----------

def read_source(name):
    """读维护员放的素材：只收 SOURCES 里的一般档（root 所有、群组与其他人不能写、不是连结）。
    回传 (bytes, sha256)。"""
    if not isinstance(name, str) or os.path.basename(name) != name or name.startswith("."):
        raise CandidateError(f"素材要写档名（{SOURCES}/ 里的），不是路径：{name!r}")
    path = os.path.join(SOURCES, name)
    try:
        _safe(os.path.realpath(SOURCES), "dir")
        _safe(path, "file")
    except PartError as error:
        raise CandidateError(f"{name} 不是维护员放的素材：{error}") from None
    with open(path, "rb") as file:
        data = file.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise CandidateError(f"{name} 太大")
    return data, hashlib.sha256(data).hexdigest()


def check_crop(crop, size):
    if (not isinstance(crop, (list, tuple)) or len(crop) != 4 or not all(type(v) is int for v in crop)):
        raise CandidateError("裁切框要写成 4 个整数：左 上 右 下")
    x0, y0, x1, y1 = crop
    if not (0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1]):
        raise CandidateError(f"裁切框 {list(crop)} 超出素材范围 {list(size)}")
    if not (MIN_SIDE <= x1 - x0 <= MAX_SIDE and MIN_SIDE <= y1 - y0 <= MAX_SIDE):
        raise CandidateError(f"裁切框的宽高要在 {MIN_SIDE}–{MAX_SIDE} 像素")
    return [x0, y0, x1, y1]


def check_shadow_box(box, crop):
    if box is None:
        return None
    if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(type(v) is int for v in box):
        raise CandidateError("阴影框要写成 4 个整数：左 上 右 下（裁切后的座标）")
    width, height = crop[2] - crop[0], crop[3] - crop[1]
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise CandidateError(f"阴影框 {list(box)} 超出裁切后的范围 {width}×{height}")
    return [x0, y0, x1, y1]


def derive(source_bytes, crop, remove_ids=(), shadow_box=None):
    """从素材原图、裁切框、阴影框、要挖的编号算出零件。同样的输入每次结果都一样。"""
    image = Image.open(io.BytesIO(source_bytes))
    image.load()
    crop = check_crop(list(crop), image.size)
    shadow_box = check_shadow_box(shadow_box, crop)
    cut = cut_out(image, crop=tuple(crop), remove_ids=tuple(remove_ids),
                  shadow_box=tuple(shadow_box) if shadow_box else None)
    if cut.report.get("unknown_remove_ids"):
        raise CandidateError(f"编号图上没有这些编号：{cut.report['unknown_remove_ids']}")
    return cut


def removed_overlay(source_bytes, crop, remove_ids, shadow_box=None):
    """检查图：裁切后的原图，挖掉的地方标红并写上编号；右边是成品放在深色与浅色底上。"""
    base = derive(source_bytes, crop, (), shadow_box)
    final = derive(source_bytes, crop, remove_ids, shadow_box)
    removed = (base.alpha > 0) & (final.alpha == 0)
    crop_rgb = Image.open(io.BytesIO(source_bytes)).convert("RGB").crop(tuple(crop))
    pixels = np.asarray(crop_rgb).copy()
    pixels[removed] = (pixels[removed] * 0.3 + np.array([255, 0, 0]) * 0.7).astype(np.uint8)
    marked = Image.fromarray(pixels)
    draw = ImageDraw.Draw(marked)
    for hole in final.report["enclosed"]:
        if hole["removed"]:
            x0, y0, x1, y1 = hole["bbox"]
            draw.rectangle([x0 - 2, y0 - 2, x1 + 1, y1 + 1], outline=(255, 0, 0), width=1)
            draw.text((x0, max(0, y0 - 12)), str(hole["id"]), fill=(255, 0, 0))
    width, height = marked.size
    sheet = Image.new("RGB", (width * 3, height), (0, 0, 0))
    sheet.paste(marked, (0, 0))
    for column, colour in ((1, (34, 24, 60)), (2, (235, 230, 220))):
        tile = Image.new("RGBA", (width, height), colour + (255,))
        tile.alpha_composite(final.image)
        sheet.paste(tile.convert("RGB"), (column * width, 0))
    return sheet, int(removed.sum())


# ---------- 她的目录 ----------

def _folder(name, create=False):
    if not isinstance(name, str) or not NAME.match(name):
        raise CandidateError("名字只能用小写英数、- 和 _（例如 side 或 back-standing）")
    for path in (os.path.dirname(CANDIDATES), CANDIDATES):
        if os.path.islink(path):
            raise CandidateError(f"{path} 是连结，不用")
    folder = os.path.join(CANDIDATES, name)
    if os.path.islink(folder):
        raise CandidateError(f"{folder} 是连结，不用")
    if create:
        os.makedirs(folder, exist_ok=True)
    elif not os.path.isdir(folder):
        raise CandidateError(f"还没有 {name} 这个候选；先 prepare")
    return folder


def _locked(folder):
    path = os.path.join(folder, "submitted.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as file:
            return json.load(file).get("request")
    return None


def _request_path(request_id):
    return os.path.join(REQUESTS, request_id + ".json")


def _heal(folder):
    """（锁里呼叫）修复中途当掉留下的状态：
    - requests/ 里没送完的暂存档（.*.part）删掉；
    - 候选的锁标记指到的申请既不存在、也没有维护员的决定（送出到一半当掉、或撤回到一半当掉），
      标记就是孤立的，删掉，候选恢复成可修改。"""
    if os.path.isdir(REQUESTS):
        for entry in os.listdir(REQUESTS):
            if entry.startswith(".") and entry.endswith(".part"):
                os.remove(os.path.join(REQUESTS, entry))
    request_id = _locked(folder) if folder else None
    if request_id and not os.path.exists(_request_path(request_id)) and request_id not in _decided():
        os.remove(os.path.join(folder, "submitted.json"))


def _check_unlocked(name, folder, action):
    """（锁里呼叫）送出后到维护员处理前，候选锁住不能改。退回了就自动解锁（照理由修改后重交）；
    封印了就一直锁着：那个名字已经是正式零件，新的要换名字。"""
    _heal(folder)
    request_id = _locked(folder)
    if not request_id:
        return
    decision = _decided().get(request_id)
    if decision is None:
        raise CandidateError(f"{name} 已经送出申请了，等维护员处理就好，不要重送、不要催"
                             + ("；要改先 withdraw" if action != "submit" else ""))
    if decision.get("decision") == "rejected":
        os.remove(os.path.join(folder, "submitted.json"))
        return
    raise CandidateError(f"{name} 已经封印成正式零件了（{decision.get('file')}），不能再改或再交；新的候选请换个名字")


def _write_json(path, data):
    temporary = path + ".part"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=1)
    os.replace(temporary, path)


def _read_json(path):
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def prepare(name, source, crop, shadow_box=None):
    with _Lock():
        return _prepare(name, source, crop, shadow_box)


def _prepare(name, source, crop, shadow_box):
    folder = _folder(name, create=True)
    _check_unlocked(name, folder, "prepare")
    data, sha = read_source(source)
    cut = derive(data, crop, (), shadow_box)
    enclosed_sheet(cut).save(os.path.join(folder, "enclosed.png"))
    width, height = cut.image.size
    preview = Image.new("RGB", (width * 2, height))
    for column, colour in ((0, (34, 24, 60)), (1, (235, 230, 220))):
        tile = Image.new("RGBA", (width, height), colour + (255,))
        tile.alpha_composite(cut.image)
        preview.paste(tile.convert("RGB"), (column * width, 0))
    preview.save(os.path.join(folder, "preview.png"))
    for stale in ("candidate.png", "check-removed.png", "check-edges.png", "report.json"):
        if os.path.exists(os.path.join(folder, stale)):
            os.remove(os.path.join(folder, stale))
    info = {"name": name, "source": source, "source_sha256": sha, "crop": list(crop),
            "shadow_box": list(shadow_box) if shadow_box else None,
            "enclosed": cut.report["enclosed"], "whaledraw": __version__}
    _write_json(os.path.join(folder, "prepare.json"), info)
    return info


def finalize(name, remove_ids, cut_edges_ok=()):
    with _Lock():
        return _finalize(name, remove_ids, cut_edges_ok)


def _finalize(name, remove_ids, cut_edges_ok):
    folder = _folder(name)
    _check_unlocked(name, folder, "finalize")
    info = _read_json(os.path.join(folder, "prepare.json"))
    data, sha = read_source(info["source"])
    if sha != info["source_sha256"]:
        raise CandidateError("素材在 prepare 之后换过了；重新 prepare")
    remove_ids = sorted({int(i) for i in remove_ids})
    if not set(cut_edges_ok) <= set(SIDES):
        raise CandidateError(f"--cut-edges 只能是 {', '.join(SIDES)}")
    shadow_box = info.get("shadow_box")
    cut = derive(data, info["crop"], remove_ids, shadow_box)
    path = os.path.join(folder, "candidate.png")
    cut.image.save(path)
    sheet, removed_pixels = removed_overlay(data, info["crop"], remove_ids, shadow_box)
    sheet.save(os.path.join(folder, "check-removed.png"))
    points = edge_points(np.asarray(cut.image.getchannel("A")))
    if points:
        edge_sheet(cut.image, points).save(os.path.join(folder, "check-edges.png"))
    with open(path, "rb") as file:
        candidate_sha = hashlib.sha256(file.read()).hexdigest()
    sides = cut_edges(cut.image)
    report = {
        "name": name, "source": info["source"], "source_sha256": sha, "crop": info["crop"],
        "shadow_box": shadow_box, "remove_ids": remove_ids, "removed_pixels": removed_pixels,
        "cut_edges": sides, "cut_edges_ok": sorted(set(cut_edges_ok), key=SIDES.index),
        "quality": edge_quality(cut.image), "candidate_sha256": candidate_sha, "whaledraw": __version__,
    }
    problems = []
    if report["quality"]["halo"] > 0.02:
        problems.append(f"边缘有白边（{report['quality']['halo']:.1%}）")
    if report["quality"]["specks"]:
        problems.append(f"有 {report['quality']['specks']} 块零碎残渣")
    unexpected = [s for s, n in sides.items() if n and s not in report["cut_edges_ok"]]
    if unexpected:
        problems.append(f"被裁切框切到：{', '.join(unexpected)}（裁大一点，或半身特写才用 --cut-edges）")
    report["problems"] = problems
    _write_json(os.path.join(folder, "report.json"), report)
    return report


class DecisionsCorrupt(CandidateError):
    pass


def parse_decisions(data):
    """严格解析决定纪录（bytes）：每一行都要是完整的 JSON 物件、有 request，最后一行要以换行结尾。
    有问题就丢 DecisionsCorrupt（不吞坏行）。"""
    if not data:
        return {}
    if not data.endswith(b"\n"):
        raise DecisionsCorrupt("决定纪录最后一行不完整（没有换行，可能写到一半被中断）；请维护员执行 seal_part.py recover")
    decided = {}
    for number, line in enumerate(data.split(b"\n")[:-1], start=1):
        try:
            row = json.loads(line.decode("utf-8"))
            decided[row["request"]] = row
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, RecursionError):
            raise DecisionsCorrupt(f"决定纪录第 {number} 行坏了；请维护员检查（seal_part.py recover）") from None
    return decided


def _decided():
    """维护员已经处理过的申请编号（封印或退回）。还没有决定纪录就是都还没处理；纪录坏了就拒绝（fail closed）。"""
    try:
        with open(DECISIONS, "rb") as file:
            data = file.read()
    except FileNotFoundError:
        return {}
    return parse_decisions(data)


def pending():
    decided = _decided()
    out = []
    if os.path.isdir(REQUESTS):
        for entry in sorted(os.listdir(REQUESTS)):
            if entry.endswith(".json") and entry[:-5] not in decided:
                try:
                    # 维护员（root）也会呼叫这里：不跟随连结，只读一般档。
                    fd = os.open(os.path.join(REQUESTS, entry), os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as file:
                        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                            continue
                        row = json.loads(file.read(1 << 20))
                    if isinstance(row, dict) and row.get("request") == entry[:-5]:
                        out.append(row)
                except (OSError, ValueError, RecursionError):
                    continue
    return out


def submit(name, pose, note):
    with _Lock() as lock:
        return _submit(name, pose, note, lock)


def _submit(name, pose, note, lock=None):
    """（锁里）检查与送出在同一个锁里完成。顺序：
    1. 申请写进暂存档 requests/.<编号>.json.part（不算待审）；
    2. 写候选的锁标记 submitted.json（指到这份申请）；
    3. 暂存档改名成 requests/<编号>.json：这一步才算送出（原子）。
    任何一步失败都清掉前面做的；中途被杀，下次任何动作都会由 _heal 清掉暂存档与孤立的锁标记。"""
    folder = _folder(name)
    _check_unlocked(name, folder, "submit")
    if not isinstance(pose, str) or not POSE_NAME.match(pose):
        raise CandidateError("姿势名只能用小写英数、- 和 _（例如 side-standing）")
    if not isinstance(note, str) or not note.strip() or len(note) > 200:
        raise CandidateError("说明要写一句（200 字以内）")
    report = _read_json(os.path.join(folder, "report.json"))
    with open(os.path.join(folder, "candidate.png"), "rb") as file:
        if hashlib.sha256(file.read()).hexdigest() != report["candidate_sha256"]:
            raise CandidateError("candidate.png 在 finalize 之后被改过；重新 finalize")
    if report["problems"]:
        raise CandidateError("候选还有问题，先修好再交：" + "；".join(report["problems"]))
    waiting = pending()
    if any(item.get("name") == name for item in waiting):
        raise CandidateError(f"{name} 已经有一份待审的申请；等维护员处理")
    if len(waiting) >= MAX_PENDING:
        raise CandidateError(f"已经有 {len(waiting)} 份待审的申请（上限 {MAX_PENDING}）；等维护员处理完再交")
    os.makedirs(REQUESTS, exist_ok=True)
    if os.path.islink(REQUESTS):
        raise CandidateError(f"{REQUESTS} 是连结，不用")
    request_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + name
    request = {
        "request": request_id, "name": name, "pose": pose, "note": note.strip(),
        "source": report["source"], "source_sha256": report["source_sha256"], "crop": report["crop"],
        "shadow_box": report.get("shadow_box"), "remove_ids": report["remove_ids"], "cut_edges_ok": report["cut_edges_ok"],
        "candidate": os.path.join(folder, "candidate.png"), "candidate_sha256": report["candidate_sha256"],
        "quality": report["quality"], "whaledraw": __version__,
    }
    final = _request_path(request_id)
    temporary = os.path.join(REQUESTS, "." + request_id + ".json.part")
    marker = os.path.join(folder, "submitted.json")
    try:
        with open(temporary, "x", encoding="utf-8") as file:
            json.dump(request, file, ensure_ascii=False, indent=1)
            file.flush()
            os.fsync(file.fileno())
        _crashpoint("submit-after-temp")
        _write_json(marker, {"request": request_id})
        _crashpoint("submit-after-marker")
        if os.path.lexists(final):
            raise CandidateError("申请编号撞到了；再交一次")
        if lock is not None:
            lock.check()
        os.rename(temporary, final)
        _crashpoint("submit-after-publish")
    except Exception:
        for leftover in (temporary, marker):
            if os.path.lexists(leftover) and not os.path.exists(final):
                os.remove(leftover)
        raise
    request["say"] = f"我做好了新姿勢候選『{pose}』，等維護員封印"
    return request


def withdraw(name):
    with _Lock():
        return _withdraw(name)


def _withdraw(name):
    """（锁里）撤回：先把申请改名成 .withdrawn（之后就不算待审），再删锁标记。
    也能撤回孤立的申请（申请还在、候选却没有指到它的锁标记，见 status）。"""
    folder = _folder(name)
    _heal(folder)
    decided = _decided()
    request_id = _locked(folder)
    if request_id and decided.get(request_id, {}).get("decision") in ("sealed", "imported"):
        raise CandidateError(f"{name} 已经封印成正式零件了，不能撤回")
    targets = [request_id] if request_id else []
    targets += [r["request"] for r in pending() if r.get("name") == name and r["request"] != request_id]
    targets = [t for t in targets if t not in decided]
    if not targets:
        raise CandidateError(f"{name} 没有待审的申请")
    for target in targets:
        if os.path.exists(_request_path(target)):
            os.replace(_request_path(target), os.path.join(REQUESTS, target + ".withdrawn"))
        _crashpoint("withdraw-after-rename")
    if request_id:
        os.remove(os.path.join(folder, "submitted.json"))
    return targets[0] if len(targets) == 1 else targets


def status():
    """候选与申请的状态（会顺便修复中途当掉的状态，所以也拿锁）。
    孤立的申请（申请在待审、候选却没有指到它）会列成 orphan：她 withdraw，或维护员 reject。"""
    with _Lock():
        if os.path.isdir(CANDIDATES):
            for name in os.listdir(CANDIDATES):
                folder = os.path.join(CANDIDATES, name)
                if os.path.isdir(folder) and not os.path.islink(folder):
                    _heal(folder)
        else:
            _heal(None)
        rows = _status()
    claimed = {r["request"] for r in rows if r["request"]}
    for request in pending():
        if request["request"] not in claimed:
            rows.append({"name": request.get("name"), "state": "orphan request: run withdraw, or the maintainer rejects it",
                         "request": request["request"]})
    return rows


def _status():
    decided = _decided()
    rows = []
    if os.path.isdir(CANDIDATES):
        for name in sorted(os.listdir(CANDIDATES)):
            folder = os.path.join(CANDIDATES, name)
            if not os.path.isdir(folder) or os.path.islink(folder):
                continue
            request_id = _locked(folder)
            state = "prepared"
            if os.path.exists(os.path.join(folder, "report.json")):
                state = "finalized"
            if request_id:
                decision = decided.get(request_id)
                state = f"{decision['decision']}" if decision else "waiting for the maintainer"
                if decision and decision.get("reason"):
                    state += f" ({decision['reason']})"
            rows.append({"name": name, "state": state, "request": request_id})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m whaledraw.candidate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources")
    p = sub.add_parser("prepare")
    p.add_argument("name")
    p.add_argument("--source", required=True)
    p.add_argument("--crop", nargs=4, type=int, required=True)
    p.add_argument("--shadow-box", nargs=4, type=int)
    f = sub.add_parser("finalize")
    f.add_argument("name")
    f.add_argument("--remove", default="")
    f.add_argument("--cut-edges", default="")
    s = sub.add_parser("submit")
    s.add_argument("name")
    s.add_argument("--pose", required=True)
    s.add_argument("--note", required=True)
    w = sub.add_parser("withdraw")
    w.add_argument("name")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.command == "sources":
            names = sorted(os.listdir(SOURCES)) if os.path.isdir(SOURCES) else []
            result = {"sources": [n for n in names if not n.startswith(".")]}
        elif args.command == "prepare":
            info = prepare(args.name, args.source, args.crop, args.shadow_box)
            result = {"ok": True, "enclosed": [{k: h[k] for k in ("id", "center", "area")} for h in info["enclosed"]],
                      "look_at": os.path.join(CANDIDATES, args.name, "enclosed.png")}
        elif args.command == "finalize":
            ids = [int(x) for x in args.remove.split(",") if x.strip()]
            sides = [x.strip() for x in args.cut_edges.split(",") if x.strip()]
            report = finalize(args.name, ids, sides)
            result = {"ok": not report["problems"], "problems": report["problems"], "quality": report["quality"],
                      "look_at": [os.path.join(CANDIDATES, args.name, n) for n in ("check-removed.png", "check-edges.png")]}
        elif args.command == "submit":
            request = submit(args.name, args.pose, args.note)
            result = {"ok": True, "request": request["request"], "say_in_group": request["say"]}
        elif args.command == "withdraw":
            result = {"ok": True, "withdrawn": withdraw(args.name)}
        else:
            result = {"candidates": status()}
    except (CandidateError, PartError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
