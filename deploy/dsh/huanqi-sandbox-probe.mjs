// 换气（huanqi）沙盒探针：只在 VM 的沙盒启动里载入（host-sandbox.sh 用 PROBE=huanqi-sandbox-probe.mjs 放进沙盒设定；
// 不进 release）。用真宿主建一个群聊会话，把这个会话的模型串流换成「只有思考、没有正文」（空白回合），
// 换气额度从 0 开始（host-sandbox.sh 用 HUANQI_CREDIT=0 写好帐本）：换气会走「额度用完、熔断」那条路，
// 在 turn/end 的会话事件里补提示。修正前那里同步写会话，宿主拒绝重入，换气的 async 包装把错误变成
// 没人接的 rejection，整个程序结束；修正后要活着，熔断提示等这一轮结束再补上。
// 不连模型、没有金钥，只记事件类型与换气的判词，没有任何群友资料。
import console from 'node:console';
import { randomUUID } from 'node:crypto';
import process from 'node:process';
import { setTimeout } from 'node:timers';
import { pathToFileURL } from 'node:url';

export const name = 'huanqi-sandbox-probe';
export const inject = ['agents'];

const RELEASE = process.env.SBX_RELEASE;
const GROUP_CWD = process.env.SBX_GROUP_CWD;
const CHANNEL = '1000000000000000001';
const GUILD = '1000000000000000002';
const AUTHOR = '1000000000000000003';
const say = (...parts) => console.log('[probe]', ...parts);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const short = (error) =>
	String(error?.stack ?? error?.message ?? error)
		.split('\n')
		.slice(0, 3)
		.join(' | ')
		.slice(0, 400);
const mod = (path) =>
	import(pathToFileURL(`${RELEASE}/web/node_modules/${path}`).href);

export function apply(ctx) {
	const empty = new Set();
	let calls = 0;
	// 这个探针建的会话：模型串流只给一段思考就结束（宿主会当成正常结束、最后一条只有 reasoning）。
	ctx.on('llm/stream', (options, next) => {
		if (!empty.has(options?.sessionId)) return next();
		calls++;
		return (async function* () {
			yield { type: 'reasoning-delta', index: 0, text: '想一想要怎麼回……' };
			yield { type: 'finish', reason: { kind: 'stop' } };
		})();
	});
	void (async () => {
		try {
			await ctx.get('loader')?.await?.();
			const social = await mod('dsh-social-runtime/index.mjs');
			let runtime;
			for (let i = 0; i < 60 && !runtime; i++) {
				try {
					runtime = social.currentRuntime();
				} catch {
					await sleep(500);
				}
			}
			if (!runtime) return say('FAIL social runtime 没有就绪');
			const llm = await import(
				pathToFileURL(
					'/opt/dsh/app/node_modules/@deepseek-ai/dsh-llm/lib/index.js',
				).href
			);
			const presets = ctx.get('agentPresets');
			const presetId = presets
				? (await presets.resolve('whale-social')).id
				: undefined;
			const sessionId = `session-${randomUUID()}`;
			empty.add(sessionId);
			const handle = await ctx.agents.create({
				sessionId,
				meta: { cwd: GROUP_CWD, agentPreset: presetId },
				agentOptions: {
					provider: 'opencode-go',
					model: 'deepseek-v4.1-flash',
					maxTokens: 4096,
				},
				setup: async (agentCtx) => {
					if (presets) await presets.mount(agentCtx, presetId);
				},
			});
			const chat = handle.agent;
			await sleep(2500); // runtime 每秒 mount 一次
			const messageId = String(
				BigInt('1000000000000000100') + BigInt(Date.now() % 1000),
			);
			runtime.admit({
				id: messageId,
				channel_id: CHANNEL,
				guild_id: GUILD,
				author: { id: AUTHOR, username: '沙盒' },
				content: '在嗎',
			});
			runtime.prepare(chat, CHANNEL, messageId);
			say('OK 会话建好，送一则讯息（模型只回思考）');
			chat.followup(
				llm.createUserMessage({
					content: [{ type: 'text', text: '在嗎' }],
					source: {
						kind: 'user',
						discordMessageId: messageId,
						discordChannelId: CHANNEL,
					},
				}),
			);
			// 等这一轮、换气补的那轮都结束（修正后熔断提示在 whenIdle 之后才补）。
			for (let i = 0; i < 3; i++) {
				await Promise.race([chat.whenIdle(), sleep(15_000)]);
				await sleep(2_000);
			}
			const events = chat.session?.snapshotEvents?.() ?? [];
			const ends = events
				.filter((event) => event.type === 'turn/end')
				.map((event) => event.data?.reason?.kind ?? '?');
			const notices = events
				.filter(
					(event) =>
						event.type === 'agent/inbox/spliced' &&
						JSON.stringify(event.data ?? {}).includes('loop-guard'),
				)
				.map((event) =>
					String(
						event.data?.inserted?.[0]?.source?.summary ??
							event.data?.inserted?.[0]?.source?.form ??
							'',
					).slice(0, 80),
				);
			say(
				'结果',
				JSON.stringify({
					modelCalls: calls,
					turnEnds: ends,
					huanqiNotices: notices,
					status: chat.status,
				}),
			);
			say('ALIVE 程序还活着');
			say('DONE');
		} catch (error) {
			say('FAIL', short(error));
		}
	})();
}
