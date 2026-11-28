// 重现并验收 2026-11-28「@ 了只显示正在输入、不回话」与第 1 轮审查列出的情境。
// 直接拿 VM release 副本里真正的桥接程式码来跑：index.js 的 enqueuePrompt／runPrompt／saveIncoming
// 与 deliverMessage（送讯回圈），接上同一份副本的 natural-participation.mjs 与 turn-guard.mjs。
// Discord、模型、时钟都是假的，不连网路、不发任何真讯息。公开仓库不收录上游桥接原文，
// 所以要给一份 release 副本（VM 上 web/node_modules 底下的 dsh-plugin-discord 与 dsh-social-runtime）。
//
//   node deploy/dsh/typing-hang-repro.mjs --release <副本根目录> [--only 情境名]
//
// 判定是「每一则 @ 都有正确的回应」：逐则讯息 ID 检查送出的内容、补的那句、先说的那句，
// 每则都要有一组「收到／结果」纪录，「正在输入」一定熄灯且不超过期限，日志不能出现讯息内文。
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
].map((marker) => declaration(indexText, marker));
const helpers = ['function findPromptSeq(', 'function sanitizeFilename('].map(
	(marker) => declaration(indexText, marker),
);
const deliverSource = declaration(
	indexText,
	'const deliverMessage = (message, naturalTicket) => {',
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

const BOT = '1000000000000000001';
const OWNER = '1000000000000000002';
const CHANNEL = '1000000000000000003';
const GUILD = '1000000000000000004';
const SESSION = 'session-00000000-0000-0000-0000-000000000000';
const STUCK = '欸 我剛剛卡住了，你再說一次';
const STEPS = '這一輪我做了太多步，先停一下；要繼續再叫我。';
const DEADLINE_MS = 280_000;
const seconds = (at) => Math.round((at - START) / 100) / 10;

// ---- 假的会话（agent loop）：照 inbox 一则一则开回合，回合内容由情境的 model 决定 ----
class FakeAgent {
	constructor(scene, rest) {
		this.scene = scene;
		this.rest = rest;
		this.id = SESSION;
		this.events = [];
		this.seq = 0;
		this.queue = [];
		this.rounds = new Map();
		this.cancels = [];
		this.activity = Promise.resolve();
		this.running = undefined;
		this.session = {
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
			post: (content) =>
				this.rest.tracked.request('POST', `/channels/${CHANNEL}/messages`, {
					content,
				}),
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
			await this.scene.model(
				{
					key: this.keyOf?.(id),
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
	mock.timers.setTime(START);
	const logs = [];
	const posted = [];
	const typing = [];
	let nextId = 2000000000000000000n;
	const id = () => String(nextId++);
	const logger = {
		info: (text) => logs.push({ at: Date.now(), level: 'info', text }),
		warn: (text) => logs.push({ at: Date.now(), level: 'warn', text }),
	};
	const rest = {
		async createMessage(channelId, content, replyTo) {
			await scene.sendDelay?.(content);
			const sent = {
				id: id(),
				at: Date.now(),
				content,
				replyTo,
				via: 'bridge',
			};
			posted.push(sent);
			return sent;
		},
		async createMessageWithFiles(channelId, content, files) {
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
				const sent = {
					id: id(),
					at: Date.now(),
					content: body?.content ?? '',
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
	agent.keyOf = (messageId) =>
		[...messages].find(([, message]) => message.id === messageId)?.[0];
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
		principal: async () => ({ guildId: GUILD, botId: OWNER }),
		bot: () => BOT,
		continuity: new Map(),
	});
	scopes.rpc = async () => 'scope';
	scopes.historyCanDeliver = async () => true;

	// 桥接：从 index.js 取出的真方法，挂在一个只有必要栏位的物件上。
	const env = {
		...guard,
		...(guard.foldSocialReply
			? { extractSocialReply: guard.foldSocialReply }
			: {}),
		...oldBridge,
		prepareSocialTurn: async () => {},
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
		},
		gatewayScopes: scopes,
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
			if (command.kind === 'stop') {
				agent.cancel({ kind: 'user' });
				return { chunks: ['已停止目前的回合。'], files: [] };
			}
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

	const handlers = [];
	for (const [index, item] of scene.messages.entries()) {
		globalThis.setTimeout(() => {
			const message = {
				id: id(),
				channel_id: CHANNEL,
				guild_id: GUILD,
				author: { id: OWNER, username: '站長' },
				content: item.content,
				attachments: item.attachments ?? [],
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
	const cap = START + 16 * 60_000;
	let quietSince;
	for (;;) {
		mock.timers.tick(250);
		for (let index = 0; index < 6; index++) await realImmediate();
		const busy =
			Date.now() < START + last + 1000 ||
			agent.running ||
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
	return { logs, posted, typing, messages, agent };
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

const draw = async (
	tools,
	{ tool = 4_000, total = 100_000, preview = 30_000 } = {},
) => {
	await tools.wait(tool);
	tools.tool('krita_batch', { action: 'start', width: 512, height: 512 });
	await tools.wait(preview - tool);
	await tools.post('預覽 · 作品');
	await tools.wait(total - preview);
};
const mention = `<@${BOT}>`;
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
				await draw(tools, { total: 320_000 });
				tools.say('好啦畫好了，脖子拉短了。');
				return tools.rescue(2);
			}
			await tools.wait(9_000);
			tools.say('我在啦');
		},
		expect: {
			A: { replies: ['怎麼了？'], within: 5_000, status: 'bare-sent' },
			Q: { status: 'answered', never: [STUCK] },
			B: { never: [STUCK] },
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
		name: '要画图：动手前先说一句',
		messages: [{ key: 'A', at: 0, content: `${mention} 畫一隻貓 PRIVATE-A` }],
		async model(turn, tools) {
			await draw(tools, { total: 100_000 });
			tools.say('畫好了');
		},
		expect: {
			A: {
				preface: '好，我畫一下',
				prefaceBy: 6_000,
				posts: ['畫好了', '預覽 · 作品'],
				never: [STUCK],
				status: 'answered',
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
		name: '画太久到期限，排在后面的 @ 仍然有回应',
		messages: [
			{ key: 'A', at: 0, content: `${mention} 畫一張大圖 PRIVATE-A` },
			{ key: 'B', at: 36_000, content: `${mention} 在嗎 PRIVATE-B` },
		],
		async model(turn, tools) {
			if (turn.kind === 'rescue')
				return (await tools.wait(60_000), tools.say('這個我不畫啦'));
			if (turn.key === 'A') {
				await draw(tools, { total: 445_000 });
				tools.say('畫好了');
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
			A: {
				preface: '好，我畫一下',
				prefaceBy: 6_000,
				replies: [STEPS],
				status: 'notice-sent',
			},
			B: { posts: ['在喔'], never: [STUCK], status: 'answered' },
		},
	},
	{
		name: '画好了，后面排着补救轮：不等它，也停掉它',
		messages: [{ key: 'A', at: 0, content: `${mention} 畫隻狗 PRIVATE-A` }],
		async model(turn, tools) {
			if (turn.kind === 'rescue')
				return (await tools.wait(60_000), tools.say('這個我不畫啦'));
			await draw(tools, { total: 60_000 });
			tools.say('畫好了，給你');
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
					JSON.stringify(event.data).includes('這個我不畫啦'),
			) &&
			agent.queue.length === 0
				? []
				: ['补救轮没有被停掉（只该停掉自己的补救轮、保留 inbox）'],
		expect: {
			A: { posts: ['畫好了，給你'], never: [STUCK], status: 'answered' },
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
mock.timers.reset();
console.log(
	failed
		? `\n结果：${failed} 个情境失败`
		: '\n结果：全部通过（每一则 @ 都有正确的回应）',
);
process.exit(failed ? 1 : 0);
