// drawIntent（这一则是不是在请她画／做图）的正反例。studio.test.mjs 逐句检查；
// 也可以直接跑 `node deploy/social/draw-intent-cases.mjs [studio.mjs 路径]` 印出结果表。
// 句子是自己写的口语（繁简混写），不是群友原话。attachments：这则带了几张参考图；
// editing：draw_request 带了 source_job，而且是这个频道画过的作品。
import console from 'node:console';
import { resolve } from 'node:path';
import process from 'node:process';
import { URL, pathToFileURL } from 'node:url';

export const DRAW_INTENT_CASES = [
	// ---- 要画 ----
	{ text: '<@1> 可以幫我畫一隻小鯨魚嗎', want: true },
	{ text: '帮我画一只猫', want: true },
	{ text: '畫貓', want: true },
	{ text: '再來一張', want: true },
	{ text: '幫我做一張頭像', want: true },
	{ text: '帮我做个头像', want: true },
	{ text: '能不能画个可爱的小狗给我', want: true },
	{ text: '小鯨魚 畫一張夕陽海邊的圖給我', want: true },
	{ text: '我想要一張貓咪的桌布', want: true },
	{ text: '可以生成一张壁纸吗', want: true },
	{ text: '照這張畫一個Q版的', attachments: 1, want: true },
	{ text: '把这张图变成动漫风格', attachments: 1, want: true },
	{ text: '參考這張幫我畫我家的貓', attachments: 1, want: true },
	{ text: '照這張', attachments: 1, want: true },
	{ text: '不要畫貓了，改畫狗', want: true },
	{ text: 'draw me a cat', want: true },
	{ text: 'can you make me an avatar?', want: true },
	{ text: '眼睛改大一點', editing: true, want: true },
	{ text: '背景换成蓝色', editing: true, want: true },
	{ text: '麻煩畫個生日卡片給我妹', want: true },
	{ text: '来一张小鲸鱼的头像', want: true },
	{ text: '拜託幫我畫畫看我的OC', want: true },
	{ text: '欸 你畫一下我', want: true },
	{ text: '可不可以幫我畫一張圖，不要太複雜', want: true },
	{ text: '能幫我P一張圖嗎', want: true },
	{ text: '把我的貓畫成水彩風', want: true },
	// ---- 不是要画 ----
	{ text: '請問你是怎麼畫畫的', want: false },
	{ text: '先不要畫', want: false },
	{ text: '描述一下這張圖', attachments: 1, want: false },
	{ text: '你最喜歡什麼顏色', want: false },
	{ text: '<@1> 你畫畫的時候還能聊天嗎', want: false },
	{ text: '這張畫得不錯', want: false },
	{ text: '幫我看看這張圖', want: false },
	{ text: '别画了', want: false },
	{ text: '不用畫了，謝謝', want: false },
	{ text: '你是用什么软件画的', want: false },
	{ text: '你会画画吗', want: false },
	{ text: '這張圖是什麼意思', attachments: 1, want: false },
	{ text: '幫我看這張圖裡寫了什麼', attachments: 1, want: false },
	{ text: '你畫的貓好可愛', want: false },
	{ text: '畫好了嗎', want: false },
	{ text: '我想學畫畫', want: false },
	{ text: '他畫風好特別', want: false },
	{ text: '你做的頭像我很喜歡', want: false },
	{ text: '晚餐吃什麼好', want: false },
	{ text: '這個顏色好看嗎', attachments: 1, want: false },
	{ text: '眼睛改大一點', want: false },
	{ text: '剛剛那張畫室畫完了嗎', want: false },
	{ text: '算了不要画了', want: false },
	{ text: 'how do you draw so fast', want: false },
	{ text: "don't draw anything", want: false },
	{ text: '先别画，等我想好', want: false },
	{ text: '這張照片是在哪裡拍的', attachments: 1, want: false },
	{ text: '你能告訴我這張圖畫的是什麼嗎', attachments: 1, want: false },
	{ text: '好可爱，再说一次', want: false },
	{ text: '幫我做一張表格', want: false },
	{ text: '你畫一張要多久', want: false },
];

// 照 drawIntent 的参数跑一句（editing 换成旧版的 sourceJob 写法也通，旧版拿来比较修前修后）。
export function judgeCase(drawIntent, row) {
	return drawIntent(row.text, {
		attachments: row.attachments ?? 0,
		editing: Boolean(row.editing),
		sourceJob: row.editing ? 'work-' + '0'.repeat(24) : undefined,
	});
}

if (
	process.argv[1] &&
	import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
	const target = process.argv[2]
		? pathToFileURL(resolve(process.argv[2])).href
		: new URL('./studio.mjs', import.meta.url).href;
	const { drawIntent } = await import(target);
	let wrong = 0;
	console.log('| # | 句子 | 附件 | 改作品 | 应为 | 结果 |');
	console.log('| - | - | - | - | - | - |');
	DRAW_INTENT_CASES.forEach((row, index) => {
		const got = judgeCase(drawIntent, row);
		if (got !== row.want) wrong++;
		console.log(
			`| ${index + 1} | ${row.text.replace(/\|/g, '/')} | ${row.attachments ?? 0} | ${row.editing ? '是' : '否'} | ${row.want ? '画' : '不画'} | ${got === row.want ? '✔' : '✖ 误判'} |`,
		);
	});
	console.log(`
共 ${DRAW_INTENT_CASES.length} 句，误判 ${wrong} 句`);
}
