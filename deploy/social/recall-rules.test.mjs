// 翻旧事的规则（recall-rules.mjs）与背景整理批次帐（notes-batches.mjs）的单元测试。
// 整条路径（真的 social runtime、送进模型的内容）在 deploy/dsh/memory-rules-repro.mjs 用 release 副本跑。
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { NoteBatches, failureKind } from './notes-batches.mjs';
import {
	ChannelVisibility,
	MEMORY_MANNERS,
	RECALL_WINDOW_MS,
	datedRange,
	inWindow,
	placeOf,
	recallPlan,
	withTopic,
} from './recall-rules.mjs';

const DAY = 86_400_000;
// 2026-11-28 14:46（台北）。
const NOW = Date.parse('2026-11-28T14:46:00+08:00');

test('打招呼、闲聊、只 @ 不翻；明确问旧事、提到人、日期、情绪、频道才翻', () => {
	for (const text of [
		'早安',
		'真的是玩坏了这个肥鱼',
		'真的是玩壞了這個肥魚',
		'<@123456789012345678>',
		'哈哈笑死',
		'在嗎',
	])
		assert.equal(recallPlan(text, { now: NOW }).recall, false, text);
	assert.deepEqual(recallPlan('上次那個包裹到了嗎', { now: NOW }).clues, [
		'asking',
	]);
	assert.ok(recallPlan('你還記得我養的貓嗎', { now: NOW }).explicit);
	assert.ok(recallPlan('今天好累喔', { now: NOW }).clues.includes('feeling'));
	assert.ok(recallPlan('聚餐是晚上幾點', { now: NOW }).clues.includes('time'));
	assert.ok(
		recallPlan('誰有固定出去流汗的習慣', { now: NOW }).clues.includes('person'),
	);
	assert.equal(recallPlan('明天見', { now: NOW }).recall, false);
	assert.ok(
		recallPlan('阿明今天沒來', { now: NOW, names: ['阿明'] }).clues.includes(
			'person',
		),
	);
	assert.ok(
		recallPlan('<@555555555555555555> 在嗎', {
			now: NOW,
			humans: new Set(['555555555555555555']),
		}).clues.includes('person'),
	);
	const channel = recallPlan('b-room 那邊在吵什麼', {
		now: NOW,
		channels: new Map([['42424242', 'b-room']]),
	});
	assert.ok(channel.channels.has('42424242'));
	// 话题线索：范围内的搜寻有强的命中才算。
	assert.equal(
		withTopic(recallPlan('今天吃拉麵', { now: NOW }), true).recall,
		true,
	);
	assert.equal(
		withTopic(recallPlan('今天吃拉麵', { now: NOW }), false).recall,
		false,
	);
});

test('平常只翻 6 个月内；提到日期就翻那段时间（可以更早）', () => {
	const plain = recallPlan('上次潛水看到海龜是在哪', { now: NOW });
	assert.equal(plain.until, NOW);
	assert.equal(plain.since, NOW - RECALL_WINDOW_MS);
	assert.equal(inWindow(NOW - 250 * DAY, plain), false);
	assert.equal(inWindow(NOW - 4 * DAY, plain), true);
	const march = recallPlan('3月潛水看到海龜那次', { now: NOW });
	assert.ok(march.dated);
	assert.equal(inWindow(Date.parse('2026-03-20T12:00:00+08:00'), march), true);
	assert.equal(inWindow(Date.parse('2026-04-02T12:00:00+08:00'), march), false);
	// 月日：取最近一次已经过去的那天，前后放宽两天。
	const day = datedRange('11/24 說的', { now: NOW });
	assert.ok(
		day.since <= Date.parse('2026-11-24T00:00:00+08:00') &&
			day.until >= Date.parse('2026-11-24T23:59:00+08:00'),
	);
	assert.ok(
		datedRange('12月25日那天', { now: NOW }).until < NOW,
		'還沒到的 12/25 是去年的',
	);
	assert.equal(
		new Date(datedRange('2025年5月', { now: NOW }).since).getUTCMonth(),
		3,
		'台北 5/1 00:00 是 UTC 4/30',
	);
	assert.ok(
		datedRange('去年', { now: NOW }).until <
			Date.parse('2026-01-01T00:00:00+08:00'),
	);
	assert.ok(datedRange('三個月前', { now: NOW }).since < NOW - 90 * DAY);
	assert.equal(datedRange('3個月', { now: NOW }), undefined);
	assert.equal(datedRange('今天好熱', { now: NOW }), undefined);
});

test('范围：同一个频道可以；别的公开频道要点名或明确在问、并标来源；私讯与不公开的频道不拿到别处', () => {
	const isPublic = (id) => ['A', 'B'].includes(id);
	const here = { guildId: 'G', channelId: 'A', dm: false };
	const chat = recallPlan('今天好熱', { now: NOW });
	const asking = recallPlan('上次說的暗號', { now: NOW });
	const named = { ...chat, recall: true, channels: new Set(['B']) };
	const row = (channel, scope = 'group', guild = 'G') => ({
		channel_id: channel,
		guild_id: scope === 'dm' ? null : guild,
		scope,
	});
	assert.equal(placeOf(row('A'), here, chat, isPublic), 'here');
	assert.equal(
		placeOf(row('B'), here, chat, isPublic),
		null,
		'閒聊不翻別的頻道',
	);
	assert.equal(placeOf(row('B'), here, asking, isPublic), 'elsewhere');
	assert.equal(placeOf(row('B'), here, named, isPublic), 'elsewhere');
	assert.equal(placeOf(row('P'), here, asking, isPublic), null, '不公開的頻道');
	assert.equal(
		placeOf(row('B', 'group', 'H'), here, asking, isPublic),
		null,
		'別的伺服器',
	);
	assert.equal(
		placeOf(row('D', 'dm'), here, asking, isPublic),
		null,
		'群聊看不到私訊',
	);
	// 私讯：同一个私讯可以，公开频道可以（标来源），不公开的频道不行。
	const dm = { guildId: null, channelId: 'D', dm: true };
	assert.equal(placeOf(row('D', 'dm'), dm, asking, isPublic), 'here');
	assert.equal(placeOf(row('E', 'dm'), dm, asking, isPublic), null);
	assert.equal(placeOf(row('A'), dm, asking, isPublic), 'elsewhere');
	assert.equal(placeOf(row('P'), dm, asking, isPublic), null);
});

test('频道可见性：@everyone 看得到才算公开；讨论串跟着上层，私人讨论串不公开；问不到就当不公开', async () => {
	const VIEW = String(1n << 10n);
	const answers = {
		'/guilds/90000': {
			id: '90000',
			roles: [{ id: '90000', permissions: VIEW }],
		},
		'/guilds/90000/channels': [
			{ id: '11111', name: 'a-room', permission_overwrites: [] },
			{
				id: '22222',
				name: 'secret',
				permission_overwrites: [{ id: '90000', allow: '0', deny: VIEW }],
			},
			{
				id: '33333',
				name: 'opened',
				permission_overwrites: [{ id: '90000', allow: VIEW, deny: '0' }],
			},
		],
		'/channels/44444': {
			id: '44444',
			guild_id: '90000',
			type: 11,
			parent_id: '11111',
			name: 'thread',
		},
		'/channels/55555': {
			id: '55555',
			guild_id: '90000',
			type: 12,
			parent_id: '11111',
			name: 'private-thread',
		},
		'/channels/66666': {
			id: '66666',
			guild_id: '90000',
			type: 11,
			parent_id: '22222',
			name: 'secret-thread',
		},
	};
	const visibility = new ChannelVisibility(async (path) => {
		if (!(path in answers)) throw new Error('404');
		return answers[path];
	});
	assert.equal(visibility.isPublic('11111'), false, '還沒載入就當不公開');
	await visibility.warm('90000');
	assert.equal(visibility.isPublic('11111'), true);
	assert.equal(visibility.isPublic('22222'), false);
	assert.equal(visibility.isPublic('33333'), true);
	for (const id of ['44444', '55555', '66666', '77777'])
		await visibility.warmChannel(id);
	assert.equal(visibility.isPublic('44444'), true);
	assert.equal(visibility.isPublic('55555'), false);
	assert.equal(visibility.isPublic('66666'), false);
	assert.equal(visibility.isPublic('77777'), false);
	assert.equal(visibility.names('90000').get('11111'), 'a-room');
	// 载入失败：照旧当成不公开，不丢错。
	const broken = new ChannelVisibility(async () => {
		throw new Error('down');
	});
	await broken.warm('90000');
	assert.equal(broken.isPublic('11111'), false);
});

test('行为规则写着 keeper 的几条，用她的口吻', () => {
	for (const rule of [
		'記得不等於要說',
		'以現在說的為準',
		'不要編',
		'具體的那一刻',
		'舊帳',
		'日記',
	])
		assert.ok(MEMORY_MANNERS.includes(rule), rule);
});

test('背景整理批次帐：失败留下步骤与类别（不留内文）、到时间才重试、做过的步骤记着、完成后不再收同一批', () => {
	const root = mkdtempSync(join(tmpdir(), 'note-batches-'));
	try {
		let now = 1000;
		const batches = new NoteBatches(root, {
			maxAttempts: 2,
			retryMs: 100,
			now: () => now,
		});
		const lines = [
			{
				role: 'user',
				speaker: '1',
				name: '甲',
				text: '我的貓叫麻糬',
				id: '123456',
			},
		];
		const batch = batches.open(lines, '#general', false, {
			channelId: 'C',
			guildId: 'G',
		});
		assert.equal(
			batches.open(lines, '#general', false).id,
			batch.id,
			'同一批只有一份',
		);
		const failure = batches.fail(
			batch,
			'model',
			Object.assign(new Error('500 key sk-secret 麻糬'), { status: 500 }),
		);
		assert.deepEqual(
			[failure.stage, failure.kind, failure.attempts, failure.gaveUp],
			['model', 'http-500', 1, false],
		);
		const saved = readFileSync(join(root, batch.id + '.json'), 'utf8');
		assert.ok(!JSON.stringify(JSON.parse(saved).failure).includes('sk-secret'));
		assert.equal(batches.due().length, 0, '還沒到重試時間');
		now += 100;
		assert.equal(batches.due().length, 1);
		batches.mark(batch, { people: ['1'] });
		assert.deepEqual(batches.read(batch.id).step.people, ['1']);
		batches.finish(batch);
		assert.equal(
			batches.open(lines, '#general', false),
			undefined,
			'整理完的不再收',
		);
		// 超过次数：移到 failed/，不删。
		const other = batches.open([
			{ role: 'user', speaker: '2', text: 'x', id: '654321' },
		]);
		batches.fail(other, 'parse', new SyntaxError('Unexpected token'));
		assert.equal(
			batches.fail(other, 'parse', new SyntaxError('Unexpected token')).gaveUp,
			true,
		);
		assert.deepEqual(readdirSync(join(root, 'failed')), [other.id + '.json']);
		assert.equal(batches.due().length, 0);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test('失败类别：只留类别，不留错误内文', () => {
	assert.equal(
		failureKind(Object.assign(new Error('x'), { code: 'EACCES' })),
		'fs-EACCES',
	);
	assert.equal(
		failureKind(new Error('request failed 429 Too Many Requests sk-abc')),
		'http-429',
	);
	assert.equal(failureKind(new SyntaxError('Unexpected token 麻')), 'bad-json');
	assert.equal(
		failureKind(Object.assign(new Error('x'), { name: 'TimeoutError' })),
		'timeout',
	);
	assert.equal(
		failureKind(Object.assign(new Error('x'), { kind: 'no-text' })),
		'no-text',
	);
	assert.equal(failureKind(new Error('奇怪的錯 sk-abc')), 'error');
});
