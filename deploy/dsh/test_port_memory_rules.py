"""移植脚本（翻旧事的规则）：只换本次需要的接点，对不上就整个停下、不写任何档案；同一份副本不能移植两次。

公开仓库不收录现行 release，这里用移植脚本自己的接点拼出一份假的 release；
设了 DSH_RELEASE_COPY（release 副本根目录）时，另外对真的副本跑一次（只在暂存复本上写）。
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import port_memory_rules as port

JOIN = "\n/*--*/\n"


def fake_runtime():
    consolidate = port.OLD_CONSOLIDATE_START + "\n".join(port.OLD_CONSOLIDATE_MARKS) + "\n"
    anchors = [port.CRYPTO[0], port.IMPORTS[0], port.CTOR[0], port.ADMIT_WARM[0], port.OLD_RELATED, port.OLD_PLACE, port.OLD_CONTEXT,
               port.OLD_DM_MEMORIES, port.OLD_DM_LINE, port.OLD_ROLE_DM, port.MANNERS[0], port.OLD_GROUP_SIGNATURE,
               port.OLD_GROUP_MEMORIES, port.OLD_PREPARE[0], port.OLD_INBOX[0], port.OLD_SPEAKERS[0], port.OLD_CONTROL_PERSON,
               port.OLD_CONTROL_FACTS, port.OLD_CONTROL_LIST, consolidate + port.OLD_CONSOLIDATE_END, "drawRequestTool",
               "this.studios.has(agent.id)"]
    return JOIN.join(anchors) + "\n"


def fake_release(root):
    contents = {
        "dsh-social-runtime/index.mjs": fake_runtime(),
        "dsh-social-runtime/memory.mjs": JOIN.join([port.MEMORY_SIGNATURE[0], port.MEMORY_CATEGORY[0]]),
        "dsh-social-runtime/notes.mjs": JOIN.join([port.OLD_NOTES_CONTEXT, port.OLD_NOTES_CARDS, port.OLD_NOTES_UPDATE,
                                                   port.OLD_NOTES_HEADER]),
        "dsh-social-runtime/archive.mjs": JOIN.join([port.ARCHIVE_RELATED_OPT[0], port.ARCHIVE_RELATED_ROWS[0],
                                                     port.ARCHIVE_SEARCH_OPT[0], port.ARCHIVE_SEARCH_ROWS[0],
                                                     port.ARCHIVE_AROUND[0]]),
        "dsh-social-runtime/chat-tools.mjs": JOIN.join([port.TOOLS_IMPORT[0], port.OLD_TOOLS_EXECUTE, port.OLD_TOOLS_SEARCH,
                                                        port.OLD_TOOLS_OPEN, port.OLD_TOOLS_RENDER[0]]),
        "dsh-plugin-discord/lib/gateway-scopes.mjs": port.OLD_SCOPES_MEMORIES,
        "dsh-plugin-discord/lib/natural-participation.mjs": port.NP_WARN[0],
        "dsh-fish-memory/lib/index.js": port.OLD_FISH_CFG,
    }
    for name, text in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return contents


class PortTests(unittest.TestCase):
    def test_runtime_rules_replace_old_settings_and_private_labels(self):
        ported = port.port_runtime(fake_runtime())
        self.assertNotIn("reasoningEffort: 'off'", ported)
        self.assertIn("reasoningEffort: go ? 'low' : 'off'", ported)
        self.assertIn("sessionId: `session-${randomUUID()}`", ported)
        self.assertIn("maxTokens: 2000", ported)
        self.assertIn("kind: 'event', importance: 0.6", ported)
        self.assertNotIn("（私下跟你說的，群裡別提）", ported)
        self.assertIn("name: 'social-memory-manners'", ported)
        self.assertIn("this.recallFor(", ported)
        # 第 1 轮审查：要不要翻问回忆守门员；/記憶 的输出照范围；闸道的权限变动事件让快取作废；批次自己醒来。
        self.assertIn("return this.studios.has(agent.id) ? undefined : this.awaitRecall(source, agent);", ported)
        self.assertIn("this.scopeView(this.whereOf(this.ingress.get(message.id)))", ported)
        self.assertIn("if (this.visibility.observe(payload)) this.recallPlans.clear();", ported)
        self.assertIn("if (!failure.gaveUp) this.scheduleNotes();", ported)
        self.assertIn("return this.noteTasks.get(batch.id) ?? this.queueNoteBatch(batch.id);", ported)
        self.assertNotIn("this.cachedFetch(path)", ported.split("this.visibility = new ChannelVisibility")[1][:200])
        with self.assertRaises(ValueError):
            port.port_runtime(ported)

    def test_runtime_refuses_release_without_studio_fix(self):
        with self.assertRaises(ValueError):
            port.port_runtime(fake_runtime().replace("drawRequestTool", "x"))

    def test_runtime_refuses_unexpected_consolidate(self):
        with self.assertRaises(ValueError):
            port.port_runtime(fake_runtime().replace("maxTokens: 800,", "maxTokens: 900,"))

    def test_each_file_refuses_a_second_port(self):
        for fn, text in ((port.port_memory, JOIN.join([port.MEMORY_SIGNATURE[0], port.MEMORY_CATEGORY[0]])),
                         (port.port_fish, port.OLD_FISH_CFG), (port.port_scopes, port.OLD_SCOPES_MEMORIES),
                         (port.port_participation, port.NP_WARN[0])):
            with self.assertRaises(ValueError):
                fn(fn(text))

    def test_whole_port_writes_everything_or_nothing(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = pathlib.Path(scratch)
            fake_release(root)
            # 有一个接点对不上（chat-tools 的 execute 被改过）：整个停下，一个档案都不写。
            tools = root / "dsh-social-runtime/chat-tools.mjs"
            tools.write_text(tools.read_text(encoding="utf-8").replace("scopes(exec.agent)", "other(exec.agent)"), encoding="utf-8")
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with self.assertRaises(ValueError):
                port.main([str(root), "--worktree"])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
            # 接点都对得上：全部写入，新增两个模组。
            fake_release(root)
            port.main([str(root), "--worktree"])
            for name in port.ADDED:
                self.assertTrue((root / name).exists(), name)
            self.assertIn("提示过没有配置", (root / "dsh-fish-memory/lib/index.js").read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                port.main([str(root), "--worktree"])


@unittest.skipUnless(os.environ.get("DSH_RELEASE_COPY"), "没有 release 副本（设 DSH_RELEASE_COPY）")
class RealReleaseCopyTests(unittest.TestCase):
    def test_port_applies_on_a_scratch_copy_and_modules_parse(self):
        with tempfile.TemporaryDirectory() as scratch:
            target = pathlib.Path(scratch) / "release"
            shutil.copytree(os.environ["DSH_RELEASE_COPY"], target)
            for path in target.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            port.main([str(target), "--worktree"])
            for name in port.PATCHED + port.ADDED:
                subprocess.run(["node", "--check", str(target / name)], check=True)
            with self.assertRaises(ValueError):
                port.main([str(target), "--worktree"])


if __name__ == "__main__":
    unittest.main()
