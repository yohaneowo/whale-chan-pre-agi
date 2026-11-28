// 记忆召回离线基线：阶段 A（不呼叫任何模型）。用法见 README.md。
//   node run.mjs [--social DIR] [--fish DIR] [--opencc DIR] [--only c1-1,c2-2] [--json OUT] [--twice] [--quiet]
// 结束码：0＝跑完（不管有没有失败的案例——这是量基线，不是闸门）；2＝环境不对（版本钉死对不上、镜像缺东西）；
//        3＝--twice 两次结果不一样（不可重复，不能当基线）。
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
	NOW,
	backupWorld,
	buildWorld,
	composeAuto,
	fingerprint,
	getScale,
	guardMirror,
	loadDeps,
	restoreWorld,
	runTool,
	setScale,
} from './harness.mjs';
import { CASES, CATEGORIES } from './cases.mjs';

const here = dirname(fileURLToPath(import.meta.url));

export async function withClock(fn) {
	const real = Date.now;
	Date.now = () => NOW;
	try {
		return await fn();
	} finally {
		Date.now = real;
	}
}

async function runCase(deps, testCase) {
	const timing = {};
	let checks = [];
	let surfaced = {};
	let tokens = {};
	const observed = {};
	if (testCase.custom) {
		const api = {
			buildWorld: (spec) => buildWorld(deps, structuredClone(spec)),
			composeAuto,
			backupWorld,
			restoreWorld,
		};
		const out = await testCase.custom(api);
		checks = out.checks;
		surfaced = out.surfaced;
		tokens = { auto: out.tokens };
		timing.auto = out.ms;
	} else {
		let auto;
		let tool;
		let world;
		if (testCase.auto) {
			world = await buildWorld(deps, structuredClone(testCase.world));
			auto = composeAuto(world, testCase.auto.view, testCase.auto);
			timing.auto = auto.ms;
			world.cleanup();
		}
		let toolWorld;
		if (testCase.tool) {
			toolWorld = await buildWorld(deps, structuredClone(testCase.world));
			tool = await runTool(toolWorld, testCase.tool.view, testCase.tool.calls, testCase.auto);
			timing.tool = tool.ms;
		}
		const aWorld = toolWorld ?? world;
		try {
			checks = testCase.check({ auto, tool, world: aWorld });
		} finally {
			toolWorld?.cleanup();
		}
		if (auto) surfaced.auto = auto.sources;
		if (tool) surfaced.tool = tool.sources;
		tokens = { auto: auto?.tokens, tool: tool?.tokens };
		if (testCase.observe?.auto) {
			const w = await buildWorld(deps, structuredClone(testCase.world));
			const seen = composeAuto(w, testCase.observe.auto.view, testCase.observe.auto);
			observed.dmSession = {
				sources: seen.all,
				labelledPrivate: /私訊（私下）/.test(seen.prompt),
			};
			w.cleanup();
		}
	}
	const all = [...Object.values(surfaced).flatMap((value) => (Array.isArray(value) ? value : [...(value.memory ?? []), ...(value.related ?? []), ...(value.notes ?? [])]))];
	return {
		result: {
			id: testCase.id,
			cat: testCase.cat,
			title: testCase.title,
			difficulty: testCase.difficulty,
			verdict: checks.every(([, ok]) => ok) ? 'PASS' : 'FAIL',
			checks: checks.map(([what, ok, detail]) => ({ what, ok: Boolean(ok), ...(ok ? {} : { detail: String(detail ?? '') }) })),
			surfaced,
			filler_hits: [...new Set(all)].filter((sym) => /^fm?\d+$/.test(sym)),
			est_tokens: tokens,
			...(Object.keys(observed).length ? { observed } : {}),
		},
		timing,
	};
}

export async function runAll(deps, { only, cases = CASES } = {}) {
	return withClock(async () => {
		const selected = only?.length ? cases.filter((c) => only.some((prefix) => c.id === prefix || c.id.startsWith(prefix))) : cases;
		const results = [];
		const timing = {};
		for (const testCase of selected) {
			const { result, timing: ms } = await runCase(deps, testCase);
			results.push(result);
			timing[testCase.id] = ms;
		}
		return { results, timing };
	});
}

// ---------- 消融（只改检索设定，不改任何正式程式）----------
// 同一批题、换一个检索设定重跑，看哪些缺口是「调设定就能补」、哪些不是。基线永远是没有任何变体的那一次。
export const VARIANTS = {
	baseline: {},
	'gate-0.8': { engine: { queryGate: 0.8 } },
	'threshold-0.5': { engine: { threshold: 0.5 } },
	'gate-0.8+threshold-0.5': { engine: { queryGate: 0.8, threshold: 0.5 } },
	'related-min-6': { runtime: { recallMinScore: 6 } },
	'related-min-12': { runtime: { recallMinScore: 12 } },
};
export function withVariant(deps, name) {
	const variant = VARIANTS[name];
	if (!variant) throw new Error('没有这个变体：' + name);
	const engine = variant.engine
		? { blank: deps.engine.blank, recall: (data, query, now, sid) => deps.engine.recall({ ...data, policy: { ...data.policy, ...variant.engine } }, query, now, sid) }
		: deps.engine;
	return { ...deps, engine, runtimeConfig: variant.runtime ?? {} };
}

export function summarize(results) {
	const byCategory = {};
	for (const result of results) {
		const row = (byCategory[result.cat] ??= { cat: result.cat, name: CATEGORIES[result.cat], total: 0, pass: 0, failed: [] });
		row.total++;
		if (result.verdict === 'PASS') row.pass++;
		else row.failed.push(result.id);
	}
	return Object.values(byCategory);
}

export function digest(header, results) {
	return createHash('sha256').update(JSON.stringify({ header, results })).digest('hex');
}

function percentile(values, p) {
	const sorted = [...values].sort((a, b) => a - b);
	return sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))] : 0;
}

async function main() {
	const args = process.argv.slice(2);
	const option = (name, fallback) => {
		const at = args.indexOf('--' + name);
		return at >= 0 ? args[at + 1] : fallback;
	};
	const socialDir = resolve(option('social', process.env.SOCIAL_DIR ?? join(here, '..', '..', 'social')));
	const fishDir = resolve(option('fish', process.env.FISH_DIR ?? '/fish'));
	const openccDir = resolve(option('opencc', process.env.OPENCC_DIR ?? '/opencc'));
	const only = option('only')?.split(',');
	if (option('ledger') || option('memory') || option('facts') || option('memstyle'))
		setScale({
			...(option('ledger') ? { ledger: Number(option('ledger')) } : {}),
			...(option('memory') ? { memory: Number(option('memory')) } : {}),
			...(option('facts') ? { facts: Number(option('facts')) } : {}),
			...(option('memstyle') ? { memoryStyle: option('memstyle') } : {}),
		});
	let deps;
	try {
		deps = await loadDeps({ socialDir, fishDir, openccDir });
		const missing = guardMirror(socialDir);
		if (missing.length) throw new Error('index.mjs 里这几段不见了，harness 镜像的接线要跟着改：' + missing.join(' ｜ '));
	} catch (error) {
		console.error('环境不对：' + error.message);
		process.exit(2);
	}
	const header = { harness: 'recall-baseline/1', now: new Date(NOW).toISOString(), fingerprint: fingerprint(socialDir), scale: getScale(), cases: CASES.length };
	if (args.includes('--ablate')) {
		const table = {};
		for (const name of Object.keys(VARIANTS)) {
			const run = await runAll(withVariant(deps, name), { only });
			const auto = run.results.filter((x) => x.surfaced.auto);
			table[name] = {
				pass: run.results.filter((x) => x.verdict === 'PASS').length,
				total: run.results.length,
				byCategory: Object.fromEntries(summarize(run.results).map((row) => [row.cat, `${row.pass}/${row.total}`])),
				failed: run.results.filter((x) => x.verdict === 'FAIL').map((x) => x.id),
				noise_per_auto_turn: +(auto.reduce((sum, x) => sum + x.filler_hits.length, 0) / Math.max(1, auto.length)).toFixed(2),
				mean_auto_tokens: Math.round(auto.reduce((sum, x) => sum + (x.est_tokens.auto ?? 0), 0) / Math.max(1, auto.length)),
			};
			console.log(`${name.padEnd(24)} ${table[name].pass}/${table[name].total}  噪声 ${table[name].noise_per_auto_turn}/轮  注入约 ${table[name].mean_auto_tokens} token/轮  失败：${table[name].failed.join(' ')}`);
		}
		if (option('json')) writeFileSync(option('json'), JSON.stringify({ header: { harness: 'recall-baseline/1', now: new Date(NOW).toISOString(), scale: getScale(), fingerprint: fingerprint(socialDir) }, ablation: table }, null, '\t'));
		return;
	}
	const first = await runAll(deps, { only });
	const hash = digest(header, first.results);
	if (args.includes('--twice')) {
		const second = await runAll(deps, { only });
		if (digest(header, second.results) !== hash) {
			console.error('两次执行的结果不一样：不可重复，不能当基线。');
			process.exit(3);
		}
		console.log('--twice：两次执行的结果 hash 相同 ' + hash.slice(0, 16));
	}
	const latency = Object.values(first.timing).flatMap((row) => Object.values(row));
	const output = {
		header,
		summary: summarize(first.results),
		results: first.results,
		hash,
		timing_ms: { note: '延迟不进入 hash', p50: +percentile(latency, 0.5).toFixed(2), p95: +percentile(latency, 0.95).toFixed(2), max: +Math.max(0, ...latency).toFixed(2) },
	};
	const outPath = option('json');
	if (outPath) writeFileSync(outPath, JSON.stringify(output, null, '\t'));
	if (!args.includes('--quiet')) {
		for (const row of output.summary) console.log(`${row.cat} ${row.name.padEnd(12, '　')} ${row.pass}/${row.total}  ${row.failed.length ? '失败：' + row.failed.join(' ') : ''}`);
		const pass = first.results.filter((r) => r.verdict === 'PASS').length;
		console.log(`合计 ${pass}/${first.results.length} 题通过；hash ${hash.slice(0, 16)}；延迟 p50 ${output.timing_ms.p50}ms p95 ${output.timing_ms.p95}ms`);
		for (const result of first.results.filter((r) => r.verdict === 'FAIL'))
			for (const check of result.checks.filter((c) => !c.ok)) console.log(`  ✗ ${result.id} ${check.what}${check.detail ? '（' + check.detail + '）' : ''}`);
	}
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
