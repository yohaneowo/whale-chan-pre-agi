// 重现 2026-11-28 「@ 了只显示正在输入、不回话」：用假的时钟把当天的时间线跑一遍。
// 不连 Discord、不叫模型。桥接按频道串行、闸道讯息 5 分钟效期的行为，直接载入 VM 上
// 部署的 gateway-scopes.mjs（公开仓库不收录），群聊行为处理载入要检验的那一份。
//
//   node deploy/dsh/typing-hang-repro.mjs --participation <natural-participation.mjs> \
//     --gateway <部署版 gateway-scopes.mjs> [--deadline 240000]
//
// 当天：07:57:24 第一则 @，她续画半天前的旧图，回合连同两轮补救跑到 08:04:49；
// 07:57:55「你怎麼了」；07:58:00 第二则 @，排在第一轮后面，轮到它时已经过期被丢掉。
// 有任何一则 @ 没有下文（回话或一句失败提示），或处理过程丢错，就以代码 1 结束。
import console from 'node:console';
import process from 'node:process';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const { values } = parseArgs({
	options: {
		participation: { type: 'string' },
		gateway: { type: 'string' },
		deadline: { type: 'string' },
	},
});
if (!values.participation || !values.gateway) {
	console.error('需要 --participation 与 --gateway 两个档案路径。');
	process.exit(2);
}
const deadlineMs = values.deadline ? Number(values.deadline) : Infinity;

// 假时钟：闸道模组用 Date.now，群聊行为处理用注入的 now。
let clock = Date.parse('2026-11-27T23:57:24.665Z');
const realNow = Date.now;
Date.now = () => clock;
const stamp = () => new Date(clock + 8 * 3600_000).toISOString().slice(11, 19);

const { NaturalParticipation } = await import(
	pathToFileURL(resolve(values.participation)).href
);
const { GatewayScopes } = await import(
	pathToFileURL(resolve(values.gateway)).href
);

const BOT = '1000000000000000001';
const OWNER = '1000000000000000002';
const CHANNEL = '1000000000000000003';
const GUILD = '1000000000000000004';
const SESSION = 'session-00000000-0000-0000-0000-000000000000';
let nextId = 2000000000000000000n;

const posted = [];
const warnings = [];
const sources = new Map();
const scopes = new GatewayScopes({
	socket: null,
	// 部署版的绑定表 entries() 回传阵列。
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
const agent = { id: SESSION, inbox: { nextTurn: [], remove() {} } };

// 桥接：按频道串行；轮到时先向闸道取本次证明，取不到就回空结果（部署版 discord-bounded 的写法）。
// 第一轮模拟当天的长回合；有 --deadline 时按期限取消、回传失败提示（修正版桥接的写法）。
let chain = Promise.resolve();
let rounds = 0;
const bridge = (message) => {
	const round = ++rounds;
	const run = chain.then(async () => {
		if (!(await scopes.turn(agent, CHANNEL, message.id))) {
			console.log(
				`${stamp()} 桥接：#${round} 排队过期，闸道不给证明，回空结果`,
			);
			return { chunks: [] };
		}
		const work = round === 1 ? 445_000 : 9_000;
		const spent = Math.min(work, deadlineMs);
		clock += spent;
		scopes.end(agent);
		if (spent < work) {
			console.log(`${stamp()} 桥接：#${round} 超过期限，取消回合`);
			return {
				chunks: ['⚠️ 回合失败: 這一輪我做了太多步，先停一下；要繼續再叫我。'],
			};
		}
		return { chunks: [round === 1 ? '這個我不畫啦' : '我在'] };
	});
	chain = run.catch(() => {});
	return run;
};

const directory = await mkdtemp(join(tmpdir(), 'typing-hang-repro-'));
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
	now: () => clock,
	wait: async () => {},
	warn: (text) => warnings.push(text),
	log: (text) => console.log(`${stamp()} 纪录：${text}`),
});
await participation.ready;

const send = async (message, ticket) => {
	const reply = await bridge(message);
	for (const chunk of reply.chunks) {
		if (participation.turnNotice(CHANNEL, chunk)) continue;
		posted.push({ at: stamp(), text: chunk });
		participation.recordReply(CHANNEL, chunk, String(nextId++), OWNER);
	}
	return ticket;
};
const receive = (content, offsetMs) => {
	clock = Date.parse('2026-11-27T23:57:24.665Z') + offsetMs;
	const message = {
		id: String(nextId++),
		channel_id: CHANNEL,
		guild_id: GUILD,
		author: { id: OWNER, username: '站長' },
		content,
		timestamp: new Date(clock).toISOString(),
		mentions: content.includes(`<@${BOT}>`) ? [{ id: BOT }] : [],
	};
	sources.set(message.id, { ...message });
	scopes.receive(message);
	console.log(`${stamp()} 收到：${content.replace(BOT, 'bot')}`);
	return participation
		.receive(
			message,
			{ botUserId: BOT, botName: '小鯨魚' },
			(text, ticket) => send(message, ticket),
			async (text) => {
				posted.push({ at: stamp(), text });
				participation.recordReply(CHANNEL, text, String(nextId++), OWNER);
			},
		)
		.catch((error) => {
			warnings.push(`群聊行为处理未完成，保持安静：${error?.message ?? error}`);
		});
};

const handlers = [
	receive(`<@${BOT}>`, 0),
	receive('你怎麼了', 31_120),
	receive(`<@${BOT}>`, 35_379),
];
await Promise.all(handlers);
await participation.saves.catch(() => {});
await rm(directory, { recursive: true });
Date.now = realNow;

console.log('\n送出的讯息：');
for (const row of posted) console.log(`  ${row.at} ${row.text}`);
console.log('警告：');
for (const line of warnings) console.log(`  ${line}`);
const lastAnswered = posted.some((row) => row.at >= '07:58:00');
const crashed = warnings.some((line) => line.includes('未完成'));
const ok = lastAnswered && !crashed;
console.log(
	ok
		? '\n结果：通过（最后一则 @ 有下文，没有丢错）'
		: `\n结果：失败（${[!lastAnswered && '最后一则 @ 没有下文', crashed && '处理过程丢错被吞掉'].filter(Boolean).join('、')}）`,
);
process.exit(ok ? 0 : 1);
