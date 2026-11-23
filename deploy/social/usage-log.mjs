// 用量监控：每次模型调用记一行（版本、类别、新输入、快取、输出），余额读数长期留着，
// 换版本时记一笔部署时间。/用量 拿这些按版本对比「每轮对话」的 token 与花费，每次更新后看变动。
// 档案都在 social-runtime/usage/ 底下，一行一笔 JSON，只追加。
import { appendFileSync, mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const KINDS = {
	chat: '对话',
	compaction: '压缩',
	judge: '插话判断',
	notes: '记忆整理',
	eval: '风格测试',
	other: '其他',
};

function readLines(path) {
	try {
		return readFileSync(path, 'utf8')
			.split('\n')
			.filter(Boolean)
			.map((line) => {
				try {
					return JSON.parse(line);
				} catch {
					return undefined;
				}
			})
			.filter(Boolean);
	} catch {
		return [];
	}
}

// 这次调用是什么：会话里带工具的是对话的一步，不带工具的是压缩；
// 会话外的看系统提示认出插话判断、记忆整理与风格测试（它把群聊说明接在系统提示后面）。
export function callKind(options) {
	if (options?.sessionId !== undefined)
		return options.tools?.length ? 'chat' : 'compaction';
	const system =
		typeof options?.system === 'string'
			? options.system
			: JSON.stringify(options?.system ?? '');
	if (system.includes('判断一位鲸鱼娘')) return 'judge';
	if (system.includes('幫鯨魚娘整理')) return 'notes';
	if (system.includes('你現在在 Discord 群聊')) return 'eval';
	return 'other';
}

export class UsageLog {
	constructor(dir, { version, now = Date.now() } = {}) {
		this.dir = dir;
		this.version = version;
		mkdirSync(dir, { recursive: true, mode: 0o700 });
		// 版本跟上一笔不同，就是刚部署：记下时间，之后按这些时间切版本。
		const last = readLines(this.deploysPath).at(-1);
		if (version && last?.version !== version)
			this.append(this.deploysPath, { t: now, version });
	}

	get callsPath() {
		return join(this.dir, 'calls.jsonl');
	}

	get deploysPath() {
		return join(this.dir, 'deploys.jsonl');
	}

	get balancePath() {
		return join(this.dir, 'balance.jsonl');
	}

	append(path, row) {
		appendFileSync(path, JSON.stringify(row) + '\n', { mode: 0o600 });
	}

	// step 是这一轮的第几次调用（对话），1 就是新的一轮。
	record({ kind, scope, step, usage, now = Date.now() }) {
		if (!usage) return;
		this.append(this.callsPath, {
			t: now,
			v: this.version,
			kind,
			...(scope ? { scope } : {}),
			...(step ? { step } : {}),
			fresh: usage.inputTokens ?? 0,
			cache: usage.cacheReadTokens ?? 0,
			out: usage.outputTokens ?? 0,
		});
	}

	balance(total, now = Date.now()) {
		if (Number.isFinite(total))
			this.append(this.balancePath, { t: now, total });
	}

	// 每个版本一段：起讫时间、对话轮数、每轮平均 token、各类调用的份量、期间余额减少（充值不算）。
	versions(now = Date.now()) {
		const deploys = readLines(this.deploysPath);
		const calls = readLines(this.callsPath);
		const readings = readLines(this.balancePath);
		return deploys.map((deploy, index) => {
			const end = deploys[index + 1]?.t ?? now;
			const mine = calls.filter(
				(call) =>
					call.v === deploy.version && call.t >= deploy.t && call.t < end,
			);
			const chat = mine.filter((call) => call.kind === 'chat');
			const turns = chat.filter((call) => call.step === 1).length;
			const sum = (rows, key) =>
				rows.reduce((total, row) => total + row[key], 0);
			const kinds = {};
			for (const call of mine) {
				const row = (kinds[call.kind] ??= { calls: 0, tokens: 0 });
				row.calls++;
				row.tokens += call.fresh + call.cache + call.out;
			}
			let spent = 0;
			const inside = readings.filter((row) => row.t >= deploy.t && row.t < end);
			for (let i = 1; i < inside.length; i++)
				if (inside[i].total < inside[i - 1].total)
					spent += inside[i - 1].total - inside[i].total;
			return {
				version: deploy.version,
				from: deploy.t,
				to: end,
				turns,
				chatCalls: chat.length,
				fresh: sum(chat, 'fresh'),
				cache: sum(chat, 'cache'),
				out: sum(chat, 'out'),
				kinds,
				spent,
				measured: inside.length >= 2,
			};
		});
	}

	// /用量：目前版本与上一版并排；轮数太少时提醒还看不准。
	report(now = Date.now(), format = (t) => new Date(t).toISOString()) {
		const all = this.versions(now);
		if (!all.length) return '还没有用量纪录（这版上线后才开始记）。';
		const per = (value, count) => (count ? Math.round(value / count) : 0);
		const lines = all.slice(-2).map((v) => {
			const hours = Math.max((v.to - v.from) / 3600000, 1 / 60);
			const total = Object.values(v.kinds).reduce((s, k) => s + k.tokens, 0);
			const share = Object.entries(v.kinds)
				.sort((a, b) => b[1].tokens - a[1].tokens)
				.map(
					([kind, k]) =>
						`${KINDS[kind] ?? kind} ${k.calls} 次 ${total ? Math.round((k.tokens / total) * 100) : 0}%`,
				)
				.join('、');
			return [
				`【${v.version}】${format(v.from)} 起，${hours.toFixed(1)} 小时`,
				`对话 ${v.turns} 轮，每轮 ${v.turns ? (v.chatCalls / v.turns).toFixed(2) : 0} 次调用；每轮新输入 ${per(v.fresh, v.turns)}、快取 ${per(v.cache, v.turns)}、输出 ${per(v.out, v.turns)} tokens`,
				`各类调用（按 token）：${share || '无'}`,
				v.measured
					? `余额减少 ${v.spent.toFixed(2)} 元，每轮约 ${v.turns ? ((v.spent / v.turns) * 100).toFixed(2) : '—'} 分，一天约 ${((v.spent / hours) * 24).toFixed(2)} 元（含判断、整理、压缩）`
					: '余额读数还不够，花费要等下一次读余额（每 10 分钟）。',
				v.turns < 30 ? '（轮数还少，数字只供参考）' : '',
			]
				.filter(Boolean)
				.join('\n');
		});
		return lines.join('\n\n');
	}
}
