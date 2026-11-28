"""把「正在输入不停」的修正与背景画室移植到 VM 现行 release 的副本（不整档覆盖）。

现行 release（social64-trust-krita53-books-cef077-discord-bounded-1126）有仓库里没有的补丁：
gatewayScopes、history-view、drain、承诺、Krita 对帐。这里只替换本次修正需要的接点，其余原样保留；
任何接点对不上就整个停下，不写任何档案；写到一半失败会把已写的还原（不留半套）。

    python deploy/dsh/port_typing_fix.py <release 副本根目录> [--repo <仓库根目录>] [--base a86bd2c]

release 副本根目录底下要有 dsh-plugin-discord/lib/ 与 dsh-social-runtime/（VM 上是
web/node_modules/ 底下那两个套件）。步骤与验收见 typing-hang-port.md。
"""

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

import social_patch as sp

BASE = "a86bd2c"  # 现行 release 的 natural-participation.mjs 与 bridge.mjs 是从这一版改出来的

# 现行 release 的 runPrompt 有两段等待：whenIdle 之后再 settleSocialTurn；两段要一起换掉，
# 只换 whenIdle 时，已经正常回话的回合还会被旧的 settle 卡住（第 1 轮审查）。
RELEASE_WAIT = "await agent.whenIdle();\n\t\t\tawait settleSocialTurn(agent);"
# 现行 release 的群聊送讯（含承诺追踪）；整段换成「每次成功送出就记一笔」的版本。
RELEASE_SEND = '''let sent;
if (prepared.text.trim()) {
  sent = await rest.createMessage(message.channel_id, prepared.text, target.messageId);
  await recordSocialCommitment(message, sent, participation.replyPartner(target, message.author.id), drafts);
}
if (prepared.files.length) sent = (await rest.createMessageWithFiles(message.channel_id, "", prepared.files)) ?? sent;
if (!sent) continue;
participation.recordReply(message.channel_id, target.text, sent.id, participation.replyPartner(target, message.author.id), target.messageId, index === 0);'''
SEND = (RELEASE_SEND, sp.group_send("  await recordSocialCommitment(message, sent, participation.replyPartner(target, message.author.id), drafts);\n"))
# 现行 release 才有的：承诺追问那一句也是这一轮的答覆。
COMMITMENT_REPLY = ("participation.recordReply(message.channel_id, text, sent.id, message.author.id, message.id);",
                    "participation.recordReply(message.channel_id, text, sent.id, message.author.id, message.id, true, naturalTicket);")
PARTICIPATION_LOG = ("  warn: (text) => logger.warn(text),\n  guildOwner:",
                     "  warn: (text) => logger.warn(text),\n  log: (text) => logger.info(text),\n  guildOwner:")
SUBSCRIBE_ANCHOR = "async function hydrateSameChannelReply(message) {"
OLD_MESSAGE_FAILED = '.catch(() => logger.warn("群聊行为处理未完成，保持安静。"))'
OLD_EDIT_FAILED = '.catch(() => logger.warn("编辑讯息处理未完成，保持安静。"))'
# 闸道租约只结束自己的：A 期限到被放弃后才收尾时，不能清掉已经轮到的 B 的租约（第 2 轮审查）。
LEASE_DECLARE = ("let scopeAgent;", "let scopeAgent;\n\t\tlet socialLease;")
# 期限到或 /stop 以后才轮到的（被放弃后才收尾的 A）：不再去拿闸道租约。turn() 会覆盖同一个会话的租约，
# A 晚拿一次就把已经轮到的 B 的租约换掉，B 的正常答覆会被当成不能投递。
LEASE_GUARD = ("scopeAgent=agent;", "if (socialBudget?.signal.aborted) return { chunks: [TURN_DEADLINE_NOTICE], files: [] };\n\t\t\tscopeAgent=agent;")
LEASE_TAKE = ("if (this.gatewayScopes && !await this.gatewayScopes.turn(agent,channelId,messageId))\n\t\t\treturn {chunks:[],files:[]};",
              "if (this.gatewayScopes && !await this.gatewayScopes.turn(agent,channelId,messageId))\n\t\t\treturn {chunks:[],files:[]};\n\t\tsocialLease = this.gatewayScopes?.active.get(agent);")
LEASE_END = ("if (scopeAgent) this.gatewayScopes?.end(scopeAgent);",
             "if (scopeAgent && this.gatewayScopes?.active.get(scopeAgent) === socialLease) this.gatewayScopes.end(scopeAgent);")
# 背景画室：/stop 也停画室；登入后建画室，画室收下请求时告诉群聊行为处理。
STUDIO_IMPORT = 'import { createSocialStudio, stopSocialStudio } from "dsh-social-runtime/bridge";\n'
STUDIO_STOP = ("commandStop(channelId) {\n\t\tstopSocialTurns(channelId);",
               "commandStop(channelId) {\n\t\tstopSocialTurns(channelId);\n\t\tstopSocialStudio(channelId);")
STUDIO_WIRING = '''const socialStudio = createSocialStudio({ bridges: { group: bridge, owner: ownerBridge }, rest, log: (text) => logger.info(text) });
socialStudio.on((event) => participation.drawEvent(event));
'''
# 闸道核验过的聊天请求由 history-view 附上的工具说明：聊天会话改成「交给画室」的说明。
HISTORY_GUIDE = ("return tools.KRITA_GUIDE+tools.referenceHint(",
                 "return (await import(new URL('../../dsh-social-runtime/studio.mjs',import.meta.url))).DRAW_GUIDE+tools.referenceHint(")

BRIDGE_IMPORT = ("import { currentRuntime } from './index.mjs';",
                 "import { currentRuntime } from './index.mjs';\nimport { foldSocialReply } from './turn-guard.mjs';\nimport { connectStudio } from './studio.mjs';")
BRIDGE_KRITA_IMPORT = ("import { reconcileKritaReply } from './krita-tools.mjs';",
                       "import { kritaDeliveries, reconcileKritaReply } from './krita-tools.mjs';")
BRIDGE_EXTRACT_START = "export function extractSocialReply(events, promptSeq) {"
BRIDGE_EXTRACT_KEEP = "\tconst messageId = sourceOf(events.find((event) => event.seq === promptSeq))?.discordMessageId;"
BRIDGE_EXTRACT_NEW = BRIDGE_EXTRACT_START + "\n\tconst { text, reasonKind, errorMessage } = foldSocialReply(events, promptSeq);\n"
BRIDGE_EXPORT = '''
// 一轮的期限、取消、归帐与取回答（typing-hang 修正）。
export * from './turn-guard.mjs';
// 背景画室：画图不在聊天回合里做。
export * from './studio.mjs';

export function createSocialStudio(options) {
	const runtime = currentRuntime();
	return connectStudio({
		...options,
		runtime,
		toGroupText,
		deliveries: (messageId) => kritaDeliveries(runtime, messageId),
	});
}

export function stopSocialStudio(channelId) {
	return currentRuntime().studio?.stopChannel(channelId) ?? 0;
}
'''

# ---- dsh-social-runtime/index.mjs：画室会话只画图，聊天会话只交代 ----
RUNTIME = [
    ('import { kritaTools, kritaGuideFor, reportKritaRoute, observeKritaScope, referenceAttachments } from "./krita-tools.mjs";',
     'import { kritaTools, kritaGuideFor, reportKritaRoute, observeKritaScope, referenceAttachments } from "./krita-tools.mjs";\n'
     'import { referenceHint as kritaReferenceHint } from "./krita-tools.mjs";\n'
     'import { drawRequestTool, DRAW_GUIDE } from "./studio.mjs";'),
    ('for (const tool of kritaTools(runtime)) ctx.tools.register(tool);',
     'for (const tool of kritaTools(runtime)) ctx.tools.register(tool);\n\tctx.tools.register(drawRequestTool(runtime));'),
    # 画室会话的编号（→ 它服务的聊天会话）；模型请求另外计数，画图时聊天不会被「目前有点忙」挡下。
    ('this.inFlight = 0;', 'this.inFlight = 0;\n\t\tthis.studioInFlight = 0;\n\t\tthis.studios = new Map();'),
    ('if (this.inFlight >= 2) {',
     'const studioCall = this.studios.has(options.sessionId);\n\t\tif (studioCall ? this.studioInFlight >= 1 : this.inFlight >= 2) {'),
    ('this.inFlight++;', 'if (studioCall) this.studioInFlight++;\n\t\telse this.inFlight++;'),
    ('this.inFlight--;', 'if (studioCall) this.studioInFlight--;\n\t\t\telse this.inFlight--;'),
    ('toolGuard(exec) {',
     "toolGuard(exec) {\n"
     "\t\tif (this.studios.has(exec.agent?.id) && exec.name !== 'krita_batch') return '畫室只用 krita_batch 畫圖。';\n"
     "\t\tif (exec.name === 'krita_batch' && !this.studios.has(exec.agent?.id)) return '畫圖交給背景畫室：用 draw_request 交代要畫什麼，再回一句短話；不要自己用 krita_batch。';"),
    ("exec.name !== 'remember' && exec.name !== 'krita_batch'",
     "exec.name !== 'remember' && exec.name !== 'draw_request'"),
    ("agent.ctx.systemPrompt.context({ name: 'krita-works', order: 951, interpolate: false, required: true, text: () => kritaGuideFor(this, agent) });",
     "agent.ctx.systemPrompt.context({ name: 'krita-works', order: 951, interpolate: false, required: true, text: () => this.studios.has(agent.id) ? kritaGuideFor(this, agent) : (kritaGuideFor(this, agent) ? DRAW_GUIDE + kritaReferenceHint(this.turns.get(agent.id)) : '') });"),
    ("if (this.isGroup(agent) && !this.groupToolsFull) {\n\t\t\tagent.ctx.tools.restrict({\n\t\t\t\tallow: ['remember', 'chat_search', 'chat_open', 'krita_batch'],\n\t\t\t});",
     "if (this.studios.has(agent.id)) {\n\t\t\tagent.ctx.tools.restrict({ allow: ['krita_batch'] });\n\t\t\tagent.ctx.tools.presentAs('native');\n"
     "\t\t} else if (this.isGroup(agent) && !this.groupToolsFull) {\n\t\t\tagent.ctx.tools.restrict({\n\t\t\t\tallow: ['remember', 'chat_search', 'chat_open', 'draw_request'],\n\t\t\t});"),
    ("if (this.isGroup(agent)) {\n\t\t\tagent.ctx.systemPrompt.section({\n\t\t\t\tname: 'social-stickers',",
     "if (this.isGroup(agent) && !this.studios.has(agent.id)) {\n\t\t\tagent.ctx.systemPrompt.section({\n\t\t\t\tname: 'social-stickers',"),
]

# ---- dsh-social-runtime/krita-tools.mjs ----
KRITA = [
    # 画室画的图仍归原本的聊天会话（续改、recent 找得到以前在聊天里画的作品）。
    ("const session = agent?.session?.id;", "const session = runtime.studios?.get(agent?.id) ?? agent?.session?.id;"),
    ("\t\t\t\t\t\tconst receipt = {", "\t\t\t\t\t\tif (preview) turn.previewsSent = (turn.previewsSent ?? 0) + 1;\n\t\t\t\t\t\tconst receipt = {"),
]
KRITA_EXPORT = '''
// 画室用：这则请求 Krita 实际回覆了几张（正式交付、预览）。
export function kritaDeliveries(runtime, messageId) {
	const turn = deliveries.get(runtime)?.get(String(messageId));
	if (!turn) return { final: 0, previews: 0 };
	const final = [...turn.receipts.values()].filter((receipt) => receipt?.delivery === 'sent').length;
	return { final, previews: turn.previewsSent ?? 0 };
}
'''


def port_index(content):
    once = sp.once
    if "runSocialTurn" in content:
        raise ValueError("这份 release 已经移植过。")
    content = sp.TURN_IMPORT + STUDIO_IMPORT + content
    for anchor, replacement in sp.LIFECYCLE:
        content = once(content, anchor, replacement)
    content = once(content, sp.FOLLOWUP[0], sp.FOLLOWUP[1])
    content = once(content, RELEASE_WAIT, sp.WAIT)
    for anchor, replacement in (sp.NOTICE_TICKET, SEND, COMMITMENT_REPLY, sp.TRACKED_REST, PARTICIPATION_LOG,
                                LEASE_DECLARE, LEASE_GUARD, LEASE_TAKE, LEASE_END, STUDIO_STOP, HISTORY_GUIDE):
        content = once(content, anchor, replacement)
    content = once(content, SUBSCRIBE_ANCHOR, sp.SUBSCRIBE + STUDIO_WIRING + SUBSCRIBE_ANCHOR)
    content = once(content, OLD_MESSAGE_FAILED, sp.MESSAGE_FAILED)
    content = once(content, OLD_EDIT_FAILED, sp.EDIT_FAILED)
    # 保留现行 release 自己的补丁（gatewayScopes、history-view、drain、承诺、Krita）。
    for kept in ("this.gatewayScopes.turn(agent,channelId,messageId)", "historyCanDeliver(agent,messageId)",
                 "historyView.note(", "dsh.maintenance-drain.root.v1", "parseCommitmentReply(chunk)",
                 "await prepareSocialTurn(agent, channelId, messageId);", "recordSocialCommitment(message, sent,",
                 "tools.referenceHint("):
        if kept not in content:
            raise ValueError("移植后少了现行 release 的补丁，停止。")
    for gone in ("await agent.whenIdle();", "await settleSocialTurn(agent);", "findPromptSeq(events, messageId)",
                 "String(error)}`);\n\t\t\t\t\t\tbreak;", "rest.triggerTyping(channelId)", "if (!sent) continue;",
                 "incoming attachment failed: ${String(error)}"):
        if gone in content:
            raise ValueError("旧的等待、送讯或日志还在，停止。")
    return content


def port_bridge(content, develop_bridge):
    if "turn-guard.mjs" in content:
        raise ValueError("这份 release 的 bridge.mjs 已经移植过。")
    content = sp.once(content, *BRIDGE_IMPORT)
    content = sp.once(content, *BRIDGE_KRITA_IMPORT)
    start = content.index(BRIDGE_EXTRACT_START)
    end = content.index(BRIDGE_EXTRACT_KEEP, start)
    # 要换掉的那段必须和 develop 的 extractSocialReply 本体一样（只有结尾 Krita 对帐那行不同）。
    develop_start = develop_bridge.index(BRIDGE_EXTRACT_START)
    develop_end = develop_bridge.index("\treturn { text, reasonKind, errorMessage };", develop_start)
    if content[start:end] != develop_bridge[develop_start:develop_end]:
        raise ValueError("现行 release 的 extractSocialReply 和 develop 不同，须重新审阅。")
    content = content[:start] + BRIDGE_EXTRACT_NEW + content[end:]
    return content.rstrip("\n") + "\n" + BRIDGE_EXPORT


def port_runtime(content):
    if "drawRequestTool" in content:
        raise ValueError("这份 release 的 index.mjs 已经移植过。")
    for anchor, replacement in RUNTIME:
        content = sp.once(content, anchor, replacement)
    return content


def port_krita(content):
    if "kritaDeliveries" in content:
        raise ValueError("这份 release 的 krita-tools.mjs 已经移植过。")
    for anchor, replacement in KRITA:
        content = sp.once(content, anchor, replacement)
    return content.rstrip("\n") + "\n" + KRITA_EXPORT


def apply_hunks(content, patch):
    """逐段套用 unified diff：每段（上下文加删掉的行）必须在档案里刚好出现一次，否则停止。

    现行 release 在 develop 之外改过几行（承诺），碰到的那几段会少带一点上下文再找，
    但仍然要刚好出现一次才套。
    """
    hunks = []
    for line in patch.split("\n"):
        if line.startswith("@@"):
            hunks.append([])
        elif hunks and line[:1] in (" ", "-", "+"):
            hunks[-1].append((line[:1], line[1:]))
    if not hunks:
        raise ValueError("没有要套用的改动。")
    content = "\n" + content
    for hunk in hunks:
        lead = next(i for i, (tag, _) in enumerate(hunk) if tag != " ")
        trail = next(i for i, (tag, _) in enumerate(reversed(hunk)) if tag != " ")
        trims = sorted(((x, y) for x in range(lead + 1) for y in range(trail + 1)), key=sum)
        for x, y in trims:
            part = hunk[x:len(hunk) - y]
            # 至少留两行上下文（或有要删的行），免得只凭一行巧合对上；前后都对齐到整行。
            if sum(tag == " " for tag, _ in part) < 2 and all(tag != "-" for tag, _ in part):
                continue
            before = "\n" + "\n".join(text for tag, text in part if tag != "+") + "\n"
            after = "\n" + "\n".join(text for tag, text in part if tag != "-") + "\n"
            # 相邻两段会共用换行，str.count 不算重叠的，这里逐一找。
            first = content.find(before)
            if first >= 0 and content.find(before, first + 1) < 0:
                content = content.replace(before, after, 1)
                break
        else:
            raise ValueError("natural-participation.mjs 有一段改动在现行 release 里对不上：" + hunk[lead][1].strip()[:60])
    return content[1:]


def write_all(files, replace=os.replace):
    """全部写成功才算：先写暂存档，再逐一换上；换到一半失败就把已换的还原、删掉新增的。"""
    staged = []
    try:
        for path, data in files:
            temporary = path.with_name(path.name + ".port-tmp")
            temporary.write_bytes(data)
            staged.append((path, temporary))
        backups = []
        for path, _ in staged:
            backup = path.with_name(path.name + ".port-bak") if path.exists() else None
            if backup:
                shutil.copy2(path, backup)
            backups.append((path, backup))
        done = []
        try:
            for path, temporary in staged:
                replace(temporary, path)
                done.append(path)
        except OSError as error:
            for path, backup in backups:
                if path not in done:
                    continue
                if backup:
                    os.replace(backup, path)
                else:
                    path.unlink(missing_ok=True)
            raise ValueError(f"写入 {pathlib.Path(staged[len(done)][0]).name} 失败（{type(error).__name__}），已还原已写的档案。") from error
        finally:
            for _, backup in backups:
                if backup and backup.exists():
                    backup.unlink()
    finally:
        for _, temporary in staged:
            if temporary.exists():
                temporary.unlink()


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, check=True).stdout


def main(argv=None, replace=os.replace):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("release", type=pathlib.Path)
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--rev", default="HEAD", help="要移植的提交（预设 HEAD）")
    parser.add_argument("--worktree", action="store_true", help="用工作目录里还没提交的版本（只供本机检查）")
    args = parser.parse_args(argv)
    plugin = args.release / "dsh-plugin-discord/lib"
    runtime = args.release / "dsh-social-runtime"
    index_path, np_path = plugin / "index.js", plugin / "natural-participation.mjs"
    bridge_path, runtime_path, krita_path = runtime / "bridge.mjs", runtime / "index.mjs", runtime / "krita-tools.mjs"
    read = lambda path: path.read_text(encoding="utf-8").replace("\r\n", "\n")
    source = lambda name: (args.repo / name).read_bytes() if args.worktree else git(args.repo, "show", f"{args.rev}:{name}")
    index = port_index(read(index_path))
    develop_bridge = git(args.repo, "show", f"{args.base}:deploy/social/bridge.mjs").decode("utf-8")
    bridge = port_bridge(read(bridge_path), develop_bridge)
    social = port_runtime(read(runtime_path))
    krita = port_krita(read(krita_path))
    # natural-participation.mjs：现行 release = develop + 承诺的几行，把本分支对 develop 的改动逐段套上去
    # （每段的上下文都要在现行 release 里刚好出现一次）。
    patch = git(args.repo, "diff", "-U3", args.base, *([] if args.worktree else [args.rev]), "--", "deploy/dsh/natural-participation.mjs").decode("utf-8")
    participation = apply_hunks(read(np_path), patch)
    encode = lambda text: text.encode("utf-8")
    # 全部检查过才写档；写到一半失败会还原。
    write_all([
        (np_path, encode(participation)),
        (index_path, encode(index)),
        (bridge_path, encode(bridge)),
        (runtime_path, encode(social)),
        (krita_path, encode(krita)),
        (runtime / "turn-guard.mjs", source("deploy/social/turn-guard.mjs")),
        (runtime / "studio.mjs", source("deploy/social/studio.mjs")),
    ], replace=replace)
    print("已移植：index.js、natural-participation.mjs、bridge.mjs、index.mjs、krita-tools.mjs，新增 turn-guard.mjs、studio.mjs")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print("移植中止，没有写入任何档案：" + str(error), file=sys.stderr)
        sys.exit(1)
