#!/usr/bin/env python3
"""零件的审阅与封印（维护帐号专用，只能以 root 执行）。不装进 whaledraw 安装目录，她的 PYTHONPATH 里没有它。

  sudo python3 -I seal_part.py <安装目录，如 /opt/dsh/whaledraw-0.2.1> <指令> ...

指令：
  requests                         列出待审的候选申请（也会检查、修复上次中断留下的状态）
  review <申请编号>                 从素材原图＋裁切框＋编号重算一次，跟她交的 candidate.png 逐像素比对；
                                    产出审阅总览图到 /var/lib/whaledraw-review/<申请编号>/（root 700）
  seal <申请编号> --as <名字.png>   再重算一次、比对，封印「重算出来的那一份」：建一组新的零件目录
                                    （旧零件全部带过去），切换 current，写进决定纪录
  reject <申请编号> --reason "..."  退回，理由写进决定纪录（她读得到）
  import <png> --as <名字.png> --pose <姿势名> [--cut-edges bottom]
                                    把维护员自己准备的零件（例如 0.2.0 的 whale_front）封印进新的一组
  recover                          只做检查与修复（见下面「中断后的修复」）

零件目录：/opt/dsh/whaledraw-parts/<日期-序号>/ 一组一个目录，current 是 root 的连结，指到目前那一组。
每次封印都建新的一组（旧零件复制过去再加新的），所以回滚就是把 current 指回上一组。

单写者：seal / reject / import / recover 都要先拿到 /opt/dsh/whaledraw-parts/.seal.lock（root 600）
的独占锁，拿不到就拒绝（不排队）；再拿她也用的共用锁 /opt/dsh/whaledraw-parts/requests.lock
（root 644，在 root 的目录里，她只能打开、不能删除或换掉；第一次执行时由这里建立），所以她撤回与维护员
封印不会交错。锁档的拥有者、权限、是不是同一个 inode 都会核对，不对就停下并警告。

决定纪录 decisions.log：每次写之前严格检查；只有「最后一行写到一半」这种情况会自动修复（整份原档先
备份成 decisions.log.corrupt-<时间>，再去掉那半行）；中间有坏行就停下，要人处理。写完再读回来确认。

可恢复的提交（seal / import）：
  1. 先写意图 .pending-commit.json（要建哪一组、哪个申请、哪个档名、原本的 current）；
  2. 建新的一组、逐一验证；
  3. 原子地切换 current；
  4. 在 decisions.log 记下决定；
  5. 删掉意图档。
中断后的修复（每个会改东西的指令开头都会先做）：有意图档时，先严格检查意图档本身（必要栏位齐全、
型别对、组名格式对、路径落在零件总目录里），不对就什么都不动、停下要人处理。然后只有两种情况会自动处理：
  - current 指到意图里的新组，而且那一组完整（目录在、每个零件都通过 load_part、原本那一组的零件都在、
    INDEX.md 在）→ 补上还没写的决定（第 3 步之后中断）；
  - current 指到意图里记的「原本那一组」，而且那一组也完整 → 删掉做到一半的新组（第 3 步之前中断），申请仍是待审；
  - 其他所有情况（current 不存在、指到第三个组、指到的组不完整）→ 什么都不动、保留意图档，停下要人处理。
    第一次封印（原本没有 current）中途当掉也算这一类：那时候的状态要人看过再处理。
处理完删掉意图档，并把做了什么印出来。
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import stat
import sys

try:
    import fcntl
except ImportError:
    fcntl = None

REVIEW_ROOT = "/var/lib/whaledraw-review"
# decisions.log 的坏尾巴备份（decisions.log.corrupt-*）：超过份数或总量就不再自动修复，停下警告要人清理，
# 不会默默删掉旧备份。
MAX_CORRUPT_BACKUPS = 10
MAX_CORRUPT_BYTES = 50 * 1024 * 1024
INTENT_KEYS = {"set", "request", "decision", "file", "previous"}


def fail(message, code=1):
    print(f"refusing: {message}", file=sys.stderr)
    sys.exit(code)


class Tool:
    def __init__(self, version_dir):
        version_dir = os.path.realpath(version_dir)
        sys.path[:0] = [os.path.join(version_dir, "lib"), version_dir]
        from whaledraw import candidate, parts       # 用该版本自己的程式重算、验证（两边算法只有一份）
        from PIL import Image
        self.candidate, self.parts, self.Image = candidate, parts, Image
        if not hasattr(parts, "PARTS_ROOT"):
            fail(f"{version_dir} 的 whaledraw 太旧，没有零件目录（要 0.2.1 以上）", 2)
        self.crash_at = None        # 测试用：在这一步「当掉」（不做任何清理）

    def _crashpoint(self, name):
        if self.crash_at == name:
            raise self.candidate.Crash(name)

    # ---------- 读她的档：不跟随连结、只收一般档 ----------
    @staticmethod
    def _read_hers(path, limit=64 * 1024 * 1024):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                fail(f"{path} 不是一般档案")
            data = file.read(limit + 1)
        if len(data) > limit:
            fail(f"{path} 太大")
        return data

    def request(self, request_id):
        c = self.candidate
        if not c.NAME.match(request_id.split("-", 1)[-1]) or "/" in request_id:
            fail(f"申请编号格式不对：{request_id!r}")
        try:
            data = json.loads(self._read_hers(os.path.join(c.REQUESTS, request_id + ".json"), 1 << 20))
        except (OSError, ValueError, RecursionError) as error:
            fail(f"读不到申请 {request_id}（可能已经撤回）：{type(error).__name__}")
        if not isinstance(data, dict) or data.get("request") != request_id:
            fail("申请内容跟编号对不上")
        if request_id in c._decided():
            fail(f"{request_id} 已经处理过了")
        return data

    def rederive(self, req):
        """从素材重算，跟她的 candidate.png 逐像素比对。回传 (重算的 Cutout, 素材 bytes)。"""
        c = self.candidate
        source, sha = c.read_source(req["source"])
        if sha != req["source_sha256"]:
            fail("素材的 sha256 跟申请里的不一样（素材被换过）")
        cut = c.derive(source, req["crop"], req["remove_ids"], req.get("shadow_box"))
        expected = os.path.join(c.CANDIDATES, req["name"], "candidate.png")
        if os.path.realpath(req["candidate"]) != os.path.realpath(expected):
            fail("申请里的候选路径不在她的候选目录")
        hers = self.Image.open(io.BytesIO(self._read_hers(expected))).convert("RGBA")
        if hers.size != cut.image.size or hers.tobytes() != cut.image.tobytes():
            fail("她的 candidate.png 跟重算结果不一样（交申请之后被改过，或参数对不上）；请退回")
        return cut, source

    # ---------- 零件目录 ----------
    def _root(self):
        root = self.parts.PARTS_ROOT
        if os.path.islink(root):
            fail(f"{root} 是连结")
        os.makedirs(root, mode=0o755, exist_ok=True)
        info = os.lstat(root)
        if info.st_uid != 0 or info.st_mode & 0o022:
            fail(f"{root} 不是 root 所有、或群组与其他人可写")
        return root

    def _journal_path(self):
        return os.path.join(self._root(), ".pending-commit.json")

    def _shared(self):
        """她也用的共用锁（root 的目录里、root 644；不存在就由这里建立）。"""
        return self.candidate._Lock(create=True)

    @contextlib.contextmanager
    def _seal_lock(self):
        """单写者：拿不到就拒绝，不排队。"""
        if fcntl is None:
            fail("只能在 Linux 上执行")
        fd = os.open(os.path.join(self._root(), ".seal.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                fail(".seal.lock 不是一般档案")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("另一个 seal / reject / import 正在执行；等它做完再跑")
            yield
        finally:
            os.close(fd)

    def _current(self):
        link = os.path.join(self._root(), "current")
        if not os.path.lexists(link):
            return None
        if not os.path.islink(link) or os.lstat(link).st_uid != 0:
            fail(f"{link} 不是 root 的连结")
        target = os.readlink(link)
        if not self.parts.SET_NAME.match(target):
            fail(f"{link} 指到 {target!r}，不是一组零件目录")
        return os.path.join(self._root(), target)

    def _next_set_name(self):
        root = self._root()
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        for n in range(1, 1000):
            if not os.path.lexists(os.path.join(root, f"{day}-{n}")):
                return f"{day}-{n}"
        fail("今天的零件目录序号用完了")

    @staticmethod
    def _write_durable(path, data):
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    def _log_path(self):
        path = os.path.join(self._root(), "decisions.log")
        if os.path.islink(path):
            fail(f"{path} 是连结；需要人处理")
        return path

    def _check_log(self):
        """（锁里）严格检查决定纪录；只自动修复「最后一行写到一半」。回传修复说明或 None。"""
        path = self._log_path()
        try:
            with open(path, "rb") as file:
                data = file.read()
        except FileNotFoundError:
            return None
        try:
            self.candidate.parse_decisions(data)
            return None
        except self.candidate.DecisionsCorrupt:
            pass
        cut = data.rfind(b"\n") + 1                       # 最后一个换行之后，就是写到一半的那一段
        head, tail = data[:cut], data[cut:]
        try:
            self.candidate.parse_decisions(head)
        except self.candidate.DecisionsCorrupt as error:
            fail(f"决定纪录中间有坏行，没有自动修复，需要人处理：{error}")
        if not tail:
            fail("决定纪录有问题但找不到写到一半的尾巴；需要人处理")
        folder = os.path.dirname(path)
        old = [os.path.join(folder, e) for e in os.listdir(folder) if e.startswith("decisions.log.corrupt-")]
        total = sum(os.lstat(f).st_size for f in old) + len(data)
        if len(old) + 1 > MAX_CORRUPT_BACKUPS or total > MAX_CORRUPT_BYTES:
            fail(f"警告：决定纪录又坏了，但坏尾巴的备份已有 {len(old)} 份、共 {total // 1024} KB"
                 f"（上限 {MAX_CORRUPT_BACKUPS} 份、{MAX_CORRUPT_BYTES // 1048576} MB）。这次不自动修复，也不会删旧备份；"
                 "请人看过备份、决定保留哪些后再执行 recover。也请查一下为什么一直写到一半（磁碟满？一再被杀？）")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backup = path + ".corrupt-" + stamp
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temporary = path + ".tmp"
        with open(temporary, "wb") as file:
            file.write(head)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        note = (f"警告：决定纪录最后一行写到一半（{len(tail)} 位元组），已隔离；原档备份在 {backup}"
                f"（目前共 {len(old) + 1} 份备份，上限 {MAX_CORRUPT_BACKUPS} 份）")
        print(json.dumps({"repaired": note}, ensure_ascii=False), file=sys.stderr)
        return note

    def _decide(self, request_id, decision, **extra):
        self._check_log()
        path = self._log_path()
        row = {"request": request_id, "decision": decision,
               "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **extra}
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(path, 0, 0)
        os.chmod(path, 0o644)
        with open(path, "rb") as file:
            written = self.candidate.parse_decisions(file.read())
        if written.get(request_id, {}).get("decision") != decision:
            fail(f"决定纪录写完读不回 {request_id}；需要人处理")

    def recover(self):
        """（锁里）有意图档就照上面的规则修复。回传做了什么（没事就是 None）。"""
        path = self._journal_path()
        if not os.path.lexists(path):
            return None
        if os.path.islink(path):
            fail(f"{path} 是连结")
        try:
            with open(path, encoding="utf-8") as file:
                intent = json.load(file)
        except (OSError, ValueError, RecursionError):
            fail(f"{path} 读不懂；没有动任何东西。请人工检查 current 与 decisions.log 后再删掉它")
        problem = self._intent_problem(intent)
        if problem:
            fail(f"{path} 不是完整的意图档（{problem}）；没有动任何东西，请人工检查 current 与 decisions.log 后再处理")
        set_name, previous = intent["set"], intent["previous"]
        folder = os.path.join(self._root(), set_name)
        link = os.path.join(self._root(), "current")
        if not os.path.lexists(link):
            fail(f"上次中断时 current 不存在（意图：{set_name}，原本：{previous}）；没有动任何东西，意图档留在 {path}，需要人处理")
        current = self._current()
        if os.path.basename(current) not in (set_name, previous):
            fail(f"current 指到 {os.path.basename(current)}，既不是意图里的新组 {set_name}、也不是原本那一组 {previous}；"
                 f"没有动任何东西，意图档留在 {path}，需要人处理")
        if os.path.basename(current) == set_name:
            problems = self._set_problems(set_name, intent)
            if problems:
                fail("上次中断后 current 指到 " + set_name + "，但这一组不完整：" + "；".join(problems)
                     + f"。没有动任何东西，意图档留在 {path}；需要人处理（例如把 current 指回 {intent.get('previous')}）")
            if intent["request"] not in self.candidate._decided():
                self._decide(intent["request"], intent["decision"], set=set_name, file=intent["file"], recovered=True)
            action = f"上次在切换 current 之后中断：{intent['request']} 已启用（逐件验证过），补记决定"
        else:
            problems = self._set_problems(previous, None)
            if problems:
                fail("current 指到原本那一组，但它有问题：" + "；".join(problems) + f"。没有动任何东西，意图档留在 {path}；需要人处理")
            if os.path.lexists(folder) and not os.path.islink(folder):
                if os.path.dirname(os.path.realpath(folder)) != os.path.realpath(self._root()):
                    fail(f"{folder} 不在零件总目录里；没有动任何东西，需要人处理")
                shutil.rmtree(folder)
            action = f"上次在切换 current 之前中断：删掉做到一半的 {set_name}，{intent['request']} 仍是待审"
        os.unlink(path)
        print(json.dumps({"recovered": action}, ensure_ascii=False), file=sys.stderr)
        return action

    def _intent_problem(self, intent):
        """意图档的完整格式：只有这五个栏位、型别对、组名与档名格式对、组目录落在零件总目录里。
        回传问题描述（None 代表没问题）。"""
        if not isinstance(intent, dict):
            return "不是 JSON 物件"
        if set(intent) != INTENT_KEYS:
            return f"栏位应为 {sorted(INTENT_KEYS)}，实际是 {sorted(intent)}"
        set_name, previous = intent["set"], intent["previous"]
        if not isinstance(set_name, str) or not self.parts.SET_NAME.match(set_name):
            return f"set={set_name!r} 不是组名"
        if previous is not None and (not isinstance(previous, str) or not self.parts.SET_NAME.match(previous)):
            return f"previous={previous!r} 不是组名"
        if previous == set_name:
            return "新组和原本那一组同名"
        request = intent["request"]
        if (not isinstance(request, str) or not request or len(request) > 200
                or any(c in request for c in "/\\\x00\n")):
            return f"request={request!r} 格式不对"
        if intent["decision"] not in ("sealed", "imported"):
            return f"decision={intent['decision']!r} 不对"
        name = intent["file"]
        if (not isinstance(name, str) or os.path.basename(name) != name or not name.endswith(".png")
                or name.startswith(".") or "\x00" in name):
            return f"file={name!r} 不是普通的 .png 档名"
        root = os.path.realpath(self._root())
        for value in (set_name, previous):
            if value is not None and os.path.dirname(os.path.realpath(os.path.join(root, value))) != root:
                return f"{value} 不在零件总目录里"
        return None

    def _set_problems(self, set_name, intent):
        """一组零件完整吗？目录在、是 root 的、INDEX.md 在、每个零件都通过 load_part（sha、封印、拥有者），
        有意图时：新零件在，而且原本那一组的零件都带过来了。回传问题清单（空的才算完整）。"""
        folder = os.path.join(self._root(), set_name)
        problems = []
        if not os.path.isdir(folder) or os.path.islink(folder):
            return [f"{set_name} 不存在或是连结"]
        try:
            self.parts._safe(folder, "dir")
        except self.parts.PartError as error:
            problems.append(str(error))
        if intent and not os.path.isfile(os.path.join(folder, "INDEX.md")):
            problems.append("没有 INDEX.md（这一组没建完）")
        pngs = sorted(e for e in os.listdir(folder) if e.endswith(".png"))
        for entry in pngs:
            try:
                self.parts.load_part(os.path.join(folder, entry))
            except self.parts.PartError as error:
                problems.append(f"{entry}：{error}")
        if intent:
            if intent["file"] not in pngs:
                problems.append(f"新零件 {intent['file']} 不在")
            previous = intent.get("previous")
            if previous:
                old = os.path.join(self._root(), previous)
                if os.path.isdir(old):
                    missing = sorted(set(e for e in os.listdir(old) if e.endswith(".png")) - set(pngs))
                    if missing:
                        problems.append(f"原本那一组的零件不见了：{missing}")
        return problems

    def _publish(self, request_id, decision, png_name, image_bytes, record, lock=None):
        """（锁里）照「可恢复的提交」的五步建新的一组并启用。"""
        parts = self.parts
        if os.path.basename(png_name) != png_name or not png_name.endswith(".png") or png_name.startswith("."):
            fail(f"名字要是普通的 .png 档名：{png_name!r}")
        current = self._current()
        if current and os.path.lexists(os.path.join(current, png_name)):
            fail(f"目前那一组已经有 {png_name}；换一个名字")
        set_name = self._next_set_name()
        folder = os.path.join(self._root(), set_name)
        journal = self._journal_path()
        self._write_durable(journal, {"set": set_name, "request": request_id, "decision": decision,
                                      "file": png_name, "previous": os.path.basename(current) if current else None})
        self._crashpoint("seal-after-intent")
        try:
            os.mkdir(folder, 0o755)       # 一定是新建的（已存在、或被预先放了连结都会失败）
            os.chown(folder, 0, 0)
            os.chmod(folder, 0o755)
            if current:
                for entry in sorted(os.listdir(current)):
                    if entry.endswith(".png"):
                        parts.load_part(os.path.join(current, entry))   # 旧零件也要验证过才带过去
                        for suffix in (".png", ".json"):
                            name = entry[:-4] + suffix
                            shutil.copyfile(os.path.join(current, name), os.path.join(folder, name),
                                            follow_symlinks=False)
            with open(os.path.join(folder, png_name), "xb") as file:
                file.write(image_bytes)
            with open(os.path.join(folder, png_name[:-4] + ".json"), "x", encoding="utf-8") as file:
                json.dump(record, file, ensure_ascii=False, indent=1)
            index = ["# 零件（这一组：" + set_name + "）", ""]
            for entry in sorted(os.listdir(folder)):
                if entry.endswith(".json"):
                    with open(os.path.join(folder, entry), encoding="utf-8") as f:
                        meta = json.load(f)
                    edges = "、".join(meta["cut_edges_ok"]) or "无"
                    index.append(f"- `{meta['file']}`：姿势 {meta['pose']}，{meta['size'][0]}×{meta['size'][1]}，故意裁掉的边：{edges}")
            with open(os.path.join(folder, "INDEX.md"), "x", encoding="utf-8") as file:
                file.write("\n".join(index) + "\n")
            for entry in os.listdir(folder):
                os.chown(os.path.join(folder, entry), 0, 0)
                os.chmod(os.path.join(folder, entry), 0o644)
            for entry in os.listdir(folder):
                if entry.endswith(".png"):
                    parts.load_part(os.path.join(folder, entry))
            os.sync()
        except Exception:
            # 一般错误：当场清掉（还没切换 current）。被杀掉的话由下次的 recover 清。
            if os.path.lexists(folder) and not os.path.islink(folder):
                shutil.rmtree(folder, ignore_errors=True)
            os.unlink(journal)
            raise
        self._crashpoint("seal-after-build")
        if lock is not None:
            lock.check()                                  # 共用锁还在、没被换掉，才启用
        link = os.path.join(self._root(), "current")
        temporary = link + ".new"
        if os.path.lexists(temporary):
            os.unlink(temporary)
        os.symlink(set_name, temporary)
        os.lchown(temporary, 0, 0)
        os.replace(temporary, link)
        self._crashpoint("seal-after-switch")
        self._decide(request_id, decision, set=set_name, file=png_name)
        self._crashpoint("seal-after-decision")
        os.unlink(journal)
        part = parts.load_part(os.path.join(link, png_name))   # 透过 current 读一次，确认她那条路径可用
        return folder, part

    # ---------- 指令 ----------
    def cmd_requests(self, args):
        try:
            with self._seal_lock(), self._shared():
                self._check_log()
                self.recover()
        except SystemExit:
            print(json.dumps({"note": "另一个封印正在执行，这次没有检查中断状态"}, ensure_ascii=False), file=sys.stderr)
        rows = [{k: r.get(k) for k in ("request", "name", "pose", "note", "remove_ids", "cut_edges_ok")}
                for r in self.candidate.pending()]
        print(json.dumps(rows, ensure_ascii=False, indent=1))

    def cmd_recover(self, args):
        with self._seal_lock(), self._shared():
            repaired = self._check_log()
            action = self.recover()
        print(json.dumps({"recovered": action or "没有需要修复的", "log": repaired or "完好"}, ensure_ascii=False))

    def cmd_review(self, args):
        req = self.request(args.request)
        cut, source = self.rederive(req)
        sheet, removed = self.candidate.removed_overlay(source, req["crop"], req["remove_ids"], req.get("shadow_box"))
        folder = os.path.join(REVIEW_ROOT, req["request"])
        os.makedirs(REVIEW_ROOT, mode=0o700, exist_ok=True)
        os.chmod(REVIEW_ROOT, 0o700)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        from whaledraw.check import edge_points, edge_sheet, enclosed_sheet
        import numpy as np
        base = self.candidate.derive(source, req["crop"], (), req.get("shadow_box"))
        enclosed_sheet(base).save(os.path.join(folder, "enclosed.png"))
        sheet.save(os.path.join(folder, "removed-and-result.png"))
        points = edge_points(np.asarray(cut.image.getchannel("A")))
        if points:
            edge_sheet(cut.image, points).save(os.path.join(folder, "edges.png"))
        from whaledraw.cutout import cut_edges, edge_quality
        summary = {"request": req["request"], "pose": req["pose"], "note": req["note"], "source": req["source"],
                   "crop": req["crop"], "remove_ids": req["remove_ids"], "removed_pixels": removed,
                   "quality": edge_quality(cut.image), "cut_edges": cut_edges(cut.image),
                   "cut_edges_ok": req["cut_edges_ok"], "candidate_matches_rederived": True, "look_at": folder,
                   "reminder": "自检数字不能代替看图：一定要看 look_at 里的三张图再决定"}
        print(json.dumps(summary, ensure_ascii=False, indent=1))

    def cmd_seal(self, args):
        with self._seal_lock(), self._shared() as lock:
            self._check_log()
            self.recover()
            req = self.request(args.request)       # 锁里重读：她刚撤回、或别人刚处理过，这里就会拒绝
            cut, _ = self.rederive(req)
            buffer = io.BytesIO()
            cut.image.save(buffer, format="PNG")
            data = buffer.getvalue()
            record = self.parts.seal_record(data, args.as_name, req["pose"], req["cut_edges_ok"], extra={
                "request": req["request"], "source": req["source"], "source_sha256": req["source_sha256"],
                "crop": req["crop"], "shadow_box": req.get("shadow_box"), "remove_ids": req["remove_ids"],
                "note": req["note"]})
            folder, part = self._publish(req["request"], "sealed", args.as_name, data, record, lock)
        print(json.dumps({"sealed": os.path.join(self.parts.PARTS_ROOT, "current", args.as_name),
                          "set": folder, "expected_hole_pixels": part.expected_holes}, ensure_ascii=False))

    def cmd_reject(self, args):
        if not args.reason.strip():
            fail("退回要写理由")
        with self._seal_lock(), self._shared() as lock:
            self._check_log()
            self.recover()
            req = self.request(args.request)
            lock.check()
            self._decide(req["request"], "rejected", reason=args.reason.strip()[:300])
        print(json.dumps({"rejected": req["request"]}, ensure_ascii=False))

    def cmd_import(self, args):
        with open(args.png, "rb") as file:
            data = file.read()
        image = self.Image.open(io.BytesIO(data))
        image.load()
        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, format="PNG")
        data = buffer.getvalue()
        sides = [s for s in args.cut_edges.split(",") if s]
        record = self.parts.seal_record(data, args.as_name, args.pose, sides,
                                        extra={"imported_from": os.path.abspath(args.png)})
        with self._seal_lock(), self._shared() as lock:
            self._check_log()
            self.recover()
            request_id = "import-" + args.as_name + "-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            folder, part = self._publish(request_id, "imported", args.as_name, data, record, lock)
        print(json.dumps({"sealed": os.path.join(self.parts.PARTS_ROOT, "current", args.as_name),
                          "set": folder, "expected_hole_pixels": part.expected_holes}, ensure_ascii=False))


def main(argv):
    parser = argparse.ArgumentParser(prog="seal_part.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version_dir")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("requests")
    sub.add_parser("recover")
    sub.add_parser("review").add_argument("request")
    s = sub.add_parser("seal")
    s.add_argument("request")
    s.add_argument("--as", dest="as_name", required=True)
    r = sub.add_parser("reject")
    r.add_argument("request")
    r.add_argument("--reason", required=True)
    i = sub.add_parser("import")
    i.add_argument("png")
    i.add_argument("--as", dest="as_name", required=True)
    i.add_argument("--pose", required=True)
    i.add_argument("--cut-edges", default="")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        fail("封印只能由维护帐号以 root 执行（sudo）")
    tool = Tool(args.version_dir)
    try:
        getattr(tool, "cmd_" + args.command)(args)
    except (tool.candidate.CandidateError, tool.parts.PartError) as error:
        fail(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
