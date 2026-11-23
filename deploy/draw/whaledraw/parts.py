"""零件（抠好的人物、道具图）：由维护帐号「封印」后放进只读的零件目录，画图时用 load_part() 读。

封印 = png 旁边的一个 json，记下 png 的 sha256、这张图上**本来就有、故意留的**透明洞的像素数
（发丝间、呆毛圈里这类，在抠图时就决定好）、姿势名，以及哪几边是故意裁掉的（半身特写）。
load_part() 读进来，这些会自动带进 scene.image()，呼叫的人不用、也不该自己填。

零件目录（0.2.1 起）：
    /opt/dsh/whaledraw-parts/<日期-序号>/   一组零件（例如 20261102-1），root 所有、只读
    /opt/dsh/whaledraw-parts/current        root 的连结，指到目前那一组；加新姿势就建新的一组再切过去
画图时用 /opt/dsh/whaledraw-parts/current/<名字>.png。

load_part() 只认这种零件：
- 路径规范化后，就在某一组零件目录里；png、json、那一组目录、零件总目录、/opt/dsh 都是 root 所有、
  群组与其他人不能写，png 和 json 不是连结；路径上经过的连结（例如 current）也要是 root 的、
  只指向同一层的一组目录。她自己目录里的 png（包括候选零件）一律不认。
- png 跟封印时一模一样（sha256），封印格式正确，预期洞数是合理范围内的整数，而且等于从图上重新
  量到的洞数（json 被改过就对不上）。
任何一项不符都报 PartError，并说明要找维护员。

封印工具不在这个套件里：维护帐号用 repo 的 deploy/draw/seal_part.py（只能以 root 执行，不装进
安装目录），见 DEPLOY.md。这样挡的是「走捷径」（自己封、改 json），挡不住蓄意绕过；见 DEPLOY.md
「残余风险」。
"""

import hashlib
import io
import json
import os
import re
import stat
from dataclasses import dataclass, field

from PIL import Image

from .cutout import edge_quality

SEAL_VERSION = 2
PARTS_ROOT = "/opt/dsh/whaledraw-parts"   # 底下每一组是 <日期-序号>/，加上 root 的 current 连结
TRUSTED_TOP = "/opt/dsh"                  # 从零件那一组往上到这一层（含），每一层都检查 owner 与权限
TRUSTED_UID = 0
SET_NAME = re.compile(r"[0-9]{8}-[0-9]{1,3}\Z")
POSE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}\Z")
SIDES = ("top", "bottom", "left", "right")
MAX_HOLE_FRACTION = 0.10      # 故意留的洞最多占图面积的 10%（真实立绘约 1%）
MAX_PART_BYTES = 64 * 1024 * 1024
FIX = "请找维护员处理（不要自己封印、不要改 json）"

_LOADED = object()            # load_part() 做出来的 Part 才有这个记号


class PartError(Exception):
    pass


@dataclass(frozen=True)
class Part:
    image: Image.Image
    expected_holes: int      # 这张零件本来就有的透明洞像素数（原尺寸）
    path: str
    sha256: str
    pose: str = ""
    cut_edges_ok: tuple = () # 故意裁掉的边（例如半身特写的 "bottom"）；自检不把这几边的直线断面当问题
    _origin: object = field(default=None, repr=False, compare=False)

    def is_loaded(self):
        return self._origin is _LOADED


def _safe(path, kind):
    """path 不是连结、是 kind（"dir"/"file"）、TRUSTED_UID 所有、群组与其他人不能写。"""
    try:
        info = os.lstat(path)
    except OSError as error:
        raise PartError(f"读不到 {path}（{error.strerror}）；{FIX}") from None
    if stat.S_ISLNK(info.st_mode):
        raise PartError(f"{path} 是连结，不认；{FIX}")
    if kind == "dir" and not stat.S_ISDIR(info.st_mode) or kind == "file" and not stat.S_ISREG(info.st_mode):
        raise PartError(f"{path} 不是{'目录' if kind == 'dir' else '一般档案'}；{FIX}")
    if info.st_uid != TRUSTED_UID or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PartError(f"{path} 不是维护帐号放的（拥有者 {info.st_uid}、权限 {stat.S_IMODE(info.st_mode):o}）；"
                        f"零件只能来自 {PARTS_ROOT}/current/；{FIX}")


def _check_links(path):
    """路径上经过零件总目录里的连结（例如 current）时：连结要是 TRUSTED_UID 的，内容只能是同一层的
    一组目录名（不含 /）。其他地方的连结不管：规范化之后零件还是要落在信任目录里。"""
    root = os.path.realpath(PARTS_ROOT)
    parts = os.path.abspath(path).split(os.sep)
    for depth in range(1, len(parts)):
        here = os.sep.join(parts[:depth + 1]) or os.sep
        try:
            info = os.lstat(here)
        except OSError:
            return
        if stat.S_ISLNK(info.st_mode) and os.path.realpath(os.path.dirname(here)) == root:
            target = os.readlink(here)
            if info.st_uid != TRUSTED_UID or not SET_NAME.match(target):
                raise PartError(f"{here} 这个连结不是维护帐号建的，或没有指到一组零件目录；{FIX}")


def check_trusted(png_path):
    """回传规范化后的 (png, json) 路径；不在信任的零件目录、或任何一层的拥有者/权限不对，就报 PartError。"""
    if os.name != "posix":
        raise PartError("零件只能在 Linux 上读（要检查拥有者与权限）")
    _check_links(png_path)
    real = os.path.realpath(png_path)
    folder = os.path.dirname(real)
    root = os.path.realpath(PARTS_ROOT)
    if os.path.dirname(folder) != root or not SET_NAME.match(os.path.basename(folder)):
        raise PartError(f"{png_path} 不在 {PARTS_ROOT}/current/ 里。她自己目录里的图（包括候选零件）不能当零件用；"
                        f"要新零件，请交候选给维护员封印")
    top = os.path.realpath(TRUSTED_TOP)
    level = folder
    while True:
        _safe(level, "dir")
        if level == top or os.path.dirname(level) == level:
            break
        level = os.path.dirname(level)
    json_path = os.path.splitext(real)[0] + ".json"
    _safe(real, "file")
    _safe(json_path, "file")
    return real, json_path


def _read(path, limit):
    with open(path, "rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise PartError(f"{path} 太大；{FIX}")
    return data


def load_part(png_path):
    """读一个封印过的零件。不是信任目录里的、没封印、封印不对、或 png 被改过，都报 PartError。"""
    real, json_path = check_trusted(png_path)
    try:
        data = json.loads(_read(json_path, 1 << 20).decode("utf-8"))
    except FileNotFoundError:
        raise PartError(f"{png_path} 没有封印（{json_path}）；{FIX}") from None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise PartError(f"{json_path} 读不懂（{type(error).__name__}）；{FIX}") from None
    if not isinstance(data, dict) or data.get("seal_version") != SEAL_VERSION:
        raise PartError(f"{json_path} 不是这一版的封印档；{FIX}")
    sha, holes = data.get("sha256"), data.get("expected_hole_pixels")
    pose, cut_edges = data.get("pose"), data.get("cut_edges_ok")
    if not isinstance(sha, str) or len(sha) != 64:
        raise PartError(f"{json_path} 的 sha256 格式不对；{FIX}")
    if not isinstance(pose, str) or not POSE_NAME.match(pose):
        raise PartError(f"{json_path} 的 pose 格式不对；{FIX}")
    if not isinstance(cut_edges, list) or not set(cut_edges) <= set(SIDES) or len(set(cut_edges)) != len(cut_edges):
        raise PartError(f"{json_path} 的 cut_edges_ok 格式不对；{FIX}")
    try:
        raw = _read(real, MAX_PART_BYTES)   # 只读一次：算 sha256 和开图用的是同一份内容
    except OSError as error:
        raise PartError(f"读不到 {real}（{error.strerror}）；{FIX}") from None
    actual = hashlib.sha256(raw).hexdigest()
    if actual != sha:
        raise PartError(f"{png_path} 跟封印时不一样（sha256 不符），不能用；{FIX}")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        image = image.convert("RGBA")
    except Exception as error:  # noqa: BLE001 - Pillow raises many kinds
        raise PartError(f"{png_path} 打不开（{type(error).__name__}）；{FIX}") from None
    area = image.width * image.height
    if type(holes) is not int or not 0 <= holes <= area * MAX_HOLE_FRACTION:
        raise PartError(f"{json_path} 的 expected_hole_pixels={holes!r} 不合理"
                        f"（要是 0 到 {int(area * MAX_HOLE_FRACTION)} 的整数）；{FIX}")
    measured = edge_quality(image)["hole_pixels"]
    if measured != holes:
        raise PartError(f"{json_path} 写的预期洞数 {holes} 跟图上量到的 {measured} 不一样（json 被改过？）；{FIX}")
    return Part(image=image, expected_holes=holes, path=real, sha256=actual, pose=pose,
                cut_edges_ok=tuple(cut_edges), _origin=_LOADED)


def verify(part):
    """scene.image() 用：确认 part 真的是 load_part() 做的、而且读进来之后没被改过。"""
    if not isinstance(part, Part) or not part.is_loaded():
        raise TypeError("零件一定要用 load_part() 读，不能自己做 Part")
    fresh = load_part(part.path)
    if (fresh.sha256 != part.sha256 or fresh.expected_holes != part.expected_holes
            or fresh.cut_edges_ok != part.cut_edges_ok
            or fresh.image.size != part.image.size or fresh.image.tobytes() != part.image.convert("RGBA").tobytes()):
        raise PartError(f"{part.path} 读进来之后被改过（图或封印内容）；不要改零件，{FIX}")
    return fresh


def seal_record(image_bytes, name, pose, cut_edges_ok=(), extra=None):
    """维护员的封印工具用：算出一份封印 json 的内容（不写档）。放在这里，让封印与 load_part 的格式
    只有一份定义；她就算呼叫它，也只得到一个 dict，写进她自己目录也没有用（load_part 不认那里）。"""
    if not POSE_NAME.match(pose or ""):
        raise PartError(f"姿势名 {pose!r} 只能用小写英数、- 和 _")
    if not set(cut_edges_ok) <= set(SIDES):
        raise PartError(f"cut_edges_ok 只能是 {SIDES} 的组合")
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    quality = edge_quality(image)
    data = {
        "seal_version": SEAL_VERSION,
        "file": name,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "size": list(image.size),
        "expected_hole_pixels": quality["hole_pixels"],
        "pose": pose,
        "cut_edges_ok": sorted(set(cut_edges_ok), key=SIDES.index),
        "quality_at_seal": quality,
    }
    data.update(extra or {})
    return data
