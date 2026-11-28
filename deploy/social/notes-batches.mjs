// 背景整理记忆的批次帐：一批对话交给模型整理以前先存下来，成功才收掉；失败留下「在哪一步、什么原因」
// （原因只记类别，不记内文：错误讯息可能带金钥或聊天内容），之后有限次重试。
// 每一步做完就记一笔（模型的结果、哪几个人的卡写好了、日记、事实），重试时做过的不再做，不会重复写入。
// 上一个程序跑到一半就停掉的批次（还没失败过），新程序启动后也算到期（冷启动接着做）。
import { createHash } from 'node:crypto';
import {
	mkdirSync,
	readFileSync,
	readdirSync,
	renameSync,
	unlinkSync,
	writeFileSync,
} from 'node:fs';
import { join } from 'node:path';

const NAME = /^[a-f0-9]{24}\.json$/;

// 错误只留类别：逾时、HTTP 状态码、档案系统的错误码、格式错、模型没回字。
export function failureKind(error) {
	if (error?.kind && /^[a-z-]{2,24}$/.test(error.kind)) return error.kind;
	const code = String(error?.code ?? '');
	if (/^E[A-Z]{2,15}$/.test(code)) return 'fs-' + code;
	if (error?.name === 'AbortError' || error?.name === 'TimeoutError')
		return 'timeout';
	const status = Number(error?.status ?? error?.statusCode);
	if (Number.isInteger(status) && status >= 100 && status < 600)
		return 'http-' + status;
	const message = String(error?.message ?? '');
	const http = /\b([45]\d\d)\b/.exec(message);
	if (http) return 'http-' + http[1];
	if (error instanceof SyntaxError) return 'bad-json';
	return 'error';
}

export class NoteBatches {
	constructor(
		root,
		{ maxAttempts = 3, retryMs = 5 * 60000, now = Date.now } = {},
	) {
		this.root = root;
		this.maxAttempts = maxAttempts;
		this.retryMs = retryMs;
		this.now = now;
		for (const folder of ['', 'failed'])
			mkdirSync(join(root, folder), { recursive: true, mode: 0o700 });
		this.donePath = join(root, 'done.txt');
		this.bootAt = now();
	}

	path(id, folder = '') {
		return join(this.root, folder, id + '.json');
	}

	write(batch) {
		const temporary = this.path(batch.id) + '.tmp';
		writeFileSync(temporary, JSON.stringify(batch), { mode: 0o600 });
		renameSync(temporary, this.path(batch.id));
	}

	read(id) {
		try {
			return JSON.parse(readFileSync(this.path(id), 'utf8'));
		} catch {
			return undefined;
		}
	}

	done() {
		try {
			return new Set(
				readFileSync(this.donePath, 'utf8').split('\n').filter(Boolean),
			);
		} catch {
			return new Set();
		}
	}

	// 同一批讯息（同样的讯息 id、同一个地点）只会有一份；已经整理完的再交来就略过（回 undefined）。
	// where：这批在哪个频道（{ channelId, guildId }，重试前核对删除要用）。
	open(lines, place = '', personal = false, where) {
		const key = JSON.stringify([
			place,
			personal,
			lines.map((line) => String(line.id ?? line.text)),
		]);
		const id = createHash('sha256').update(key).digest('hex').slice(0, 24);
		if (this.done().has(id)) return undefined;
		const existing = this.read(id);
		if (existing) return existing;
		const batch = {
			id,
			created: this.now(),
			place,
			personal,
			...(where ? { where } : {}),
			lines,
			attempts: 0,
			step: {},
		};
		this.write(batch);
		return batch;
	}

	// 一步做完就记下来（result：模型整理的结果；people：写好人物卡的人；journal、facts）。
	mark(batch, patch) {
		batch.step = { ...batch.step, ...patch };
		this.write(batch);
	}

	// 记一次失败：哪一步、什么类别、第几次、下次什么时候可以重试。超过次数就移到 failed/（不删，留给维护看）。
	fail(batch, stage, error) {
		batch.attempts = (batch.attempts ?? 0) + 1;
		batch.failure = { stage, kind: failureKind(error), at: this.now() };
		batch.retryAt = this.now() + this.retryMs * batch.attempts;
		this.write(batch);
		if (batch.attempts >= this.maxAttempts) {
			renameSync(this.path(batch.id), this.path(batch.id, 'failed'));
			return { ...batch.failure, attempts: batch.attempts, gaveUp: true };
		}
		return { ...batch.failure, attempts: batch.attempts, gaveUp: false };
	}

	finish(batch) {
		const done = [...this.done(), batch.id].slice(-2000);
		const temporary = this.donePath + '.tmp';
		writeFileSync(temporary, done.join('\n') + '\n', { mode: 0o600 });
		renameSync(temporary, this.donePath);
		try {
			unlinkSync(this.path(batch.id));
		} catch {
			/* 已经收掉了。 */
		}
	}

	// 还留着的批次什么时候可以再跑：失败过的看 retryAt；上一个程序留下、还没失败过的现在就可以。
	// 这个程序自己刚开、还在排队的（attempts 0、在启动之后建的）不算。
	retryTime(batch) {
		if (batch?.attempts) return batch.retryAt ?? 0;
		return batch && batch.created < this.bootAt ? batch.created : undefined;
	}

	pending() {
		let names;
		try {
			names = readdirSync(this.root).filter((name) => NAME.test(name));
		} catch {
			return [];
		}
		return names
			.map((name) => this.read(name.slice(0, -5)))
			.filter((batch) => batch && this.retryTime(batch) !== undefined);
	}

	// 到了可以重试的批次（时间到了、还没放弃）。
	due() {
		return this.pending()
			.filter((batch) => this.retryTime(batch) <= this.now())
			.sort((a, b) => a.created - b.created);
	}

	// 下一批什么时候到期（没有就回 undefined），给自动唤醒用。
	nextDue() {
		const times = this.pending().map((batch) => this.retryTime(batch));
		return times.length ? Math.min(...times) : undefined;
	}
}

// 一批对话的「内容指纹」：讯息 id 与文字。重试前核对过删改，指纹变了，之前模型整理的结果就不能再用。
export function linesBasis(lines) {
	return createHash('sha256')
		.update(
			JSON.stringify(lines.map((line) => [String(line.id ?? ''), line.text])),
		)
		.digest('hex')
		.slice(0, 24);
}
