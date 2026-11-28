// 沙盒探针：只在 VM 的沙盒启动里载入（host-sandbox.sh 放进沙盒设定；不进 release）。
// 等 social runtime 就绪，用真宿主建聊天会话与画室会话，送一则讯息，记下宿主丢的错。
import console from 'node:console';
import { randomUUID } from 'node:crypto';
import process from 'node:process';
import { setTimeout } from 'node:timers';
import { pathToFileURL } from 'node:url';

export const name = 'sandbox-probe';
export const inject = ['agents'];

const RELEASE = process.env.SBX_RELEASE;
const GROUP_CWD = process.env.SBX_GROUP_CWD;
const CHANNEL = '1000000000000000001';
const GUILD = '1000000000000000002';
const AUTHOR = '1000000000000000003';
const say = (...parts) => console.log('[probe]', ...parts);
const short = (error) =>
	String(error?.stack ?? error?.message ?? error)
		.split('\n')
		.slice(0, 3)
		.join(' | ')
		.slice(0, 400);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mod = (path) =>
	import(pathToFileURL(`${RELEASE}/web/node_modules/${path}`).href);

function summarize(agent) {
	const events = agent.session?.snapshotEvents?.() ?? [];
	const types = events.map((event) => event.type);
	const ends = events
		.filter((event) => event.type === 'turn/end')
		.map((event) =>
			JSON.stringify(event.data?.reason ?? event.data).slice(0, 300),
		);
	const errors = events
		.filter((event) => /error|fail/i.test(event.type))
		.map(
			(event) => `${event.type}:${JSON.stringify(event.data).slice(0, 300)}`,
		);
	return {
		count: events.length,
		types: [...new Set(types)].join(','),
		ends,
		errors,
	};
}

export function apply(ctx) {
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
			say('OK social runtime 就绪');
			const llm = await import(
				pathToFileURL(
					'/opt/dsh/app/node_modules/@deepseek-ai/dsh-llm/lib/index.js',
				).href
			);
			const presets = ctx.get('agentPresets');
			const bridge = {
				ctx,
				config: {
					cwd: GROUP_CWD,
					preset: 'whale-social',
					titlePrefix: '[sbx] ',
				},
				sessionId: (id) => (id.startsWith('session-') ? id : `session-${id}`),
				agentOptions: () => ({
					provider: 'opencode-go',
					model: 'deepseek-v4.1-flash',
					maxTokens: 4096,
				}),
				async composeSetup(presetId) {
					if (!presets) return { setup: () => Promise.resolve() };
					const resolvedId = (await presets.resolve(presetId)).id;
					return {
						agentPreset: resolvedId,
						setup: async (agentCtx) => {
							await presets.mount(agentCtx, resolvedId);
						},
					};
				},
				userMessage: (content, source) =>
					llm.createUserMessage({ content, source }),
			};
			// 1. 聊天会话（跟桥接 createSession 同样的建法）。
			const composition = await bridge.composeSetup('whale-social');
			const handle = await ctx.agents.create({
				sessionId: bridge.sessionId(randomUUID()),
				meta: { cwd: GROUP_CWD, agentPreset: composition.agentPreset },
				agentOptions: bridge.agentOptions(),
				setup: composition.setup,
			});
			const chat = handle.agent;
			say('OK 聊天会话建好', chat.id);
			await sleep(2500); // runtime 每秒 mount 一次
			const messageId = String(
				BigInt('1000000000000000100') + BigInt(Date.now() % 1000),
			);
			runtime.admit({
				id: messageId,
				channel_id: CHANNEL,
				guild_id: GUILD,
				author: { id: AUTHOR, username: '沙盒' },
				content: '幫我畫一隻貓',
			});
			runtime.prepare(chat, CHANNEL, messageId);
			say('OK 聊天会话 prepare/mount 没丢错');
			const message = bridge.userMessage(
				[{ type: 'text', text: '幫我畫一隻貓' }],
				{
					kind: 'user',
					discordMessageId: messageId,
					discordChannelId: CHANNEL,
				},
			);
			chat.followup(message);
			await Promise.race([chat.whenIdle(), sleep(20000)]);
			say('聊天会话一轮结果', JSON.stringify(summarize(chat)));
			// 2. 画室：真的 connectStudio + request，宿主建画室会话。
			const { connectStudio } = await mod('dsh-social-runtime/studio.mjs');
			const posted = [];
			const studio = connectStudio({
				runtime,
				bridges: { group: bridge, owner: bridge },
				rest: {
					request: async (method, path, body) => (
						posted.push({ method, path, content: body?.content }),
						{ id: '1' }
					),
				},
				deliveries: () => ({ final: 0, previews: 0 }),
				log: (text) => say('studio-log', text),
			});
			const studioMessage = String(BigInt(messageId) + 1n);
			runtime.admit({
				id: studioMessage,
				channel_id: CHANNEL,
				guild_id: GUILD,
				author: { id: AUTHOR, username: '沙盒' },
				content: '畫一隻貓',
			});
			const result = studio.request({
				channelId: CHANNEL,
				guildId: GUILD,
				messageId: studioMessage,
				authorId: AUTHOR,
				authorName: '沙盒',
				chatSessionId: chat.session.id,
				brief: '畫一隻貓',
			});
			say('studio.request', JSON.stringify(result));
			for (let i = 0; i < 40 && posted.length === 0; i++) await sleep(500);
			const studioIds = [...runtime.studios.keys()];
			say('畫室登记', JSON.stringify(studioIds));
			for (const id of studioIds) {
				const agent = ctx.agents.get(id);
				say(
					'畫室会话',
					id,
					agent ? 'live' : 'missing',
					agent ? JSON.stringify(summarize(agent)) : '',
				);
				if (agent) say('畫室会话 bindings', runtime.bindings?.[id] ?? '(none)');
			}
			say('画室补的话', JSON.stringify(posted));
			studio.stopChannel?.(CHANNEL);
			say('DONE');
		} catch (error) {
			say('FAIL', short(error));
		}
	})();
}
