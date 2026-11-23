// 阶段 B 批次执行器（OpenCode CLI）。没有 --go 也没有 --rehearse 就只演练：核对冻结文件、列出要跑的工作，不呼叫任何模型。
//   node run.mjs --inputs-before B.json --inputs-after A.json --out DIR --mapping-out FILE
//        [--repeats 1|2|3] [--max-calls N] [--max-fail-streak N] [--call-timeout-sec S] [--model M] [--cli PATH] [--opencc DIR] [--go | --rehearse]
// 规则：
//   - 开跑前核对 spec.mjs 与两份输入的 sha256 是否等于 FROZEN.json 登记的；不一致就拒绝（判定标准跑完不准改）。
//   - 修前修后提示相同（hash 一样）的题只跑一组，两边共用。
//   - 每一笔回答「当下」就用 spec.mjs 的 judge 判分（回答先转简体），结果和回答一起追加写进 DIR/results.jsonl；
//     重跑自动跳过 status=ok 的（键＝题号|提示 hash|第几次），status=failed 的会重跑。
//   - --max-calls 必须是正整数（--go 一定要给）；每一次真正发出的呼叫（含重试）都算；到上限就停，结束码 3。
//   - 失败＝超时、非 0 结束码、空回答、事件解析失败、提供者错误：记成 status=failed，不算完成；非致命的失败重试一次（也算呼叫）；
//     连续 N 笔（--max-fail-streak，预设 3）最终失败就整批停下，结束码 4。提供者「没有路由／额度用完／没授权」整批中止，结束码 5。
//   - 每次呼叫前（含重试前）检查 DIR/STOP；每次呼叫有逾时（预设 60 秒），所以放 STOP 之后最坏要等「一次呼叫的时间」。
//   - 给人判的档案放在 DIR/blind-package/answers-blind.jsonl：只有 blindId、题号、问题、回答、要看什么，顺序打乱，不含版本与自动判定；
//     对照表 mapping 写在 --mapping-out 指定的地方（必须在 DIR 之外，档名要标明 DO-NOT-OPEN），打包给人判的时候只给 blind-package 目录。
//   - 盲判的种子：--go 时每次用密码学随机数产生，只存在对照表里（不写进输出目录、不印出来）；对照表已存在就沿用它的种子（同一批续跑，blindId 才不会变）。
//     --seed 只准排演（--rehearse）和测试用；--go 给 --seed 一律拒绝。
//   - 同一批的判分：unknown 类的题另外从「自动判为通过」的回答里每题抽 1 笔，写进 DIR/unknown-spot-check.jsonl 给人复核。
import { createHash, createHmac, randomBytes } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve, isAbsolute } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { SPEC, judge } from './spec.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const sha16 = (data) => createHash('sha256').update(data).digest('hex').slice(0, 16);
// 与 harness.mjs 的 PINNED.opencc 相同（OpenCC 1.4.2 的 t2cn.js）。
const OPENCC_SHA256 = 'b78e83f940e73a022d8d996333e924edf07aa5b6133b8cddd46fbbff1ac339b6';

export const EXIT = { ok: 0, usage: 2, maxCalls: 3, failStreak: 4, fatal: 5, someFailed: 6 };
export const FATAL = /no-route|free-limit|auth|unauthorized|forbidden/i;
const MAX_REPEATS = 3;

function positiveInt(name, raw) {
	if (typeof raw !== 'string' || !/^[1-9]\d*$/.test(raw.trim())) throw new Error(`${name} 必须是正整数，收到：${JSON.stringify(raw)}`);
	const value = Number(raw);
	if (!Number.isSafeInteger(value)) throw new Error(`${name} 太大：${raw}`);
	return value;
}

export function parseArgs(argv) {
	const args = { repeats: 1, maxCalls: null, maxFailStreak: 3, callTimeoutSec: 60, model: 'opencode-go/deepseek-v4.1-flash', go: false, rehearse: false, cli: 'opencode-cli' };
	const value = (i, name) => {
		if (i + 1 >= argv.length || argv[i + 1].startsWith('--')) throw new Error(`${name} 少了值`);
		return argv[i + 1];
	};
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === '--go') args.go = true;
		else if (a === '--rehearse') args.rehearse = true;
		else if (a === '--inputs-before') args.before = value(i++, a);
		else if (a === '--inputs-after') args.after = value(i++, a);
		else if (a === '--out') args.out = value(i++, a);
		else if (a === '--mapping-out') args.mappingOut = value(i++, a);
		else if (a === '--opencc') args.opencc = value(i++, a);
		else if (a === '--repeats') args.repeats = positiveInt(a, value(i++, a));
		else if (a === '--max-calls') args.maxCalls = positiveInt(a, value(i++, a));
		else if (a === '--max-fail-streak') args.maxFailStreak = positiveInt(a, value(i++, a));
		else if (a === '--call-timeout-sec') args.callTimeoutSec = positiveInt(a, value(i++, a));
		else if (a === '--model') args.model = value(i++, a);
		else if (a === '--cli') args.cli = value(i++, a);
		else if (a === '--cli-script') args.cliScript = value(i++, a); // 测试用：cli 当 node，第一个参数是假 opencode 的脚本
		else if (a === '--seed') args.seed = value(i++, a);
		else throw new Error(`不认得的参数：${a}`);
	}
	if (args.repeats > MAX_REPEATS) throw new Error(`--repeats 最多 ${MAX_REPEATS}`);
	if (args.go && args.rehearse) throw new Error('--go 和 --rehearse 不能同时给');
	if (args.go && args.seed !== undefined) throw new Error('--go 不准给 --seed（盲判种子由程式随机产生；固定种子只给排演和测试用）');
	if ((args.go || args.rehearse) && args.maxCalls === null) throw new Error('开跑（--go／--rehearse）一定要给 --max-calls（正整数）');
	if (args.go && !args.opencc) throw new Error('--go 要给 --opencc（回答要先转简体才能判分）');
	if ((args.go || args.rehearse) && !args.mappingOut) throw new Error('开跑要给 --mapping-out（对照表放哪里；必须在 --out 之外）');
	return args;
}

// 冻结文件核对：回传不一致的清单（空＝都对得上）。
export function checkFrozen(files, frozen) {
	return Object.entries(frozen).filter(([name]) => sha16(readFileSync(files[name])) !== frozen[name]).map(([name]) => name);
}

// 要跑的工作。before／after 是 inputs-*.json 解析后的 items。
export function planJobs(before, after, repeats) {
	const byId = (items) => new Map(items.map((item) => [item.id, item]));
	const b = byId(before);
	const a = byId(after);
	const jobs = [];
	for (const [id, itemB] of b) {
		const itemA = a.get(id);
		const variants = itemB.hash === itemA.hash ? [['both', itemB]] : [['before', itemB], ['after', itemA]];
		for (const [variant, item] of variants)
			for (let repeat = 1; repeat <= repeats; repeat++) jobs.push({ key: `${id}|${item.hash}|${repeat}`, id, variant, hash: item.hash, repeat, item });
	}
	return jobs;
}

// 带种子的洗牌（可重复）。
export function shuffle(list, seed) {
	let state = parseInt(sha16(seed), 16) % 2147483647 || 1;
	const next = () => (state = (state * 48271) % 2147483647) / 2147483647;
	const copy = [...list];
	for (let i = copy.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[copy[i], copy[j]] = [copy[j], copy[i]];
	}
	return copy;
}

// 解析 opencode run --format json 的事件流：拼出文字、取 token、认出提供者错误；
// events＝认得的事件数，bad＝长得像事件却解析不了的行数（输出被截断之类），两者都用来认定「解析失败」。
export function parseEvents(stdout) {
	let text = '';
	let tokens;
	let error;
	let events = 0;
	let bad = 0;
	for (const line of stdout.split('\n')) {
		if (!line.trim().startsWith('{')) continue;
		let event;
		try {
			event = JSON.parse(line);
		} catch {
			bad++;
			continue;
		}
		events++;
		if (event.type === 'text') text += event.part?.text ?? '';
		else if (event.type === 'step_finish') tokens = event.part?.tokens;
		else if (event.type === 'error') error = event.error;
	}
	return { text: text.trim(), tokens, error, events, bad };
}

// 把一次呼叫的原始结果归类：{ ok:true } 或 { ok:false, reason, fatal }。
export function classify(result) {
	const errorText = result.error ? JSON.stringify(result.error) : '';
	if (FATAL.test(errorText) || FATAL.test(result.stderr ?? '')) return { ok: false, fatal: true, reason: '提供者拒绝：' + (errorText || result.stderr).slice(0, 200) };
	if (result.timedOut) return { ok: false, reason: '逾时' };
	if (result.exit !== 0) return { ok: false, reason: `非 0 结束码（${result.exit ?? result.signal ?? '无'}）` };
	if (result.error) return { ok: false, reason: '模型错误：' + errorText.slice(0, 200) };
	if (result.bad > 0 || (result.events === 0 && !result.text)) return { ok: false, reason: '事件流解析失败' };
	if (!result.text || !result.text.trim()) return { ok: false, reason: '空回答' };
	return { ok: true };
}

// 真正呼叫 opencode（每次呼叫一个临时目录：自订的无工具代理，系统提示放档案里）。
export function makeOpencodeCaller(args, persona) {
	const tmpRoot = mkdtempSync(join(tmpdir(), 'phaseb-'));
	const caller = (job) => {
		const dir = mkdtempSync(join(tmpRoot, 'call-'));
		try {
			writeFileSync(join(dir, 'system.txt'), persona + '\n\n' + job.item.systemTail);
			writeFileSync(join(dir, 'opencode.json'), JSON.stringify({
				$schema: 'https://opencode.ai/config.json',
				agent: { evalbot: { mode: 'primary', description: 'plain Q&A, no tools', prompt: '{file:./system.txt}', tools: Object.fromEntries(['bash', 'edit', 'write', 'read', 'grep', 'glob', 'list', 'patch', 'todowrite', 'todoread', 'webfetch', 'task'].map((name) => [name, false])) } },
			}));
			const started = Date.now();
			const run = spawnSync(args.cli, [...(args.cliScript ? [args.cliScript] : []), 'run', '-m', args.model, '--agent', 'evalbot', '--format', 'json', job.item.user], { cwd: dir, encoding: 'utf8', timeout: args.callTimeoutSec * 1000, maxBuffer: 20 * 1024 * 1024 });
			const parsed = parseEvents(run.stdout ?? '');
			return { ...parsed, ms: Date.now() - started, exit: run.status, signal: run.signal, timedOut: run.error?.code === 'ETIMEDOUT', stderr: (run.stderr ?? '').slice(0, 500) };
		} finally {
			rmSync(dir, { recursive: true, force: true });
		}
	};
	caller.cleanup = () => rmSync(tmpRoot, { recursive: true, force: true });
	return caller;
}

export function readRows(resultsPath) {
	return existsSync(resultsPath) ? readFileSync(resultsPath, 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line)) : [];
}

// 每个键最后一笔记录（后写的盖过先写的：失败后补成功，就以成功为准）。
export function latestByKey(rows) {
	const latest = new Map();
	for (const row of rows) latest.set(row.key, row);
	return latest;
}

// 执行核心。caller(job) → { text, tokens, error, ms, exit, timedOut, events, bad, stderr }；可注入假呼叫器。
// 回传 { calls, outcome: 'done'|'stop'|'max-calls'|'fail-streak'|'fatal', exitCode, ok, failed }。
export function execute({ order, resultsPath, caller, simplify = (text) => text, judgeFn = judge, maxCalls, maxFailStreak = 3, stopPath, log = () => {}, now = () => new Date().toISOString() }) {
	if (!Number.isSafeInteger(maxCalls) || maxCalls < 1) throw new Error('maxCalls 必须是正整数');
	if (!Number.isSafeInteger(maxFailStreak) || maxFailStreak < 1) throw new Error('maxFailStreak 必须是正整数');
	const finished = new Set([...latestByKey(readRows(resultsPath)).values()].filter((row) => row.status === 'ok').map((row) => row.key));
	const todo = order.filter((job) => !finished.has(job.key));
	let calls = 0;
	let streak = 0;
	let ok = 0;
	let failed = 0;
	const stopped = () => stopPath && existsSync(stopPath);
	const write = (job, fields) => appendFileSync(resultsPath, JSON.stringify({ key: job.key, id: job.id, variant: job.variant, hash: job.hash, repeat: job.repeat, ...fields, at: now() }) + '\n');
	const end = (outcome, exitCode) => ({ calls, outcome, exitCode, ok, failed });

	for (const job of todo) {
		let attempts = 0;
		let last;
		let verdict;
		while (attempts < 2) {
			if (stopped()) {
				if (attempts > 0) { write(job, { status: 'failed', reason: last.reason + '（看到 STOP，没有重试）', attempts, ms: last.ms ?? null }); failed++; }
				log('看到 STOP 档，停下。');
				return end('stop', EXIT.ok);
			}
			if (calls >= maxCalls) {
				if (attempts > 0) { write(job, { status: 'failed', reason: last.reason + '（到 --max-calls，没有重试）', attempts, ms: last.ms ?? null }); failed++; }
				log('到达 --max-calls，停下。');
				return end('max-calls', EXIT.maxCalls);
			}
			calls++;
			attempts++;
			let raw;
			try {
				raw = caller(job);
			} catch (error) {
				raw = { exit: null, error: { message: '呼叫器丢出例外：' + String(error?.message ?? error).slice(0, 150) } };
			}
			const verdictOfCall = classify(raw);
			if (verdictOfCall.fatal) { log(verdictOfCall.reason + '。整批中止。'); return end('fatal', EXIT.fatal); }
			last = { ...verdictOfCall, ms: raw.ms, tokens: raw.tokens };
			if (verdictOfCall.ok) {
				verdict = judgeFn(job.id, simplify(raw.text));
				write(job, { status: 'ok', attempts, text: raw.text, tokens: raw.tokens ?? null, ms: raw.ms ?? null, auto: verdict.auto, failedChecks: verdict.failed, human: verdict.human });
				ok++;
				streak = 0;
				break;
			}
			log(`${job.id}#${job.repeat} 第 ${attempts} 次失败：${verdictOfCall.reason}`);
		}
		if (!verdict) {
			write(job, { status: 'failed', reason: last.reason, attempts, ms: last.ms ?? null });
			failed++;
			streak++;
			if (streak >= maxFailStreak) { log(`连续 ${streak} 笔最终失败，整批停下。`); return end('fail-streak', EXIT.failStreak); }
		}
	}
	return end('done', failed > 0 ? EXIT.someFailed : EXIT.ok);
}

// 「每题每版 3 次里通过几次」的表格。同一个函数在跑完时、也在演练时用。
// 通过＝status=ok 且自动判定通过；failed 的不算通过也不算做完。
// 需要人判的题（spec.human）：表里只显示「需人判」（没做完或有失败才加注），自动判定不显示、不计入合计——它只是给人看的参考，放进表里容易被误读成分数。
export function summaryTable(jobs, rows) {
	const latest = latestByKey(rows);
	const cell = (id, variant) => {
		const group = jobs.filter((job) => job.id === id && job.variant === variant);
		if (!group.length) return null;
		const results = group.map((job) => latest.get(job.key));
		const done = results.filter((row) => row?.status === 'ok');
		return { pass: done.filter((row) => row.auto).length, done: done.length, total: group.length, failed: results.filter((row) => row?.status === 'failed').length };
	};
	const ids = [...new Set(jobs.map((job) => job.id))];
	const progress = (c) => `${c.done < c.total ? `（应跑 ${c.total}，做完 ${c.done}）` : ''}${c.failed ? ` 失败${c.failed}` : ''}`;
	const show = (c, human) => (c ? (human ? `需人判${progress(c)}` : `${c.pass}/${c.done}${c.done < c.total ? `（应跑 ${c.total}）` : ''}${c.failed ? ` 失败${c.failed}` : ''}`) : '—');
	const lines = ['题号\t修前\t修后\t备注'];
	const totals = { before: [0, 0], after: [0, 0] };
	for (const id of ids) {
		const human = Boolean(SPEC[id]?.human);
		const both = cell(id, 'both');
		const before = both ?? cell(id, 'before');
		const after = both ?? cell(id, 'after');
		if (!human) for (const [name, c] of [['before', before], ['after', after]]) if (c) { totals[name][0] += c.pass; totals[name][1] += c.done; }
		lines.push([id, show(before, human), show(after, human), both ? '提示相同，两边共用' : ''].join('\t'));
	}
	lines.push(['合计（只含自动判的题）', `${totals.before[0]}/${totals.before[1]}`, `${totals.after[0]}/${totals.after[1]}`, '通过／做完；共用的题两边各算一次；「需人判」的题不在内'].join('\t'));
	return lines.join('\n');
}

// unknown 类的题：从「自动判为通过」的回答里每题抽 1 笔给人复核（挑哪一笔由种子决定，种子不知道就没法预先安排）。
export function unknownSpotCheck(jobs, rows, seed) {
	const latest = latestByKey(rows);
	const picks = [];
	for (const id of Object.keys(SPEC).filter((name) => SPEC[name].unknown)) {
		const passed = jobs.filter((job) => job.id === id && latest.get(job.key)?.status === 'ok' && latest.get(job.key).auto);
		if (!passed.length) continue;
		const job = passed[parseInt(createHmac('sha256', seed).update('spot|' + id).digest('hex').slice(0, 8), 16) % passed.length];
		picks.push({ case: id, key: job.key, variant: job.variant, repeat: job.repeat, answer: latest.get(job.key).text });
	}
	return picks;
}

// 盲判种子：--go 每次用密码学随机数产生；对照表已存在就沿用它的种子（同一批续跑，blindId 才不会变）。
export const newSeed = () => randomBytes(16).toString('hex');
export function resolveSeed({ explicitSeed, mappingPath, go }) {
	if (go && explicitSeed !== undefined) throw new Error('--go 不准给固定种子');
	if (explicitSeed !== undefined) return explicitSeed;
	if (existsSync(mappingPath)) {
		let saved;
		try {
			saved = JSON.parse(readFileSync(mappingPath, 'utf8'));
		} catch {
			throw new Error('对照表已存在但读不懂，不覆盖：' + mappingPath);
		}
		if (typeof saved.seed !== 'string' || saved.seed.length < 16) throw new Error('对照表已存在但没有种子（旧格式？），不覆盖：' + mappingPath);
		return saved.seed;
	}
	return newSeed();
}

// 给人判的盲档：只含需要人看的题；不含版本、hash、自动判定、时间；blindId＝HMAC(种子, 键)，不知道种子就反推不出版本；
// 顺序由种子决定。对照表（含种子）只写在 mappingPath。回传笔数。
export function writeBlind({ jobs, rows, seed, blindDir, mappingPath }) {
	const latest = latestByKey(rows);
	const entries = {};
	const out = [];
	for (const job of shuffle(jobs, seed + ':blind')) {
		const row = latest.get(job.key);
		if (!row || row.status !== 'ok' || !SPEC[job.id]?.human) continue;
		const blindId = 'b' + createHmac('sha256', seed).update('blind|' + job.key).digest('hex').slice(0, 10);
		entries[blindId] = { key: job.key, id: job.id, variant: job.variant, hash: job.hash, repeat: job.repeat };
		out.push({ blindId, case: job.id, question: job.item.user.split('\n\n').pop(), look: SPEC[job.id].human, answer: row.text });
	}
	mkdirSync(blindDir, { recursive: true });
	writeFileSync(join(blindDir, 'answers-blind.jsonl'), out.map((row) => JSON.stringify(row)).join('\n') + (out.length ? '\n' : ''));
	mkdirSync(dirname(mappingPath), { recursive: true });
	writeFileSync(mappingPath, JSON.stringify({ seed, entries }, null, 1));
	return out.length;
}

// 对照表必须在输出目录之外，档名要标明不要打开。
export function checkMappingPath(mappingPath, outDir) {
	const rel = relative(resolve(outDir), resolve(mappingPath));
	if (!rel.startsWith('..') && !isAbsolute(rel)) throw new Error('--mapping-out 不能放在 --out 目录里面（盲档和对照表要分开）');
	if (!/DO-NOT-OPEN/.test(mappingPath.replace(/^.*[\\/]/, ''))) throw new Error('--mapping-out 的档名要含 DO-NOT-OPEN');
}

async function loadSimplify(openccDir) {
	const file = join(openccDir, 'dist', 'esm', 't2cn.js');
	const data = readFileSync(file);
	if (createHash('sha256').update(data).digest('hex') !== OPENCC_SHA256) throw new Error(`OpenCC t2cn.js 的 sha256 不是固定的 1.4.2：${file}`);
	const staging = mkdtempSync(join(tmpdir(), 'phaseb-opencc-'));
	try {
		writeFileSync(join(staging, 't2cn.mjs'), data);
		const { Converter } = await import(pathToFileURL(join(staging, 't2cn.mjs')).href);
		return Converter({ from: 'tw', to: 'cn' });
	} finally {
		rmSync(staging, { recursive: true, force: true });
	}
}

async function main() {
	let args;
	try {
		args = parseArgs(process.argv.slice(2));
		if (!args.before || !args.after || !args.out) throw new Error('要给 --inputs-before、--inputs-after、--out');
		if (args.mappingOut) checkMappingPath(args.mappingOut, args.out);
	} catch (error) {
		console.error('拒绝执行：' + error.message);
		process.exit(EXIT.usage);
	}
	const files = { spec: join(here, 'spec.mjs'), before: resolve(args.before), after: resolve(args.after) };
	const frozen = JSON.parse(readFileSync(join(here, 'FROZEN.json'), 'utf8'));
	const bad = checkFrozen(files, frozen);
	if (bad.length) {
		console.error('拒绝执行：这些档案和事先登记（FROZEN.json）的 sha256 对不上：' + bad.join('、'));
		process.exit(EXIT.usage);
	}
	const before = JSON.parse(readFileSync(files.before, 'utf8'));
	const after = JSON.parse(readFileSync(files.after, 'utf8'));
	const out = resolve(args.out);
	mkdirSync(out, { recursive: true });
	const jobs = planJobs(before.items, after.items, args.repeats);
	const resultsPath = join(out, 'results.jsonl');
	const finished = new Set([...latestByKey(readRows(resultsPath)).values()].filter((row) => row.status === 'ok').map((row) => row.key));
	const todo = jobs.filter((job) => !finished.has(job.key));
	const seed = args.go || args.rehearse ? resolveSeed({ explicitSeed: args.seed, mappingPath: resolve(args.mappingOut), go: args.go }) : (args.seed ?? 'dry-run');
	console.log(`冻结文件核对：都对得上。工作 ${jobs.length} 个（共用 ${jobs.filter((j) => j.variant === 'both').length}、修前 ${jobs.filter((j) => j.variant === 'before').length}、修后 ${jobs.filter((j) => j.variant === 'after').length}），已完成 ${finished.size}，待跑 ${todo.length}，${args.maxCalls === null ? '这次不呼叫' : `这次最多呼叫 ${args.maxCalls} 次`}，单次逾时 ${args.callTimeoutSec} 秒，连续失败 ${args.maxFailStreak} 笔就停。`);
	if (!args.go && !args.rehearse) {
		console.log('（演练：没有 --go，不呼叫任何模型。）顺序已遮版本并打乱：');
		const order = shuffle(todo, seed);
		console.log(order.slice(0, 5).map((j) => `${j.id}#${j.repeat}`).join('、') + (order.length > 5 ? ' …' : ''));
		return;
	}
	const order = shuffle(todo, seed);
	let caller;
	let simplify = (text) => text;
	if (args.opencc) simplify = await loadSimplify(args.opencc);
	if (args.rehearse) {
		const { makeFakeCaller } = await import('./fake-caller.mjs');
		caller = makeFakeCaller();
		console.log('（排演：用假呼叫器，没有呼叫任何模型。）');
	} else {
		caller = makeOpencodeCaller(args, readFileSync(join(here, '..', '..', '..', 'social', 'persona-group.md'), 'utf8'));
	}
	let result;
	try {
		result = execute({ order, resultsPath, caller, simplify, maxCalls: args.maxCalls, maxFailStreak: args.maxFailStreak, stopPath: join(out, 'STOP'), log: (line) => console.log(line) });
	} finally {
		caller.cleanup?.();
	}
	const rows = readRows(resultsPath);
	console.log('\n' + summaryTable(jobs, rows));
	const spot = unknownSpotCheck(jobs, rows, seed);
	writeFileSync(join(out, 'unknown-spot-check.jsonl'), spot.map((row) => JSON.stringify(row)).join('\n') + (spot.length ? '\n' : ''));
	console.log(`不知道类的题每题抽 1 笔「自动判过」的回答给人复核：${spot.length} 笔，写在 ${join(out, 'unknown-spot-check.jsonl')}。`);
	const blindCount = writeBlind({ jobs, rows, seed, blindDir: join(out, 'blind-package'), mappingPath: resolve(args.mappingOut) });
	console.log(`\n本次呼叫 ${result.calls} 次（含重试）；成功 ${result.ok}、失败 ${result.failed}；结束原因：${result.outcome}。给人判的盲档 ${blindCount} 笔写在 ${join(out, 'blind-package')}；对照表在 --mapping-out（别打开）。`);
	process.exit(result.exitCode);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
