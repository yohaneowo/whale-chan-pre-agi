// 假呼叫器：排演（--rehearse）和测试用，不呼叫任何模型。回答是事先写好的字串（不是真模型的输出），失败是照剧本演的。
//   makeFakeCaller({ answers, failOnce, failAlways })
//     answers    题号 → 回答（字串，或 (job) => 字串）；没写的题回「嗯嗯，我想想」
//     failOnce   Set<题号>：这些题每个工作的第一次呼叫逾时，重试时正常
//     failAlways Set<题号>：这些题每次呼叫都回非 0 结束码
// 回传的函式带 .calls（目前呼叫次数）与 .log（每次呼叫的 { id, repeat, variant, kind }）。
const GOOD = {
	'c1-1': '猫咪结扎之后要先隔离观察几天，状况还好。',
	'c1-2': '阿甲最近在学吉他。',
	'c1-7': '你对海鲜过敏啦。',
	'c2-1': '阿丙的生日我没听过，不知道耶。',
	'c2-2': '没有人跟我提过冰岛，我不知道。',
	'c2-3': '阿戊最近在忙什么我没印象，不知道。',
	'c3-1': '你说你在准备考研究所。',
	'c4-1': '阿甲下周五要搬家，需要人帮忙。',
	'c9-1': '我找不到他出差的纪录，不知道。',
	'c9-2': '我不记得有人说过。',
	// 故意示范「说不知道又猜一个」会被判不过
	'c9-3': (job) => (job.repeat % 2 ? '我不知道你的行程，但应该是去日本出差吧。' : '我没印象你说过。'),
};

export function makeFakeCaller({ answers = GOOD, failOnce = new Set(['c6-3']), failAlways = new Set(['c8-1']) } = {}) {
	const seenFirst = new Set();
	const caller = (job) => {
		caller.calls++;
		const log = (kind) => caller.log.push({ id: job.id, repeat: job.repeat, variant: job.variant, kind });
		if (failAlways.has(job.id)) { log('exit1'); return { text: '', exit: 1, events: 0, bad: 0, stderr: 'simulated crash', ms: 5 }; }
		if (failOnce.has(job.id) && !seenFirst.has(job.key)) { seenFirst.add(job.key); log('timeout'); return { text: '', exit: null, timedOut: true, events: 0, bad: 0, ms: 60000 }; }
		const answer = answers[job.id];
		const text = typeof answer === 'function' ? answer(job) : (answer ?? '嗯嗯，我想想。');
		log('ok');
		return { text, tokens: { input: 7200, output: text.length }, exit: 0, events: 3, bad: 0, ms: 12 };
	};
	caller.calls = 0;
	caller.log = [];
	return caller;
}
