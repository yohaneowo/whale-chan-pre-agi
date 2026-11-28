// 翻旧事的规则（照 dsh-memory-keeper 的运行规则，第 1 步）：
//   1. 有线索才翻：打招呼、闲聊、只 @ 不翻；提到具体的人、时间、地点、东西、话题、情绪，或明确在问以前的事才翻。
//   2. 能翻哪里（先按范围过滤，再搜寻）：服务器、频道、讨论串、私讯各自分开。
//      群聊只翻这个频道；同服务器的公开频道要有人明确问到（点名那个频道，或明确在问以前的事）才翻，而且要说出来源。
//      私讯可以想起公开频道的事；私讯、不是所有人都看得到的频道（含私人讨论串）不拿到别的地方讲。
//   3. 翻多久以前的：平常 6 个月内，有提到日期才查更早。
// 近期旁听、自动想起、工具、模型最后拿到的内容（闸道附上的记忆）都用这里同一套规则。

const DAY = 86_400_000;
// 平常翻得到的范围（keeper 的预设）。
export const RECALL_WINDOW_MS = 183 * DAY;
const VIEW_CHANNEL = 1n << 10n;
const ADMINISTRATOR = 1n << 3n;
// 公开的讨论串（公告串、公开讨论串）跟着上层频道；私人讨论串只有被拉进去的人看得到。
const PUBLIC_THREADS = new Set([10, 11]);
const PRIVATE_THREAD = 12;

// 明确在问以前的事（繁简都列；「之前」「那天」这类也算，宁可让她查一下再说没找到）。
const ASKING =
	/上次|上回|之前|先前|以前|那天|那时|那時|那次|那阵子|那陣子|当时|當時|还记得|還記得|记不记得|記不記得|你记得|你記得|后来|後來|到了(?:吗|嗎|没|沒)|怎么样了|怎麼樣了|怎样了|怎樣了|说过|說過|讲过|講過|提过|提過|问过|問過|聊过|聊過|昨天|前天|上周|上週|上礼拜|上禮拜|上星期|上个月|上個月|去年|前年|前几天|前幾天|几天前|幾天前|周前|週前|个月前|個月前|年前/u;
// 问时间的说法（几点、几号、什么时候）与说到之后的安排（下个月、下周）：时间是线索。「明天」「今天」太常见，不算。
const WHEN =
	/几号|幾號|几点|幾點|什么时候|什麼時候|哪天|哪一天|何时|何時|下个月|下個月|下周|下週|下礼拜|下禮拜|下星期|明年/u;
// 问「谁」：在问人。
const WHO = /谁|誰|哪位/u;
// 情绪词：心情是线索（keeper：人、时间、地点、物品、话题、情绪词）。「笑死」「哈哈」这种口头禅不算。
const FEELING =
	/难过|難過|伤心|傷心|好累|很累|累死|好烦|好煩|烦死|煩死|生气|生氣|委屈|想哭|哭了|焦虑|焦慮|压力|壓力|失眠|害怕|孤单|孤單|寂寞|崩溃|崩潰|心情不好|不开心|不開心|好开心|好開心|超开心|超開心|兴奋|興奮|紧张|緊張|担心|擔心/u;
const NUMBER = {
	一: 1,
	二: 2,
	两: 2,
	兩: 2,
	三: 3,
	四: 4,
	五: 5,
	六: 6,
	七: 7,
	八: 8,
	九: 9,
	十: 10,
	半: 0.5,
};
const count = (value) => (/^\d+$/.test(value) ? Number(value) : NUMBER[value]);

// 某地时区里某天 00:00 的时间（毫秒）。
function dayStart(year, month, day, timeZone) {
	const guess = Date.UTC(year, month - 1, day);
	const parts = Object.fromEntries(
		new Intl.DateTimeFormat('en-US', {
			timeZone,
			year: 'numeric',
			month: '2-digit',
			day: '2-digit',
			hour: '2-digit',
			minute: '2-digit',
			hourCycle: 'h23',
		})
			.formatToParts(new Date(guess))
			.map(({ type, value }) => [type, value]),
	);
	const shown = Date.UTC(
		Number(parts.year),
		Number(parts.month) - 1,
		Number(parts.day),
		Number(parts.hour),
		Number(parts.minute),
	);
	return guess - (shown - guess);
}

function today(now, timeZone) {
	const parts = Object.fromEntries(
		new Intl.DateTimeFormat('en-US', {
			timeZone,
			year: 'numeric',
			month: '2-digit',
			day: '2-digit',
		})
			.formatToParts(new Date(now))
			.map(({ type, value }) => [type, value]),
	);
	return {
		year: Number(parts.year),
		month: Number(parts.month),
		day: Number(parts.day),
	};
}

// 话里提到的日期 → 要翻的时间范围（{ since, until }）；没提到日期回 undefined。
// 认得：2026-05-01、2026/5/1、2026年5月1日、2026年5月、5月1日（5月1號）、5/1、去年、前年、N 个月前、N 年前。
// 只说月日的，取最近一次已经过去的那天；一天的前后各放宽两天，一个月就整个月。
export function datedRange(
	text,
	{ now = Date.now(), timeZone = 'Asia/Taipei' } = {},
) {
	const value = String(text ?? '');
	const {
		year: thisYear,
		month: thisMonth,
		day: thisDay,
	} = today(now, timeZone);
	const around = (year, month, day) => ({
		since: dayStart(year, month, day, timeZone) - 2 * DAY,
		until: dayStart(year, month, day, timeZone) + 3 * DAY - 1,
	});
	const wholeMonth = (year, month) => ({
		since: dayStart(year, month, 1, timeZone),
		until:
			(month === 12
				? dayStart(year + 1, 1, 1, timeZone)
				: dayStart(year, month + 1, 1, timeZone)) - 1,
	});
	const valid = (month, day) =>
		month >= 1 && month <= 12 && (day === undefined || (day >= 1 && day <= 31));
	let match =
		/(20\d{2})\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2})\s*[日号號]?)?/u.exec(
			value,
		);
	if (match) {
		const [year, month, day] = [
			Number(match[1]),
			Number(match[2]),
			match[3] && Number(match[3]),
		];
		if (valid(month, day || undefined))
			return day ? around(year, month, day) : wholeMonth(year, month);
	}
	const lastYear = (month, day) =>
		month > thisMonth || (month === thisMonth && (day ?? 1) > thisDay)
			? thisYear - 1
			: thisYear;
	match =
		/(?<![\d/])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]?/u.exec(value) ??
		/(?<![\d/.])(\d{1,2})\/(\d{1,2})(?![\d/])/u.exec(value);
	if (match) {
		const [month, day] = [Number(match[1]), Number(match[2])];
		if (valid(month, day)) {
			const year = /去年/u.test(value) ? thisYear - 1 : lastYear(month, day);
			return around(year, month, day);
		}
	}
	match = /(?<![\d个個])(\d{1,2})\s*月(?![前份]?\s*[前])/u.exec(value);
	if (match && valid(Number(match[1]))) {
		const month = Number(match[1]);
		const year = /去年/u.test(value) ? thisYear - 1 : lastYear(month, 1);
		return wholeMonth(year, month);
	}
	match = /(\d+|[一二两兩三四五六七八九十半])\s*(?:个|個)?\s*月前/u.exec(value);
	if (match && count(match[1])) {
		const months = count(match[1]);
		return {
			since: now - (months + 1) * 31 * DAY,
			until: now - Math.max(0, months - 1) * 28 * DAY,
		};
	}
	match = /(\d+|[一二两兩三四五六七八九十半])\s*年前/u.exec(value);
	if (match && count(match[1])) {
		const years = count(match[1]);
		return {
			since: now - (years + 0.5) * 366 * DAY,
			until: now - Math.max(0, years - 0.5) * 365 * DAY,
		};
	}
	if (/前年/u.test(value))
		return {
			since: dayStart(thisYear - 2, 1, 1, timeZone),
			until: dayStart(thisYear - 1, 1, 1, timeZone) - 1,
		};
	if (/去年/u.test(value))
		return {
			since: dayStart(thisYear - 1, 1, 1, timeZone),
			until: dayStart(thisYear, 1, 1, timeZone) - 1,
		};
	return undefined;
}

// 这一轮要不要翻旧事、可以翻哪里、翻多久以前的。
//   text：叫她的那则原文；names：最近说过话的人的名字（不含说话的人自己）；
//   humans：最近说过话的人的 id（@ 到这些人才算提到人；@她自己、没给就不算）；
//   channels：同服务器已知的频道（id → 名字）。
// recall=false 时，长期记忆、人物卡、旧讯息、翻纪录的工具一律不用（话题线索由呼叫端再用范围内的搜寻补判，见 withTopic）。
export function recallPlan(
	text,
	{
		now = Date.now(),
		timeZone = 'Asia/Taipei',
		names = [],
		humans = new Set(),
		channels = new Map(),
	} = {},
) {
	const value = String(text ?? '');
	const people = [...value.matchAll(/<@!?(\d{5,25})>/gu)]
		.map((match) => match[1])
		.filter((id) => humans.has(id));
	const named = names.filter(
		(name) =>
			typeof name === 'string' &&
			name.trim().length >= 2 &&
			value.includes(name.trim()),
	);
	const mentioned = new Set(
		[...value.matchAll(/<#(\d{5,25})>/gu)].map((match) => match[1]),
	);
	for (const [id, name] of channels)
		if (typeof name === 'string' && name.length >= 2 && value.includes(name))
			mentioned.add(id);
	const range = datedRange(value, { now, timeZone });
	const explicit = ASKING.test(value) || Boolean(range);
	const clues = [
		...(explicit ? ['asking'] : []),
		...(range ? ['date'] : []),
		...(WHEN.test(value) ? ['time'] : []),
		...(FEELING.test(value) ? ['feeling'] : []),
		...(people.length || named.length || WHO.test(value) ? ['person'] : []),
		...(mentioned.size ? ['channel'] : []),
	];
	return {
		recall: clues.length > 0,
		explicit,
		clues,
		channels: mentioned,
		dated: Boolean(range),
		since: range?.since ?? now - RECALL_WINDOW_MS,
		until: range?.until ?? now,
	};
}

// 范围内的搜寻有强的命中（具体的东西、话题撞上了）也算线索。
export function withTopic(plan, hits) {
	return !plan.recall && hits
		? { ...plan, recall: true, clues: ['topic'] }
		: plan;
}

export const inWindow = (at, plan) =>
	Number.isFinite(at) && at >= plan.since && at <= plan.until;

// 一则讯息（原文帐本的一行，或带频道的记忆）在这一轮能不能用、怎么标来源：
//   where：这一轮在哪里说话 { guildId, channelId, dm }；
//   回传 'here'（同一个地方）、'elsewhere'（别的公开频道，要说是哪里听来的），或 null（不能用）。
export function placeOf(row, where, plan, isPublic) {
	if (!row || !where?.channelId) return null;
	const channel = String(row.channel_id ?? row.channelId ?? '');
	const guild = row.guild_id ?? row.guildId ?? null;
	if (!channel) return null;
	const direct = row.scope ? row.scope === 'dm' : !guild;
	if (direct) return where.dm && channel === where.channelId ? 'here' : null;
	if (
		!where.dm &&
		channel === where.channelId &&
		guild === (where.guildId ?? null)
	)
		return 'here';
	if (!isPublic(channel)) return null;
	if (where.dm) return 'elsewhere';
	if (guild !== (where.guildId ?? null)) return null;
	// 别的公开频道：有人点名那个频道，或明确在问以前的事，才拿出来。
	return plan.channels.has(channel) || plan.explicit ? 'elsewhere' : null;
}

// 频道看不看得到：@everyone 有没有「检视频道」。快取 10 分钟；还不知道的一律当成不公开（宁可不提）。
export class ChannelVisibility {
	constructor(fetch, { ttl = 600_000, now = Date.now } = {}) {
		this.fetch = fetch;
		this.ttl = ttl;
		this.now = now;
		this.channels = new Map();
		this.guilds = new Map();
		this.loading = new Map();
	}

	isPublic(channelId) {
		const row = this.channels.get(String(channelId));
		return Boolean(row?.public && this.now() - row.at < this.ttl * 6);
	}

	name(channelId) {
		return this.channels.get(String(channelId))?.name;
	}

	// 同服务器已知的频道（id → 名字），给 recallPlan 认「点名那个频道」。
	names(guildId) {
		const out = new Map();
		for (const [id, row] of this.channels)
			if (row.guildId === guildId && row.name) out.set(id, row.name);
		return out;
	}

	static everyone(guild, channel) {
		const role = (guild?.roles ?? []).find((row) => row?.id === guild.id);
		let allowed = BigInt(role?.permissions ?? 0);
		if (allowed & ADMINISTRATOR) return true;
		const own = (channel?.permission_overwrites ?? []).find(
			(row) => row?.id === guild.id,
		);
		if (own) {
			allowed &= ~BigInt(own.deny ?? 0);
			allowed |= BigInt(own.allow ?? 0);
		}
		return (allowed & VIEW_CHANNEL) !== 0n;
	}

	// 载入一个服务器所有频道的可见性（失败就照旧，下次再试）。
	warm(guildId) {
		if (!/^\d{5,25}$/.test(String(guildId ?? ''))) return Promise.resolve();
		const fresh = this.guilds.get(guildId);
		if (fresh && this.now() - fresh < this.ttl) return Promise.resolve();
		if (this.loading.has(guildId)) return this.loading.get(guildId);
		const task = (async () => {
			const [guild, channels] = await Promise.all([
				this.fetch(`/guilds/${guildId}`),
				this.fetch(`/guilds/${guildId}/channels`),
			]);
			if (!guild?.id || !Array.isArray(channels)) return;
			const at = this.now();
			for (const channel of channels)
				if (channel?.id)
					this.channels.set(String(channel.id), {
						guildId,
						name: channel.name,
						public: ChannelVisibility.everyone(guild, channel),
						at,
					});
			this.guilds.set(guildId, at);
		})()
			.catch(() => {})
			.finally(() => this.loading.delete(guildId));
		this.loading.set(guildId, task);
		return task;
	}

	// 讨论串不在频道清单里：另外问一次（公开讨论串跟着上层频道，私人讨论串不公开）。
	async warmChannel(channelId) {
		const id = String(channelId ?? '');
		if (!/^\d{5,25}$/.test(id) || this.channels.has(id)) return;
		try {
			const channel = await this.fetch(`/channels/${id}`);
			if (!channel?.id) return;
			if (channel.guild_id) await this.warm(channel.guild_id);
			const parent = this.channels.get(String(channel.parent_id ?? ''));
			const thread =
				PUBLIC_THREADS.has(channel.type) || channel.type === PRIVATE_THREAD;
			this.channels.set(id, {
				guildId: channel.guild_id,
				name: channel.name,
				public: thread
					? channel.type !== PRIVATE_THREAD && Boolean(parent?.public)
					: Boolean(this.channels.get(id)?.public),
				at: this.now(),
			});
		} catch {
			/* 问不到就当成不公开。 */
		}
	}
}

// 这一轮想起的东西带进提示时附的话：找到了怎么用、明确在问却没找到怎么说。
export const FOUND_NOTE =
	'上面想起的都是以前的事：跟現在聊的有關才提，沒關係就放著（記得不等於要說）；要提就照原話和日期說，別的頻道聽來的要講是在哪裡聽到的。以前記的跟對方現在說的對不上，以現在說的為準。';
export const NOT_FOUND_NOTE =
	'有人在問以前的事，可是你翻過了，這裡能翻的範圍裡沒找到可靠的紀錄：就老實說沒找到或不記得了，可以問一句是哪天、在哪裡說的；不要編，也不要拿別的事來湊。';
export const NO_CLUE_TOOL =
	'這句話沒有要翻舊事的線索（沒提到具體的人、時間、地點、東西或話題，也不是在問以前的事）：先回眼前這句，不用翻紀錄。';

// 她的行为规则（keeper 运行规则里适合当规矩的几条），用她的口吻写。群聊与私讯都带，画室不带。
export const MEMORY_MANNERS = `## 想起以前的事

- 記得不等於要說：想起什麼，先看跟現在聊的有沒有關係；沒關係就放著，拿不準就別提。
- 打招呼、閒聊、只叫你一聲，就回眼前這句，不要翻舊帳；對方提到具體的人、時間、地方、東西或話題，或明著問以前的事，才去想。
- 想起來了就照原話和日期講（像「11/24 你在這說過⋯」）；別的頻道聽來的，要說是在哪個頻道聽到的。
- 翻不到就直說「我沒找到欸」「這個我不記得了」，可以問一句是哪天、在哪裡說的；不要編，也不要拿別的事來湊。
- 以前寫的日記、筆記是當時的你寫下的故事，讀到了不等於你一直記得，不要裝成很熟的樣子。
- 以前記的跟對方現在說的對不上，以現在說的為準；嘴硬可以，事實照現在的。
- 記人記的是具體的那一刻（「你上次半夜三點還在趕報告」），不是貼標籤（「你很拚」）。
- 私訊裡聊的、只有部分人看得到的頻道裡聊的，不拿到別的地方講。`;
