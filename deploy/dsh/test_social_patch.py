"""群聊接线补丁：一轮有期限且必定熄灯、送讯凭这一轮的 ticket 记帐、日志不写内文
（2026-11-28 「正在输入」不停的事故）。

公开仓库不收录上游桥接原文，这里用补丁自己的接点拼出一份假的上游入口，
确认每个接点都还唯一匹配，并检查补丁后的关键接线。真正的执行顺序由
typing-hang-repro.mjs 对 VM 现行 release 的副本实际跑一遍。
"""

import re
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

    def test_typing_lifecycle_is_owned_by_the_turn_guard(self):
        # 「正在输入」由 runSocialTurn 亮、由它熄；runPrompt 拿到这一轮的 budget。
        self.assertIn("await runSocialTurn({ channelId, messageId, beginTyping", self.patched)
        self.assertIn("this.runPrompt(channelId, messageId, text, socialBudget.beginTyping, attachments, socialBudget)", self.patched)
        self.assertIn("async runPrompt(channelId, messageId, text, beginTyping, attachments, socialBudget) {", self.patched)
        # 附件下载与 flush 都有期限，不会卡住灯。
        self.assertIn("AbortSignal.timeout(3e4)", self.patched)
        self.assertIn("await socialBounded(this.saveIncoming(", self.patched)
        self.assertIn("await socialBounded(flushable.flush(agent.session), socialBudget?.signal)", self.patched)
        self.assertNotIn("await flushable.flush(agent.session);", self.patched)

    def test_wait_and_extract_are_bound_to_this_inbox_message(self):
        self.assertNotIn("await agent.whenIdle();", self.patched)
        self.assertNotIn("findPromptSeq(events, messageId)", self.patched)
        self.assertIn("const socialPrompt = socialFollowup(agent, this.userMessage([{", self.patched)
        self.assertIn("await awaitSocialTurn(agent, socialPrompt, { signal: socialBudget?.signal", self.patched)
        self.assertIn("const promptSeq = socialPromptSeq(events, socialPrompt);", self.patched)
        self.assertIn("if (socialWait.timedOut) return { chunks: [TURN_DEADLINE_NOTICE], files: [] };", self.patched)
        # 期限到了就不再把讯息送进会话。
        self.assertIn("if (socialBudget?.signal.aborted) return { chunks: [TURN_DEADLINE_NOTICE], files: [] };\nconst socialPrompt", self.patched)

    def test_every_send_is_booked_by_ticket(self):
        self.assertIn("participation.turnNotice(message.channel_id, chunk, naturalTicket)", self.patched)
        self.assertIn("target.messageId, index === 0, naturalTicket);", self.patched)
        self.assertIn("participation.recordFiles(message.channel_id, naturalTicket, await rest.createMessageWithFiles(", self.patched)
        self.assertEqual(self.patched.count("participation.sendFailed(message.channel_id, naturalTicket);"), 2)
        # 工具送讯（discord_send_message、Krita）记到正在跑的那则呼叫；开始画图时先说一句。
        self.assertIn("registerDiscordRest(trackSocialSends(rest, (channelId, messageId) => participation.recordToolMessage(channelId, messageId)));", self.patched)
        self.assertIn("onSocialTurn((event) => participation.turnEvent(event));", self.patched)
        # 控制讯息不带 ticket：不算答覆。
        self.assertIn("participation.recordReply(message.channel_id, text, sent.id);\n  };", self.patched)

    def test_logs_carry_codes_not_content(self):
        for line in self.patched.splitlines():
            if "logger.warn" in line or 'this.log("warn"' in line:
                self.assertNotRegex(line, r"String\(error|error\?\.stack|error\.message|redactSecrets\(String", line)
        self.assertIn("群聊行为处理未完成，保持安静 #${message.id}：${errorTrace(error)}", self.patched)
        self.assertIn("编辑讯息处理未完成，保持安静 #${message.id}：${errorTrace(error)}", self.patched)
        self.assertIn("prompt failed #${messageId}：${errorTrace(error)}", self.patched)
        self.assertIn("log: (text) => logger.info(text),", self.patched)
        self.assertIn("errorTrace", self.patched.splitlines()[1])

    def test_round_two_lifecycle_fixes(self):
        # 每次成功送出就记一笔：文字送出就算答覆，后面的贴图附件失败不再让她补「卡住了」。
        self.assertNotIn("if (!sent) continue;", self.patched)
        self.assertIn("if (!sent) throw error;", self.patched)
        self.assertIn("discord-bridge: sticker upload failed #${message.id}：${errorTrace(error)}", self.patched)
        # /stop 连排队中的也停；「正在输入」熄灯后不重送；附件下载失败不写错误内文。
        self.assertIn("commandStop(channelId) {\n\t\tstopSocialTurns(channelId);", self.patched)
        self.assertIn("const socialQueuedAt = Date.now();", self.patched)
        self.assertIn("socialTyping(rest, channelId, config.typingIntervalMs)", self.patched)
        self.assertNotIn("rest.triggerTyping(channelId)", self.patched)
        self.assertIn("incoming attachment failed #${messageId}：${errorTrace(error)}", self.patched)

    def test_drifted_upstream_is_refused(self):
        for anchor in ("await agent.whenIdle();", "const promptSeq = findPromptSeq(events, messageId);", "const response = await fetch(attachment.url);"):
            with self.assertRaises(ValueError):
                module.social_patch(fake_upstream().replace(anchor, re.sub(r"\w+", "x", anchor, count=1)))


if __name__ == "__main__":
    unittest.main()
