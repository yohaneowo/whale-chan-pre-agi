import assert from 'node:assert/strict';
import test from 'node:test';
import { calledAsFish, filterDiscordMessage } from './discord-policy.mjs';

test('叫名字与回复 bot 都不需要再次提及', () => {
	const base = {
		guild_id: 'guild',
		author: { id: 'member' },
		content: '鲸鱼娘，你觉得呢？',
	};
	assert.equal(filterDiscordMessage(base, '123'), base.content);
	for (const name of ['小肥魚', '小鯨魚', '大肥鱼'])
		assert.equal(filterDiscordMessage({ ...base, content: name }, '123'), name);
	assert.equal(
		filterDiscordMessage(
			{
				...base,
				content: '继续',
				referenced_message: { author: { id: '123' } },
			},
			'123',
		),
		'继续',
	);
	assert.equal(
		filterDiscordMessage(
			{
				...base,
				content: '继续',
				referenced_message: { author: { id: 'other' } },
			},
			'123',
		),
		null,
	);
});

const message = (content, extra = {}) => ({
	guild_id: 'test-guild',
	author: { id: 'member' },
	content,
	...extra,
});

test('普通文字、全体与角色提及留待判断，明确提及属于直接呼叫', () => {
	for (const text of [
		'你好',
		'@everyone 你好',
		'<@&123> 你好',
		'<@1234> 你好',
		'/unknown',
	]) {
		assert.equal(filterDiscordMessage(message(text), '123'), null);
	}
	assert.equal(filterDiscordMessage(message('<@123> 你好'), '123'), '你好');
	assert.equal(filterDiscordMessage(message('你好 <@!123>'), '123'), '你好');
});

test('管理命令保留，但前缀碰撞不能绕过提及条件', () => {
	assert.equal(filterDiscordMessage(message('/new 测试'), '123'), '/new 测试');
	assert.equal(filterDiscordMessage(message('/new-secret'), '123'), null);
	assert.equal(
		filterDiscordMessage(message('<@123> /unknown'), '123'),
		'/unknown',
	);
});

test('机器人不回应，私聊保留普通内容；缺少 bot 身分时拒绝群聊', () => {
	assert.equal(
		filterDiscordMessage(
			message('<@123> hi', { author: { id: 'other', bot: true } }),
			'123',
		),
		null,
	);
	assert.equal(
		filterDiscordMessage(
			message('<@123> hi', { author: { id: '123' } }),
			'123',
		),
		null,
	);
	assert.equal(
		filterDiscordMessage(message('你好', { guild_id: undefined }), '123'),
		'你好',
	);
	assert.equal(filterDiscordMessage(message('<@123> hi'), undefined), null);
});

test('群友叫「魚」算在叫她，一般说到鱼不算', () => {
	// 都是群里真的出现过的叫法。
	for (const text of [
		'鱼',
		'魚！',
		'鱼鱼鱼鱼鱼鱼鱼！',
		'oi 鱼',
		'ok鱼',
		'@鱼',
		'鱼？',
		'鱼~',
		'你在干嘛鱼',
		'你没事吧鱼',
		'没在聊天的话，你在干嘛，鱼',
		'晚安，鱼',
		'请开始您的表演，鱼，重要的事情要说三遍',
		'鱼，我在直播，你看得到吗',
		'鱼看得到吗',
		'鱼你的余额剩多少',
		'鱼我加你的粮了你看得到吗',
	])
		assert.ok(calledAsFish(text), text);
	for (const text of [
		'我在他原本的鱼缸里面啊',
		'我们这边养鱼',
		'我们养的鱼',
		'晚餐吃鱼',
		'它是弱丁鱼，你是傻鱼',
		'那人家鱿鱼娘还会炒饭',
		'偶尔查一下鱼的余额',
		'鱼香肉丝好吃',
		'这条鱼10块钱可以活蛮久的',
	])
		assert.ok(!calledAsFish(text), text);
	assert.equal(filterDiscordMessage(message('晚安，鱼'), '123'), '晚安，鱼');
	assert.equal(filterDiscordMessage(message('我们这边养鱼'), '123'), null);
});
