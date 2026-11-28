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
        "dsh.maintenance-drain.root.v1", "parseCommitmentReply(chunk)", "await prepareSocialTurn(agent, channelId, messageId);",
        "recordSocialCommitment(message, sent,", "tools.referenceHint("]
# 租约那几个接点在现行 release 里是连在一起的（turn() 之前要先查期限，之后才记下自己的租约）。
LEASE_BLOCK = "scopeAgent=agent;\n\t\tif (this.gatewayScopes && !await this.gatewayScopes.turn(agent,channelId,messageId))\n\t\t\treturn {chunks:[],files:[]};"


def fake_release():
    anchors = [anchor for anchor, _ in sp.LIFECYCLE]
    anchors += [sp.FOLLOWUP[0], port.RELEASE_WAIT, sp.NOTICE_TICKET[0], port.RELEASE_SEND, port.COMMITMENT_REPLY[0],
                sp.TRACKED_REST[0], port.PARTICIPATION_LOG[0], port.LEASE_DECLARE[0], LEASE_BLOCK,
                port.LEASE_END[0], port.HISTORY_GUIDE[0], port.SUBSCRIBE_ANCHOR, port.OLD_MESSAGE_FAILED, port.OLD_EDIT_FAILED,
                port.STUDIO_DELETE[0], port.DELIVER_CHECK[0], port.SENT_CONTROL[0], port.SENT_EDIT_CONTROL[0]]
    kept = [k for k in KEPT if k not in ("this.gatewayScopes.turn(agent,channelId,messageId)",
                                         "recordSocialCommitment(message, sent,", "tools.referenceHint(")]
    return "\n/*--*/\n".join(anchors + kept) + "\n"


class PortIndexTests(unittest.TestCase):
    def test_both_release_waits_are_replaced_and_release_patches_kept(self):
        ported = port.port_index(fake_release())
        self.assertNotIn("await settleSocialTurn(agent);", ported)
        self.assertNotIn("await agent.whenIdle();", ported)
        self.assertIn(sp.WAIT, ported)
        for kept in KEPT:
            self.assertIn(kept, ported)
        self.assertTrue(ported.startswith(sp.TURN_IMPORT + port.STUDIO_IMPORT))
        self.assertIn("message.id, true, naturalTicket);", ported)
        self.assertIn(sp.SUBSCRIBE + port.STUDIO_WIRING + port.GONE_WIRING + port.SUBSCRIBE_ANCHOR, ported)

    def test_round_two_fixes_are_wired(self):
        ported = port.port_index(fake_release())
        # 每次成功送出就记一笔：文字一送出就记，贴图附件失败只记日志（不再补「卡住了」）。
        self.assertNotIn("if (!sent) continue;", ported)
        text_sent = ported.index("sent = await socialReply((replyTo) => rest.createMessage(message.channel_id, prepared.text, replyTo), target.messageId,")
        booked = ported.index("participation.recordReply(message.channel_id, target.text, sent.id,", text_sent)
        self.assertLess(booked, ported.index('createMessageWithFiles(message.channel_id, "", prepared.files)', text_sent))
        self.assertIn("recordSocialCommitment(message, sent,", ported)
        self.assertIn("if (!sent) throw error;", ported)
        # 闸道租约：期限到或 /stop 以后不再去拿；只结束自己的。
        self.assertIn("if (socialBudget?.signal.aborted) return { chunks: [TURN_DEADLINE_NOTICE], files: [] };\n\t\t\tscopeAgent=agent;", ported)
        self.assertIn("socialLease = this.gatewayScopes?.active.get(agent);", ported)
        self.assertIn("this.gatewayScopes?.active.get(scopeAgent) === socialLease) this.gatewayScopes.end(scopeAgent);", ported)
        # /stop 停这个频道的讯息与画室；进队列时间记下来，排队中的也停。
        self.assertIn("commandStop(channelId) {\n\t\tstopSocialTurns(channelId);\n\t\tstopSocialStudio(channelId);", ported)
        self.assertIn("const socialQueuedAt = Date.now();", ported)
        self.assertIn("queuedAt: socialQueuedAt", ported)
        # 「正在输入」改用 socialTyping；附件失败的日志不写错误内文。
        self.assertIn("const beginTypingFor = (channelId) => socialTyping(rest, channelId, config.typingIntervalMs);", ported)
        self.assertNotIn("rest.triggerTyping(channelId)", ported)
        self.assertIn("incoming attachment failed #${messageId}：${errorTrace(error)}", ported)
        # 闸道附上的工具说明：聊天会话改成交给画室，附上画室最近的结果（没画成的已经回报过了）。
        self.assertIn("studioModule.DRAW_GUIDE+studioModule.studioRecentNote(", ported)
        # 送答前的核对没过就隔一下再核对（2026-11-28 第 4 则）。
        self.assertIn("!await socialCanDeliver(() => this.gatewayScopes.historyCanDeliver(agent,messageId), {", ported)
        self.assertIn("socialCanDeliver, TURN_REVOKED_NOTICE }", port.STUDIO_IMPORT)

    def test_review_revoked_messages_get_no_fallback_and_sent_replies_are_booked(self):
        ported = port.port_index(fake_release())
        # 讯息被撤销：送答前的核对、闸道租约都回「訊息已撤回」（群聊行为处理不发、不补「卡住了」）；期限到回期限提示。
        self.assertIn("return {chunks:this.gatewayScopes.revoked?.has(messageId) ? [TURN_REVOKED_NOTICE]\n"
                      "\t\t\t\t\t: socialBudget?.signal.aborted ? [TURN_DEADLINE_NOTICE] : [],files:[]};", ported)
        self.assertIn("return {chunks:this.gatewayScopes.revoked?.has(messageId) ? [TURN_REVOKED_NOTICE] : [],files:[]};\n"
                      "\t\tsocialLease =", ported)
        # 她实际送出的话（回答、补的那句、画室的那句）记到那则讯息名下，回放只接这些。
        # 复验②：记的是实际送出的字（群聊转繁体、遮机密、拿掉合成字以后的），不是过滤前的。
        self.assertIn("naturalTicket);\n  historyView.sent(message.channel_id, message.id, prepared.text);", ported)
        self.assertNotIn("historyView.sent(message.channel_id, message.id, target.text)", ported)
        self.assertEqual(ported.count("const sent = await rest.createMessage(message.channel_id, shown, message.id);\n"
                                      "    participation.recordReply(message.channel_id, text, sent.id);\n"
                                      "    historyView.sent(message.channel_id, message.id, shown);"), 2)
        self.assertNotIn("historyView.sent(message.channel_id, message.id, text);", ported)
        # 复验④：讯息被删或改掉，这一轮不管怎么结束都只回「訊息已撤回」。
        self.assertIn('revoked: () => Boolean(this.gatewayScopes?.revoked?.has(messageId)) }', ported)
        self.assertIn("historyView.sent(event.channelId, event.messageId, event.text)", ported)

    def test_round_three_deleted_request_stops_the_studio(self):
        ported = port.port_index(fake_release())
        # 讯息被删（含批次删除）：通知画室安静停掉那张，图不会掉成一般群讯。
        self.assertIn('socialArchive("delete",data);noteSocialStudioDeleted(data);};', ported)
        self.assertIn("noteSocialStudioDeleted,", port.STUDIO_IMPORT)
        self.assertIn("export function noteSocialStudioDeleted(data) {", port.BRIDGE_EXPORT)
        self.assertIn("data.ids", port.BRIDGE_EXPORT)

    def test_drift_or_second_run_is_refused(self):
        with self.assertRaises(ValueError):
            port.port_index(fake_release().replace(port.RELEASE_WAIT, "await agent.whenIdle();"))
        with self.assertRaises(ValueError):
            port.port_index(fake_release().replace("historyView.note(", ""))
        with self.assertRaises(ValueError):
            port.port_index(port.port_index(fake_release()))


class HistoryViewTests(unittest.TestCase):
    def test_replay_is_dated_bounded_and_keeps_her_replies(self):
        fake = "".join(anchor for anchor, _ in (port.HISTORY_HELPERS, port.HISTORY_AGE, port.HISTORY_TEXT,
                                                 port.HISTORY_REPLIES, port.HISTORY_PUSH, port.HISTORY_SENT))
        ported = port.port_history(fake)
        self.assertIn("const REPLAY_MAX_AGE_MS=6*3600_000;", ported)
        # 回放范围照 Discord 讯息的时间（讯息编号），不照会话时间（部署前审查）。
        self.assertIn("filter(([id])=>!liveId || id===liveId || snowflakeTime(id)>=since)", ported)
        self.assertNotIn("event.time", ported)
        # 接上的回答只用这个频道实际送出的（historyView.sent 记的），不从会话里抓。
        self.assertIn("const text=sentText(target.channelId,row.id);", ported)
        self.assertIn("	sent(channel,event,text) {", ported)
        self.assertNotIn("asked=bySeq.has(event.seq)", ported)
        self.assertIn("text:replayText(current,native,id===liveId)", ported)
        self.assertIn("if (event.seq<start && replies.has(event.seq)) result.push(replies.get(event.seq));", ported)
        with self.assertRaises(ValueError):
            port.port_history(ported)

    def test_history_source_carries_name_and_time(self):
        ported = port.port_scopes(port.SCOPES_SOURCE[0] + "\n" + port.SCOPES_GONE[0])
        self.assertIn("name,at:Date.parse(row.timestamp)}", ported)
        # 复验④：取原讯息回 404 Unknown Message（删除事件还没到）也记成撤销。
        self.assertIn("this.noteRevoked({channel_id:row.channel,id:row.event});", ported)
        self.assertIn("10008", ported)
        with self.assertRaises(ValueError):
            port.port_scopes(ported)


class RuntimeAndKritaTests(unittest.TestCase):
    def fake_runtime(self):
        # apply 里注册工具（现行 release 有 8 处）：这里放在 apply 里，加上几处其他工具。
        return (port.APPLY_START + "\n" + port.APPLY_RUNTIME[0]
                + "\n/*--*/\n".join(anchor for anchor, _ in port.RUNTIME)
                + "\n" + "\tctx.tools.register(other);\n" * 3)

    def test_studio_sessions_only_draw_and_chat_sessions_only_hand_off(self):
        ported = port.port_runtime(self.fake_runtime())
        self.assertIn("registerTool(drawRequestTool(runtime));", ported)
        self.assertIn("agent.ctx.tools.restrict({ allow: ['krita_batch'] });", ported)
        self.assertIn("allow: ['remember', 'chat_search', 'chat_open', 'draw_request'],", ported)
        self.assertNotIn("'chat_open', 'krita_batch'", ported)
        self.assertIn("if (exec.name === 'krita_batch') return '畫圖交給背景畫室", ported)
        # 画室的模型请求另外计数：画图时聊天不会被「目前有点忙」挡下。
        self.assertIn("studioCall ? this.studioInFlight >= 1 : this.inFlight >= 2", ported)
        self.assertIn("if (studioCall) this.studioInFlight++;\n\t\telse this.inFlight++;", ported)
        with self.assertRaises(ValueError):
            port.port_runtime(ported)

    def test_round_three_studio_tools_privacy_and_send_origin(self):
        ported = port.port_runtime(self.fake_runtime())
        # 画室会话不管 groupTools 是 chat 还是 full 都能用 krita_batch，其余一律挡；不往下走群聊的限制。
        self.assertIn("if (this.studios.has(exec.agent?.id)) return exec.name === 'krita_batch' ? undefined : '畫室只用 krita_batch 畫圖。';", ported)
        # 画室会话不带记忆、人物卡与相关旧讯息（群聊会话会带拥有者私讯的记忆）。
        self.assertIn("text: () => {\n\t\t\t\tif (this.studios.has(agent.id)) return '';", ported)
        # 每个工具都包上送讯来源；apply 里只剩 registerTool 那一处直接注册。
        self.assertIn("sendOrigin(exec) {", ported)
        self.assertIn("socialSendOriginTool(tool, (exec) => runtime.sendOrigin(exec))", ported)
        self.assertEqual(ported.count("ctx.tools.register("), 1)
        self.assertIn('import { socialSendOriginTool, socialRecencyNote } from "./turn-guard.mjs";', ported)
        with self.assertRaises(ValueError):
            port.port_runtime(self.fake_runtime().replace("\tctx.tools.register(other);\n" * 3, ""))

    def test_ingress_keeps_the_quoted_message_for_the_draw_gate(self):
        ported = port.port_runtime(self.fake_runtime())
        self.assertIn("quoted: quotedOf(message),", ported)
        self.assertIn("studioRecentNote, quotedOf } from \"./studio.mjs\";", ported)

    def test_krita_works_stay_with_the_chat_session_and_deliveries_are_counted(self):
        fake = "\n".join(anchor for anchor, _ in port.KRITA) + "\n"
        ported = port.port_krita(fake)
        self.assertIn("const session = runtime.studios?.get(agent?.id) ?? agent?.session?.id;", ported)
        self.assertIn("turn.previewsSent = (turn.previewsSent ?? 0) + 1;", ported)
        self.assertIn("export function kritaDeliveries(runtime, messageId) {", ported)
        # 请求那则被删了：Discord 拒绝，不掉成一般群讯。
        self.assertIn("fail_if_not_exists: true,", ported)
        self.assertNotIn("fail_if_not_exists: false", ported)
        # 这则请求碰过的作品编号：画室记成这个频道画的（续改要核对 source_job）。
        self.assertIn("works: new Set(),", ported)
        self.assertIn("turn.works.add(work);", ported)
        self.assertIn("works: [...(turn.works ?? [])]", ported)
        with self.assertRaises(ValueError):
            port.port_krita(ported)


class StrictReplyTests(unittest.TestCase):
    # Sol 部署前审查：送答前的核对过了、POST 等待或重试的期间原讯息才被删，回答不能掉成一则没回覆任何人的群讯。
    def test_every_reply_uses_a_strict_reference_and_rejection_counts_as_revoked(self):
        ported = port.port_index(fake_release())
        self.assertNotIn("fail_if_not_exists: false", ported)
        self.assertIn("message_id: replyToMessageId,\n\t\t\t\tfail_if_not_exists: true", ported)
        # 附件（贴图 GIF）没跟着文字送时也挂同一个参照。
        self.assertIn("async createMessageWithFiles(channelId, content, files, replyToMessageId) {", ported)
        self.assertIn("message_reference: { message_id: replyToMessageId, fail_if_not_exists: true }", ported)
        self.assertIn('socialReply((replyTo) => rest.createMessageWithFiles(message.channel_id, "", prepared.files, replyTo), target.messageId,', ported)
        # 被拒（讯息不在）：request 交给 rest.onGone，群聊行为处理、闸道、画室都记成撤回。
        self.assertIn("socialNoteGone(this, path, body, form, socialError);\n\t\t\t\tthrow socialError;", ported)
        for line in ("participation.messageGone(channelId, messageId);",
                     "scopeAuthority?.noteRevoked({ channel_id: channelId, id: messageId });",
                     "noteSocialStudioDeleted({ channel_id: channelId, id: messageId });"):
            self.assertIn(line, ported)
        self.assertIn("socialReply, socialNoteGone,", ported.splitlines()[0])
        # 附件送不出去的那句提示：撤回了就不说。
        self.assertIn('if (participation.canSend(message.channel_id, naturalTicket)) await rest.createMessage(message.channel_id, "⚠️ 附件上传失败', ported)

    def test_a_release_with_a_lenient_reply_left_is_refused(self):
        with self.assertRaises(ValueError):
            port.port_index(fake_release() + "\nfail_if_not_exists: false\n")

    def test_discord_send_message_reply_is_strict(self):
        fake = "x\n" + port.TOOLS_STRICT[0] + "\ny\n"
        ported = port.port_discord_tools(fake)
        self.assertIn(port.TOOLS_STRICT[1], ported)
        self.assertNotIn("fail_if_not_exists: false", ported)
        with self.assertRaises(ValueError):
            port.port_discord_tools(ported)
        with self.assertRaises(ValueError):
            port.port_discord_tools(fake + port.TOOLS_STRICT[0])


class HuanqiTests(unittest.TestCase):
    def fake(self):
        return "".join(anchor for anchor, _ in (port.HUANQI_AFTER_TURN, port.HUANQI_CONTINUE, port.HUANQI_BREAK,
                                                 port.HUANQI_LAND))

    def test_empty_turn_writes_wait_for_the_turn_to_end(self):
        ported = port.port_huanqi(self.fake())
        self.assertIn("function afterTurn(agent, write, what)", ported)
        # 熔断与续跑那段的写入都等这一轮结束，两个 IIFE 都接住错误（不然 rejection 没人接，整个程序结束）。
        self.assertNotIn("\n                    agent.inject(message(fullNote", ported)
        self.assertNotIn("\n                    agent.steer(message(strings.circuitBreakNotice", ported)
        self.assertNotIn("\n                    agent.inject(message(strings.creditWarning", ported)
        self.assertEqual(ported.count("})().catch((error) => ctx.logger.warn("), 2)
        self.assertEqual(ported.count("afterTurn(agent, () => agent."), 3)
        self.assertNotIn("                })();\n", ported)
        with self.assertRaises(ValueError):
            port.port_huanqi(ported)

    def test_huanqi_drift_is_refused(self):
        with self.assertRaises(ValueError):
            port.port_huanqi(self.fake().replace("'huanqi: circuit break (empty turn)'", "'x'"))


class WriteAllTests(unittest.TestCase):
    def test_a_failed_second_write_restores_everything(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = pathlib.Path(scratch)
            first, second, new = root / "a.js", root / "b.js", root / "new.mjs"
            first.write_text("原本 A", encoding="utf-8")
            second.write_text("原本 B", encoding="utf-8")
            calls = []

            def flaky(source, target):
                calls.append(target)
                if len(calls) == 2:
                    raise PermissionError("唯读")
                os.replace(source, target)

            with self.assertRaises(ValueError):
                port.write_all([(first, b"new A"), (second, b"new B"), (new, b"new")], replace=flaky)
            self.assertEqual(first.read_text(encoding="utf-8"), "原本 A")
            self.assertEqual(second.read_text(encoding="utf-8"), "原本 B")
            self.assertFalse(new.exists())
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["a.js", "b.js"], "暂存与备份都清掉")
            port.write_all([(first, b"new A"), (new, b"new")])
            self.assertEqual(first.read_bytes(), b"new A")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["a.js", "b.js", "new.mjs"])


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
                         "dsh-plugin-discord/lib/history-view.mjs", "dsh-plugin-discord/lib/gateway-scopes.mjs",
                         "dsh-social-runtime/bridge.mjs", "dsh-social-runtime/turn-guard.mjs",
                         "dsh-social-runtime/studio.mjs", "dsh-social-runtime/index.mjs",
                         "dsh-social-runtime/krita-tools.mjs", "dsh-social-runtime/discord-tools.mjs",
                         "dsh-huanqi/lib/index.js"):
                subprocess.run(["node", "--check", str(target / name)], check=True)
            with self.assertRaises(ValueError):
                port.main([str(target), "--worktree"])


if __name__ == "__main__":
    unittest.main()
