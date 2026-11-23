// 直接呼叫优先进入会话；未直接呼叫的群聊交给另一层有限度的插话判断。
const commands =
	/^\/(?:new|sessions|list|use|switch|resume|current|session|stop|cancel|help)(?:\s|$)/i;

export const defaultNames = [
	'鲸鱼娘',
	'鯨魚娘',
	'大肥鱼',
	'大肥魚',
	'小鲸鱼',
	'小鯨魚',
	'小肥鱼',
	'小肥魚',
];

// 群友叫她「魚」：整句就是魚、句首的魚（后面不是鱼缸、鱼肉这类词）、标点或语气词后面的魚。
// 「養魚」「吃魚」「這條魚」这类一般说法或第三人称不算，交给插话判断。
const fishCall =
	/^(?:@|oi|ok|hey|喂|欸|誒)?\s*[魚鱼](?![缸肉片丸竿塘苗排湯汤池類类香翅餌饵干乾生板卵鱗鳞油露])|(?:^|[\s,，、。.!！?？~～…]|[嘛吧啊呢啦喔哦欸呀耶咧捏])[魚鱼]+(?:[\s,，、。.!！?？~～…]|$)/iu;

export function calledAsFish(content) {
	return fishCall.test(String(content ?? '').trim());
}

export function filterDiscordMessage(message, botUserId, names = defaultNames) {
	if (message.author?.bot || message.author?.id === botUserId) return null;
	const content = String(message.content ?? '').trim();
	if (message.guild_id === undefined) return content || null;
	if (commands.test(content)) return content;
	if (!botUserId || !/^\d+$/.test(botUserId)) return null;
	const mentions = [`<@${botUserId}>`, `<@!${botUserId}>`];
	const calledByName = names.some(
		(name) =>
			name && content.toLocaleLowerCase().includes(name.toLocaleLowerCase()),
	);
	const calledFish = calledAsFish(content);
	const repliedToBot = message.referenced_message?.author?.id === botUserId;
	if (
		!mentions.some((mention) => content.includes(mention)) &&
		!calledByName &&
		!calledFish &&
		!repliedToBot
	)
		return null;
	const prompt = mentions
		.reduce((text, mention) => text.replaceAll(mention, ''), content)
		.trim();
	return prompt || '有人在群里提及了你，请简短回应。';
}
