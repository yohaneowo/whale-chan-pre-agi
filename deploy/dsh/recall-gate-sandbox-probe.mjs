// 回忆守门员的真模型实测探针：只在 VM 的沙盒启动里载入（notes-sandbox.sh 用 PROBE 指定；不进 release）。
// 用真宿主、正式的模型路线，一题一题问（一次只问一个）：
//   gate：新版 recall-rules.mjs 的 createRecallGate（要不要想起更早的事），每题跑 SBX_ROUNDS 轮；
//   chat：对照组「让聊天模型自己决定要不要叫 chat_search」——群聊人设 + 她的行为规则 + chat_search 工具，
//         看她第一步有没有叫工具（只看第一步、叫了就停）。只跑守门员题目、一轮。
// 题目是合成的（deploy/eval/recall-gate），每题印一行 [sbx] JSON：判断、毫秒、用量；不印任何金钥。
import console from 'node:console';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import process from 'node:process';
import { clearTimeout, setTimeout } from 'node:timers';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export const name = 'recall-gate-sandbox-probe';
export const inject = ['agents'];

const RELEASE = process.env.SBX_RELEASE;
const say = (value) => console.log('[sbx]', JSON.stringify(value));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const short = (error) =>
	String(error?.message ?? error)
		.replace(/sk-[A-Za-z0-9_-]{8,}/g, '[key]')
		.split('\n')[0]
		.slice(0, 160);

const SEARCH = {
	name: 'chat_search',
	description:
		'翻聊天紀錄原文：有人問過去的具體事情（誰說過什麼、哪天發生什麼）時先用這個查，再回答，不要憑印象。可以用關鍵詞、說話的人、日期範圍篩選。',
	parameters: {
		type: 'object',
		properties: {
			query: { type: 'string', description: '關鍵詞，空白分開' },
			author: { type: 'string' },
		},
	},
};

export function apply(ctx) {
	void (async () => {
		try {
			await ctx.get('loader')?.await?.();
			await sleep(3000);
			const rules = await import(
				pathToFileURL(
					`${RELEASE}/web/node_modules/dsh-social-runtime/recall-rules.mjs`,
				).href
			);
			const input = JSON.parse(
				readFileSync(join(process.env.SBX_EXTRA, 'gate-input.json'), 'utf8'),
			);
			const selection = ctx.get('agentDefaultModel')?.currentSelection?.();
			say({
				k: 'model',
				provider: selection?.provider,
				model: selection?.model,
			});
			const gate = rules.createRecallGate(ctx);
			const facts = (item) => ({
				speaker: '阿華',
				text: item.text,
				recent: item.recent.map((row) => ({
					at: Date.now() - row.min * 60000,
					who: row.who,
					text: row.text,
				})),
			});
			const rounds = Number(process.env.SBX_ROUNDS || 2);
			for (let round = 1; round <= rounds; round++)
				for (const item of input.cases) {
					if (rules.noContent(item.text)) {
						say({
							k: 'gate',
							id: item.id,
							round,
							recall: false,
							skipped: 'no-content',
							ms: 0,
						});
						continue;
					}
					const verdict = await gate(facts(item));
					say({
						k: 'gate',
						id: item.id,
						round,
						recall: verdict.recall,
						explicit: verdict.explicit,
						queries: verdict.queries,
						reason: verdict.reason,
						error: verdict.error,
						ms: verdict.ms,
						attempts: verdict.attempts,
						usage: verdict.usage,
					});
				}
			// 对照组：聊天模型自己决定（人设 + 行为规则 + chat_search），只看第一步。
			const go = selection?.provider === 'opencode-go';
			const system = `${input.persona}\n\n${rules.MEMORY_MANNERS}\n\n可以用 chat_search 翻以前的聊天紀錄（需要想起更早以前的事時才用）。`;
			// SBX_CHAT=0：只跑守门员（对照组跟守门员的提示词无关，改了提示词不用重跑）。
			const control =
				process.env.SBX_CHAT === '0'
					? []
					: input.cases.filter((row) => row.cat !== '題庫');
			for (const item of control) {
				const started = Date.now();
				let tool = false;
				let text = '';
				let last;
				let finish;
				let error;
				const controller = new globalThis.AbortController();
				const timer = setTimeout(() => controller.abort(), 40000);
				try {
					const recent = item.recent
						.map((row) => `[${row.min} 分鐘前] ${row.who}：${row.text}`)
						.join('\n');
					for await (const chunk of ctx.get('llm').stream({
						provider: selection?.provider ?? 'deepseek-account',
						model: selection?.model ?? 'deepseek-flash',
						...(go ? { sessionId: `session-${randomUUID()}` } : {}),
						// 这条路线不接受 off（会直接结束、没有字）；聊天会话实际送的思考深度由宿主决定，这里不指定。
						...(process.env.SBX_CHAT_EFFORT
							? { reasoningEffort: process.env.SBX_CHAT_EFFORT }
							: {}),
						maxTokens: 600,
						system,
						tools: [SEARCH],
						messages: [
							{
								role: 'user',
								content: [
									{
										type: 'text',
										text:
											(recent ? `（前面的對話）\n${recent}\n\n` : '') +
											`阿華：${item.text}`,
									},
								],
							},
						],
						signal: controller.signal,
					})) {
						if (
							chunk.type === 'tool-call-delta' &&
							(chunk.name ?? 'chat_search') === 'chat_search'
						)
							tool = true;
						if (chunk.type === 'text-delta') text += chunk.text;
						if (chunk.type === 'usage') last = chunk.usage;
						if (chunk.type === 'finish') finish = chunk.reason?.kind;
						if (tool) break;
					}
				} catch (thrown) {
					error = controller.signal.aborted ? 'timeout' : short(thrown);
				} finally {
					clearTimeout(timer);
				}
				say({
					k: 'chat',
					id: item.id,
					tool,
					ms: Date.now() - started,
					textChars: text.length,
					finish,
					error,
					usage: last
						? {
								input: last.inputTokens,
								output: last.outputTokens,
								reasoning: last.reasoningTokens,
								cacheRead: last.cacheReadTokens,
							}
						: undefined,
				});
			}
			say({ k: 'done' });
		} catch (error) {
			say({ k: 'fail', error: short(error) });
		}
	})();
}
