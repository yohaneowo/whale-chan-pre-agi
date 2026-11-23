"""DEPLOY.md 里会真的拿去执行的 shell 片段：把原文从文件里抽出来跑（只把 /opt/dsh 换成临时目录、sudo 换成直接执行的替身），
确认「失败就整段非 0、不往下做」。Rin 第 6 轮：失败码被外面的 echo 吞掉、核对步骤失败了后面还继续。
需要 root（chown）与 bash，只在 Linux 容器里跑；其他环境跳过。"""

import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest

import pack_stage

HERE = pathlib.Path(__file__).parent
DEPLOY = HERE.parent / "draw" / "DEPLOY.md"
ENABLED = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("bash")


def blocks():
    """DEPLOY.md 里所有 ```sh 区块（去掉缩排），回传文字清单。"""
    found, current = [], None
    for line in DEPLOY.read_text(encoding="utf-8").splitlines():
        if current is None:
            if re.match(r"^\s*```sh\s*$", line):
                current = []
        elif re.match(r"^\s*```\s*$", line):
            found.append(textwrap.dedent("\n".join(current)) + "\n")
            current = None
        else:
            current.append(line)
    return found


def find_block(*needles):
    matches = [b for b in blocks() if all(n in b for n in needles)]
    assert len(matches) == 1, (needles, len(matches))
    return matches[0]


@unittest.skipUnless(ENABLED, "要 root 与 bash（Linux 容器）")
class DeploySnippetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "opt" / "dsh" / "vendor").mkdir(parents=True)
        self.shim = self.root / "shim"
        self.shim.mkdir()
        sudo = self.shim / "sudo"
        sudo.write_text('#!/bin/sh\nif [ "$1" = "-n" ]; then shift; fi\nexec "$@"\n')
        sudo.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def run_block(self, text, extra_path=None):
        text = text.replace("/opt/dsh/", f"{self.root}/opt/dsh/")
        path = os.pathsep.join(filter(None, [extra_path, str(self.shim), os.environ["PATH"]]))
        return subprocess.run(["bash", "-c", text], capture_output=True, text=True, timeout=120,
                              env={"PATH": path, "HOME": str(self.home), "LANG": "C.UTF-8"})

    # ---------- vendor 复制 ----------
    def vendor_block(self):
        return find_block("NEW=", "trap finish EXIT")

    def stage_built(self, names=("discord-0.6.0-social.56", "social-0.1.50", "social-presets-0.53.0")):
        for name in names:
            folder = self.home / "stage-social56" / "built" / name
            (folder / "lib").mkdir(parents=True)
            (folder / "package.json").write_text("{}")
            (folder / "lib" / "index.js").write_text("1")
            folder.chmod(0o700)

    def test_vendor_block_has_nothing_after_the_inner_shell(self):
        """外层不能再接 echo "$?" 之类的指令：那会把里面的失败码换成 0。最后一条非空行必须是 heredoc 的结束记号。"""
        lines = [line for line in self.vendor_block().splitlines() if line.strip()]
        self.assertEqual(lines[-1], "EOF")
        self.assertNotRegex(self.vendor_block(), r"(?m)^EOF\n.*\S")

    def test_vendor_block_success(self):
        self.stage_built()
        result = self.run_block(self.vendor_block())
        self.assertEqual(result.returncode, 0, result.stderr)
        vendor = self.root / "opt" / "dsh" / "vendor"
        self.assertEqual(sorted(p.name for p in vendor.iterdir()), ["discord-0.6.0-social.56", "social-0.1.50", "social-presets-0.53.0"])
        self.assertEqual(oct((vendor / "social-0.1.50").stat().st_mode & 0o777), "0o755")
        self.assertEqual(oct((vendor / "social-0.1.50" / "package.json").stat().st_mode & 0o777), "0o644")

    def test_vendor_block_returns_nonzero_when_a_target_already_exists_and_copies_nothing(self):
        """Rin 的复现：里面那层 shell 失败了，外层以前仍回 0。"""
        self.stage_built()
        (self.root / "opt" / "dsh" / "vendor" / "social-0.1.50").mkdir()
        result = self.run_block(self.vendor_block())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.returncode, 1)
        self.assertIn("vendor 里已经有 social-0.1.50", result.stderr)
        self.assertEqual([p.name for p in (self.root / "opt" / "dsh" / "vendor").iterdir()], ["social-0.1.50"])

    def test_vendor_block_returns_nonzero_when_a_stage_directory_is_missing(self):
        self.stage_built(("discord-0.6.0-social.56", "social-0.1.50"))
        result = self.run_block(self.vendor_block())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list((self.root / "opt" / "dsh" / "vendor").iterdir()), [])

    def test_vendor_block_returns_nonzero_when_a_copy_fails_halfway_and_names_the_started_directories(self):
        self.stage_built()
        fail = self.root / "failcp"
        fail.mkdir()
        cp = fail / "cp"
        cp.write_text('#!/bin/sh\ncase "$*" in *social-0.1.50*) echo "cp: simulated failure" >&2; exit 1;; esac\nexec /bin/cp "$@"\n')
        cp.chmod(0o755)
        result = self.run_block(self.vendor_block(), extra_path=str(fail))
        self.assertEqual(result.returncode, 1)
        self.assertIn("discord-0.6.0-social.56 social-0.1.50", result.stderr)

    def test_vendor_block_does_not_trust_a_failed_find_to_mean_no_problems(self):
        """复制后核对权限的那条 find 失败时，以前 `| wc -l` 得到 0，被当成「没有问题」。"""
        self.stage_built()
        fake = self.fail_tool("find", 'case "$*" in *-perm*) exit 1;; esac\nexec /usr/bin/find "$@"')
        result = self.run_block(self.vendor_block(), extra_path=fake)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("都已复制并改好", result.stdout)

    # ---------- stage 完整性核对 ----------
    def make_stage(self, name):
        """用 pack_stage 打一个小 stage，解到 home/<name>，回传 MANIFEST 的 sha256。"""
        source = self.root / "stage-src"
        (source / "deploy" / "dsh").mkdir(parents=True, exist_ok=True)
        (source / "deploy" / "dsh" / "configure_social.py").write_text("print('hi')")
        (source / "built" / "discord-0.6.0-social.56").mkdir(parents=True, exist_ok=True)
        (source / "built" / "discord-0.6.0-social.56" / "package.json").write_text('{"version": "0.6.0-social.56"}')
        (source / "api.md").write_text("doc")
        (source / "lib").mkdir(exist_ok=True)
        (source / "lib" / "生成档案.js").write_text("中文档名也要能核对", encoding="utf-8")
        tar = self.root / "stage.tar"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pack_stage.main([str(tar), "--add", f".={source}"]), 0)
        target = self.home / name
        target.mkdir()
        with tarfile.open(tar) as archive:
            archive.extractall(target)
        return hashlib.sha256((target / "MANIFEST.sha256").read_bytes()).hexdigest()

    def stage_block(self, stage, fingerprint):
        block = find_block("bash -euo pipefail -s --", f"cd ~/{stage}")
        return block.replace("<MANIFEST_SHA256>", fingerprint)

    def test_stage_check_passes_for_an_intact_stage(self):
        for stage in ("stage-whaledraw-022", "stage-social56"):
            fingerprint = self.make_stage(stage)
            result = self.run_block(self.stage_block(stage, fingerprint))
            with self.subTest(stage):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("全部通过", result.stdout)

    def test_stage_check_fails_fast_on_every_kind_of_tampering(self):
        for stage in ("stage-whaledraw-022", "stage-social56"):
            fingerprint = self.make_stage(stage)
            folder = self.home / stage
            block = self.stage_block(stage, fingerprint)
            tamper = {
                "changed file": lambda: (folder / "api.md").write_text("changed"),
                "deleted file": lambda: (folder / "api.md").unlink(),
                "extra file": lambda: (folder / "extra.txt").write_text("x"),
                "extra symlink": lambda: os.symlink("/etc/hostname", folder / "link"),
                "manifest replaced": lambda: (folder / "MANIFEST.sha256").write_text(
                    (folder / "MANIFEST.sha256").read_text().replace("api.md", "api.md")
                    + hashlib.sha256(b"x").hexdigest() + "  x\n"),
            }
            for label, action in tamper.items():
                action()
                result = self.run_block(block)
                with self.subTest(stage=stage, tamper=label):
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn("全部通过", result.stdout, "失败后不该继续往下印成功")
                shutil.rmtree(folder)
                self.make_stage(stage)

    def fail_tool(self, name, body):
        folder = self.root / f"fail-{name}"
        folder.mkdir(exist_ok=True)
        tool = folder / name
        tool.write_text("#!/bin/sh\n" + body + "\n")
        tool.chmod(0o755)
        return str(folder)

    def test_stage_check_stops_when_any_helper_command_fails_even_after_printing_output(self):
        """Rin 的复现：产生清单的指令先印出东西、再 exit 1。以前用 <(…) 进程替换，失败被吞掉，外层的 diff 还是回 0。"""
        for stage in ("stage-whaledraw-022", "stage-social56"):
            fingerprint = self.make_stage(stage)
            block = self.stage_block(stage, fingerprint)
            real = {name: shutil.which(name) for name in ("find", "sort", "sed", "awk", "sha256sum", "mktemp", "stat", "id", "diff", "grep")}
            good_find = self.run_block(block)
            self.assertEqual(good_find.returncode, 0, good_find.stderr)
            fixtures = {
                # 先把真正的输出印出来，再失败
                "find": 'printf "./api.md\\n./lib/x\\n"; exit 1',
                "sort": f'{real["sort"]} "$@"; exit 1',
                "sed": f'{real["sed"]} "$@"; exit 1',
                "awk": f'{real["awk"]} "$@"; exit 1',
                "diff": f'{real["diff"]} "$@"; exit 1',
                "mktemp": "exit 1",
                "id": "exit 1",
                "stat": "exit 1",
                "sha256sum": f'{real["sha256sum"]} "$@"; exit 1',
            }
            for tool, body in fixtures.items():
                result = self.run_block(block, extra_path=self.fail_tool(tool, body))
                with self.subTest(stage=stage, tool=tool):
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("全部通过", result.stdout)

    def test_stage_check_does_not_use_process_substitution(self):
        self.assertNotIn("<(", self.stage_block("stage-social56", "0" * 64))

    def test_stage_check_refuses_a_missing_or_malformed_fingerprint(self):
        self.make_stage("stage-social56")
        for bad in ("", "abc", "G" * 64, "A" * 64, "0" * 63, "0" * 65):
            result = self.run_block(self.stage_block("stage-social56", bad))
            with self.subTest(bad=bad):
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("全部通过", result.stdout)
        wrong = self.run_block(self.stage_block("stage-social56", "0" * 64))
        self.assertEqual(wrong.returncode, 1)

    # ---------- systemd-run --wait 探针 ----------
    def probe_block(self):
        return find_block("exit 7", "systemd-run")

    def fake_systemd_run(self, code):
        folder = self.root / f"systemd-run-{code}"
        folder.mkdir(exist_ok=True)
        tool = folder / "systemd-run"
        tool.write_text(f"#!/bin/sh\nexit {code}\n")
        tool.chmod(0o755)
        return str(folder)

    def test_systemd_run_probe_only_passes_when_the_unit_exit_code_7_comes_through(self):
        """Rin 第 8 轮：回 1 分不出是单元真的跑了，还是 systemd-run 自己出错。必须得到特定的 7 才算数。"""
        passed = self.run_block(self.probe_block(), extra_path=self.fake_systemd_run(7))
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("结束码 7", passed.stdout)
        for code in (0, 1, 2, 126, 127, 255):                       # 包括 systemd-run 自己出错常见的 1、126、127
            result = self.run_block(self.probe_block(), extra_path=self.fake_systemd_run(code))
            with self.subTest(code=code):
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"得到 {code}", result.stderr)

    def test_systemd_run_probe_does_not_use_bin_false(self):
        self.assertNotIn("/bin/false", self.probe_block())

    # ---------- 安装 / 还原 configure_social.py ----------
    def configure_files(self, current="old social.55 script: discord-0.6.0-social.55\n"):
        (self.root / "opt" / "dsh" / "configure_social.py").write_text(current)
        (self.root / "opt" / "dsh" / "configure_social.py").chmod(0o644)
        new = self.home / "stage-social56" / "deploy" / "dsh"
        new.mkdir(parents=True, exist_ok=True)
        (new / "configure_social.py").write_text("new script with --show-bundle\n")
        (new / "run_social_e2e.py").write_text("# root runs e2e and writes the attestation\n")
        (new / "social-e2e.mjs").write_text("// the e2e script\n")

    def install_block(self):
        return find_block("BAK=$CUR.social55", "install -o root -g root -m 644")

    def restore_block(self):
        return find_block("BAK=$CUR.social55", "找不到备份")

    def test_install_then_restore_round_trip(self):
        self.configure_files()
        installed = self.run_block(self.install_block())
        self.assertEqual(installed.returncode, 0, installed.stderr)
        current = self.root / "opt" / "dsh" / "configure_social.py"
        self.assertIn("--show-bundle", current.read_text())
        restored = self.run_block(self.restore_block())
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(current.read_text(), "old social.55 script: discord-0.6.0-social.55\n")

    def test_install_refuses_to_overwrite_an_existing_backup_and_a_non_social55_script(self):
        self.configure_files()
        backup = self.root / "opt" / "dsh" / "configure_social.py.social55"
        backup.write_text("precious\n")
        result = self.run_block(self.install_block())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(backup.read_text(), "precious\n", "备份被覆盖了")
        self.assertIn("old social.55", (self.root / "opt" / "dsh" / "configure_social.py").read_text())
        backup.unlink()
        self.configure_files("something else entirely\n")
        self.assertEqual(self.run_block(self.install_block()).returncode, 1)
        self.configure_files("already new --show-bundle discord-0.6.0-social.55\n")
        self.assertEqual(self.run_block(self.install_block()).returncode, 1)

    def test_restore_fails_when_the_fingerprint_record_is_missing(self):
        """记录读不到时，以前会拿两个空字串去比，比出「一致」。"""
        self.configure_files()
        self.assertEqual(self.run_block(self.install_block()).returncode, 0)
        (self.home / "configure_social.social55.sha256").unlink()
        result = self.run_block(self.restore_block())
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("已还原", result.stdout)
        (self.home / "configure_social.social55.sha256").write_text("")
        result = self.run_block(self.restore_block())
        self.assertEqual(result.returncode, 1)

    def test_install_installs_the_e2e_files_and_stops_when_one_is_missing(self):
        self.configure_files()
        result = self.run_block(self.install_block())
        self.assertEqual(result.returncode, 0, result.stderr)
        opt = self.root / "opt" / "dsh"
        self.assertEqual((opt / "run_social_e2e.py").read_text(), "# root runs e2e and writes the attestation\n")
        self.assertEqual(oct((opt / "social-e2e.mjs").stat().st_mode & 0o777), "0o644")
        (opt / "configure_social.py.social55").unlink()
        self.configure_files()
        (self.home / "stage-social56" / "deploy" / "dsh" / "social-e2e.mjs").unlink()
        self.assertNotEqual(self.run_block(self.install_block()).returncode, 0)

    def test_restore_fails_when_the_backup_is_missing_or_does_not_match_the_recorded_fingerprint(self):
        self.configure_files()
        self.assertEqual(self.run_block(self.restore_block()).returncode, 1)               # 没有备份
        self.assertEqual(self.run_block(self.install_block()).returncode, 0)
        (self.root / "opt" / "dsh" / "configure_social.py.social55").write_text("old script discord-0.6.0-social.55 tampered\n")
        result = self.run_block(self.restore_block())
        self.assertEqual(result.returncode, 1)
        self.assertIn("指纹", result.stderr)


class DocRulesTest(unittest.TestCase):
    """不需要 root：全篇的规矩。"""

    def test_every_vm_python_run_in_the_doc_uses_dash_B(self):
        """VM 上用 Python 跑的检查一律加 -B，免得在 root 拥有的目录里写出 __pycache__。"""
        text = DEPLOY.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            for match in re.finditer(r"python3 (?=[-A-Za-z$~/.<])", line):
                self.assertRegex(line[match.start():], r"^python3 (-I )?-B ", f"DEPLOY.md 第 {number} 行的 python3 没有 -B：{line.strip()[:80]}")

    def test_no_process_substitution_and_no_status_hiding_pipelines_in_snippets(self):
        """<(…) 里的失败 pipefail 管不到；$(… | wc -l) 在 find 失败时得到 0；[ "$(a)" = "$(b)" ] 两边都失败时两个空字串相等。"""
        for number, block in enumerate(blocks(), 1):
            self.assertNotIn("<(", block, f"第 {number} 个片段用了进程替换")
            self.assertNotRegex(block, r"\|\s*wc -l\)", f"第 {number} 个片段在 $(…) 里用 | wc -l")
            self.assertNotRegex(block, r'\[ "\$\([^"]*\)" = "\$\(', f"第 {number} 个片段拿两个 $(…) 相比")
            self.assertNotRegex(block, r'(?m)^\s*[^#\n]*\bsha256sum [^\n]*\|\s*cut\b[^\n]*\)\s*=\s*"\$\(', f"第 {number} 个片段拿两个指纹相比")

    def test_the_doc_does_not_describe_the_retired_gate_rules(self):
        """Rin 第 8 轮：DEPLOY.md 里不能还有「checks 至少 13 项」这种已经废除的旧规则（要写也得标明是旧的）。"""
        text = DEPLOY.read_text(encoding="utf-8")
        self.assertNotIn("至少 13 项", text)
        self.assertNotIn("/bin/false", text)
        for number, line in enumerate(text.splitlines(), 1):
            if "13" in line and "checks" in line:
                self.assertIn("旧", line, f"DEPLOY.md 第 {number} 行提到 13 项 checks 却没有标明是旧规则：{line.strip()[:80]}")

    def test_no_snippet_swallows_a_failure_with_a_trailing_echo_of_the_status(self):
        for block in blocks():
            self.assertNotRegex(block, r'(?m)^\s*echo "[^"]*\$\?', "片段里不要用 echo 印 $?（会把失败码换成 0）")

if __name__ == "__main__":
    unittest.main()
