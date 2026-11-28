// 重现并验收 2026-11-28「@ 了只显示正在输入、不回话」与第 1、2 轮审查列出的情境，
// 以及背景画室（画图交给另一个会话，聊天不被卡住）与第 3 轮审查的 5 条 High：画室不带私讯内容、
// 每一段等待都有期限、请求被删时图不掉成一般群讯、工具送讯照发送来源归帐、画室在 chat／full 都能用 krita_batch。
// 直接拿 VM release 副本里真正的桥接程式码来跑：index.js 的 enqueuePrompt／runPrompt／saveIncoming／
// commandStop、DiscordRest 与 deliverMessage（送讯回圈），接上同一份副本的 natural-participation.mjs、
// turn-guard.mjs 与 studio.mjs（画室：真的 Studio／connectStudio／draw_request，画室会话是假的）。
// Discord、模型、时钟都是假的，不连网路、不发任何真讯息。公开仓库不收录上游桥接原文，
// 所以要给一份 release 副本（VM 上 web/node_modules 底下的 dsh-plugin-discord 与 dsh-social-runtime）。
//
//   node deploy/dsh/typing-hang-repro.mjs --release <副本根目录> [--only 情境名]
//
// 判定是「每一则 @ 都有正确、及时的回应」：逐则讯息 ID 检查送出的内容、补的那句、先说的那句、
// 几秒内回应、画好的图回覆到原本那则，每则都要有一组「收到／结果」纪录，「正在输入」一定熄灯、
// 不超过期限（同一则讯息的每一轮加起来算），日志不能出现讯息内文。
// 另外几项检查直接用副本里真的 social runtime（index.mjs）、krita-tools.mjs 与 DiscordRest（假 fetch）跑。
// 任何一项不符就以代码 1 结束。
import console from 'node:console';
import { randomUUID } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import process from 'node:process';
import { mock } from 'node:test';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

const { values } = parseArgs({
	options: { release: { type: 'string' }, only: { type: 'string' } },
});
if (!values.release) {
	console.error('需要 --release <副本根目录>。');
	process.exit(2);
}
const root = resolve(values.release);
const plugin = join(root, 'dsh-plugin-discord/lib');
const runtimeDir = join(root, 'dsh-social-runtime');

const START = Date.parse('2026-11-27T23:57:24.665Z'); // 台湾 07:57:24，第一则 @
mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: START });
const { setTimeout: delay } = await import('node:timers/promises');
const realImmediate = () =>
	new Promise((done) => globalThis.setImmediate(done));

// ---- 从原始码取出整段宣告（按括号配对，略过字串、样板字串与注释） ----
function blockEnd(text, open) {
	let depth = 0;
	const stack = [];
	for (let index = open; index < text.length; index++) {
		const char = text[index];
		const mode = stack.at(-1);
		if (mode === '`') {
			if (char === '\\') index++;
			else if (char === '`') stack.pop();
			else if (char === '$' && text[index + 1] === '{') {
				stack.push('${');
				index++;
			}
			continue;
		}
		if (mode === "'" || mode === '"') {
			if (char === '\\') index++;
			else if (char === mode) stack.pop();
			continue;
		}
		if (char === '/' && text[index + 1] === '/') {
			index = text.indexOf('\n', index);
			continue;
		}
		if (char === '/' && text[index + 1] === '*') {
			index = text.indexOf('*/', index) + 1;
			continue;
		}
		if (char === "'" || char === '"' || char === '`') {
			stack.push(char);
			continue;
		}
		if (char === '{') {
			if (mode === '${') stack.push('{');
			else depth++;
		} else if (char === '}') {
			if (mode === '{') stack.pop();
			else if (mode === '${') stack.pop();
			else if (--depth === 0) return index + 1;
		}
	}
	throw new Error('括号对不上');
}
function declaration(text, marker) {
	const start = text.indexOf(marker);
	if (start < 0 || text.indexOf(marker, start + 1) >= 0)
		throw new Error('找不到唯一的宣告：' + marker);
	return text.slice(start, blockEnd(text, text.indexOf('{', start)));
}

const indexText = readFileSync(join(plugin, 'index.js'), 'utf8');
const methods = [
	'async enqueuePrompt(',
	'async saveIncoming(',
	'async runPrompt(',
	'commandStop(channelId) {',
].map((marker) => declaration(indexText, marker));
const restSource = declaration(indexText, 'var DiscordRest = class {');
// 「正在输入」的那段：修正前是一整个函式，修正后是一行 socialTyping。
const typingStart = indexText.indexOf('const beginTypingFor = (channelId) =>');
const typingSource = indexText.slice(
	typingStart,
	indexText[indexText.indexOf('=>', typingStart) + 3] === '{'
		? blockEnd(indexText, indexText.indexOf('{', typingStart)) + 1
		: indexText.indexOf('\n', typingStart),
);
const helpers = ['function findPromptSeq(', 'function sanitizeFilename('].map(
	(marker) => declaration(indexText, marker),
);
const deliverSource = declaration(
	indexText,
	'const deliverMessage = (message, naturalTicket) => {',
);
// 讯息被删：index.js 原本那一行（修正后会通知画室）。
const deleteStart = indexText.indexOf('const onMessageDelete = (data) =>');
const deleteSource = indexText.slice(
	deleteStart,
	indexText.indexOf('\n', deleteStart),
);
const patched = indexText.includes('runSocialTurn');

// turn-guard（修正后才有）；修正前用副本 bridge.mjs 里原本的 settleSocialTurn／extractSocialReply。
let guard = {};
const guardPath = join(runtimeDir, 'turn-guard.mjs');
if (existsSync(guardPath)) guard = await import(pathToFileURL(guardPath).href);
let oldBridge = {};
if (!patched) {
	const bridgeText = readFileSync(join(runtimeDir, 'bridge.mjs'), 'utf8');
	const pieces = [
		"const RESCUE = 'loop-guard';",
		'const sourceOf = (event) => event?.data?.source ?? event?.data?.message?.source;',
		declaration(bridgeText, 'export async function settleSocialTurn(').replace(
			/^export /,
			'',
		),
		declaration(bridgeText, 'export function extractSocialReply(').replace(
			/^export /,
			'',
		),
	];
	oldBridge = new Function(
		'delay',
		'currentRuntime',
		'reconcileKritaReply',
		pieces.join('\n') + '\nreturn { settleSocialTurn, extractSocialReply };',
	)(
		delay,
		() => ({}),
		(runtime, id, text) => text,
	);
}
const { NaturalParticipation, naturalConfig } = await import(
	pathToFileURL(join(plugin, 'natural-participation.mjs')).href
);
const { GatewayScopes } = await import(
	pathToFileURL(join(plugin, 'gateway-scopes.mjs')).href
);
// 背景画室（修正后才有）。
let studioModule;
const studioPath = join(runtimeDir, 'studio.mjs');
if (existsSync(studioPath))
	studioModule = await import(pathToFileURL(studioPath).href);
// 桥接收到「讯息被删」时通知画室（修正后才有），取 bridge.mjs 里的原文，currentRuntime 换成情境自己的。
const bridgeSource = readFileSync(join(runtimeDir, 'bridge.mjs'), 'utf8');
const deletedSource = bridgeSource.includes(
	'export function noteSocialStudioDeleted(',
)
	? declaration(
			bridgeSource,
			'export function noteSocialStudioDeleted(',
		).replace(/^export /, '')
	: undefined;

const BOT = '1000000000000000001';
const OWNER = '1000000000000000002';
const FRIEND = '1000000000000000005';
const CHANNEL = '1000000000000000003';
const GUILD = '1000000000000000004';
const SESSION = 'session-00000000-0000-0000-0000-000000000000';
const STUCK = '欸 我剛剛卡住了，你再說一次';
const STEPS = '這一輪我做了太多步，先停一下；要繼續再叫我。';
const GO = '好，我去畫';
const QUEUED = '前面還有一張，畫完就畫你的';
const FINAL = '畫好了 · 作品';
const STUDIO_FAILED = '這張我畫到一半卡住了，先停下來；要再畫再叫我。';
const STUDIO_TIMEOUT = '這張畫太久了，我先停筆；要繼續再叫我。';
const DEADLINE_MS = 280_000;
// 每个情境用自己的一段时间与自己的讯息 ID（真的 Discord ID 不会重复；
// turn-guard 按讯息 ID 记期限、按频道记 /stop，情境之间不能互相影响）。
let sceneStart = START;
let sceneCount = 0;
let nextId = 2000000000000000000n;
const seconds = (at) => Math.round((at - sceneStart) / 100) / 10;

// ---- 假的会话（agent loop）：照 inbox 一则一则开回合，回合内容由情境的 model 决定 ----
class FakeAgent {
	constructor(scene, rest, id = SESSION, model = scene.model) {
		this.scene = scene;
		this.rest = rest;
		this.id = id;
		this.model = model;
		this.events = [];
		this.seq = 0;
		this.queue = [];
		this.rounds = new Map();
		this.cancels = [];
		this.activity = Promise.resolve();
		this.running = undefined;
		this.session = {
			id,
			header: { cwd: '/srv/dsh/discord' },
			snapshotEvents: () => this.events,
		};
		this.inbox = {
			nextTurn: this.queue,
			nextStep: [],
			remove: (id) => {
				const index = this.queue.findIndex((message) => message.id === id);
				if (index < 0) return false;
				this.queue.splice(index, 1);
				return true;
			},
		};
	}
	push(type, data) {
		this.events.push({ seq: ++this.seq, type, time: Date.now(), data });
	}
	followup(message) {
		this.queue.push(message);
		if (this.running) return;
		this.running = true;
		this.activity = (async () => {
			while (this.queue.length) await this.turn(this.queue.shift());
			this.running = undefined;
		})();
	}
	async whenIdle() {
		let activity;
		do await (activity = this.activity);
		while (activity !== this.activity);
	}
	cancel(cause, options = {}) {
		this.cancels.push({
			reason: cause?.reason ?? cause?.kind,
			keepInbox: Boolean(options.keepInbox),
		});
		if (!options.keepInbox) this.queue.length = 0;
		this.controller?.abort();
	}
	async turn(message) {
		const controller = (this.controller = new globalThis.AbortController());
		const signal = controller.signal;
		this.push('turn/start', {});
		this.push('user/message', message);
		const id = message.source?.discordMessageId;
		const round = (this.rounds.get(id) ?? 0) + 1;
		if (message.source?.kind === 'user') this.rounds.set(id, round);
		const wait = (ms) =>
			new Promise((done, fail) => {
				const handle = globalThis.setTimeout(done, ms);
				signal.addEventListener(
					'abort',
					() => {
						globalThis.clearTimeout(handle);
						fail(new Error('aborted'));
					},
					{ once: true },
				);
			});
		let rescues = 0;
		const tools = {
			wait,
			say: (text) =>
				this.push('assistant/message', {
					message: { content: [{ type: 'text', text }] },
				}),
			tool: (name, args) =>
				this.push('tool/call', { name, arguments: JSON.stringify(args) }),
			// 工具送讯（discord_send_message 不挂回复）。after：工具开始以后过多久才真的送出。
			// 修正后 runtime 在工具一开始执行时记下来源（哪个会话、为哪则讯息），这里照做。
			post: (content, { after = 0 } = {}) => {
				const send = async () => {
					if (after) await delay(after);
					return this.rest.tracked.request(
						'POST',
						`/channels/${CHANNEL}/messages`,
						{ content },
					);
				};
				return guard.runWithSocialSendOrigin
					? guard.runWithSocialSendOrigin(
							{ sessionId: this.id, channelId: CHANNEL, messageId: id },
							send,
						)
					: send();
			},
			// Krita 的预览与交付：回覆原本那则请求。
			reply: (content, replyTo) =>
				this.rest.tracked.request('POST', `/channels/${CHANNEL}/messages`, {
					content,
					message_reference: { message_id: replyTo, fail_if_not_exists: false },
				}),
			// 聊天回合交代画图（修正后才有 draw_request）。
			drawRequest: this.rest.drawTool
				? (args) =>
						this.rest.drawTool.execute(
							{ action: 'start', brief: '畫圖', ...args },
							{ agent: this },
						)
				: undefined,
			rescue: (count) => {
				rescues = count;
			},
			fail: () => {
				throw Object.assign(new Error('模型出错'), { failed: true });
			},
			cancelled: () => {
				throw Object.assign(new Error('取消'), { cancelled: true });
			},
		};
		try {
			const kind = message.source?.kind === 'loop-guard' ? 'rescue' : 'user';
			await this.model(
				{
					key: this.keyOf?.(id),
					messageId: id,
					round,
					kind,
					text: message.content?.[0]?.text ?? '',
				},
				tools,
			);
			this.push('turn/end', { reason: { kind: 'completed' } });
		} catch (error) {
			this.push('turn/end', {
				reason:
					signal.aborted || error.cancelled
						? { kind: 'cancelled' }
						: { kind: 'error', error: { message: '模型出错' } },
			});
		}
		for (let index = 0; index < rescues; index++)
			this.queue.push({
				id: randomUUID(),
				role: 'user',
				content: [{ type: 'text', text: '〔换气〕' }],
				source: { kind: 'loop-guard', form: 'notice' },
			});
	}
}

// ---- 一个情境：假 Discord + 真桥接程式码 + 真群聊行为处理 ----
async function play(scene) {
	sceneStart = START + sceneCount++ * 3_600_000;
	mock.timers.setTime(sceneStart);
	const logs = [];
	const posted = [];
	const typing = [];
	const id = () => String(nextId++);
	// 被删掉的讯息：回覆它时，fail_if_not_exists=false 会掉成一般讯息（orphan），否则 Discord 拒绝（400）。
	const deleted = new Set();
	const logger = {
		info: (text) => logs.push({ at: Date.now(), level: 'info', text }),
		warn: (text) => logs.push({ at: Date.now(), level: 'warn', text }),
	};
	const rest = {
		async createMessage(channelId, content, replyTo) {
			await scene.sendDelay?.(content);
			// DiscordRest.createMessage 固定 fail_if_not_exists=false。
			const orphan = Boolean(replyTo && deleted.has(replyTo));
			const sent = {
				id: id(),
				at: Date.now(),
				content,
				replyTo: orphan ? undefined : replyTo,
				orphan,
				via: 'bridge',
			};
			posted.push(sent);
			return sent;
		},
		async createMessageWithFiles(channelId, content, files) {
			await scene.filesFail?.();
			const sent = {
				id: id(),
				at: Date.now(),
				content: `［附件 ${files.length}］`,
				via: 'bridge',
			};
			posted.push(sent);
			return sent;
		},
		async request(method, path, body) {
			if (method === 'POST' && /\/messages$/.test(path)) {
				await scene.toolDelay?.(body?.content ?? '');
				const reference = body?.message_reference;
				const orphan = Boolean(reference && deleted.has(reference.message_id));
				if (orphan && reference.fail_if_not_exists !== false)
					throw new Error(
						`discord REST POST ${path} failed: 400 {"code":10008}`,
					);
				const sent = {
					id: id(),
					at: Date.now(),
					content: body?.content ?? '',
					replyTo: orphan ? undefined : reference?.message_id,
					orphan,
					via: 'tool',
				};
				posted.push(sent);
				return sent;
			}
		},
		triggerTyping: async () => {},
	};
	const directory = mkdtempSync(join(tmpdir(), 'typing-hang-repro-'));
	const participation = new NaturalParticipation({
		ctx: {
			get: (name) =>
				name === 'agentDefaultModel'
					? { currentSelection: () => ({ provider: 'x', model: 'y' }) }
					: {
							async *stream() {
								yield {
									type: 'text-delta',
									text: '{"speak":false,"reason":"x"}',
								};
								yield { type: 'finish', reason: { kind: 'stop' } };
							},
						},
		},
		stateFile: join(directory, 'state.json'),
		ownerIds: [OWNER],
		config: naturalConfig({}),
		warn: logger.warn,
		log: logger.info,
	});
	await participation.ready;
	rest.tracked = guard.trackSocialSends
		? guard.trackSocialSends(rest, (channelId, messageId) =>
				participation.recordToolMessage(channelId, messageId),
			)
		: rest;
	const unsubscribe = guard.onSocialTurn?.((event) =>
		participation.turnEvent(event),
	);
	const agent = new FakeAgent(scene, rest);
	const messages = new Map();
	const keyOf = (messageId) =>
		[...messages].find(([, message]) => message.id === messageId)?.[0];
	agent.keyOf = keyOf;
	// 宿主核对过的本轮来源（runtime.turns／sourceForTurn 的替身）：draw_request 从这里取请求者。
	const sourceOf = (messageId) => {
		const message = [...messages.values()].find((row) => row.id === messageId);
		return (
			message && {
				messageId: message.id,
				channelId: message.channel_id,
				guildId: message.guild_id,
				authorId: message.author.id,
				authorName: message.author.username,
				owner: message.author.id === OWNER,
				text: message.content,
			}
		);
	};
	const runtime = {
		turns: new Map(),
		studios: new Map(),
		sourceForTurn: (current) => {
			const last = current.events.findLast(
				(event) =>
					event.type === 'user/message' && event.data?.source?.kind === 'user',
			);
			return sourceOf(last?.data?.source?.discordMessageId);
		},
		prepare: (current, channelId, messageId) =>
			runtime.turns.set(current.id, sourceOf(messageId)),
	};
	const studioAgents = [];
	let studio;
	if (studioModule) {
		const fakeBridge = {
			config: {
				preset: 'whale-social',
				cwd: '/srv/dsh/discord',
				titlePrefix: '',
			},
			composeSetup: async () => ({ setup: async () => {} }),
			sessionId: (raw) => 'session-studio-' + raw,
			agentOptions: () => ({}),
			userMessage: (content, source) => ({
				id: randomUUID(),
				role: 'user',
				content,
				source,
			}),
			ctx: {
				agents: {
					create: async ({ sessionId }) => {
						const studioAgent = new FakeAgent(
							scene,
							rest,
							sessionId,
							scene.studio ?? (async () => {}),
						);
						studioAgent.keyOf = keyOf;
						studioAgents.push(studioAgent);
						return { agent: studioAgent };
					},
				},
			},
		};
		studio = studioModule.connectStudio({
			runtime,
			bridges: { group: fakeBridge, owner: fakeBridge },
			rest,
			log: logger.info,
			// Krita 实际回覆了几张：看送到原本那则的「畫好了」与「預覽」。
			deliveries: (messageId) => ({
				final: posted.filter(
					(row) => row.replyTo === messageId && row.content.startsWith(FINAL),
				).length,
				previews: posted.filter(
					(row) => row.replyTo === messageId && row.content.startsWith('預覽'),
				).length,
			}),
			...(scene.drawDeadlineMs ? { deadlineMs: scene.drawDeadlineMs } : {}),
		});
		studio.on((event) => participation.drawEvent(event));
		rest.drawTool = studioModule.drawRequestTool(runtime);
	}
	const sources = new Map();
	const scopes = new GatewayScopes({
		socket: null,
		bindings: {
			get: (channel) => (channel === CHANNEL ? SESSION : undefined),
			entries: () => [[CHANNEL, SESSION]],
		},
		stat: async () => null,
		inspect: async () => {},
		request: async (path) => sources.get(path.split('/').at(-1)),
		destination: async () => ({ botId: BOT, guildId: GUILD }),
		principal: async (target, actor) => ({ guildId: GUILD, botId: actor }),
		bot: () => BOT,
		continuity: new Map(),
	});
	scopes.rpc = async () => 'scope';
	// 只看租约是不是还是这一则的（不连 Discord 核对来源）。
	scopes.historyCanDeliver = async (current, event) =>
		scopes.active.get(current)?.row.event === event;

	// 桥接：从 index.js 取出的真方法，挂在一个只有必要栏位的物件上。
	const env = {
		...guard,
		...(guard.foldSocialReply
			? { extractSocialReply: guard.foldSocialReply }
			: {}),
		...oldBridge,
		prepareSocialTurn: async (current, channelId, messageId) => {
			await scene.prepareDelay?.(keyOf(messageId));
			runtime.prepare(current, channelId, messageId);
		},
		stopSocialStudio: (channelId) => studio?.stopChannel(channelId) ?? 0,
		historyView: { note() {} },
		fetch: (url, options) => scene.fetch(url, options),
		mkdir: async () => {},
		writeFile: async () => {},
		join,
		chunkReply: (text) => (text.trim() ? [text.trim()] : []),
		extractFileMarkers: (text) => ({ text, paths: [] }),
	};
	const scope = (values) =>
		new Proxy(values, {
			has: () => true,
			get: (target, key) =>
				key === Symbol.unscopables
					? undefined
					: key in target
						? target[key]
						: globalThis[key],
		});
	const Bridge = new Function(
		'scope',
		`with (scope) { ${helpers.join('\n')}\n return class Bridge { ${methods.join('\n')} } }`,
	)(scope(env));
	const bridge = Object.assign(new Bridge(), {
		channelChains: new Map(),
		config: {
			preset: 'whale-social',
			cwd: '/srv/dsh/discord',
			maxChunksPerReply: 6,
			maxIncomingBytes: 8e6,
			uploadRoots: [],
		},
		log: (level, text) => logger[level === 'warn' ? 'warn' : 'info'](text),
		ctx: {
			get: (name) =>
				name === 'sessions' ? { flush: () => scene.flush?.() } : undefined,
			agents: { get: () => agent },
		},
		gatewayScopes: scopes,
		store: { get: (channel) => (channel === CHANNEL ? SESSION : undefined) },
		sessionId: (raw) => raw,
		ensureChannelAgent: async () => ({ agent, created: false }),
		injectNoticeOnce() {},
		userMessage: (content, source) => ({
			id: randomUUID(),
			role: 'user',
			content,
			source,
		}),
		collectUploads: async () => ({ files: [], problems: [] }),
		async handle(command, channelId, messageId, beginTyping, attachments) {
			if (command.kind === 'stop')
				return { chunks: this.commandStop(channelId), files: [] };
			return await this.enqueuePrompt(
				channelId,
				messageId,
				command.text,
				beginTyping,
				attachments,
			);
		},
	});
	const beginTypingFor = () => {
		const row = { on: Date.now(), off: undefined };
		typing.push(row);
		return () => {
			row.off ??= Date.now();
		};
	};
	const deliverMessage = new Function(
		'scope',
		`with (scope) { ${deliverSource}; return deliverMessage; }`,
	)(
		scope({
			...env,
			isAllowed: () => true,
			botUserId: BOT,
			config: { allowedUsers: [OWNER], allowedChannels: ['*'] },
			emptyContentWarned: new Set(),
			rest,
			parseCommand: (content) =>
				/^\/stop\b/.test(content)
					? { kind: 'stop' }
					: { kind: 'prompt', text: content },
			socialCommitmentFollowup: () => false,
			parseCommitmentReply: (text) => ({ text, drafts: [] }),
			prepareGroupReply: async (text) =>
				(await scene.prepare?.(text)) ?? { text, files: [] },
			redactSecrets: (text) => text,
			toGroupText: async (text) => text,
			recordSocialCommitment: async () => {},
			logger,
			participation,
			bridge,
			ownerBridge: bridge,
			beginTypingFor,
		}),
	);

	const onMessageDelete = deleteSource
		? new Function(
				'scope',
				`with (scope) { ${deleteSource}; return onMessageDelete; }`,
			)(
				scope({
					scopeAuthority: undefined,
					socialArchive: () => {},
					noteSocialStudioDeleted: deletedSource
						? new Function(
								'currentRuntime',
								`${deletedSource}; return noteSocialStudioDeleted;`,
							)(() => runtime)
						: undefined,
				}),
			)
		: () => {};
	for (const item of scene.messages)
		if (item.deleteAt !== undefined)
			globalThis.setTimeout(() => {
				const message = messages.get(item.key);
				if (!message) return;
				deleted.add(message.id);
				onMessageDelete({
					id: message.id,
					channel_id: CHANNEL,
					guild_id: GUILD,
				});
			}, item.deleteAt);

	const handlers = [];
	for (const [index, item] of scene.messages.entries()) {
		globalThis.setTimeout(() => {
			const message = {
				id: id(),
				channel_id: CHANNEL,
				guild_id: GUILD,
				author: item.author ?? { id: OWNER, username: '站長' },
				content: item.content,
				attachments: item.attachments ?? [],
				...(item.extra ?? {}),
				timestamp: new Date().toISOString(),
				mentions: item.content.includes(`<@${BOT}>`) ? [{ id: BOT }] : [],
			};
			messages.set(item.key ?? String(index), message);
			sources.set(message.id, { ...message });
			scopes.receive(message);
			const controlReply = async (text) => {
				const sent = await rest.createMessage(CHANNEL, text, message.id);
				participation.recordReply(CHANNEL, text, sent.id);
			};
			handlers.push(
				participation
					.receive(
						message,
						{ botUserId: BOT, botName: '小鯨魚' },
						(text, ticket) =>
							deliverMessage({ ...message, content: text }, ticket),
						controlReply,
					)
					.catch((error) =>
						logger.warn(
							`群聊行为处理未完成，保持安静 #${message.id}：${guard.errorTrace ? guard.errorTrace(error) : ''}`,
						),
					),
			);
		}, item.at);
	}
	const last = Math.max(...scene.messages.map((item) => item.at));
	const cap = sceneStart + 16 * 60_000;
	let quietSince;
	for (;;) {
		mock.timers.tick(250);
		for (let index = 0; index < 6; index++) await realImmediate();
		const busy =
			Date.now() < sceneStart + last + 1000 ||
			agent.running ||
			studioAgents.some((row) => row.running) ||
			Boolean(studio?.running || studio?.waiting.length) ||
			typing.some((row) => row.off === undefined) ||
			handlers.length < scene.messages.length;
		const settled = await Promise.race([
			Promise.all(handlers).then(() => true),
			realImmediate().then(() => false),
		]);
		if (!busy && settled) quietSince ??= Date.now();
		else quietSince = undefined;
		if ((quietSince && Date.now() - quietSince > 30_000) || Date.now() > cap)
			break;
	}
	unsubscribe?.();
	await participation.saves.catch(() => {});
	rmSync(directory, { recursive: true });
	return { logs, posted, typing, messages, agent, studioAgents };
}

// ---- 判定 ----
function judge(scene, result) {
	const problems = [...(scene.check?.(result) ?? [])];
	const { logs, posted, typing, messages } = result;
	for (const row of typing) {
		if (row.off === undefined)
			problems.push(`「正在输入」从 ${seconds(row.on)}s 起一直没熄`);
		else if (row.off - row.on > DEADLINE_MS + 1000)
			problems.push(
				`「正在输入」亮了 ${Math.round((row.off - row.on) / 1000)} 秒，超过期限`,
			);
	}
	if (scene.typingMax) {
		const total = typing.reduce(
			(sum, row) => sum + ((row.off ?? Date.now()) - row.on),
			0,
		);
		if (total > scene.typingMax)
			problems.push(
				`「正在输入」一共亮了 ${Math.round(total / 1000)} 秒（应在 ${scene.typingMax / 1000} 秒内）`,
			);
	}
	for (const line of logs)
		if (/PRIVATE/.test(line.text))
			problems.push(
				'日志出现讯息内文：' +
					line.text.slice(0, 40).replace(/PRIVATE\S*/g, '<内文>'),
			);
	for (const [key, expect] of Object.entries(scene.expect)) {
		const message = messages.get(key);
		if (!message) {
			problems.push(`#${key} 没有送出`);
			continue;
		}
		const replies = posted
			.filter((row) => row.replyTo === message.id)
			.map((row) => row.content);
		if (expect.final) {
			const final = posted.find(
				(row) => row.replyTo === message.id && row.content.startsWith(FINAL),
			);
			if (!final) problems.push(`#${key} 画好的图没有回覆到原本那则`);
			else if (final.at - Date.parse(message.timestamp) > expect.final)
				problems.push(
					`#${key} 画好的图太晚（${Math.round((final.at - Date.parse(message.timestamp)) / 1000)} 秒）`,
				);
			if (expect.finalAfter) {
				const other = posted.find(
					(row) =>
						row.replyTo === messages.get(expect.finalAfter)?.id &&
						row.content.startsWith(FINAL),
				);
				if (!other || !final || final.at < other.at)
					problems.push(`#${key} 应该排在 #${expect.finalAfter} 后面才画好`);
			}
		}
		if (expect.lastTypingBy) {
			const lastOff = Math.max(
				0,
				...typing.map((row) => row.off ?? Number.POSITIVE_INFINITY),
			);
			if (lastOff - Date.parse(message.timestamp) > expect.lastTypingBy)
				problems.push(
					`#${key} 的「正在输入」到 ${Math.round((lastOff - Date.parse(message.timestamp)) / 1000)} 秒才熄（同一则讯息的每一轮合起来应在 ${expect.lastTypingBy / 1000} 秒内）`,
				);
		}
		const anyText = posted.map((row) => row.content);
		for (const text of expect.posts ?? [])
			if (!anyText.includes(text)) problems.push(`#${key} 没有送出「${text}」`);
		for (const text of expect.once ?? [])
			if (anyText.filter((row) => row === text).length !== 1)
				problems.push(`#${key}「${text}」应该刚好送一次`);
		for (const text of expect.replies ?? [])
			if (!replies.includes(text)) problems.push(`#${key} 没有回覆「${text}」`);
		for (const text of expect.never ?? [])
			if (replies.includes(text)) problems.push(`#${key} 不该回覆「${text}」`);
		if (expect.prefaceBy) {
			const preface = posted.find(
				(row) => row.replyTo === message.id && row.content === expect.preface,
			);
			if (!preface)
				problems.push(`#${key} 动手前没有先说「${expect.preface}」`);
			else if (preface.at - Date.parse(message.timestamp) > expect.prefaceBy)
				problems.push(
					`#${key} 先说的那句太晚（${Math.round((preface.at - Date.parse(message.timestamp)) / 1000)} 秒）`,
				);
		}
		if (expect.within) {
			const first = posted.find(
				(row) =>
					row.at >= Date.parse(message.timestamp) &&
					(row.replyTo === message.id ||
						(expect.posts ?? []).includes(row.content)),
			);
			if (!first || first.at - Date.parse(message.timestamp) > expect.within)
				problems.push(`#${key} 没有在 ${expect.within / 1000} 秒内回应`);
		}
		const received = logs.filter(
			(line) => line.text === `直接呼叫 收到 #${message.id}`,
		);
		const results = logs.filter((line) =>
			line.text.startsWith(`直接呼叫 结果 #${message.id} `),
		);
		if (received.length !== 1 || results.length !== 1)
			problems.push(
				`#${key} 的「收到／结果」纪录不成对（收到 ${received.length}、结果 ${results.length}）`,
			);
		else if (
			expect.status &&
			!results[0].text.includes(`status=${expect.status} `)
		)
			problems.push(
				`#${key} 结果应为 ${expect.status}：${results[0].text.replace(/^直接呼叫 结果 #\d+ /, '')}`,
			);
	}
	return problems;
}

// 聊天回合要画图：修正后交给画室（draw_request），照工具结果回一句；修正前在聊天回合里自己画
// （Krita 的预览与交付回覆原本那则），画完才回话。
const chatDraw = async (tools, turn, { minutes = 3, silent = false } = {}) => {
	if (tools.drawRequest) {
		const result = await tools.drawRequest({ brief: '畫一張圖 PRIVATE-BRIEF' });
		await tools.wait(1_500);
		if (!silent) tools.say(/前面還有/.test(result.message) ? QUEUED : GO);
		return;
	}
	await tools.wait(4_000);
	tools.tool('krita_batch', { action: 'start', width: 512, height: 512 });
	await tools.wait(30_000);
	await tools.reply('預覽 · 作品', turn.messageId);
	await tools.wait(minutes * 60_000 - 34_000);
	await tools.reply(FINAL, turn.messageId);
	tools.say('畫好了');
};
// 画室会话画一张：实际时间照 VM 上的中位数（约 3 分钟），预览与交付都回覆原本那则。
const studioDraw = async (turn, tools, minutes = 3) => {
	await tools.wait(10_000);
	tools.tool('krita_batch', { action: 'start', width: 512, height: 512 });
	await tools.wait(30_000);
	await tools.reply('預覽 · 作品', turn.messageId);
	await tools.wait(minutes * 60_000 - 40_000);
	await tools.reply(FINAL, turn.messageId);
	tools.say('畫好了');
};
const mention = `<@${BOT}>`;
const friend = { id: FRIEND, username: '小羅' };
const scenes = [
	{
		name: '事故：只 @、问「你怎么了」、再 @',
		messages: [
			{ key: 'A', at: 0, content: mention },
			{ key: 'Q', at: 31_120, content: '你怎麼了 PRIVATE-Q' },
			{ key: 'B', at: 35_379, content: mention },
		],
		// 修正前：空 @ 让她续画半天前的旧图（445 秒，含两轮补救）。
		async model(turn, tools) {
			if (turn.kind === 'rescue')
				return (await tools.wait(60_000), tools.say('這個我不畫啦'));
			if (turn.key === 'Q')
				return (await tools.wait(9_000), tools.say('我沒事啦'));
			if (turn.key === 'A') {
				await tools.wait(4_000);
				tools.tool('krita_batch', { action: 'start' });
				await tools.wait(316_000);
				tools.say('好啦畫好了，脖子拉短了。');
				return tools.rescue(2);
			}
			await tools.wait(9_000);
			tools.say('我在啦');
		},
		expect: {
			A: { replies: ['怎麼了？'], within: 5_000, status: 'bare-sent' },
			Q: { status: 'answered', never: [STUCK] },
			B: { posts: ['我在啦'], never: [STUCK], within: 30_000 },
		},
	},
	{
		name: '画图期间同频道另一则 @ 马上回话；画好回覆原本那则；画图时不亮「正在输入」',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 畫一隻貓 PRIVATE-A` },
			{
				key: 'B',
				at: 20_000,
				content: `${mention} 今天星期幾 PRIVATE-B`,
				author: friend,
			},
		],
		async model(turn, tools) {
			if (turn.key === 'A') return chatDraw(tools, turn);
			await tools.wait(3_000);
			tools.say('今天星期五');
		},
		studio: (turn, tools) => studioDraw(turn, tools),
		typingMax: 30_000,
		expect: {
			A: {
				posts: [GO],
				within: 10_000,
				final: 240_000,
				never: [STUCK],
				status: 'answered',
			},
			B: {
				posts: ['今天星期五'],
				within: 15_000,
				never: [STUCK],
				status: 'answered',
			},
		},
	},
	{
		name: '第二个画图请求排队并告知，前一张画完才画它',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 畫一隻貓 PRIVATE-A` },
			{
				key: 'C',
				at: 10_000,
				content: `${mention} 也幫我畫一隻狗 PRIVATE-C`,
				author: friend,
			},
		],
		async model(turn, tools) {
			return chatDraw(tools, turn);
		},
		studio: (turn, tools) => studioDraw(turn, tools),
		typingMax: 30_000,
		expect: {
			A: { posts: [GO], within: 10_000, final: 240_000, status: 'answered' },
			C: {
				posts: [QUEUED],
				within: 10_000,
				final: 480_000,
				finalAfter: 'A',
				never: [STUCK],
				status: 'answered',
			},
		},
	},
	{
		name: '交给画室后她没说话：替她说「好，我去畫」',
		messages: [{ key: 'A', at: 0, content: `${mention} 畫月亮 PRIVATE-A` }],
		async model(turn, tools) {
			return chatDraw(tools, turn, { silent: true });
		},
		studio: (turn, tools) => studioDraw(turn, tools, 1),
		typingMax: 30_000,
		expect: {
			A: {
				posts: [GO],
				within: 10_000,
				final: 120_000,
				never: [STUCK],
				status: 'draw-ack-sent',
			},
		},
	},
	{
		name: '背景画图失败：回一句简短说明',
		messages: [{ key: 'A', at: 0, content: `${mention} 畫一座城堡 PRIVATE-A` }],
		async model(turn, tools) {
			if (!tools.drawRequest) {
				await tools.wait(60_000);
				return tools.fail();
			}
			return chatDraw(tools, turn);
		},
		async studio(turn, tools) {
			await tools.wait(60_000);
			tools.fail();
		},
		typingMax: 30_000,
		expect: {
			A: {
				posts: [GO],
				replies: [STUDIO_FAILED],
				within: 10_000,
				never: [STUCK],
			},
		},
	},
	{
		name: '背景画图超时：停笔并说明，画室换下一张',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 畫一幅超大的畫 PRIVATE-A` },
			{
				key: 'C',
				at: 5_000,
				content: `${mention} 畫一顆蘋果 PRIVATE-C`,
				author: friend,
			},
		],
		drawDeadlineMs: 300_000,
		async model(turn, tools) {
			return chatDraw(tools, turn, { minutes: turn.key === 'A' ? 12 : 1 });
		},
		async studio(turn, tools) {
			if (turn.key === 'A') return tools.wait(20 * 60_000);
			return studioDraw(turn, tools, 1);
		},
		typingMax: 30_000,
		expect: {
			A: {
				posts: [GO],
				replies: [STUDIO_TIMEOUT],
				within: 10_000,
				never: [STUCK],
			},
			C: { posts: [QUEUED], final: 420_000, never: [STUCK] },
		},
	},
	{
		name: '同频道 A 送讯慢、B 先送达：各记各的',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 問題A PRIVATE-A` },
			{ key: 'B', at: 1_000, content: `${mention} 問題B PRIVATE-B` },
		],
		sendDelay: async (content) => content === '答覆A' && (await delay(10_000)),
		async model(turn, tools) {
			await tools.wait(2_000);
			tools.say(turn.key === 'B' ? '答覆B' : '答覆A');
		},
		expect: {
			A: { posts: ['答覆A'], never: [STUCK], status: 'answered' },
			B: { posts: ['答覆B'], never: [STUCK], status: 'answered' },
		},
	},
	{
		name: 'A 的工具讯息在 B 那轮才送达：不算给 B',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 傳個東西 PRIVATE-A` },
			{ key: 'B', at: 10_000, content: `${mention} 說點什麼 PRIVATE-B` },
		],
		toolDelay: async (content) => content === '工具A' && (await delay(20_000)),
		async model(turn, tools) {
			if (turn.key === 'A') {
				await tools.wait(279_000);
				void tools.post('工具A');
				return tools.wait(60_000);
			}
			await tools.wait(25_000);
		},
		expect: {
			A: { replies: [STEPS], status: 'notice-sent' },
			B: { replies: [STUCK], status: 'fallback-sent' },
		},
	},
	{
		name: 'A 被放弃后才收尾：不清掉 B 的闸道验证',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 問題A PRIVATE-A` },
			{ key: 'B', at: 5_000, content: `${mention} 在嗎 PRIVATE-B` },
		],
		prepareDelay: async (key) => key === 'A' && (await delay(300_000)),
		async model(turn, tools) {
			await tools.wait(turn.key === 'B' ? 20_000 : 1_000);
			tools.say(turn.key === 'B' ? '在喔' : '答覆A');
		},
		expect: {
			A: { replies: [STEPS], never: ['答覆A'], status: 'notice-sent' },
			B: { posts: ['在喔'], never: [STUCK], status: 'answered' },
		},
	},
	{
		name: '单则 @ 模型正常结束但没有正文',
		messages: [{ key: 'A', at: 0, content: `${mention} 說點什麼 PRIVATE-A` }],
		async model(turn, tools) {
			await tools.wait(3_000);
		},
		expect: { A: { replies: [STUCK], status: 'fallback-sent' } },
	},
	{
		name: '单则 @ 回合被取消',
		messages: [{ key: 'A', at: 0, content: `${mention} 幫我看看 PRIVATE-A` }],
		async model(turn, tools) {
			await tools.wait(3_000);
			tools.cancelled();
		},
		expect: { A: { replies: [STUCK], status: 'fallback-sent' } },
	},
	{
		name: '附件下载卡住',
		messages: [
			{
				key: 'A',
				at: 0,
				content: `${mention} 看這張 PRIVATE-A`,
				attachments: [
					{
						id: '1',
						url: 'https://cdn.example/a.png',
						filename: 'a.png',
						size: 10,
						content_type: 'image/png',
					},
				],
			},
		],
		fetch: () => new Promise(() => {}),
		async model(turn, tools) {
			await tools.wait(2_000);
			tools.say('看到了');
		},
		expect: { A: { replies: [STEPS], status: 'notice-sent' } },
	},
	{
		name: '附件下载失败：日志不记附件档名与网址',
		messages: [
			{
				key: 'A',
				at: 0,
				content: `${mention} 看這份 PRIVATE-A`,
				attachments: [
					{
						id: '1',
						url: 'https://cdn.example/PRIVATE-FILE.pdf',
						filename: 'PRIVATE-FILE.pdf',
						size: 10,
						content_type: 'application/pdf',
					},
				],
			},
		],
		fetch: async (url) => {
			throw new TypeError(`fetch failed: ${url}`);
		},
		async model(turn, tools) {
			await tools.wait(2_000);
			tools.say('檔案打不開耶');
		},
		expect: { A: { posts: ['檔案打不開耶'], status: 'answered' } },
	},
	{
		name: '附件下载中收到 /stop：不再送进模型、不再送答',
		messages: [
			{
				key: 'A',
				at: 0,
				content: `${mention} 看這張 PRIVATE-A`,
				attachments: [
					{
						id: '1',
						url: 'https://cdn.example/a.png',
						filename: 'a.png',
						size: 10,
						content_type: 'image/png',
					},
				],
			},
			{ key: 'S', at: 5_000, content: '/stop' },
		],
		fetch: async () => {
			await delay(20_000);
			return new globalThis.Response(new Uint8Array([1, 2, 3]));
		},
		async model(turn, tools) {
			await tools.wait(2_000);
			tools.say('看到了');
		},
		expect: { A: { never: [STUCK, '看到了'], status: 'stopped' } },
		check: ({ posted }) =>
			posted.some((row) => row.content === '看到了')
				? ['/stop 之后还是送了答覆']
				: [],
	},
	{
		name: 'flush 卡住',
		messages: [{ key: 'A', at: 0, content: `${mention} 你好 PRIVATE-A` }],
		flush: () => new Promise(() => {}),
		async model(turn, tools) {
			await tools.wait(3_000);
			tools.say('嗨嗨');
		},
		expect: { A: { posts: ['嗨嗨'], status: 'answered' } },
	},
	{
		name: '说「我查一下」后自动续做（同一个 Discord ID）',
		messages: [{ key: 'A', at: 0, content: `${mention} 幫我查 PRIVATE-A` }],
		async model(turn, tools) {
			if (turn.round === 1)
				return (await tools.wait(2_000), tools.say('我查一下'));
			await tools.wait(5_000);
			tools.say('查到了：明天會下雨');
		},
		expect: {
			A: { once: ['我查一下', '查到了：明天會下雨'], status: 'answered' },
		},
	},
	{
		name: '自动续做不重新计时：同一则讯息的「正在输入」合起来不超过期限',
		messages: [{ key: 'A', at: 0, content: `${mention} 幫我找資料 PRIVATE-A` }],
		async model(turn, tools) {
			if (turn.round === 1)
				return (await tools.wait(200_000), tools.say('我找一下'));
			await tools.wait(200_000);
			tools.say('找到了');
		},
		expect: {
			A: {
				posts: ['我找一下'],
				replies: [STEPS],
				never: ['找到了'],
				lastTypingBy: DEADLINE_MS + 1_000,
				status: 'notice-sent',
			},
		},
	},
	{
		name: '比较久的事：20 秒没动静先说一句',
		messages: [{ key: 'A', at: 0, content: `${mention} 查天氣 PRIVATE-A` }],
		async model(turn, tools) {
			await tools.wait(45_000);
			tools.say('晴天');
		},
		expect: {
			A: {
				preface: '等我一下，我弄一下',
				prefaceBy: 21_000,
				posts: ['晴天'],
				status: 'answered',
			},
		},
	},
	{
		name: '做太久到期限，排在后面的 @ 仍然有回应',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 整理一大堆資料 PRIVATE-A` },
			{ key: 'B', at: 36_000, content: `${mention} 在嗎 PRIVATE-B` },
		],
		async model(turn, tools) {
			if (turn.kind === 'rescue')
				return (await tools.wait(60_000), tools.say('這個我不做啦'));
			if (turn.key === 'A') {
				await tools.wait(445_000);
				tools.say('整理好了');
				return tools.rescue(2);
			}
			await tools.wait(9_000);
			tools.say('在喔');
		},
		check: ({ agent }) =>
			agent.cancels.some(
				(row) => row.reason === 'discord-turn-deadline' && row.keepInbox,
			)
				? []
				: ['到期限没有取消自己的回合（或没有保留 inbox）'],
		expect: {
			A: { replies: [STEPS], status: 'notice-sent' },
			B: { posts: ['在喔'], never: [STUCK], status: 'answered' },
		},
	},
	{
		name: '答完了，后面排着补救轮：不等它，也停掉它',
		messages: [{ key: 'A', at: 0, content: `${mention} 講個笑話 PRIVATE-A` }],
		async model(turn, tools) {
			if (turn.kind === 'rescue')
				return (await tools.wait(60_000), tools.say('這個我不講啦'));
			await tools.wait(20_000);
			tools.say('好啦講完了');
			tools.rescue(2);
		},
		// 补救讯息还在 inbox 就移掉，已经开跑就取消（keepInbox，别人的讯息不动）；补救轮不能有产出。
		check: ({ agent }) =>
			agent.cancels.every(
				(row) => row.reason === 'discord-turn-answered' && row.keepInbox,
			) &&
			!agent.events.some(
				(event) =>
					event.type === 'assistant/message' &&
					JSON.stringify(event.data).includes('這個我不講啦'),
			) &&
			agent.queue.length === 0
				? []
				: ['补救轮没有被停掉（只该停掉自己的补救轮、保留 inbox）'],
		expect: {
			A: { posts: ['好啦講完了'], never: [STUCK], status: 'answered' },
		},
	},
	{
		name: '送讯出错：日志只有错误类别，补一句固定文字',
		messages: [{ key: 'A', at: 0, content: `${mention} 你好 PRIVATE-A` }],
		prepare: async (text) => {
			throw new Error(`送不出去：${text} PRIVATE-ERR`);
		},
		async model(turn, tools) {
			await tools.wait(2_000);
			tools.say('嗨 PRIVATE-REPLY');
		},
		expect: { A: { replies: [STUCK], status: 'fallback-sent' } },
	},
	{
		name: '文字送出了、贴图上传失败：不再补「卡住了」',
		messages: [{ key: 'A', at: 0, content: `${mention} 你好 PRIVATE-A` }],
		prepare: async (text) => ({
			text,
			files: [{ filename: 'sticker.gif', data: new Uint8Array(1) }],
		}),
		filesFail: async () => {
			throw new Error(
				'discord REST POST /channels/x/messages failed: 500 PRIVATE-UP',
			);
		},
		async model(turn, tools) {
			await tools.wait(2_000);
			tools.say('嗨嗨');
		},
		expect: { A: { posts: ['嗨嗨'], never: [STUCK], status: 'answered' } },
	},
	{
		name: '/stop 停掉的不补「卡住了」',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 做很久的事 PRIVATE-A` },
			{ key: 'S', at: 10_000, content: '/stop' },
		],
		async model(turn, tools) {
			await tools.wait(200_000);
			tools.say('做完了');
		},
		expect: { A: { never: [STUCK], status: 'stopped' } },
	},
	{
		name: '不算「只 @」：附 PDF、Discord 贴图、引用抓不到、她一分钟内刚说过话',
		messages: [
			{
				key: 'P',
				at: 0,
				content: mention,
				attachments: [
					{
						id: '1',
						url: 'https://cdn.example/a.pdf',
						filename: 'a.pdf',
						size: 10,
						content_type: 'application/pdf',
					},
				],
			},
			{
				key: 'K',
				at: 30_000,
				content: mention,
				author: friend,
				extra: { sticker_items: [{ id: '9', name: 'wave' }] },
			},
			{
				key: 'R',
				at: 90_000,
				content: mention,
				author: { id: '1000000000000000006', username: '阿明' },
				extra: {
					message_reference: {
						message_id: '1999999999999999999',
						channel_id: CHANNEL,
					},
				},
			},
			{
				key: 'N',
				at: 100_000,
				content: mention,
				author: { id: '1000000000000000007', username: '小美' },
			},
		],
		async model(turn, tools) {
			await tools.wait(2_000);
			tools.say(`收到 ${turn.key}`);
		},
		expect: {
			P: {
				posts: ['收到 P'],
				never: ['怎麼了？', '嗯？怎麼了', '我在，怎麼了？'],
				status: 'answered',
			},
			K: {
				posts: ['收到 K'],
				never: ['怎麼了？', '嗯？怎麼了', '我在，怎麼了？'],
				status: 'answered',
			},
			R: {
				posts: ['收到 R'],
				never: ['怎麼了？', '嗯？怎麼了', '我在，怎麼了？'],
				status: 'answered',
			},
			N: {
				posts: ['收到 N'],
				never: ['怎麼了？', '嗯？怎麼了', '我在，怎麼了？'],
				status: 'answered',
			},
		},
	},
	{
		name: '画室回一句短话时 Discord 一直不回：不卡住画室，下一张照画',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 畫一座城堡 PRIVATE-A` },
			{
				key: 'C',
				at: 5_000,
				content: `${mention} 畫一顆蘋果 PRIVATE-C`,
				author: friend,
			},
		],
		// 「卡住了」那句永远送不出去（不管走桥接的 createMessage 还是 REST）。
		sendDelay: (content) => content === STUDIO_FAILED && new Promise(() => {}),
		toolDelay: (content) => content === STUDIO_FAILED && new Promise(() => {}),
		async model(turn, tools) {
			return chatDraw(tools, turn, { minutes: 1 });
		},
		async studio(turn, tools) {
			if (turn.key === 'A') {
				await tools.wait(60_000);
				return tools.fail();
			}
			return studioDraw(turn, tools, 1);
		},
		typingMax: 30_000,
		check({ logs, messages }) {
			const id = messages.get('A')?.id;
			return logs.some(
				(line) =>
					line.text.startsWith(`畫室 结果 #${id} status=failed `) &&
					line.text.includes('note=timeout'),
			)
				? []
				: ['画室那句短话送不出去时，没有到期限就放弃'];
		},
		expect: {
			A: { posts: [GO], within: 10_000, never: [STUCK] },
			C: { posts: [QUEUED], final: 330_000, never: [STUCK] },
		},
	},
	{
		name: '请求讯息被删了：画室安静停掉，图不贴成一般群讯',
		messages: [
			{
				key: 'A',
				at: 0,
				content: `${mention} 畫一隻貓 PRIVATE-A`,
				deleteAt: 60_000,
			},
		],
		async model(turn, tools) {
			return chatDraw(tools, turn);
		},
		// 最坏的情况：画室会话照样画完，交付时 fail_if_not_exists=false（会掉成一般讯息）。
		studio: (turn, tools) => studioDraw(turn, tools),
		typingMax: 30_000,
		check({ logs, posted, messages }) {
			const problems = [];
			const id = messages.get('A')?.id;
			if (posted.some((row) => row.content.startsWith(FINAL)))
				problems.push('请求被删以后，画好的图还是贴出来了');
			for (const row of posted.filter((row) => row.orphan))
				problems.push(
					`回覆对象被删，「${row.content.slice(0, 12)}」掉成一般群讯`,
				);
			if (
				!logs.some((line) =>
					line.text.startsWith(`畫室 结果 #${id} status=deleted `),
				)
			)
				problems.push('画室没有因为请求被删而停下');
			return problems;
		},
		expect: {
			A: { posts: [GO], within: 10_000, never: [STUCK, STUDIO_FAILED] },
		},
	},
	{
		name: 'A 的工具讯息跨轮才发出、没挂回复：算给 A，不算给 B',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 傳個東西 PRIVATE-A` },
			{ key: 'B', at: 10_000, content: `${mention} 說點什麼 PRIVATE-B` },
		],
		async model(turn, tools) {
			if (turn.key === 'A') {
				await tools.wait(270_000);
				// 工具在 A 那轮开始，拖了 20 秒才真的送出：那时 A 已到期限、B 正在跑。
				void tools.post('工具A', { after: 20_000 });
				return tools.wait(60_000);
			}
			await tools.wait(25_000);
		},
		check({ posted, messages }) {
			const sent = posted.find((row) => row.content === '工具A');
			const b = messages.get('B');
			const bStart = Date.parse(b?.timestamp ?? '');
			return sent && sent.at > bStart + 270_000
				? []
				: ['工具A 应该在 B 那轮才送出（情境本身没有重现跨轮送讯）'];
		},
		expect: {
			A: { replies: [STEPS], status: 'notice-sent' },
			B: { replies: [STUCK], status: 'fallback-sent' },
		},
	},
];

// 「正在输入」被限流：拿副本里真正的 DiscordRest 与 beginTypingFor 跑。第一次 typing 回 429（retry_after 1 秒），
// 0.5 秒时熄灯；熄灯以后不能再送 typing。
async function typingRateLimited() {
	sceneStart = START + sceneCount++ * 3_600_000;
	mock.timers.setTime(sceneStart);
	const posts = [];
	const fetch = async (url, options = {}) => {
		posts.push({ at: Date.now(), url, signal: options.signal });
		if (posts.length === 1)
			return new globalThis.Response(JSON.stringify({ retry_after: 1 }), {
				status: 429,
			});
		return new globalThis.Response(null, { status: 204 });
	};
	const DiscordRest = new Function(
		'scope',
		`with (scope) { return ${restSource.replace(/^var DiscordRest = /, '')} }`,
	)(
		new Proxy(
			{ fetch, API_BASE: 'https://discord.invalid/api/v10' },
			{
				has: () => true,
				get: (target, key) =>
					key === Symbol.unscopables
						? undefined
						: key in target
							? target[key]
							: globalThis[key],
			},
		),
	);
	const rest = new DiscordRest({ token: 'x' });
	const beginTypingFor = new Function(
		'scope',
		`with (scope) { ${typingSource}; return beginTypingFor; }`,
	)(
		new Proxy(
			{ ...guard, rest, config: { typingIntervalMs: 8_000 } },
			{
				has: () => true,
				get: (target, key) =>
					key === Symbol.unscopables
						? undefined
						: key in target
							? target[key]
							: globalThis[key],
			},
		),
	);
	const stop = beginTypingFor(CHANNEL);
	for (let index = 0; index < 4; index++) {
		mock.timers.tick(125);
		for (let round = 0; round < 6; round++) await realImmediate();
	}
	stop();
	const stoppedAt = Date.now();
	for (let index = 0; index < 120; index++) {
		mock.timers.tick(250);
		for (let round = 0; round < 6; round++) await realImmediate();
	}
	const after = posts.filter(
		(row) => row.at > stoppedAt && !row.signal?.aborted,
	);
	return after.length
		? [
				`熄灯后又送了 ${after.length} 次 typing（第一次在熄灯后 ${Math.round((after[0].at - stoppedAt) / 100) / 10} 秒）`,
			]
		: [];
}

// ---- 用副本里真的 social runtime、krita-tools、画室与 DiscordRest 跑的检查（第 3 轮审查） ----
const engine = {
	blank: () => ({ policy: {} }),
	recall: (data) => ({ selected: data.memories.map((m) => ({ m })) }),
};
// 1×1 的 PNG（Krita 交付的替身）。
const PNG =
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';
const work = (n) => 'work-' + String(n).padStart(24, '0');
const sceneOf = (values) =>
	new Proxy(values, {
		has: () => true,
		get: (target, key) =>
			key === Symbol.unscopables
				? undefined
				: key in target
					? target[key]
					: globalThis[key],
	});
async function realRuntime(groupTools) {
	const { SocialRuntime } = await import(
		pathToFileURL(join(runtimeDir, 'index.mjs')).href
	);
	const stateRoot = mkdtempSync(join(tmpdir(), 'typing-hang-runtime-'));
	const runtime = new SocialRuntime(
		{ get: () => undefined },
		{
			stateRoot,
			ownerIds: [OWNER],
			allowedChannels: ['*'],
			groupCwd: '/srv/dsh/discord',
			groupTools,
		},
		engine,
	);
	return {
		runtime,
		cleanup: () => rmSync(stateRoot, { recursive: true, force: true }),
	};
}
// 挂在真 runtime 上的假会话：记下系统提示的每一段、工具清单、inbox 监听；followup 跑 model。
function runtimeAgent(id, { cwd = '/srv/dsh/discord', model } = {}) {
	const agent = {
		id,
		events: [],
		seq: 0,
		contexts: [],
		allowed: undefined,
		listeners: new Map(),
		inbox: { nextTurn: [], nextStep: [], remove: () => false },
		session: { id, header: { cwd }, snapshotEvents: () => agent.events },
		ctx: {
			tools: {
				restrict: ({ allow }) => {
					agent.allowed = allow;
				},
				presentAs: () => {},
			},
			systemPrompt: {
				context: (row) => agent.contexts.push(row),
				section: (row) => agent.contexts.push(row),
			},
			on: (name, listener) => agent.listeners.set(name, listener),
		},
		push(type, data) {
			agent.events.push({ seq: ++agent.seq, type, time: Date.now(), data });
		},
		// 模型看得到的系统提示（每一段都展开）。
		prompt: () =>
			agent.contexts
				.map((row) => (typeof row.text === 'function' ? row.text() : row.text))
				.join('\n'),
		activity: Promise.resolve(),
		followup(message) {
			agent.listeners.get('agent/inbox/inserted')?.({ message });
			agent.activity = (async () => {
				agent.push('turn/start', {});
				agent.push('user/message', message);
				await model?.(agent, message);
				agent.push('turn/end', { reason: { kind: 'completed' } });
			})();
		},
		whenIdle: () => agent.activity,
		cancel() {},
	};
	return agent;
}
const userTurn = (agent, messageId, text = '畫') => {
	agent.push('turn/start', {});
	agent.push('user/message', {
		id: randomUUID(),
		role: 'user',
		content: [{ type: 'text', text }],
		source: {
			kind: 'user',
			discordMessageId: messageId,
			discordChannelId: CHANNEL,
		},
	});
};
const admitted = (runtime, author = { id: OWNER, username: '站長' }) => {
	const messageId = String(nextId++);
	runtime.admit({
		id: messageId,
		channel_id: CHANNEL,
		guild_id: GUILD,
		author,
		content: `${mention} 幫我畫一隻貓`,
	});
	return messageId;
};
async function settle(done, ms = 120_000) {
	for (let elapsed = 0; elapsed < ms && !done(); elapsed += 250) {
		mock.timers.tick(250);
		for (let round = 0; round < 6; round++) await realImmediate();
	}
}
// 假 Discord 的 fetch：被删的讯息照 Discord 的规则（fail_if_not_exists=false 掉成一般讯息，否则 400）。
function fakeDiscord() {
	const created = [];
	const deleted = new Set();
	const fetch = async (url, options = {}) => {
		if (options.method === 'POST' && /\/channels\/\d+\/messages$/.test(url)) {
			const payload =
				options.body instanceof globalThis.FormData
					? JSON.parse(options.body.get('payload_json'))
					: JSON.parse(options.body ?? '{}');
			const reference = payload.message_reference;
			const orphan = Boolean(reference && deleted.has(reference.message_id));
			if (orphan && reference.fail_if_not_exists !== false)
				return new globalThis.Response(
					JSON.stringify({ code: 10008, message: 'Unknown Message' }),
					{ status: 400 },
				);
			const row = {
				id: String(nextId++),
				content: payload.content ?? '',
				replyTo: orphan ? undefined : reference?.message_id,
				orphan,
			};
			created.push(row);
			return new globalThis.Response(JSON.stringify(row), { status: 200 });
		}
		return new globalThis.Response(null, { status: 204 });
	};
	const DiscordRest = new Function(
		'scope',
		`with (scope) { return ${restSource.replace(/^var DiscordRest = /, '')} }`,
	)(sceneOf({ fetch, API_BASE: 'https://discord.invalid/api/v10' }));
	return { rest: new DiscordRest({ token: 'x' }), created, deleted };
}
const studioBridge = (create) => ({
	config: { preset: '', cwd: '/srv/dsh/discord', titlePrefix: '' },
	composeSetup: async () => ({ setup: async () => {} }),
	sessionId: (raw) => 'session-studio-' + raw,
	agentOptions: () => ({}),
	userMessage: (content, source) => ({
		id: randomUUID(),
		role: 'user',
		content,
		source,
	}),
	ctx: { agents: { create } },
});

// 1. 群聊的画室会话不带私讯记忆、拥有者人物卡（正式设定 groupTools=full）。
async function studioPrivacy() {
	const problems = [];
	const { runtime, cleanup } = await realRuntime('full');
	try {
		const SECRETS = ['DM-CANARY-記憶-7731', 'DM-CANARY-人物卡-7732'];
		// 合成的私讯资料：拥有者私讯范围的记忆、拥有者人物卡上「私下」的条目。
		runtime.memory.recallAcross = (scopes) =>
			scopes.some((scope) => scope.startsWith('owner-'))
				? [
						{
							id: 'm-1',
							at: new Date().toISOString(),
							authorId: OWNER,
							authorName: '站長',
							text: SECRETS[0],
							scope: 'owner-' + OWNER,
						},
					]
				: [];
		runtime.notes.context = (people) =>
			people.some(({ id }) => id === OWNER)
				? `你對在場的人知道的事（人物卡）：\n【站長｜${OWNER}】\n- 私下：${SECRETS[1]}`
				: '';
		const messageId = admitted(runtime);
		const chat = runtimeAgent('session-chat');
		userTurn(chat, messageId);
		runtime.prepare(chat, CHANNEL, messageId);
		const chatPrompt = chat.prompt();
		if (!SECRETS.every((secret) => chatPrompt.includes(secret)))
			problems.push('合成的私讯资料没进到群聊会话（检查本身无效）');
		const posted = [];
		runtime.discordRest = {
			request: async (method, path, body) => {
				const row = {
					id: String(nextId++),
					content: body?.content ?? '',
					replyTo: body?.message_reference?.message_id,
				};
				posted.push(row);
				return row;
			},
		};
		let studioAgent;
		const studio = studioModule.connectStudio({
			runtime,
			bridges: {
				group: studioBridge(async ({ sessionId }) => {
					// 最坏的模型：把看到的系统提示与收到的讯息全部贴进交付的那则。
					studioAgent = runtimeAgent(sessionId, {
						model: async (agent, message) => {
							const seen = [
								agent.prompt(),
								...message.content.map((block) => block.text),
							].join('\n');
							await runtime.discordRest.request(
								'POST',
								`/channels/${CHANNEL}/messages`,
								{
									content: `${FINAL}\n${seen}`,
									message_reference: {
										message_id: message.source.discordMessageId,
									},
								},
							);
							agent.push('assistant/message', {
								message: { content: [{ type: 'text', text: '畫好了' }] },
							});
						},
					});
					return { agent: studioAgent };
				}),
			},
			rest: { request: async () => ({ id: '1' }) },
			deliveries: (id) => ({
				final: posted.filter((row) => row.replyTo === id).length,
				previews: 0,
			}),
		});
		const result = await studioModule
			.drawRequestTool(runtime)
			.execute({ action: 'start', brief: '畫一隻橘貓' }, { agent: chat });
		if (!result.ok) problems.push('draw_request 没有收下');
		await settle(() => !studio.running && !studio.waiting.length);
		if (!studioAgent) problems.push('画室会话没有建立');
		else {
			const studioPrompt = studioAgent.prompt();
			for (const secret of SECRETS)
				if (studioPrompt.includes(secret))
					problems.push(`画室会话的上下文出现私讯内容（${secret.slice(10)}）`);
		}
		if (!posted.some((row) => row.replyTo === messageId))
			problems.push('画室没有把图回覆到原本那则（检查本身无效）');
		for (const secret of SECRETS)
			if (posted.some((row) => row.content.includes(secret)))
				problems.push(`画室的输出出现私讯内容（${secret.slice(10)}）`);
	} finally {
		cleanup();
	}
	return problems;
}

// 2. 请求那则被删了：真的 krita_batch 交付与画室补的那句都不掉成一般群讯（真 DiscordRest，假 fetch）。
async function deletedRequest() {
	const problems = [];
	const { runtime, cleanup } = await realRuntime('full');
	try {
		const discord = fakeDiscord();
		runtime.discordRest = discord.rest;
		const messageId = admitted(runtime);
		const studioAgent = runtimeAgent('session-studio-krita');
		runtime.studios.set(studioAgent.id, 'session-chat');
		userTurn(studioAgent, messageId);
		runtime.prepare(studioAgent, CHANNEL, messageId);
		const { kritaTools } = await import(
			pathToFileURL(join(runtimeDir, 'krita-tools.mjs')).href
		);
		let job = 0;
		const krita = kritaTools(runtime, {
			call: async (socket, data) =>
				data.cancel ? {} : { ok: true, png: PNG, job: work(job), revision: 2 },
		}).find((tool) => tool.name === 'krita_batch');
		job = 1;
		await krita.execute(
			{ action: 'deliver', job: work(1) },
			{ agent: studioAgent },
		);
		if (!discord.created.some((row) => row.replyTo === messageId))
			problems.push('Krita 交付没有回覆到原本那则（检查本身无效）');
		const before = discord.created.length;
		discord.deleted.add(messageId);
		job = 2;
		const result = await krita.execute(
			{ action: 'deliver', job: work(2) },
			{ agent: studioAgent },
		);
		if (discord.created.length > before)
			problems.push(
				`请求被删后，Krita 的图贴成了一般群讯（${discord.created.length - before} 则）`,
			);
		if (result.ok) problems.push('请求被删后，Krita 交付仍回报成功');
		// 画室没收到删除通知（例如闸道漏了这个事件）时，补的那句也不能掉成一般群讯。
		const failing = studioBridge(async () => {
			throw new Error('建立失败');
		});
		failing.composeSetup = async () => {
			throw new Error('建立失败');
		};
		const studio = studioModule.connectStudio({
			runtime,
			bridges: { group: failing, owner: failing },
			rest: discord.rest,
			deliveries: () => ({ final: 0, previews: 0 }),
		});
		studio.request({
			channelId: CHANNEL,
			guildId: GUILD,
			messageId,
			authorId: OWNER,
			authorName: '站長',
			chatSessionId: 'session-chat',
			brief: '畫一隻貓',
		});
		await settle(() => !studio.running && !studio.waiting.length);
		if (discord.created.length > before)
			problems.push('请求被删后，画室补的那句贴成了一般群讯');
	} finally {
		cleanup();
	}
	return problems;
}

// 3. 画室会话在 groupTools=chat 与 full 都能用 krita_batch（且只能用它）；聊天会话都不能用。
async function studioToolsByMode() {
	const problems = [];
	for (const mode of ['chat', 'full']) {
		const { runtime, cleanup } = await realRuntime(mode);
		try {
			const studioAgent = runtimeAgent('session-studio-tools');
			const ownerStudio = runtimeAgent('session-studio-owner', {
				cwd: '/srv/dsh/owner',
			});
			const chat = runtimeAgent('session-chat');
			runtime.studios.set(studioAgent.id, chat.id);
			runtime.studios.set(ownerStudio.id, 'session-owner');
			const blocked = (agent, name) =>
				runtime.toolGuard({ agent, name, arguments: {} }) !== undefined;
			if (blocked(studioAgent, 'krita_batch'))
				problems.push(`groupTools=${mode}：群聊画室的 krita_batch 被挡下`);
			if (blocked(ownerStudio, 'krita_batch'))
				problems.push(`groupTools=${mode}：私讯画室的 krita_batch 被挡下`);
			for (const name of ['remember', 'bash', 'discord_send_message'])
				if (!blocked(studioAgent, name))
					problems.push(`groupTools=${mode}：画室会话可以用 ${name}`);
			if (!blocked(chat, 'krita_batch'))
				problems.push(`groupTools=${mode}：聊天会话可以直接用 krita_batch`);
			if (blocked(chat, 'draw_request'))
				problems.push(`groupTools=${mode}：聊天会话不能用 draw_request`);
			runtime.mount(studioAgent);
			runtime.mount(chat);
			if (JSON.stringify(studioAgent.allowed) !== '["krita_batch"]')
				problems.push(
					`groupTools=${mode}：画室会话的工具清单不是只有 krita_batch`,
				);
			if (chat.allowed?.includes('krita_batch'))
				problems.push(`groupTools=${mode}：聊天会话的工具清单有 krita_batch`);
		} finally {
			cleanup();
		}
	}
	return problems;
}

// 4. 工具送讯照发送来源归帐：runtime 注册的每个工具都包了来源；A 的工具拖到 B 那轮才送出仍算 A。
async function sendOriginCheck() {
	if (!guard.socialSendOriginTool)
		return ['turn-guard 没有记下工具送讯的来源，仍照「送出时轮到谁」归帐'];
	const problems = [];
	const indexSource = readFileSync(join(runtimeDir, 'index.mjs'), 'utf8');
	const apply = indexSource.slice(
		indexSource.indexOf('export async function apply('),
	);
	if (
		apply.split('ctx.tools.register(').length !== 2 ||
		!apply.includes(
			'socialSendOriginTool(tool, (exec) => runtime.sendOrigin(exec))',
		)
	)
		problems.push('runtime 有工具没有包上送讯来源');
	const { runtime, cleanup } = await realRuntime('full');
	try {
		const a = admitted(runtime);
		const b = admitted(runtime, friend);
		const chat = runtimeAgent('session-chat');
		userTurn(chat, a);
		runtime.prepare(chat, CHANNEL, a);
		const sent = [];
		const tracked = guard.trackSocialSends(
			{ request: async () => ({ id: String(nextId++) }) },
			(channelId, messageId) => sent.push(messageId),
		);
		const tool = guard.socialSendOriginTool(
			{
				name: 'discord_send_message',
				execute: async () => {
					await delay(20_000);
					return tracked.request('POST', `/channels/${CHANNEL}/messages`, {
						content: '工具A',
					});
				},
			},
			(exec) => runtime.sendOrigin(exec),
		);
		const pending = tool.execute({}, { agent: chat });
		// A 那轮结束，轮到 B（会话事件与桥接此刻在跑的都换成 B），工具才送出。
		chat.push('turn/end', { reason: { kind: 'cancelled' } });
		userTurn(chat, b);
		let finished = false;
		const turn = guard
			.runSocialTurn(
				{ channelId: CHANNEL, messageId: b, beginTyping: () => () => {} },
				async () => {
					await pending;
					return { chunks: [], files: [] };
				},
			)
			.finally(() => {
				finished = true;
			});
		await settle(() => finished, 60_000);
		await turn;
		if (sent.join() !== a)
			problems.push(
				`工具A 记到 ${sent.map((id) => (id === a ? 'A' : id === b ? 'B' : '?')).join('、') || '没有人'}（应该是 A）`,
			);
	} finally {
		cleanup();
	}
	return problems;
}

const runtimeChecks = [
	{
		name: '群聊画室不带私讯记忆与拥有者人物卡（上下文与输出）',
		run: studioPrivacy,
	},
	{
		name: '请求被删：真 krita_batch 交付与画室补话都不掉成一般群讯',
		run: deletedRequest,
	},
	{
		name: '画室会话在 groupTools=chat 与 full 都能用 krita_batch，聊天会话都不能',
		run: studioToolsByMode,
	},
	{
		name: '工具送讯照发送来源归帐（真 runtime 的 sendOrigin）',
		run: sendOriginCheck,
	},
];

console.log(`release 副本：${patched ? '已移植修正' : '未移植（现行版本）'}`);
let failed = 0;
for (const scene of scenes) {
	if (values.only && !scene.name.includes(values.only)) continue;
	const result = await play(scene);
	const problems = judge(scene, result);
	failed += problems.length ? 1 : 0;
	console.log(`\n${problems.length ? '✖' : '✔'} ${scene.name}`);
	for (const row of result.posted)
		console.log(
			`    ${seconds(row.at)}s ${row.via === 'tool' ? '工具' : '送出'}${row.replyTo ? '（回覆）' : ''}：${row.content.replace(/PRIVATE\S*/g, '<内文>')}`,
		);
	for (const row of result.typing)
		console.log(
			`    正在输入：${seconds(row.on)}s → ${row.off === undefined ? '没熄' : seconds(row.off) + 's'}`,
		);
	for (const problem of problems) console.log(`    问题：${problem}`);
	if (process.env.REPRO_DEBUG)
		for (const line of result.logs)
			console.log(`    日志 ${seconds(line.at)}s ${line.level}：${line.text}`);
}
if (!values.only || '「正在输入」被限流'.includes(values.only)) {
	const problems = await typingRateLimited();
	failed += problems.length ? 1 : 0;
	console.log(`
${problems.length ? '✖' : '✔'} 「正在输入」被限流：熄灯后不再重送`);
	for (const problem of problems) console.log(`    问题：${problem}`);
}
for (const check of runtimeChecks) {
	if (values.only && !check.name.includes(values.only)) continue;
	let problems;
	try {
		problems = await check.run();
	} catch (error) {
		problems = [
			`检查本身出错：${String(error?.message ?? error).slice(0, 120)}`,
		];
	}
	failed += problems.length ? 1 : 0;
	console.log(`\n${problems.length ? '✖' : '✔'} ${check.name}`);
	for (const problem of problems) console.log(`    问题：${problem}`);
}
mock.timers.reset();
console.log(
	failed
		? `\n结果：${failed} 个情境失败`
		: '\n结果：全部通过（每一则 @ 都有正确的回应）',
);
process.exit(failed ? 1 : 0);
