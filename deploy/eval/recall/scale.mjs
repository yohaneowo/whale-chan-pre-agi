// 规模与延迟：帐本和记忆越来越大，每一轮的召回要花多久？（不进基线 hash，延迟每次都不一样。）
//   node scale.mjs [--fish DIR] [--opencc DIR] [--json OUT]
// 每个规模建一个全合成世界，量三条路径各自的中位数（5 次，先暖机 1 次）：
//   related＝每轮自动想起旧讯息（archive.related，每次从头读整个帐本）
//   recall ＝鱼记忆召回（memory.recallAcross，每次读全部记忆档）
//   search ＝她用的 chat_search（archive.search）
import { writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';
import { NOW, buildWorld, composeAuto, estimateTokens, loadDeps, runTool, setScale } from './harness.mjs';
import { withClock } from './run.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const option = (name, fallback) => (args.includes('--' + name) ? args[args.indexOf('--' + name) + 1] : fallback);
const deps = await loadDeps({
	socialDir: resolve(option('social', process.env.SOCIAL_DIR ?? join(here, '..', '..', 'social'))),
	fishDir: resolve(option('fish', process.env.FISH_DIR ?? '/fish')),
	openccDir: resolve(option('opencc', process.env.OPENCC_DIR ?? '/opencc')),
});
const median = (list) => [...list].sort((a, b) => a - b)[Math.floor(list.length / 2)];
const users = { A: { id: '910000000000000001', name: '阿甲' }, B: { id: '910000000000000002', name: '阿乙' }, C: { id: '910000000000000003', name: '阿丙' }, O: { id: '910000000000000099', name: '站長' }, BOT: { id: '910000000000000077', name: '鯨魚娘' }, ...Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`F${i + 1}`, { id: '9100000000000000' + (60 + i), name: '路過者' + (i + 1) }])) };
const channels = { c1: { id: '920000000000000001', name: '閒聊', guild: '930000000000000001' }, c2: { id: '920000000000000002', name: '遊戲', guild: '930000000000000002' } };
const sizes = [
	{ ledger: 1000, memory: 100 },
	{ ledger: 3600, memory: 300 },
	{ ledger: 10000, memory: 1000 },
	{ ledger: 30000, memory: 3000 },
];
const rows = [];
await withClock(async () => {
	for (const size of sizes) {
		setScale(size);
		const world = await buildWorld(deps, { users: structuredClone(users), channels: structuredClone(channels), owner: 'O', filler: { who: ['A', 'B', 'C'] } });
		const query = '阿甲家那兩隻貓結紮之後狀況還好嗎';
		const measure = (fn) => {
			fn();
			const times = [];
			for (let i = 0; i < 5; i++) {
				const started = performance.now();
				fn();
				times.push(performance.now() - started);
			}
			return +median(times).toFixed(1);
		};
		const related = measure(() => world.runtime.archive.related(query, { scopes: ['group'], normalize: (t) => deps.simplify(t).toLowerCase(), recentSince: NOW - 3 * 3600000 }));
		const recall = measure(() => world.runtime.memory.recallAcross(['group'], query, NOW));
		const search = measure(() => world.runtime.archive.search({ query: '貓', scopes: ['group'], normalize: (t) => deps.simplify(t).toLowerCase() }));
		const turn = measure(() => composeAuto(world, 'group', { user: 'B', ch: 'c1', text: query, id: 'q' + Math.random(), noLedger: true }));
		const tool = await runTool(world, 'group', [{ name: 'chat_search', args: { query: '養' } }]);
		rows.push({ ...size, related_ms: related, recall_ms: recall, search_ms: search, full_turn_ms: turn, chat_search_tokens: tool.tokens, chat_search_hits: tool.sources.length });
		console.log(`帐本 ${String(size.ledger).padStart(6)} 则 / 记忆 ${String(size.memory).padStart(5)} 条：想起旧话 ${related} ms，记忆召回 ${recall} ms，翻原文 ${search} ms，一轮自动注入合计 ${turn} ms；chat_search「养」回 ${tool.sources.length} 则、约 ${tool.tokens} token`);
		world.cleanup();
	}
});
if (option('json')) writeFileSync(option('json'), JSON.stringify({ note: '延迟，不进入基线 hash；单次、单机、暖机后中位数', rows }, null, '\t'));
void estimateTokens;
