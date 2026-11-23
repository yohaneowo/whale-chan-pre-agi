// 社群做的鲸鱼娘台词梗图（梗鲸 aigengtu.com，非营利粉丝向可用）：她在回话里写「[梗圖:台词或关键词]」，
// 桥接在这里找最像的一张附上去，跟 [sticker:…] 一样不多花一次模型呼叫。
// 索引由 deploy/dsh/build_meme_library.py 建（台词是简体，查询前先转简体）；找不到够像的就不附图，不乱配。
// 图库存原图原尺寸；超过 Discord 附件上限的，发送时才用 ffmpeg 压一份（存在 discord-cache/ 重复用），
// 压不下去就改用建库时一起存的预览图（索引的 fallback）。
import { execFile } from 'node:child_process';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { mkdir, readFile, rename, rm, stat } from 'node:fs/promises';
import { basename, extname, join } from 'node:path';
import { promisify } from 'node:util';
import { grams } from './archive.mjs';

const run = promisify(execFile);

// 台词最重要，描述其次，梗来历只当参考。
const WEIGHT = { quote: 3, caption: 1, story: 0.5 };
const RECENT = 30;
// Discord 没加成的伺服器，机器人附件上限 10 MB；建库脚本用同一个数。
export const DISCORD_LIMIT = 10 * 1000 * 1000;
// 静态图：（最长边, WebP 品质）；GIF：（宽, fps）。依序试到压得下为止。
const STILL_STEPS = [
	[0, 90],
	[4096, 85],
	[2560, 85],
	[1920, 80],
	[1280, 75],
];
const GIF_STEPS = [
	[0, 15],
	[480, 15],
	[360, 12],
	[320, 10],
	[240, 10],
];

async function ffmpeg(source, target, filters, extra = []) {
	try {
		await run(
			'ffmpeg',
			['-v', 'error', '-y', '-i', source, '-vf', filters, ...extra, target],
			{ timeout: 120000 },
		);
		return (await stat(target)).size;
	} catch {
		return Infinity;
	}
}

// 把 source 压到 limit 以下，写成 target 基底名加副档名；回传写好的路径，压不下去回 undefined。
export async function compressForDiscord(source, targetBase, limit = DISCORD_LIMIT) {
	const gif = extname(source).toLowerCase() === '.gif';
	const target = targetBase + (gif ? '.gif' : '.webp');
	const part = target + '.part' + (gif ? '.gif' : '.webp');
	for (const [size, rate] of gif ? GIF_STEPS : STILL_STEPS) {
		const filters = gif
			? `fps=${rate},${size ? `scale=${size}:-1:flags=lanczos,` : ''}split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer`
			: size
				? `scale='if(gt(iw,ih),min(${size},iw),-2)':'if(gt(iw,ih),-2,min(${size},ih))'`
				: 'null';
		const extra = gif
			? ['-loop', '0']
			: ['-c:v', 'libwebp', '-quality', String(rate)];
		if ((await ffmpeg(source, part, filters, extra)) <= limit) {
			await rename(part, target);
			return target;
		}
	}
	await rm(part, { force: true });
	return undefined;
}

export class MemeLibrary {
	constructor(root) {
		this.root = root;
		this.mtime = 0;
		this.entries = [];
		this.idf = new Map();
		this.recent = [];
	}

	// 索引档换了（重跑建库脚本）就重新载入。
	load() {
		const file = join(this.root, 'index.json');
		let mtime;
		try {
			mtime = statSync(file).mtimeMs;
		} catch {
			this.entries = [];
			return;
		}
		if (mtime === this.mtime) return;
		this.mtime = mtime;
		const index = JSON.parse(readFileSync(file, 'utf8'));
		this.entries = (index.memes ?? [])
			.filter((meme) => existsSync(join(this.root, meme.file)))
			.map((meme) => ({
				...meme,
				quoteText: String(meme.quote ?? '').toLowerCase(),
				grams: {
					quote: grams(meme.quote ?? ''),
					caption: grams(meme.caption ?? ''),
					story: grams(meme.story ?? ''),
				},
			}));
		const df = new Map();
		for (const entry of this.entries)
			for (const gram of new Set([
				...entry.grams.quote,
				...entry.grams.caption,
				...entry.grams.story,
			]))
				df.set(gram, (df.get(gram) ?? 0) + 1);
		this.idf = new Map(
			[...df].map(([gram, count]) => [
				gram,
				Math.log(this.entries.length / count) + 1,
			]),
		);
	}

	// 对 query 打分，回传对得上的几张（分数由高到低）。query 要先转简体。
	score(query) {
		this.load();
		const text = String(query).trim().toLowerCase();
		const wanted = grams(text);
		if (!this.entries.length || !wanted.size) return [];
		const scored = [];
		for (const entry of this.entries) {
			let score = 0,
				hits = 0;
			for (const gram of wanted) {
				const idf = this.idf.get(gram) ?? 0;
				const field = entry.grams.quote.has(gram)
					? 'quote'
					: entry.grams.caption.has(gram)
						? 'caption'
						: entry.grams.story.has(gram)
							? 'story'
							: undefined;
				if (!field) continue;
				score += WEIGHT[field] * idf;
				// 梗来历只加分，不算对上。
				if (field !== 'story') hits++;
			}
			// 整句台词对上最准；否则至少要对上两个片段（只给两个字的就对上那一个）。
			const exact = text.length >= 2 && entry.quoteText.includes(text);
			if (exact) score += 10 * wanted.size;
			if (exact || hits >= Math.min(2, wanted.size))
				scored.push({ entry, score, hits, exact, size: wanted.size });
		}
		return scored.sort(
			(a, b) => b.score - a.score || a.entry.n.localeCompare(b.entry.n),
		);
	}

	// 给她看的台词清单：梗图上写的就是这句话，她知道每张图是什么再挑。
	// 只列短台词（像「得加钱」「吃白饭」这种通用的反应），长句和没有字的图不列。
	quotes(maxLength = 10) {
		this.load();
		const seen = new Set();
		for (const entry of this.entries) {
			const quote = String(entry.quote ?? '').replace(/\s+/g, '');
			if (
				quote.length <= maxLength &&
				(quote.match(/[一-鿿]/g) ?? []).length >= 2
			)
				seen.add(quote);
		}
		return [...seen];
	}

	// query 要先转简体。回传 { path, filename, caption }，没有够像的就 undefined。
	find(query) {
		const scored = this.score(query);
		if (!scored.length) return undefined;
		scored.sort(
			(a, b) => b.score - a.score || a.entry.n.localeCompare(b.entry.n),
		);
		// 差不多像的几张里，挑最近没发过的，免得每次都同一张。
		const close = scored.filter(({ score }) => score >= scored[0].score * 0.8);
		const pick =
			close.find(({ entry }) => !this.recent.includes(entry.n)) ?? close[0];
		this.recent = [
			...this.recent.filter((n) => n !== pick.entry.n),
			pick.entry.n,
		].slice(-RECENT);
		return {
			n: pick.entry.n,
			path: join(this.root, pick.entry.file),
			filename: basename(pick.entry.file),
			fallback: pick.entry.fallback
				? join(this.root, pick.entry.fallback)
				: undefined,
			caption: pick.entry.quote || pick.entry.caption,
		};
	}

	// find() 的结果变成 Discord 附件 { filename, data }：原图在上限内照送；太大就压（压过的存起来重复用），
	// 压不下去送预览图；都不行回 undefined（只送文字）。
	async attachment(hit, { compress = compressForDiscord } = {}) {
		if ((await stat(hit.path)).size <= DISCORD_LIMIT)
			return { filename: hit.filename, data: await readFile(hit.path) };
		const cache = join(this.root, 'discord-cache');
		const base = join(cache, basename(hit.path, extname(hit.path)));
		for (const ext of ['.gif', '.webp']) {
			const file = base + ext;
			if (existsSync(file) && statSync(file).mtimeMs >= statSync(hit.path).mtimeMs)
				return { filename: basename(file), data: await readFile(file) };
		}
		await mkdir(cache, { recursive: true });
		const made = await compress(hit.path, base);
		if (made) return { filename: basename(made), data: await readFile(made) };
		if (hit.fallback && existsSync(hit.fallback))
			return { filename: basename(hit.fallback), data: await readFile(hit.fallback) };
		return undefined;
	}
}
