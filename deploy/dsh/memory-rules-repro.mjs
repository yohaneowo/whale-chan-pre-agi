// 重现「翻旧事」的问题（小鲸鱼记忆第一阶段）：直接拿 VM release 副本里真正的 social runtime（index.mjs、memory.mjs、
// notes.mjs、archive.mjs、chat-tools.mjs）、真的 fish-memory 召回引擎、Discord 外挂真的 gateway-scopes（闸道附上的
// 「本频道可核对的记忆」），用假资料、假 Discord 跑。判定看「实际送进模型的内容」：系统提示每一段展开、闸道附上的记忆，
// 再加上最坏的模型（每轮都拿暗号去叫 chat_search、chat_open）拿到的工具输出；不看函式的回传值。
//
//   node deploy/dsh/memory-rules-repro.mjs --release <副本根目录> [--only 情境名]
//
// 副本根目录底下要有 dsh-social-runtime、dsh-plugin-discord、dsh-fish-memory（VM 上 web/node_modules 底下那几个）。
// 现行版本（没移植）应该大多失败；移植后（port_memory_rules.py）全部通过，代码 0。
import { spawnSync } from 'node:child_process';
import console from 'node:console';
import {
	mkdirSync,
	mkdtempSync,
	readFileSync,
	readdirSync,
	rmSync,
	writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import process from 'node:process';
import { setTimeout } from 'node:timers';
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
const home = mkdtempSync(join(tmpdir(), 'memory-rules-home-'));
// fish-memory 的设定档与诊断日志放在 DSH_HOME 底下（载入时就决定），先指到暂存目录。
process.env.DSH_HOME = home;
const load = (path) => import(pathToFileURL(join(root, path)).href);
const engine = await load('dsh-fish-memory/lib/index.js');
const { SocialRuntime } = await load('dsh-social-runtime/index.mjs');
const { ArchiveStore } = await load('dsh-social-runtime/archive.mjs');
const { chatTools } = await load('dsh-social-runtime/chat-tools.mjs');
const { GatewayScopes } = await load(
	'dsh-plugin-discord/lib/gateway-scopes.mjs',
);

const DAY = 86_400_000;
const NOW = Date.now();
const GUILD = '1100000000000000001';
const BOT = '1100000000000000002';
const OWNER = '1100000000000000003';
const FRIEND = '1100000000000000004';
const FRIEND2 = '1100000000000000005';
const GENERAL = '1200000000000000001';
const ROOM_A = '1200000000000000002';
const ROOM_B = '1200000000000000003';
const SECRET_ROOM = '1200000000000000004';
const DM = '1200000000000000005';
const VIEW = 1n << 10n;
const HISTORY = 1n << 16n;
const ROLE = '1300000000000000001';
const ROLE_ROOM = '1200000000000000006';
const MEMBER_ROOM = '1200000000000000007';
const FORUM = '1200000000000000008';
const FORUM_POST = '1200000000000000009';
// 频道与权限（每个世界各一份，情境里可以改：收紧权限、论坛改成不公开）。
const CHANNELS = [
	{ id: GENERAL, name: 'general' },
	{ id: ROOM_A, name: 'a-room' },
	{ id: ROOM_B, name: 'b-room' },
	// 不公开的频道：@everyone 看不到。
	{ id: SECRET_ROOM, name: 'secret-room', hidden: true },
	// @everyone 看得到，但有一个角色被拒绝检视（那个角色的人看不到）：不算公开。
	{
		id: ROLE_ROOM,
		name: 'role-room',
		overwrites: [{ id: ROLE, type: 0, allow: '0', deny: String(VIEW) }],
	},
	// @everyone 看得到，但有一个人被拒绝检视：不算公开。
	{
		id: MEMBER_ROOM,
		name: 'member-room',
		overwrites: [{ id: FRIEND2, type: 1, allow: '0', deny: String(VIEW) }],
	},
	// 论坛（公开），底下一则贴文（讨论串，跟着论坛）。
	{ id: FORUM, name: 'forum-x', type: 15 },
];
// 回忆守门员（假的，照题目回答；真模型的准确度另外用 recall-gate 实测）：目标原文 → 判断。
const GATE = {
	真的是玩壞了這個肥魚: { recall: false },
	今天好熱喔: { recall: false },
	早安: { recall: false },
	'上次說的暗號是什麼？': { recall: true, explicit: true, queries: ['暗號'] },
	'b-room 那邊說的暗號是什麼？': {
		recall: true,
		explicit: true,
		queries: ['暗號'],
	},
	'上次 a-room 說的暗號是什麼？': {
		recall: true,
		explicit: true,
		queries: ['暗號'],
	},
	'上次那個包裹到了嗎？': { recall: true, explicit: true, queries: ['包裹'] },
	'上次說的潛水課你還記得嗎？': {
		recall: true,
		explicit: true,
		queries: ['潛水課'],
	},
	'那次潛水看到海龜是在哪裡？': {
		recall: true,
		explicit: true,
		queries: ['海龜'],
	},
	'潛水看到海龜那次是在哪裡？': {
		recall: true,
		explicit: true,
		queries: ['海龜'],
	},
	'早安 @某人': { recall: false },
	'那個搞定了沒？': { recall: true, explicit: true, queries: ['重灌'] },
	'role-room 那邊說的暗號是什麼？': {
		recall: true,
		explicit: true,
		queries: ['暗號'],
	},
	'member-room 那邊說的暗號是什麼？': {
		recall: true,
		explicit: true,
		queries: ['暗號'],
	},
	'論壇那篇說的暗號是什麼？': {
		recall: true,
		explicit: true,
		queries: ['暗號'],
	},
	'上次說的口令是什麼？': { recall: true, explicit: true, queries: ['口令'] },
};
const NAMES = {
	[OWNER]: '站長',
	[FRIEND]: '群友甲',
	[FRIEND2]: '群友乙',
	[BOT]: '鯨魚娘',
};
let sequence = 0;
const snowflake = (at) =>
	((BigInt(at - 1420070400000) << 22n) + BigInt(++sequence)).toString();
// 台北时间的「月/日」（她看到的日期就是这个样子）。
const stamp = (at) => {
	const part = Object.fromEntries(
		new Intl.DateTimeFormat('en-US', {
			timeZone: 'Asia/Taipei',
			month: '2-digit',
			day: '2-digit',
		})
			.formatToParts(new Date(at))
			.map(({ type, value }) => [type, value]),
	);
	return `${part.month}/${part.day}`;
};

// 一个情境的世界：假 Discord（讯息、频道、权限）、原文帐本、真的 social runtime。
function world() {
	const stateRoot = mkdtempSync(join(tmpdir(), 'memory-rules-runtime-'));
	const messages = new Map();
	const warns = [];
	const services = {};
	const runtime = new SocialRuntime(
		{
			get: (name) => services[name],
			logger: { warn: (text) => warns.push(text), info() {} },
		},
		{
			stateRoot,
			ownerIds: [OWNER],
			allowedChannels: ['*'],
			groupCwd: '/srv/dsh/discord',
		},
		engine,
	);
	runtime.archive = new ArchiveStore(join(stateRoot, 'archive'));
	const channels = CHANNELS.map((row) => ({ ...row }));
	const api = { down: false };
	const channel = (id) => {
		const row = channels.find((entry) => entry.id === id);
		if (id === FORUM_POST)
			return {
				id,
				guild_id: GUILD,
				type: 11,
				parent_id: FORUM,
				name: 'forum-post',
			};
		return row
			? {
					id,
					guild_id: GUILD,
					type: row.type ?? 0,
					name: row.name,
					permission_overwrites: [
						...(row.hidden
							? [{ id: GUILD, type: 0, allow: '0', deny: String(VIEW) }]
							: []),
						...(row.overwrites ?? []),
					],
				}
			: id === DM
				? { id, type: 1 }
				: undefined;
	};
	runtime.discordRest = {
		request: async (method, path) => {
			if (api.down) throw new Error('Discord API 500');
			if (path === `/guilds/${GUILD}`)
				return {
					id: GUILD,
					owner_id: OWNER,
					roles: [
						{ id: GUILD, permissions: String(VIEW | HISTORY | (1n << 11n)) },
						{ id: ROLE, permissions: '0' },
					],
				};
			if (path === `/guilds/${GUILD}/channels`)
				return channels.map((row) => channel(row.id));
			let match = /^\/channels\/(\d+)\/messages\/(\d+)$/.exec(path);
			if (match) {
				const row = messages.get(match[2]);
				if (!row || row.channel_id !== match[1])
					throw new Error('404 {"code": 10008}');
				return row;
			}
			match = /^\/channels\/(\d+)$/.exec(path);
			if (match) return channel(match[1]);
			throw new Error('404');
		},
	};
	const say = (channelId, author, content, at = NOW) => {
		const message = {
			id: snowflake(at),
			channel_id: channelId,
			...(channelId === DM ? {} : { guild_id: GUILD }),
			author: { id: author, username: NAMES[author], bot: author === BOT },
			content,
			timestamp: new Date(at).toISOString(),
		};
		messages.set(message.id, message);
		runtime.archive.record('message', message);
		return message;
	};
	// 模型：回忆守门员照 GATE 回答（记下被问过的原文）；背景整理交给情境给的 consolidator。
	const gateAsked = [];
	const llmCalls = [];
	const model = { consolidator: undefined };
	services.agentDefaultModel = {
		currentSelection: () => ({
			provider: 'opencode-go',
			model: 'deepseek-v4.1-flash',
		}),
	};
	services.llm = {
		async *stream(options) {
			llmCalls.push(options);
			const prompt = options.messages?.[0]?.content?.[0]?.text ?? '';
			if (String(options.system).includes('回憶守門員')) {
				const target =
					/<untrusted_target_[0-9a-f]+>\n([\s\S]*?)\n<\/untrusted_target/.exec(
						prompt,
					)?.[1] ?? '';
				gateAsked.push({ target, prompt });
				const verdict = GATE[target.replace(/^\d{1,2}月/, '')] ?? {
					recall: false,
					unknown: true,
				};
				yield {
					type: 'text-delta',
					text: JSON.stringify({
						reason: '假的',
						explicit: false,
						queries: [],
						...verdict,
					}),
				};
				yield { type: 'finish', reason: { kind: 'stop' } };
				return;
			}
			if (!model.consolidator)
				throw new Error('这个情境没有设定背景整理的模型');
			const text = await model.consolidator(options);
			yield { type: 'text-delta', text };
			yield { type: 'finish', reason: { kind: 'stop' } };
		},
	};
	// 平常的群聊（几百则）：让「多常见」的权重跟正式帐本差不多（门槛是照正式帐本定的）。
	const subjects = [
		'我',
		'你',
		'他',
		'大家',
		'站長',
		'我朋友',
		'我室友',
		'我同事',
	];
	const verbs = [
		'剛吃完',
		'正在看',
		'明天要去',
		'昨天玩了',
		'想買',
		'不想寫',
		'在追',
		'終於做完',
	];
	const objects = [
		'拉麵',
		'新番',
		'報告',
		'遊戲',
		'電影',
		'作業',
		'咖啡',
		'火鍋',
		'鍵盤',
		'耳機',
		'小說',
		'演唱會',
	];
	for (let index = 0; index < 240; index++)
		say(
			[GENERAL, ROOM_A, ROOM_B][index % 3],
			[OWNER, FRIEND, FRIEND2][index % 3],
			`${subjects[index % 8]}${verbs[(index >> 3) % 8]}${objects[index % 12]}，${['好累', '笑死', '超讚', '還行', '有夠爛'][index % 5]}`,
			NOW - (30 + (index % 120)) * 3600_000 * 2,
		);
	return {
		runtime,
		stateRoot,
		messages,
		warns,
		services,
		say,
		channels,
		api,
		gateAsked,
		llmCalls,
		model,
		cleanup: () => rmSync(stateRoot, { recursive: true, force: true }),
	};
}

// 挂在真 runtime 上的假会话：记下系统提示的每一段（section 与 context 都展开，就是送进模型的系统部分）。
function fakeAgent(id, cwd) {
	const contexts = [];
	const agent = {
		id,
		events: [],
		session: { id, header: { cwd }, snapshotEvents: () => agent.events },
		ctx: {
			on() {},
			tools: { restrict() {}, presentAs() {} },
			systemPrompt: {
				section: (row) => contexts.push(row),
				context: (row) => contexts.push(row),
			},
		},
		prompt: () =>
			contexts
				.map((row) => {
					try {
						return typeof row.text === 'function' ? row.text() : row.text;
					} catch {
						return '';
					}
				})
				.filter(Boolean)
				.join('\n'),
	};
	return agent;
}

// 一轮：有人在某个地方说了一句话。回传这一轮送进模型的全部内容（系统提示 + 闸道附上的记忆 + 最坏模型拿到的工具输出）。
async function turn(w, { channelId, author, text, probes = [], openIds = [] }) {
	const message = w.say(channelId, author, text);
	w.runtime.admit(message);
	// 频道可见性是背景载入的（收到讯息时开始）；这里等它载完再组提示（正式环境里下一轮一定载完了）。
	await w.runtime.visibility?.warm(GUILD);
	await w.runtime.visibility?.warmChannel?.(channelId);
	const dm = channelId === DM;
	const agent = fakeAgent(
		'session-' + message.id,
		dm ? '/srv/dsh/owner' : '/srv/dsh/discord',
	);
	agent.events.push({
		seq: 1,
		type: 'user/message',
		data: {
			message: {
				role: 'user',
				content: [{ type: 'text', text }],
				source: {
					kind: 'user',
					discordMessageId: message.id,
					discordChannelId: channelId,
				},
			},
		},
	});
	// prepare 可能要等回忆守门员（新版回传 promise）。
	await w.runtime.prepare(agent, channelId, message.id);
	const prompt = agent.prompt();
	// 闸道（history-view）附上的「本頻道有原始來源可核對的記憶」：用 Discord 外挂真的 GatewayScopes。
	const scopes = new GatewayScopes({
		socket: '',
		bindings: new Map([[channelId, agent.id]]),
		continuity: new Map(),
		bot: () => BOT,
		request: (path) => w.runtime.discordRest.request('GET', path),
		principal: async (target, actor) => ({ guildId: GUILD, botId: actor }),
		destination: async () => ({ botId: BOT, guildId: dm ? null : GUILD }),
	});
	const proxy = Object.create(w.runtime, {
		heartbeatSessions: { value: { redact: (value) => value } },
	});
	const hints = await scopes.historyMemories(
		{
			channelId,
			guildId: dm ? null : GUILD,
			ownerId: dm ? OWNER : undefined,
			sessionId: agent.id,
			botId: BOT,
		},
		undefined,
		proxy,
	);
	// 最坏的模型：每一轮都拿暗号去翻纪录、打开那几则。
	const [search, open] = chatTools(w.runtime);
	const tools = [];
	for (const query of probes)
		tools.push((await search.execute({ query }, { agent })).message ?? '');
	for (const id of openIds)
		tools.push(
			(await open.execute({ message_id: id, before: 0, after: 0 }, { agent }))
				.message ?? '',
		);
	const memory = hints.length
		? '本頻道有原始來源可核對的記憶\n' + hints.map((row) => row.text).join('\n')
		: '';
	return {
		prompt,
		memory,
		tools,
		input: [prompt, memory, ...tools].join('\n'),
		agent,
	};
}

const lineOf = (text, needle) =>
	text.split('\n').find((line) => line.includes(needle)) ?? '';
const results = [];
async function scenario(name, run) {
	if (values.only && !name.includes(values.only)) return;
	const problems = [];
	try {
		await run(problems);
	} catch (error) {
		problems.push(
			'情境本身出错：' +
				String(error?.stack ?? error)
					.split('\n')
					.slice(0, 3)
					.join(' | '),
		);
	}
	results.push({ name, problems });
	console.log(`${problems.length ? '✗' : '✓'} ${name}`);
	for (const problem of problems) console.log('    - ' + problem);
}

// 1. 今天 #general 的事故：四天前同频道的「送货」，跟现在的闲聊放在一起。
await scenario('閒聊不翻四天前同頻道的「送貨」（含只 @）', async (problems) => {
	const w = world();
	try {
		const old = w.say(
			GENERAL,
			FRIEND,
			'明天下雨還要送貨，包裹好重',
			NOW - 4 * DAY,
		);
		// 三条路径都放一份：remember 工具记的原话（闸道会附上）、背景整理记成「事实」的笔记、人物卡上的条目。
		w.runtime.memory.remember(
			'group',
			{
				authorId: FRIEND,
				authorName: '群友甲',
				messageId: old.id,
				channelId: GENERAL,
				guildId: GUILD,
			},
			{ text: '明天下雨還要送貨', kind: 'event', importance: 0.5 },
			NOW - 4 * DAY,
		);
		w.runtime.memory.remember(
			'group',
			{ authorId: FRIEND, authorName: '群友甲', messageId: old.id },
			{ text: '群友甲在等包裹送貨', kind: 'fact', importance: 0.6 },
			NOW - 4 * DAY,
		);
		w.runtime.notes.updateCard(
			FRIEND,
			'群友甲',
			[{ text: '在等包裹送貨', importance: 3, src: [old.id] }],
			NOW - 4 * DAY,
		);
		for (const text of ['真的是玩壞了這個肥魚', `<@${BOT}>`]) {
			const seen = await turn(w, {
				channelId: GENERAL,
				author: FRIEND,
				text,
				probes: ['送貨'],
				openIds: [old.id],
			});
			for (const [part, body] of [
				['系统提示', seen.prompt],
				['闸道附上的记忆', seen.memory],
				['工具输出', seen.tools.join('\n')],
			])
				if (body.includes('送貨'))
					problems.push(
						`「${text}」：${part}带进了四天前的送貨（${lineOf(body, '送貨').slice(0, 60)}）`,
					);
		}
	} finally {
		w.cleanup();
	}
});

// 2. 两个公开频道、一个不公开的频道、一则私讯各放一个暗号。
function canaries(w) {
	const at = NOW - 2 * DAY;
	const a = w.say(ROOM_A, FRIEND, '我們的暗號是 KIWI-A-1111', at);
	const b = w.say(ROOM_B, FRIEND2, '我們的暗號是 MANGO-B-2222', at);
	const p = w.say(SECRET_ROOM, FRIEND, '我們的暗號是 PRIV-P-4444', at);
	const d = w.say(DM, OWNER, '私訊的暗號是 PEACH-DM-3333', at);
	const remember = (scope, row, author) =>
		w.runtime.memory.remember(
			scope,
			{
				authorId: author,
				authorName: NAMES[author],
				messageId: row.id,
				channelId: row.channel_id,
				...(row.guild_id ? { guildId: GUILD } : {}),
			},
			{ text: row.content, kind: 'event', importance: 0.5 },
			at,
		);
	remember('group', b, FRIEND2);
	remember('group', p, FRIEND);
	remember('owner-' + OWNER, d, OWNER);
	// 人物卡：站长私讯整理出的「（私下）」条目、不公开频道整理出的条目。
	w.runtime.notes.updateCard(
		OWNER,
		'站長',
		[
			{
				text: '（私下）私訊的暗號是 PEACH-DM-3333',
				importance: 3,
				src: [d.id],
			},
		],
		at,
	);
	w.runtime.notes.updateCard(
		FRIEND,
		'群友甲',
		[{ text: '說暗號是 PRIV-P-4444', importance: 3, src: [p.id] }],
		at,
	);
	return { a, b, p, d };
}
const ALL_PROBES = ['暗號', 'KIWI', 'MANGO', 'PRIV', 'PEACH'];

await scenario(
	'A 頻道閒聊：不出現 B 頻道、私訊、不公開頻道的暗號',
	async (problems) => {
		const w = world();
		try {
			const ids = canaries(w);
			const seen = await turn(w, {
				channelId: ROOM_A,
				author: OWNER,
				text: '今天好熱喔',
				probes: ALL_PROBES,
				openIds: [ids.b.id, ids.p.id, ids.d.id],
			});
			for (const secret of ['MANGO-B-2222', 'PEACH-DM-3333', 'PRIV-P-4444'])
				if (seen.input.includes(secret))
					problems.push(
						`送进模型的内容出现 ${secret}：${lineOf(seen.input, secret).slice(0, 70)}`,
					);
		} finally {
			w.cleanup();
		}
	},
);

await scenario(
	'A 頻道明確問舊事：群聊裡不出現私訊、不公開頻道的暗號；B 頻道的要標來源',
	async (problems) => {
		const w = world();
		try {
			const ids = canaries(w);
			const seen = await turn(w, {
				channelId: ROOM_A,
				author: OWNER,
				text: '上次說的暗號是什麼？',
				probes: ALL_PROBES,
				openIds: [ids.p.id, ids.d.id],
			});
			for (const secret of ['PEACH-DM-3333', 'PRIV-P-4444'])
				if (seen.input.includes(secret))
					problems.push(
						`群聊送进模型的内容出现 ${secret}：${lineOf(seen.input, secret).slice(0, 70)}`,
					);
			for (const line of seen.input.split('\n'))
				if (line.includes('MANGO-B-2222') && !line.includes('#b-room'))
					problems.push(`B 頻道的暗號没标来源：${line.slice(0, 70)}`);
			if (!seen.input.includes('KIWI-A-1111'))
				problems.push('明确问了，却没翻到这个频道自己的暗號');
		} finally {
			w.cleanup();
		}
	},
);

await scenario(
	'A 頻道明確問 B 頻道（公開）的事：可以提，而且說出來源',
	async (problems) => {
		const w = world();
		try {
			canaries(w);
			const seen = await turn(w, {
				channelId: ROOM_A,
				author: OWNER,
				text: 'b-room 那邊說的暗號是什麼？',
			});
			const line = lineOf(seen.input, 'MANGO-B-2222');
			if (!line)
				problems.push('问到 B 頻道的事，送进模型的内容里没有 B 頻道的暗號');
			else if (!line.includes('#b-room'))
				problems.push(`B 頻道的暗號没标来源：${line.slice(0, 70)}`);
			for (const secret of ['PEACH-DM-3333', 'PRIV-P-4444'])
				if (seen.input.includes(secret)) problems.push(`出现 ${secret}`);
		} finally {
			w.cleanup();
		}
	},
);

await scenario(
	'私訊問群聊的事：想得起公開頻道的（標來源），不公開頻道的不拿來講',
	async (problems) => {
		const w = world();
		try {
			const ids = canaries(w);
			const seen = await turn(w, {
				channelId: DM,
				author: OWNER,
				text: '上次 a-room 說的暗號是什麼？',
				probes: ['暗號', 'PRIV'],
				openIds: [ids.p.id],
			});
			const line = lineOf(seen.input, 'KIWI-A-1111');
			if (!line) problems.push('私讯里想不起 A 頻道（公开）的暗號');
			else if (!line.includes('a-room'))
				problems.push(`A 頻道的暗號没标来源：${line.slice(0, 70)}`);
			if (seen.input.includes('PRIV-P-4444'))
				problems.push(
					`私讯里出现不公开频道的暗號：${lineOf(seen.input, 'PRIV-P-4444').slice(0, 70)}`,
				);
		} finally {
			w.cleanup();
		}
	},
);

// 3. 有人明确问旧事：找得到就带同频道的原文和日期；找不到就要她说没找到。
await scenario(
	'明確問「上次那個包裹到了嗎」：帶同頻道原文和日期；找不到就說沒找到',
	async (problems) => {
		const w = world();
		try {
			const sent = NOW - 4 * DAY;
			w.say(GENERAL, FRIEND2, '我的包裹今天寄出了，大概三天到', sent);
			w.say(ROOM_B, FRIEND2, '露營的包裹在 b-room 這邊說', sent);
			const found = await turn(w, {
				channelId: GENERAL,
				author: FRIEND2,
				text: '上次那個包裹到了嗎？',
				probes: ['包裹'],
			});
			const line = lineOf(found.prompt, '包裹今天寄出了');
			if (!line) problems.push('系统提示里没有同频道的原文');
			else if (!line.includes(stamp(sent)))
				problems.push(`原文没带日期 ${stamp(sent)}：${line.slice(0, 60)}`);
			if (!found.tools.join('\n').includes('包裹今天寄出了'))
				problems.push('chat_search 找不到同频道的原文');
			const missing = await turn(w, {
				channelId: GENERAL,
				author: FRIEND2,
				text: '上次說的潛水課你還記得嗎？',
				probes: ['潛水課'],
			});
			if (!/沒找到|不記得/.test(missing.prompt))
				problems.push(
					'明确问了却什么都没找到，系统提示没有要她说没找到（不要编）',
				);
			if (!/沒找到/.test(missing.tools.join('\n')))
				problems.push(
					'chat_search 没找到时没有说没找到：' +
						missing.tools.join(' / ').slice(0, 100),
				);
		} finally {
			w.cleanup();
		}
	},
);

// 4. 超过 6 个月的事：没提到日期不翻；有提到日期才翻。
await scenario(
	'超過 6 個月的事：沒提日期不翻，有提日期才翻',
	async (problems) => {
		const w = world();
		try {
			const at = NOW - 250 * DAY;
			const month = Number(
				new Intl.DateTimeFormat('en-US', {
					timeZone: 'Asia/Taipei',
					month: 'numeric',
				}).format(new Date(at)),
			);
			const old = w.say(GENERAL, FRIEND, '上次在墾丁潛水看到海龜，超大隻', at);
			w.runtime.memory.remember(
				'group',
				{
					authorId: FRIEND,
					authorName: '群友甲',
					messageId: old.id,
					channelId: GENERAL,
					guildId: GUILD,
				},
				{ text: '上次在墾丁潛水看到海龜', kind: 'event', importance: 0.5 },
				at,
			);
			const plain = await turn(w, {
				channelId: GENERAL,
				author: FRIEND,
				text: '那次潛水看到海龜是在哪裡？',
				probes: ['海龜'],
			});
			if (plain.input.includes('墾丁'))
				problems.push(
					`没提日期，却翻出 8 个多月前的事：${lineOf(plain.input, '墾丁').slice(0, 60)}`,
				);
			const dated = await turn(w, {
				channelId: GENERAL,
				author: FRIEND,
				text: `${month}月潛水看到海龜那次是在哪裡？`,
				probes: ['海龜'],
			});
			if (!dated.input.includes('墾丁'))
				problems.push(`提了「${month}月」，却翻不到那时候的事`);
		} finally {
			w.cleanup();
		}
	},
);

// 5. 她的行为规则：群聊与私讯的系统提示都带。
await scenario(
	'行為規則（記得不等於要說、以現在為準…）在群聊與私訊都有',
	async (problems) => {
		const w = world();
		try {
			for (const channelId of [GENERAL, DM]) {
				const seen = await turn(w, { channelId, author: OWNER, text: '早安' });
				for (const rule of [
					'記得不等於要說',
					'以現在說的為準',
					'不要編',
					'具體的那一刻',
				])
					if (!seen.prompt.includes(rule))
						problems.push(
							`${channelId === DM ? '私讯' : '群聊'}的系统提示没有「${rule}」`,
						);
			}
		} finally {
			w.cleanup();
		}
	},
);

// 6. 背景整理：模型失败、格式错、写入失败，各自留下失败在哪一步、遮掉机密的原因；能安全重试、不重复写入。
function consolidationWorld(reply) {
	const w = world();
	const calls = [];
	// 正式环境的模型路线（world 已经设好）；背景整理的回答照情境给的 reply。
	w.model.consolidator = (options) => {
		calls.push(options);
		return reply(calls.length, options);
	};
	const line1 = w.say(
		GENERAL,
		FRIEND,
		'我家的貓叫麻糬，今天剛結紮',
		NOW - 3600_000,
	);
	const line2 = w.say(GENERAL, BOT, '麻糬辛苦了', NOW - 3500_000);
	const lines = [
		{
			role: 'user',
			speaker: FRIEND,
			name: '群友甲',
			text: line1.content,
			at: NOW - 3600_000,
			id: line1.id,
		},
		{
			role: 'assistant',
			text: line2.content,
			at: NOW - 3500_000,
			id: line2.id,
		},
	];
	const state = () => {
		const card = readFileSync(
			join(w.stateRoot, 'notes', 'people', FRIEND + '.md'),
			'utf8',
		);
		const journalDir = join(w.stateRoot, 'notes', 'journal');
		const journal = readdirSync(journalDir)
			.map((name) => readFileSync(join(journalDir, name), 'utf8'))
			.join('\n');
		const records = w.runtime.memory
			.active('group')
			.filter((row) => row.text.includes('麻糬'));
		return {
			cardNotes: card.split('\n').filter((row) => row.includes('麻糬')).length,
			journalLines: journal.split('\n').filter((row) => row.includes('麻糬'))
				.length,
			records: records.length,
			kinds: records.map((row) => row.kind),
		};
	};
	const batches = () => {
		const dir = join(w.stateRoot, 'notes', 'batches');
		try {
			return readdirSync(dir)
				.filter((name) => name.endsWith('.json'))
				.map((name) => JSON.parse(readFileSync(join(dir, name), 'utf8')));
		} catch {
			return [];
		}
	};
	const retry = async () => {
		if (w.runtime.noteBatches) w.runtime.noteBatches.retryMs = 0;
		for (const batch of batches()) {
			batch.retryAt = 0;
			writeFileSync(
				join(w.stateRoot, 'notes', 'batches', batch.id + '.json'),
				JSON.stringify(batch),
			);
		}
		w.runtime.retryNotes?.();
		await w.runtime.notesQueue;
		await w.runtime.notesQueue;
	};
	return { w, calls, lines, state, batches, retry };
}
const SECRET = 'sk-abcdefghijklmnopqrstuvwx1234';
const goodReply = JSON.stringify({
	people: [
		{
			id: FRIEND,
			name: '群友甲',
			notes: [{ text: '家裡的貓叫麻糬，剛結紮', importance: 3, src: [1] }],
		},
	],
	journal: [{ text: '群友甲的貓麻糬剛結紮', src: [1] }],
	facts: [],
});
async function consolidationCase(problems, { reply, stage, breakWrite }) {
	const c = consolidationWorld(reply);
	try {
		let restore;
		if (breakWrite) restore = breakWrite(c.w.runtime);
		await c.w.runtime.consolidate(c.lines, '#general').catch(() => {});
		await c.w.runtime.notesQueue;
		const recorded = c.batches();
		const failure = recorded[0]?.failure;
		if (!failure) problems.push(`失败没留下纪录（批次帐里没有这批）`);
		else if (failure.stage !== stage)
			problems.push(`失败的步骤记成 ${failure.stage}，应该是 ${stage}`);
		const logs = c.w.warns.join('\n');
		if (!logs.includes(stage))
			problems.push(
				`日志没写出失败在哪一步（${stage}）：${logs.slice(0, 80) || '（没有日志）'}`,
			);
		// 批次本身要留着来源（重试要用），所以只看失败纪录与日志。
		const everything =
			logs + JSON.stringify(recorded.map((batch) => batch.failure));
		if (everything.includes(SECRET) || everything.includes('麻糬'))
			problems.push('失败纪录或日志里出现机密或聊天内文');
		restore?.();
		await c.retry();
		const after = c.state();
		if (
			after.cardNotes !== 1 ||
			after.journalLines !== 1 ||
			after.records !== 1
		)
			problems.push(
				`重试后人物卡 ${after.cardNotes} 条、日记 ${after.journalLines} 条、召回记录 ${after.records} 条（都应该是 1）`,
			);
		if (after.kinds.includes('fact'))
			problems.push('整理出的笔记还是标成「事实」（每轮都会抢常驻名额）');
		if (c.batches().length) problems.push('重试成功后批次还留着');
		const options = c.calls.at(-1) ?? {};
		if (options.reasoningEffort === 'off')
			problems.push('整理还是指定思考深度 off（这条路线不接受）');
		if (!/^session-/.test(options.sessionId ?? ''))
			problems.push('整理没有带会话编号（这条路线会回 400 MissingSessionID）');
		if (!(options.maxTokens >= 2000))
			problems.push(`整理的 maxTokens 是 ${options.maxTokens}（应该约 2000）`);
	} catch (error) {
		problems.push(
			'人物卡或日记没写出来：' + String(error?.message ?? error).slice(0, 80),
		);
	} finally {
		c.w.cleanup();
	}
}
await scenario('背景整理：模型失敗', (problems) =>
	consolidationCase(problems, {
		stage: 'model',
		reply: (n) => {
			if (n === 1)
				throw Object.assign(
					new Error(`upstream 500: invalid reasoning effort (key ${SECRET})`),
					{ status: 500 },
				);
			return goodReply;
		},
	}),
);
await scenario('背景整理：格式錯', (problems) =>
	consolidationCase(problems, {
		stage: 'parse',
		reply: (n) => (n === 1 ? '好的，這是整理：麻糬 ' + SECRET : goodReply),
	}),
);
await scenario('背景整理：寫入失敗（人物卡寫好、日記寫不進去）', (problems) =>
	consolidationCase(problems, {
		stage: 'write',
		reply: () => goodReply,
		breakWrite: (runtime) => {
			const original = runtime.notes.addJournal.bind(runtime.notes);
			runtime.notes.addJournal = () => {
				throw Object.assign(
					new Error(`EACCES: permission denied, open '/x/${SECRET}'`),
					{ code: 'EACCES' },
				);
			};
			return () => (runtime.notes.addJournal = original);
		},
	}),
);

// 7. fish-memory 的设定档：不存在就用预设值、只提示一次；损坏才报警。
await scenario(
	'fish-memory 設定檔：不存在只提示一次，損壞才報警',
	async (problems) => {
		const dir = mkdtempSync(join(tmpdir(), 'memory-rules-fish-'));
		try {
			const probe = join(dir, 'probe.mjs');
			writeFileSync(
				probe,
				`const m = await import(${JSON.stringify(pathToFileURL(join(root, 'dsh-fish-memory/lib/index.js')).href)});\n` +
					`for (let i = 0; i < 5; i++) m.loadCfg();\n` +
					`if (process.argv[2] === 'broken') { (await import('node:fs')).writeFileSync(process.env.DSH_HOME + '/fish-memory-config.json', '{壞掉'); m.loadCfg(); m.loadCfg(); }\n`,
			);
			const count = (mode) => {
				const sub = join(dir, mode);
				mkdirSync(sub);
				spawnSync(process.execPath, [probe, mode], {
					env: { ...process.env, DSH_HOME: sub },
					encoding: 'utf8',
				});
				let log = '';
				try {
					log = readFileSync(join(sub, 'fish-memory-diag.log'), 'utf8');
				} catch {
					/* 没有日志。 */
				}
				return log.split('\n').filter((line) => line.includes('配置文件'))
					.length;
			};
			const missing = count('missing');
			if (missing > 1)
				problems.push(
					`设定档不存在时，5 次读取写了 ${missing} 行（应该只提示 1 次）`,
				);
			const broken = count('broken');
			if (broken - missing < 2)
				problems.push(
					`设定档损坏时没有每次报警（多了 ${broken - missing} 行，应该 2 行）`,
				);
		} finally {
			rmSync(dir, { recursive: true, force: true });
		}
	},
);

// ==== 第 1 轮审查（Sol）找到的问题：每一项在修之前都会失败 ====
const waitFor = async (check, ms = 3000) => {
	const until = Date.now() + ms;
	while (Date.now() < until) {
		if (await check()) return true;
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
	return Boolean(await check());
};
// 群友甲的人物卡：私讯整理出的名字、站长卡上「（私下）」的条目、不公开频道整理来的条目、一条出处混了不公开频道的。
function privateCards(w) {
	const at = NOW - 2 * DAY;
	const ids = canaries(w);
	const here = w.say(GENERAL, FRIEND, '我最近在學衝浪', at);
	const hidden = w.say(SECRET_ROOM, FRIEND, '我的口令是 MIX-SECRET-9999', at);
	w.runtime.notes.updateCard(
		FRIEND,
		'群友甲',
		[
			{
				text: '口令是 MIX-SECRET-9999，最近在學衝浪',
				importance: 3,
				src: [here.id, hidden.id],
			},
		],
		at,
	);
	// 站长卡上一条群聊看得到的（让站长的卡在群聊会出现，才看得到标题与曾用名）。
	const hike = w.say(GENERAL, OWNER, '週末要去爬山', at);
	w.runtime.notes.updateCard(
		OWNER,
		'站長',
		[{ text: '週末要去爬山', importance: 3, src: [hike.id] }],
		at,
	);
	// 旧版留下的名字没有出处（不知道是在私讯还是群里看到的）。
	w.runtime.notes.updateCard(OWNER, '私訊小名阿肥', [], at);
	return { ...ids, here, hidden };
}
const PRIVATE_BITS = [
	'PEACH-DM-3333',
	'PRIV-P-4444',
	'MIX-SECRET-9999',
	'私訊小名阿肥',
];

await scenario(
	'審查 1：群聊的背景整理只拿得到群聊看得到的人物卡條目與名字',
	async (problems) => {
		const w = world();
		try {
			privateCards(w);
			const asked = [];
			w.model.consolidator = (options) => {
				asked.push(options.messages[0].content[0].text);
				return JSON.stringify({ people: [], journal: [], facts: [] });
			};
			const a = w.say(GENERAL, OWNER, '今天天氣不錯', NOW - 60_000);
			const b = w.say(GENERAL, FRIEND, '對啊適合出門', NOW - 50_000);
			await w.runtime
				.consolidate(
					[
						{
							role: 'user',
							speaker: OWNER,
							name: '站長',
							text: a.content,
							at: NOW - 60_000,
							id: a.id,
						},
						{
							role: 'user',
							speaker: FRIEND,
							name: '群友甲',
							text: b.content,
							at: NOW - 50_000,
							id: b.id,
						},
					],
					'#general',
				)
				.catch(() => {});
			await w.runtime.notesQueue;
			const text = asked.join('\n');
			if (!asked.length) problems.push('背景整理没有问模型');
			for (const bit of PRIVATE_BITS)
				if (text.includes(bit))
					problems.push(
						`群聊的背景整理送进模型的「現有記憶」出现 ${bit}：${lineOf(text, bit).slice(0, 70)}`,
					);
		} finally {
			w.cleanup();
		}
	},
);

await scenario(
	'審查 1：人物卡一條的每個出處都要看得到；沒分範圍的舊名字群聊不顯示',
	async (problems) => {
		const w = world();
		try {
			privateCards(w);
			for (const [author, text] of [
				[FRIEND, '上次說的口令是什麼？'],
				[OWNER, '上次說的暗號是什麼？'],
			]) {
				const seen = await turn(w, { channelId: GENERAL, author, text });
				for (const bit of PRIVATE_BITS)
					if (seen.input.includes(bit))
						problems.push(
							`群聊「${text}」送进模型的内容出现 ${bit}：${lineOf(seen.input, bit).slice(0, 70)}`,
						);
			}
			// 私讯整理记下的名字（新的、有出处 dm）也不能出现在群聊。
			w.model.consolidator = () =>
				JSON.stringify({
					people: [
						{
							id: OWNER,
							name: '私訊新名字',
							notes: [{ text: '喜歡貓', importance: 3, src: [1] }],
						},
					],
					journal: [],
					facts: [],
				});
			const d = w.say(DM, OWNER, '我喜歡貓', NOW - 30_000);
			await w.runtime
				.consolidate(
					[
						{
							role: 'user',
							speaker: OWNER,
							name: '私訊新名字',
							text: d.content,
							at: NOW - 30_000,
							id: d.id,
						},
					],
					'私訊',
					{ private: true },
				)
				.catch(() => {});
			await w.runtime.notesQueue;
			const seen = await turn(w, {
				channelId: GENERAL,
				author: OWNER,
				text: '上次說的暗號是什麼？',
			});
			if (seen.input.includes('私訊新名字'))
				problems.push(
					`私讯整理记下的名字出现在群聊：${lineOf(seen.input, '私訊新名字').slice(0, 70)}`,
				);
		} finally {
			w.cleanup();
		}
	},
);

// 站长下 /記憶 指令：回覆的文字（就是会贴到那个频道的内容）。
async function command(w, channelId, text) {
	const message = w.say(channelId, OWNER, text);
	w.runtime.admit(message);
	await w.runtime.visibility?.warm(GUILD);
	const out = [];
	await w.runtime.control(message, async (body) => out.push(body));
	return out.join('\n');
}

await scenario(
	'審查 2：站長在群組用 /記憶：只顯示這個群組看得到的；私訊裡才看得到全部',
	async (problems) => {
		const w = world();
		try {
			const ids = privateCards(w);
			w.runtime.notes.addFact('不公開頻道的提議 PRIV-FACT-1212', {
				pending: true,
				src: [ids.p.id],
			});
			for (const [text, bits] of [
				[`/記憶 人物卡 <@${OWNER}>`, ['PEACH-DM-3333', '私訊小名阿肥']],
				[`/記憶 人物卡 <@${FRIEND}>`, ['PRIV-P-4444', 'MIX-SECRET-9999']],
				['/記憶', ['PRIV-P-4444', 'PEACH-DM-3333']],
				['/記憶 查 暗號', ['PRIV-P-4444', 'PEACH-DM-3333']],
				['/記憶 事實', ['PRIV-FACT-1212']],
			]) {
				const out = await command(w, ROOM_A, text);
				for (const bit of bits)
					if (out.includes(bit))
						problems.push(
							`群组里「${text}」的输出出现 ${bit}：${lineOf(out, bit).slice(0, 70)}`,
						);
			}
			const dm = await command(w, DM, `/記憶 人物卡 <@${OWNER}>`);
			if (!dm.includes('PEACH-DM-3333'))
				problems.push('私讯里看不到站长卡上私下的条目（私讯应该看得到全部）');
		} finally {
			w.cleanup();
		}
	},
);

// 一则在某个频道说的暗号，存成召回记录（出处是那则讯息）。
function plant(w, channelId, text, at = NOW - 2 * DAY) {
	const row = w.say(channelId, FRIEND, text, at);
	w.runtime.memory.remember(
		'group',
		{
			authorId: FRIEND,
			authorName: NAMES[FRIEND],
			messageId: row.id,
			channelId,
			guildId: GUILD,
		},
		{ text, kind: 'event', importance: 0.5 },
		at,
	);
	return row;
}

await scenario(
	'審查 3：公開頻道要看完整的權限覆寫（角色、個人的拒絕都算不公開）',
	async (problems) => {
		const w = world();
		try {
			plant(w, ROLE_ROOM, '我們的暗號是 ROLEDENY-7777');
			plant(w, MEMBER_ROOM, '我們的暗號是 MEMBERDENY-8888');
			for (const text of [
				'role-room 那邊說的暗號是什麼？',
				'member-room 那邊說的暗號是什麼？',
				'上次說的暗號是什麼？',
			]) {
				const seen = await turn(w, {
					channelId: ROOM_A,
					author: OWNER,
					text,
					probes: ['暗號', 'ROLEDENY', 'MEMBERDENY'],
				});
				for (const bit of ['ROLEDENY-7777', 'MEMBERDENY-8888'])
					if (seen.input.includes(bit))
						problems.push(
							`「${text}」送进模型的内容出现 ${bit}（那个频道有人看不到）`,
						);
			}
		} finally {
			w.cleanup();
		}
	},
);

// 假闸道：照正式的 DiscordGateway 只认讯息事件；runtime 拿到它以后要自己看权限变动的事件。
function fakeGateway() {
	return {
		seen: [],
		dispatch(payload) {
			this.seen.push(payload.t);
		},
	};
}

await scenario(
	'審查 3：權限收緊後不再放行（閘道通知就作廢快取；論壇、公開討論串跟著上層頻道）',
	async (problems) => {
		const w = world();
		try {
			canaries(w);
			const post = w.say(
				FORUM_POST,
				FRIEND,
				'論壇的暗號是 FORUM-6666',
				NOW - 2 * DAY,
			);
			w.runtime.admit(post);
			await w.runtime.visibility?.warm(GUILD);
			await w.runtime.visibility?.warmChannel(FORUM_POST);
			const gateway = fakeGateway();
			w.runtime.gateway = gateway;
			const before = await turn(w, {
				channelId: ROOM_A,
				author: OWNER,
				text: '上次說的暗號是什麼？',
				probes: ['暗號'],
			});
			if (!before.input.includes('MANGO-B-2222'))
				problems.push('（前提）收紧之前 B 频道是公开的，应该翻得到');
			if (!before.input.includes('FORUM-6666'))
				problems.push('（前提）论坛是公开的，贴文应该翻得到');
			// B 频道改成不公开、论坛改成不公开；Discord 送来 CHANNEL_UPDATE。
			w.channels.find((row) => row.id === ROOM_B).hidden = true;
			w.channels.find((row) => row.id === FORUM).hidden = true;
			for (const id of [ROOM_B, FORUM])
				w.runtime.gateway.dispatch({
					op: 0,
					t: 'CHANNEL_UPDATE',
					d: { id, guild_id: GUILD, type: id === FORUM ? 15 : 0 },
				});
			if (!gateway.seen.includes('CHANNEL_UPDATE'))
				problems.push('闸道原本的处理没有被呼叫');
			await new Promise((resolve) => setTimeout(resolve, 50));
			const after = await turn(w, {
				channelId: ROOM_A,
				author: OWNER,
				text: '上次說的暗號是什麼？',
				probes: ['暗號', 'MANGO', 'FORUM'],
			});
			for (const bit of ['MANGO-B-2222', 'FORUM-6666'])
				if (after.input.includes(bit))
					problems.push(
						`权限收紧（并收到 CHANNEL_UPDATE）以后，送进模型的内容还出现 ${bit}`,
					);
		} finally {
			w.cleanup();
		}
	},
);

await scenario(
	'審查 3：快取過期又問不到 Discord（API 故障）就當成不公開',
	async (problems) => {
		const w = world();
		try {
			canaries(w);
			await w.runtime.visibility?.warm(GUILD);
			// 十一分钟以后，Discord API 故障。
			const later = Date.now() + 11 * 60_000;
			if (w.runtime.visibility) w.runtime.visibility.now = () => later;
			w.api.down = true;
			const seen = await turn(w, {
				channelId: ROOM_A,
				author: OWNER,
				text: 'b-room 那邊說的暗號是什麼？',
				probes: ['暗號', 'MANGO'],
			});
			if (seen.input.includes('MANGO-B-2222'))
				problems.push(
					'快取过期、Discord 问不到的时候，别的频道的暗号还是放行了',
				);
		} finally {
			w.cleanup();
		}
	},
);

await scenario(
	'審查 3：冷啟動的第一則就照規則（權限還沒載入時不放行，載好了才放行）',
	async (problems) => {
		const w = world();
		try {
			canaries(w);
			// 不先载入（正式环境刚重启）：直接收讯息、准备回话。
			const message = w.say(ROOM_A, OWNER, 'b-room 那邊說的暗號是什麼？');
			w.runtime.admit(message);
			const agent = fakeAgent('session-cold', '/srv/dsh/discord');
			agent.events.push({
				seq: 1,
				type: 'user/message',
				data: {
					message: {
						role: 'user',
						content: [{ type: 'text', text: message.content }],
						source: {
							kind: 'user',
							discordMessageId: message.id,
							discordChannelId: ROOM_A,
						},
					},
				},
			});
			await w.runtime.prepare(agent, ROOM_A, message.id);
			const prompt = agent.prompt();
			if (prompt.includes('PRIV-P-4444') || prompt.includes('PEACH-DM-3333'))
				problems.push('冷启动的第一则出现不公开频道或私讯的暗号');
			if (!prompt.includes('MANGO-B-2222'))
				problems.push(
					'冷启动的第一则：准备回话时没先载好频道权限（点名的公开频道想不起来）',
				);
		} finally {
			w.cleanup();
		}
	},
);

// 背景整理的批次：自动醒来、冷启动接着做、同一批同时交两次、讯息改过以后重试。
function notesLines(w, text = '我家的貓叫麻糬，今天剛結紮') {
	const row = w.say(GENERAL, FRIEND, text, NOW - 3600_000);
	return {
		row,
		lines: [
			{
				role: 'user',
				speaker: FRIEND,
				name: '群友甲',
				text: row.content,
				at: NOW - 3600_000,
				id: row.id,
			},
		],
	};
}
// 模型照收到的对话整理（日记写出那一行的原文，看得出用的是哪个版本的文字）。
const echoNotes = (options) => {
	const said =
		/\[1\] [^：]+：(.+)/.exec(options.messages[0].content[0].text)?.[1] ?? '';
	return JSON.stringify({
		people: [
			{
				id: FRIEND,
				name: '群友甲',
				notes: [{ text: '說：' + said, importance: 3, src: [1] }],
			},
		],
		journal: [{ text: '群友甲說：' + said, src: [1] }],
		facts: [],
	});
};
const journalText = (w) => {
	const dir = join(w.stateRoot, 'notes', 'journal');
	try {
		return readdirSync(dir)
			.map((name) => readFileSync(join(dir, name), 'utf8'))
			.join('\n');
	} catch {
		return '';
	}
};

await scenario('審查：背景整理失敗的批次會自己醒來重試', async (problems) => {
	const w = world();
	try {
		const { lines } = notesLines(w);
		let calls = 0;
		w.model.consolidator = (options) => {
			calls++;
			if (calls === 1)
				throw Object.assign(new Error('upstream 500'), { status: 500 });
			return echoNotes(options);
		};
		w.runtime.config.notesWakeMinMs = 10;
		if (w.runtime.noteBatches) w.runtime.noteBatches.retryMs = 30;
		await w.runtime.consolidate(lines, '#general').catch(() => {});
		// 之后没有任何新的对话进来。
		const woke = await waitFor(() => journalText(w).includes('麻糬'), 2000);
		if (!woke)
			problems.push(
				`失败的批次到期后没有自己重试（模型只被问了 ${calls} 次，日记没写出来）`,
			);
	} finally {
		w.cleanup();
	}
});

await scenario(
	'審查：冷啟動時把上一個程序留下的批次接著做',
	async (problems) => {
		const w = world();
		try {
			const { lines } = notesLines(w);
			// 上一个程序：批次存下来了，还没整理完就停掉（没失败纪录）。
			w.model.consolidator = () => new Promise(() => {});
			void w.runtime.consolidate(lines, '#general').catch(() => {});
			await new Promise((resolve) => setTimeout(resolve, 30));
			// 新程序（同一个资料夹）启动。
			const next = new SocialRuntime(
				{
					get: (name) => w.services[name],
					logger: { warn: (text) => w.warns.push(text), info() {} },
				},
				{
					stateRoot: w.stateRoot,
					ownerIds: [OWNER],
					allowedChannels: ['*'],
					groupCwd: '/srv/dsh/discord',
					notesColdStartMs: 20,
					notesWakeMinMs: 10,
				},
				engine,
			);
			next.archive = w.runtime.archive;
			w.model.consolidator = echoNotes;
			const done = await waitFor(() => journalText(w).includes('麻糬'), 2000);
			if (!done)
				problems.push('新程序启动以后，上一个程序留下的批次没有接着整理');
		} finally {
			w.cleanup();
		}
	},
);

await scenario(
	'審查：同一批同時交兩次，只問一次模型、只寫一筆日記',
	async (problems) => {
		const w = world();
		try {
			const { lines } = notesLines(w);
			let calls = 0;
			w.model.consolidator = async (options) => {
				calls++;
				await new Promise((resolve) => setTimeout(resolve, 30));
				return echoNotes(options);
			};
			await Promise.all([
				w.runtime.consolidate(lines, '#general').catch(() => {}),
				w.runtime.consolidate(lines, '#general').catch(() => {}),
			]);
			await w.runtime.notesQueue;
			await w.runtime.notesQueue;
			const written = journalText(w)
				.split('\n')
				.filter((line) => line.includes('麻糬')).length;
			if (calls !== 1 || written !== 1)
				problems.push(
					`同一批同时交两次：模型被问了 ${calls} 次、日记写了 ${written} 笔（都应该是 1）`,
				);
		} finally {
			w.cleanup();
		}
	},
);

await scenario('審查：訊息編輯以後，重試用改過的文字', async (problems) => {
	const w = world();
	try {
		const { row, lines } = notesLines(w, '我家的貓叫麻糬，今天剛結紮');
		w.model.consolidator = echoNotes;
		// 第一次：模型整理好了，日记写不进去。
		const original = w.runtime.notes.addJournal.bind(w.runtime.notes);
		w.runtime.notes.addJournal = () => {
			throw Object.assign(new Error('EACCES'), { code: 'EACCES' });
		};
		await w.runtime.consolidate(lines, '#general').catch(() => {});
		await w.runtime.notesQueue;
		w.runtime.notes.addJournal = original;
		// 群友改了讯息。
		w.runtime.archive.record('edit', {
			...w.messages.get(row.id),
			content: '我家的貓叫湯圓，今天剛結紮',
			edited_timestamp: new Date(NOW - 1000).toISOString(),
		});
		if (w.runtime.noteBatches) w.runtime.noteBatches.retryMs = 0;
		const dir = join(w.stateRoot, 'notes', 'batches');
		for (const name of readdirSync(dir).filter((entry) =>
			entry.endsWith('.json'),
		)) {
			const batch = JSON.parse(readFileSync(join(dir, name), 'utf8'));
			batch.retryAt = 0;
			writeFileSync(join(dir, name), JSON.stringify(batch));
		}
		w.runtime.retryNotes?.();
		await w.runtime.notesQueue;
		await w.runtime.notesQueue;
		const journal = journalText(w);
		if (journal.includes('麻糬'))
			problems.push(
				`讯息改过以后，重试还是写进旧的文字：${lineOf(journal, '麻糬').slice(0, 60)}`,
			);
		if (!journal.includes('湯圓')) problems.push('重试没有用改过的文字写日记');
	} finally {
		w.cleanup();
	}
});

await scenario(
	'審查：重試前訊息被刪了就不整理那則；帳本裡沒有的訊息照樣整理',
	async (problems) => {
		const w = world();
		try {
			const { row, lines } = notesLines(w);
			let calls = 0;
			w.model.consolidator = (options) => {
				calls++;
				if (calls === 1)
					throw Object.assign(new Error('upstream 500'), { status: 500 });
				return echoNotes(options);
			};
			await w.runtime.consolidate(lines, '#general').catch(() => {});
			await w.runtime.notesQueue;
			w.runtime.archive.record('delete', {
				id: row.id,
				channel_id: GENERAL,
				guild_id: GUILD,
			});
			if (w.runtime.noteBatches) w.runtime.noteBatches.retryMs = 0;
			const dir = join(w.stateRoot, 'notes', 'batches');
			for (const name of readdirSync(dir).filter((entry) =>
				entry.endsWith('.json'),
			)) {
				const batch = JSON.parse(readFileSync(join(dir, name), 'utf8'));
				batch.retryAt = 0;
				writeFileSync(join(dir, name), JSON.stringify(batch));
			}
			w.runtime.retryNotes?.();
			await w.runtime.notesQueue;
			await w.runtime.notesQueue;
			if (journalText(w).includes('麻糬'))
				problems.push('讯息在重试前被删了，重试还是把它整理进日记');
			// 帐本里没有的讯息（id 对不上帐本，例如帐本漏记）：不能当成删掉、整批默默不整理。
			const loose = [
				{
					role: 'user',
					speaker: FRIEND,
					name: '群友甲',
					text: '我家的狗叫布丁，今天去打預防針',
					at: NOW - 600_000,
					id: '1999999999999999999',
				},
			];
			w.model.consolidator = (options) =>
				echoNotes(options).replaceAll('麻糬', '布丁');
			const result = await w.runtime
				.consolidate(loose, '#general')
				.catch((error) => ({ error: String(error?.message) }));
			await w.runtime.notesQueue;
			if (!journalText(w).includes('布丁'))
				problems.push(
					`帐本里没有的讯息整批没整理（${JSON.stringify(result).slice(0, 60)}）`,
				);
		} finally {
			w.cleanup();
		}
	},
);

await scenario('審查：出處已經被刪掉的記憶不再帶出', async (problems) => {
	const w = world();
	try {
		const row = plant(w, GENERAL, '我們的暗號是 GONE-5050');
		const dm = w.say(DM, OWNER, '私訊說的暗號是 GONE-DM-6060', NOW - 2 * DAY);
		w.runtime.memory.remember(
			'owner-' + OWNER,
			{ authorId: OWNER, authorName: '站長', messageId: dm.id, channelId: DM },
			{ text: '私訊說的暗號是 GONE-DM-6060', kind: 'event', importance: 0.5 },
			NOW - 2 * DAY,
		);
		w.runtime.archive.record('delete', {
			id: row.id,
			channel_id: GENERAL,
			guild_id: GUILD,
		});
		w.runtime.archive.record('delete', { id: dm.id, channel_id: DM });
		const group = await turn(w, {
			channelId: GENERAL,
			author: FRIEND,
			text: '上次說的暗號是什麼？',
		});
		if (group.input.includes('GONE-5050'))
			problems.push(
				`群聊：出处被删掉的记忆还是带出来了：${lineOf(group.input, 'GONE-5050').slice(0, 70)}`,
			);
		const direct = await turn(w, {
			channelId: DM,
			author: OWNER,
			text: '上次說的暗號是什麼？',
		});
		if (direct.input.includes('GONE-DM-6060'))
			problems.push(
				`私讯：出处被删掉的记忆还是带出来了：${lineOf(direct.input, 'GONE-DM-6060').slice(0, 70)}`,
			);
		const listed = await command(w, GENERAL, '/記憶');
		if (listed.includes('GONE-5050'))
			problems.push('群组里 /記憶 还列出出处被删掉的记忆');
	} finally {
		w.cleanup();
	}
});

await scenario(
	'審查：要不要翻不靠關鍵字（打招呼帶 @ 不翻；含蓄的追問要翻）',
	async (problems) => {
		const w = world();
		try {
			const old = w.say(
				GENERAL,
				FRIEND,
				'我今天要把電腦重灌，好麻煩',
				NOW - 3 * DAY,
			);
			w.runtime.notes.updateCard(
				FRIEND,
				'群友甲',
				[{ text: '在重灌電腦', importance: 3, src: [old.id] }],
				NOW - 3 * DAY,
			);
			const hello = await turn(w, {
				channelId: GENERAL,
				author: OWNER,
				text: `早安 <@${FRIEND}>`,
				probes: ['重灌'],
			});
			if (hello.input.includes('重灌'))
				problems.push(
					`打招呼（早安 @群友甲）却翻出三天前的事：${lineOf(hello.input, '重灌').slice(0, 60)}`,
				);
			const follow = await turn(w, {
				channelId: GENERAL,
				author: OWNER,
				text: '那個搞定了沒？',
			});
			if (!follow.prompt.includes('重灌'))
				problems.push('含蓄的追问（那個搞定了沒？）没有想起三天前的重灌');
			const asked = w.gateAsked.map((row) => row.target);
			if (w.runtime.recallVerdicts && !asked.includes('那個搞定了沒？'))
				problems.push('要不要翻没有问回忆守门员');
		} finally {
			w.cleanup();
		}
	},
);

rmSync(home, { recursive: true, force: true });
const failed = results.filter((row) => row.problems.length);
const patched = Boolean(
	readdirSync(join(root, 'dsh-social-runtime')).includes('recall-rules.mjs'),
);
console.log(
	`\nrelease 副本：${patched ? '已移植翻旧事的规则' : '未移植（现行版本）'}`,
);
console.log(
	`${results.length - failed.length} 项通过，${failed.length} 项失败`,
);
process.exit(failed.length ? 1 : 0);
