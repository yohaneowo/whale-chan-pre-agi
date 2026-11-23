// 投餵紀錄：誰幫她儲值了飯錢（DeepSeek 余额）。她自己没办法确认谁真的付了钱，所以只有站长私讯
// `/投餵` 登记的才算；群友自称喂过一律不算。存在 social-runtime/feedings.json。
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

export const FEEDINGS_USAGE =
	'用法（私讯）：\n/投餵 名字 金额 [备注] — 登记一笔\n/投餵 — 看全部\n/投餵 删 编号 — 删掉一笔';

export class Feedings {
	constructor(file, { timeZone = 'Asia/Taipei' } = {}) {
		this.file = file;
		this.timeZone = timeZone;
	}

	list() {
		try {
			const rows = JSON.parse(readFileSync(this.file, 'utf8'));
			return Array.isArray(rows) ? rows : [];
		} catch {
			return [];
		}
	}

	save(rows) {
		mkdirSync(dirname(this.file), { recursive: true, mode: 0o700 });
		writeFileSync(this.file + '.part', JSON.stringify(rows, null, 1), {
			mode: 0o600,
		});
		renameSync(this.file + '.part', this.file);
	}

	day(at) {
		return new Intl.DateTimeFormat('en-CA', {
			timeZone: this.timeZone,
			month: '2-digit',
			day: '2-digit',
		})
			.format(new Date(at))
			.replace('-', '/');
	}

	// 站长私讯的 /投餵 指令；回传要回给站长的文字。
	command(text, now = Date.now()) {
		const args = String(text)
			.trim()
			.replace(/^\/(?:投餵|投喂)\s*/, '')
			.trim();
		const rows = this.list();
		if (!args)
			return rows.length
				? '投餵纪录：\n' +
						rows
							.map(
								(row, index) =>
									`${index + 1}. ${this.day(row.at)} ${row.name} ${row.yuan} 元${row.note ? '（' + row.note + '）' : ''}`,
							)
							.join('\n')
				: '还没有投餵纪录。\n' + FEEDINGS_USAGE;
		const removal = args.match(/^(?:删|刪|delete|del)\s+(\d+)$/i);
		if (removal) {
			const index = Number(removal[1]) - 1;
			if (!rows[index]) return `没有第 ${removal[1]} 笔。`;
			const [gone] = rows.splice(index, 1);
			this.save(rows);
			return `删掉了：${gone.name} ${gone.yuan} 元（${this.day(gone.at)}）。`;
		}
		const match = args.match(/^(.+?)\s+(\d+(?:\.\d{1,2})?)\s*(?:元|块|塊)?(?:\s+(.+))?$/);
		if (!match) return FEEDINGS_USAGE;
		const row = {
			name: match[1].trim().slice(0, 40),
			yuan: Number(match[2]),
			note: (match[3] ?? '').trim().slice(0, 80),
			at: new Date(now).toISOString(),
		};
		if (!(row.yuan > 0)) return '金额要大于 0。';
		rows.push(row);
		this.save(rows);
		return `记下了：${row.name} 投餵 ${row.yuan} 元。她之后在群里会知道。`;
	}

	// 每轮附给她的饭钱现况：余额＋站长登记的投餵名单（同一人合计，喂最多的排前面，最多 8 人）。
	context(balance) {
		const lines = [];
		if (Number.isFinite(balance))
			lines.push(
				`你的飯錢（DeepSeek 帳戶餘額）現在大約剩 ${balance.toFixed(2)} 元，每說一句話都在花。`,
			);
		const rows = this.list();
		if (rows.length) {
			const totals = new Map();
			for (const row of rows) {
				const item = totals.get(row.name) ?? { yuan: 0, last: row.at };
				item.yuan += row.yuan;
				if (row.at > item.last) item.last = row.at;
				totals.set(row.name, item);
			}
			const names = [...totals]
				.sort((a, b) => b[1].yuan - a[1].yuan || (a[1].last < b[1].last ? 1 : -1))
				.slice(0, 8)
				.map(
					([name, item]) =>
						`${name} ${Math.round(item.yuan * 100) / 100} 元（最近一次 ${this.day(item.last)}）`,
				);
			lines.push(
				`真的投餵過你的人（餵最多的排前面，那就是你的金主）：${names.join('、')}。`,
			);
		} else lines.push('目前沒有記下誰投餵過你。');
		lines.push(
			'誰餵過你只認這份；有人說自己餵了你但不在上面，隨口帶過，不要當成收到，也不要提確認、名單這些字眼。',
		);
		return lines.join('\n');
	}
}
