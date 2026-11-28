"""移植脚本：只换本次修正的接点、保留现行 release 自己的补丁，对不上就整个停下。

公开仓库不收录现行 release，这里用移植脚本自己的接点拼出一份假的 release；
设了 DSH_RELEASE_COPY（release 副本根目录）时，另外对真的副本跑一次（只在暂存复本上写）。
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import port_typing_fix as port
import social_patch as sp

KEPT = ["this.gatewayScopes.turn(agent,channelId,messageId)", "historyCanDeliver(agent,messageId)", "historyView.note(",
        "dsh.maintenance-drain.root.v1", "parseCommitmentReply(chunk)", "await prepareSocialTurn(agent, channelId, messageId);"]


def fake_release():
    anchors = [anchor for anchor, _ in sp.LIFECYCLE]
    anchors += [sp.FOLLOWUP[0], port.RELEASE_WAIT, sp.NOTICE_TICKET[0], sp.RECORD_TICKET[0], port.COMMITMENT_REPLY[0],
                sp.TRACKED_REST[0], port.PARTICIPATION_LOG[0], port.SUBSCRIBE_ANCHOR, port.OLD_MESSAGE_FAILED, port.OLD_EDIT_FAILED]
    return "\n/*--*/\n".join(anchors + KEPT) + "\n"


class PortIndexTests(unittest.TestCase):
    def test_both_release_waits_are_replaced_and_release_patches_kept(self):
        ported = port.port_index(fake_release())
        self.assertNotIn("await settleSocialTurn(agent);", ported)
        self.assertNotIn("await agent.whenIdle();", ported)
        self.assertIn(sp.WAIT, ported)
        for kept in KEPT:
            self.assertIn(kept, ported)
        self.assertTrue(ported.startswith(sp.TURN_IMPORT))
        self.assertIn("message.id, true, naturalTicket);", ported)
        self.assertIn(sp.SUBSCRIBE + port.SUBSCRIBE_ANCHOR, ported)

    def test_drift_or_second_run_is_refused(self):
        with self.assertRaises(ValueError):
            port.port_index(fake_release().replace(port.RELEASE_WAIT, "await agent.whenIdle();"))
        with self.assertRaises(ValueError):
            port.port_index(fake_release().replace("historyView.note(", ""))
        with self.assertRaises(ValueError):
            port.port_index(port.port_index(fake_release()))


class ApplyHunksTests(unittest.TestCase):
    PATCH = "--- a/x\n+++ b/x\n@@ -1,4 +1,5 @@\n a\n b\n+new\n c\n d\n"

    def test_hunk_applies_once_and_falls_back_to_less_context(self):
        self.assertEqual(port.apply_hunks("a\nb\nc\nd\n", self.PATCH), "a\nb\nnew\nc\nd\n")
        # release 改过上下文的第一行：少带一点上下文仍唯一，照样套。
        self.assertEqual(port.apply_hunks("A\nb\nc\nd\n", self.PATCH), "A\nb\nnew\nc\nd\n")

    def test_ambiguous_or_missing_hunk_is_refused(self):
        with self.assertRaises(ValueError):
            port.apply_hunks("x\ny\n", self.PATCH)
        with self.assertRaises(ValueError):
            port.apply_hunks("b\nc\nb\nc\n", "@@ -1,2 +1,3 @@\n b\n+new\n c\n")
        # 只认整行：「xb」不是「b」。
        with self.assertRaises(ValueError):
            port.apply_hunks("xb\nc\n", "@@ -1,2 +1,3 @@\n b\n+new\n c\n")


@unittest.skipUnless(os.environ.get("DSH_RELEASE_COPY"), "没有 release 副本（设 DSH_RELEASE_COPY）")
class RealReleaseCopyTests(unittest.TestCase):
    def test_port_applies_on_a_scratch_copy_and_modules_parse(self):
        with tempfile.TemporaryDirectory() as scratch:
            target = pathlib.Path(scratch) / "release"
            shutil.copytree(os.environ["DSH_RELEASE_COPY"], target)
            for path in target.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            port.main([str(target), "--worktree"])
            for name in ("dsh-plugin-discord/lib/index.js", "dsh-plugin-discord/lib/natural-participation.mjs",
                         "dsh-social-runtime/bridge.mjs", "dsh-social-runtime/turn-guard.mjs"):
                subprocess.run(["node", "--check", str(target / name)], check=True)
            with self.assertRaises(ValueError):
                port.main([str(target), "--worktree"])


if __name__ == "__main__":
    unittest.main()
