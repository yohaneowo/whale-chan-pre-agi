"""把「正在输入不停」的修正与背景画室移植到 VM 现行 release 的副本（不整档覆盖）。

现行 release（social64-trust-krita53-books-cef077-discord-bounded-1126）有仓库里没有的补丁：
gatewayScopes、history-view、drain、承诺、Krita 对帐。这里只替换本次修正需要的接点，其余原样保留；
任何接点对不上就整个停下，不写任何档案；写到一半失败会把已写的还原（不留半套）。

    python deploy/dsh/port_typing_fix.py <release 副本根目录> [--repo <仓库根目录>] [--base a86bd2c]

release 副本根目录底下要有 dsh-plugin-discord/lib/、dsh-social-runtime/ 与 dsh-huanqi/（VM 上是
web/node_modules/ 底下那三个套件）。步骤与验收见 typing-hang-port.md。
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
              "if (this.gatewayScopes && !await this.gatewayScopes.turn(agent,channelId,messageId))\n"
              "\t\t\treturn {chunks:this.gatewayScopes.revoked?.has(messageId) ? [TURN_REVOKED_NOTICE] : [],files:[]};\n"
              "\t\tsocialLease = this.gatewayScopes?.active.get(agent);")
# 送答前的核对偶尔会因为 Discord 慢或限流没过，整轮答覆就被丢掉（2026-11-28 第 4 则）：没过隔一下再核对。
# 部署前审查：讯息被撤销（删掉或改掉）时回「訊息已撤回」的提示（群聊行为处理不发出去、也不补「卡住了」，
# 不然补的那句会掉成一般群讯）；期限到或 /stop 时照期限的提示。
DELIVER_CHECK = ("if (this.gatewayScopes && !await this.gatewayScopes.historyCanDeliver(agent,messageId))\n\t\t\t\treturn {chunks:[],files:[]};",
                 "if (this.gatewayScopes && !await socialCanDeliver(() => this.gatewayScopes.historyCanDeliver(agent,messageId), {\n"
                 "\t\t\t\tsignal: socialBudget?.signal,\n"
                 "\t\t\t\trevoked: () => Boolean(this.gatewayScopes.revoked?.has(messageId)),\n"
                 "\t\t\t\tonMiss: (attempt) => this.log(\"warn\", `送答核对没过 #${messageId}：第 ${attempt} 次`),\n"
                 "\t\t\t}))\n"
                 "\t\t\t\treturn {chunks:this.gatewayScopes.revoked?.has(messageId) ? [TURN_REVOKED_NOTICE]\n"
                 "\t\t\t\t\t: socialBudget?.signal.aborted ? [TURN_DEADLINE_NOTICE] : [],files:[]};")
LEASE_END = ("if (scopeAgent) this.gatewayScopes?.end(scopeAgent);",
             "if (scopeAgent && this.gatewayScopes?.active.get(scopeAgent) === socialLease) this.gatewayScopes.end(scopeAgent);")
# 背景画室：/stop 也停画室；登入后建画室，画室收下请求时告诉群聊行为处理。
STUDIO_IMPORT = 'import { createSocialStudio, stopSocialStudio, noteSocialStudioDeleted, socialCanDeliver, TURN_REVOKED_NOTICE } from "dsh-social-runtime/bridge";\n'
# 请求讯息被删（含批次删除）：画室那张安静停掉，画好的图不会掉成一般群讯（第 3 轮审查）。
STUDIO_DELETE = ('const onMessageDelete = (data) => {scopeAuthority?.noteRevoked(data);socialArchive("delete",data);};',
                 'const onMessageDelete = (data) => {scopeAuthority?.noteRevoked(data);socialArchive("delete",data);noteSocialStudioDeleted(data);};')
STUDIO_STOP = ("commandStop(channelId) {\n\t\tstopSocialTurns(channelId);",
               "commandStop(channelId) {\n\t\tstopSocialTurns(channelId);\n\t\tstopSocialStudio(channelId);")
STUDIO_WIRING = '''const socialStudio = createSocialStudio({ bridges: { group: bridge, owner: ownerBridge }, rest, log: (text) => logger.info(text) });
socialStudio.on((event) => participation.drawEvent(event));
// 画室回覆请求的那句（没画成、画太久…）也是她对那则讯息实际送出的回答，回放时接上。
socialStudio.on((event) => { if (event.type === "end" && event.noteId && event.text) historyView.sent(event.channelId, event.messageId, event.text); });
'''
# 聊天回放接上的「她当时的回答」只用这个频道实际送出的（部署前审查：会话里会有心跳另开一轮的定时输出、
# 没送出去的草稿）：桥接每次成功送出文字、补话（等我一下、卡住了、好我去畫）都记到那则讯息名下。
SENT_REPLY = ("participation.recordReply(message.channel_id, target.text, sent.id, participation.replyPartner(target, message.author.id), target.messageId, index === 0, naturalTicket);\n  await recordSocialCommitment(",
              "participation.recordReply(message.channel_id, target.text, sent.id, participation.replyPartner(target, message.author.id), target.messageId, index === 0, naturalTicket);\n"
              "  historyView.sent(message.channel_id, message.id, prepared.text);\n  await recordSocialCommitment(")
# 回放记的是实际送出的字（群聊转繁体、遮机密以后的；复验：记成过滤前的，回放会接上被拿掉的字）。
SENT_CONTROL = ("const sent = await rest.createMessage(message.channel_id, message.guild_id === void 0 ? redactSecrets(text) : await toGroupText(text), message.id);\n    participation.recordReply(message.channel_id, text, sent.id);",
                "const shown = message.guild_id === void 0 ? redactSecrets(text) : await toGroupText(text);\n"
                "    const sent = await rest.createMessage(message.channel_id, shown, message.id);\n    participation.recordReply(message.channel_id, text, sent.id);\n"
                "    historyView.sent(message.channel_id, message.id, shown);")
SENT_EDIT_CONTROL = ("const sent = await rest.createMessage(message.channel_id, await toGroupText(text), message.id);\n    participation.recordReply(message.channel_id, text, sent.id);",
                     "const shown = await toGroupText(text);\n"
                     "    const sent = await rest.createMessage(message.channel_id, shown, message.id);\n    participation.recordReply(message.channel_id, text, sent.id);\n"
                     "    historyView.sent(message.channel_id, message.id, shown);")
# 讯息被删或改掉（闸道 revoked）：这一轮不管怎么结束都只回「訊息已撤回」（复验：删了以后撞到期限仍发期限提示）。
REVOKED_TURN = ('queuedAt: socialQueuedAt, log: (line) => this.log("info", line) }',
                'queuedAt: socialQueuedAt, log: (line) => this.log("info", line), revoked: () => Boolean(this.gatewayScopes?.revoked?.has(messageId)) }')
# 闸道核验过的聊天请求由 history-view 附上的工具说明：聊天会话改成「交给画室」的说明。
# 另外附上这个频道画室最近的结果（没画成的已经回报过了，不要自己补画）。
HISTORY_GUIDE = ("return tools.KRITA_GUIDE+tools.referenceHint(",
                 "const studioModule=await import(new URL('../../dsh-social-runtime/studio.mjs',import.meta.url));\n"
                 "        return studioModule.DRAW_GUIDE+studioModule.studioRecentNote(ctx.get('socialHeartbeatSessions')?.runtime.studio,target.channelId)+tools.referenceHint(")

# ---- dsh-plugin-discord/lib/history-view.mjs 与 gateway-scopes.mjs：闸道核验过的聊天回放 ----
# 现行 release 在每次呼叫模型前，把聊天会话改写成「这个频道最近 26 则核验过的群友讯息原文」：
# 没有日期、没有说话的人、没有她当时的回答，也没有「叫你」的标记。所以几天前没头没尾的一句会被当成刚刚的话
# （2026-11-28：4 天前同一个频道的一句闲聊），之前的画图请求看起来都还没回，她就一直交给画室（同日第 6、8 则）。
HISTORY_HELPERS = ("let authority;\n", '''let authority;
// 聊天回放只放这么久以内的讯息；更早的不当成眼前的对话（要找旧事用记忆与聊天纪录搜寻）。
const REPLAY_MAX_AGE_MS=6*3600_000;
const snowflakeTime=id=>{ try { return Number((BigInt(id)>>22n)+1420070400000n); } catch { return NaN; } };
const replayStamp=at=>Number.isFinite(at) ? new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',
	hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(at)) : '時間不明';
// 回放的每一则都标上时间与说话的人（都取自 Discord 核对过的原讯息）；这次的那则再标明是不是在叫她
// （取桥接自己写的那行标头，群友的内文伪造不了换行）。
function replayText(current,native,live) {
	const when=replayStamp(Number.isFinite(current.at)?current.at:snowflakeTime(current.id));
	const who=current.name ? current.name+'：' : '';
	if (!live) return `［${when}，之前的訊息］${who}${current.text}`;
	const header=String(native?.content?.find(block=>block.type==='text')?.text??'').split('\\n',1)[0];
	const how=/沒人叫你/u.test(header) ? '沒人叫你，是你自己想插一句，只說一兩句'
		: /沒人直接叫你/u.test(header) ? '沒人直接叫你，但大家在接你剛說的話' : '這則在叫你';
	return `［${when}，這次的新訊息；${how}］${who}${current.text}`;
}
// 她在这个频道对每则讯息实际送出的文字（桥接每次成功送出时记下，historyView.sent）。回放接上的「她当时的回答」
// 只用这里的，不从会话里抓（会话里有心跳另开一轮的定时输出、没送出去的草稿）。只记这次启动以后送出的。
const sentReplies=new Map();
const sentText=(channel,event)=>(sentReplies.get(channel+':'+event)??[]).join('\\n').trim();
// 接上的回答用宿主产生的 assistant 讯息当形状（同一轮她自己的回答；没有就用之前最近一则），文字换成实际送出的。
function replyShape(session,whole,seq,start) {
	let shape;
	let open=false;
	for (const event of whole) {
		if (event.seq>=start) break;
		if (event.seq===seq) { open=true; continue; }
		if (event.seq>seq && (event.type==='turn/end' || origin(event))) { if (open && shape?.same) break; open=false; }
		if (event.type!=='assistant/message' || (event.seq>seq && !open)) continue;
		const message=derivedWithoutCursor(session,event);
		if (message?.role==='assistant') shape={message,same:open};
	}
	return shape?.message;
}
''')
HISTORY_AGE = ("\tconst chosen=[...sources].slice(-26);",
               "\t// 聊天（有当轮讯息）时，太久以前的不再当成眼前的对话回放。时间照 Discord 讯息本身（讯息编号里的时间），\n"
               "\t// 不照进会话的时间（很晚才进会话的旧讯息也算旧的；部署前审查）。\n"
               "\tconst since=Date.now()-REPLAY_MAX_AGE_MS;\n"
               "\tconst chosen=[...sources].filter(([id])=>!liveId || id===liveId || snowflakeTime(id)>=since).slice(-26);")
HISTORY_TEXT = ("content:[{type:'text',text:current.text}]}};",
                "content:[{type:'text',text:replayText(current,native,id===liveId)}]}};")
HISTORY_REPLIES = ("\tconst bySeq=new Map(proven.filter(row=>verified.has(row.id)).map(row=>[row.seq,row]));\n",
                   "\tconst bySeq=new Map(proven.filter(row=>verified.has(row.id)).map(row=>[row.seq,row]));\n"
                   "\t// 回放的旧讯息后面接上她当时在这个频道实际送出的回答（只取文字）：不然一串旧问题看起来都还没回。\n"
                   "\tconst replies=new Map();\n\tfor (const [seq,row] of bySeq) {\n\t\tif (seq>=start) continue;\n"
                   "\t\tconst text=sentText(target.channelId,row.id);\n\t\tconst shape=text && replyShape(session,whole,seq,start);\n"
                   "\t\tif (shape) replies.set(seq,{...shape,id:'verified-reply-'+seq,content:[{type:'text',text}]});\n\t}\n")
HISTORY_SENT = ("\towns(id) { return authority?.owns(id)===true; },\n",
                "\towns(id) { return authority?.owns(id)===true; },\n"
                "\t// 桥接每次成功送出（她的回答、补的那句、画室的那句）都记一笔：哪个频道、回应哪则讯息、送了什么。\n"
                "\tsent(channel,event,text) {\n"
                "\t\tif (!discordId(channel) || !discordId(event) || typeof text!=='string' || !text.trim()) return;\n"
                "\t\tconst key=channel+':'+event;\n\t\tconst rows=sentReplies.get(key)??[];\n\t\tsentReplies.delete(key);\n"
                "\t\tsentReplies.set(key,[...rows,text.trim().slice(0,1000)].slice(-6));\n"
                "\t\tif (sentReplies.size>2048) sentReplies.delete(sentReplies.keys().next().value);\n\t},\n")
HISTORY_PUSH = ("\t\t\tresult.push(retained.message);\n",
                "\t\t\tresult.push(retained.message);\n\t\t\tif (event.seq<start && replies.has(event.seq)) result.push(replies.get(event.seq));\n")
# 送答前的核对向 Discord 取原讯息：回 404 Unknown Message（10008）就是被删了，删除事件还没到也一样当成撤销
# （复验：删除事件还没到、REST 已经查不到时，会掉到「卡住了」的一般备援）。
SCOPES_GONE = ("\t\t\t\tthis.request('/channels/'+row.channel+'/messages/'+row.event,signal),\n",
               "\t\t\t\tthis.request('/channels/'+row.channel+'/messages/'+row.event,signal).catch(error=>{\n"
               "\t\t\t\t\tif (/ 404 [\\s\\S]*\"code\"\\s*:\\s*10008/u.test(String(error?.message))) this.noteRevoked({channel_id:row.channel,id:row.event});\n"
               "\t\t\t\t\tthrow error;\n\t\t\t\t}),\n")
SCOPES_SOURCE = ("return row.content.trim() || attachments.length ? {id:event,text:row.content,actor,attachments,images} : null;",
                 "const name=String(row.author.global_name||row.author.username||'').replace(/[\\r\\n［］[\\]←：:]/gu,'').slice(0,40);\n"
                 "\t\t\treturn row.content.trim() || attachments.length ? {id:event,text:row.content,actor,attachments,images,name,at:Date.parse(row.timestamp)} : null;")

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

// 讯息被删（MESSAGE_DELETE 带 id，MESSAGE_DELETE_BULK 带 ids）：在画或排队的那张安静停掉。
export function noteSocialStudioDeleted(data) {
	let studio;
	try {
		studio = currentRuntime().studio;
	} catch {
		return 0; // runtime 还没就绪：没有画室，也就没有要停的。
	}
	if (!studio || typeof data?.channel_id !== 'string') return 0;
	const ids = Array.isArray(data.ids) ? data.ids : [data.id];
	let count = 0;
	for (const id of ids)
		if (typeof id === 'string') count += studio.requestDeleted(data.channel_id, id);
	return count;
}
'''

# ---- dsh-social-runtime/index.mjs：画室会话只画图，聊天会话只交代 ----
RUNTIME = [
    ('import { kritaTools, kritaGuideFor, reportKritaRoute, observeKritaScope, referenceAttachments } from "./krita-tools.mjs";',
     'import { kritaTools, kritaGuideFor, reportKritaRoute, observeKritaScope, referenceAttachments } from "./krita-tools.mjs";\n'
     'import { referenceHint as kritaReferenceHint } from "./krita-tools.mjs";\n'
     'import { drawRequestTool, DRAW_GUIDE, studioRecentNote, quotedOf } from "./studio.mjs";\n'
     'import { socialSendOriginTool, socialRecencyNote } from "./turn-guard.mjs";'),
    # 群友讯息记下被回覆的那则（谁说的、说了什么、有几张图）：要不要画的守门员要看。
    ("\t\t\treferenceAttachments: referenceAttachments(message, this.ingress),\n",
     "\t\t\treferenceAttachments: referenceAttachments(message, this.ingress),\n\t\t\tquoted: quotedOf(message),\n"),
    ('for (const tool of kritaTools(runtime)) ctx.tools.register(tool);',
     'for (const tool of kritaTools(runtime)) ctx.tools.register(tool);\n\tctx.tools.register(drawRequestTool(runtime));'),
    # 画室会话的编号（→ 它服务的聊天会话）；模型请求另外计数，画图时聊天不会被「目前有点忙」挡下。
    ('this.inFlight = 0;', 'this.inFlight = 0;\n\t\tthis.studioInFlight = 0;\n\t\tthis.studios = new Map();'),
    ('if (this.inFlight >= 2) {',
     'const studioCall = this.studios.has(options.sessionId);\n\t\tif (studioCall ? this.studioInFlight >= 1 : this.inFlight >= 2) {'),
    ('this.inFlight++;', 'if (studioCall) this.studioInFlight++;\n\t\telse this.inFlight++;'),
    ('this.inFlight--;', 'if (studioCall) this.studioInFlight--;\n\t\t\telse this.inFlight--;'),
    # 画室会话只能用 krita_batch，而且不管 groupTools 是 chat 还是 full 都能用（不再往下走群聊的限制）；
    # 聊天会话一律不能用 krita_batch（第 3 轮审查）。
    ('toolGuard(exec) {',
     "toolGuard(exec) {\n"
     "\t\tif (this.studios.has(exec.agent?.id)) return exec.name === 'krita_batch' ? undefined : '畫室只用 krita_batch 畫圖。';\n"
     "\t\tif (exec.name === 'krita_batch') return '畫圖交給背景畫室：用 draw_request 交代要畫什麼，再回一句短話；不要自己用 krita_batch。';"),
    # 画室会话不带记忆、人物卡、相关旧讯息（群聊会话会带拥有者私讯的记忆，画室不能有）：
    # 它只需要「画什么、回覆哪则」，都在画室收到的那则讯息里（第 3 轮审查）。
    ("\t\t\ttext: () => {\n\t\t\t\tconst source = this.turns.get(agent.id);\n\t\t\t\t// 追問承諾時",
     "\t\t\ttext: () => {\n\t\t\t\tif (this.studios.has(agent.id)) return '';\n\t\t\t\tconst source = this.turns.get(agent.id);\n\t\t\t\t// 追問承諾時"),
    # 工具送讯照「发送来源」归帐：工具一开始执行就记下哪个会话、为哪则讯息在跑（第 3 轮审查）。
    ("\n\tsourceForTurn(agent) {",
     "\n\t// 工具执行一开始的来源：哪个会话、为哪则讯息在跑。这次执行里送出的讯息照它归帐（turn-guard.mjs），\n"
     "\t// 不看送出时轮到谁。\n"
     "\tsendOrigin(exec) {\n\t\tconst source = this.sourceForTurn(exec?.agent);\n"
     "\t\treturn {\n\t\t\tsessionId: exec?.agent?.id,\n\t\t\tchannelId: source?.channelId,\n\t\t\tmessageId: source?.messageId,\n\t\t};\n\t}\n"
     "\n\tsourceForTurn(agent) {"),
    ("exec.name !== 'remember' && exec.name !== 'krita_batch'",
     "exec.name !== 'remember' && exec.name !== 'draw_request'"),
    ("agent.ctx.systemPrompt.context({ name: 'krita-works', order: 951, interpolate: false, required: true, text: () => kritaGuideFor(this, agent) });",
     "agent.ctx.systemPrompt.context({ name: 'krita-works', order: 951, interpolate: false, required: true, text: () => this.studios.has(agent.id) ? kritaGuideFor(this, agent) : (kritaGuideFor(this, agent) ? DRAW_GUIDE + studioRecentNote(this.studio, this.turns.get(agent.id)?.channelId) + kritaReferenceHint(this.turns.get(agent.id)) : '') });\n"
     # 同一个频道的会话会延续好几天：离上一则群友讯息很久了，就说之前的话题已经过去（2026-11-28：4 天前的闲聊被当成刚刚的话）。
     "\t\tagent.ctx.systemPrompt.context({ name: 'social-recency', order: 949, interpolate: false, required: true, text: () => this.studios.has(agent.id) ? '' : socialRecencyNote(agent.session?.snapshotEvents?.() ?? [], this.turns.get(agent.id)) });"),
    # 不把换气（huanqi）的提示从画室拿掉：拿掉以后那一轮会变成完全空白，换气额度 0 时它处理空白回合会在
    # 会话事件里同步写入，宿主当成重入、整个 dsh 停掉（2026-11-28 沙盒实测）。改由画室提示词说明那是给聊天用的，
    # 没画完由 runStudioJob 自己接着画。
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
    # 请求那则被删了：让 Discord 拒绝（fail_if_not_exists），图不会掉成一般群讯（第 3 轮审查）。
    ("message_id: source.messageId,\n\t\t\t\t\t\t\t\t\t\tfail_if_not_exists: false,",
     "message_id: source.messageId,\n\t\t\t\t\t\t\t\t\t\tfail_if_not_exists: true,"),
    # 这则请求 Krita 实际碰过的作品编号（闸道收下的才算，闸道只让请求者在原会话动自己的作品）：
    # 画室记成这个频道画的，之后续改要核对 source_job 是这个频道的作品（部署前审查）。
    ("\t\t\toriginals: new Set(),\n\t\t\tredraws: new Set(),\n",
     "\t\t\toriginals: new Set(),\n\t\t\tredraws: new Set(),\n\t\t\tworks: new Set(),\n"),
    ("\t\t\t\t\tif (!result.ok)\n\t\t\t\t\t\treturn refusal(result.message || 'Krita 這次沒有做成。');\n",
     "\t\t\t\t\tif (!result.ok)\n\t\t\t\t\t\treturn refusal(result.message || 'Krita 這次沒有做成。');\n"
     "\t\t\t\t\tfor (const work of [result.data?.job, result.job, args.job])\n"
     "\t\t\t\t\t\tif (/^work-[0-9a-f]{24}$/.test(work ?? '')) turn.works.add(work);\n"),
]
# 每个工具都包一层送讯来源（apply 里的 ctx.tools.register 全部换掉）。
APPLY_START = "export async function apply(ctx, config = {}) {"
APPLY_RUNTIME = ("\truntime = new SocialRuntime(ctx, config, engine);\n",
                 "\truntime = new SocialRuntime(ctx, config, engine);\n"
                 "\t// 每个工具都包一层：执行一开始记下来源，之后送出的讯息照来源归帐。\n"
                 "\tconst registerTool = (tool) =>\n\t\tctx.tools.register(\n"
                 "\t\t\tsocialSendOriginTool(tool, (exec) => runtime.sendOrigin(exec)),\n\t\t);\n")
KRITA_EXPORT = '''
// 画室用：这则请求 Krita 实际回覆了几张（正式交付、预览）。
export function kritaDeliveries(runtime, messageId) {
	const turn = deliveries.get(runtime)?.get(String(messageId));
	if (!turn) return { final: 0, previews: 0, works: [] };
	const final = [...turn.receipts.values()].filter((receipt) => receipt?.delivery === 'sent').length;
	return { final, previews: turn.previewsSent ?? 0, works: [...(turn.works ?? [])] };
}
'''

# ---- dsh-huanqi/lib/index.js（换气 1.1.0）：空白回合的熔断不能在会话事件里同步写会话 ----
# 换气在 turn/end 的 session/event 里补提示（inject／steer）。宿主正在发布那一笔事件，inject／steer 要写 inbox
# （agent/inbox/spliced），宿主拒绝重入（「session append cannot reenter while another append is being published」）。
# 熔断那段包在 async 的 IIFE 里、没有 catch：同步丢的错变成没人接的 rejection，Node 直接结束整个 dsh
# （2026-11-28 沙盒：换气额度 0、画室一轮完全空白）。续跑那段的额度警告也同样被拒（宿主接住、只记一行）。
# 改法：这些写入都等这一轮整个结束（whenIdle）再做，写失败只记日志；两个 IIFE 都接住错误。
# 额度、判词、只 steer 一次（landPrompted）这些行为都不变。
HUANQI_AFTER_TURN = ("""        }).catch((error) => {
            ctx.logger.warn(`dsh-loop-guard: could not resume after a break: ${String(error)}`);
        });
    }
""", """        }).catch((error) => {
            ctx.logger.warn(`dsh-loop-guard: could not resume after a break: ${String(error)}`);
        });
    }
    /* 会话事件（turn/end）里不能同步写会话：宿主正在发布这一笔，inject／steer 写 inbox 会被拒绝重入。
     * 等这一轮整个结束再写；写失败只记日志，不能丢出去（丢到 async 里没人接，整个程序会结束）。 */
    function afterTurn(agent, write, what) {
        const run = () => {
            try {
                write();
            }
            catch (error) {
                ctx.logger.warn(`dsh-huanqi: ${what} failed: ${String(error)}`);
            }
        };
        const whenIdle = agent.whenIdle;
        if (typeof whenIdle !== 'function') {
            setTimeout(run, 0);
            return;
        }
        void Promise.resolve().then(() => whenIdle.call(agent)).then(run, (error) => {
            ctx.logger.warn(`dsh-huanqi: ${what} skipped: ${String(error)}`);
        });
    }
""")
HUANQI_CONTINUE = ("""                    scheduleResume(agent, notice, `huanqi: empty-turn continuation, credit ${left}/${ledger.creditMax}`);
                })();
                if (left <= config.warnAtCredit) {
                    agent.inject(message(strings.creditWarning(left, ledger.creditMax, config.normalTurnThreshold), 'notice', `huanqi: credit low (${left}/${ledger.creditMax})`));
                }
""", """                    scheduleResume(agent, notice, `huanqi: empty-turn continuation, credit ${left}/${ledger.creditMax}`);
                })().catch((error) => ctx.logger.warn(`dsh-huanqi: empty-turn continuation failed: ${String(error)}`));
                if (left <= config.warnAtCredit) {
                    afterTurn(agent, () => agent.inject(message(strings.creditWarning(left, ledger.creditMax, config.normalTurnThreshold), 'notice', `huanqi: credit low (${left}/${ledger.creditMax})`)), 'credit warning');
                }
""")
HUANQI_BREAK = ("""                    agent.inject(message(fullNote === null ? strings.circuitBreakNotice(ledger.creditMax) : fullNote, 'notice', 'huanqi: circuit break (empty turn)'));
                })();
""", """                    afterTurn(agent, () => agent.inject(message(fullNote === null ? strings.circuitBreakNotice(ledger.creditMax) : fullNote, 'notice', 'huanqi: circuit break (empty turn)')), 'circuit-break notice');
                })().catch((error) => ctx.logger.warn(`dsh-huanqi: circuit break failed: ${String(error)}`));
""")
HUANQI_LAND = ("""                    agent.steer(message(strings.circuitBreakNotice(ledger.creditMax), 'notice', 'huanqi: land your state, then ask the user to compact'));
                }
            }
        }));
""", """                    afterTurn(agent, () => agent.steer(message(strings.circuitBreakNotice(ledger.creditMax), 'notice', 'huanqi: land your state, then ask the user to compact')), 'land-your-state steer');
                }
            }
        }));
""")


def port_huanqi(content):
    if "function afterTurn(agent, write, what)" in content:
        raise ValueError("这份 release 的 dsh-huanqi 已经移植过。")
    for anchor, replacement in (HUANQI_AFTER_TURN, HUANQI_CONTINUE, HUANQI_BREAK, HUANQI_LAND):
        content = sp.once(content, anchor, replacement)
    return content


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
                                LEASE_DECLARE, LEASE_GUARD, LEASE_TAKE, LEASE_END, STUDIO_STOP, HISTORY_GUIDE, STUDIO_DELETE,
                                DELIVER_CHECK, SENT_REPLY, SENT_CONTROL, SENT_EDIT_CONTROL):
        content = once(content, anchor, replacement)
    content = once(content, *REVOKED_TURN)
    content = once(content, SUBSCRIBE_ANCHOR, sp.SUBSCRIBE + STUDIO_WIRING + SUBSCRIBE_ANCHOR)
    content = once(content, OLD_MESSAGE_FAILED, sp.MESSAGE_FAILED)
    content = once(content, OLD_EDIT_FAILED, sp.EDIT_FAILED)
    # 保留现行 release 自己的补丁（gatewayScopes、history-view、drain、承诺、Krita）。
    for kept in ("this.gatewayScopes.turn(agent,channelId,messageId)", "this.gatewayScopes.historyCanDeliver(agent,messageId)",
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
    start = content.index(APPLY_START)
    head, body = content[:start], content[start:]
    if body.count("ctx.tools.register(") < 5:
        raise ValueError("现行 release 的 apply 注册工具的写法变了，须重新审阅。")
    body = sp.once(body.replace("ctx.tools.register(", "registerTool("), *APPLY_RUNTIME)
    return head + body


def port_history(content):
    if "replayText(" in content:
        raise ValueError("这份 release 的 history-view.mjs 已经移植过。")
    for anchor, replacement in (HISTORY_HELPERS, HISTORY_AGE, HISTORY_TEXT, HISTORY_REPLIES, HISTORY_PUSH, HISTORY_SENT):
        content = sp.once(content, anchor, replacement)
    return content


def port_scopes(content):
    if "at:Date.parse(row.timestamp)" in content:
        raise ValueError("这份 release 的 gateway-scopes.mjs 已经移植过。")
    return sp.once(sp.once(content, *SCOPES_SOURCE), *SCOPES_GONE)


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
    history_path, scopes_path = plugin / "history-view.mjs", plugin / "gateway-scopes.mjs"
    bridge_path, runtime_path, krita_path = runtime / "bridge.mjs", runtime / "index.mjs", runtime / "krita-tools.mjs"
    huanqi_path = args.release / "dsh-huanqi/lib/index.js"
    read = lambda path: path.read_text(encoding="utf-8").replace("\r\n", "\n")
    source = lambda name: (args.repo / name).read_bytes() if args.worktree else git(args.repo, "show", f"{args.rev}:{name}")
    index = port_index(read(index_path))
    develop_bridge = git(args.repo, "show", f"{args.base}:deploy/social/bridge.mjs").decode("utf-8")
    bridge = port_bridge(read(bridge_path), develop_bridge)
    social = port_runtime(read(runtime_path))
    krita = port_krita(read(krita_path))
    history = port_history(read(history_path))
    scopes = port_scopes(read(scopes_path))
    huanqi = port_huanqi(read(huanqi_path))
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
        (history_path, encode(history)),
        (scopes_path, encode(scopes)),
        (huanqi_path, encode(huanqi)),
        (runtime / "turn-guard.mjs", source("deploy/social/turn-guard.mjs")),
        (runtime / "studio.mjs", source("deploy/social/studio.mjs")),
    ], replace=replace)
    print("已移植：index.js、natural-participation.mjs、history-view.mjs、gateway-scopes.mjs、bridge.mjs、index.mjs、krita-tools.mjs、dsh-huanqi/lib/index.js，新增 turn-guard.mjs、studio.mjs")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print("移植中止，没有写入任何档案：" + str(error), file=sys.stderr)
        sys.exit(1)
