"""群聊接线补丁：等回合有上限、错误不再被吞掉（2026-11-28 「正在输入」不停的事故）。

公开仓库不收录上游桥接原文，这里用补丁自己的接点拼出一份假的上游入口，
确认每个接点都还唯一匹配，并检查补丁后的关键接线。
"""

import unittest

import social_patch as module


def fake_upstream():
    anchors = []
    original = module.once

    def record(content, anchor, replacement):
        anchors.append(anchor)
        return content + "\n" + replacement

    module.once = record
    try:
        module.social_patch("const participation = new NaturalParticipation({\nrelay = new QuestionRelay({")
    finally:
        module.once = original
    body = "\n/*--*/\n".join(anchors)
    return body + "\nconst participation = new NaturalParticipation({\n/* 原本的群聊接线 */\nrelay = new QuestionRelay({\n"


class SocialPatchTests(unittest.TestCase):
    def setUp(self):
        self.patched = module.social_patch(fake_upstream())

    def test_turn_wait_is_bounded_and_stops_typing_with_notice(self):
        self.assertNotIn("await agent.whenIdle();", self.patched)
        self.assertIn("const socialWait = await awaitSocialTurn(agent, messageId);", self.patched)
        self.assertIn("if (socialWait.timedOut) return { chunks: [TURN_DEADLINE_NOTICE], files: [] };", self.patched)
        self.assertIn("awaitSocialTurn, TURN_DEADLINE_NOTICE", self.patched.splitlines()[0])

    def test_errors_are_logged_with_message_id_not_swallowed(self):
        self.assertNotIn('.catch(() => logger.warn("群聊行为处理未完成，保持安静。"))', self.patched)
        self.assertIn("群聊行为处理未完成，保持安静 #${message.id}：${redactSecrets(", self.patched)
        self.assertIn("编辑讯息处理未完成，保持安静 #${message.id}：${redactSecrets(", self.patched)

    def test_participation_pairs_are_logged_at_info(self):
        self.assertIn("log: (text) => logger.info(text),", self.patched)

    def test_drifted_upstream_is_refused(self):
        with self.assertRaises(ValueError):
            module.social_patch(fake_upstream().replace("await agent.whenIdle();", "await agent.idle();"))


if __name__ == "__main__":
    unittest.main()
