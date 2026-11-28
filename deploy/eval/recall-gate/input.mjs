// 把回忆守门员的真模型题目整理成一个 JSON（给 VM 沙盒里的探针读）：
//   node deploy/eval/recall-gate/input.mjs <输出.json>
// 内容：cases.mjs 的题目，加上回想题库 36 题的问题（auto 与 observe 那一句，预期「要翻」），
// 再附上聊天人设（deploy/social/persona-group.md）与 chat_search 的说明，给「让聊天模型自己决定查不查」那组对照用。
import console from 'node:console';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { CASES } from '../recall/cases.mjs';
import { GATE_CASES } from './cases.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const recall = [];
for (const item of CASES)
	for (const [suffix, turn] of [
		['', item.auto],
		[':observe', item.observe?.auto],
	])
		if (turn?.text)
			recall.push({
				id: item.id + suffix,
				cat: '題庫',
				text: turn.text,
				recent: [],
				want: { recall: true },
			});
const persona = readFileSync(
	join(here, '../../social/persona-group.md'),
	'utf8',
);
const out = process.argv[2];
if (!out) {
	console.error('用法：node input.mjs <输出.json>');
	process.exit(2);
}
writeFileSync(
	out,
	JSON.stringify({ cases: [...GATE_CASES, ...recall], persona }, null, 1),
);
console.log(
	`${GATE_CASES.length} 題守門員題目、${recall.length} 題題庫問題 → ${out}`,
);
