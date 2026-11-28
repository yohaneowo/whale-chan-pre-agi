// 背景整理记忆的沙盒探针：只在 VM 的沙盒启动里载入（notes-sandbox.sh 放进沙盒设定；不进 release）。
// 用真宿主、真 social runtime、真模型（正式的模型路线）跑一次整理，不画图、不碰 Krita、没有 Discord。
//   1. 照旧参数（思考深度 off、maxTokens 800）与新参数（low、2000）各直接问一次模型，记思考多长、正文多长、怎么结束；
//   2. 跑一次真的 runtime.consolidate（合成的三句对话），看人物卡与日记有没有真的写出来、批次帐有没有收掉。
// 只记长度、档名与合成资料的条目，没有任何群友资料；金钥只在环境变数里。
import console from 'node:console';
import { randomUUID } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import process from 'node:process';
import { setTimeout } from 'node:timers';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export const name = 'notes-sandbox-probe';
export const inject = ['agents'];

const RELEASE = process.env.SBX_RELEASE;
const AUTHOR = '1000000000000000003';
const say = (...parts) => console.log('[sbx]', ...parts);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const short = (error) =>
	String(error?.message ?? error)
		.replace(/sk-[A-Za-z0-9_-]{8,}/g, '[key]')
		.split('\n')[0]
		.slice(0, 200);
let serial = 0n;
const nextId = () =>
	String(
		BigInt('1000000000000000500') +
			BigInt(Date.now() % 100000) * 10n +
			++serial,
	);

export function apply(ctx) {
	void (async () => {
		try {
			await ctx.get('loader')?.await?.();
			const social = await import(
				pathToFileURL(
					`${RELEASE}/web/node_modules/dsh-social-runtime/index.mjs`,
				).href
			);
			let runtime;
			for (let i = 0; i < 60 && !runtime; i++) {
				try {
					runtime = social.currentRuntime();
				} catch {
					await sleep(500);
				}
			}
			if (!runtime) return say('FAIL social runtime 没有就绪');
			const selection = ctx.get('agentDefaultModel')?.currentSelection?.();
			say(
				'模型',
				JSON.stringify({
					provider: selection?.provider,
					model: selection?.model,
				}),
			);
			for (const [label, effort, maxTokens] of [
				['旧参数', 'off', 800],
				['新参数', 'low', 2000],
			]) {
				let reasoning = 0;
				let text = '';
				let finish;
				const started = Date.now();
				try {
					for await (const chunk of ctx.get('llm').stream({
						provider: selection?.provider,
						model: selection?.model,
						// 正式路线每次都要带会话编号（跟 consolidate、画室守门员一样）。
						...(selection?.provider === 'opencode-go'
							? { sessionId: `session-${randomUUID()}` }
							: {}),
						reasoningEffort: effort,
						maxTokens,
						system:
							'只輸出 JSON：{"people":[],"journal":[],"facts":[]}，把對話裡值得記的事整理進去。',
						messages: [
							{
								role: 'user',
								content: [
									{
										type: 'text',
										text: '對話：\n[1] 沙盒（id:1）：我今天去爬山了，腳好痠\n[2] 你：辛苦了 泡個腳吧\n[3] 沙盒（id:1）：下週還要去合歡山',
									},
								],
							},
						],
					})) {
						if (chunk.type === 'reasoning-delta')
							reasoning += chunk.text.length;
						if (chunk.type === 'text-delta') text += chunk.text;
						if (chunk.type === 'finish')
							finish =
								chunk.reason?.kind +
								(chunk.reason?.failure
									? ' ' +
										String(chunk.reason.failure.code ?? '') +
										'：' +
										short(chunk.reason.failure.message)
									: '') +
								(chunk.reason?.message
									? '：' + short(chunk.reason.message)
									: '');
					}
				} catch (error) {
					finish = 'thrown: ' + short(error);
				}
				let json = false;
				try {
					JSON.parse(text.replace(/^```(?:json)?\s*|\s*```$/g, '').trim());
					json = true;
				} catch {
					/* 不是 JSON。 */
				}
				say(
					label,
					JSON.stringify({
						effort,
						maxTokens,
						reasoningChars: reasoning,
						textChars: text.length,
						json,
						finish,
						secs: Math.round((Date.now() - started) / 1000),
					}),
				);
			}
			const notes = join(runtime.root, 'notes');
			const now = Date.now();
			const lines = [
				{
					role: 'user',
					speaker: AUTHOR,
					name: '沙盒',
					text: '我今天去爬山了，腳好痠',
					id: nextId(),
					at: now - 60_000,
				},
				{
					role: 'assistant',
					text: '辛苦了 泡個腳吧',
					id: nextId(),
					at: now - 50_000,
				},
				{
					role: 'user',
					speaker: AUTHOR,
					name: '沙盒',
					text: '下週還要去合歡山，我養的柴犬叫豆皮會一起去',
					id: nextId(),
					at: now - 40_000,
				},
			];
			const before = {
				card: existsSync(join(notes, 'people', AUTHOR + '.md')),
				journal: existsSync(join(notes, 'journal'))
					? readdirSync(join(notes, 'journal')).length
					: 0,
			};
			let outcome;
			const started = Date.now();
			try {
				outcome = {
					ok: true,
					value: await runtime.consolidate(lines, '#沙盒頻道'),
				};
			} catch (error) {
				outcome = {
					ok: false,
					error: short(error),
					stage: error?.stage,
					kind: error?.kind,
				};
			}
			const cardPath = join(notes, 'people', AUTHOR + '.md');
			const card = existsSync(cardPath) ? readFileSync(cardPath, 'utf8') : '';
			const journalDir = join(notes, 'journal');
			const journals = existsSync(journalDir) ? readdirSync(journalDir) : [];
			const journal = journals
				.map((file) => readFileSync(join(journalDir, file), 'utf8'))
				.join('\n');
			const batchDir = join(notes, 'batches');
			const pending = existsSync(batchDir)
				? readdirSync(batchDir).filter((file) => file.endsWith('.json'))
				: [];
			let records = [];
			try {
				records = runtime.memory
					.active('group')
					.map((row) => ({ kind: row.kind, channel: Boolean(row.channelId) }));
			} catch {
				/* 读不到就算了。 */
			}
			say(
				'整理结果',
				JSON.stringify({
					outcome,
					secs: Math.round((Date.now() - started) / 1000),
					before,
					cardNotes: card.split('\n').filter((line) => line.startsWith('- ')),
					journalLines: journal
						.split('\n')
						.filter((line) => line.startsWith('- ')),
					pendingBatches: pending.length,
					records,
				}),
			);
			say('DONE');
		} catch (error) {
			say('FAIL', short(error));
		}
		setTimeout(() => process.kill(process.pid, 'SIGTERM'), 3000).unref?.();
	})();
}
