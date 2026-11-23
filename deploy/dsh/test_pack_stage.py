import contextlib
import hashlib
import io
import os
import pathlib
import tarfile
import tempfile
import unittest

import pack_stage as ps


class PackStageTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))      # pack_stage 会印出指纹，测试里不需要
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        src = self.root / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("alpha", encoding="utf-8")
        (src / "sub" / "b.txt").write_text("beta", encoding="utf-8")
        (src / "sub" / "x.test.mjs").write_text("skip me", encoding="utf-8")
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "c.pyc").write_bytes(b"\x00")
        (src / "node_modules").mkdir()
        (src / "node_modules" / "d.js").write_text("dep", encoding="utf-8")
        (self.root / "single.py").write_text("print(1)", encoding="utf-8")
        self.src = src

    def tearDown(self):
        self.tmp.cleanup()

    def pack(self, out="out.tar", *extra):
        target = self.root / out
        code = ps.main([str(target), "--add", f"deploy/x={self.src}", "--add", f"tool/single.py={self.root / 'single.py'}", *extra])
        return code, target

    def test_contents_manifest_and_default_excludes(self):
        code, target = self.pack("out.tar", "--exclude", "*.test.mjs")
        self.assertEqual(code, 0)
        with tarfile.open(target) as archive:
            names = archive.getnames()
            manifest = archive.extractfile(ps.MANIFEST).read().decode()
        self.assertEqual(names, sorted(names))
        self.assertEqual(set(names), {"deploy/x/a.txt", "deploy/x/sub/b.txt", "tool/single.py", ps.MANIFEST})
        expected = {"deploy/x/a.txt": b"alpha", "deploy/x/sub/b.txt": b"beta", "tool/single.py": b"print(1)"}
        self.assertEqual(manifest, "".join(f"{hashlib.sha256(expected[p]).hexdigest()}  {p}\n" for p in sorted(expected)))

    def test_tar_is_deterministic_and_normalised(self):
        _, first = self.pack("one.tar")
        os.utime(self.src / "a.txt", (12345, 12345))           # 改时间戳，内容不变：结果要一样
        _, second = self.pack("two.tar")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            for info in archive.getmembers():
                self.assertEqual((info.mtime, info.uid, info.gid, info.mode), (0, 0, 0, 0o644))

    def test_a_changed_byte_changes_the_manifest(self):
        _, first = self.pack("one.tar")
        (self.src / "a.txt").write_text("alphA", encoding="utf-8")
        _, second = self.pack("two.tar")
        self.assertNotEqual(first.read_bytes(), second.read_bytes())

    def test_dot_destination_puts_directory_contents_at_the_root(self):
        target = self.root / "root.tar"
        self.assertEqual(ps.main([str(target), "--add", f".={self.src}"]), 0)
        with tarfile.open(target) as archive:
            self.assertIn("a.txt", archive.getnames())
            self.assertIn("sub/x.test.mjs", archive.getnames())

    def test_bad_requests_are_refused(self):
        target = str(self.root / "bad.tar")
        for argv in (["--add", f"../escape={self.src}"], ["--add", f"/abs={self.src}"], ["--add", f"x={self.root / 'missing'}"],
                     ["--add", "no-equals"], ["--add", f".={self.root / 'single.py'}"], ["--add", f"{ps.MANIFEST}={self.root / 'single.py'}"],
                     []):
            with self.subTest(argv=argv):
                self.assertEqual(ps.main([target] + argv), 1)

    # ---- Rin 第 6 轮：重复路径不能静默只留最后一项 ----
    def test_two_entries_with_the_same_destination_are_refused(self):
        other = self.root / "other.txt"
        other.write_text("different", encoding="utf-8")
        target = str(self.root / "dup.tar")
        self.assertEqual(ps.main([target, "--add", f"tool/x.py={self.root / 'single.py'}", "--add", f"tool/x.py={other}"]), 1)
        self.assertFalse((self.root / "dup.tar").exists(), "拒绝时不该留下半成品")

    def test_directory_entries_that_land_on_the_same_path_are_refused(self):
        """两个目录展开后有同一个路径（a.txt）；以及目录里的档案跟另一个 --add 的单一档案撞名。"""
        second = self.root / "src2"
        second.mkdir()
        (second / "a.txt").write_text("other alpha", encoding="utf-8")
        target = str(self.root / "dup2.tar")
        self.assertEqual(ps.main([target, "--add", f"d={self.src}", "--add", f"d={second}"]), 1)
        self.assertEqual(ps.main([target, "--add", f"d={self.src}", "--add", f"d/a.txt={self.root / 'single.py'}"]), 1)
        self.assertEqual(ps.main([target, "--add", f".={self.src}", "--add", f"a.txt={self.root / 'single.py'}"]), 1)

    def test_same_source_added_twice_is_refused_too(self):
        target = str(self.root / "dup3.tar")
        self.assertEqual(ps.main([target, "--add", f"d={self.src}", "--add", f"d={self.src}"]), 1)

    def test_a_path_cannot_be_both_a_file_and_a_directory(self):
        target = str(self.root / "clash.tar")
        self.assertEqual(ps.main([target, "--add", f"d/a.txt={self.root / 'single.py'}", "--add", f"d/a.txt/b={self.root / 'single.py'}"]), 1)
        self.assertEqual(ps.main([target, "--add", f"d/a.txt/b={self.root / 'single.py'}", "--add", f"d/a.txt={self.root / 'single.py'}"]), 1)

    def test_unsafe_path_characters_are_refused(self):
        """空白、换行、反斜线会让「sha256  路径」的清单对不上，甚至被塞进假的一行。"""
        target = str(self.root / "bad-names.tar")
        for dest in ("has space/x", "line\nbreak/x", "back\\slash/x", "semi;colon/x", "a/./b", "a//b", "tab\tx"):
            with self.subTest(dest=dest):
                self.assertEqual(ps.main([target, "--add", f"{dest}={self.root / 'single.py'}"]), 1)
        odd = self.root / "src3"
        odd.mkdir()
        (odd / "with space.txt").write_text("x", encoding="utf-8")
        self.assertEqual(ps.main([target, "--add", f"d={odd}"]), 1)

    def test_non_ascii_letters_in_file_names_are_accepted_and_listed(self):
        """真实的鱼记忆套件里有「生成档案.js」：中文档名是正常的，只拒绝空白、控制字元、反斜线这些会弄乱清单的字元。"""
        (self.src / "生成档案.js").write_text("x", encoding="utf-8")
        code, target = self.pack("cjk.tar")
        self.assertEqual(code, 0)
        with tarfile.open(target) as archive:
            self.assertIn("deploy/x/生成档案.js", archive.getnames())
            self.assertIn("  deploy/x/生成档案.js\n", archive.extractfile(ps.MANIFEST).read().decode("utf-8"))
    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "符号链接测试只在 POSIX 跑")
    def test_output_that_is_a_symlink_is_refused_and_the_target_untouched(self):
        victim = self.root / "victim.txt"
        victim.write_text("precious")
        link = self.root / "out-link.tar"
        os.symlink(victim, link)
        self.assertEqual(ps.main([str(link), "--add", f"d={self.src}"]), 1)
        self.assertEqual(victim.read_text(), "precious")
        self.assertTrue(link.is_symlink())
        os.unlink(link)
        os.symlink(self.root / "not-there.tar", link)                      # 悬空的符号链接也不行（不然会在别处凭空建一个档案）
        self.assertEqual(ps.main([str(link), "--add", f"d={self.src}"]), 1)
        self.assertFalse((self.root / "not-there.tar").exists())

    def test_output_that_is_a_directory_is_refused(self):
        self.assertEqual(ps.main([str(self.root), "--add", f"d={self.src}"]), 1)
    @unittest.skipUnless(hasattr(os, "mkfifo"), "管线测试只在 POSIX 跑")
    def test_special_files_are_refused_instead_of_hanging(self):
        os.mkfifo(self.src / "pipe")
        self.assertEqual(ps.main([str(self.root / "fifo.tar"), "--add", f"d={self.src}"]), 1)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "符号链接测试只在 POSIX 跑")
    def test_symlinks_are_refused(self):
        os.symlink(self.root / "single.py", self.src / "link.py")
        self.assertEqual(ps.main([str(self.root / "s.tar"), "--add", f"d={self.src}"]), 1)


if __name__ == "__main__":
    unittest.main()
