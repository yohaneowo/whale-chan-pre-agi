// 一则 Discord 讯息在桥接里的那一轮：期限、取消、归帐，都绑在这一轮自己的识别码上。
// 桥接按频道串行 runPrompt，但送讯在串行之外；这里不靠「最早还没结束的那轮」这类顺序假设。
import process from 'node:process';
import { clearTimeout, setTimeout } from 'node:timers';
import { setTimeout as delay } from 'node:timers/promises';

const { AbortController } = globalThis;

// 一轮（含 huanqi 补救轮、附件、flush）最多多久。依 VM 上 881 轮的耗时：p95 28 秒；
// Krita 作画 16 轮里 12 轮在 280 秒内画完。仍短于闸道 5 分钟的讯息效期，
// 排在后面的那则 @ 轮到时还有效。DISCORD_TURN_DEADLINE_MS 可调。
export const DEFAULT_TURN_DEADLINE_MS = 280_000;
export const TURN_STEPS_TEXT = '這一輪我做了太多步，先停一下；要繼續再叫我。';
export const TURN_DEADLINE_NOTICE = '⚠️ 回合失败: ' + TURN_STEPS_TEXT;
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

// ---- 每一轮的事件：开始、进度（开始画图）、结束 ----
const listeners = new Set();
// 频道 → 桥接此刻正在跑的那则讯息 ID（runPrompt 期间）。
const active = new Map();

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

// 工具（discord_send_message、Krita 的预览与交付）用的 REST：送到某频道成功时，
// 记到那个频道此刻正在跑的那则讯息名下。桥接自己的送讯不经过这里，另外记帐。
export function trackSocialSends(rest, onSent) {
	return new Proxy(rest, {
		get(target, key, receiver) {
			if (key !== 'request') return Reflect.get(target, key, receiver);
			return async (method, path, ...rest) => {
				const result = await target.request(method, path, ...rest);
				const channelId =
					method === 'POST' &&
					/^\/channels\/(\d+)\/messages$/.exec(String(path))?.[1];
				const messageId = channelId && active.get(channelId);
				if (messageId && result?.id)
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
	},
	body,
) {
	const controller = new AbortController();
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
		startedAt: now(),
		deadlineMs,
		signal: controller.signal,
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
	const late = new Promise((resolve) => {
		cancelGrace = timer(() => {
			controller.abort();
			stop();
			log(`回合到期 #${budget.messageId}：已熄灯，等收尾`);
			cancelGrace = timer(
				() =>
					resolve({
						chunks: [TURN_DEADLINE_NOTICE],
						files: [],
						deadline: 'abandoned',
					}),
				graceMs,
			);
		}, deadlineMs);
	});
	try {
		return await Promise.race([
			Promise.resolve().then(() => body(budget)),
			late,
		]);
	} finally {
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
const DRAW_ACTIONS = new Set([
	'start',
	'paint',
	'paint_plan',
	'plan_strokes_from_image',
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

function drawingCall(event) {
	if (event.type !== 'tool/call' || event.data?.name !== 'krita_batch')
		return false;
	let args = event.data.arguments;
	try {
		if (typeof args === 'string') args = JSON.parse(args);
	} catch {
		return false;
	}
	return DRAW_ACTIONS.has(args?.action);
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
		drawing: false,
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
		if (drawingCall(event)) state.drawing = true;
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
	} = {},
) {
	const started = startedAt ?? now();
	const promptId = typeof prompt === 'string' ? prompt : prompt?.id;
	const source = typeof prompt === 'string' ? {} : (prompt?.source ?? {});
	let settled = false;
	const done = Promise.resolve()
		.then(() => agent.whenIdle())
		.then(() => settleRescues(agent, pause))
		.finally(() => {
			settled = true;
		});
	done.catch(() => {});
	let drawingSent = false;
	const elapsed = () => now() - started;
	for (;;) {
		await Promise.race([done.catch(() => {}), pause(poll)]);
		const state = socialTurnState(eventsOf(agent), promptId);
		if (state.drawing && !drawingSent) {
			drawingSent = true;
			emit({
				type: 'progress',
				channelId: String(source.discordChannelId ?? ''),
				messageId: String(source.discordMessageId ?? ''),
				drawing: true,
				elapsedMs: elapsed(),
			});
		}
		if (state.answered) {
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
