// 记忆召回离线基线的执行器：用「真正的」ArchiveStore／MemoryStore／NotesStore／chatTools／SocialRuntime，
// 在临时目录里建一个全合成的世界，然后量「她这一轮看得到什么」。不呼叫任何模型、不连网络、不碰 VM。
//
// 固定条件（同样的输入跑两次要得到同样的结果）：
// - 时钟：Date.now() 固定在 NOW（只在执行期间覆盖，结束就还原）；
// - 外部依赖：鱼记忆引擎与 OpenCC 繁转简都用 sha256 钉死，对不上就拒绝执行；
// - 每个视角（auto／tool）各建一个全新的临时世界，召回刷新（会写回档案）不会影响下一题；
// - 帐本、记忆的 id 全部由案例里的符号名称决定（m1、r1…），不用随机数；
// - 延迟不进入结果的 hash（它每次都不一样），其余全部进 hash。
import { createHash } from 'node:crypto';
import {
	cpSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { performance } from 'node:perf_hooks';
import { pathToFileURL } from 'node:url';

export const NOW = Date.parse('2026-11-08T12:00:00.000Z');
export const PINNED = {
	fishEntry: '0fe490abf945ce00ac3156d4f1ff0caa2b1ade02337c31306d1381454b439444',
	opencc: 'b78e83f940e73a022d8d996333e924edf07aa5b6133b8cddd46fbbff1ac339b6',
};
export const GROUP_ROOT = '/srv/dsh/discord';
const sha = (data) => createHash('sha256').update(data).digest('hex');
const SOCIAL_FILES = [
	'archive.mjs',
	'chat-tools.mjs',
	'compaction.mjs',
	'index.mjs',
	'memory.mjs',
	'notes.mjs',
];

// ---------- 外部依赖与被测程式的指纹 ----------
export async function loadDeps({ socialDir, fishDir, openccDir }) {
	const fishEntry = join(fishDir, 'lib', 'index.js');
	if (sha(readFileSync(fishEntry)) !== PINNED.fishEntry)
		throw new Error(`鱼记忆引擎入口的 sha256 不是固定的 1.3.7：${fishEntry}`);
	const t2cn = join(openccDir, 'dist', 'esm', 't2cn.js');
	const opencc = readFileSync(t2cn);
	if (sha(opencc) !== PINNED.opencc)
		throw new Error(`OpenCC t2cn.js 的 sha256 不是固定的 1.4.2：${t2cn}`);
	const staging = mkdtempSync(join(tmpdir(), 'recall-opencc-'));
	writeFileSync(join(staging, 't2cn.mjs'), opencc);
	const { Converter } = await import(pathToFileURL(join(staging, 't2cn.mjs')).href);
	const toSimplified = Converter({ from: 'tw', to: 'cn' });
	rmSync(staging, { recursive: true, force: true });
	const engine = await import(pathToFileURL(fishEntry).href);
	const url = (name) => pathToFileURL(join(socialDir, name)).href;
	const [archive, memory, notes, chatTools, index] = await Promise.all([
		import(url('archive.mjs')),
		import(url('memory.mjs')),
		import(url('notes.mjs')),
		import(url('chat-tools.mjs')),
		import(url('index.mjs')),
	]);
	return {
		engine,
		simplify: (text) => toSimplified(String(text)),
		ArchiveStore: archive.ArchiveStore,
		createBackup: archive.createBackup,
		MemoryStore: memory.MemoryStore,
		NotesStore: notes.NotesStore,
		chatTools: chatTools.chatTools,
		SocialRuntime: index.SocialRuntime,
		socialDir,
	};
}

export function fingerprint(socialDir, node = process.version) {
	const files = {};
	for (const name of SOCIAL_FILES)
		files[name] = sha(readFileSync(join(socialDir, name)));
	return { node, ...PINNED, files };
}

// ---------- 小工具 ----------
const AGO = /^(\d+)(d|h|m)$/;
export const ago = (spec) => {
	const match = AGO.exec(spec);
	if (!match) throw new Error('时间写法不对：' + spec);
	return NOW - Number(match[1]) * { d: 86400000, h: 3600000, m: 60000 }[match[2]];
};
const iso = (ms) => new Date(ms).toISOString();
const uuid = (n) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`;

// 保守的上限估算：一个汉字算 1 token，其余每 3.5 个字元算 1 token（DeepSeek 实际通常更少）。
export function estimateTokens(text) {
	const han = (String(text).match(/\p{Script=Han}/gu) ?? []).length;
	const rest = String(text).length - han;
	return Math.ceil(han + rest / 3.5);
}

// 背景闲聊：话题刻意避开案例用到的主题（猫、羽球、搬家、聚餐、生日、手术……）。
export const FILLER = [
	'今天天氣真好，風有點涼',
	'昨天那場球賽有人看嗎',
	'這首歌的前奏好好聽',
	'我剛下班，路上塞車塞到不行',
	'有人推薦好用的耳機嗎',
	'週末想去看電影，有什麼好片',
	'新出的那款遊戲更新了好多東西',
	'早餐我都吃蛋餅配豆漿',
	'最近咖啡漲價了欸',
	'我的電腦風扇好吵，要不要拿去清一下',
	'下雨天最適合在家睡覺',
	'這個版本的角色強度好像被調整了',
	'有人知道這個要怎麼設定嗎',
	'哈哈這個梗太好笑了',
	'我昨天熬夜追劇，現在好睏',
	'捷運今天好擠',
	'誰要一起組隊打排位',
	'剛看到一個很可愛的貼圖',
	'我手搖飲都點微糖少冰',
	'好想放假喔',
	'這週的新番有人追嗎',
	'晚點我再來看看這個',
	'剛剛地震你們有感覺嗎',
	'台北的天氣一直在變',
	'我的網路突然變超慢',
	'有沒有人會修這種小問題',
	'這家便當店的雞腿真的很大',
	'我最近都用平板看影片',
	'等一下要開會，先不聊了',
	'今天的太陽好大',
	'這個頻道好安靜喔',
	'晚上有人要語音嗎',
	'我昨天走了兩萬步，腿好痠',
	'這個遊戲的音樂超好聽',
	'剛換了新的鍵盤，手感不錯',
	'有人收到通知了嗎',
	'午餐隨便吃個麵就好',
	'週一症候群又來了',
	'欸你們有看到那張圖嗎',
	'這個更新之後畫面變得好清楚',
];

// 背景闲聊的产生器：前 40 则用上面的手写句，之后用固定种子的伪随机数从「词池」抽词造句，词频是 Zipf 分布
// （少数词很常见、多数词很少见，和真的聊天一样），所以「这个双字片段在帐本里出现过几次」有真实的分布。
// 词池刻意不含案例用到的主题字词；harness.test.mjs 会检查这一点。
const POOL =
	'今天 昨天 明天 現在 剛剛 等等 晚上 早上 中午 週末 最近 一直 已經 還是 可能 應該 真的 好像 其實 如果 因為 所以 但是 然後 而且 或者 比較 非常 有點 一些 一下 大家 我們 你們 他們 自己 朋友 同學 老闆 同事 時間 地方 東西 事情 問題 方法 辦法 感覺 覺得 知道 看到 聽到 想到 說過 回來 過去 出去 起來 下來 開始 結束 繼續 準備 需要 可以 不要 不會 沒有 什麼 怎麼 為什麼 哪裡 誰要 多少 很多 一點 全部 通知 訊息 照片 影片 音樂 歌曲 電影 劇情 角色 版本 更新 設定 畫面 聲音 網路 電腦 鍵盤 滑鼠 耳機 螢幕 速度 價格 便宜 好吃 好玩 好看 好聽 好累 好睏 好笑 可愛 厲害 奇怪 麻煩 簡單 安全 天氣 下雨 太陽 溫度 捷運 公車 路上 塞車 咖啡 飲料 便當 麵包 早餐 午餐 宵夜 排隊 老店 新店 比賽 隊伍 排位 遊戲 地圖 裝備 任務 等級 抽卡 劇集 漫畫 小說 作業 報告 會議 考卷 下班 上班 放假 加班 睡覺 起床 洗澡 散步 跑步 運氣'.split(' ');
const PARTICLES = ['啊', '喔', '吧', '欸', '啦', '耶', '嗎', '呢', '的', '了', '在', '也', '就', '都', '還', '又'];
function mulberry32(seed) {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}
function zipf(random) {
	const weights = POOL.map((_, rank) => 1 / (rank + 1));
	const total = weights.reduce((sum, weight) => sum + weight, 0);
	return () => {
		let target = random() * total;
		for (let i = 0; i < POOL.length; i++) {
			target -= weights[i];
			if (target <= 0) return POOL[i];
		}
		return POOL[0];
	};
}
// 聊天长度分布照 2026-11-08 正式帐本（只读数了字数，没看内容）：中位数 9 字、p90 36 字。
function sentence(random, pick) {
	const words = random() < 0.55 ? 1 + Math.floor(random() * 2) : 2 + Math.floor(random() * 5);
	const parts = [];
	for (let i = 0; i < words; i++) {
		parts.push(pick());
		if (random() < 0.3) parts.push(PARTICLES[Math.floor(random() * PARTICLES.length)]);
	}
	return parts.join('');
}
export function makeFiller(count) {
	const lines = FILLER.slice(0, Math.min(count, FILLER.length));
	const random = mulberry32(20261108);
	const pick = zipf(random);
	while (lines.length < count) lines.push(sentence(random, pick));
	return lines;
}
// 背景记忆记录：和案例无关的人（阿丁……）说过的事，数量照正式环境 group 范围的 305 条。
const FILLER_PEOPLE = ['阿丁', '阿己', '阿庚', '阿辛', '阿壬', '阿癸', '小華', '小美', '小傑', '小強', '小玲', '小偉'];
// 背景记忆分两种，照正式环境的结构：
// - 事件（kind: event）：第三人称的整理笔记，几乎只有内容词（没有「你们／大家／什么」这类功能词），所以和随便一句话撞字的机会小；
// - 事实（kind: fact，鱼记忆的 stable_profile）：引擎保留「常驻名额」（factsMax=2），所以真实环境每一轮都会带 2 条。
//   2026-11-08 只读量了 120 个真实提问：每一轮都回 2～4 条记忆（平均 2.1 条、约 57 字），从没有 0 条也没有 6 条，
//   就是这个结构；全用功能词造的合成记忆会回 5～6 条无关事件，那是 harness 的假象。
// 内容词：名词、形容词、活动（照片、遊戲、比賽……）。拿掉功能词与泛称（什麼、沒有、可以、問題、事情、東西、知道……）：
// 整理出来的笔记很少整句都是这些词，让它们进笔记会让每个提问都撞上一堆（这正是合成记忆的假象来源）。
const FUNCTIONAL = new Set('什麼 怎麼 為什麼 哪裡 誰要 多少 很多 一點 全部 沒有 可以 不要 不會 需要 準備 開始 結束 繼續 回來 過去 出去 起來 下來 說過 想到 聽到 看到 知道 覺得 感覺 方法 辦法 問題 事情 東西 地方 時間 一些 一下 一直 已經 還是 可能 應該 真的 好像 其實 如果 因為 所以 但是 然後 而且 或者 比較 非常 有點 大家 我們 你們 他們 自己'.split(' '));
const CONTENT_WORDS = POOL.slice(POOL.indexOf('朋友')).filter((word) => !FUNCTIONAL.has(word));
export function makeMemoryFiller(count, style = 'content') {
	const random = mulberry32(20261109);
	const pick = style === 'all' ? zipf(random) : (() => {
		const weights = CONTENT_WORDS.map((_, rank) => 1 / (rank + 1));
		const total = weights.reduce((sum, weight) => sum + weight, 0);
		return () => {
			let target = random() * total;
			for (let i = 0; i < CONTENT_WORDS.length; i++) {
				target -= weights[i];
				if (target <= 0) return CONTENT_WORDS[i];
			}
			return CONTENT_WORDS[0];
		};
	})();
	return Array.from({ length: count }, (_, i) => {
		const who = FILLER_PEOPLE[Math.floor(random() * FILLER_PEOPLE.length)];
		const body = style === 'all'
			? sentence(random, pick) + (random() < 0.5 ? sentence(random, pick) : '')
			: Array.from({ length: 2 + Math.floor(random() * 3) }, pick).join(random() < 0.5 ? '' : '，');
		return { who, text: who + body, kind: i % 25 === 0 ? 'fact' : 'event' };
	});
}
export const POOL_WORDS = POOL;
// 规模：预设照 2026-11-08 的正式环境（帐本 3,638 则、记忆 305 条）；敏感度测试用 setScale 改。
export const DEFAULT_SCALE = { ledger: 3600, memory: 300, facts: 12, memoryStyle: 'content' };
let scale = { ...DEFAULT_SCALE };
export const setScale = (next) => {
	scale = { ...DEFAULT_SCALE, ...next };
};
export const getScale = () => ({ ...scale });

// ---------- 世界 ----------
// spec：users、owner、channels、ledger、memories、forgets、cards、facts、filler。
export async function buildWorld(deps, spec, { root } = {}) {
	const dir = root ?? mkdtempSync(join(tmpdir(), 'recall-world-'));
	const users = spec.users;
	const channels = spec.channels;
	const ownerId = users[spec.owner].id;
	const ids = new Map();
	const names = new Map();
	let counter = 0;
	const idOf = (sym) => {
		if (!ids.has(sym)) {
			ids.set(sym, String(800000000000000000n + BigInt(++counter)));
			names.set(ids.get(sym), sym);
		}
		return ids.get(sym);
	};
	const symOf = (id) => names.get(String(id)) ?? String(id);

	const runtime = new deps.SocialRuntime(
		{ get: () => undefined },
		{ stateRoot: join(dir, 'home', 'social-runtime'), ownerIds: [ownerId], groupCwd: GROUP_ROOT, dailyMemories: 1000, ...deps.runtimeConfig },
		deps.engine,
	);
	runtime.simplify = deps.simplify;
	runtime.archive = new deps.ArchiveStore(join(runtime.root, 'archive'), {
		timeZone: 'Asia/Taipei',
		now: () => NOW,
	});
	for (const [key, channel] of Object.entries(channels)) {
		channel.key = key;
		runtime.guildOwners.set(`/channels/${channel.id}`, {
			value: { name: channel.name },
			at: NOW,
		});
	}
	runtime.channelLabel = async (channelId) => {
		const channel = Object.values(channels).find((row) => row.id === channelId);
		return channel ? { channel: channel.name } : undefined;
	};

	const rows = new Map(); // 符号 → 讯息（给检查用）
	const messageData = (event) => {
		const user = users[event.user];
		const channel = channels[event.ch];
		return {
			id: idOf(event.id),
			channel_id: channel.id,
			guild_id: channel.guild,
			author: {
				id: user.id,
				username: user.name,
				global_name: user.name,
				bot: Boolean(event.bot),
			},
			content: event.text,
			timestamp: iso(event.at),
		};
	};
	const events = [];
	for (const event of spec.ledger ?? []) events.push({ ...event, at: ago(event.ago) });
	if (spec.filler) {
		// 背景闲聊：直接用 archive.append 写（和 record 写出的栏位一样，3,600 则也写得快）；28% 是她自己（bot）发的，
		// 和正式帐本的比例（1057/3638）接近；70% 在第一个频道、其余在第二个。
		const { who, spanDays = 40 } = spec.filler;
		const count = scale.ledger;
		const lines = makeFiller(count);
		const start = NOW - spanDays * 86400000;
		const stop = NOW - 8 * 3600000;
		const humans = ['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', ...who];
		for (let i = 0; i < count; i++) {
			const bot = i % 7 === 2 || i % 7 === 5;
			const user = users[bot ? 'BOT' : humans[i % humans.length]];
			const channel = channels[i % 10 < 7 ? 'c1' : 'c2'];
			const at = Math.round(start + ((stop - start) * i) / Math.max(1, count - 1));
			const sym = `f${i + 1}`;
			rows.set(sym, { id: sym, user: bot ? 'BOT' : humans[i % humans.length], ch: channel.key, bot, text: lines[i], at });
			runtime.archive.append({
				t: iso(at), kind: 'message', id: idOf(sym), channel_id: channel.id, guild_id: channel.guild, scope: 'group',
				author_id: user.id, author: user.name, bot, content: lines[i],
			});
		}
	}
	if (spec.memFill !== false) {
		const items = makeMemoryFiller(scale.memory, scale.memoryStyle);
		const facts = makeMemoryFiller(scale.facts, 'content').map((item) => ({ ...item, kind: 'fact' }));
		[...items.map((item) => ({ ...item, kind: 'event' })), ...facts].forEach((item, i, all) => {
			runtime.memory.append('group', {
				id: uuid(100000 + i), type: 'memory', at: iso(NOW - (1 + (i * 39) / all.length) * 86400000),
				authorId: '9100000000009' + String(i % 10), authorName: item.who, messageId: idOf('fm' + i),
				kind: item.kind, importance: item.kind === 'fact' ? 0.7 : 0.5, tags: [],
			}, item.text);
		});
	}
	events.sort((a, b) => a.at - b.at);
	for (const event of events) {
		runtime.archive.now = () => event.at;
		if (event.del) {
			const channel = channels[rows.get(event.del).ch];
			runtime.archive.record('delete', {
				id: idOf(event.del),
				channel_id: channel.id,
				guild_id: channel.guild,
			});
		} else if (event.edit) {
			const base = rows.get(event.edit);
			runtime.archive.record('edit', {
				...messageData({ ...base, id: event.edit, text: event.text, at: base.at }),
				edited_timestamp: iso(event.at),
				content: event.text,
			});
		} else {
			rows.set(event.id, event);
			runtime.archive.record('message', messageData(event));
		}
	}
	runtime.archive.now = () => NOW;

	// 记忆（鱼记忆的有来源记录）。scope：'group' 或 'owner'（拥有者私讯，等于「私下」）。
	const scopeName = (scope) => (scope === 'owner' ? 'owner-' + ownerId : scope);
	const records = new Map();
	let recordCounter = 0;
	for (const record of spec.memories ?? []) {
		const user = users[record.user];
		const id = uuid(++recordCounter);
		records.set(record.n, id);
		const channel = record.ch ? channels[record.ch] : undefined;
		runtime.memory.append(
			scopeName(record.scope ?? 'group'),
			{
				id,
				type: 'memory',
				at: iso(ago(record.ago)),
				authorId: user.id,
				authorName: user.name,
				messageId: idOf(record.msg),
				...(channel ? { channelId: channel.id } : {}),
				...(channel?.guild ? { guildId: channel.guild } : {}),
				kind: record.kind ?? 'event',
				importance: record.importance ?? 0.5,
				tags: record.tags ?? [],
			},
			record.text,
		);
	}
	for (const forget of spec.forgets ?? []) {
		const target = (spec.memories ?? []).find((row) => row.n === forget.target);
		runtime.memory.forget(scopeName(target.scope ?? 'group'), records.get(forget.target), ago(forget.ago));
	}
	// 人物卡与长期事实（背景整理的产物）。src 写符号名称，这里换成帐本里的讯息 id。
	for (const card of spec.cards ?? []) {
		const user = users[card.user];
		for (const note of card.notes)
			runtime.notes.updateCard(
				user.id,
				user.name,
				[{ text: note.text, importance: note.importance ?? 3, src: (note.src ?? []).map(idOf) }],
				ago(note.ago ?? '5d'),
			);
	}
	for (const fact of spec.facts ?? [])
		runtime.notes.addFact(fact.text, {
			pending: Boolean(fact.pending),
			now: ago(fact.ago ?? '5d'),
			src: (fact.src ?? []).map(idOf),
		});

	const world = {
		dir,
		deps,
		spec,
		runtime,
		users,
		channels,
		ownerId,
		idOf,
		symOf,
		rows,
		records,
		recordSym: (id) => [...records].find(([, value]) => value === id)?.[0] ?? id,
		isBot: (sym) => Boolean(rows.get(sym)?.bot),
		rowText: (sym) => rows.get(sym)?.text,
		cleanup: () => rmSync(dir, { recursive: true, force: true }),
	};
	return world;
}

// ---------- 一轮自动注入的上下文（镜像 index.mjs 的 systemPrompt.text：634–684 行） ----------
// 呼叫的都是真函数：recallAcross、relatedLines、recentSpeakers、groupContext、relatedText、notes.context；
// 只有「把它们接起来」的几行是照抄的。guardMirror() 会检查 index.mjs 里那几行还在。
export function guardMirror(socialDir) {
	const source = readFileSync(join(socialDir, 'index.mjs'), 'utf8');
	// 移植过翻旧事的规则（deploy/dsh/port_memory_rules.py）的 release：镜像的是新的接线（composeRecall）。
	if (source.includes("from './recall-rules.mjs'"))
		return [
			"scope === GROUP_SCOPE || scope.startsWith('owner-')",
			'const recall = this.recallFor(',
			'(record) => this.recordPlace(record, recall)',
			'this.relatedLines(agent, recall.where ? source : undefined)',
			'recall.plan.recall ? this.recentSpeakers(source) : []',
			"where: kind === GROUP_SCOPE ? '群聊' + (from ? ' ' + from : '') : '私讯'",
			'this.recallNote(recall, memories.length > 0 || related.length > 0)',
			// 第 1 轮审查以后：要不要翻问回忆守门员、守门员给的搜寻词一起找、人物卡的名字分范围。
			...(source.includes('awaitRecall(')
				? ['recall.query || query', 'names: this.namesVisible(recall.where)']
				: []),
		].filter((needle) => !source.includes(needle));
	const needles = [
		"scope === GROUP_SCOPE || scope.startsWith('owner-')",
		'this.memoryScopes(scope)',
		'this.memory.recallAcross(',
		'this.relatedLines(agent, source)',
		'this.groupContext(',
		'this.relatedText(related)',
		"where: from === GROUP_SCOPE ? '群聊' : '私讯'",
	];
	return needles.filter((needle) => !source.includes(needle));
}

export function composeAuto(world, view, turn) {
	const { runtime, users, channels } = world;
	const asker = users[turn.user];
	const channel = turn.ch ? channels[turn.ch] : undefined;
	const questionId = world.idOf(turn.id ?? 'q');
	// 这句话本身先写进帐本（桥接是先记帐本才交给会话的），同频道最近 6 小时的发话者算「在场」。
	const guildId = view === 'group' ? channel.guild : undefined;
	if (!turn.noLedger) {
		runtime.archive.record('message', {
			id: questionId,
			channel_id: channel?.id ?? 'dm-' + asker.id,
			guild_id: guildId ?? null,
			author: { id: asker.id, username: asker.name, global_name: asker.name, bot: false },
			content: turn.text,
			timestamp: iso(NOW - 60000),
		});
	}
	runtime.ingress.clear();
	for (const row of world.rows.values())
		if (!row.bot && channel && channels[row.ch] === channel && row.at >= NOW - 6 * 3600000)
			runtime.ingress.set(world.idOf(row.id), {
				channelId: channel.id,
				authorId: users[row.user].id,
			});
	const source = {
		authorId: asker.id,
		authorName: asker.name,
		owner: asker.id === world.ownerId,
		channelId: channel?.id,
		guildId,
		messageId: questionId,
		text: turn.text,
	};
	const agent = {
		id: 'agent-' + view,
		session: { id: 'session-' + view, header: { cwd: view === 'group' ? GROUP_ROOT + '/room' : '/srv/dsh/owner' } },
	};
	if (view === 'dm') runtime.bindings[agent.id] = 'owner-' + world.ownerId;
	const scope = runtime.scopeFor(agent);
	if (typeof runtime.recallFor === 'function') return composeRecall(world, view, { source, agent, scope });

	const started = performance.now();
	let memories = [];
	let error;
	try {
		memories = runtime.memory.recallAcross(
			scope === 'group' || scope.startsWith('owner-') ? runtime.memoryScopes(scope) : [scope],
			source.text,
		);
	} catch (cause) {
		error = String(cause?.message ?? cause);
	}
	const related = runtime.relatedLines(agent, source);
	let notesText = '';
	let prompt;
	if (view === 'group') {
		const people = runtime.recentSpeakers(source);
		try {
			notesText = runtime.notes.context(people);
		} catch {
			notesText = '';
		}
		prompt = runtime.groupContext(source, memories, people, related);
	} else {
		prompt = [
			'这是拥有者的私人会话；记忆与群聊相通（where 标出是群聊还是私讯的事），私讯的事在群里只是知道、不会说出来。',
			JSON.stringify({
				source: { authorId: source.authorId, name: source.authorName, isOwner: source.owner },
				memories: memories.map(({ id, at, authorId, authorName, text, scope: from }) => ({
					id, at, authorId, authorName, where: from === 'group' ? '群聊' : '私讯', text,
				})),
			}),
			runtime.relatedText(related),
		]
			.filter(Boolean)
			.join('\n');
	}
	const ms = performance.now() - started;
	const sym = (id) => world.symOf(id);
	const sources = {
		memory: memories.map((record) => sym(record.messageId)),
		related: related.map((row) => sym(row.id)),
		notes: [...notesText.matchAll(/〔原文 (\d+)〕/g)].map((match) => sym(match[1])),
	};
	const sections = [
		memories.map((record) => record.text).join('\n'),
		notesText,
		runtime.relatedText(related),
	].join('\n');
	return {
		view,
		prompt,
		memories: memories.map((record) => ({
			n: world.recordSym(record.id),
			source: sym(record.messageId),
			scope: record.scope,
			author: record.authorName,
			kind: record.kind,
			text: record.text,
		})),
		related: related.map((row) => ({ source: sym(row.id), scope: row.scope, bot: row.bot, author: row.author, text: row.content })),
		notesText,
		sources,
		all: [...new Set([...sources.memory, ...sources.related, ...sources.notes])].sort(),
		tokens: estimateTokens(sections),
		ms,
		error,
	};
}

// 翻旧事的规则（port_memory_rules.py 移植后的 release）：这一轮的来源要像桥接那样登记（ingress），
// 群聊频道都当成公开的（题目是照「所有群聊共用一份记忆」出的）。
// 第 1 轮审查以后要不要翻由回忆守门员（模型）判断。题库不呼叫模型：用真模型对这些问题录下来的判断
// （gate-verdicts.json，deploy/eval/recall-gate 在 VM 沙盒实测产生）；没录到的（工具题的默认问句）当成「要翻、明确在问」。
const GATE_VERDICTS = (() => {
	try {
		return JSON.parse(
			readFileSync(new globalThis.URL('./gate-verdicts.json', import.meta.url), 'utf8'),
		);
	} catch {
		return {};
	}
})();
function recallTurn(world, view, source, agent) {
	const { runtime } = world;
	source.channelId ??= 'dm-' + source.authorId;
	runtime.ingress.set(source.messageId, source);
	runtime.recallPlans?.clear();
	runtime.recallVerdicts?.set(source.messageId, {
		verdict: GATE_VERDICTS[source.text] ?? {
			recall: true,
			explicit: true,
			queries: [],
		},
	});
	for (const channel of Object.values(world.channels))
		if (channel.guild) {
			runtime.visibility.channels.set(channel.id, { guildId: channel.guild, name: channel.name, public: true, at: Date.now() });
			runtime.visibility.guilds?.set(channel.guild, Date.now());
		}
	agent.session.snapshotEvents = () => [
		{ type: 'user/message', data: { message: { source: { kind: 'user', discordMessageId: source.messageId, discordChannelId: source.channelId } } } },
	];
	if (view === 'dm') runtime.bindings[agent.id] = 'owner-' + world.ownerId;
}

// 新的接线（镜像移植后 index.mjs 的 social-memory 那段；guardMirror 检查那几行还在）。
function composeRecall(world, view, { source, agent, scope }) {
	const { runtime } = world;
	recallTurn(world, view, source, agent);
	const started = performance.now();
	const recall = runtime.recallFor(source);
	let memories = [];
	let error;
	try {
		if (recall.plan.recall)
			memories = runtime.memory.recallAcross(
				scope === 'group' ? ['group'] : [scope, 'group'],
				recall.query || source.text,
				Date.now(),
				(record) => runtime.recordPlace(record, recall),
			);
	} catch (cause) {
		error = String(cause?.message ?? cause);
	}
	const related = runtime.relatedLines(agent, recall.where ? source : undefined);
	let notesText = '';
	let prompt;
	if (view === 'group') {
		const people = recall.plan.recall ? runtime.recentSpeakers(source) : [];
		try {
			notesText = runtime.notes.context(people, {
				admit: (line) => runtime.noteLabel(line, recall),
				...(runtime.namesVisible ? { names: runtime.namesVisible(recall.where) } : {}),
			});
		} catch {
			notesText = '';
		}
		prompt = runtime.groupContext(source, memories, people, related, recall);
	} else
		prompt = [
			JSON.stringify({
				memories: memories.map(({ id, at, authorId, authorName, text, scope: kind, from }) => ({
					id,
					at,
					authorId,
					authorName,
					where: kind === 'group' ? '群聊' + (from ? ' ' + from : '') : '私讯',
					text,
				})),
			}),
			runtime.relatedText(related),
			runtime.recallNote(recall, memories.length > 0 || related.length > 0),
		]
			.filter(Boolean)
			.join('\n');
	const ms = performance.now() - started;
	const sym = (id) => world.symOf(id);
	const sources = {
		memory: memories.map((record) => sym(record.messageId)),
		related: related.map((row) => sym(row.id)),
		notes: [...notesText.matchAll(/〔原文 (\d+)〕/g)].map((match) => sym(match[1])),
	};
	return {
		view,
		prompt,
		plan: recall.plan,
		memories: memories.map((record) => ({
			n: world.recordSym(record.id),
			source: sym(record.messageId),
			scope: record.scope,
			author: record.authorName,
			kind: record.kind,
			text: record.text,
		})),
		related: related.map((row) => ({ source: sym(row.id), scope: row.scope, bot: row.bot, author: row.author, text: row.content })),
		notesText,
		sources,
		all: [...new Set([...sources.memory, ...sources.related, ...sources.notes])].sort(),
		tokens: estimateTokens([memories.map((record) => record.text).join('\n'), notesText, runtime.relatedText(related)].join('\n')),
		ms,
		error,
	};
}

// ---------- 她自己翻原文的工具（chat_search／chat_open），照模型会下的参数脚本化 ----------
// turn：这一轮是谁在哪里说了什么（移植后的 release 要靠它判断能不能翻、能翻哪里）；题目没写就当成站长在问以前的事。
export async function runTool(world, view, calls, turn) {
	const { runtime } = world;
	const tools = new Map(world.deps.chatTools(runtime).map((tool) => [tool.name, tool]));
	const agent = {
		id: 'agent-' + view,
		session: { id: 'session-' + view, header: { cwd: view === 'group' ? GROUP_ROOT + '/room' : '/srv/dsh/owner' } },
	};
	if (typeof runtime.recallFor === 'function') {
		const ask = turn ?? { user: world.spec.owner, ch: view === 'group' ? 'c1' : undefined, text: '之前大家說過的事你還記得嗎' };
		const asker = world.users[ask.user];
		const channel = ask.ch ? world.channels[ask.ch] : undefined;
		recallTurn(
			world,
			view,
			{
				authorId: asker.id,
				authorName: asker.name,
				owner: asker.id === world.ownerId,
				channelId: channel?.id,
				guildId: view === 'group' ? channel?.guild : undefined,
				messageId: world.idOf('tool-question'),
				text: ask.text,
			},
			agent,
		);
	}
	const results = [];
	const started = performance.now();
	for (const call of calls) {
		const args = { ...call.args };
		if (args.message_id) args.message_id = world.idOf(args.message_id);
		if (args.channel_id) args.channel_id = world.channels[args.channel_id].id;
		const out = await tools.get(call.name).execute(args, { agent });
		const text = String(out.message ?? '');
		const ids = [...text.matchAll(/〔(\d+)〕/g)].map((match) => world.symOf(match[1]));
		results.push({ name: call.name, ok: out.ok, text, ids });
	}
	const sources = [...new Set(results.flatMap((result) => result.ids))].sort();
	return {
		view,
		calls: results,
		sources,
		text: results.map((result) => result.text).join('\n'),
		tokens: estimateTokens(results.map((result) => result.text).join('\n')),
		ms: performance.now() - started,
	};
}

// ---------- 备份与还原（回滚案例用真正的 createBackup） ----------
// 和正式环境一样：以 dsh 的家（social-runtime 的上一层）为根，打包 social-runtime 底下的记忆、人物卡与帐本。
export async function backupWorld(world, name = 'snapshot') {
	const home = dirname(world.runtime.root);
	return world.deps.createBackup({
		root: home,
		paths: ['memory', 'notes', 'archive'].map((part) => join(world.runtime.root, part)),
		out: join(world.dir, 'backups'),
		now: NOW,
		name,
	});
}

export async function restoreWorld(world, archivePath) {
	const { execFile } = await import('node:child_process');
	const { promisify } = await import('node:util');
	const home = dirname(world.runtime.root);
	for (const part of ['memory', 'notes', 'archive'])
		rmSync(join(world.runtime.root, part), { recursive: true, force: true });
	await promisify(execFile)('tar', ['-xzf', archivePath, '-C', home]);
}

// ---------- 检查用的小函数 ----------
export const has = (list, sym) => list.includes(sym);
// 取出某段文字之前最近的「标题行」（以冒号结尾的一行）。
export function headerBefore(prompt, snippet) {
	const at = prompt.indexOf(snippet);
	if (at < 0) return undefined;
	const lines = prompt.slice(0, at).split('\n');
	lines.pop();
	for (let i = lines.length - 1; i >= 0; i--) if (/[:：]$/.test(lines[i].trim())) return lines[i];
	return undefined;
}
export function botShare(world, syms) {
	return syms.length ? syms.filter((sym) => world.isBot(sym)).length / syms.length : 0;
}
