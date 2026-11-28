// 真模型沙盒探针：只在 VM 的沙盒启动里载入（studio-sandbox.sh 放进沙盒设定；不进 release）。
// 用真宿主、真 social runtime、真模型、真 Krita 闸道跑画室与聊天，Discord 换成假的（送出的东西存进沙盒目录）。
// 不连 Discord、没有 bot token。日志只写情境代码、秒数、工具名与回答文字（都是测试讯息，没有群友资料）。
import console from 'node:console';
import { randomUUID } from 'node:crypto';
import { Buffer } from 'node:buffer';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import process from 'node:process';
import { setTimeout } from 'node:timers';
import { pathToFileURL } from 'node:url';

export const name = 'studio-sandbox-probe';
export const inject = ['agents'];

const RELEASE = process.env.SBX_RELEASE;
const GROUP_CWD = process.env.SBX_GROUP_CWD;
const OUT = process.env.SBX_OUT;
const ONLY = (process.env.SBX_SCENARIOS || 'all').split(',');
const CHANNEL = '1000000000000000001';
const GUILD = '1000000000000000002';
const AUTHOR = '1000000000000000003';
const BOT = '1000000000000000009';
const say = (...parts) => console.log('[sbx]', ...parts);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mod = (path) =>
	import(pathToFileURL(`${RELEASE}/web/node_modules/${path}`).href);
const short = (error) =>
	String(error?.stack ?? error?.message ?? error)
		.split('\n')
		.slice(0, 3)
		.join(' | ')
		.slice(0, 400);
let seq = BigInt('1000000000000001000');
const nextId = () => String(seq++);
const t0 = Date.now();
const secs = (from = t0) => Math.round((Date.now() - from) / 1000);

// 会话里这次以后发生了什么：每个回合怎么结束、叫了哪些工具、思考多长、说了什么。
function digest(agent, fromSeq = 0) {
	const events = (agent.session?.snapshotEvents?.() ?? []).filter(
		(event) => (event.seq ?? 0) >= fromSeq,
	);
	const tools = {};
	let reasoningMax = 0;
	let reasoningSteps = 0;
	const texts = [];
	const ends = [];
	const notices = [];
	for (const event of events) {
		const data = event.data ?? {};
		const message = data.message ?? data;
		if (event.type === 'tool/call') {
			let action = '';
			try {
				action = JSON.parse(data.arguments ?? '{}').action ?? '';
			} catch {
				/* 参数不是 JSON。 */
			}
			const key = data.name + (action ? ':' + action : '');
			tools[key] = (tools[key] ?? 0) + 1;
		}
		if (event.type === 'assistant/message')
			for (const block of message.content ?? []) {
				if (block.type === 'reasoning') {
					reasoningSteps++;
					reasoningMax = Math.max(reasoningMax, (block.text ?? '').length);
				}
				if (block.type === 'text' && block.text?.trim())
					texts.push(block.text.trim().slice(0, 200));
			}
		if (event.type === 'turn/end')
			ends.push(JSON.stringify(data.reason ?? {}).slice(0, 160));
		if (event.type === 'user/message' && message?.source?.kind === 'loop-guard')
			notices.push(message.source.summary ?? 'loop-guard');
	}
	return { tools, reasoningMax, reasoningSteps, texts, ends, notices };
}

const lastSeq = (agent) =>
	(agent.session?.snapshotEvents?.() ?? []).at(-1)?.seq ?? 0;

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
			const studioMod = await mod('dsh-social-runtime/studio.mjs');
			const guard = await mod('dsh-social-runtime/turn-guard.mjs');
			const krita = await mod('dsh-social-runtime/krita-tools.mjs');
			const llm = await import(
				pathToFileURL(
					'/opt/dsh/app/node_modules/@deepseek-ai/dsh-llm/lib/index.js',
				).href
			);
			mkdirSync(OUT, { recursive: true });
			// 假 Discord：图存进沙盒目录，文字只记在日志里。
			const posted = [];
			let pngCount = 0;
			const fakeRest = {
				async request(method, path, body, form) {
					if (method !== 'POST' || !/\/messages$/.test(path)) return [];
					const id = nextId();
					let payload = body ?? {};
					const files = [];
					if (form && typeof form.get === 'function') {
						payload = JSON.parse(String(form.get('payload_json') ?? '{}'));
						for (let i = 0; i < 4; i++) {
							const file = form.get(`files[${i}]`);
							if (!file) break;
							const name = `${String(++pngCount).padStart(2, '0')}-${String(
								payload.content ?? '',
							)
								.replace(/[^\w-]+/g, '_')
								.slice(0, 40)}.png`;
							writeFileSync(
								join(OUT, name),
								Buffer.from(await file.arrayBuffer()),
							);
							files.push(name);
						}
					}
					const row = {
						at: secs(),
						id,
						replyTo: payload.message_reference?.message_id,
						content: String(payload.content ?? '').slice(0, 120),
						files,
					};
					posted.push(row);
					say('discord 送出', JSON.stringify(row));
					return { id, channel_id: path.split('/')[2] };
				},
			};
			runtime.discordRest = fakeRest;
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
			const studio = studioMod.connectStudio({
				runtime,
				bridges: { group: bridge, owner: bridge },
				rest: fakeRest,
				deliveries: (messageId) => krita.kritaDeliveries(runtime, messageId),
				log: (text) => say('studio-log', text),
			});
			const studioEnds = [];
			studio.on((event) => {
				if (event.type === 'end') studioEnds.push(event);
			});

			const newChat = async () => {
				const composition = await bridge.composeSetup('whale-social');
				const handle = await ctx.agents.create({
					sessionId: bridge.sessionId(randomUUID()),
					meta: { cwd: GROUP_CWD, agentPreset: composition.agentPreset },
					agentOptions: bridge.agentOptions(),
					setup: composition.setup,
				});
				await sleep(2500); // runtime 每秒 mount 一次
				return handle.agent;
			};
			// 一则群聊 @：照桥接的格式送进聊天会话，等这一轮（awaitSocialTurn），取回答（foldSocialReply）。
			const clock = () =>
				new Intl.DateTimeFormat('en-GB', {
					timeZone: 'Asia/Taipei',
					hour: '2-digit',
					minute: '2-digit',
					hourCycle: 'h23',
				}).format(new Date());
			const send = (chat, content) => {
				const messageId = nextId();
				runtime.admit({
					id: messageId,
					channel_id: CHANNEL,
					guild_id: GUILD,
					author: { id: AUTHOR, username: '沙盒' },
					content: `<@${BOT}> ${content}`,
				});
				runtime.prepare(chat, CHANNEL, messageId);
				const text = [
					'（#沙盒頻道｜沙盒伺服器，群聊新訊息，2026/11/28（週六）。要回的是標「← 叫你」的那則，前面的只是上下文；更早的訊息你都回過了，不要再回。）',
					`（這段出現的人的 Discord ID：沙盒 ${AUTHOR}）`,
					`#${messageId.slice(-4)} ${clock()} 沙盒：<@${BOT}> ${content}  ← 叫你`,
				].join('\n');
				const prompt = guard.socialFollowup(
					chat,
					bridge.userMessage([{ type: 'text', text }], {
						kind: 'user',
						discordMessageId: messageId,
						discordChannelId: CHANNEL,
					}),
				);
				return { messageId, prompt, sentAt: Date.now() };
			};
			const answer = async (chat, sent) => {
				const wait = await guard.awaitSocialTurn(chat, sent.prompt, {
					startedAt: sent.sentAt,
				});
				const events = chat.session.snapshotEvents();
				const seqOf = guard.socialPromptSeq(events, sent.prompt);
				const reply =
					seqOf === undefined
						? { text: '', reasonKind: 'lost' }
						: guard.foldSocialReply(events, seqOf);
				return {
					status: wait.status,
					secs: Math.round((Date.now() - sent.sentAt) / 1000),
					text: reply.text.trim().slice(0, 200),
					reason: reply.reasonKind,
				};
			};
			const studioDone = async (messageId, limitMs) => {
				const until = Date.now() + limitMs;
				while (Date.now() < until) {
					const end = studioEnds.find((event) => event.messageId === messageId);
					if (end) return end;
					await sleep(2000);
				}
				return undefined;
			};
			const studioAgentFor = (chat) => {
				for (const [id, chatId] of runtime.studios)
					if (chatId === chat.session.id) return ctx.agents.get(id);
				return undefined;
			};
			const results = [];
			const record = (row) => {
				results.push(row);
				say('结果', JSON.stringify(row));
			};

			// 画一张小鯨魚：聊天交给画室，画室真的用 Krita 画完交付。
			async function drawWhale(code, effort, disturb) {
				// effort 只给实验用（思考深度）；现行版本不改。
				if (effort) runtime.studioReasoning = effort;
				const chat = await newChat();
				const started = Date.now();
				const sent = send(chat, '可以幫我畫一隻小鯨魚嗎');
				const chatTurn = await answer(chat, sent);
				const chatDigest = digest(chat);
				say(
					code,
					'聊天回合',
					JSON.stringify({ ...chatTurn, tools: chatDigest.tools }),
				);
				let disturbed = '';
				if (disturb) {
					// 等画室真的开始动手（叫过一次 krita_batch）再干扰。
					for (let i = 0; i < 240; i++) {
						const agent = studioAgentFor(chat);
						if (agent && digest(agent).tools['krita_batch:start']) break;
						await sleep(1000);
					}
					const agent = studioAgentFor(chat);
					if (agent) disturbed = await disturb(agent);
				}
				const end = await studioDone(sent.messageId, 12 * 60_000);
				const agent = studioAgentFor(chat);
				const studioDigest = agent ? digest(agent) : {};
				const pngs = posted
					.filter((row) => row.replyTo === sent.messageId && row.files.length)
					.map((row) => row.files.join(','));
				record({
					code,
					effort,
					disturbed,
					secs: secs(started),
					chatSecs: chatTurn.secs,
					chatText: chatTurn.text,
					chatTools: chatDigest.tools,
					outcome: end?.outcome ?? 'no-end',
					studioTools: studioDigest.tools,
					reasoningMax: studioDigest.reasoningMax,
					reasoningSteps: studioDigest.reasoningSteps,
					studioEnds: studioDigest.ends,
					studioNoticesSeen: studioDigest.notices,
					studioTexts: studioDigest.texts?.slice(-3),
					pngs,
				});
				return { chat, sent, end };
			}

			const want = (code) => ONLY.includes('all') || ONLY.includes(code);
			// 1. 画一张小鯨魚并交出图（换气额度 0，跟 2026-11-28 正式环境一样）。
			if (want('draw')) await drawWhale('draw');
			// 实验用：思考深度（这条路线 off 与 low 实测一样，没采用，见 typing-hang-port.md）。
			if (want('draw-low')) await drawWhale('draw-low', 'low');
			if (want('draw-off')) await drawWhale('draw-off', 'off');
			// 2. 画图途中出现换气提示（照 huanqi 真的文字与来源），还有一次聊天回合那种 hook 中止：仍要画完。
			if (want('draw-huanqi'))
				await drawWhale('draw-huanqi', undefined, async (agent) => {
					const notice = (text, summary) =>
						llm.createUserMessage({
							content: [{ type: 'text', text }],
							source: { kind: 'loop-guard', form: 'notice', summary },
						});
					const breaker =
						'〔换气〕延续额度已耗尽、思考已熔断（连续 6 次都没产出正文），本轮不再自动续跑。现在只做三件事，别的一概不做：① 把当前进度落盘；② 在账本里加一条索引；③ 落完就停，告诉使用者「需要手动压缩」。不要再开新任务、不要再探索。';
					agent.inject(notice(breaker, 'huanqi: circuit break'));
					agent.steer(
						notice(
							breaker,
							'huanqi: land your state, then ask the user to compact',
						),
					);
					await sleep(4000);
					agent.cancel(
						{ kind: 'hook', reason: 'discord-turn-answered' },
						{ keepInbox: true },
					);
					return 'inject+steer+hook-cancel';
				});
			// 3. 画室失败以后再聊一般话题：不能被交给画室。
			if (want('after-fail')) {
				const chat = await newChat();
				const deadline = studio.deadlineMs;
				studio.deadlineMs = 15_000; // 让这张一定画不完（timeout），画室回「畫太久了」
				const sent = send(chat, '可以幫我畫一隻小海豚嗎');
				const first = await answer(chat, sent);
				const end = await studioDone(sent.messageId, 3 * 60_000);
				studio.deadlineMs = deadline;
				const accepted = () =>
					studio.recent.length +
					(studio.running ? 1 : 0) +
					studio.waiting.length;
				for (const [code, content] of [
					['after-fail-color', '你最喜歡什麼顏色'],
					['after-fail-chat', '你畫畫的時候還能聊天嗎'],
				]) {
					const before = accepted();
					const from = lastSeq(chat);
					const turn = await answer(chat, send(chat, content));
					const d = digest(chat, from);
					record({
						code,
						firstOutcome: end?.outcome ?? 'no-end',
						firstChat: first.text,
						secs: turn.secs,
						status: turn.status,
						text: turn.text,
						tools: d.tools,
						studioAccepted: accepted() - before,
					});
				}
			}
			// 4. 连 @ 两则：桥接按频道串行，第二则在第一则答完后马上轮到（跟正式环境一样），两则都要有正常回答。
			if (want('double')) {
				const chat = await newChat();
				const a = send(chat, '晚餐吃什麼好');
				const ra = await answer(chat, a);
				const b = send(chat, '想吃點熱的');
				const rb = await answer(chat, b);
				record({ code: 'double', a: ra, b: rb, tools: digest(chat).tools });
			}
			// 6. 要不要画（部署前审查）：「先不要畫」「請問你是怎麼畫畫的」不交给画室；「幫我做一張頭像」交给画室并画完。
			// 工具结果只记 draw_request 的（看程式有没有挡、挡的理由）。
			if (want('intent')) {
				const chat = await newChat();
				const accepted = () =>
					studio.recent.length +
					(studio.running ? 1 : 0) +
					studio.waiting.length;
				const drawResults = (from) =>
					chat.session
						.snapshotEvents()
						.filter(
							(event) =>
								(event.seq ?? 0) >= from &&
								event.type === 'tool/result' &&
								JSON.stringify(event.data ?? {}).includes('畫'),
						)
						.map((event) => JSON.stringify(event.data ?? {}).slice(0, 160));
				for (const [code, content] of [
					['intent-dont', '先不要畫'],
					['intent-how', '請問你是怎麼畫畫的'],
				]) {
					const before = accepted();
					const from = lastSeq(chat);
					const turn = await answer(chat, send(chat, content));
					record({
						code,
						secs: turn.secs,
						status: turn.status,
						text: turn.text,
						tools: digest(chat, from).tools,
						drawResults: drawResults(from),
						studioAccepted: accepted() - before,
					});
				}
				for (const content of ['幫我做一張頭像', '小鯨魚的頭像，幫我做一張']) {
					const before = accepted();
					const from = lastSeq(chat);
					const sent = send(chat, content);
					const turn = await answer(chat, sent);
					const handed = accepted() - before;
					const end = handed
						? await studioDone(sent.messageId, 12 * 60_000)
						: undefined;
					record({
						code: 'intent-avatar',
						content,
						secs: turn.secs,
						text: turn.text,
						tools: digest(chat, from).tools,
						drawResults: drawResults(from),
						studioAccepted: handed,
						outcome: end?.outcome ?? 'not-handed',
						pngs: posted
							.filter(
								(row) => row.replyTo === sent.messageId && row.files.length,
							)
							.map((row) => row.files.join(',')),
					});
					// 她先问要什么样的头像（没交给画室、程式也没挡）时，才再说清楚一次。
					if (handed) break;
				}
			}
			// 5. 背景整理记忆（2026-11-25 改走 opencode-go 以后每批都失败）：照 consolidate 的参数直接问一次模型，
			// 看它回了什么（思考多长、正文多长、怎么结束），再跑一次真的 consolidate。
			if (want('consolidate')) {
				const selection = ctx.get('agentDefaultModel')?.currentSelection?.();
				const kinds = {};
				let reasoning = 0;
				let text = '';
				let finish;
				try {
					for await (const chunk of ctx.get('llm').stream({
						provider: selection?.provider,
						model: selection?.model,
						reasoningEffort: 'off',
						maxTokens: 800,
						system:
							'只輸出 JSON：{"people":[],"journal":[],"facts":[]}，把對話裡值得記的事整理進去。',
						messages: [
							{
								role: 'user',
								content: [
									{
										type: 'text',
										text: '對話：\n沙盒：我今天去爬山了，腳好痠\n你：辛苦了 泡個腳吧\n沙盒：下週還要去合歡山',
									},
								],
							},
						],
					})) {
						kinds[chunk.type] = (kinds[chunk.type] ?? 0) + 1;
						if (chunk.type === 'reasoning-delta')
							reasoning += chunk.text.length;
						if (chunk.type === 'text-delta') text += chunk.text;
						if (chunk.type === 'finish') finish = chunk.reason;
					}
				} catch (error) {
					finish = { thrown: short(error) };
				}
				let consolidated;
				try {
					const now = Date.now();
					const lines = [
						{
							role: 'user',
							speaker: AUTHOR,
							name: '沙盒',
							text: '我今天去爬山了，腳好痠',
							id: nextId(),
							at: now - 60_000,
						},
						{
							role: 'assistant',
							text: '辛苦了 泡個腳吧',
							id: nextId(),
							at: now - 50_000,
						},
						{
							role: 'user',
							speaker: AUTHOR,
							name: '沙盒',
							text: '下週還要去合歡山',
							id: nextId(),
							at: now - 40_000,
						},
					];
					const value = await runtime.consolidate(lines, '#沙盒頻道');
					consolidated = {
						ok: true,
						value: JSON.stringify(value).slice(0, 300),
					};
				} catch (error) {
					consolidated = { ok: false, error: short(error) };
				}
				record({
					code: 'consolidate',
					kinds,
					reasoningChars: reasoning,
					textChars: text.length,
					text: text.slice(0, 200),
					finish: JSON.stringify(finish).slice(0, 300),
					consolidated,
				});
			}
			say('SUMMARY', JSON.stringify(results));
			say('DONE');
		} catch (error) {
			say('FAIL', short(error));
		}
		// 跑完就收掉沙盒（不等 timeout），把记忆体还回去。
		setTimeout(() => process.kill(process.pid, 'SIGTERM'), 3000).unref?.();
	})();
}
