"""把要放上 VM 暂放区（stage）的档案打成一个「每次打出来都完全一样」的 tar，并带一份 sha256 清单，方便上传后逐档核对。

  python pack_stage.py OUT.tar --add 目的路径=来源 [--add …] [--exclude 样式 …]

- 来源可以是档案或目录；目录会整个展开；遇到符号链接一律拒绝。目的路径是 stage 里的相对路径，
  目的路径写 "." 表示把目录的内容直接放在 stage 根目录。
- 预设略过 __pycache__、*.pyc、node_modules、.git；--exclude 可再加（样式比对路径的每一段，fnmatch）。
- tar 是确定性的：档案依路径排序、时间戳 0、拥有者 0、档案 644、目录不单独列；所以同样的输入永远得到同样的 sha256。
- 最后一个档案是 MANIFEST.sha256（sha256sum 的格式，不含它自己）。打包时会印出：档案数、tar 的 sha256、MANIFEST 的 sha256。
  上传解开后在 VM 上：
      cd ~/stage-xxx && sha256sum -c --quiet MANIFEST.sha256 && echo 全部吻合
      diff <(find . -type f ! -name MANIFEST.sha256 | sed 's,^\\./,,' | sort) <(awk '{print $2}' MANIFEST.sha256 | sort) && echo 没有多的档案
      sha256sum MANIFEST.sha256        # 要跟打包时印出的 MANIFEST sha256 一样
  连线参数（SSH 金钥、跳板）不在这里，也不会写进 repo。
"""

import argparse
import fnmatch
import hashlib
import io
import os
import pathlib
import re
import sys
import tarfile

DEFAULT_EXCLUDES = ("__pycache__", "*.pyc", "node_modules", ".git")
MANIFEST = "MANIFEST.sha256"


class PackError(ValueError):
    pass


def excluded(parts, patterns):
    return any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in patterns)


SAFE_PATH = re.compile(r"[\w.@+-]+(/[\w.@+-]+)*\Z")   # 字母、数字（含中文等 Unicode 字母）和 . _ @ + -，段与段之间一个 /。清单是「sha256  路径」逐行的格式，路径里有空白、控制字元（含换行）、反斜线会让清单与 VM 上的核对对不上，或被塞进假的行


def dest_ok(dest):
    parts = dest.split("/")
    return bool(SAFE_PATH.match(dest)) and "." not in parts and ".." not in parts


def put(files, path, source):
    """登记一项。同一个目的路径出现两次一律拒绝（以前会静默只留最后一项）；
    一个路径同时是档案又是别人的目录（a 与 a/b）也拒绝，解开时会互相打架。"""
    if not dest_ok(path):
        raise PackError(f"路径不合法（只收字母、数字、. _ @ + - 和 /；不能有空白、控制字元、反斜线、. 或 ..）：{path!r}")
    if path == MANIFEST:
        raise PackError(f"{MANIFEST} 是保留的名字")
    if path in files:
        raise PackError(f"目的路径重复：{path}（来源 {files[path]} 与 {source}）")
    parts = path.split("/")
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in files:
            raise PackError(f"{'/'.join(parts[:i])} 已经是档案，不能同时当目录：{path}")
    prefix = path + "/"
    if any(other.startswith(prefix) for other in files):
        raise PackError(f"{path} 已经是目录（底下有档案），不能同时当档案")
    files[path] = source


def collect(adds, patterns):
    """回传 {stage 内相对路径: 来源档案路径}；重复路径、符号链接、特殊档案、找不到来源都丢 PackError。"""
    files = {}
    for spec in adds:
        if "=" not in spec:
            raise PackError(f"--add 要写成 目的路径=来源：{spec!r}")
        dest, source = spec.split("=", 1)
        if dest.startswith("/"):
            raise PackError(f"目的路径不能是绝对路径：{dest!r}")
        dest = dest.rstrip("/") or "."
        if dest != "." and not dest_ok(dest):
            raise PackError(f"目的路径不合法：{dest!r}")
        source = pathlib.Path(source)
        if source.is_symlink():
            raise PackError(f"来源是符号链接，拒绝：{source}")
        if source.is_dir():
            for folder, dirs, names in os.walk(source, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not excluded([d], patterns))
                for name in sorted(names):
                    full = pathlib.Path(folder) / name
                    relative = full.relative_to(source).as_posix()
                    if excluded(relative.split("/"), patterns):
                        continue
                    if full.is_symlink():
                        raise PackError(f"含符号链接，拒绝：{full}")
                    if not full.is_file():
                        raise PackError(f"不是一般档案（管线、装置等），拒绝：{full}")
                    put(files, relative if dest == "." else dest + "/" + relative, full)
                for name in dirs:
                    if (pathlib.Path(folder) / name).is_symlink():
                        raise PackError(f"含符号链接，拒绝：{pathlib.Path(folder) / name}")
        elif source.is_file():
            if dest == ".":
                raise PackError("单一档案要写明目的路径，不能是 '.'")
            put(files, dest, source)
        else:
            raise PackError(f"找不到来源（或不是一般档案／目录）：{source}")
    if not files:
        raise PackError("没有任何档案")
    return files


def build(files):
    """回传 (tar 的位元组, MANIFEST 文字)。"""
    lines, contents = [], {}
    for path in sorted(files):
        try:
            data = pathlib.Path(files[path]).read_bytes()
        except OSError as error:
            raise PackError(f"读不了 {files[path]}（{error.strerror}）") from None
        contents[path] = data
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path}")
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    contents[MANIFEST] = manifest
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(contents):
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(contents[path]), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(contents[path]))
    return buffer.getvalue(), manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=pathlib.Path)
    parser.add_argument("--add", action="append", default=[], metavar="DEST=SOURCE")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN")
    args = parser.parse_args(argv)
    try:
        if args.out.is_symlink():
            raise PackError(f"输出目标是符号链接，拒绝（会写到链接指向的地方）：{args.out}")
        if args.out.is_dir():
            raise PackError(f"输出目标是目录：{args.out}")
        files = collect(args.add, DEFAULT_EXCLUDES + tuple(args.exclude))
        data, manifest = build(files)
        if args.out.is_symlink():                      # 打包的当中被换成符号链接：写之前再看一次
            raise PackError(f"输出目标是符号链接，拒绝：{args.out}")
    except PackError as error:
        print(f"拒绝：{error}", file=sys.stderr)
        return 1
    args.out.write_bytes(data)
    print(f"files: {len(files)} (+{MANIFEST})")
    print(f"tar sha256:      {hashlib.sha256(data).hexdigest()}")
    print(f"MANIFEST sha256: {hashlib.sha256(manifest).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
