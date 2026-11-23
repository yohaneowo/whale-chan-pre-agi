// 原文帐本：闸道收到的每一则讯息（群聊与私讯、她自己说的、编辑与删除）按天只追加，是记忆的唯一正本；
// 人物卡、日记、事实都只是从这里整理出来的索引。删除只追加一笔「已删除」，不改旧行。
import { execFile } from 'node:child_process';
import {
	appendFileSync,
	existsSync,
	mkdirSync,
	readFileSync,
	readdirSync,
	statSync,
	unlinkSync,
} from 'node:fs';
import { isAbsolute, join, relative } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';

const run = promisify(execFile);

// 太常见、比对不出话题的片段（已转简体）。
const COMMON = new Set(
	// 后面一串是叫她的名字（大肥鱼、鲸鱼娘、小鲸鱼……），叫她不等于在聊什么话题。
	'什么 我们 你们 他们 一下 可以 这个 那个 就是 没有 不是 知道 真的 还是 然后 大家 现在 时候 怎么 为什么 觉得 应该 因为 所以 但是 如果 已经 一个 自己 东西 哈哈 呵呵 好啊 好的 不要 不会 我的 你的 是不 不是 大肥 肥鱼 鲸鱼 鱼娘 小鲸 小肥'.split(
		' ',
	),
);

// 「阿X／小X／老X」里其实是一般词、不是外号的（已转简体）：问到它们不代表在问某个没出现过的人。
const NICK_WORDS = new Set(
	'小心 小时 小時 小孩 小姐 小吃 小事 小说 小說 小学 小學 小朋 小气 小氣 小声 小聲 小声 小型 小组 小白 小区 小子 小偷 小鬼 小弟 小妹 小哥 小姑 小叔 小三 小便 小费 小看 小编 小米 小熊 小猫 小狗 小鸟 小鱼 老师 老師 老板 老闆 老公 老婆 老是 老实 老實 老人 老家 老友 老大 老爸 老妈 老媽 老哥 老弟 老妹 老姐 老外 老鼠 老虎 老化 老旧 老舊 老爷 老爺 老太 老先 老同 阿姨 阿公 阿嬷 阿嬤 阿妈 阿媽 阿爸 阿嫂 阿伯 阿哥 阿弟 阿妹 阿姐 阿里 阿拉 阿宅 阿婆 阿叔 阿娇 阿嬌'.split(
		' ',
	),
);

// 比对用的片段：中文取相邻两字（不用分词也能比），英文取整个单字（3 个字母以上）；
// 网址、@提及、自订表情先拿掉（不然英文字母片段会让任何夹英文的句子都「重叠」）。繁简与大小写先统一。
export function grams(
	text,
	normalize = (value) => String(value).toLowerCase(),
) {
	const clean = normalize(String(text))
		.replace(/https?:\/\/\S+/g, ' ')
		.replace(/<a?:\w+:\d+>|<[@#][!&]?\d+>/g, ' ');
	const out = new Set();
	for (const run of clean.match(/\p{Script=Han}+/gu) ?? [])
		for (let i = 0; i + 1 < run.length; i++) {
			const gram = run.slice(i, i + 2);
			if (!COMMON.has(gram)) out.add(gram);
		}
	for (const word of clean.match(/[a-z][a-z0-9]{2,}/g) ?? []) out.add(word);
	return out;
}

function localDay(at, timeZone) {
	return new Intl.DateTimeFormat('en-CA', {
		timeZone,
		year: 'numeric',
		month: '2-digit',
		day: '2-digit',
	}).format(new Date(at));
}

export class ArchiveStore {
	constructor(
		root,
		{ timeZone = 'Asia/Taipei', redact = (text) => text, now = Date.now } = {},
	) {
		this.root = root;
		this.timeZone = timeZone;
		this.redact = redact;
		this.now = now;
		// 逐档解析快取（档案没变＝大小与修改时间一样，就不重新解析 JSON）与出处索引快取。
		this.parsed = new Map();
		this.indexCache = null;
		mkdirSync(root, { recursive: true, mode: 0o700 });
	}

	// 一个日档的所有行（已解析）；写到一半的行略过。回传的是快取里的物件，呼叫端不要改它。
	fileRows(name) {
		const path = join(this.root, name);
		const info = statSync(path);
		const key = `${info.size}:${info.mtimeMs}`;
		const cached = this.parsed.get(name);
		if (cached?.key === key) return cached.rows;
		const rows = [];
		for (const line of readFileSync(path, 'utf8').split('\n')) {
			if (!line) continue;
			try {
				rows.push(JSON.parse(line));
			} catch {
				/* 写到一半的最后一行。 */
			}
		}
		this.parsed.set(name, { key, rows });
		return rows;
	}

	append(row) {
		appendFileSync(
			join(this.root, localDay(Date.parse(row.t), this.timeZone) + '.jsonl'),
			JSON.stringify(row) + '\n',
			{ mode: 0o600 },
		);
	}

	// 读帐本（可限定日期），套用编辑与删除，回传每则讯息的最新样子，按时间排序；删掉的不回。
	load({ since, until } = {}) {
		let files;
		try {
			files = readdirSync(this.root).filter((name) =>
				/^\d{4}-\d{2}-\d{2}\.jsonl$/.test(name),
			);
		} catch {
			return [];
		}
		const from = since ? localDay(since, this.timeZone) : '';
		const to = until ? localDay(until, this.timeZone) : '9999';
		const messages = new Map();
		const deleted = new Set();
		for (const name of files.sort()) {
			const day = name.slice(0, 10);
			if (day < from || day > to) continue;
			for (const row of this.fileRows(name)) {
				if (row.kind === 'delete') deleted.add(row.id);
				else if (row.kind === 'edit' && messages.has(row.id))
					Object.assign(messages.get(row.id), {
						content: row.content,
						edited: row.t,
					});
				else if (row.kind === 'message' && !messages.has(row.id))
					messages.set(row.id, { ...row });
			}
		}
		const at = (row) => Date.parse(row.t);
		return [...messages.values()]
			.filter(
				(row) =>
					!deleted.has(row.id) &&
					(!since || at(row) >= since) &&
					(!until || at(row) <= until),
			)
			.sort((a, b) => at(a) - at(b));
	}

	// 每则讯息的出处资料：派生的东西（记忆记录、人物卡、日记、事实）在被带进她的提示之前，用它核对来源。
	// id → { id, scope, bot, author_id, author, content（最后一次编辑后）, t（毫秒）, edited（最后一次编辑的毫秒，没编辑过就没有）, deleted }。
	// 被删的讯息（帐本里有 delete 行）deleted 为 true；names 是非 bot 作者的名字（给 subject 比对人名用）。
	// 帐本没变（每个日档的大小与修改时间一样）就吃快取。
	index() {
		let files;
		try {
			files = readdirSync(this.root)
				.filter((name) => /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(name))
				.sort();
		} catch {
			return { messages: new Map(), names: new Set() };
		}
		const signature = files
			.map((name) => {
				const info = statSync(join(this.root, name));
				return `${name}:${info.size}:${info.mtimeMs}`;
			})
			.join('|');
		if (this.indexCache?.signature === signature) return this.indexCache.value;
		const messages = new Map();
		const deleted = new Set();
		const names = new Set();
		for (const name of files)
			for (const row of this.fileRows(name)) {
				if (row.kind === 'delete') deleted.add(row.id);
				else if (row.kind === 'edit' && messages.has(row.id))
					Object.assign(messages.get(row.id), {
						content: row.content,
						edited: Date.parse(row.t),
					});
				else if (row.kind === 'message' && !messages.has(row.id)) {
					messages.set(row.id, {
						id: row.id,
						scope: row.scope,
						bot: Boolean(row.bot),
						author_id: row.author_id,
						author: row.author,
						content: row.content,
						t: Date.parse(row.t),
					});
					if (!row.bot && row.author) names.add(String(row.author));
				}
			}
		for (const id of deleted)
			if (messages.has(id)) messages.get(id).deleted = true;
			else messages.set(id, { id, deleted: true });
		const value = { messages, names };
		this.indexCache = { signature, value };
		return value;
	}

	lookup(id) {
		return this.index().messages.get(String(id));
	}

	// 这句话在问「谁」？回传 { known, unknown }：
	// known＝句子里出现的、帐本里有的人的名字；unknown＝看起来像外号（阿X／小X／老X）、却在帐本的内容和作者里都没出现过的名字。
	// 问的是一个从没出现过的人（unknown 非空而且没有 known），拿来凑数的旧话和记忆（靠「最近」「在忙」这类常用词撞上的）都是别人的，不该带出来。
	subject(text, normalize = (value) => String(value).toLowerCase(), index = this.index()) {
		const query = normalize(String(text ?? ''));
		const { messages, names } = index;
		const known = [...names]
			.map((name) => normalize(name))
			.filter((name) => name.length >= 2 && query.includes(name));
		const unknown = [];
		for (const match of query.matchAll(/(?:阿|小|老)\p{Script=Han}{1,2}/gu)) {
			const forms = [match[0], match[0].slice(0, 2)];
			if (forms.some((form) => NICK_WORDS.has(form))) continue;
			if (
				known.some((name) => forms.some((form) => name.includes(form))) ||
				[...names].some((name) =>
					forms.some((form) => normalize(name).includes(form)),
				)
			)
				continue;
						// 帐本里有没有别人提过：这句提问本身早在召回之前就记进帐本了（桥接先记帐本才交给会话），不能算。
			let mentioned = false;
			for (const row of messages.values()) {
				if (!row.content) continue;
				const content = normalize(row.content);
				if (content !== query && forms.some((form) => content.includes(form))) {
					mentioned = true;
					break;
				}
			}
			if (!mentioned && !unknown.includes(match[0])) unknown.push(match[0]);
		}
		return { known, unknown };
	}

	// 关键词（空白分开、全部都要有）、作者、频道、时间、范围（group / dm）筛选；
	// normalize 把繁简、大小写统一后再比。回传最新的 limit 则，按时间排序。
	// 她自己说的话（bot）预设不算：那不是证据，常常是她自己讲错又被整理进去的；要核对她自己说过什么，用 includeBot 或指定作者。
	search({
		query = '',
		author,
		channel,
		since,
		until,
		scopes = ['group'],
		limit = 20,
		includeBot = false,
		normalize = (text) => String(text).toLowerCase(),
	} = {}) {
		const words = normalize(query).split(/\s+/).filter(Boolean);
		const who = author ? normalize(author) : '';
		const hits = this.load({ since, until }).filter(
			(row) =>
				scopes.includes(row.scope) &&
				(includeBot || Boolean(who) || !row.bot) &&
				(!channel || row.channel_id === channel) &&
				(!who ||
					row.author_id === author ||
					normalize(row.author).includes(who)) &&
				words.every((word) =>
					normalize(
						row.content +
							' ' +
							(row.attachments ?? []).map((file) => file.name).join(' '),
					).includes(word),
				),
		);
		return hits.slice(-limit);
	}

	// 每轮自动想起：拿这句话跟帐本里的旧讯息比片段，分数高的几则就是「可能想起的旧讯息」。
	// 片段按「在帐本里多少则讯息出现」加权（越常见越不算分，「等下」「回来」几乎不计），
	// 长讯息按长度打折，免得靠篇幅取胜。bot 的讯息（链接预览等）不参与；同一频道最近的讯息
	// （本来就在她眼前）与这句本身不算。至少要两个片段对上、总分过门槛才算想起。
	related(
		text,
		{
			scopes = ['group'],
			normalize = (value) => String(value).toLowerCase(),
			channel,
			recentSince,
			excludeId,
			limit = 4,
			minScore = 9,
			withScore = false,
		} = {},
	) {
		const wanted = grams(text, normalize);
		if (wanted.size < 2) return [];
		// 太短的讯息（「好啊」「笑死」）承载不了话题，不参与想起。
		const rows = this.load()
			.filter((row) => scopes.includes(row.scope) && !row.bot)
			.map((row) => ({ row, own: grams(row.content, normalize) }))
			.filter(({ own }) => own.size >= 3);
		if (!rows.length) return [];
		const seen = new Map();
		for (const { own } of rows)
			for (const gram of own) seen.set(gram, (seen.get(gram) ?? 0) + 1);
		// 英文整个单字比中文两字片段更有辨识度（外号、游戏名），多算一点。
		const weight = (gram) =>
			Math.log((rows.length + 1) / ((seen.get(gram) ?? 0) + 1)) *
			(/^[a-z]/.test(gram) ? 1.5 : 1);
		const average =
			rows.reduce((sum, { own }) => sum + own.size, 0) / rows.length || 1;
		const scored = [];
		for (const { row, own } of rows) {
			if (
				row.id === excludeId ||
				(channel &&
					row.channel_id === channel &&
					Date.parse(row.t) >= recentSince)
			)
				continue;
			let hits = 0;
			let score = 0;
			for (const gram of wanted)
				if (own.has(gram)) {
					hits++;
					score += weight(gram);
				}
			score /= 0.5 + (0.5 * own.size) / average;
			// 一个很少见的字（例如某个外号）分数就够高；常见片段凑不过门槛。
			if (hits && score >= minScore)
				scored.push({ row, score, at: Date.parse(row.t) });
		}
		// 有强的就把比它弱一半以上的刷掉，免得陪榜的杂讯一起带进来。
		const best = Math.max(0, ...scored.map(({ score }) => score));
		return scored
			.filter(({ score }) => score >= best / 2)
			.sort((a, b) => b.score - a.score || b.at - a.at)
			.slice(0, limit)
			.map(({ row, score }) => (withScore ? { ...row, score } : row))
			.sort((a, b) => Date.parse(a.t) - Date.parse(b.t));
	}

	// 一则讯息的前后文（同一个频道）。
	around(id, { before = 5, after = 5, scopes = ['group'] } = {}) {
		const all = this.load();
		const target = all.find((row) => row.id === id);
		if (!target || !scopes.includes(target.scope)) return [];
		const same = all.filter((row) => row.channel_id === target.channel_id);
		const index = same.indexOf(target);
		return same.slice(Math.max(0, index - before), index + after + 1);
	}

	// kind：message（新讯息）、edit（改过的内容）、delete（单则或批次删除）。
	// extra 会并进那一行（例如补抓的标记）。
	record(kind, data, extra = {}) {
		const stamp = new Date(this.now()).toISOString();
		const where = {
			channel_id: String(data.channel_id ?? ''),
			guild_id: data.guild_id ? String(data.guild_id) : null,
			scope: data.guild_id ? 'group' : 'dm',
		};
		if (kind === 'delete') {
			for (const id of data.ids ?? [data.id])
				if (id) this.append({ t: stamp, kind, id: String(id), ...where });
			return;
		}
		// 连结预览载入也会发编辑事件，但没有 edited_timestamp，内容也没变。
		if (kind === 'edit' && !data.edited_timestamp) return;
		const author = data.author ?? {};
		const sent = kind === 'edit' ? data.edited_timestamp : data.timestamp;
		this.append({
			t: Number.isFinite(Date.parse(sent ?? ''))
				? new Date(sent).toISOString()
				: stamp,
			kind,
			id: String(data.id),
			...where,
			author_id: author.id ? String(author.id) : null,
			author: String(author.global_name || author.username || ''),
			bot: Boolean(author.bot),
			content: this.redact(String(data.content ?? '')),
			...(data.message_reference?.message_id
				? { reply_to: String(data.message_reference.message_id) }
				: {}),
			...(data.attachments?.length
				? {
						attachments: data.attachments.slice(0, 10).map((file) => ({
							name: String(file.filename ?? ''),
							type: file.content_type ?? null,
							url: String(file.url ?? ''),
						})),
					}
				: {}),
			...(data.sticker_items?.length
				? { stickers: data.sticker_items.map((item) => String(item.name)) }
				: {}),
			...extra,
		});
	}
}

// 补抓：把 bot 所在每个服务器的每个频道（含讨论串、封存的公开讨论串）与拥有者私讯的历史讯息，
// 从最新往回一页 100 则抓进帐本。只读、不发任何讯息；帐本里已经有的 id 跳过。
// REST 抓到的讯息不带 guild_id，这里补上，范围才标得对。
const TEXT_TYPES = new Set([0, 2, 5, 10, 11, 12, 13]);

export async function backfillLedger(
	rest,
	archive,
	{ ownerIds = [], pause = () => delay(300), log = () => {} } = {},
) {
	const known = new Set(archive.load().map((row) => row.id));
	const report = { channels: 0, added: 0, skipped: 0, failed: [] };
	const page = async (channel, guildId) => {
		report.channels++;
		let before;
		for (;;) {
			let rows;
			try {
				rows = await rest.request(
					'GET',
					`/channels/${channel.id}/messages?limit=100${before ? '&before=' + before : ''}`,
				);
			} catch (error) {
				report.failed.push(
					`${channel.name ?? channel.id}：${String(error?.message).slice(0, 80)}`,
				);
				return;
			}
			if (!Array.isArray(rows) || !rows.length) return;
			for (const row of rows) {
				if (known.has(row.id)) {
					report.skipped++;
					continue;
				}
				known.add(row.id);
				archive.record(
					'message',
					{ ...row, ...(guildId ? { guild_id: guildId } : {}) },
					{ backfill: true },
				);
				report.added++;
			}
			before = rows.at(-1).id;
			await pause();
			if (rows.length < 100) return;
		}
	};
	const guilds = await rest.request('GET', '/users/@me/guilds?limit=200');
	for (const guild of guilds ?? []) {
		let channels = [];
		try {
			channels = await rest.request('GET', `/guilds/${guild.id}/channels`);
			const active = await rest.request(
				'GET',
				`/guilds/${guild.id}/threads/active`,
			);
			channels.push(...(active?.threads ?? []));
			for (const parent of channels.filter((row) =>
				[0, 5, 15].includes(row.type),
			))
				try {
					const archived = await rest.request(
						'GET',
						`/channels/${parent.id}/threads/archived/public?limit=100`,
					);
					channels.push(...(archived?.threads ?? []));
				} catch {
					/* 没有封存讨论串或没权限就略过。 */
				}
		} catch (error) {
			report.failed.push(
				`${guild.name}：${String(error?.message).slice(0, 80)}`,
			);
		}
		const seen = new Set();
		for (const channel of channels)
			if (TEXT_TYPES.has(channel.type) && !seen.has(channel.id)) {
				seen.add(channel.id);
				log(`补抓 ${guild.name} #${channel.name}`);
				await page(channel, guild.id);
			}
	}
	for (const id of ownerIds)
		try {
			const dm = await rest.request('POST', '/users/@me/channels', {
				recipient_id: id,
			});
			await page({ id: dm.id, name: '私讯' });
		} catch (error) {
			report.failed.push(`私讯：${String(error?.message).slice(0, 80)}`);
		}
	return report;
}

// 一键备份：她的记忆、会话纪录、收藏的图、对话附件与她的家打成一个 tar.gz，留最近 keep 份。
// 打完列一次档案清单验证读得回来；清单是空的或缺帐本就当失败。
export async function createBackup({
	root,
	paths,
	out,
	keep = 14,
	now = Date.now(),
	exclude = [],
	name = 'whale-memory',
}) {
	mkdirSync(out, { recursive: true, mode: 0o700 });
	const stamp = new Date(now)
		.toISOString()
		.replace(/[-:]/g, '')
		.replace('T', '-')
		.slice(0, 13);
	const target = join(out, `${name}-${stamp}.tar.gz`);
	const present = paths
		.filter((path) => existsSync(path))
		.map((path) => (isAbsolute(path) ? relative(root, path) : path))
		.map((path) => path.split('\\').join('/'));
	if (!present.length) throw new Error('没有可以备份的资料。');
	await run(
		'tar',
		[
			'-czf',
			target,
			...exclude.map((pattern) => '--exclude=' + pattern),
			'-C',
			root,
			...present,
		],
		{ maxBuffer: 16 * 1024 * 1024 },
	);
	const { stdout } = await run('tar', ['-tzf', target], {
		maxBuffer: 64 * 1024 * 1024,
	});
	const entries = stdout.split('\n').filter(Boolean);
	if (!entries.length) throw new Error('备份档是空的。');
	const backups = readdirSync(out)
		.filter((file) => file.startsWith(name + '-2') && file.endsWith('.tar.gz'))
		.sort();
	for (const file of backups.slice(0, Math.max(0, backups.length - keep)))
		unlinkSync(join(out, file));
	return {
		path: target,
		size: statSync(target).size,
		entries: entries.length,
		ledger: entries.some((entry) => entry.includes('social-runtime/archive/')),
	};
}

// ---------- 派生条目（记忆记录、人物卡条目、长期事实）共用的出处判断 ----------
// 呼叫点：memory.mjs 的 vet／visible／remember（去重与配额）／recallAcross；notes.mjs 的 vet／visibleLines／updateCard／addFact／context。
// 一条派生条目的来源 ids 对原文帐本核对。lookup(id) 回传帐本里那则讯息的出处资料（见 ArchiveStore.index），查不到回 undefined；
// editedAfter(info) 由呼叫端决定「这则讯息的编辑是不是发生在这条整理之后」（记忆比时间，人物卡比日期）。
// 回传 { keep, ids, cite, revised, edited? }：
//   keep＝能不能带进提示（任何一个来源被删→不能；所有来源都是她自己说的话→不能；查不到来源→照旧能）；
//   revised＝有来源被编辑过；edited＝有来源在整理之后被编辑过时，编辑后的原话（这条整理的是旧版本）；cite＝挑一个出处给她去翻原文（优先不是她自己说的）。
export function judgeSources(ids, lookup, editedAfter) {
	const sources = ids.map((id) => ({ id, info: lookup(id) }));
	if (!sources.length) return { keep: true, ids, cite: undefined, revised: false };
	if (sources.some(({ info }) => info?.deleted)) return { keep: false, ids };
	if (sources.every(({ info }) => info?.bot)) return { keep: false, ids };
	const stale = sources.find(({ info }) => info?.edited && editedAfter(info))?.info;
	const best = sources.find(({ info }) => !info?.bot && !info?.deleted) ?? sources[0];
	return {
		keep: true,
		ids,
		cite: best.id,
		revised: sources.some(({ info }) => info?.edited),
		...(stale ? { edited: String(stale.content ?? '') } : {}),
	};
}

// 带进提示时，来源被编辑过的旧条目一律照常带出（记忆带编辑后的整句原话，人物卡／事实附上编辑后的原话），编辑之后整理的新条目也照常带出；
// 不判断「谁取代谁」（文字相似度会误判短句，写入时间或行序也不可靠——旧会话可能很晚才整理完），宁可同一件事出现新旧两种说法，也不藏还有效的事。
// 唯一的合并：stale 旧条目「显示的内容一模一样、来源共享」时只留排在最前面的一条（记忆的 stale 旧条目显示的都是同一句编辑后的原话，重复带没有意义，不丢信息）。
// **合并只能在最后输出时做**（memory.mjs 的 dedupeShown，在召回排好序之后、或列表取完之后）：检索和打分要对所有记录进行，先合并会让被合并掉的那条搜不到。
// items：[{ ids, stale, order, shown? }]（order 越大越新；shown 是带进提示时显示的内容，人物卡／事实没有，所以不合并）；回传要留下的 index 集合。
export function collapseRevised(items) {
	const shares = (a, b) => a.ids.some((id) => b.ids.includes(id));
	const dropped = new Set();
	const kept = [];
	items
		.map((_, index) => index)
		.filter((index) => items[index].stale && items[index].shown !== undefined)
		.sort((a, b) => items[b].order - items[a].order)
		.forEach((index) => {
			if (kept.some((other) => items[other].shown === items[index].shown && shares(items[index], items[other]))) dropped.add(index);
			else kept.push(index);
		});
	return new Set(items.map((_, index) => index).filter((index) => !dropped.has(index)));
}

// 写入时判断「已经有这一条」：existing 是旧条目核对的结果（judgeSources 加 stale），newIds 是新条目的来源。
//  - 来源已失效（被删、全是她自己的话）的旧条目不算——它带不进提示，不能挡住后来有效出处说的同一件事；
//  - 来源被编辑过的旧条目：新条目来自同一个来源（编辑之后重新整理出一样的字）就是同一件事；来自别的来源不算。
export function blocksDuplicate(existing, newIds) {
	if (!existing.keep) return false;
	if (!existing.stale) return true;
	return existing.ids.some((id) => newIds.includes(id));
}
