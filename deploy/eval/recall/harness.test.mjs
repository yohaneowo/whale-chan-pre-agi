// harness 自己的测试（反证）：评分的检查真的会因为被测的行为改变而翻转，案例的背景没有偷塞答案，结果可重复。
//   node --test harness.test.mjs        （环境变数 FISH_DIR、OPENCC_DIR 同 run.mjs）
import assert from 'node:assert/strict';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { CASES } from './cases.mjs';
import { POOL_WORDS, estimateTokens, guardMirror, headerBefore, loadDeps, makeFiller, makeMemoryFiller } from './harness.mjs';
import { digest, runAll } from './run.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const socialDir = resolve(process.env.SOCIAL_DIR ?? join(here, '..', '..', 'social'));
const deps = await loadDeps({
	socialDir,
	fishDir: resolve(process.env.FISH_DIR ?? '/fish'),
	openccDir: resolve(process.env.OPENCC_DIR ?? '/opencc'),
});
const verdicts = (run) => Object.fromEntries(run.results.map((row) => [row.id, row.verdict]));

test('案例清单：9 类、每类至少 2 题、id 不重复', () => {
	const ids = CASES.map((row) => row.id);
	assert.equal(new Set(ids).size, ids.length);
	const counts = {};
	for (const row of CASES) counts[row.cat] = (counts[row.cat] ?? 0) + 1;
	assert.deepEqual(Object.keys(counts).sort(), ['1', '2', '3', '4', '5', '6', '7', '8', '9']);
	for (const [cat, count] of Object.entries(counts)) assert.ok(count >= 2, `第 ${cat} 类只有 ${count} 题`);
});

test('index.mjs 里被镜像的接线都还在', () => {
	assert.deepEqual(guardMirror(socialDir), []);
});

test('背景闲聊的词池不含案例主题字词，且产生器可重复', () => {
	const topics = ['貓', '結紮', '吉他', '羽球', '運動', '搬家', '箱子', '聚餐', '手機', '活動', '副本', '公會', '花生', '過敏', '考試', '研究所', '手術', '醫院', '主人', '儲值', '出差', '日本', '香菜', '海鮮', '鑰匙', '生日', '冰島', '新竹', '聚會'];
	for (const word of POOL_WORDS) for (const topic of topics) assert.ok(!word.includes(topic), `${word} 含 ${topic}`);
	const lines = makeFiller(3000).concat(makeMemoryFiller(300).map((row) => row.text));
	for (const line of lines) for (const topic of topics) assert.ok(!line.includes(topic), `背景「${line}」含案例主题 ${topic}`);
	assert.deepEqual(makeFiller(500), makeFiller(500));
	assert.deepEqual(makeMemoryFiller(50), makeMemoryFiller(50));
});

test('headerBefore 找得到最近的标题行', () => {
	const prompt = '甲\n標題一：\n- a\n- b\n標題二（說明）：\n- c';
	assert.equal(headerBefore(prompt, 'b'), '標題一：');
	assert.equal(headerBefore(prompt, 'c'), '標題二（說明）：');
	assert.equal(headerBefore(prompt, 'zzz'), undefined);
	assert.ok(estimateTokens('你好abcd') >= 2);
});

const SUBSET = ['c1-1', 'c2-1', 'c4-3', 'c6-2', 'c9-1', 'c9-4'];

test('可重复：同样的输入跑两次，结果完全相同', async () => {
	const header = { subset: SUBSET };
	const first = await runAll(deps, { only: SUBSET });
	const second = await runAll(deps, { only: SUBSET });
	assert.equal(digest(header, first.results), digest(header, second.results));
});

test('反证：想起旧话被关掉，c1-1 要翻成失败，不相干的 c2-1 仍然通过', async () => {
	// 移植后的 release 想起旧话有两条路：整句比对（related）与回忆守门员给的搜寻词（search），两条都关掉。
	const real = deps.ArchiveStore.prototype.related;
	const search = deps.ArchiveStore.prototype.search;
	deps.ArchiveStore.prototype.related = () => [];
	deps.ArchiveStore.prototype.search = () => [];
	try {
		const run = verdicts(await runAll(deps, { only: ['c1-1', 'c2-1'] }));
		assert.equal(run['c1-1'], 'FAIL');
		assert.equal(run['c2-1'], 'PASS');
	} finally {
		deps.ArchiveStore.prototype.related = real;
		deps.ArchiveStore.prototype.search = search;
	}
});

test('反证：帐本不套用删除，c9-1 要翻成失败', async () => {
	const real = deps.ArchiveStore.prototype.record;
	deps.ArchiveStore.prototype.record = function (kind, ...rest) {
		return kind === 'delete' ? undefined : real.call(this, kind, ...rest);
	};
	try {
		assert.equal(verdicts(await runAll(deps, { only: ['c9-1'] }))['c9-1'], 'FAIL');
	} finally {
		deps.ArchiveStore.prototype.record = real;
	}
	assert.equal(verdicts(await runAll(deps, { only: ['c9-1'] }))['c9-1'], 'PASS');
});

test('反证：忘掉不生效，c4-3 与 c9-4 要翻成失败', async () => {
	const real = deps.MemoryStore.prototype.forget;
	deps.MemoryStore.prototype.forget = () => false;
	try {
		const run = verdicts(await runAll(deps, { only: ['c4-3', 'c9-4'] }));
		assert.equal(run['c4-3'], 'FAIL');
		assert.equal(run['c9-4'], 'FAIL');
	} finally {
		deps.MemoryStore.prototype.forget = real;
	}
});

test('反证：群聊会话的搜索把私讯也算进去，c6-2 要翻成失败', async () => {
	const real = deps.ArchiveStore.prototype.search;
	// 移植后的 release 另外有一道范围过滤（filter）也会挡私讯：两道都拿掉。
	deps.ArchiveStore.prototype.search = function (options) {
		return real.call(this, {
			...options,
			scopes: ['group', 'dm'],
			filter: undefined,
		});
	};
	try {
		assert.equal(verdicts(await runAll(deps, { only: ['c6-2'] }))['c6-2'], 'FAIL');
	} finally {
		deps.ArchiveStore.prototype.search = real;
	}
});

test('反证：人物卡不分人（全部带出），c3-1 要翻成失败', async () => {
	const real = deps.NotesStore.prototype.context;
	deps.NotesStore.prototype.context = function () {
		return ['X1', 'X2'].map((key) => this.card(key === 'X1' ? '910000000000000021' : '910000000000000022')).map((card) => card.notes.map((line) => '- ' + this.cite(line)).join('\n')).join('\n');
	};
	try {
		assert.equal(verdicts(await runAll(deps, { only: ['c3-1'] }))['c3-1'], 'FAIL');
	} finally {
		deps.NotesStore.prototype.context = real;
	}
});
