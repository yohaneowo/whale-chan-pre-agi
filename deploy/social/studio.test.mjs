// 背景画室（studio.mjs）与 turn-guard 第 2 轮修正的单元测试。
// 画室：同一时间只画一张、其余排队并告知、失败或超时回一句、/stop 与取消、
// draw_request 只认宿主核对过的本轮来源；turn-guard：同一则讯息共用期限与停止讯号、
// 工具送讯照「是哪则讯息送出的」记帐、「正在输入」熄灯后不重送。
// 第 3 轮审查：每一段等待都有期限、请求被删时安静停掉且不贴成一般群讯、工具送讯照发送来源归帐。
import assert from 'node:assert/strict';
import test from 'node:test';

const flush = async (rounds = 10) => {
	for (let index = 0; index < rounds; index++) await Promise.resolve();
};
const ids = (n) => String(1000000000000000000n + BigInt(n));
const job = (n, extra = {}) => ({
	channelId: ids(1),
	messageId: ids(100 + n),
	authorId: ids(200 + n),
	chatSessionId: 'session-chat',
	brief: '畫一隻貓',
	...extra,
});
// 可以手动触发的计时器。
function manualTimers() {
	const rows = [];
	const timer = (run, ms) => {
		const row = { run, ms, cleared: false };
		rows.push(row);
		return () => {
			row.cleared = true;
		};
	};
	return {
		rows,
		timer,
		fire: (ms) =>
			rows
				.filter((row) => row.ms === ms && !row.cleared)
				.forEach((row) => ((row.cleared = true), row.run())),
	};
}

test('画室：同一时间只画一张，第二张排队并告知，前一张画完才画它', async () => {
	const { Studio, STUDIO_TEXT } = await import('./studio.mjs');
	const runs = [];
	const studio = new Studio({
		run: (item, signal) =>
			new Promise((resolve) => runs.push({ item, signal, resolve })),
		notify: async () => assert.fail('画好了不该补话'),
		timer: manualTimers().timer,
	});
	const events = [];
	studio.on((event) => events.push(event));
	const first = studio.request(job(1));
	const second = studio.request(job(2));
	assert.equal(first.status, 'started');
	assert.equal(first.text, STUDIO_TEXT.started);
	assert.equal(second.status, 'queued');
	assert.equal(second.ahead, 1);
	assert.equal(second.text, '前面還有一張，畫完就畫你的');
	await flush();
	assert.equal(runs.length, 1, '同一时间只画一张');
	runs[0].resolve({ delivered: 'final', status: 'answered' });
	await flush();
	assert.equal(runs.length, 2, '前一张画完才开始下一张');
	assert.equal(runs[1].item.messageId, ids(102));
	runs[1].resolve({ delivered: 'final', status: 'answered' });
	await studio.idle;
	assert.deepEqual(
		events
			.filter((event) => event.type === 'accepted')
			.map((event) => event.status),
		['started', 'queued'],
	);
	assert.deepEqual(
		events
			.filter((event) => event.type === 'end')
			.map((event) => event.outcome),
		['delivered', 'delivered'],
	);
});

test('画室：没画成回一句说明（失败、只有预览、超时）；超时不回来就放弃它，换下一张', async () => {
	const { Studio, STUDIO_TEXT } = await import('./studio.mjs');
	const notes = [];
	const timers = manualTimers();
	const results = [
		() => Promise.reject(new Error('坏了')),
		() => Promise.resolve({ delivered: 'preview', status: 'settled' }),
		() => new Promise(() => {}),
		() => Promise.resolve({ delivered: 'final' }),
	];
	let index = 0;
	const studio = new Studio({
		run: () => results[index++](),
		notify: async (item, text) => notes.push([item.messageId, text]),
		timer: timers.timer,
		deadlineMs: 600_000,
		graceMs: 30_000,
		log: () => {},
	});
	for (let n = 1; n <= 4; n++)
		studio.request(job(n, { authorId: ids(300 + n) }));
	await flush(30);
	// 第三张卡住：期限到先发取消，再等 30 秒还不回来就放弃。
	timers.fire(600_000);
	await flush();
	timers.fire(30_000);
	await flush(30);
	await studio.idle;
	assert.deepEqual(notes, [
		[ids(101), STUDIO_TEXT.failed],
		[ids(102), STUDIO_TEXT.partial],
		[ids(103), STUDIO_TEXT.timeout],
	]);
	assert.equal(index, 4, '放弃卡住的那张以后继续画下一张');
});

test('画室：同一个人再交代一次就改成新的要求；取消只动自己的；/stop 停掉整个频道，都不补话', async () => {
	const { Studio } = await import('./studio.mjs');
	const runs = [];
	const notes = [];
	const studio = new Studio({
		run: (item, signal) =>
			new Promise((resolve) => {
				runs.push({ item, signal });
				signal.addEventListener('abort', () =>
					resolve({ delivered: 'none', status: 'cancelled' }),
				);
			}),
		notify: async (item, text) => notes.push(text),
		timer: manualTimers().timer,
	});
	studio.request(job(1));
	studio.request(job(2));
	// 2 号还在排队：改成新的要求，位置不变。
	const replaced = studio.request(
		job(2, { messageId: ids(122), brief: '改畫狗' }),
	);
	assert.equal(replaced.status, 'queued');
	assert.equal(studio.waiting.length, 1);
	assert.equal(studio.waiting[0].brief, '改畫狗');
	// 别人不能取消 1 号；1 号本人可以。
	assert.equal(studio.cancel({ channelId: ids(1), authorId: ids(999) }), 0);
	await flush();
	assert.equal(studio.cancel({ channelId: ids(1), authorId: ids(201) }), 1);
	assert.equal(runs[0].signal.aborted, true);
	await flush(20);
	assert.equal(runs[1]?.item.brief, '改畫狗');
	// 1 号本人正在画的时候又交代：停掉旧的、马上画新的。
	const again = studio.request(
		job(2, { messageId: ids(123), brief: '改畫兔子' }),
	);
	assert.equal(again.status, 'started');
	assert.equal(runs[1].signal.aborted, true);
	await flush(20);
	assert.equal(runs[2]?.item.brief, '改畫兔子');
	studio.request(job(5));
	assert.equal(studio.stopChannel(ids(1)), 2);
	await flush(20);
	await studio.idle;
	assert.deepEqual(notes, [], '取消、改要求、/stop 都不补「卡住了」');
	assert.equal(studio.request(job(9, { brief: '' })).status, 'invalid');
});

test('画室：排满了就请对方晚点再叫', async () => {
	const { Studio, STUDIO_TEXT } = await import('./studio.mjs');
	const studio = new Studio({
		run: () => new Promise(() => {}),
		notify: async () => {},
		timer: manualTimers().timer,
		maxWaiting: 2,
	});
	for (let n = 1; n <= 3; n++) studio.request(job(n));
	const full = studio.request(job(4));
	assert.equal(full.status, 'full');
	assert.equal(full.text, STUDIO_TEXT.full);
});

test('draw_request：请求者与要回覆的讯息取自宿主核对过的本轮来源；画室会话自己不能再交代画图', async () => {
	const { drawRequestTool, Studio } = await import('./studio.mjs');
	const requests = [];
	const source = {
		messageId: ids(101),
		channelId: ids(1),
		guildId: ids(2),
		authorId: ids(201),
		authorName: '小羅',
		referenceAttachments: [{ id: ids(55) }],
	};
	const agent = { id: 'session-chat', session: { id: 'session-chat' } };
	const studio = new Studio({
		run: () => new Promise(() => {}),
		notify: async () => {},
		timer: manualTimers().timer,
	});
	const original = studio.request.bind(studio);
	studio.request = (input) => (requests.push(input), original(input));
	const runtime = {
		sourceForTurn: () => source,
		turns: new Map([[agent.id, source]]),
		studios: new Map(),
		studio,
	};
	// 照正式宿主的规则登记（少了 output 会丢错），每次回传值都照 output.schema 验过、render 得出来。
	const { hostTools, checkToolResult } = await import('./host-contract.mjs');
	const definition = drawRequestTool(runtime);
	hostTools().register(definition);
	const tool = {
		execute: async (args, exec) => {
			const value = await definition.execute(args, exec);
			checkToolResult(definition, args, value);
			return value;
		},
	};
	const started = await tool.execute(
		{ action: 'start', brief: '貓', messageId: ids(999) },
		{ agent },
	);
	assert.equal(started.ok, true);
	assert.match(started.message, /好，我去畫/);
	assert.equal(
		requests[0].messageId,
		ids(101),
		'回覆的是本轮那则，不是模型参数',
	);
	assert.equal(requests[0].authorId, ids(201));
	assert.deepEqual(requests[0].attachmentIds, [ids(55)]);
	const queued = await tool.execute(
		{ action: 'start', brief: '狗' },
		{ agent: { ...agent, id: 'other' } },
	);
	assert.equal(queued.ok, false, '本轮来源核对不上就不收');
	runtime.turns.set('other', {
		...source,
		messageId: ids(102),
		authorId: ids(202),
	});
	runtime.sourceForTurn = (current) => runtime.turns.get(current.id);
	const second = await tool.execute(
		{ action: 'start', brief: '狗' },
		{ agent: { id: 'other', session: { id: 's2' } } },
	);
	assert.match(second.message, /前面還有一張，畫完就畫你的/);
	runtime.studios.set(agent.id, 'session-x');
	const fromStudio = await tool.execute(
		{ action: 'start', brief: '貓' },
		{ agent },
	);
	assert.equal(fromStudio.ok, false, '画室会话不能再交代画图');
	runtime.studios.clear();
	runtime.sourceForTurn = () => source;
	const cancelled = await tool.execute({ action: 'cancel' }, { agent });
	assert.match(cancelled.message, /已取消 1 張/);
	assert.match(
		(await tool.execute({ action: 'status' }, { agent })).message,
		/這個頻道/,
	);
	delete runtime.studio;
	assert.equal(
		(await tool.execute({ action: 'start', brief: '貓' }, { agent })).ok,
		false,
	);
});

test('connectStudio：画室会话先登记再建立、每个聊天会话一个、绑定原本那则请求、看 Krita 实际交付', async () => {
	const { connectStudio, STUDIO_TEXT } = await import('./studio.mjs');
	const created = [];
	const posted = [];
	const runtime = { prepared: [] };
	runtime.prepare = (agent, channelId, messageId) =>
		runtime.prepared.push([agent.id, channelId, messageId]);
	const makeAgent = (sessionId) => {
		const agent = {
			id: sessionId,
			events: [],
			inbox: { nextTurn: [], nextStep: [], remove: () => false },
			session: {
				id: sessionId,
				header: {},
				snapshotEvents: () => agent.events,
			},
			followup(message) {
				agent.events.push({
					seq: agent.events.length + 1,
					type: 'user/message',
					data: message,
				});
				agent.events.push({
					seq: agent.events.length + 1,
					type: 'assistant/message',
					data: { message: { content: [{ type: 'text', text: '畫好了' }] } },
				});
				agent.events.push({
					seq: agent.events.length + 1,
					type: 'turn/end',
					data: { reason: { kind: 'completed' } },
				});
			},
			whenIdle: async () => {},
			cancel() {},
		};
		return agent;
	};
	const bridge = {
		config: {
			preset: 'whale-social',
			cwd: '/srv/dsh/discord',
			titlePrefix: '',
		},
		composeSetup: async () => ({
			agentPreset: 'whale-social',
			setup: async () => {},
		}),
		sessionId: (raw) => 'session-' + raw,
		agentOptions: () => ({ maxTokens: 4096 }),
		userMessage: (content, source) => ({
			id: 'u-' + posted.length + '-' + Math.random(),
			role: 'user',
			content,
			source,
		}),
		ctx: {
			agents: {
				create: async (options) => {
					assert.ok(
						runtime.studios.has(options.sessionId),
						'建立前就登记成画室',
					);
					assert.equal(
						options.meta.parentSession,
						undefined,
						'不设 parentSession（history-view 会拒）',
					);
					assert.equal(options.meta.origin, undefined);
					created.push(options);
					return { agent: makeAgent(options.sessionId) };
				},
			},
		},
	};
	let final = 1;
	const studio = connectStudio({
		runtime,
		bridges: { group: bridge, owner: bridge },
		rest: {
			request: async (method, path, body, form, signal) =>
				posted.push([
					method,
					path,
					body,
					form,
					signal instanceof globalThis.AbortSignal,
				]),
		},
		toGroupText: async (text) => text + '（繁）',
		deliveries: () => ({ final, previews: 0 }),
		timer: manualTimers().timer,
	});
	assert.equal(runtime.studio, studio);
	studio.request(job(1, { guildId: ids(2) }));
	await flush(40);
	await studio.idle;
	studio.request(job(2, { guildId: ids(2), authorId: ids(777) }));
	final = 0;
	await flush(40);
	await studio.idle;
	assert.equal(created.length, 1, '同一个聊天会话共用一个画室会话');
	assert.deepEqual(
		runtime.prepared.map((row) => row.slice(1)),
		[
			[ids(1), ids(101)],
			[ids(1), ids(102)],
		],
	);
	const prompt = created.length && runtime.studios.get(created[0].sessionId);
	assert.equal(prompt, 'session-chat');
	// 第二张 Krita 没交付：回覆原本那则，群聊转繁体；那则被删了就让 Discord 拒绝，不掉成一般群讯。
	assert.deepEqual(posted, [
		[
			'POST',
			`/channels/${ids(1)}/messages`,
			{
				content: STUDIO_TEXT.failed + '（繁）',
				allowed_mentions: { parse: [] },
				message_reference: { message_id: ids(102), fail_if_not_exists: true },
			},
			undefined,
			true,
		],
	]);
});

test('同一则讯息自动续做：期限与停止讯号沿用第一轮，不重新计时', async () => {
	const { runSocialTurn, TURN_DEADLINE_NOTICE } =
		await import('./turn-guard.mjs');
	let clock = 1_000_000;
	const now = () => clock;
	const timers = manualTimers();
	const beginTyping = () => () => {};
	const first = await runSocialTurn(
		{
			channelId: 'c9',
			messageId: 'm9',
			beginTyping,
			deadlineMs: 280_000,
			timer: timers.timer,
			now,
		},
		async (budget) => {
			clock += 200_000;
			return { chunks: ['我查一下'], files: [], startedAt: budget.startedAt };
		},
	);
	assert.deepEqual(first.chunks, ['我查一下']);
	// 第二轮（同一则讯息）：只剩 80 秒。
	let second;
	const running = runSocialTurn(
		{
			channelId: 'c9',
			messageId: 'm9',
			beginTyping,
			deadlineMs: 280_000,
			timer: timers.timer,
			now,
		},
		(budget) => {
			second = budget;
			return new Promise(() => {});
		},
	);
	await flush();
	assert.equal(second.startedAt, 1_000_000, '期限从第一轮开始算');
	const deadline = timers.rows.filter((row) => !row.cleared).at(-1);
	assert.equal(deadline.ms, 80_000);
	deadline.run();
	assert.equal(second.signal.aborted, true);
	timers.rows
		.filter((row) => !row.cleared)
		.at(-1)
		.run();
	assert.deepEqual((await running).chunks, [TURN_DEADLINE_NOTICE]);
});

test('/stop：正在跑与已经排进队列的讯息都停，不再送答；之后来的不受影响', async () => {
	const { runSocialTurn, stopSocialTurns, socialTurnStopped } =
		await import('./turn-guard.mjs');
	const timers = manualTimers();
	const beginTyping = () => () => {};
	let clock = 5_000_000;
	const now = () => clock;
	const queuedAt = clock;
	let running;
	const first = runSocialTurn(
		{
			channelId: 'c7',
			messageId: 'm71',
			beginTyping,
			timer: timers.timer,
			now,
			queuedAt,
		},
		(budget) => {
			running = budget;
			return new Promise((resolve) =>
				budget.signal.addEventListener('abort', () =>
					resolve({ chunks: ['做完了'], files: [] }),
				),
			);
		},
	);
	await flush();
	clock += 1_000;
	assert.equal(stopSocialTurns('c7', now), 1);
	assert.equal(running.stopped, true);
	const outcome = await first;
	assert.deepEqual(
		outcome,
		{ chunks: [], files: [], stopped: true },
		'停掉以后连已经拿到的答覆也不送',
	);
	assert.equal(socialTurnStopped('c7', 'm71'), true);
	// 在 /stop 之前就排进队列、之后才轮到的：一开始就是停的。
	const queued = await runSocialTurn(
		{
			channelId: 'c7',
			messageId: 'm72',
			beginTyping,
			timer: timers.timer,
			now,
			queuedAt,
		},
		async (budget) => ({
			chunks: [budget.signal.aborted ? 'aborted' : '跑了'],
			files: [],
		}),
	);
	assert.equal(queued.stopped, true);
	clock += 1_000;
	const later = await runSocialTurn(
		{
			channelId: 'c7',
			messageId: 'm73',
			beginTyping,
			timer: timers.timer,
			now,
			queuedAt: clock,
		},
		async () => ({ chunks: ['好'], files: [] }),
	);
	assert.deepEqual(later.chunks, ['好']);
});

test('工具送讯照「是哪则讯息送出的」记帐：挂回复算被回复的那则；没挂回复看工具执行时记下的来源', async () => {
	const { runSocialTurn, runWithSocialSendOrigin, trackSocialSends } =
		await import('./turn-guard.mjs');
	const timers = manualTimers();
	const beginTyping = () => () => {};
	const sent = [];
	let release;
	const slow = new Promise((resolve) => (release = resolve));
	const rest = trackSocialSends(
		{
			request: async (method, path, body) => {
				if (body?.content === 'slow') await slow;
				return { id: 's-' + sent.length + '-' + (body?.content ?? 'form') };
			},
		},
		(channelId, messageId) => sent.push([channelId, messageId]),
	);
	const origin = (messageId) => ({
		sessionId: 'chat',
		channelId: '51',
		messageId,
	});
	let late;
	await runSocialTurn(
		{ channelId: '51', messageId: 'A', beginTyping, timer: timers.timer },
		async () => {
			late = runWithSocialSendOrigin(origin('A'), () =>
				rest.request('POST', '/channels/51/messages', { content: 'slow' }),
			);
			return { chunks: [], files: [] };
		},
	);
	await runSocialTurn(
		{ channelId: '51', messageId: 'B', beginTyping, timer: timers.timer },
		async () => {
			release();
			await late;
			// 挂了回复（Krita 交付）：算被回复的那则，不管来源是谁。
			const form = new globalThis.FormData();
			form.append(
				'payload_json',
				JSON.stringify({
					message_reference: { message_id: '1000000000000000042' },
				}),
			);
			await runWithSocialSendOrigin(origin('B'), () =>
				rest.request('POST', '/channels/51/messages', {}, form),
			);
			await runWithSocialSendOrigin(origin('B'), () =>
				rest.request('POST', '/channels/51/messages', { content: 'b' }),
			);
			// 没有来源、也没挂回复（不是工具送的）：不归给此刻在跑的 B。
			await rest.request('POST', '/channels/51/messages', { content: '?' });
			// 来源是别的频道：不算。
			await runWithSocialSendOrigin(
				{ sessionId: 'chat', channelId: '99', messageId: 'B' },
				() => rest.request('POST', '/channels/51/messages', { content: 'x' }),
			);
			return { chunks: [], files: [] };
		},
	);
	assert.deepEqual(sent, [
		['51', 'A'],
		['51', '1000000000000000042'],
		['51', 'B'],
	]);
});

test('A 的工具跨轮才发出、没挂回复：照发送来源算给 A，不算给此刻在跑的 B（第 3 轮审查）', async () => {
	const { runSocialTurn, socialSendOriginTool, trackSocialSends } =
		await import('./turn-guard.mjs');
	const timers = manualTimers();
	const sent = [];
	const rest = trackSocialSends(
		{ request: async () => ({ id: 'sent-1' }) },
		(channelId, messageId) => sent.push(messageId),
	);
	// runtime 注册工具时包的那一层：执行一开始问宿主「现在在跑哪则」。
	let current = 'A';
	let go;
	const ready = new Promise((resolve) => (go = resolve));
	const tool = socialSendOriginTool(
		{
			name: 'discord_send_message',
			async execute(args, exec) {
				assert.equal(this.name, 'discord_send_message', '保留原本的 this');
				assert.equal(exec.agent.id, 'chat');
				await ready;
				return rest.request('POST', '/channels/51/messages', {
					content: args.content,
				});
			},
		},
		(exec) => ({
			sessionId: exec.agent.id,
			channelId: '51',
			messageId: current,
		}),
	);
	const pending = tool.execute({ content: '工具A' }, { agent: { id: 'chat' } });
	current = 'B';
	await runSocialTurn(
		{
			channelId: '51',
			messageId: 'B',
			beginTyping: () => () => {},
			timer: timers.timer,
		},
		async () => {
			// 送出那一刻桥接在跑 B（送出前后都是 B）。
			go();
			await pending;
			return { chunks: [], files: [] };
		},
	);
	assert.deepEqual(sent, ['A']);
	// 来源取不到（宿主出错）也照常执行，只是不归给任何一则。
	const broken = socialSendOriginTool(
		{
			name: 't',
			execute: () =>
				rest.request('POST', '/channels/51/messages', { content: 'y' }),
		},
		() => {
			throw new Error('坏了');
		},
	);
	await broken.execute({}, { agent: { id: 'chat' } });
	assert.deepEqual(sent, ['A']);
});

test('「正在输入」：熄灯后不再送，送到一半的也取消；不在 REST 里等限流重送', async () => {
	const { socialTyping } = await import('./turn-guard.mjs');
	const requests = [];
	let tick;
	const stop = socialTyping(
		{ request: async (...args) => requests.push(args) },
		'61',
		8_000,
		{
			every: (run) => ((tick = run), 7),
			clear: (handle) => assert.equal(handle, 7),
		},
	);
	await flush();
	assert.equal(requests.length, 1);
	const [method, path, body, form, signal] = requests[0];
	assert.deepEqual(
		[method, path, body, form],
		['POST', '/channels/61/typing', undefined, undefined],
	);
	assert.ok(
		signal instanceof globalThis.AbortSignal,
		'带 onceOnly 信号：429 不在 REST 里重送',
	);
	tick();
	await flush();
	assert.equal(requests.length, 2);
	stop();
	assert.equal(signal.aborted, true, '熄灯时取消送到一半的那次');
	tick();
	await flush();
	assert.equal(requests.length, 2, '熄灯后不再送');
});

test('画室判断不该画（最后回「不畫」）：照实说不画，不说「卡住了」', async () => {
	const { Studio, runStudioJob, STUDIO_TEXT } = await import('./studio.mjs');
	const notes = [];
	const events = [];
	const agent = { session: { snapshotEvents: () => events } };
	const deps = {
		acquire: async () => agent,
		bind: () => {},
		followup: () => {
			const prompt = { id: 'p1', source: { kind: 'user' } };
			events.push(
				{ seq: 1, type: 'user/message', data: prompt },
				{
					seq: 2,
					type: 'assistant/message',
					data: { message: { content: [{ type: 'text', text: '不畫' }] } },
				},
				{ seq: 3, type: 'turn/end', data: { reason: { kind: 'completed' } } },
			);
			return prompt;
		},
		wait: async () => ({ status: 'answered' }),
		deliveries: () => ({ final: 0, previews: 0 }),
	};
	const studio = new Studio({
		run: (item, signal) => runStudioJob(deps, item, signal),
		notify: async (item, text) => notes.push(text),
		timer: manualTimers().timer,
	});
	studio.request(job(1));
	await flush(20);
	await studio.idle;
	assert.deepEqual(notes, [STUDIO_TEXT.declined]);
});

test('画室：补的那句一直送不出去也有期限，到了就放弃（只记日志），下一张照画（第 3 轮审查）', async () => {
	const { Studio, STUDIO_TEXT } = await import('./studio.mjs');
	const timers = manualTimers();
	const logs = [];
	const runs = [];
	const signals = [];
	const studio = new Studio({
		run: (item) =>
			new Promise((resolve) => {
				runs.push(item.messageId);
				resolve(
					runs.length === 1
						? { delivered: 'none', status: 'settled' }
						: { delivered: 'final', status: 'answered' },
				);
			}),
		// Discord 一直不回。
		notify: (item, text, signal) => {
			assert.equal(text, STUDIO_TEXT.failed);
			signals.push(signal);
			return new Promise(() => {});
		},
		timer: timers.timer,
		notifyTimeoutMs: 45_000,
		log: (line) => logs.push(line),
	});
	studio.request(job(1));
	studio.request(job(2));
	await flush(30);
	assert.deepEqual(runs, [ids(101)], '补话还没放弃前，下一张还没开始');
	timers.fire(45_000);
	await flush(30);
	await studio.idle;
	assert.equal(signals[0].aborted, true, '放弃时也取消送到一半的请求');
	assert.deepEqual(runs, [ids(101), ids(102)], '补话卡住不影响下一张');
	assert.ok(
		logs.some((line) =>
			line.startsWith(
				`畫室 结果 #${ids(101)} status=failed turn=settled note=timeout `,
			),
		),
	);
});

test('画室：叫停（取消、/stop）后画室会话不回来，等一段收尾时间就换下一张，不等到画图期限', async () => {
	const { Studio } = await import('./studio.mjs');
	const timers = manualTimers();
	const runs = [];
	const studio = new Studio({
		// 完全不理取消信号的画室会话。
		run: (item) => {
			runs.push(item.messageId);
			return runs.length === 1
				? new Promise(() => {})
				: Promise.resolve({ delivered: 'final' });
		},
		notify: async () => assert.fail('叫停的不补话'),
		timer: timers.timer,
		deadlineMs: 600_000,
		graceMs: 30_000,
	});
	studio.request(job(1));
	studio.request(job(2));
	await flush(20);
	assert.equal(studio.cancel({ channelId: ids(1), authorId: ids(201) }), 1);
	await flush(20);
	assert.deepEqual(runs, [ids(101)]);
	timers.fire(30_000);
	await flush(30);
	await studio.idle;
	assert.deepEqual(runs, [ids(101), ids(102)]);
	assert.equal(
		timers.rows.some((row) => row.ms === 600_000 && !row.cleared),
		false,
		'换下一张时清掉上一张的期限',
	);
});

test('画室：请求那则被删了，正在画的与排队的都安静停掉；之后再交代同一则也不画（第 3 轮审查）', async () => {
	const { Studio, drawRequestTool } = await import('./studio.mjs');
	const runs = [];
	const notes = [];
	const logs = [];
	const studio = new Studio({
		run: (item, signal) =>
			new Promise((resolve) => {
				runs.push({ item, signal });
				signal.addEventListener('abort', () =>
					resolve({ delivered: 'none', status: 'cancelled' }),
				);
			}),
		notify: async (item, text) => notes.push(text),
		timer: manualTimers().timer,
		log: (line) => logs.push(line),
	});
	studio.request(job(1));
	studio.request(job(2));
	studio.request(job(3));
	await flush(20);
	// 排队中的 2 号被删：拿掉，不补话。
	assert.equal(studio.requestDeleted(ids(1), ids(102)), 1);
	assert.deepEqual(
		studio.waiting.map((item) => item.messageId),
		[ids(103)],
	);
	// 正在画的 1 号被删：发出取消，不补话。
	assert.equal(studio.requestDeleted(ids(1), ids(101)), 1);
	assert.equal(runs[0].signal.aborted, true);
	await flush(20);
	assert.equal(runs[1]?.item.messageId, ids(103), '换下一张');
	studio.stop(runs[1].item, 'stopped');
	await flush(20);
	await studio.idle;
	assert.deepEqual(notes, []);
	assert.ok(
		logs.some(
			(line) => line === `畫室 结果 #${ids(102)} status=deleted secs=0`,
		),
	);
	assert.ok(
		logs.some((line) =>
			line.startsWith(`畫室 结果 #${ids(101)} status=deleted `),
		),
	);
	// 别的频道没有这则：不受影响；已经删掉的那则再交代就不收，别的频道同一个 ID 照常收。
	assert.equal(studio.requestDeleted(ids(9), ids(101)), 0);
	assert.equal(studio.request(job(1)).status, 'deleted');
	assert.equal(studio.request(job(1, { channelId: ids(8) })).status, 'started');
	// draw_request 照实告诉她不用回话。
	const source = {
		messageId: ids(104),
		channelId: ids(1),
		guildId: ids(2),
		authorId: ids(204),
		authorName: '阿明',
	};
	const agent = {
		id: 'chat',
		session: { id: 'session-chat' },
	};
	const runtime = {
		studio,
		studios: new Map(),
		turns: new Map([['chat', source]]),
		sourceForTurn: () => source,
	};
	studio.requestDeleted(ids(1), ids(104));
	const result = await drawRequestTool(runtime).execute(
		{ action: 'start', brief: '畫貓' },
		{ agent },
	);
	assert.equal(result.ok, true);
	assert.match(result.message, /刪掉了/);
});
