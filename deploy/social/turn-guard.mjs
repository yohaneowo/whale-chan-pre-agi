// 一则 Discord 讯息在桥接里的那一轮：期限、取消、归帐，都绑在这一轮自己的识别码上。
// 桥接按频道串行 runPrompt，但送讯在串行之外；这里不靠「最早还没结束的那轮」这类顺序假设。
// 同一则讯息自动续做（「我看看」之后再给一轮）时，期限与停止讯号沿用第一轮的，不重新计时。
// 画图不在这里：聊天回合只交代给背景画室（studio.mjs），几秒就结束。
import { AsyncLocalStorage } from 'node:async_hooks';
import process from 'node:process';
import {
	clearInterval,
	clearTimeout,
	setInterval,
	setTimeout,
} from 'node:timers';
import { setTimeout as delay } from 'node:timers/promises';

const { AbortController } = globalThis;

// 一则讯息（含自动续做、huanqi 补救轮、附件、flush）最多多久。依 VM 上 881 轮的耗时：p95 28 秒；
// 画图搬到背景画室以后，聊天回合应该在几十秒内结束，这只是保护上限。
// 仍短于闸道 5 分钟的讯息效期，排在后面的那则 @ 轮到时还有效。DISCORD_TURN_DEADLINE_MS 可调。
export const DEFAULT_TURN_DEADLINE_MS = 280_000;
export const TURN_STEPS_TEXT = '這一輪我做了太多步，先停一下；要繼續再叫我。';
export const TURN_DEADLINE_NOTICE = '⚠️ 回合失败: ' + TURN_STEPS_TEXT;
// 讯息已经被删或改掉（闸道记成撤销）：桥接回这句，群聊行为处理认得它，不发出去、也不补「卡住了」
// （回覆对象不在了，补的那句会掉成一般群讯）。开头跟「⏹️ 回合被取消」一样，旧的处理也不会发出去。
export const TURN_REVOKED_NOTICE = '⏹️ 回合被取消（訊息已撤回）';
// 期限到了以后，给桥接自己收尾（取消回合、写 turn/end、回传提示）的时间；再久就不等了。
const GRACE_MS = 15_000;
// 取消后等回合写入 turn/end 的时间。
const CANCEL_SETTLE_MS = 5_000;

export function turnDeadlineMs(env = process.env) {
	const value = Number(env.DISCORD_TURN_DEADLINE_MS);
	return Number.isInteger(value) && value >= 30_000 && value <= 1_800_000
		? value
		: DEFAULT_TURN_DEADLINE_MS;
}

// 日志只写固定的错误类别与堆疊里的「位置」那几行，不写 error.message
// （它可能带着私讯或群聊内文、Discord 回传的内容）。
export function errorTrace(error) {
	const name = /^[A-Za-z]{1,40}$/.test(String(error?.name ?? ''))
		? error.name
		: 'Error';
	const status =
		/^discord REST [A-Z]+ \S+ failed: (\d{3})\b/.exec(
			String(error?.message ?? ''),
		)?.[1] ?? '';
	// 只认「at 函式名 (档案:行:列)」这种格式的行，取函式名、档名与行号；其余（含 eval 的）不收。
	const frames = String(error?.stack ?? '')
		.split('\n')
		.slice(1)
		.map((line) =>
			/^\s*at (?:([\w$.<>[\] ]{1,80}) \()?(?:file:\/\/|node:)?[\w./:@\\-]*?([\w.-]{1,80}):(\d{1,6}):\d{1,6}\)?$/.exec(
				line,
			),
		)
		.filter(Boolean)
		.slice(0, 3)
		.map(([, fn, file, row]) => `${fn ? fn.trim() + '@' : ''}${file}:${row}`);
	return [name + (status ? ' ' + status : ''), ...frames].join(' | ');
}

// ---- 每一轮的事件：开始、结束 ----
const listeners = new Set();
// 频道 → 桥接此刻正在跑的那则讯息 ID（runPrompt 期间）。
const active = new Map();
// 「频道:讯息 ID」→ 这则讯息的期限与停止讯号（同一则讯息的每一轮共用）。
const budgets = new Map();
// 频道 → 最近一次 /stop 的时间：之前就排进队列、还没轮到的讯息也不再跑。
const stops = new Map();
const budgetKey = (channelId, messageId) => `${channelId}:${messageId}`;

// 这则讯息的期限与停止讯号；第一次用到时开始计时，之后的每一轮（自动续做）都沿用。
function budgetFor(channelId, messageId, deadlineMs, now, queuedAt) {
	const key = budgetKey(channelId, messageId);
	let budget = budgets.get(key);
	// 很久以前的同一则（例如被编辑后再叫她）重新计时。
	if (
		budget &&
		now() - budget.startedAt > budget.deadlineMs + GRACE_MS + 60_000
	) {
		budgets.delete(key);
		budget = undefined;
	}
	if (!budget) {
		budget = {
			channelId,
			messageId,
			startedAt: now(),
			deadlineMs,
			controller: new AbortController(),
			stopped: false,
		};
		budgets.set(key, budget);
		if (budgets.size > 256) budgets.delete(budgets.keys().next().value);
	}
	const stoppedAt = stops.get(channelId);
	if (
		stoppedAt !== undefined &&
		queuedAt !== undefined &&
		queuedAt <= stoppedAt
	)
		stopBudget(budget);
	return budget;
}

function stopBudget(budget) {
	budget.stopped = true;
	budget.controller.abort();
}

// /stop：这个频道正在跑与已经排队的讯息都停掉（不再送进模型、不再送答）。回传停了几则。
export function stopSocialTurns(channelId, now = () => Date.now()) {
	const channel = String(channelId);
	stops.set(channel, now());
	let count = 0;
	for (const budget of budgets.values())
		if (budget.channelId === channel && !budget.stopped) {
			stopBudget(budget);
			count++;
		}
	return count;
}

// 这则讯息是不是被 /stop 停掉了（桥接送答前问）。
export function socialTurnStopped(channelId, messageId) {
	return Boolean(
		budgets.get(budgetKey(String(channelId), String(messageId)))?.stopped,
	);
}

export function onSocialTurn(listener) {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

function emit(event) {
	for (const listener of listeners)
		try {
			listener(event);
		} catch {
			/* 听的人出错不影响这一轮。 */
		}
}

export function activeSocialTurn(channelId) {
	return active.get(String(channelId));
}

// 送讯挂的「回复」是哪一则（JSON 或附件表单的 payload_json）。
function referenceOf(body, form) {
	let payload = body;
	try {
		if (form && typeof form.get === 'function')
			payload = JSON.parse(String(form.get('payload_json') ?? '{}'));
	} catch {
		return undefined;
	}
	const id = payload?.message_reference?.message_id;
	return /^\d{16,22}$/.test(String(id ?? '')) ? String(id) : undefined;
}

// 工具送讯的来源：工具一开始执行就记下「哪个会话、为哪则讯息」在跑（runtime 在注册工具时包上），
// 之后这次执行里送出的讯息，不管拖到什么时候才真的送出，都认这个来源。
const sendOrigin = new AsyncLocalStorage();

export function runWithSocialSendOrigin(origin, run) {
	return sendOrigin.run(origin ?? {}, run);
}

// runtime 注册工具时包一层：originOf(exec) 回传 { sessionId, channelId, messageId }（取自宿主核对过的本轮来源）。
export function socialSendOriginTool(tool, originOf) {
	return {
		...tool,
		execute(args, exec) {
			let origin;
			try {
				origin = originOf(exec);
			} catch {
				origin = undefined;
			}
			return runWithSocialSendOrigin(origin, () =>
				tool.execute.call(tool, args, exec),
			);
		},
	};
}

// 工具（discord_send_message、Krita 的预览与交付）用的 REST：送到某频道成功时记帐。
// 归给谁看「是哪则讯息送出的」：挂了回复就是被回复的那则；没挂回复时，看这次工具执行一开始记下的来源
// （同一个频道才算）。不看送出时桥接正在跑哪一轮：A 的工具拖到 B 那轮才送出，仍然算 A 的。
// 没有来源、也没挂回复的送讯不归给任何一则。桥接自己的送讯不经过这里，另外凭 ticket 记帐。
export function trackSocialSends(rest, onSent) {
	return new Proxy(rest, {
		get(target, key, receiver) {
			if (key !== 'request') return Reflect.get(target, key, receiver);
			return async (method, path, body, form, ...rest) => {
				const channelId =
					method === 'POST' &&
					/^\/channels\/(\d+)\/messages$/.exec(String(path))?.[1];
				const origin = channelId ? sendOrigin.getStore() : undefined;
				const result = await target.request(method, path, body, form, ...rest);
				if (!channelId || !result?.id) return result;
				const messageId =
					referenceOf(body, form) ??
					(origin?.messageId && String(origin.channelId) === channelId
						? String(origin.messageId)
						: undefined);
				if (messageId)
					try {
						onSent(channelId, messageId, String(result.id));
					} catch {
						/* 记帐失败不影响已送出的讯息。 */
					}
				return result;
			};
		},
	});
}

// 「正在输入」：每隔几秒送一次 typing。被限流（429）时不在 REST 里等着重送（onceOnly），
// 熄灯后连送到一半的那次也取消，不会熄灯后又亮。
export function socialTyping(
	rest,
	channelId,
	intervalMs = 8_000,
	{ every = setInterval, clear = clearInterval } = {},
) {
	const controller = new AbortController();
	let stopped = false;
	const fire = () => {
		if (stopped) return;
		Promise.resolve()
			.then(() =>
				rest.request(
					'POST',
					`/channels/${channelId}/typing`,
					undefined,
					undefined,
					controller.signal,
				),
			)
			.catch(() => {});
	};
	fire();
	const handle = every(fire, Math.max(intervalMs, 3_000));
	return () => {
		if (stopped) return;
		stopped = true;
		clear(handle);
		controller.abort();
	};
}

// 包住桥接的 runPrompt：「正在输入」从这里亮起，期限从这里算，任何情况都会熄灯。
// body(budget) 是原本的 runPrompt；期限到了先熄灯、发出取消信号，再给它一点时间自己收尾，
// 还不回来就直接回传超时提示（卡住的附件下载、flush 不会再拖住「正在输入」和频道队列）。
export async function runSocialTurn(
	{
		channelId,
		messageId,
		beginTyping,
		deadlineMs = turnDeadlineMs(),
		graceMs = GRACE_MS,
		now = () => Date.now(),
		timer = (run, ms) => {
			const handle = setTimeout(run, ms);
			return () => clearTimeout(handle);
		},
		log = () => {},
		// 这则讯息进入频道队列的时间（之后来的 /stop 也会停掉它）。
		queuedAt,
	},
	body,
) {
	const shared = budgetFor(
		String(channelId),
		String(messageId),
		deadlineMs,
		now,
		queuedAt,
	);
	const controller = shared.controller;
	let stopTyping;
	let lit = false;
	const stop = () => {
		if (!lit) return;
		lit = false;
		try {
			stopTyping?.();
		} catch {
			/* 熄灯失败也不能卡住这一轮。 */
		}
	};
	const budget = {
		channelId: String(channelId),
		messageId: String(messageId),
		// 期限从这则讯息第一轮开始算（自动续做不重新计时）。
		startedAt: shared.startedAt,
		deadlineMs: shared.deadlineMs,
		signal: controller.signal,
		get stopped() {
			return shared.stopped;
		},
		// 原本的 runPrompt 自己会叫 beginTyping；只亮一次，熄灯交给这里。
		beginTyping: () => {
			if (!lit && !controller.signal.aborted) {
				lit = true;
				stopTyping = beginTyping();
			}
			return stop;
		},
	};
	active.set(budget.channelId, budget.messageId);
	emit({
		type: 'start',
		channelId: budget.channelId,
		messageId: budget.messageId,
	});
	let cancelGrace;
	const ended = () =>
		shared.stopped
			? { chunks: [], files: [], stopped: true }
			: { chunks: [TURN_DEADLINE_NOTICE], files: [], deadline: 'abandoned' };
	let offAbort = () => {};
	const late = new Promise((resolve) => {
		// 期限到或 /stop：先熄灯、发出取消信号，再给它一点时间自己收尾，还不回来就不等。
		const onAbort = () => {
			cancelGrace?.();
			stop();
			if (!shared.stopped) log(`回合到期 #${budget.messageId}：已熄灯，等收尾`);
			cancelGrace = timer(() => resolve(ended()), graceMs);
		};
		if (controller.signal.aborted) return onAbort();
		controller.signal.addEventListener('abort', onAbort, { once: true });
		offAbort = () => controller.signal.removeEventListener('abort', onAbort);
		const remaining = shared.startedAt + shared.deadlineMs - now();
		cancelGrace = timer(() => controller.abort(), Math.max(0, remaining));
	});
	try {
		const result = await Promise.race([
			Promise.resolve().then(() => body(budget)),
			late,
		]);
		// 收到 /stop 以后不再送答（runPrompt 已经拿到的答覆也不送）。
		return shared.stopped ? { chunks: [], files: [], stopped: true } : result;
	} finally {
		offAbort();
		cancelGrace?.();
		stop();
		if (active.get(budget.channelId) === budget.messageId)
			active.delete(budget.channelId);
		emit({
			type: 'end',
			channelId: budget.channelId,
			messageId: budget.messageId,
		});
	}
}

// 期限到了就不再等的 I/O（flush、附件）：信号一到回传 fallback，不丢错。
export async function socialBounded(promise, signal, fallback) {
	if (!signal) return await promise;
	if (signal.aborted) return fallback;
	let off;
	const aborted = new Promise((resolve) => {
		const onAbort = () => resolve(fallback);
		signal.addEventListener('abort', onAbort, { once: true });
		off = () => signal.removeEventListener('abort', onAbort);
	});
	try {
		return await Promise.race([promise, aborted]);
	} finally {
		off();
	}
}

// ---- 事件档里属于这一轮的那段 ----
const RESCUE = 'loop-guard';
// 会话里桥接、runtime、huanqi 自己插进来的讯息；其余的（群友、心跳、网页）都是别人的。
const INTERNAL = new Set([
	RESCUE,
	'runtime-context',
	'time-context',
	'compact-checkpoint',
	'model-selection',
	'plugin:dsh-plugin-discord',
	'maintenance-probe',
]);
const messageOf = (event) => event?.data?.message ?? event?.data;
const sourceOf = (event) => messageOf(event)?.source;
const eventsOf = (agent) =>
	agent.session?.snapshotEvents?.() ?? agent.session?.events ?? [];

function textOf(event) {
	return (messageOf(event)?.content ?? [])
		.filter((block) => block.type === 'text')
		.map((block) => block.text ?? '')
		.join('');
}

// 这一轮在事件档里的样子。promptId 是桥接这次送进 inbox 的那则讯息的 id（每次都不同），
// 同一则 Discord 讯息自动续做时 discordMessageId 会重复，不能拿来找。
export function socialTurnState(events, promptId) {
	const start = events.findIndex(
		(event) =>
			event.type === 'user/message' && messageOf(event)?.id === promptId,
	);
	if (start < 0) return { entered: false };
	const state = {
		entered: true,
		start,
		ended: false,
		completed: false,
		text: '',
		foreign: false,
		rescueRunning: false,
	};
	let lastStart = -1;
	let lastEnd = -1;
	for (let index = start + 1; index < events.length; index++) {
		const event = events[index];
		if (event.type === 'turn/start') lastStart = index;
		if (event.type === 'turn/end') lastEnd = index;
		if (event.type === 'user/message') {
			const kind = sourceOf(event)?.kind;
			if (!INTERNAL.has(kind)) state.foreign = true;
			if (state.ended && kind === RESCUE && !state.foreign)
				state.rescueRunning = true;
		}
		if (state.ended) continue;
		if (event.type === 'assistant/message') {
			const text = textOf(event);
			if (text !== '') state.text = text;
		}
		if (event.type === 'turn/end') {
			state.ended = true;
			state.completed = event.data?.reason?.kind === 'completed';
		}
	}
	// 「补救轮正在跑」：最后一个 turn/start 在最后一个 turn/end 之后。
	if (state.rescueRunning) state.rescueRunning = lastStart > lastEnd;
	state.answered = state.ended && state.completed && state.text.trim() !== '';
	return state;
}

// 只动属于这一轮的东西：inbox 里还没开始的本则讯息与它的补救讯息，正在跑的本轮或本轮的补救轮。
// 别人的回合（心跳、网页、下一则群聊）与别人的 inbox 一律不碰（keepInbox）。
function cancelOwned(agent, prompt, state, reason) {
	const pending = [
		...(agent.inbox?.nextTurn ?? []),
		...(agent.inbox?.nextStep ?? []),
	];
	const mine = (message) =>
		message?.id === prompt.id ||
		(state.entered && !state.foreign && message?.source?.kind === RESCUE);
	let removed = 0;
	for (const message of pending)
		if (mine(message) && agent.inbox?.remove?.(message.id)) removed++;
	const runningMine =
		state.entered && !state.foreign && (!state.ended || state.rescueRunning);
	if (runningMine)
		try {
			agent.cancel({ kind: 'hook', reason }, { keepInbox: true });
		} catch {
			return { removed, cancelled: false };
		}
	return { removed, cancelled: runningMine };
}

// 取代桥接原本的 `await agent.whenIdle()`（及 settleSocialTurn）：等这一轮与它的补救轮结束，
// 回合已经完整回答就不等补救轮（顺手停掉它，省 token），到期限就只取消自己的那一轮。
// 回传 status：answered／settled／lost（讯息没进会话）／deadline-cancelled／
// deadline-unsettled（取消了但 5 秒内没收尾）／deadline-not-owner（正在跑的是别人的回合，没有取消）。
// untilIdle：等到会话整个闲下来（含补救轮）才回来，不因为第一轮有文字就提早回传、也不停掉补救轮
// （画室用：画室要看的是图有没有交付，不是有没有说话）。
export async function awaitSocialTurn(
	agent,
	prompt,
	{
		signal,
		startedAt,
		deadlineMs = turnDeadlineMs(),
		pause = (ms) => delay(ms),
		now = () => Date.now(),
		poll = 500,
		untilIdle = false,
	} = {},
) {
	const started = startedAt ?? now();
	const promptId = typeof prompt === 'string' ? prompt : prompt?.id;
	let settled = false;
	const done = Promise.resolve()
		.then(() => agent.whenIdle())
		.then(() => settleRescues(agent, pause))
		.finally(() => {
			settled = true;
		});
	done.catch(() => {});
	const elapsed = () => now() - started;
	for (;;) {
		await Promise.race([done.catch(() => {}), pause(poll)]);
		const state = socialTurnState(eventsOf(agent), promptId);
		if (state.answered && !untilIdle) {
			const stopped =
				state.rescueRunning ||
				(agent.inbox?.nextTurn ?? []).some(
					(message) => message?.source?.kind === RESCUE,
				)
					? cancelOwned(
							agent,
							{ id: undefined },
							state,
							'discord-turn-answered',
						)
					: { removed: 0, cancelled: false };
			return {
				timedOut: false,
				status: 'answered',
				rescueStopped: stopped.removed + Number(stopped.cancelled),
				elapsedMs: elapsed(),
			};
		}
		if (settled) {
			await done;
			return {
				timedOut: false,
				status: state.entered ? 'settled' : 'lost',
				elapsedMs: elapsed(),
			};
		}
		if (signal?.aborted || elapsed() >= deadlineMs) {
			const outcome = cancelOwned(
				agent,
				{ id: promptId },
				state,
				'discord-turn-deadline',
			);
			if (!outcome.cancelled)
				return {
					timedOut: true,
					status: state.entered ? 'deadline-not-owner' : 'deadline-not-entered',
					elapsedMs: elapsed(),
				};
			await Promise.race([done.catch(() => {}), pause(CANCEL_SETTLE_MS)]);
			return {
				timedOut: true,
				status: settled ? 'deadline-cancelled' : 'deadline-unsettled',
				elapsedMs: elapsed(),
			};
		}
	}
}

// huanqi 判定一轮是废的时，会等这一轮结束、再补一则 loop-guard 讯息另开一轮。
// 等补救轮跑完（最多几次）。
async function settleRescues(agent, pause) {
	for (let round = 0; round < 8; round++) {
		await pause(300);
		const events = eventsOf(agent);
		const ended = events.findLastIndex((event) => event.type === 'turn/end');
		if (
			!events
				.slice(ended + 1)
				.some(
					(event) =>
						event.type === 'turn/start' ||
						(event.type === 'user/message' && sourceOf(event)?.kind === RESCUE),
				)
		)
			return;
		await agent.whenIdle();
	}
}

// 送答前的核对（闸道的 historyCanDeliver：要向 Discord 重新取原讯息、频道、成员与身分组，3 秒期限）
// 偶尔会因为 Discord 慢或限流而没过；没过就整轮答覆丢掉、补一句「我剛剛卡住了」
// （2026-11-28 第 4 则：回合正常答完，3 秒后核对没过）。所以没过时隔一下再核对，最多 attempts 次；
// 讯息已经被删或改掉（revoked）、期限到或 /stop 就不再试。核对过了，但核对的这段时间里被中止或撤销，
// 也不算过（部署前审查：核对成功时会忽略刚刚的中止讯号）。进来之前就已经到期限的（例如 flush 卡到期限）
// 不在此列：答案已经有了，核对过就照送（「flush 卡住」情境）。
export async function socialCanDeliver(
	check,
	{
		signal,
		revoked = () => false,
		attempts = 3,
		gapMs = 1_500,
		pause = (ms) => delay(ms),
		onMiss = () => {},
	} = {},
) {
	const abortedBefore = Boolean(signal?.aborted);
	for (let attempt = 1; ; attempt++) {
		let ok;
		try {
			ok = await check();
		} catch {
			ok = false;
		}
		if (ok) return (abortedBefore || !signal?.aborted) && !revoked();
		onMiss(attempt);
		if (attempt >= attempts || signal?.aborted || revoked()) return false;
		await pause(gapMs);
		if (signal?.aborted || revoked()) return false;
	}
}

// 同一个频道的会话一直延续下去（几天前的对话都还在会话里）。距离上一则群友讯息很久了，
// 就提醒她之前的话题已经过去（2026-11-28：4 天前同一个频道的一句闲聊，
// 她把这句当成刚刚的话接着回）。events 是会话事件，source 是这一轮（ingress 的那一则）。
export const STALE_GAP_MS = 3 * 3_600_000;
export function socialRecencyNote(events, source, now = Date.now()) {
	if (!source?.messageId || !Array.isArray(events)) return '';
	let last;
	for (let index = events.length - 1; index >= 0; index--) {
		const event = events[index];
		if (event?.type !== 'user/message') continue;
		const from = messageOf(event)?.source;
		if (from?.kind !== 'user' || !from.discordMessageId) continue;
		if (String(from.discordMessageId) === String(source.messageId)) continue;
		last = event;
		break;
	}
	const at = Number(last?.time);
	if (!Number.isFinite(at) || now - at < STALE_GAP_MS) return '';
	const hours = Math.floor((now - at) / 3_600_000);
	const gap = hours >= 48 ? `${Math.floor(hours / 24)} 天` : `${hours} 小時`;
	return `距離你上次在這裡聊天已經過了 ${gap}。那之前的話題都已經過去了，不要接舊話題、不要回舊訊息，只看這次的新訊息。`;
}

// 送进 inbox 并留下这则讯息本身（它的 id 是这一轮的识别码）。
export function socialFollowup(agent, message) {
	agent.followup(message);
	return message;
}

// 这一轮的讯息在事件档里的 seq（自动续做时 discordMessageId 会重复，只认这次的 id）。
export function socialPromptSeq(events, prompt) {
	return events.find(
		(event) =>
			event.type === 'user/message' && messageOf(event)?.id === prompt?.id,
	)?.seq;
}

// 取回答：与桥接的 extractReply 相同，但回合结束后紧接着是 huanqi 的补救讯息时继续往下收，
// 取最后一则有文字的回答；碰到别的讯息（下一个人说话）就停。
// 回合已经正常结束且有文字时不往下收：补救轮的回答会盖掉真正的答案
// （2026-11-28 「畫好了」被换成不相干的「這個我不畫啦」）。
export function foldSocialReply(events, promptSeq) {
	let text = '';
	let reasonKind;
	let errorMessage;
	for (const event of events) {
		if (event.seq < promptSeq) continue;
		if (event.type === 'user/message' && event.seq > promptSeq) {
			if (reasonKind === undefined) continue;
			if (sourceOf(event)?.kind !== RESCUE) break;
			if (reasonKind === 'completed' && text.trim() !== '') break;
			reasonKind = undefined;
			errorMessage = undefined;
			continue;
		}
		if (reasonKind !== undefined) continue;
		if (event.type === 'assistant/message') {
			const joined = textOf(event);
			if (joined !== '') text = joined;
			continue;
		}
		if (event.type === 'turn/end') {
			reasonKind = event.data.reason?.kind;
			errorMessage = event.data.reason?.error?.message;
		}
	}
	return { text, reasonKind, errorMessage };
}
