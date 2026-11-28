// 回忆守门员真模型实测的统计：读沙盒印出的 [sbx] 行（run.log）与题目（gate-input.json），印出
// 每类准确度、两轮一不一致、延迟（中位数、P95）、用量；对照组（聊天模型自己决定）叫工具的比例与延迟；
// 并把题库问题的判断写成 deploy/eval/recall/gate-verdicts.json（题库照这些真模型判断跑）。
//   node deploy/eval/recall-gate/report.mjs <run.log> <gate-input.json> [--verdicts <输出.json>]
// 价格：生产路线（opencode-go）的单价查不到（TBD）；这里用 DeepSeek 官方 deepseek-flash 的美元价当参考
// （docs/deepseek-notes.md，离峰：命中 0.003、未命中 0.15、输出 0.60；高峰加倍，每百万 token）。
import console from 'node:console';
import { readFileSync, writeFileSync } from 'node:fs';
import process from 'node:process';

const [log, inputPath] = process.argv.slice(2);
const verdictsAt = process.argv.indexOf('--verdicts');
const input = JSON.parse(readFileSync(inputPath, 'utf8'));
const rows = readFileSync(log, 'utf8')
	.split('\n')
	.filter((line) => line.includes('[sbx] {'))
	.map((line) => JSON.parse(line.slice(line.indexOf('{'))));
const byId = new Map(input.cases.map((item) => [item.id, item]));
const gate = rows.filter((row) => row.k === 'gate');
const chat = rows.filter((row) => row.k === 'chat');
const pct = (values, p) => {
	const sorted = [...values].sort((a, b) => a - b);
	return sorted.length
		? sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))]
		: 0;
};
const mean = (values) =>
	values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;
const cost = (usage, peak = false) => {
	const k = peak ? 2 : 1;
	return (
		((usage.cacheRead ?? 0) * 0.003 * k +
			(usage.input ?? 0) * 0.15 * k +
			((usage.output ?? 0) + (usage.reasoning ?? 0)) * 0.6 * k) /
		1e6
	);
};

const cats = new Map();
const wrong = [];
for (const row of gate) {
	const item = byId.get(row.id);
	if (!item) continue;
	const entry = cats.get(item.cat) ?? { n: 0, ok: 0, err: 0 };
	entry.n++;
	if (row.error) entry.err++;
	const ok = !row.error && row.recall === item.want.recall;
	if (ok) entry.ok++;
	else
		wrong.push(
			`${row.id}（第 ${row.round} 輪）${item.text.slice(0, 24)} → ${row.error ? '沒判出來 ' + row.error : row.recall ? '要翻' : '不翻'}（${row.reason ?? ''}）`,
		);
	cats.set(item.cat, entry);
}
console.log('## 回憶守門員（真模型）');
for (const [cat, entry] of cats)
	console.log(
		`- ${cat}：${entry.ok}/${entry.n}${entry.err ? `（沒判出來 ${entry.err}）` : ''}`,
	);
const core = gate.filter((row) => byId.get(row.id)?.cat !== '題庫');
const coreOk = core.filter(
	(row) => !row.error && row.recall === byId.get(row.id).want.recall,
).length;
console.log(`- 守門員題目合計：${coreOk}/${core.length}`);
const rounds = new Map();
for (const row of gate) {
	const list = rounds.get(row.id) ?? [];
	list.push(row.recall);
	rounds.set(row.id, list);
}
const unstable = [...rounds].filter(([, list]) => new Set(list).size > 1);
console.log(
	`- 兩輪判得不一樣：${unstable.length} 題${unstable.length ? '（' + unstable.map(([id]) => id).join('、') + '）' : ''}`,
);
const timed = gate.filter((row) => !row.skipped);
const ms = timed.map((row) => row.ms);
const usage = timed.map((row) => row.usage ?? {});
console.log(
	`- 延遲：中位數 ${pct(ms, 0.5)} ms、P95 ${pct(ms, 0.95)} ms、最長 ${Math.max(...ms)} ms（${timed.length} 次）`,
);
console.log(
	`- 用量（每次平均）：輸入 ${mean(usage.map((u) => u.input ?? 0)).toFixed(0)}、快取命中 ${mean(usage.map((u) => u.cacheRead ?? 0)).toFixed(0)}、輸出 ${mean(usage.map((u) => (u.output ?? 0) + (u.reasoning ?? 0))).toFixed(0)} token`,
);
console.log(
	`- 參考成本（deepseek-flash 官方價）：每次約 US$${mean(usage.map((u) => cost(u))).toFixed(6)}（離峰）／US$${mean(usage.map((u) => cost(u, true))).toFixed(6)}（高峰）；opencode-go 單價 TBD`,
);
if (wrong.length) {
	console.log('- 判錯／沒判出來：');
	for (const line of wrong) console.log('  - ' + line);
}

if (chat.length) {
	console.log(
		'\n## 對照組：聊天模型自己決定要不要叫 chat_search（只看第一步）',
	);
	let ok = 0;
	const miss = [];
	for (const row of chat) {
		const item = byId.get(row.id);
		if (!row.error && row.tool === item.want.recall) ok++;
		else
			miss.push(
				`${row.id} ${item.text.slice(0, 24)} → ${row.error ? '出錯 ' + row.error : row.tool ? '叫了工具' : '沒叫'}`,
			);
	}
	const cms = chat.map((row) => row.ms);
	const cu = chat.map((row) => row.usage ?? {});
	console.log(`- 跟預期一致：${ok}/${chat.length}`);
	console.log(
		`- 第一步的延遲：中位數 ${pct(cms, 0.5)} ms、P95 ${pct(cms, 0.95)} ms`,
	);
	console.log(
		`- 用量（每次平均，這裡只帶人設與規則，正式的提示更長）：輸入 ${mean(cu.map((u) => u.input ?? 0)).toFixed(0)}、快取命中 ${mean(cu.map((u) => u.cacheRead ?? 0)).toFixed(0)}、輸出 ${mean(cu.map((u) => (u.output ?? 0) + (u.reasoning ?? 0))).toFixed(0)} token`,
	);
	for (const line of miss) console.log('  - ' + line);
}

if (verdictsAt > 0) {
	const out = {};
	for (const row of gate)
		if (row.round === 1 && byId.get(row.id)?.cat === '題庫' && !row.error)
			out[byId.get(row.id).text] = {
				recall: row.recall,
				explicit: row.explicit,
				queries: row.queries ?? [],
			};
	writeFileSync(
		process.argv[verdictsAt + 1],
		JSON.stringify(out, null, '\t') + '\n',
	);
	console.log(
		`\n題庫的真模型判斷 ${Object.keys(out).length} 題 → ${process.argv[verdictsAt + 1]}`,
	);
}
