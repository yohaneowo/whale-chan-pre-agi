import contextlib
from posixpath import basename as posixpath_basename
import datetime
import hashlib
import importlib.util as importlib_util
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import configure_social as cs

HERE = pathlib.Path(__file__).parent
POSIX = os.name == "posix"


class BundleTest(unittest.TestCase):
    def test_default_bundle_is_exactly_the_social_56_release(self):
        """直接断言目标版本：这次部署的是 social.56。只比对「彼此同步」会让整组一起停在旧版时也通过。"""
        self.assertEqual(cs.DEFAULT_BUNDLE, {"discord": "discord-0.6.0-social.56", "social": "social-0.1.50",
                                             "presets": "social-presets-0.53.0"})

    def test_default_bundle_matches_what_the_prepare_scripts_build(self):
        """预设的三个套件版本，要跟 prepare_*.py 产出的版本、social/package.json 一致（升级时一起改，不一致就是漏改）。"""
        discord = (HERE / "prepare_discord_plugin.py").read_text(encoding="utf-8")
        materials = (HERE / "prepare_social_materials.py").read_text(encoding="utf-8")
        self.assertIn('"' + cs.DEFAULT_BUNDLE["discord"].split("discord-")[1] + '"', discord)
        self.assertIn(cs.DEFAULT_BUNDLE["social"], discord)
        self.assertIn(cs.DEFAULT_BUNDLE["social"], materials)
        self.assertIn(cs.DEFAULT_BUNDLE["presets"], materials)
        self.assertIn('"version": "' + cs.DEFAULT_BUNDLE["presets"].split("presets-")[1] + '"', materials)
        version = cs.DEFAULT_BUNDLE["social"].split("social-")[1]
        package = (HERE.parent / "social" / "package.json").read_text(encoding="utf-8")
        self.assertIn('"version": "' + version + '"', package)
        self.assertNotIn("social.55", discord)
        self.assertNotIn("0.1.49", materials)

    def test_prepare_scripts_read_upstream_package_json_strictly(self):
        """上游（fish、opencc、discord 桥接）的 package.json 也不当成可信的：prepare_*.py 一律用 strict_json 读。"""
        for name in ("prepare_discord_plugin.py", "prepare_social_materials.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertIn("strict_json(", text, name)
            self.assertNotRegex(text, r"json\.loads\([^)]*package\.json", name)

    def test_default_command_installs_the_current_bundle(self):
        command = cs.plugin_command()
        self.assertEqual([c for c in command if c.startswith("file:")],
                         ["file:/opt/dsh/vendor/" + cs.DEFAULT_BUNDLE[k] for k in ("discord", "social", "presets")])

    def test_rollback_to_the_previous_release_with_the_same_flags(self):
        """回滚走跟升级同一条路，只多三个参数：指到 social.55 的目录，其余（贴图、梗图、换气）照旧。"""
        command = cs.plugin_command("discord-0.6.0-social.55", "/opt/dsh/vendor/social-0.1.49", "social-presets-0.52.0",
                                    "/opt/dsh/vendor/nekowhale-1.1.1", "/opt/dsh/vendor/dsh-meme-0.1.64",
                                    "/opt/dsh/vendor/huanqi-1.1.0")
        files = [c for c in command if c.startswith("file:")]
        self.assertEqual(files[:3], ["file:/opt/dsh/vendor/discord-0.6.0-social.55", "file:/opt/dsh/vendor/social-0.1.49",
                                     "file:/opt/dsh/vendor/social-presets-0.52.0"])
        self.assertEqual(len(files), 6)
        self.assertEqual(command[-2:], ["--config.auto-install-peers=false", "--ignore-scripts"])

    def test_names_and_paths_must_be_release_folders_directly_under_vendor(self):
        bad = [("discord", "/tmp/discord-0.6.0-social.55"), ("discord", "/opt/dsh/vendor/../discord-0.6.0-social.55"),
               ("discord", "discord-0.6.0-social.55/../x"), ("discord", "/opt/dsh/vendor/sub/discord-0.6.0-social.55"),
               ("discord", "discord-0.6.0-social.x"), ("discord", "social-0.1.49"), ("social", "social-presets-0.52.0"),
               ("presets", "social-0.1.49"), ("social", "/opt/dsh/vendor/social-0.1.49/"), ("social", "social-0.1.49 ; rm -rf /"),
               ("presets", "social-presets-0.52.1")]
        for kind, value in bad:
            with self.assertRaises(ValueError, msg=f"{kind} {value}"):
                cs.bundle_dir(kind, value)
        self.assertEqual(cs.bundle_dir("social", "social-0.1.48"), "/opt/dsh/vendor/social-0.1.48")
        self.assertEqual(cs.bundle_dir("presets", "/opt/dsh/vendor/social-presets-0.51.0"),
                         "/opt/dsh/vendor/social-presets-0.51.0")

    def test_extra_dirs_are_also_direct_children_of_vendor(self):
        self.assertEqual(cs.extra_dir("nekowhale-1.1.1", "--stickers"), "/opt/dsh/vendor/nekowhale-1.1.1")
        for bad in ("/tmp/nekowhale-1.1.1", "/opt/dsh/vendor/../x", "x/../y", ".hidden", "/opt/dsh/vendor/a/b", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                cs.extra_dir(bad, "--stickers")


class StrictInputTest(unittest.TestCase):
    """configure_social.py 读外部输入的地方：严格 JSON、验收结果档、profile 的 YAML。不需要 POSIX。"""

    def test_strict_json_accepts_ordinary_json(self):
        self.assertEqual(cs.strict_json(b'{"a": [1, 2.5, true, null, "x"], "b": {"c": -3}}'),
                         {"a": [1, 2.5, True, None, "x"], "b": {"c": -3}})

    def test_strict_json_refuses_everything_the_default_parser_lets_through(self):
        bad = {"NaN": b"NaN", "Infinity": b"Infinity", "-Infinity": b"-Infinity", "nested NaN": b'{"a": [NaN]}',
               "overflow": b"1e999", "negative overflow": b"-1e999", "duplicate": b'{"a": 1, "a": 2}',
               "nested duplicate": b'{"x": {"a": 1, "a": 1}}', "BOM": b'\xef\xbb\xbf{}', "not utf-8": b'"\xff"',
               "deep": b"[" * 50000 + b"]" * 50000, "trailing junk": b"{} x", "single quotes": b"{'a': 1}", "empty": b""}
        for label, raw in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                cs.strict_json(raw)

    def test_validate_e2e_result_checks_every_item_not_just_the_count(self):
        """Rin 第 7 轮：checks=[false]*13 以前会过。现在每一项都得是 e2e 真的会 push 的那一项通过的名称，顺序也要一样。"""
        good = {"ok": True, "checks": list(cs.EXPECTED_E2E_CHECKS), "modelRequests": 27}
        self.assertTrue(cs.validate_e2e_result(json.dumps(good).encode())["ok"])
        names = list(cs.EXPECTED_E2E_CHECKS)
        bad = {"false x13": dict(good, checks=[False] * 13), "false x19": dict(good, checks=[False] * 19), "true x19": dict(good, checks=[True] * 19),
               "numbers": dict(good, checks=list(range(19))), "too few": dict(good, checks=names[:18]), "too many": dict(good, checks=names + ["extra"]),
               "one wrong name": dict(good, checks=names[:-1] + ["达到用量上限后真实流水线仍会请求模型"]), "reordered": dict(good, checks=names[::-1]),
               "duplicate item": dict(good, checks=names[:-1] + [names[0]]), "not a list": dict(good, checks="all"),
               "ok false": dict(good, ok=False), "ok string": dict(good, ok="true"), "ok 1": dict(good, ok=1),
               "extra field": dict(good, error="boom"), "no modelRequests": {"ok": True, "checks": names},
               "modelRequests 0": dict(good, modelRequests=0), "modelRequests bool": dict(good, modelRequests=True),
               "modelRequests string": dict(good, modelRequests="27"), "a list": [1, 2, 3]}
        for label, content in bad.items():
            with self.subTest(label), self.assertRaises(RuntimeError):
                cs.validate_e2e_result(json.dumps(content).encode())
        for label, raw in (("NaN", b'{"ok": true, "checks": [], "modelRequests": NaN}'), ("broken", b"{"), ("empty", b""),
                           ("duplicate ok", b'{"ok": false, "ok": true, "checks": [], "modelRequests": 1}')):
            with self.subTest(label), self.assertRaises(RuntimeError):
                cs.validate_e2e_result(raw)

    def test_expected_e2e_checks_match_the_e2e_script_in_the_repo(self):
        """social-e2e.mjs 成功时依序 push 的名称，要跟这里的清单逐字相同；改了一边没改另一边就会在这里失败。"""
        script = (HERE / "social-e2e.mjs").read_text(encoding="utf-8")
        pushed = re.findall(r"checks\.push\('([^']*)'\)", script)
        self.assertEqual(tuple(pushed), cs.EXPECTED_E2E_CHECKS)
        self.assertEqual(len(set(pushed)), len(pushed), "名称不能重复")

    def test_rollback_flag_rules(self):
        cs.validate_rollback("discord-0.6.0-social.55", "social-0.1.49", "social-presets-0.52.0")
        for args in ((None, "social-0.1.49", "social-presets-0.52.0"), ("discord-0.6.0-social.55", None, "social-presets-0.52.0"),
                     ("discord-0.6.0-social.55", "social-0.1.49", None), (None, None, None),
                     ("discord-0.6.0-social.56", "social-0.1.50", "social-presets-0.53.0")):          # 就是预设：不是回滚
            with self.subTest(args), self.assertRaises(RuntimeError):
                cs.validate_rollback(*args)
        with self.assertRaises(ValueError):                                                              # 名称不合规一样挡
            cs.validate_rollback("discord-0.6.0-social.55", "evil", "social-presets-0.52.0")

    def test_rollback_accepts_only_whole_deployed_groups(self):
        """Rin 第 8 轮：以前只拒绝「当前预设」，其他任何组合都放行。现在只收 ROLLBACK_GROUPS 里的整组。"""
        social55 = ("discord-0.6.0-social.55", "social-0.1.49", "social-presets-0.52.0")
        cs.validate_rollback(*social55)                                                                     # 白名单内的整组：过
        cs.validate_rollback(*("/opt/dsh/vendor/" + name for name in social55))                           # 写完整路径也一样
        mixed = {"discord from .55, the rest from .54": ("discord-0.6.0-social.55", "social-0.1.48", "social-presets-0.51.0"),
                 "social from .55, the rest from .54": ("discord-0.6.0-social.54", "social-0.1.49", "social-presets-0.51.0"),
                 "presets from .55, the rest current": ("discord-0.6.0-social.56", "social-0.1.50", "social-presets-0.52.0"),
                 "discord current, the rest .55": ("discord-0.6.0-social.56", "social-0.1.49", "social-presets-0.52.0")}
        for label, args in mixed.items():
            with self.subTest(label), self.assertRaises(RuntimeError):
                cs.validate_rollback(*args)
        complete_but_not_listed = {"social.54 (deployed earlier, not on the list)": ("discord-0.6.0-social.54", "social-0.1.48", "social-presets-0.51.0"),
                                   "made-up complete group": ("discord-0.6.0-social.99", "social-0.1.99", "social-presets-0.99.0")}
        for label, args in complete_but_not_listed.items():
            with self.subTest(label), self.assertRaises(RuntimeError) as caught:
                cs.validate_rollback(*args)
            self.assertIn("白名单", str(caught.exception))

    def test_rollback_mixing_two_listed_groups_is_called_out_as_mixing(self):
        second = {"discord": "discord-0.6.0-social.54", "social": "social-0.1.48", "presets": "social-presets-0.51.0"}
        with mock.patch.dict(cs.ROLLBACK_GROUPS, {"social.54": second}):
            cs.validate_rollback("discord-0.6.0-social.54", "social-0.1.48", "social-presets-0.51.0")       # 整组 54：现在在名单内
            cs.validate_rollback("discord-0.6.0-social.55", "social-0.1.49", "social-presets-0.52.0")       # 整组 55
            with self.assertRaises(RuntimeError) as caught:                                                    # 每个套件各自都部署过，但不同组
                cs.validate_rollback("discord-0.6.0-social.55", "social-0.1.48", "social-presets-0.52.0")
            self.assertIn("混搭", str(caught.exception))

    def test_rollback_groups_are_well_formed(self):
        self.assertIn("social.55", cs.ROLLBACK_GROUPS)
        names = set()
        for label, group in cs.ROLLBACK_GROUPS.items():
            self.assertEqual(set(group), {"discord", "social", "presets"}, label)
            for kind, name in group.items():
                self.assertEqual(posixpath_basename(cs.bundle_dir(kind, name)), name, f"{label} 的 {kind} 名称不合规")
            self.assertNotEqual(group, cs.DEFAULT_BUNDLE, "白名单里不该有这一版的预设（那是升级，不是回滚）")
            names.add(tuple(sorted(group.items())))
        self.assertEqual(len(names), len(cs.ROLLBACK_GROUPS), "不该有重复的组")

    @unittest.skipUnless(importlib_util.find_spec("yaml"), "没有装 PyYAML")
    def test_profile_yaml_is_strict(self):
        self.assertEqual(cs.load_profile_rows("- id: a\n  config: {x: 1}\n- id: b\n"), [{"id": "a", "config": {"x": 1}}, {"id": "b"}])
        base = "- id: base\n  config: &c {x: 1, y: 2}\n- id: other\n  config:\n    <<: *c\n    y: 3\n"       # 合并键盖掉原值：合法
        self.assertEqual(cs.load_profile_rows(base)[1]["config"], {"x": 1, "y": 3})
        for label, text in (("duplicate key", "- id: a\n  id: b\n"), ("nested duplicate", "- id: a\n  config:\n    x: 1\n    x: 2\n"),
                            ("not a list", "id: a\n"), ("list of scalars", "- a\n- b\n"), ("empty", ""), ("broken", "- id: [a\n"),
                            ("python object", "- !!python/object/apply:os.system ['true']\n")):
            with self.subTest(label), self.assertRaises(RuntimeError):
                cs.load_profile_rows(text)


class Hang(Exception):
    pass


@contextlib.contextmanager
def no_hang(seconds=10):
    """FIFO 之类的测试：万一代码卡住，计时器会丢例外把测试弄成失败，而不是让整个测试跑不完。"""
    def fire(signum, frame):
        raise Hang(f"卡住超过 {seconds} 秒")
    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@unittest.skipUnless(POSIX and hasattr(os, "mkfifo") and hasattr(signal, "setitimer"), "要 FIFO、符号链接与计时器，只在 Linux 跑")
class SafeReadTest(unittest.TestCase):
    """Rin 第 7 轮：root 读 dsh 可写的路径（result.json、cordis.patch.yml、要备份的档案）以前直接 open／copyfile，
    dsh 换成 FIFO 就能让 root 永远卡住。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.uid = os.getuid()

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, path, limit=1 << 20, owners=None, trusted=False):
        with no_hang():
            return cs.read_regular_file(str(path), limit, owners, trusted)

    def test_a_regular_file_is_read(self):
        (self.root / "a").write_bytes(b"hello")
        self.assertEqual(self.read(self.root / "a", owners={self.uid}), b"hello")
        (self.root / "empty").write_bytes(b"")
        self.assertEqual(self.read(self.root / "empty"), b"")
        (self.root / "exact").write_bytes(b"x" * 100)
        self.assertEqual(len(self.read(self.root / "exact", limit=100)), 100)

    def test_fifo_is_refused_and_does_not_hang(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(cs.UnsafeFile) as caught:
            self.read(self.root / "pipe")
        self.assertIn("不是一般档案", str(caught.exception))

    def test_fifo_with_a_writer_is_refused_too(self):
        os.mkfifo(self.root / "pipe")
        writer = os.open(self.root / "pipe", os.O_RDWR)          # 另一头有人开着
        try:
            with self.assertRaises(cs.UnsafeFile):
                self.read(self.root / "pipe")
        finally:
            os.close(writer)

    def test_symlinks_are_refused(self):
        (self.root / "real").write_bytes(b"secret")
        os.symlink(self.root / "real", self.root / "link")
        os.symlink(self.root / "nowhere", self.root / "dangling")
        os.mkfifo(self.root / "pipe")
        os.symlink(self.root / "pipe", self.root / "link-to-pipe")
        for name in ("link", "dangling", "link-to-pipe"):
            with self.subTest(name), self.assertRaises(cs.UnsafeFile):
                self.read(self.root / name)

    def test_directory_and_device_are_refused(self):
        (self.root / "folder").mkdir()
        with self.assertRaises(cs.UnsafeFile):
            self.read(self.root / "folder")
        with self.assertRaises(cs.UnsafeFile):
            self.read("/dev/null")
        with self.assertRaises(cs.UnsafeFile):
            self.read("/dev/zero")                                # 读不完的装置：不会卡住也不会读爆

    def test_oversized_file_is_refused(self):
        (self.root / "big").write_bytes(b"x" * 1001)
        with self.assertRaises(cs.UnsafeFile) as caught:
            self.read(self.root / "big", limit=1000)
        self.assertIn("太大", str(caught.exception))

    def test_owner_and_mode_rules(self):
        (self.root / "a").write_bytes(b"x")
        with self.assertRaises(cs.UnsafeFile) as caught:
            self.read(self.root / "a", owners={self.uid + 1})
        self.assertIn("拥有者", str(caught.exception))
        os.chmod(self.root / "a", 0o664)
        self.read(self.root / "a", owners={self.uid})
        with self.assertRaises(cs.UnsafeFile):
            self.read(self.root / "a", owners={self.uid}, trusted=True)
        os.chmod(self.root / "a", 0o644)
        self.assertEqual(self.read(self.root / "a", owners={self.uid}, trusted=True), b"x")

    def test_missing_file_is_a_refusal_not_a_traceback(self):
        with self.assertRaises(cs.UnsafeFile):
            self.read(self.root / "missing")

    def test_tree_files_refuses_symlinks_and_special_files_anywhere(self):
        (self.root / "t" / "sub").mkdir(parents=True)
        (self.root / "t" / "sub" / "a.js").write_bytes(b"1")
        self.assertEqual(sorted(cs.tree_files(str(self.root / "t"))), ["sub/a.js"])
        os.mkfifo(self.root / "t" / "sub" / "pipe")
        with no_hang(), self.assertRaises(cs.UnsafeFile):
            cs.tree_files(str(self.root / "t"))
        os.unlink(self.root / "t" / "sub" / "pipe")
        os.symlink(self.root / "t" / "sub" / "a.js", self.root / "t" / "link.js")
        with no_hang(), self.assertRaises(cs.UnsafeFile):
            cs.tree_files(str(self.root / "t"))
        os.unlink(self.root / "t" / "link.js")
        os.symlink(self.root, self.root / "t" / "up")
        with no_hang(), self.assertRaises(cs.UnsafeFile):
            cs.tree_files(str(self.root / "t"))

    def test_profile_backup_copy_refuses_fifo_and_symlink_and_never_hangs(self):
        backup = self.root / "backup"
        backup.mkdir()
        (self.root / "ok.json").write_bytes(b"{}")
        cs.copy_profile_file(str(self.root / "ok.json"), str(backup / "ok.json"), {self.uid})
        self.assertEqual((backup / "ok.json").read_bytes(), b"{}")
        self.assertEqual(stat.S_IMODE((backup / "ok.json").stat().st_mode), 0o600)
        os.mkfifo(self.root / "state.json")
        os.symlink("/etc/hostname", self.root / "link.json")
        for name in ("state.json", "link.json"):
            with self.subTest(name), no_hang(), self.assertRaises(cs.UnsafeFile):
                cs.copy_profile_file(str(self.root / name), str(backup / name), {self.uid})

    def test_writing_the_profile_file_replaces_a_symlink_instead_of_following_it(self):
        """root 写 cordis.patch.yml：dsh 事先把它换成指向别处的符号链接，写的是链接本身被换掉，指向的档案原封不动。"""
        victim = self.root / "victim.txt"
        victim.write_text("precious")
        target = self.root / "cordis.patch.yml"
        os.symlink(victim, target)
        cs.write_new_file(str(target), b"new: content\n", 0o600, os.getuid(), os.getgid())
        self.assertEqual(victim.read_text(), "precious")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), b"new: content\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".")], [], "暂存档要清掉")

    def test_check_profile_dir_refuses_symlinked_paths_and_wrong_owners(self):
        (self.root / "profiles").mkdir()
        os.symlink(self.root / "profiles", self.root / "linked")
        cs.check_profile_dir(str(self.root / "profiles"), {self.uid})
        with self.assertRaises(cs.UnsafeFile):
            cs.check_profile_dir(str(self.root / "linked"), {self.uid})
        with self.assertRaises(cs.UnsafeFile):
            cs.check_profile_dir(str(self.root / "profiles"), {self.uid + 1})
        (self.root / "afile").write_text("x")
        with self.assertRaises(cs.UnsafeFile):
            cs.check_profile_dir(str(self.root / "afile"), {self.uid})


    # ---- Rin 第 8 轮 A1：处理短写 ----
    DATA = b"abcdefghij" * 40

    @staticmethod
    def inject(mode):
        """替身 os.write：short＝每次只写 3 字节并如实回报；zero＝写不进去（回 0）；
        lying＝只写 1 字节却回报全写了；garbled＝写的是别的内容（长度对）。"""
        real = os.write

        def fake(fd, data):
            data = bytes(data)
            if mode == "short":
                return real(fd, data[:3])
            if mode == "zero":
                return 0
            if mode == "lying":
                real(fd, data[:1])
                return len(data)
            real(fd, b"X" * len(data))
            return len(data)
        return mock.patch.object(cs.os, "write", side_effect=fake)

    def leftovers(self, folder):
        return sorted(p.name for p in pathlib.Path(folder).iterdir() if p.name.startswith("."))

    def test_short_writes_are_completed_by_the_write_all_loop(self):
        target = self.root / "cordis.patch.yml"
        target.write_bytes(b"old")
        with self.inject("short"):
            cs.write_new_file(str(target), self.DATA, 0o600)
        self.assertEqual(target.read_bytes(), self.DATA)
        self.assertEqual(self.leftovers(self.root), [])
        backup = self.root / "backup"
        backup.mkdir()
        (self.root / "src.json").write_bytes(self.DATA)
        with self.inject("short"):
            cs.copy_profile_file(str(self.root / "src.json"), str(backup / "src.json"), {self.uid})
        self.assertEqual((backup / "src.json").read_bytes(), self.DATA)

    def test_a_write_of_zero_bytes_is_an_error_and_the_original_is_untouched(self):
        target = self.root / "cordis.patch.yml"
        target.write_bytes(b"old content")
        with self.inject("zero"), no_hang(), self.assertRaises(cs.ShortWrite) as caught:           # 不能无限循环
            cs.write_new_file(str(target), self.DATA, 0o600)
        self.assertIn("写入停住", str(caught.exception))
        self.assertEqual(target.read_bytes(), b"old content", "没写完就不能 rename 盖掉原来的")
        self.assertEqual(self.leftovers(self.root), [], "暂存档要清掉")
        backup = self.root / "backup"
        backup.mkdir()
        (self.root / "src.json").write_bytes(self.DATA)
        with self.inject("zero"), no_hang(), self.assertRaises(cs.ShortWrite):
            cs.copy_profile_file(str(self.root / "src.json"), str(backup / "src.json"), {self.uid})
        self.assertEqual(sorted(p.name for p in backup.iterdir()), [], "坏掉的备份不能留下（连暂存档也不留）")

    def test_a_write_that_claims_full_success_but_stored_less_is_caught(self):
        """Rin 注入的短写：os.write 回报全写完了，实际只写了 1 字节。以前两个函数都静默回报成功。"""
        target = self.root / "cordis.patch.yml"
        target.write_bytes(b"old content")
        for mode in ("lying", "garbled"):
            with self.subTest(mode), self.inject(mode), self.assertRaises(cs.ShortWrite):
                cs.write_new_file(str(target), self.DATA, 0o600)
            self.assertEqual(target.read_bytes(), b"old content")
            self.assertEqual(self.leftovers(self.root), [])
        backup = self.root / "backup"
        backup.mkdir()
        (self.root / "src.json").write_bytes(self.DATA)
        for mode in ("lying", "garbled"):
            with self.subTest("backup " + mode), self.inject(mode), self.assertRaises(cs.ShortWrite):
                cs.copy_profile_file(str(self.root / "src.json"), str(backup / "src.json"), {self.uid})
            self.assertEqual(sorted(p.name for p in backup.iterdir()), [])

    def test_staged_file_only_replaces_the_target_when_committed(self):
        target = self.root / "cordis.patch.yml"
        target.write_bytes(b"old")
        temp = cs.stage_file(str(target), b"new", 0o600)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(pathlib.Path(temp).read_bytes(), b"new")
        cs.discard_file(temp)                                               # 例如停 dsh 失败：丢掉暂存档
        self.assertEqual((target.read_bytes(), self.leftovers(self.root)), (b"old", []))
        cs.discard_file(temp)                                               # 重复丢弃不出错
        temp = cs.stage_file(str(target), b"new", 0o600)
        cs.commit_file(temp, str(target))
        self.assertEqual((target.read_bytes(), self.leftovers(self.root)), (b"new", []))

    def test_copy_profile_file_refuses_to_overwrite_an_existing_backup(self):
        (self.root / "src.json").write_bytes(b"{}")
        (self.root / "dst.json").write_bytes(b"precious")
        with self.assertRaises(FileExistsError):
            cs.copy_profile_file(str(self.root / "src.json"), str(self.root / "dst.json"), {self.uid})
        self.assertEqual((self.root / "dst.json").read_bytes(), b"precious")


@unittest.skipUnless(POSIX and hasattr(os, "mkfifo") and hasattr(signal, "setitimer"), "要 FIFO、符号链接与计时器，只在 Linux 跑")
class E2eGateTest(unittest.TestCase):
    """root 亲眼看过的 e2e 成功（证明）要跟现在要装的套件、result.json 绑在一起。路径、拥有者都换成临时目录与目前的帐号。"""

    NAMES = {"discord": "dsh-plugin-discord", "social": "dsh-social-runtime", "presets": "dsh-social-presets"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.uid = os.getuid()
        self.vendor = self.root / "vendor"
        self.modules = self.root / "e2e" / "node_modules"
        self.modules.mkdir(parents=True)
        self.bundles = {}
        for kind, folder in (("discord", "discord-0.6.0-social.56"), ("social", "social-0.1.50"), ("presets", "social-presets-0.53.0")):
            path = self.vendor / folder
            (path / "lib").mkdir(parents=True)
            (path / "package.json").write_text(json.dumps({"name": self.NAMES[kind], "version": folder}))
            (path / "lib" / "index.js").write_text("// " + folder)
            shutil.copytree(path, self.modules / self.NAMES[kind])
            self.bundles[kind] = str(path)
        self.result = self.root / "e2e" / "result.json"
        self.good_result = {"ok": True, "checks": list(cs.EXPECTED_E2E_CHECKS), "modelRequests": 27}
        self.result.write_text(json.dumps(self.good_result))
        self.script = self.root / "social-e2e.mjs"
        self.script.write_text("// e2e script")
        os.chmod(self.script, 0o644)
        self.attest = self.root / "e2e-attest.json"
        self.now = datetime.datetime(2026, 11, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def kw(self):
        return dict(result_path=str(self.result), script_path=str(self.script), attest_path=str(self.attest),
                    trusted_uid=self.uid, owners=frozenset({self.uid}))

    def attest_run(self, runner=lambda: 0, bundles=None):
        with no_hang(30):
            return cs.run_and_attest(bundles or self.bundles, runner, now=self.now, modules=str(self.modules), **self.kw())

    def gate(self, bundles=None, now=None, **extra):
        with no_hang(30):
            return cs.verify_e2e_gate(bundles or self.bundles, now=now or self.now + datetime.timedelta(minutes=30), **{**self.kw(), **extra})

    def rewrite_attest(self, **changes):
        data = json.loads(self.attest.read_text())
        data.update(changes)
        self.attest.write_text(json.dumps(data))
        os.chmod(self.attest, 0o644)

    # ---- 正常 ----
    def test_attested_run_passes_the_gate(self):
        attest = self.attest_run()
        self.assertEqual(attest["unit_exit"], 0)
        self.assertEqual(sorted(attest["bundles"]), ["discord", "presets", "social"])
        self.assertEqual(stat.S_IMODE(self.attest.stat().st_mode), 0o644)
        self.gate()

    # ---- run_and_attest 不留下假证明 ----
    def test_failed_unit_leaves_no_attestation_and_removes_an_old_one(self):
        self.attest_run()
        self.assertTrue(self.attest.exists())
        with self.assertRaises(RuntimeError) as caught:
            self.attest_run(runner=lambda: 1)
        self.assertIn("结束码是 1", str(caught.exception))
        self.assertFalse(self.attest.exists(), "失败的这次不能留着上一次的证明")

    def test_green_looking_result_with_a_failed_unit_is_still_refused(self):
        with self.assertRaises(RuntimeError):
            self.attest_run(runner=lambda: 124)
        self.assertFalse(self.attest.exists())

    def test_bad_result_content_leaves_no_attestation(self):
        for content in ({"ok": True, "checks": [False] * 13, "modelRequests": 3}, {"ok": False, "checks": []},
                        dict(self.good_result, checks=list(cs.EXPECTED_E2E_CHECKS)[:-1])):
            self.result.write_text(json.dumps(content))
            with self.subTest(content=str(content)[:40]), self.assertRaises(RuntimeError):
                self.attest_run()
            self.assertFalse(self.attest.exists())

    def test_installed_copy_in_the_e2e_home_must_match_the_vendor_bundle_before_and_after_the_run(self):
        called = []
        (self.modules / "dsh-social-runtime" / "lib" / "index.js").write_text("// tampered before")
        with self.assertRaises(RuntimeError) as caught:
            self.attest_run(runner=lambda: called.append(1) or 0)
        self.assertIn("不一致", str(caught.exception))
        self.assertEqual(called, [], "不一致就不该去跑 e2e")
        shutil.rmtree(self.modules / "dsh-social-runtime")
        shutil.copytree(self.vendor / "social-0.1.50", self.modules / "dsh-social-runtime")
        (self.modules / "dsh-social-runtime" / "extra.js").write_text("extra file")
        with self.assertRaises(RuntimeError):
            self.attest_run()
        os.unlink(self.modules / "dsh-social-runtime" / "extra.js")

        def swap_during_the_run():
            (self.modules / "dsh-plugin-discord" / "lib" / "index.js").write_text("// swapped while running")
            return 0
        with self.assertRaises(RuntimeError):
            self.attest_run(runner=swap_during_the_run)
        self.assertFalse(self.attest.exists())

    def test_node_modules_inside_an_installed_package_is_ignored_but_symlinks_are_not(self):
        (self.modules / "dsh-plugin-discord" / "node_modules" / "dep").mkdir(parents=True)
        (self.modules / "dsh-plugin-discord" / "node_modules" / "dep" / "x.js").write_text("dependency")
        self.attest_run()
        os.symlink("/etc/hostname", self.modules / "dsh-plugin-discord" / "lib" / "link.js")
        with self.assertRaises(RuntimeError):
            self.attest_run()

    # ---- 闸门 ----
    def test_gate_refuses_when_there_is_no_attestation(self):
        with self.assertRaises(RuntimeError) as caught:
            self.gate()
        self.assertIn("run_social_e2e.py", str(caught.exception))

    def test_a_hand_made_result_json_cannot_pass_without_root_having_run_e2e(self):
        """Rin 的情境：dsh 自己写一份看起来全绿的 result.json。没有 root 的证明，闸门不会开。"""
        self.result.write_text(json.dumps(self.good_result))
        with self.assertRaises(RuntimeError):
            self.gate()

    def test_result_json_changed_after_the_attestation_is_refused(self):
        self.attest_run()
        self.result.write_text(json.dumps(dict(self.good_result, modelRequests=28)))
        with self.assertRaises(RuntimeError) as caught:
            self.gate()
        self.assertIn("不一样", str(caught.exception))
        self.result.write_text(json.dumps({"ok": True, "checks": [False] * 13, "modelRequests": 27}))
        with self.assertRaises(RuntimeError):
            self.gate()

    def test_stale_or_future_attestations_are_refused(self):
        self.attest_run()
        self.gate(now=self.now + datetime.timedelta(hours=11))
        for delta in (datetime.timedelta(hours=13), datetime.timedelta(days=3), -datetime.timedelta(hours=2)):
            with self.subTest(delta=str(delta)), self.assertRaises(RuntimeError):
                self.gate(now=self.now + delta)

    def test_e2e_script_replaced_after_the_run_is_refused(self):
        self.attest_run()
        self.script.write_text("// a different e2e script that always passes")
        with self.assertRaises(RuntimeError) as caught:
            self.gate()
        self.assertIn("social-e2e.mjs", str(caught.exception))

    def test_bundle_changed_or_different_or_missing_from_the_attestation_is_refused(self):
        self.attest_run()
        (self.vendor / "social-0.1.50" / "lib" / "index.js").write_text("// changed after e2e")
        with self.assertRaises(RuntimeError) as caught:
            self.gate()
        self.assertIn("重跑 e2e", str(caught.exception))
        (self.vendor / "social-0.1.50" / "lib" / "index.js").write_text("// social-0.1.50")
        self.gate()
        other = self.vendor / "social-0.1.51"
        shutil.copytree(self.vendor / "social-0.1.50", other)
        with self.assertRaises(RuntimeError):
            self.gate(bundles=dict(self.bundles, social=str(other)))            # 另一个版本的目录：证明里的位置不同
        extra = self.vendor / "huanqi-1.1.0"
        extra.mkdir()
        (extra / "package.json").write_text("{}")
        with self.assertRaises(RuntimeError):
            self.gate(bundles=dict(self.bundles, huanqi=str(extra)))            # 要多装一个 e2e 没验过的额外套件

    def test_extra_bundles_not_covered_by_e2e_must_equal_what_production_runs_now(self):
        """--stickers／--memes／--huanqi：e2e 家里没装的额外套件，只允许重装正式服务现在已经在跑的那一份。"""
        extra = self.vendor / "huanqi-1.1.0"
        (extra / "lib").mkdir(parents=True)
        (extra / "package.json").write_text('{"name": "dsh-huanqi", "version": "1.1.0"}')
        (extra / "lib" / "index.js").write_text("// huanqi")
        prod = self.root / "prod" / "node_modules"
        prod.mkdir(parents=True)
        self.attest_run()
        bundles = dict(self.bundles, huanqi=str(extra))
        with self.assertRaises(RuntimeError):                                   # 正式服务那边没有：不能当成「已经在跑」
            self.gate(bundles=bundles, prod_modules=str(prod))
        shutil.copytree(extra, prod / "dsh-huanqi")
        self.gate(bundles=bundles, prod_modules=str(prod))                       # 一模一样：过
        (extra / "lib" / "index.js").write_text("// huanqi, new version")       # 要装的内容变了
        with self.assertRaises(RuntimeError):
            self.gate(bundles=bundles, prod_modules=str(prod))
        # e2e 验过的额外套件（run_social_e2e 带了 --huanqi）：就得跟证明里的指纹一样，不看正式服务
        shutil.copytree(extra, self.modules / "dsh-huanqi")
        self.attest_run(bundles=bundles)
        self.gate(bundles=bundles, prod_modules=str(self.root / "nowhere"))
        (extra / "lib" / "index.js").write_text("// changed again")
        with self.assertRaises(RuntimeError):
            self.gate(bundles=bundles, prod_modules=str(prod))

    def test_attestation_file_itself_must_be_trustworthy(self):
        self.attest_run()
        os.chmod(self.attest, 0o664)
        with self.assertRaises(RuntimeError):
            self.gate()
        os.chmod(self.attest, 0o644)
        self.gate()
        with self.assertRaises(RuntimeError):
            self.gate(trusted_uid=self.uid + 1)                                  # 拥有者不是 root（测试里是别的 uid）
        os.unlink(self.attest)
        os.symlink(self.root / "elsewhere", self.attest)
        with self.assertRaises(RuntimeError):
            self.gate()
        os.unlink(self.attest)
        os.mkfifo(self.attest)
        with self.assertRaises(RuntimeError):
            self.gate()

    def test_forged_attestation_contents_are_refused(self):
        self.attest_run()
        self.rewrite_attest(unit_exit=1)
        with self.assertRaises(RuntimeError):
            self.gate()
        for value in (True, False, "0", None, 0.0):
            self.rewrite_attest(unit_exit=value)
            with self.subTest(unit_exit=value):
                if value == 0 and not isinstance(value, bool) and isinstance(value, int):
                    continue
                with self.assertRaises(RuntimeError):
                    self.gate()
        self.attest_run()
        self.rewrite_attest(finished_at="yesterday")
        with self.assertRaises(RuntimeError):
            self.gate()
        self.attest_run()
        self.rewrite_attest(schema=2)
        with self.assertRaises(RuntimeError):
            self.gate()
        self.attest_run()
        self.rewrite_attest(checks=[False] * 13)
        with self.assertRaises(RuntimeError):
            self.gate()
        for raw in (b"{", b"[]", b'{"unit_exit": NaN}', b""):
            self.attest.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                self.gate()

    # ---- result.json 被换成别的东西：不能卡住 ----
    def test_result_json_replaced_by_fifo_symlink_directory_or_huge_file_is_refused_without_hanging(self):
        original = self.result.read_bytes()

        def clear():
            if self.result.is_dir() and not self.result.is_symlink():
                self.result.rmdir()
            elif os.path.lexists(self.result):
                os.unlink(self.result)                                                 # 档案、符号链接、FIFO 都是 unlink

        for label in ("fifo", "symlink", "directory", "oversized", "device"):
            clear()
            self.result.write_bytes(original)
            self.attest_run()                                                          # 先有一份正常的证明
            clear()
            if label == "fifo":
                os.mkfifo(self.result)
            elif label == "symlink":
                real = self.root / "real-result.json"
                real.write_bytes(original)
                os.symlink(real, self.result)
            elif label == "directory":
                self.result.mkdir()
            elif label == "oversized":
                self.result.write_bytes(original + b" " * ((1 << 20) + 10))
            else:
                os.symlink("/dev/zero", self.result)
            with self.subTest(label):
                with self.assertRaises(RuntimeError) as caught:
                    self.gate()
                self.assertIn("不安全", str(caught.exception), "要是因为 result.json 才被拒绝，不是别的原因")
                with self.assertRaises(RuntimeError):
                    self.attest_run()
        clear()
        self.result.write_bytes(original)
        self.attest_run()
        self.gate()

    def test_result_json_with_a_wrong_owner_is_refused(self):
        self.attest_run()
        with self.assertRaises(RuntimeError):
            self.gate(owners=frozenset({self.uid + 1}))


class RunSocialE2eCliTest(unittest.TestCase):
    def test_arguments_and_refusals(self):
        import run_social_e2e as rse
        with mock.patch("sys.stderr"):
            # 套件目录不存在：拒绝、结束码 1，不会去跑任何单元
            ran = []
            self.assertEqual(rse.main(["--social", "social-0.1.999"], runner=lambda: ran.append(1) or 0), 1)
            self.assertEqual(ran, [])
            self.assertEqual(rse.main(["--social", "evil"], runner=lambda: 0), 1)


@unittest.skipUnless(POSIX, "要符号链接、拥有者与权限，只在 Linux 跑（容器或 VM）")
class VerifyDirTest(unittest.TestCase):
    """Rin 第 3 轮：名字合规、实际是符号链接且指到 vendor 外面的目录，以前会被接受（脚本以 root 执行）。
    Rin 第 4 轮：/opt 是 0777 也会过（只查了 /opt/dsh 与 vendor）；package.json 是 0600、子目录 0700，dsh 读不到也会过。
    测试用临时目录当 /（anchor 参数）：root/opt/dsh/vendor，拥有者就是跑测试的帐号（uid 参数）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.anchor = self.root                                # 测试里的「/」
        self.opt = os.path.join(self.root, "opt")
        self.top = os.path.join(self.opt, "dsh")
        self.vendor = os.path.join(self.top, "vendor")
        os.makedirs(self.vendor)
        for path in (self.root, self.opt, self.top, self.vendor):
            os.chmod(path, 0o755)
        self.uid = os.getuid()
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(self.outside)
        self.good = self.make("social-0.1.50")

    def tearDown(self):
        self.tmp.cleanup()

    # 目录名前缀 → 包名（跟 VM 上现有 9 个目录的 package.json 一致）；版本是目录名去掉前缀。顺序要紧：social-presets- 在 social- 前。
    PACKAGES = (("discord-", "dsh-plugin-discord"), ("social-presets-", "dsh-social-presets"), ("social-", "dsh-social-runtime"),
                ("nekowhale-", "dsh-nekowhale-stickers"), ("dsh-meme-", "dsh-meme"), ("huanqi-", "dsh-huanqi"))

    def make(self, name, with_package=True):
        folder = os.path.join(self.vendor, name)
        os.makedirs(os.path.join(folder, "lib"))
        os.chmod(folder, 0o755)
        os.chmod(os.path.join(folder, "lib"), 0o755)
        if with_package:
            prefix, package = next((p, n) for p, n in self.PACKAGES if name.startswith(p))
            self.write(os.path.join(folder, "package.json"), json.dumps({"name": package, "version": name[len(prefix):]}).encode())
        self.write(os.path.join(folder, "lib", "index.js"))
        return folder

    @staticmethod
    def write(path, content=b"{}"):
        with open(path, "wb") as file:
            file.write(content)
        os.chmod(path, 0o644)

    def check(self, kind="social", value="social-0.1.50"):
        return cs.checked_bundle_dir(kind, value, self.vendor, self.anchor, self.uid)

    def test_a_proper_bundle_passes_and_the_resolved_path_is_returned(self):
        self.assertEqual(self.check(), self.good)
        command = cs.plugin_command(None, "social-0.1.50", None, verify=False, vendor=self.vendor)
        self.assertIn("file:" + self.good, command)

    def test_symlink_to_a_folder_outside_vendor_is_refused(self):
        """Rin 的复现：social-0.1.99 是符号链接，指到 vendor 外面。"""
        self.write(os.path.join(self.outside, "package.json"))
        os.symlink(self.outside, os.path.join(self.vendor, "social-0.1.99"))
        self.assertTrue(cs.bundle_dir("social", "social-0.1.99", self.vendor), "名字合规，写法检查会过")
        with self.assertRaises(cs.BundleError) as caught:
            self.check("social", "social-0.1.99")
        self.assertIn("符号链接", str(caught.exception))

    def test_symlink_to_another_folder_inside_vendor_is_refused_too(self):
        os.symlink(self.good, os.path.join(self.vendor, "social-0.1.98"))
        with self.assertRaises(cs.BundleError):
            self.check("social", "social-0.1.98")

    def test_a_symlink_in_the_middle_of_the_path_is_refused(self):
        moved = os.path.join(self.root, "elsewhere")
        shutil.move(self.vendor, moved)
        os.symlink(moved, self.vendor)                       # vendor 本身变成符号链接
        with self.assertRaises(cs.BundleError):
            self.check()
        os.unlink(self.vendor)
        shutil.move(moved, self.vendor)
        shutil.move(self.top, os.path.join(self.root, "dsh-moved"))
        os.symlink(os.path.join(self.root, "dsh-moved"), self.top)    # /opt/dsh 变成符号链接
        with self.assertRaises(cs.BundleError):
            self.check()

    def test_a_symlink_inside_the_bundle_is_refused(self):
        os.symlink(self.outside, os.path.join(self.good, "lib", "escape"))
        with self.assertRaises(cs.BundleError):
            self.check()
        os.unlink(os.path.join(self.good, "lib", "escape"))
        os.symlink("/etc/hostname", os.path.join(self.good, "link.js"))
        with self.assertRaises(cs.BundleError):
            self.check()

    def test_wrong_owner_is_refused_for_every_level(self):
        for path in (self.good, os.path.join(self.good, "lib"), os.path.join(self.good, "lib", "index.js"),
                     os.path.join(self.good, "package.json")):
            with self.subTest(path=path), self.assertRaises(cs.BundleError) as caught:
                # 「正确的拥有者」换成另一个 uid，等于每一层的拥有者都不对
                cs.checked_bundle_dir("social", "social-0.1.50", self.vendor, self.anchor, self.uid + 1)
            self.assertIn("拥有者", str(caught.exception))
        if os.geteuid() == 0:                                  # root 才能真的建一个别人的档案
            os.chown(os.path.join(self.good, "lib", "index.js"), 65534, 65534)
            with self.assertRaises(cs.BundleError):
                self.check()

    def test_group_or_other_writable_is_refused_at_every_level(self):
        targets = [self.root, self.opt, self.vendor, self.top, self.good, os.path.join(self.good, "lib"),
                   os.path.join(self.good, "lib", "index.js"), os.path.join(self.good, "package.json")]
        for path in targets:
            for mode in (0o020, 0o002):
                before = stat.S_IMODE(os.stat(path).st_mode)
                os.chmod(path, before | mode)
                try:
                    with self.subTest(path=os.path.relpath(path, self.root), mode=oct(mode)), self.assertRaises(cs.BundleError) as caught:
                        self.check()
                    self.assertIn("可写", str(caught.exception))
                finally:
                    os.chmod(path, before)
        self.check()

    def test_missing_folder_and_missing_package_json_are_refused(self):
        with self.assertRaises(cs.BundleError) as caught:
            self.check("social", "social-0.1.77")
        self.assertIn("不存在", str(caught.exception))
        self.make("social-0.1.76", with_package=False)
        with self.assertRaises(cs.BundleError) as caught:
            self.check("social", "social-0.1.76")
        self.assertIn("package.json", str(caught.exception))

    def test_special_files_are_refused(self):
        os.mkfifo(os.path.join(self.good, "lib", "pipe"))
        with self.assertRaises(cs.BundleError):
            self.check()

    def test_extra_dirs_get_the_same_checks(self):
        extra = self.make("nekowhale-1.1.1")
        self.assertEqual(cs.checked_extra_dir("nekowhale-1.1.1", "--stickers", self.vendor, self.anchor, self.uid), extra)
        os.symlink(self.outside, os.path.join(self.vendor, "huanqi-9.9.9"))
        self.write(os.path.join(self.outside, "package.json"))
        with self.assertRaises(cs.BundleError):
            cs.checked_extra_dir("huanqi-9.9.9", "--huanqi", self.vendor, self.anchor, self.uid)

    def test_plugin_command_with_verify_uses_the_resolved_paths_and_refuses_a_bad_extra(self):
        for name in ("discord-0.6.0-social.56", "social-presets-0.53.0"):
            self.make(name)                                   # social-0.1.50 在 setUp 里已经有了
        full = cs.plugin_command(None, None, None, verify=True, vendor=self.vendor, anchor=self.anchor, uid=self.uid)
        self.assertEqual([c for c in full if c.startswith("file:")],
                         ["file:" + os.path.join(self.vendor, cs.DEFAULT_BUNDLE[k]) for k in ("discord", "social", "presets")])
        os.symlink(self.outside, os.path.join(self.vendor, "evil"))
        with self.assertRaises(ValueError):
            cs.plugin_command(None, None, None, stickers="evil", verify=True, vendor=self.vendor, anchor=self.anchor, uid=self.uid)
        os.rename(os.path.join(self.vendor, "social-0.1.50"), os.path.join(self.vendor, "social-0.1.51"))
        with self.assertRaises(cs.BundleError):               # 预设那个目录不见了：拒绝，不会拿别的凑数
            cs.plugin_command(None, None, None, verify=True, vendor=self.vendor, anchor=self.anchor, uid=self.uid)

    # ---- Rin 第 5 轮：package.json 的 name / version 要跟目录名对得上 ----
    def put_package(self, folder, content):
        self.write(os.path.join(folder, "package.json"), content if isinstance(content, bytes) else json.dumps(content).encode())

    def test_a_correct_package_json_passes_for_every_kind(self):
        for kind, name in (("discord", "discord-0.6.0-social.56"), ("social", "social-0.1.50"), ("presets", "social-presets-0.53.0")):
            if not os.path.exists(os.path.join(self.vendor, name)):
                self.make(name)
            with self.subTest(kind=kind):
                self.assertEqual(self.check(kind, name), os.path.join(self.vendor, name))

    def test_wrong_version_in_package_json_is_refused(self):
        """目录叫 social-0.1.50，里面其实是别的版本。以前 --show-bundle 过、然后被装进去。"""
        self.put_package(self.good, {"name": "dsh-social-runtime", "version": "0.1.49"})
        with self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn("0.1.49", str(caught.exception))
        self.assertIn("对不上", str(caught.exception))
        for kind, name, version in (("discord", "discord-0.6.0-social.56", "0.6.0-social.55"), ("presets", "social-presets-0.53.0", "0.52.0")):
            folder = self.make(name)
            self.put_package(folder, {"name": cs.PACKAGE_NAMES[kind], "version": version})
            with self.subTest(kind=kind), self.assertRaises(cs.BundleError):
                self.check(kind, name)

    def test_wrong_package_name_is_refused(self):
        self.put_package(self.good, {"name": "dsh-plugin-discord", "version": "0.1.50"})     # 版本对、包名是别的套件
        with self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn("dsh-plugin-discord", str(caught.exception))
        self.put_package(self.good, {"name": "evil", "version": "0.1.50"})
        with self.assertRaises(cs.BundleError):
            self.check()

    def test_unusable_package_json_is_refused(self):
        bad = {"broken json": b"{not json", "empty": b"", "a list": b"[]", "a string": b'"x"', "null": b"null",
               "no version": b'{"name": "dsh-social-runtime"}', "no name": b'{"version": "0.1.50"}',
               "number version": b'{"name": "dsh-social-runtime", "version": 50}', "null name": b'{"name": null, "version": "0.1.50"}',
               "empty strings": b'{"name": "", "version": ""}', "not utf-8": b'{"name": "\xff", "version": "0.1.50"}',
               "utf-8 bom": b'\xef\xbb\xbf{"name": "dsh-social-runtime", "version": "0.1.50"}'}
        for label, content in bad.items():
            self.put_package(self.good, content)
            with self.subTest(label), self.assertRaises(cs.BundleError):
                self.check()
        self.put_package(self.good, {"name": "dsh-social-runtime", "version": "0.1.50"})
        self.check()

    # ---- Rin 第 6 轮：严格的 JSON ----
    def test_nonstandard_json_constants_are_refused(self):
        """name 与 version 都对，只夹一个 NaN／Infinity：Python 预设的 json.loads 会放行，JavaScript 的 JSON.parse 不会。"""
        for constant in ("NaN", "Infinity", "-Infinity"):
            content = ('{"name": "dsh-social-runtime", "version": "0.1.50", "extra": %s}' % constant).encode()
            self.put_package(self.good, content)
            with self.subTest(constant), self.assertRaises(cs.BundleError) as caught:
                self.check()
            self.assertIn(constant, str(caught.exception))
        self.put_package(self.good, b'{"name": "dsh-social-runtime", "version": "0.1.50", "extra": [1e999]}')   # 读进来会变无限大
        with self.assertRaises(cs.BundleError):
            self.check()

    def test_duplicate_keys_are_refused(self):
        """同一个键两次：各家解析器取的不一样。第一个对、最后一个错（或反过来），两种都要拒绝。"""
        for content in (b'{"name": "dsh-social-runtime", "version": "0.1.49", "version": "0.1.50"}',
                        b'{"name": "dsh-social-runtime", "version": "0.1.50", "version": "0.1.49"}',
                        b'{"name": "evil", "name": "dsh-social-runtime", "version": "0.1.50"}',
                        b'{"name": "dsh-social-runtime", "version": "0.1.50", "scripts": {"a": 1, "a": 2}}'):     # 巢状里的也算
            self.put_package(self.good, content)
            with self.subTest(content=content), self.assertRaises(cs.BundleError) as caught:
                self.check()
            self.assertIn("重复", str(caught.exception))

    def test_deeply_nested_json_is_refused_not_a_traceback(self):
        self.put_package(self.good, b'{"name": "dsh-social-runtime", "version": "0.1.50", "x": ' + b"[" * 20000 + b"]" * 20000 + b"}")
        with self.assertRaises(cs.BundleError):
            self.check()

    def test_oversized_package_json_is_refused(self):
        padding = "x" * (cs.MAX_PACKAGE_BYTES + 10)
        self.put_package(self.good, {"name": "dsh-social-runtime", "version": "0.1.50", "pad": padding})
        with self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn("太大", str(caught.exception))

    def test_extra_dirs_package_json_is_checked_too(self):
        stickers = self.make("nekowhale-1.1.1")
        self.assertEqual(cs.checked_extra_dir("nekowhale-1.1.1", "--stickers", self.vendor, self.anchor, self.uid), stickers)
        self.put_package(stickers, {"name": "dsh-nekowhale-stickers", "version": "1.1.0"})           # 错版
        with self.assertRaises(cs.BundleError):
            cs.checked_extra_dir("nekowhale-1.1.1", "--stickers", self.vendor, self.anchor, self.uid)
        self.put_package(stickers, {"name": "dsh-meme", "version": "1.1.1"})                          # 包名是别的套件
        with self.assertRaises(cs.BundleError):
            cs.checked_extra_dir("nekowhale-1.1.1", "--stickers", self.vendor, self.anchor, self.uid)
        self.put_package(stickers, b"{oops")                                                           # 坏 JSON
        with self.assertRaises(cs.BundleError):
            cs.checked_extra_dir("nekowhale-1.1.1", "--stickers", self.vendor, self.anchor, self.uid)
        for name, label in (("dsh-meme-0.1.64", "--memes"), ("huanqi-1.1.0", "--huanqi")):
            folder = self.make(name)
            with self.subTest(label):
                self.assertEqual(cs.checked_extra_dir(name, label, self.vendor, self.anchor, self.uid), folder)
        # 目录放错旗标：huanqi 的目录给 --stickers 用，包名不对
        with self.assertRaises(cs.BundleError):
            cs.checked_extra_dir("huanqi-1.1.0", "--stickers", self.vendor, self.anchor, self.uid)
        # 目录名看不出版本
        os.makedirs(os.path.join(self.vendor, "nekowhale"))
        with self.assertRaises(cs.BundleError) as caught:
            cs.checked_extra_dir("nekowhale", "--stickers", self.vendor, self.anchor, self.uid)
        self.assertIn("版本", str(caught.exception))

    def test_plugin_command_with_verify_refuses_a_wrong_version_extra(self):
        for name in ("discord-0.6.0-social.56", "social-presets-0.53.0"):
            self.make(name)
        folder = self.make("huanqi-1.1.0")
        self.put_package(folder, {"name": "dsh-huanqi", "version": "9.9.9"})
        with self.assertRaises(cs.BundleError):
            cs.plugin_command(None, None, None, huanqi="huanqi-1.1.0", verify=True, vendor=self.vendor, anchor=self.anchor, uid=self.uid)

    def test_missing_ancestor_or_anchor_outside_is_a_refusal_not_a_traceback(self):
        with self.assertRaises(cs.BundleError):               # 起点根本不在路径上
            cs.checked_bundle_dir("social", "social-0.1.50", self.vendor, os.path.join(self.root, "nope"), self.uid)
        with self.assertRaises(cs.BundleError) as caught:     # 路径中间的目录不见了
            cs.checked_bundle_dir("social", "social-0.1.50", os.path.join(self.root, "gone", "vendor"), self.anchor, self.uid)
        self.assertNotIn("Traceback", str(caught.exception))

    # ---- Rin 第 4 轮 ①：从 / 到套件目录每一层 ----
    def test_ancestor_opt_0777_is_refused(self):
        """Rin 的复现：/opt 是 0777，以前过了。这里 opt 就是测试里的 /opt。"""
        os.chmod(self.opt, 0o777)
        with self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn(self.opt, str(caught.exception))
        self.assertIn("可写", str(caught.exception))

    def test_ancestor_not_owned_by_root_is_refused(self):
        """/opt 的拥有者不是 root：用替身的 lstat，让 opt 看起来是别人的（不需要真的 chown，root 与 dsh 两种身分都能跑）。"""
        real = os.lstat

        class Foreign:
            def __init__(self, info):
                self.st_mode, self.st_uid = info.st_mode, self.uid_other
            uid_other = os.getuid() + 1000

        def fake(path):
            info = real(path)
            return Foreign(info) if path == self.opt else info
        with mock.patch.object(cs, "_lstat", side_effect=fake), self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn(self.opt, str(caught.exception))
        self.assertIn("拥有者", str(caught.exception))

    def test_every_ancestor_level_is_checked_for_mode_and_symlink(self):
        for level in (self.root, self.opt, self.top, self.vendor):
            before = stat.S_IMODE(os.stat(level).st_mode)
            os.chmod(level, before | 0o020)
            try:
                with self.subTest(level=os.path.relpath(level, self.root)), self.assertRaises(cs.BundleError):
                    self.check()
            finally:
                os.chmod(level, before)

    def test_the_filesystem_root_is_checked_too(self):
        """默认起点就是「/」，不是 /opt/dsh：连根目录一起检查。理由：根目录若被别人写得进去，就能把 /opt 换掉。
        真的「/」在测试环境里没办法改权限，用替身 lstat：「/」与它到测试目录之间的各层都看起来是 root 的 0755，只改「/」。"""
        self.assertEqual(cs.ANCHOR, "/")
        self.assertEqual(cs.chain("/opt/dsh/vendor/x"), ["/", "/opt", "/opt/dsh", "/opt/dsh/vendor", "/opt/dsh/vendor/x"])
        self.assertEqual(cs.chain("/opt", "/opt"), ["/opt"])
        real = os.lstat
        mode = {"value": stat.S_IFDIR | 0o777}

        class Fake:
            def __init__(self, mode_value, uid):
                self.st_mode, self.st_uid = mode_value, uid

        def fake(path):
            if path == "/":
                return Fake(mode["value"], self.uid)
            if (self.root + "/").startswith(path.rstrip("/") + "/"):
                return Fake(stat.S_IFDIR | 0o755, self.uid)
            return real(path)
        with mock.patch.object(cs, "_lstat", side_effect=fake), mock.patch.object(cs.os.path, "realpath", side_effect=lambda x: x):
            with self.assertRaises(cs.BundleError) as caught:
                cs.checked_bundle_dir("social", "social-0.1.50", self.vendor, "/", self.uid)
            self.assertIn("/ 的群组或其他人可写", str(caught.exception))
            mode["value"] = stat.S_IFDIR | 0o755
            self.assertEqual(cs.checked_bundle_dir("social", "social-0.1.50", self.vendor, "/", self.uid), self.good)

    def test_a_sticky_world_writable_ancestor_is_still_refused(self):
        """/tmp 这种 1777 的目录也不行：别人能在里面改名、删除，不是可信的路径。"""
        os.chmod(self.opt, 0o1777)
        with self.assertRaises(cs.BundleError):
            self.check()

    # ---- Rin 第 4 轮 ②：dsh 读得到、走得进去 ----
    def test_unreadable_file_for_dsh_is_refused(self):
        """Rin 的复现：package.json 是 0600（root 自己读得了，dsh 读不了）。以前过了，要等停掉 dsh 之后 plugin add 才失败。"""
        os.chmod(os.path.join(self.good, "package.json"), 0o600)
        with self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn("package.json", str(caught.exception))
        self.assertIn("dsh", str(caught.exception))

    def test_group_only_readable_file_is_refused(self):
        """只给群组读（0640）也不行：档案的群组是 root，dsh 不在里面，决定权在 other 位。"""
        os.chmod(os.path.join(self.good, "lib", "index.js"), 0o640)
        with self.assertRaises(cs.BundleError):
            self.check()

    def test_unsearchable_directory_for_dsh_is_refused(self):
        """子目录 0700：dsh 进不去。"""
        os.chmod(os.path.join(self.good, "lib"), 0o700)
        with self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn("lib", str(caught.exception))
        self.assertIn("dsh", str(caught.exception))

    def test_directory_without_read_or_search_bits_is_refused(self):
        for mode in (0o711, 0o744, 0o750):                    # 没有 o+r、没有 o+x、或群组才有
            os.chmod(os.path.join(self.good, "lib"), mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(cs.BundleError):
                self.check()
        os.chmod(os.path.join(self.good, "lib"), 0o755)
        self.check()

    def test_the_bundle_folder_itself_and_ancestors_must_be_searchable_for_dsh(self):
        os.chmod(self.good, 0o750)
        with self.assertRaises(cs.BundleError):
            self.check()
        os.chmod(self.good, 0o755)
        os.chmod(self.opt, 0o750)                             # 上层目录 dsh 走不进去
        with self.assertRaises(cs.BundleError) as caught:
            self.check()
        self.assertIn(self.opt, str(caught.exception))
        self.assertIn("走不进去", str(caught.exception))

    def test_read_only_for_everyone_is_fine(self):
        """正常的 root 套件：档案 0444、目录 0555 也应该过（有读、有搜寻，没人可写）。"""
        os.chmod(os.path.join(self.good, "package.json"), 0o444)
        os.chmod(os.path.join(self.good, "lib", "index.js"), 0o444)
        os.chmod(os.path.join(self.good, "lib"), 0o555)
        try:
            self.check()
        finally:
            os.chmod(os.path.join(self.good, "lib"), 0o755)

    def test_show_bundle_refuses_the_symlink_case_and_prints_the_reason(self):
        """走真正的命令行入口：--show-bundle 做跟升级同一组检查。这里用实际的 /opt/dsh/vendor（测试环境里通常不存在）
        只确认：目录不存在就拒绝、结束码不是 0、不会丢一大串 traceback。"""
        result = subprocess.run([sys.executable, str(HERE / "configure_social.py"), "--show-bundle", "--social", "social-0.1.99"],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("拒绝", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
