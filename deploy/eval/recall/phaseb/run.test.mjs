import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash, createHmac } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { EXIT, checkFrozen, checkMappingPath, classify, execute, latestByKey, makeOpencodeCaller, newSeed, parseArgs, parseEvents, planJobs, readRows, resolveSeed, shuffle, summaryTable, unknownSpotCheck, writeBlind } from './run.mjs';
import { makeFakeCaller } from './fake-caller.mjs';
import { ACTIVITY, DATE, GUESS, PLACES, SPEC, UNKNOWN, WORLD, judge } from './spec.mjs';

const here = dirname(fileURLToPath(import.meta.url));

const item = (id, hash) => ({ id, hash, user: `阿甲：${id}的问题\n`.trim(), systemTail: 's' });
const withTemp = (fn) => {
	const dir = mkdtempSync(join(tmpdir(), 'phaseb-test-'));
	try {
		return fn(dir);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
};
// 一个小批次：c1-2（要中「吉他」）、c2-1（不知道类）、c3-2（需人判）、c1-1；前后提示相同的共用一组
const smallJobs = (repeats = 1) => planJobs(['c1-2', 'c2-1', 'c3-2', 'c1-1'].map((id) => item(id, 'h-' + id)), ['c1-2', 'c2-1', 'c3-2', 'c1-1'].map((id) => item(id, id === 'c3-2' ? 'h-new' : 'h-' + id)), repeats);
const ok = (text) => ({ text, exit: 0, events: 3, bad: 0, tokens: { input: 1, output: 1 }, ms: 3 });

test('相同提示的题只跑一组，不同的两边各跑；每题每次一个工作；键不重复', () => {
	const jobs = planJobs([item('a', 'h1'), item('b', 'h2')], [item('a', 'h1'), item('b', 'h3')], 3);
	assert.equal(jobs.length, 3 + 6);
	assert.deepEqual([...new Set(jobs.filter((j) => j.id === 'a').map((j) => j.variant))], ['both']);
	assert.deepEqual([...new Set(jobs.filter((j) => j.id === 'b').map((j) => j.variant))].sort(), ['after', 'before']);
	assert.equal(new Set(jobs.map((j) => j.key)).size, jobs.length);
});

test('洗牌可重复、不丢不重；种子不同顺序不同', () => {
	const list = Array.from({ length: 30 }, (_, i) => i);
	assert.deepEqual(shuffle(list, 'x'), shuffle(list, 'x'));
	assert.deepEqual([...shuffle(list, 'x')].sort((a, b) => a - b), list);
	assert.notDeepEqual(shuffle(list, 'x'), shuffle(list, 'y'));
});

test('冻结文件核对：改了就挡下', () => {
	withTemp((dir) => {
		writeFileSync(join(dir, 'a.txt'), 'hello');
		const good = createHash('sha256').update('hello').digest('hex').slice(0, 16);
		assert.deepEqual(checkFrozen({ a: join(dir, 'a.txt') }, { a: good }), []);
		writeFileSync(join(dir, 'a.txt'), 'hello!');
		assert.deepEqual(checkFrozen({ a: join(dir, 'a.txt') }, { a: good }), ['a']);
	});
});

test('opencode 事件流：拼文字、取 token、认出提供者错误、数出解析不了的行', () => {
	const okEvents = ['{"type":"step_start"}', '{"type":"text","part":{"text":"收到"}}', '{"type":"step_finish","part":{"tokens":{"input":9,"output":3}}}'].join('\n');
	const parsed = parseEvents(okEvents);
	assert.equal(parsed.text, '收到');
	assert.deepEqual(parsed.tokens, { input: 9, output: 3 });
	assert.equal(parsed.events, 3);
	assert.equal(parsed.bad, 0);
	assert.equal(parseEvents('{"type":"error","error":{"type":"provider.no-route","message":"Model unavailable"}}').error.type, 'provider.no-route');
	assert.equal(parseEvents('{"type":"text","part":{"text":"半句').bad, 1, '被截断的行算解析失败');
});

test('参数：预设模型、没有 --go 就是演练、预设逾时 60 秒、连续失败 3 笔就停', () => {
	const args = parseArgs(['--inputs-before', 'b', '--inputs-after', 'a', '--out', 'o']);
	assert.equal(args.model, 'opencode-go/deepseek-v4.1-flash');
	assert.equal(args.go, false);
	assert.equal(args.callTimeoutSec, 60);
	assert.equal(args.maxFailStreak, 3);
	assert.equal(args.maxCalls, null);
	assert.equal(parseArgs(['--go', '--max-calls', '43', '--opencc', 'x', '--mapping-out', 'y']).maxCalls, 43);
	assert.equal(parseArgs(['--rehearse', '--max-calls', '4', '--mapping-out', 'y', '--seed', 'fixed']).seed, 'fixed', '排演可以明确给固定种子');
});

test('判定标准：每个要判的题都有规格；判定函数认得 must／mustNot／unknown', () => {
	assert.equal(Object.keys(SPEC).length, 33);
	assert.ok(judge('c4-1', '阿甲下周五要搬家').auto);
	assert.ok(!judge('c4-1', '周五，不对，是周三').auto);
	assert.ok(UNKNOWN.test('我不知道耶') && !UNKNOWN.test('他要去日本'));
	assert.equal(judge('c9-2', '我记不起来了').auto, true);
	assert.equal(judge('c9-2', '他要去日本出差').auto, false);
});

// ---------- A1：判分真的接进执行器 ----------

test('A1：每一笔成功的回答当下就判分——judge 被呼叫、结果写进 results.jsonl、数字对', () => {
	withTemp((dir) => {
		const resultsPath = join(dir, 'results.jsonl');
		const jobs = smallJobs(2);
		const answers = { 'c1-2': ['他学吉他。', '他学钢琴。'], 'c2-1': ['我不知道。', '我不知道生日，但应该是四月。'], 'c3-2': ['有两个小明，一个花生过敏、一个考研究所。', '小明在考研究所'], 'c1-1': ['猫咪结扎后要隔离。', '猫咪结扎后要隔离。'] };
		const caller = (job) => ok(answers[job.id][job.repeat - 1]);
		const judged = [];
		const result = execute({ order: jobs, resultsPath, caller, maxCalls: 100, judgeFn: (id, text) => (judged.push([id, text]), judge(id, text)) });
		assert.equal(result.outcome, 'done');
		assert.equal(result.exitCode, EXIT.ok);
		assert.equal(result.calls, jobs.length);
		assert.equal(judged.length, jobs.length, '每个回答都进了 judge');
		const rows = readRows(resultsPath);
		const by = (id, variant, repeat) => rows.find((row) => row.id === id && row.variant === variant && row.repeat === repeat);
		assert.equal(by('c1-2', 'both', 1).auto, true);
		assert.equal(by('c1-2', 'both', 2).auto, false);
		assert.ok(by('c1-2', 'both', 2).failedChecks.some((line) => line.includes('缺')), '没通过的写下原因');
		assert.equal(by('c2-1', 'both', 1).auto, true);
		assert.equal(by('c2-1', 'both', 2).auto, false, '「不知道，但应该是四月」不算通过');
		assert.equal(by('c3-2', 'before', 1).human.length > 0, true, '需人判的题带着「看什么」');
		const table = summaryTable(jobs, rows).split('\n');
		const row = (id) => table.find((line) => line.startsWith(id + '\t')).split('\t');
		assert.deepEqual(row('c1-2').slice(1, 3), ['1/2', '1/2'], '共用的题两边都显示同一个数');
		assert.deepEqual(row('c2-1').slice(1, 3), ['1/2', '1/2']);
		assert.deepEqual(row('c1-1').slice(1, 3), ['2/2', '2/2']);
		assert.deepEqual(row('c3-2').slice(1, 3), ['需人判', '需人判'], '需人判的题只显示「需人判」，不显示自动分');
		assert.deepEqual(row('合计（只含自动判的题）').slice(1, 3), ['4/6', '4/6'], '合计只含自动判的 3 题（1+1+2 / 2+2+2），c3-2 的自动分不在内');
	});
});

test('A1：回答先用 simplify 转简体再判分（繁体回答照样判得对）', () => {
	withTemp((dir) => {
		const jobs = planJobs([item('c1-1', 'x')], [item('c1-1', 'x')], 1);
		const simplify = (text) => text.replaceAll('貓', '猫').replaceAll('結紮', '结扎');
		const caller = () => ok('貓咪結紮之後要隔離');
		execute({ order: jobs, resultsPath: join(dir, 'r.jsonl'), caller, simplify, maxCalls: 5 });
		assert.equal(readRows(join(dir, 'r.jsonl'))[0].auto, true);
		const dir2 = join(dir, 'no-simplify.jsonl');
		execute({ order: jobs, resultsPath: dir2, caller, maxCalls: 5 });
		assert.equal(readRows(dir2)[0].auto, false, '不转简体就判不到——证明转换真的在判分前发生');
		assert.equal(readRows(join(dir, 'r.jsonl'))[0].text, '貓咪結紮之後要隔離', 'results 里存的是模型原话');
	});
});

// ---------- A2：--max-calls ----------

test('A2：--max-calls 不是正整数一律拒绝（NaN、0、负数、小数、Infinity、空、缺值）', () => {
	for (const bad of ['abc', '0', '-3', '1.5', 'Infinity', 'NaN', '', '1e3', ' ']) {
		assert.throws(() => parseArgs(['--max-calls', bad]), /正整数/, `拒绝 ${JSON.stringify(bad)}`);
	}
	assert.throws(() => parseArgs(['--max-calls']), /少了值/);
	assert.throws(() => parseArgs(['--max-calls', '--go']), /少了值/);
	assert.throws(() => parseArgs(['--go', '--opencc', 'o', '--mapping-out', 'm']), /--max-calls/, '开跑一定要给上限');
	assert.throws(() => parseArgs(['--rehearse', '--mapping-out', 'm']), /--max-calls/);
	assert.throws(() => parseArgs(['--repeats', '0']), /正整数/);
	assert.throws(() => parseArgs(['--repeats', '4']), /最多/);
	assert.throws(() => parseArgs(['--max-fail-streak', '0']), /正整数/);
	assert.throws(() => parseArgs(['--call-timeout-sec', '-1']), /正整数/);
	assert.throws(() => parseArgs(['--max-calsl', '3']), /不认得/, '拼错的参数不会被默默忽略');
	assert.equal(parseArgs(['--max-calls', '7']).maxCalls, 7);
	withTemp((dir) => {
		for (const bad of [NaN, 0, -1, 2.5, Infinity, undefined, '3'])
			assert.throws(() => execute({ order: [], resultsPath: join(dir, 'r.jsonl'), caller: () => ok('x'), maxCalls: bad }), /maxCalls/, `execute 也拒绝 ${String(bad)}`);
	});
});

test('A2：重试也算呼叫；到上限就停、结束码 3、不会多打一次', () => {
	withTemp((dir) => {
		const resultsPath = join(dir, 'r.jsonl');
		const jobs = smallJobs(1); // 4 组 → 5 个工作（c3-2 前后各一）
		// 每个工作第一次呼叫都逾时，重试才成功：每个工作要 2 次呼叫
		const seen = new Set();
		let issued = 0;
		const caller = (job) => {
			issued++;
			if (!seen.has(job.key)) { seen.add(job.key); return { text: '', exit: null, timedOut: true, events: 0, bad: 0 }; }
			return ok('他学吉他');
		};
		const result = execute({ order: jobs, resultsPath, caller, maxCalls: 5 });
		assert.equal(issued, 5, '真正发出的呼叫刚好 5 次');
		assert.equal(result.calls, 5);
		assert.equal(result.outcome, 'max-calls');
		assert.equal(result.exitCode, EXIT.maxCalls);
		const rows = readRows(resultsPath);
		assert.equal(rows.filter((row) => row.status === 'ok').length, 2, '5 次呼叫 = 2 个完整工作 + 第 3 个工作第一次逾时');
		const last = rows.at(-1);
		assert.equal(last.status, 'failed');
		assert.match(last.reason, /没有重试/, '到上限没来得及重试的，记成失败并写明');
	});
});

test('A2：刚好打完所有工作、同时用完额度，不算「到上限」', () => {
	withTemp((dir) => {
		const jobs = smallJobs(1);
		const result = execute({ order: jobs, resultsPath: join(dir, 'r.jsonl'), caller: () => ok('嗯'), maxCalls: jobs.length });
		assert.equal(result.outcome, 'done');
		assert.equal(result.exitCode, EXIT.ok);
	});
});

// ---------- A3：失败的呼叫不算完成 ----------

test('A3：逾时、非 0 结束码、空回答、解析失败、模型错误 → 记成 failed，不算完成，续跑时重试', () => {
	const cases = {
		逾时: { text: '', exit: null, timedOut: true, events: 0, bad: 0 },
		'非 0 结束码': { text: '有字', exit: 1, events: 2, bad: 0 },
		空回答: { text: '  ', exit: 0, events: 2, bad: 0 },
		事件流解析失败: { text: '', exit: 0, events: 0, bad: 2 },
		模型错误: { text: '', exit: 0, events: 1, bad: 0, error: { type: 'provider.overloaded', message: 'busy' } },
	};
	for (const [reason, raw] of Object.entries(cases)) {
		assert.equal(classify(raw).ok, false, reason);
		assert.match(classify(raw).reason, new RegExp(reason), reason);
	}
	assert.equal(classify({ text: '有字', exit: 0, events: 2, bad: 0 }).ok, true);
	assert.equal(classify({ text: '半句', exit: 0, events: 1, bad: 1 }).ok, false, '有字但有行被截断也不收');
	withTemp((dir) => {
		const resultsPath = join(dir, 'r.jsonl');
		const jobs = planJobs([item('c1-2', 'h')], [item('c1-2', 'h')], 1);
		for (const raw of Object.values(cases)) {
			rmSync(resultsPath, { force: true });
			let issued = 0;
			const result = execute({ order: jobs, resultsPath, caller: () => (issued++, raw), maxCalls: 10 });
			const rows = readRows(resultsPath);
			assert.equal(issued, 2, '失败后重试一次，两次都算');
			assert.equal(result.calls, 2);
			assert.equal(rows.length, 1);
			assert.equal(rows[0].status, 'failed');
			assert.equal(rows[0].text, undefined, '失败的不存回答，也没有判分');
			assert.equal(rows[0].auto, undefined);
			assert.equal(result.exitCode, EXIT.someFailed);
			// 续跑：failed 的会重跑，成功后以最后一笔为准
			const resumed = execute({ order: jobs, resultsPath, caller: () => ok('他学吉他'), maxCalls: 10 });
			assert.equal(resumed.calls, 1);
			assert.equal(latestByKey(readRows(resultsPath)).get(jobs[0].key).status, 'ok');
			assert.equal(execute({ order: jobs, resultsPath, caller: () => assert.fail('已完成的不该再呼叫'), maxCalls: 10 }).calls, 0);
		}
	});
});

test('A3：连续 N 笔最终失败就整批停下（结束码 4）；中间成功一次就重新数', () => {
	withTemp((dir) => {
		const jobs = smallJobs(1);
		const failing = () => ({ text: '', exit: 1, events: 0, bad: 0 });
		let issued = 0;
		const result = execute({ order: jobs, resultsPath: join(dir, 'a.jsonl'), caller: () => (issued++, failing()), maxCalls: 100, maxFailStreak: 2 });
		assert.equal(result.outcome, 'fail-streak');
		assert.equal(result.exitCode, EXIT.failStreak);
		assert.equal(issued, 4, '2 个工作 × (1 次 + 1 次重试)，第 2 笔最终失败后整批停下，剩下的工作没碰');
		assert.equal(readRows(join(dir, 'a.jsonl')).length, 2);
		// 失败、成功、失败、成功……永远凑不到连续 2 笔
		let n = 0;
		const flip = (job) => (Math.floor(n++ / 2) % 2 === 0 ? failing() : ok('他学吉他'));
		const alternating = execute({ order: jobs, resultsPath: join(dir, 'b.jsonl'), caller: flip, maxCalls: 100, maxFailStreak: 2 });
		assert.notEqual(alternating.outcome, 'fail-streak');
	});
});

test('A3：提供者说没有路由／额度用完／没授权 → 整批中止（结束码 5），不写 failed 也不重试', () => {
	withTemp((dir) => {
		const jobs = smallJobs(1);
		for (const error of [{ type: 'provider.no-route' }, { message: 'Free usage limit reached (free-limit)' }, { type: 'unauthorized' }]) {
			let issued = 0;
			const result = execute({ order: jobs, resultsPath: join(dir, 'f.jsonl'), caller: () => (issued++, { text: '', exit: 0, events: 1, bad: 0, error }), maxCalls: 100 });
			assert.equal(result.outcome, 'fatal');
			assert.equal(result.exitCode, EXIT.fatal);
			assert.equal(issued, 1);
		}
		assert.equal(existsSync(join(dir, 'f.jsonl')), false);
	});
});

test('A3：呼叫器自己丢例外 → 当作一次失败，不会让整个执行器崩掉', () => {
	withTemp((dir) => {
		const jobs = planJobs([item('c1-2', 'h')], [item('c1-2', 'h')], 1);
		let issued = 0;
		const result = execute({ order: jobs, resultsPath: join(dir, 'r.jsonl'), caller: () => { issued++; throw new Error('spawn ENOENT'); }, maxCalls: 10 });
		assert.equal(issued, 2);
		assert.equal(readRows(join(dir, 'r.jsonl'))[0].status, 'failed');
		assert.equal(result.exitCode, EXIT.someFailed);
	});
});

// ---------- STOP ----------

test('STOP：开跑前有就不呼叫；跑到一半出现，只会多做完手上那一次呼叫，重试前也检查', () => {
	withTemp((dir) => {
		const stopPath = join(dir, 'STOP');
		const jobs = smallJobs(1);
		writeFileSync(stopPath, '');
		assert.equal(execute({ order: jobs, resultsPath: join(dir, 'a.jsonl'), caller: () => assert.fail('不该呼叫'), maxCalls: 9, stopPath }).outcome, 'stop');
		rmSync(stopPath);
		let issued = 0;
		const result = execute({
			order: jobs,
			resultsPath: join(dir, 'b.jsonl'),
			caller: () => {
				issued++;
				if (issued === 2) writeFileSync(stopPath, ''); // 第 2 次呼叫进行中，使用者放了 STOP
				return issued === 2 ? { text: '', exit: 1, events: 0, bad: 0 } : ok('他学吉他');
			},
			maxCalls: 9,
			stopPath,
		});
		assert.equal(issued, 2, '手上那次做完；它失败了但不再重试，也不开始下一个工作');
		assert.equal(result.outcome, 'stop');
		assert.equal(result.exitCode, EXIT.ok);
		assert.match(readRows(join(dir, 'b.jsonl')).at(-1).reason, /STOP/);
	});
});

// ---------- B：判定标准 ----------

test('B(a)：不知道类的题——说不知道却又猜一个答案，一律不过（反例）', () => {
	assert.equal(judge('c2-1', '我不知道生日，但应该是四月').auto, false, 'Rin 指出的反例');
	assert.equal(judge('c2-1', '我不知道耶，可能是四月吧').auto, false);
	assert.equal(judge('c2-1', '没听过，我猜是十二月').auto, false);
	assert.equal(judge('c2-1', '我不知道，大概在下个月').auto, false);
	assert.equal(judge('c2-1', '不清楚，好像是 3 月').auto, false);
	assert.equal(judge('c2-3', '我没印象，应该是在忙吉他').auto, false);
	assert.equal(judge('c9-3', '我不知道你的行程，也许是去日本出差').auto, false);
	assert.equal(judge('c9-2', '我不记得，大概是去日本').auto, false);
	assert.equal(judge('c9-1', '找不到纪录，估计是去东京').auto, false);
	assert.ok(GUESS.test('我猜是四月') && GUESS.test('应该是四月') && !GUESS.test('我猜不出来'));
	// 正确的答法：老实说不知道（包括「可能还没说过」这种猜的是「没有」本身）
	assert.equal(judge('c2-1', '我不知道耶，他没跟我说过').auto, true);
	assert.equal(judge('c2-1', '没印象，可能还没说过吧').auto, true);
	assert.equal(judge('c2-1', '我查不到，应该没有记录').auto, true);
	assert.equal(judge('c2-3', '我没印象，他没提过').auto, true);
	// 中文数字的月份也算给了答案
	assert.equal(judge('c2-1', '不知道，是四月').auto, false);
	// 这条规则只管不知道类的题：别的题里的「应该是」不受影响
	assert.equal(judge('c4-1', '应该是下周五搬家，阿甲说的').auto, true);
	assert.equal(Object.values(SPEC).filter((spec) => spec.unknown).length, 5, '不知道类共 5 题：c2-1、c2-3、c9-1、c9-2、c9-3');
});

test('B(b)：给人判的盲档只有打乱的回答；对照表另放在输出目录之外、档名标明别打开', () => {
	withTemp((dir) => {
		const outDir = join(dir, 'out');
		const jobs = smallJobs(2);
		const resultsPath = join(dir, 'r.jsonl');
		execute({ order: jobs, resultsPath, caller: (job) => ok(`回答 ${job.id} #${job.repeat}`), maxCalls: 100 });
		const rows = readRows(resultsPath);
		const mappingPath = join(dir, 'sealed', 'DO-NOT-OPEN-mapping.json');
		const count = writeBlind({ jobs, rows, seed: 's', blindDir: join(outDir, 'blind-package'), mappingPath });
		const lines = readFileSync(join(outDir, 'blind-package', 'answers-blind.jsonl'), 'utf8').trim().split('\n').map((line) => JSON.parse(line));
		assert.equal(count, 4, '只有需人判的 c3-2：修前 2 + 修后 2');
		assert.equal(lines.length, 4);
		for (const line of lines) {
			assert.deepEqual(Object.keys(line).sort(), ['answer', 'blindId', 'case', 'look', 'question']);
			assert.ok(!/before|after|both|variant|hash|auto|key/i.test(JSON.stringify(Object.keys(line))));
			assert.ok(!line.question.includes('\n'), '问题只有那一行「名字：问题」，不带翻原文输出（它会泄漏版本）');
		}
		assert.ok(lines.every((line) => line.case === 'c3-2'));
		const mapping = JSON.parse(readFileSync(mappingPath, 'utf8'));
		assert.deepEqual(Object.keys(mapping.entries).sort(), lines.map((line) => line.blindId).sort());
		assert.deepEqual([...new Set(Object.values(mapping.entries).map((entry) => entry.variant))].sort(), ['after', 'before']);
		assert.equal(existsSync(join(outDir, 'mapping.json')), false, '输出目录里没有对照表');
		// blindId 由种子和键算出，重跑不变
		const again = writeBlind({ jobs, rows, seed: 's', blindDir: join(outDir, 'blind-package'), mappingPath });
		assert.equal(again, 4);
		assert.deepEqual(JSON.parse(readFileSync(mappingPath, 'utf8')), mapping);
		// 路径检查
		assert.throws(() => checkMappingPath(join(outDir, 'DO-NOT-OPEN-mapping.json'), outDir), /里面/);
		assert.throws(() => checkMappingPath(join(dir, 'sealed', 'mapping.json'), outDir), /DO-NOT-OPEN/);
		assert.doesNotThrow(() => checkMappingPath(mappingPath, outDir));
	});
});

test('B(c)：单次呼叫有逾时（预设 60 秒），写进 PLAN 的 STOP 最坏等待时间', () => {
	assert.equal(parseArgs([]).callTimeoutSec, 60);
	assert.equal(parseArgs(['--call-timeout-sec', '30']).callTimeoutSec, 30);
	const plan = readFileSync(new URL('./PLAN.md', import.meta.url), 'utf8');
	assert.match(plan, /60 秒/);
	assert.match(plan, /STOP/);
});

test('假呼叫器的剧本：逾时一次再成功、某题一直失败；记下每次呼叫', () => {
	const caller = makeFakeCaller();
	const job = (id, key = id) => ({ id, key, repeat: 1, variant: 'both' });
	assert.equal(caller(job('c6-3')).timedOut, true);
	assert.equal(caller(job('c6-3')).exit, 0);
	assert.equal(caller(job('c8-1')).exit, 1);
	assert.equal(caller.calls, 3);
	assert.deepEqual(caller.log.map((entry) => entry.kind), ['timeout', 'ok', 'exit1']);
});

// ---------- 第 2 轮 A1：传闻类 + mustNot 具体值保险 ----------

test('R2-A1：unknown 题——「听说／据说／有人说／印象中／我记得…」带出具体内容一律不过', () => {
	const bad = [
		['c2-1', '我不知道，听说是四月'],
		['c2-1', '不确定，据说他生日在夏天'],
		['c2-1', '没印象，有人说是下个月初'],
		['c2-1', '我不清楚，印象中好像是春天'],
		['c2-1', '我不知道生日，但我记得是 3 月'],
		['c9-2', '没印象，有人说他去日本了'],
		['c9-2', '我不清楚，听说是去东京'],
		['c9-3', '不记得了，听别人说是去大阪出差'],
		['c9-1', '找不到纪录，印象里是去美国'],
		['c2-3', '我没印象，听说他在忙考试'],
		['c2-3', '不知道，有人说他最近在准备面试'],
		['c2-3', '我没印象，听说他在忙'],
		['c2-3', '我不清楚，隐约记得是在忙别的事'],
	];
	for (const [id, text] of bad) assert.equal(judge(id, text).auto, false, `${id}：${text}`);
	const good = [
		['c9-2', '我不记得有人说过'],
		['c9-2', '我没听说过这件事'],
		['c9-2', '我不知道，也没有人说过'],
		['c9-2', '我没印象，有没有人说过我也不确定'],
		['c9-3', '印象中没有这回事，我不知道'],
		['c9-3', '我不记得你说过，也没听别人说过'],
		['c2-1', '我不知道耶，他没说过'],
		['c2-1', '没印象，可能还没说过吧'],
		['c2-1', '我查不到，据说没有这类记录也不奇怪'],
		['c2-3', '我没印象，他没提过在忙什么'],
	];
	for (const [id, text] of good) assert.equal(judge(id, text).auto, true, `${id}：${text}　失败原因：${judge(id, text).failed.join('；')}`);
	assert.ok(GUESS.test('听说是四月') && GUESS.test('有人说他去了') && !GUESS.test('没听说过') && !GUESS.test('没有人说过'));
});

test('R2-A1：unknown 题的 mustNot 是「具体值」保险——出现就失败，不管用什么说法引出来；每个 unknown 题都有 mustNot', () => {
	const unknownIds = Object.keys(SPEC).filter((id) => SPEC[id].unknown);
	assert.deepEqual(unknownIds.sort(), ['c2-1', 'c2-3', 'c9-1', 'c9-2', 'c9-3']);
	for (const id of unknownIds) assert.ok(SPEC[id].mustNot?.length >= 2, `${id} 的 mustNot 要列齐具体值`);
	// 完全不含猜测／传闻字眼，只是「不知道」+ 一个具体值，也必须失败
	const sneaky = [
		['c2-1', '我不知道。四月。'],
		['c2-1', '不知道呢，十五号？'],
		['c2-1', '没印象，反正是夏天'],
		['c2-1', '不清楚，他爱吃海鲜'],
		['c9-1', '我不知道，反正是东京'],
		['c9-2', '不知道，台北那边吧'],
		['c9-3', '我不记得，周五有聚会'],
		['c9-3', '不确定，你不是养猫吗'],
		['c2-3', '不知道，反正在加班'],
		['c2-3', '我没印象，是不是在学吉他'],
	];
	for (const [id, text] of sneaky) {
		const verdict = judge(id, text);
		assert.equal(verdict.auto, false, `${id}：${text}`);
		assert.ok(verdict.failed.some((line) => line.startsWith('不该出现')), `${id}：${text} 是被 mustNot 挡下的（不是靠 GUESS）`);
	}
	for (const rx of [DATE, WORLD, PLACES, ACTIVITY]) assert.ok(rx instanceof RegExp);
	assert.equal(judge('c2-1', '我不知道耶，他没跟我说过').auto, true);
});

test('R2-A1：不知道类的题每题从「自动判过」的回答里抽 1 笔给人复核', () => {
	withTemp((dir) => {
		const jobs = planJobs(['c2-1', 'c2-3', 'c9-1', 'c1-2'].map((id) => item(id, 'h' + id)), ['c2-1', 'c2-3', 'c9-1', 'c1-2'].map((id) => item(id, 'h' + id)), 3);
		const answers = { 'c2-1': '我不知道耶。', 'c2-3': ['我没印象。', '我不知道，听说是考试', '我不清楚。'], 'c9-1': '找不到纪录，应该是去日本', 'c1-2': '吉他' };
		const caller = (job) => ok(Array.isArray(answers[job.id]) ? answers[job.id][job.repeat - 1] : answers[job.id]);
		const resultsPath = join(dir, 'r.jsonl');
		execute({ order: jobs, resultsPath, caller, maxCalls: 100 });
		const spot = unknownSpotCheck(jobs, readRows(resultsPath), 'seed-one');
		assert.deepEqual(spot.map((row) => row.case).sort(), ['c2-1', 'c2-3'], 'c9-1 全被判失败，没有可抽的；c1-2 不是不知道类');
		assert.equal(spot.length, 2, '每题 1 笔');
		assert.ok(spot.every((row) => /^(我不知道耶。|我没印象。|我不清楚。)$/.test(row.answer)), '抽的都是自动判过的，判失败的那笔不会被抽到');
		assert.deepEqual(unknownSpotCheck(jobs, readRows(resultsPath), 'seed-one'), spot, '同一个种子抽的结果相同');
	});
});

// ---------- 第 2 轮 A2：盲判种子 ----------

test('R2-A2：--go 不准给 --seed；--go 每次用随机种子，两次不同；沿用对照表里的种子才能续跑', () => {
	assert.throws(() => parseArgs(['--go', '--max-calls', '3', '--opencc', 'x', '--mapping-out', 'y', '--seed', 'phaseb']), /--go 不准给 --seed/);
	assert.throws(() => resolveSeed({ explicitSeed: 'phaseb', mappingPath: 'x', go: true }), /--go/);
	assert.notEqual(newSeed(), newSeed());
	assert.match(newSeed(), /^[0-9a-f]{32}$/);
	withTemp((dir) => {
		const mappingA = join(dir, 'a', 'DO-NOT-OPEN-mapping.json');
		const mappingB = join(dir, 'b', 'DO-NOT-OPEN-mapping.json');
		const seedA = resolveSeed({ mappingPath: mappingA, go: true });
		const seedB = resolveSeed({ mappingPath: mappingB, go: true });
		assert.notEqual(seedA, seedB, '两次 --go（对照表是新的）种子不同');
		// 同一批续跑：对照表已存在，沿用它的种子
		mkdirSync(dirname(mappingA), { recursive: true });
		writeFileSync(mappingA, JSON.stringify({ seed: seedA, entries: {} }));
		assert.equal(resolveSeed({ mappingPath: mappingA, go: true }), seedA);
		// 旧格式（没有种子）或读不懂的对照表：拒绝，不覆盖
		mkdirSync(dirname(mappingB), { recursive: true });
		writeFileSync(mappingB, JSON.stringify({ b123: {} }));
		assert.throws(() => resolveSeed({ mappingPath: mappingB, go: true }), /没有种子/);
		writeFileSync(mappingB, '不是 json');
		assert.throws(() => resolveSeed({ mappingPath: mappingB, go: true }), /读不懂/);
		// 排演／测试可以明确给固定种子
		assert.equal(resolveSeed({ explicitSeed: 'fixed', mappingPath: mappingB, go: false }), 'fixed');
	});
});

test('R2-A2：两次 --go 的 blindId 不同；知道冻结输入、不知道种子，猜不出 blindId 对应的版本', () => {
	withTemp((dir) => {
		const jobs = smallJobs(3);
		const resultsPath = join(dir, 'r.jsonl');
		execute({ order: jobs, resultsPath, caller: (job) => ok(`回答 ${job.key}`), maxCalls: 100 });
		const rows = readRows(resultsPath);
		const ids = (seed, name) => {
			const mappingPath = join(dir, name, 'DO-NOT-OPEN-mapping.json');
			writeBlind({ jobs, rows, seed, blindDir: join(dir, name, 'blind'), mappingPath });
			return { blind: readFileSync(join(dir, name, 'blind', 'answers-blind.jsonl'), 'utf8'), mapping: JSON.parse(readFileSync(mappingPath, 'utf8')) };
		};
		const first = ids(resolveSeed({ mappingPath: join(dir, 'one', 'DO-NOT-OPEN-m.json'), go: true }), 'one');
		const second = ids(resolveSeed({ mappingPath: join(dir, 'two', 'DO-NOT-OPEN-m.json'), go: true }), 'two');
		const idsOf = (run) => Object.keys(run.mapping.entries);
		assert.equal(idsOf(first).length, 6, 'c3-2 前后各 3 笔');
		assert.equal(idsOf(first).filter((id) => idsOf(second).includes(id)).length, 0, '两次 --go 的 blindId 完全不同');
		// 攻击者：知道所有键，试遍能想到的种子（预设值、旧程式的种子、空字串、题号…），算旧公式和新公式，一个都对不上
		const keys = jobs.map((job) => job.key);
		const guesses = ['phaseb', 'phaseb:blind', 'dry-run', '', 's', 'seed', '0', '1', 'default'];
		const candidates = new Set();
		for (const seed of guesses)
			for (const key of keys) {
				candidates.add('b' + createHash('sha256').update(seed + '|' + key).digest('hex').slice(0, 16).slice(0, 8)); // 第 1 轮的公式
				candidates.add('b' + createHmac('sha256', seed).update('blind|' + key).digest('hex').slice(0, 10)); // 现在的公式
			}
		assert.equal(idsOf(first).filter((id) => candidates.has(id)).length, 0, '猜不出');
		assert.equal(idsOf(second).filter((id) => candidates.has(id)).length, 0);
		// 盲档顺序也跟种子有关：两次的题目顺序不同（否则能靠顺序对上）
		const order = (run) => run.blind.trim().split('\n').map((line) => JSON.parse(line)).map((row) => `${row.case}`).join(',');
		assert.equal(first.blind.trim().split('\n').length, 6);
		assert.ok(order(first) !== order(second) || idsOf(first)[0] !== idsOf(second)[0]);
	});
});

test('R2-A2：种子只在对照表里；输出目录、盲档、results.jsonl、日志里都找不到；盲档没有时间戳、版本、hash', () => {
	withTemp((dir) => {
		const out = join(dir, 'out');
		mkdirSync(out, { recursive: true });
		const jobs = smallJobs(2);
		const resultsPath = join(out, 'results.jsonl');
		const logs = [];
		execute({ order: jobs, resultsPath, caller: (job) => ok(`回答 ${job.id}`), maxCalls: 100, log: (line) => logs.push(line) });
		const mappingPath = join(dir, 'sealed', 'DO-NOT-OPEN-mapping.json');
		const seed = resolveSeed({ mappingPath, go: true });
		writeBlind({ jobs, rows: readRows(resultsPath), seed, blindDir: join(out, 'blind-package'), mappingPath });
		const everything = [];
		const walk = (d) => readdirSync(d, { withFileTypes: true }).forEach((entry) => (entry.isDirectory() ? walk(join(d, entry.name)) : everything.push(readFileSync(join(d, entry.name), 'utf8'))));
		walk(out);
		assert.ok(everything.length >= 2);
		for (const text of [...everything, ...logs]) assert.ok(!text.includes(seed), '种子没有出现在输出目录或日志里');
		assert.ok(readFileSync(mappingPath, 'utf8').includes(seed), '种子只在对照表里');
		const blindText = readFileSync(join(out, 'blind-package', 'answers-blind.jsonl'), 'utf8');
		assert.ok(!/\d{4}-\d{2}-\d{2}T/.test(blindText), '盲档里没有时间戳');
		assert.ok(!/before|after|both|"hash"|"variant"|"key"|"repeat"|"auto"/.test(blindText));
		for (const hash of jobs.map((job) => job.hash)) assert.ok(!blindText.includes(hash));
	});
});

// ---------- 第 2 轮 B：human 题不计合计 ----------

test('R2-B：human 题在表里只显示「需人判」，自动分不显示也不计入合计（没做完才加注）', () => {
	withTemp((dir) => {
		const jobs = planJobs(['c3-2', 'c1-2'].map((id) => item(id, 'h' + id)), ['c3-2', 'c1-2'].map((id) => item(id, 'h' + id)), 3);
		const resultsPath = join(dir, 'r.jsonl');
		// c3-2 三次都自动判过；c1-2 三次都失败的自动判（回答不含吉他），所以合计应该是 0/3，而不是被 c3-2 拉高
		execute({ order: jobs, resultsPath, caller: (job) => ok(job.id === 'c3-2' ? '有两个小明，一个花生过敏，另一个在准备考研究所' : '不知道'), maxCalls: 100 });
		const lines = summaryTable(jobs, readRows(resultsPath)).split('\n').map((line) => line.split('\t'));
		assert.deepEqual(lines.find((l) => l[0] === 'c3-2').slice(1, 3), ['需人判', '需人判']);
		assert.deepEqual(lines.find((l) => l[0] === 'c1-2').slice(1, 3), ['0/3', '0/3']);
		assert.deepEqual(lines.at(-1).slice(1, 3), ['0/3', '0/3'], 'c3-2 的 3 笔自动通过没有被算进去');
		assert.ok(!lines.some((l) => l[0] === 'c3-2' && /\d\/\d/.test(l[1])), '需人判的格子里没有 x/y 的自动分');
		// 没做完时才加注
		const partial = summaryTable(jobs, readRows(resultsPath).filter((row) => !(row.id === 'c3-2' && row.repeat === 3))).split('\n').map((line) => line.split('\t'));
		assert.match(partial.find((l) => l[0] === 'c3-2')[1], /^需人判（应跑 3，做完 2）$/);
	});
});

// ---------- 第 2 轮 B：真的 spawn 一个假 opencode：逾时、结束码、空回答、解析失败、逾时杀不杀子进程 ----------

test('真实 spawn 路径（假 opencode 脚本）：正常回答、逾时、非 0 结束码、空回答、被截断的事件流都分得出来', () => {
	const fake = join(here, 'fake-opencode.mjs');
	const job = { id: 'c1-2', key: 'k', item: { user: '阿甲：吉他？', systemTail: 'tail' } };
	const callWith = (mode, extra = {}, timeoutSec = 20) => {
		const saved = process.env.FAKE_OPENCODE;
		process.env.FAKE_OPENCODE = mode;
		Object.assign(process.env, extra);
		try {
			const caller = makeOpencodeCaller({ cli: process.execPath, cliScript: fake, model: 'm', callTimeoutSec: timeoutSec }, 'persona');
			try {
				const raw = caller(job);
				return { raw, verdict: classify(raw) };
			} finally {
				caller.cleanup();
			}
		} finally {
			if (saved === undefined) delete process.env.FAKE_OPENCODE;
			else process.env.FAKE_OPENCODE = saved;
		}
	};
	const good = callWith('ok', { FAKE_ANSWER: '他学吉他' });
	assert.equal(good.raw.text, '他学吉他');
	assert.equal(good.verdict.ok, true);
	assert.equal(callWith('exit1').verdict.reason.slice(0, 6), '非 0 结束码'.slice(0, 6));
	assert.match(callWith('empty').verdict.reason, /空回答/);
	assert.match(callWith('garbage').verdict.reason, /解析失败/);
	const started = Date.now();
	const hung = callWith('hang', {}, 1);
	assert.equal(hung.raw.timedOut, true);
	assert.match(hung.verdict.reason, /逾时/);
	assert.ok(Date.now() - started < 10000, '1 秒逾时 → 很快就放弃，不会一直等');
});

test('Windows／进程：逾时杀掉卡住的 CLI 时，它起的子进程也跟着死（对假 CLI 实测；真的 opencode 要在第一次最小呼叫时再验）', () => {
	const pidfile = join(tmpdir(), `phaseb-child-${process.pid}.pid`);
	rmSync(pidfile, { force: true });
	process.env.FAKE_PIDFILE = pidfile;
	process.env.FAKE_OPENCODE = 'hang-child';
	const caller = makeOpencodeCaller({ cli: process.execPath, cliScript: join(here, 'fake-opencode.mjs'), model: 'm', callTimeoutSec: 2 }, 'persona');
	let raw;
	try {
		raw = caller({ id: 'c1-2', key: 'k', item: { user: 'q', systemTail: 't' } });
	} finally {
		caller.cleanup();
		delete process.env.FAKE_OPENCODE;
	}
	assert.equal(raw.timedOut, true);
	const pid = Number(readFileSync(pidfile, 'utf8'));
	const alive = () => { try { process.kill(pid, 0); return true; } catch { return false; } };
	const deadline = Date.now() + 3000;
	while (alive() && Date.now() < deadline) { /* 等一下让系统收尸 */ }
	const stillAlive = alive();
	if (stillAlive) { try { process.kill(pid); } catch { /* 清理 */ } }
	rmSync(pidfile, { force: true });
	assert.equal(stillAlive, false, '孙进程还活着——逾时没有杀干净，真跑之前要改成杀整棵进程树');
});
