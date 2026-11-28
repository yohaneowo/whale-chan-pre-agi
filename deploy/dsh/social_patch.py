"""固定版桥接的群聊／私人会话接线；每个接点都要求唯一匹配。"""


def once(content, anchor, replacement):
    if content.count(anchor) != 1:
        raise ValueError("桥接接口已经变动，须重新审阅。")
    return content.replace(anchor, replacement, 1)




# ---- 一轮的期限、取消与归帐（2026-11-28「正在输入」不停）----
# 这些片段同时给 social_patch（从上游重建）与 port_typing_fix（移植到 VM 现行 release）用，
# 两边接上的程式码一模一样。
TURN_IMPORT = 'import { runSocialTurn, awaitSocialTurn, socialFollowup, socialPromptSeq, socialBounded, trackSocialSends, onSocialTurn, errorTrace, stopSocialTurns, socialTyping, socialReply, socialNoteGone, TURN_DEADLINE_NOTICE } from "dsh-social-runtime/bridge";\n'

# 「正在输入」从 runSocialTurn 亮起，期限（预设 280 秒）从这里算，任何情况都熄灯；
# 卡住的附件、flush 不会再拖住灯与频道队列。
ENQUEUE = ('await this.runPrompt(channelId, messageId, text, beginTyping, attachments)',
           'await runSocialTurn({ channelId, messageId, beginTyping, queuedAt: socialQueuedAt, log: (line) => this.log("info", line) }, (socialBudget) => this.runPrompt(channelId, messageId, text, socialBudget.beginTyping, attachments, socialBudget))')
# 进队列的时间：之后来的 /stop 连排队中的也停掉。
ENQUEUE_AT = ('async enqueuePrompt(channelId, messageId, text, beginTyping, attachments) {',
              'async enqueuePrompt(channelId, messageId, text, beginTyping, attachments) {\n\t\tconst socialQueuedAt = Date.now();')
# /stop：这个频道正在跑与排队中的讯息都停（不再送进模型、不再送答）。
COMMAND_STOP = ('commandStop(channelId) {', 'commandStop(channelId) {\n\t\tstopSocialTurns(channelId);')
# 「正在输入」：被限流时不在 REST 里重送，熄灯后连送到一半的也取消。
TYPING = ('''const beginTypingFor = (channelId) => {
\t\t\tlet timer;
\t\t\tconst fire = () => {
\t\t\t\trest.triggerTyping(channelId).catch(() => {});
\t\t\t};
\t\t\tfire();
\t\t\ttimer = setInterval(fire, Math.max(config.typingIntervalMs, 3e3));
\t\t\treturn () => {
\t\t\t\tif (timer !== void 0) clearInterval(timer);
\t\t\t\ttimer = void 0;
\t\t\t};
\t\t};''', 'const beginTypingFor = (channelId) => socialTyping(rest, channelId, config.typingIntervalMs);')
# 附件下载失败的日志不写错误内文（可能带私讯附件的档名或网址）。
INCOMING_FAILED = ('this.log("warn", `incoming attachment failed: ${String(error)}`);',
                   'this.log("warn", `incoming attachment failed #${messageId}：${errorTrace(error)}`);')
SIGNATURE = ('async runPrompt(channelId, messageId, text, beginTyping, attachments) {',
             'async runPrompt(channelId, messageId, text, beginTyping, attachments, socialBudget) {')
SAVE_SIGNATURE = ('async saveIncoming(agentCwd, messageId, attachments) {',
                  'async saveIncoming(agentCwd, messageId, attachments, incomingSignal) {')
FETCH = ('const response = await fetch(attachment.url);',
         'const response = await fetch(attachment.url, { signal: incomingSignal ? AbortSignal.any([incomingSignal, AbortSignal.timeout(3e4)]) : AbortSignal.timeout(3e4) });')
ATTACH = ('const notes = await this.saveIncoming(agentCwd, messageId, attachments);',
          'const notes = await socialBounded(this.saveIncoming(agentCwd, messageId, attachments, socialBudget?.signal), socialBudget?.signal, []);')
# 送进 inbox 的那则讯息留下来：它的 id 是这一轮的识别码（自动续做时 discordMessageId 会重复）。
FOLLOWUP = ('agent.followup(this.userMessage([{',
            'if (socialBudget?.signal.aborted) return { chunks: [TURN_DEADLINE_NOTICE], files: [] };\nconst socialPrompt = socialFollowup(agent, this.userMessage([{')
WAIT = '''const socialWait = await awaitSocialTurn(agent, socialPrompt, { signal: socialBudget?.signal, startedAt: socialBudget?.startedAt });
this.log("info", `回合结束 #${messageId}：${socialWait.status}，${Math.round(socialWait.elapsedMs / 1000)} 秒`);
if (socialWait.timedOut) return { chunks: [TURN_DEADLINE_NOTICE], files: [] };'''
FLUSH = ('if (flushable !== void 0) await flushable.flush(agent.session);',
         'if (flushable !== void 0) await socialBounded(flushable.flush(agent.session), socialBudget?.signal);')
PROMPT_SEQ = ('const promptSeq = findPromptSeq(events, messageId);',
              'const promptSeq = socialPromptSeq(events, socialPrompt);')
# 日志只写讯息 ID 与 errorTrace（错误类别与堆疊位置），不写错误内文。
PROMPT_FAILED = ('this.log("warn", `prompt failed: ${String(error)}`);',
                 'this.log("warn", `prompt failed #${messageId}：${errorTrace(error)}`);')
# 所有回覆都用严格的讯息参照（fail_if_not_exists: true）：送答前的核对过了、POST 等待或重试的期间原讯息才被删，
# Discord 会拒绝，回答不会掉成一则没回覆任何人的群讯（Sol 部署前审查）。被拒（Unknown Message）时 request
# 交给 rest.onGone，桥接当成对方撤回：不送、不补「卡住了」。附件（贴图 GIF）没跟着文字时也挂同一个参照。
STRICT_REPLY = ("message_id: replyToMessageId,\n\t\t\t\tfail_if_not_exists: false",
                "message_id: replyToMessageId,\n\t\t\t\tfail_if_not_exists: true")
FILES_REPLY = ('async createMessageWithFiles(channelId, content, files) {\n\t\tconst form = new FormData();\n\t\tform.append("payload_json", JSON.stringify({\n\t\t\tcontent,\n',
               'async createMessageWithFiles(channelId, content, files, replyToMessageId) {\n\t\tconst form = new FormData();\n\t\tform.append("payload_json", JSON.stringify({\n\t\t\tcontent,\n'
               '\t\t\t...replyToMessageId === void 0 ? {} : { message_reference: { message_id: replyToMessageId, fail_if_not_exists: true } },\n')
REST_GONE = ("throw new Error(`discord REST ${method} ${path} failed: ${String(response.status)} ${text.slice(0, 300)}`);",
             "const socialError = new Error(`discord REST ${method} ${path} failed: ${String(response.status)} ${text.slice(0, 300)}`);\n"
             "\t\t\t\tsocialNoteGone(this, path, body, form, socialError);\n\t\t\t\tthrow socialError;")
# 送讯：每一段凭 naturalTicket（这则呼叫的这一轮）记帐，送出失败也记。
# 每次成功送出就记一笔：文字送出就算答覆，后面的贴图附件失败只记日志，不再补「卡住了」。
# 她选的回覆对象不在了就改回覆叫她的那则（socialReply）；贴图跟着文字送就不挂参照，只有贴图时挂同一个参照。
def group_send(commitment=''):
    return ('''let sent;
if (prepared.text.trim()) {
  sent = await socialReply((replyTo) => rest.createMessage(message.channel_id, prepared.text, replyTo), target.messageId, message.guild_id === void 0 ? void 0 : message.id);
  participation.recordReply(message.channel_id, target.text, sent.id, participation.replyPartner(target, message.author.id), target.messageId, index === 0, naturalTicket);
''' + commitment + '''}
if (prepared.files.length) try {
  const withFiles = await (sent ? rest.createMessageWithFiles(message.channel_id, "", prepared.files)
    : socialReply((replyTo) => rest.createMessageWithFiles(message.channel_id, "", prepared.files, replyTo), target.messageId, message.guild_id === void 0 ? void 0 : message.id));
  if (!sent && withFiles) participation.recordReply(message.channel_id, target.text, withFiles.id, participation.replyPartner(target, message.author.id), target.messageId, index === 0, naturalTicket);
} catch (error) {
  if (!sent) throw error;
  logger.warn(`discord-bridge: sticker upload failed #${message.id}：${errorTrace(error)}`);
}''')
NOTICE_TICKET = ('participation.turnNotice(message.channel_id, chunk)) continue;',
                 'participation.turnNotice(message.channel_id, chunk, naturalTicket)) continue;')
RECORD_TICKET = ('target.messageId, index === 0);', 'target.messageId, index === 0, naturalTicket);')
SEND_FAILED = ('logger.warn(`discord-bridge: send failed: ${String(error)}`);',
               'participation.sendFailed(message.channel_id, naturalTicket);\nlogger.warn(`discord-bridge: send failed #${message.id}：${errorTrace(error)}`);')
FILES = ('await rest.createMessageWithFiles(message.channel_id, "", reply.files);',
         'participation.recordFiles(message.channel_id, naturalTicket, await rest.createMessageWithFiles(message.channel_id, "", reply.files));')
FILES_FAILED = ('logger.warn(`discord-bridge: file upload failed: ${String(error)}`);',
                'participation.sendFailed(message.channel_id, naturalTicket);\nlogger.warn(`discord-bridge: file upload failed #${message.id}：${errorTrace(error)}`);')
# 附件送不出去时的那句提示：讯息已经撤回（或这则被停掉）就不说。
FILES_NOTICE = ('await rest.createMessage(message.channel_id, "⚠️ 附件上传失败,文件仍在服务器上。").catch(() => {});',
                'if (participation.canSend(message.channel_id, naturalTicket)) await rest.createMessage(message.channel_id, "⚠️ 附件上传失败,文件仍在服务器上。").catch(() => {});')
HANDLING_FAILED = ('logger.warn(`discord-bridge: message handling failed: ${String(error)}`);',
                   'logger.warn(`discord-bridge: message handling failed #${message.id}：${errorTrace(error)}`);')
# 工具（discord_send_message、Krita）送到频道的讯息记到正在跑的那则呼叫名下。
TRACKED_REST = ('registerDiscordRest(rest);',
                'registerDiscordRest(trackSocialSends(rest, (channelId, messageId) => participation.recordToolMessage(channelId, messageId)));')
# 回覆的那则在送出时已经不在（严格参照被拒）：群聊行为处理把那则呼叫记成撤回（不送、不补话）。
GONE_WIRING = 'rest.onGone = (channelId, messageId) => participation.messageGone(channelId, messageId);\n'
# turn-guard 的回合事件交给群聊行为处理（目前不做事，保留接点）。
SUBSCRIBE = 'onSocialTurn((event) => participation.turnEvent(event));\n'
MESSAGE_FAILED = '.catch((error) => logger.warn(`群聊行为处理未完成，保持安静 #${message.id}：${errorTrace(error)}`))'
EDIT_FAILED = '.catch((error) => logger.warn(`编辑讯息处理未完成，保持安静 #${message.id}：${errorTrace(error)}`))'
# 一轮的整段生命周期（runPrompt 本体与 deliverMessage 送讯）共用的接点，上游与现行 release 都有。
LIFECYCLE = [ENQUEUE, ENQUEUE_AT, SIGNATURE, SAVE_SIGNATURE, FETCH, ATTACH, FLUSH, PROMPT_SEQ, PROMPT_FAILED,
             SEND_FAILED, FILES, FILES_FAILED, FILES_NOTICE, HANDLING_FAILED, COMMAND_STOP, TYPING, INCOMING_FAILED,
             STRICT_REPLY, FILES_REPLY, REST_GONE]


def social_patch(content):
    content = 'import { admitSocialMessage, prepareSocialTurn, handleSocialControl, assertGroupSession, isSocialOwner, toGroupText, redactSecrets, registerDiscordRest, socialGuildOwner, socialChannelLabel, socialConsolidate, socialImageNotes, prepareGroupReply, registerDiscordGateway, socialPresence, socialArchive, extractSocialReply } from "dsh-social-runtime/bridge";\n' + TURN_IMPORT + content
    # 群聊：文字照常回覆（繁体、遮机密），[sticker:…] 换成的表情 GIF 另发一则附件；私讯只遮机密。
    # 群聊里桥接自己的回合提示（失败、取消、没有文字）不发出去，交给群聊行为处理（失败时她自己说一句）。
    # 「回复」由她自己选：回话开头写 [回覆 #編號] 才挂到那一则，没写就是一般讯息（以前一律挂在叫她的那则）。
    # 她这句在跟谁说话（之后的续聊对象）也跟着她回覆或 @ 的人走。
    content = once(content, 'const sent = await rest.createMessage(message.channel_id, chunk, index === 0 ? message.id : void 0);', '''if (message.guild_id !== void 0 && participation.turnNotice(message.channel_id, chunk, naturalTicket)) continue;
const target = message.guild_id === void 0 ? { text: chunk } : participation.replyTarget(message.channel_id, chunk);
const prepared = message.guild_id === void 0 ? { text: redactSecrets(chunk), files: [] } : await prepareGroupReply(target.text);
''' + group_send())
    content = once(content, 'participation.recordReply(message.channel_id, chunk, sent.id, message.author.id);', '')
    content = once(content, 'return allowedUsers.length === 0 || allowedUsers.includes(message.author.id);', 'return true;')
    # 群聊自动开新会话时不发「(已自动新建会话 …)」：那是给管理用的讯息，群友不需要看到。
    content = once(content, 'const preamble = created && title !== void 0 ?', 'const preamble = this.config.preset !== "whale-social" && created && title !== void 0 ?')
    # 等这一轮（含 huanqi 补救轮）结束：绑这次送进 inbox 的讯息 id；回合已完整回答就不等补救轮、
    # 顺手停掉它；到期限只取消属于自己的那一轮，回传固定提示，交给群聊行为处理说一句。
    content = once(content, 'await agent.whenIdle();', WAIT)
    for anchor, replacement in LIFECYCLE:
        content = once(content, anchor, replacement)
    content = once(content, 'const reply = extractReply(events, promptSeq);', 'const reply = extractSocialReply(events, promptSeq);')
    # 频道白名单含 "*" 就是所有服务器的所有频道；私讯仍只收拥有者。
    content = once(content, 'if (!allowedChannels.includes(message.channel_id)) return false;', 'if (!allowedChannels.includes("*") && !allowedChannels.includes(message.channel_id)) return false;')
    # 斜线指令的回覆（例如 /new 的「已新建会话」）在群里也转繁体。
    content = once(content, 'const [first, ...others] = reply.chunks;', 'const [first, ...others] = interaction.guild_id === void 0 ? reply.chunks.map((chunk) => redactSecrets(chunk)) : await Promise.all(reply.chunks.map((chunk) => toGroupText(chunk)));')
    # 群聊收图片（存进工作目录 .discord-uploads，她用 read_image 看）；送附件只走 discord_send_message。
    # 桥接「怎么传档案」的提示讲的是 [discord-file:] 送档，群聊用不到，不注入。
    content = once(content, 'if (this.noticed.has(agent)) return;', 'if (this.config.preset === "whale-social" || this.noticed.has(agent)) return;')
    content = once(content, 'maxUploadBytes: config.maxUploadBytes,\n\t\t\t\t\tuploadRoots: config.uploadRoots,\n\t\t\t\t\tmaxIncomingBytes: config.maxIncomingBytes', 'maxUploadBytes: 0,\n\t\t\t\t\tuploadRoots: [],\n\t\t\t\t\tmaxIncomingBytes: Math.min(config.maxIncomingBytes, 8e6)')
    content = once(content, 'const composition = await this.composeSetup(storedPreset);', 'const composition = await this.composeSetup(this.config.preset === "whale-social" ? this.config.preset : storedPreset);')
    content = once(content, 'return service.currentSelection();', 'return { ...service.currentSelection(), ...(this.config.preset === "whale-social" ? { reasoningEffort: "off" } : {}) };')
    content = once(content, '...logged.reasoningEffort === void 0 ? {} : { reasoningEffort: logged.reasoningEffort }', '...logged.reasoningEffort === void 0 ? {} : { reasoningEffort: logged.reasoningEffort }, ...(bridge.config.preset === "whale-social" ? { reasoningEffort: "off" } : {})')
    # 群聊回话长短靠人设控制；上限只在她写长指令、拆帧分析时用得到（1024 曾让这类回合失败）。
    content = once(content, 'return {\n\t\t\tprovider,\n\t\t\tmodel\n\t\t};', 'return { provider, model, maxTokens: this.config.preset === "whale-social" ? 4096 : 2048, ...(this.config.preset === "whale-social" ? { reasoningEffort: "off" } : {}) };')
    # 讯息被编辑：闸道转给群聊行为处理（MESSAGE_CONTENT intent 已开，编辑事件带得到新内容）。
    content = once(content, 'case "INTERACTION_CREATE": {', '''case "MESSAGE_UPDATE": {
\t\t\t\tconst data = payload.d;
\t\t\t\tif (typeof data.id === "string" && typeof data.channel_id === "string") this.options.hooks.onMessageUpdate?.(data);
\t\t\t\treturn;
\t\t\t}
\t\t\tcase "MESSAGE_DELETE":
\t\t\tcase "MESSAGE_DELETE_BULK": {
\t\t\t\tconst data = payload.d;
\t\t\t\tif (typeof data.channel_id === "string") this.options.hooks.onMessageDelete?.(data);
\t\t\t\treturn;
\t\t\t}
\t\t\tcase "INTERACTION_CREATE": {''')
    content = once(content, 'onMessage,\n\t\t\t\t\tonInteraction,', 'onMessage,\n\t\t\t\t\tonMessageUpdate,\n\t\t\t\t\tonMessageDelete,\n\t\t\t\t\tonInteraction,')
    # 她改的状态（discord_edit_me）：闸道交给群聊插件送 op 3；重新登入时 identify 带上存档的状态。
    content = once(content, 'onReady: (ready) => {', 'onReady: (ready) => {\nregisterDiscordGateway(gateway);')
    content = once(content, 'intents: INTENTS.GUILDS | INTENTS.GUILD_MESSAGES', 'presence: () => socialPresence(),\n\t\t\t\tintents: INTENTS.GUILDS | INTENTS.GUILD_MESSAGES')
    content = once(content, 'intents: this.intents,', '...(this.options.presence?.() ? { presence: this.options.presence() } : {}),\n\t\t\t\tintents: this.intents,')
    content = once(content, FOLLOWUP[0], 'prepareSocialTurn(agent, channelId, messageId);\n' + FOLLOWUP[1])
    content = once(content, 'const agent = await this.acquire(rawId);', 'if (this.config.preset === "whale-social") await assertGroupSession(this.ctx, rawId, this.config.cwd);\nconst agent = await this.acquire(rawId);')
    content = once(content, '.filter((header) => boundIds.has(header.id))', '.filter((header) => boundIds.has(header.id) && header.cwd === this.config.cwd)')
    content = once(content, 'const reply = await bridge.handle(command, message.channel_id, message.id,', 'const reply = await (message.guild_id === void 0 ? ownerBridge : bridge).handle(command, message.channel_id, message.id,')
    content = once(content, 'const reply = await bridge.handle(command, channelId, interaction.id,', 'const reply = await (interaction.guild_id === void 0 ? ownerBridge : bridge).handle(command, channelId, interaction.id,')
    content = once(content, 'const channelId = interaction.channel_id ?? "";', '''const channelId = interaction.channel_id ?? "";
if (!isSocialOwner(author.id)) {
  void rest.ackEphemeral(interaction.id, interaction.token, "管理命令与确认操作只供拥有者使用。").catch(() => {});
  return;
}''')
    content = once(content, 'participation.interrupt(channelId, commandName);', 'if (!admitSocialMessage({ ...admission, id: interaction.id, content: "/" + commandName })) return;\nparticipation.interrupt(channelId, commandName);')
    start = content.index('const participation = new NaturalParticipation({')
    end = content.index('relay = new QuestionRelay({', start)
    # 工具送讯记帐：包住交给 runtime 的 REST（桥接自己送讯仍用原本的 rest，另外凭 ticket 记）。
    wrapper = TRACKED_REST[1] + '''
const ownerBridge = new SessionBridge({
  ctx, host, store,
  config: { cwd: join(dirname(config.cwd), "owner"), preset: "whale-owner", titlePrefix: "[私人] ", maxChunksPerReply: config.maxChunksPerReply, maxUploadBytes: config.maxUploadBytes, uploadRoots: [], maxIncomingBytes: config.maxIncomingBytes },
  log: (level, text) => logger[level](text),
});
const participation = new NaturalParticipation({
  ctx,
  stateFile: join(dirname(resolveStateFile(config.stateFile, ctx.baseUrl)), "participation-state.json"),
  ownerIds: config.allowedUsers,
  warn: (text) => logger.warn(text),
  log: (text) => logger.info(text),
  guildOwner: (guildId) => socialGuildOwner(guildId),
  channelLabel: (channelId) => socialChannelLabel(channelId),
  consolidate: (lines, place, options) => socialConsolidate(lines, place, options),
  seenImages: (message) => socialImageNotes(message),
  fetchRecent: (channelId, before) => rest.request("GET", `/channels/${channelId}/messages?limit=20&before=${before}`),
});
''' + GONE_WIRING + SUBSCRIBE + '''const onMessage = (message) => {
  // 原文帐本收全部：群聊、私讯、她自己发的，都在过滤之前记下。
  socialArchive("message", message);
  if (!isAllowed(message, botUserId, config.allowedUsers, config.allowedChannels)) return;
  if (!admitSocialMessage(message)) return;
  const controlReply = async (text) => {
    const sent = await rest.createMessage(message.channel_id, message.guild_id === void 0 ? redactSecrets(text) : await toGroupText(text), message.id);
    participation.recordReply(message.channel_id, text, sent.id);
  };
  void (async () => {
    if (await handleSocialControl(message, controlReply)) return;
    await participation.receive(message, { botUserId, botName: botLabel },
      (text, ticket) => deliverMessage({ ...message, content: text, attachments: [...(message.attachments ?? []), ...(message.referenced_message?.attachments ?? [])] }, ticket), controlReply);
  })()''' + MESSAGE_FAILED + ''';
};
const onMessageDelete = (data) => socialArchive("delete", data);
const onMessageUpdate = (message) => {
  if (typeof message.content === "string" && message.author) socialArchive("edit", message);
  if (message.guild_id === void 0 || !message.author || typeof message.content !== "string") return;
  if (!isAllowed(message, botUserId, config.allowedUsers, config.allowedChannels)) return;
  if (!admitSocialMessage(message)) return;
  const controlReply = async (text) => {
    const sent = await rest.createMessage(message.channel_id, await toGroupText(text), message.id);
    participation.recordReply(message.channel_id, text, sent.id);
  };
  // 附件在原讯息时已经收过，编辑时不再下载。
  void participation.receiveEdit(message, { botUserId, botName: botLabel },
    (text, ticket) => deliverMessage({ ...message, content: text, attachments: [] }, ticket), controlReply)
    ''' + EDIT_FAILED + ''';
};
'''
    return content[:start] + wrapper + content[end:]
