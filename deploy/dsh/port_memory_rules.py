"""把「翻旧事的规则」（小鲸鱼记忆第一阶段）移植到 VM 现行 release 的副本（不整档覆盖）。

现行 release 是 …-studio-1128c（已经套过 port_typing_fix.py）。这里只替换翻旧事、背景整理与 fish-memory 设定
需要的接点，其余原样保留；任何接点对不上就整个停下，不写任何档案；写到一半失败会把已写的还原（不留半套）。

    python deploy/dsh/port_memory_rules.py <release 副本根目录> [--repo <仓库根目录>] [--rev HEAD] [--worktree]

release 副本根目录底下要有 dsh-plugin-discord/lib/、dsh-social-runtime/ 与 dsh-fish-memory/（VM 上是
web/node_modules/ 底下那几个套件）。步骤与验收见 memory-rules-port.md。
"""

import argparse
import pathlib
import subprocess
import sys

import social_patch as sp
from port_typing_fix import git, write_all

once = sp.once

# ---- dsh-social-runtime/index.mjs ----
CRYPTO = ("import { createHash } from 'node:crypto';\n", "import { createHash, randomUUID } from 'node:crypto';\n")
IMPORTS = ("import { MemoryStore } from './memory.mjs';\n",
           "import { MemoryStore } from './memory.mjs';\n"
           "import { ChannelVisibility, FOUND_NOTE, MEMORY_MANNERS, NOT_FOUND_NOTE, inWindow, placeOf, recallPlan, withTopic } from './recall-rules.mjs';\n"
           "import { NoteBatches, failureKind } from './notes-batches.mjs';\n")

CTOR = ("\t\tthis.notes = new NotesStore(join(this.root, 'notes'));\n\t\tthis.notesQueue = Promise.resolve();\n",
        "\t\tthis.notes = new NotesStore(join(this.root, 'notes'));\n\t\tthis.notesQueue = Promise.resolve();\n"
        "\t\t// 背景整理的批次帐：失败的批次留在这里（哪一步、什么原因），之后有限次重试，不重复写入。\n"
        "\t\tthis.noteBatches = new NoteBatches(join(this.root, 'notes', 'batches'));\n"
        "\t\tthis.notesRetrying = new Set();\n"
        "\t\t// 频道看不看得到（@everyone 能不能检视）：决定别的频道的事能不能拿出来讲。还不知道的当成不公开。\n"
        "\t\tthis.visibility = new ChannelVisibility((path) => this.cachedFetch(path));\n"
        "\t\tthis.recallPlans = new Map();\n")

# 收到讯息时先把这个服务器的频道可见性载入（之后的同步判断只用快取）。
ADMIT_WARM = ("\t\tif (message.guild_id) this.lastGuildId = message.guild_id;\n",
              "\t\tif (message.guild_id) this.lastGuildId = message.guild_id;\n"
              "\t\tconst warming = this.visibility.warm(message.guild_id ?? this.lastGuildId);\n"
              "\t\tif (message.guild_id) void warming.then(() => this.visibility.warmChannel(message.channel_id));\n")

OLD_RELATED = r'''	// 每轮自动想起：用这句话从原文帐本捞几则相关的旧讯息（原话），同频道最近 3 小时的不捞。
	// 群聊只捞群聊的；私讯两边都捞。失败就不带，不影响聊天。
	relatedLines(agent, source) {
		if (!this.archive || !source?.text) return [];
		try {
			return this.archive.related(source.text, {
				scopes: this.isGroup(agent) ? ['group'] : ['group', 'dm'],
				normalize: (text) =>
					String(this.simplify?.(String(text)) ?? text).toLowerCase(),
				channel: source.channelId,
				recentSince: Date.now() - 3 * 3600000,
				excludeId: source.messageId,
				limit: 4,
				// 门槛是照正式帐本（几百则群聊讯息）实测定的。
				minScore: this.config.recallMinScore ?? 9,
			});
		} catch {
			return [];
		}
	}
'''

NEW_RELATED = r'''	// 这一轮在哪里说话（服务器、频道、是不是私讯、谁）。
	whereOf(source) {
		return source?.channelId
			? {
					guildId: source.guildId ?? null,
					channelId: source.channelId,
					dm: !source.guildId,
					authorId: source.authorId,
				}
			: undefined;
	}

	// 翻旧事的规则（recall-rules.mjs）：有线索才翻、先按范围过滤再搜寻、平常只翻 6 个月内（提到日期才更早）。
	// 同一则讯息只算一次：自动想起、长期记忆、人物卡、翻纪录的工具、闸道附上的记忆都用这一份。
	recallFor(source, now = Date.now()) {
		const where = this.whereOf(source);
		const none = {
			plan: { recall: false, explicit: false, clues: [], channels: new Set(), since: now, until: now },
			where,
			related: [],
			rows: new Map(),
			admit: () => null,
		};
		if (!where || !source?.messageId) return none;
		const cached = this.recallPlans.get(source.messageId);
		if (cached?.source === source && now - cached.at < 120000) return cached.value;
		let value = none;
		try {
			value = this.computeRecall(source, where, now);
		} catch {
			/* 算不出来就不翻（宁可不提）。 */
		}
		this.recallPlans.set(source.messageId, { source, at: now, value });
		if (this.recallPlans.size > 256)
			this.recallPlans.delete(this.recallPlans.keys().next().value);
		return value;
	}

	computeRecall(source, where, now) {
		const normalize = (text) =>
			String(this.simplify?.(String(text)) ?? text).toLowerCase();
		// 原文帐本一轮只读一次；时间以讯息本身为准。
		const rows = new Map();
		if (this.archive) for (const row of this.archive.load()) rows.set(row.id, row);
		// 提到的人：在这里（同一个服务器，私讯就是全部）说过话的群友与最近收到讯息的人（她自己、说话的人本人不算）。
		const humans = new Set();
		const names = new Set();
		const person = (id, name) => {
			if (!id || id === source.authorId) return;
			humans.add(id);
			if (name) names.add(name);
		};
		for (const entry of this.ingress.values()) person(entry.authorId, entry.authorName);
		for (const row of rows.values())
			if (!row.bot && (where.dm || row.guild_id === where.guildId)) person(row.author_id, row.author);
		let plan = recallPlan(source.text, {
			now,
			names: [...names],
			humans,
			channels: where.dm ? new Map() : this.visibility.names(where.guildId),
		});
		const isPublic = (id) => this.visibility.isPublic(id);
		// 这一则本身不算「以前的事」。
		const admitRow = (row, current) =>
			row && row.id !== source.messageId && inWindow(Date.parse(row.t), current)
				? placeOf(row, where, current, isPublic)
				: null;
		const search = (current) => {
			if (!this.archive || !source.text) return [];
			try {
				return this.archive.related(source.text, {
					scopes: where.dm ? ['group', 'dm'] : ['group'],
					normalize,
					// 眼前的对话（同一个地方 6 小时内）已经在聊天回放里，不当成「想起」。
					channel: source.channelId,
					recentSince: now - 6 * 3600000,
					excludeId: source.messageId,
					limit: 4,
					// 门槛是照正式帐本（几百则群聊讯息）实测定的。明确在问以前的事时，范围已经先缩到这里、6 个月内，
					// 只撞上一个少见的词（「上次那个包裹」的包裹）也要找得到，门槛放低。
					minScore: current.explicit
						? Math.min(this.config.recallMinScore ?? 9, 3)
						: (this.config.recallMinScore ?? 9),
					// 先按范围过滤再搜寻：别的频道、私讯、6 个月以前的根本不参加比对。
					filter: (row) => Boolean(admitRow(row, current)),
				});
			} catch {
				return [];
			}
		};
		let lines = search(plan);
		// 没有别的线索：范围内有强的命中，就当成提到了具体的东西或话题。
		plan = withTopic(plan, lines.length > 0);
		if (!plan.recall) lines = [];
		const label = (row) => this.placeLabel(row, where);
		return {
			plan,
			where,
			rows,
			related: lines.map((row) => ({ ...row, from: label(row) })),
			admit: (row) => (plan.recall ? admitRow(row, plan) : null),
			label,
		};
	}

	// 别的地方来的要说出来源：#频道名；同一个地方的不标。
	placeLabel(row, where) {
		const channel = String(row?.channel_id ?? row?.channelId ?? '');
		if (!channel || channel === where?.channelId) return '';
		if (row.scope === 'dm') return '私訊';
		const name =
			this.visibility.name(channel) ??
			this.guildOwners.get(`/channels/${channel}`)?.value?.name;
		return name ? '#' + name : '別的頻道';
	}

	// 一条记忆这一轮能不能带：私讯的只回到同一位拥有者的私讯；群聊的看它的出处（原文帐本那则讯息）在哪、什么时候。
	// 出处不明的旧条目不带（留待确认）。回传来源标签（同一个地方是空字串），不能带就回 null。
	recordPlace(record, recall) {
		const { where, plan } = recall;
		if (!where || !plan.recall) return null;
		if (record.scope !== GROUP_SCOPE)
			return where.dm &&
				record.scope === 'owner-' + where.authorId &&
				inWindow(Date.parse(record.at), plan)
				? ''
				: null;
		const row =
			recall.rows.get(String(record.messageId)) ??
			(record.channelId
				? {
						scope: 'group',
						channel_id: record.channelId,
						guild_id: record.guildId ?? null,
						t: record.at,
					}
				: undefined);
		const place = row ? recall.admit(row) : null;
		return place ? (place === 'here' ? '' : recall.label(row)) : null;
	}

	// 人物卡的一条：「（私下）」的只在私讯；其余看出处那则讯息（范围、时间）。没有出处的不带。
	noteLabel(line, recall) {
		if (!recall?.plan.recall) return null;
		if (this.notes.noteText(line).startsWith('（私下）')) {
			const day = Date.parse(
				(/^(\d{4}-\d{2}-\d{2}) /.exec(line)?.[1] ?? '') + 'T12:00:00+08:00',
			);
			return recall.where?.dm && inWindow(day, recall.plan) ? '' : null;
		}
		for (const id of this.notes.noteSources(line)) {
			const row = recall.rows.get(String(id));
			const place = row ? recall.admit(row) : null;
			if (place) return place === 'here' ? '' : recall.label(row);
		}
		return null;
	}

	// 翻纪录的工具用：这一轮的来源核对过（sourceForTurn）才有范围；群聊或拥有者私讯没有来源就什么都不翻。
	// 没绑定的私人会话（不是从 Discord 来的）照旧。
	recallScope(agent) {
		const source = this.sourceForTurn(agent);
		if (
			!source &&
			!this.isGroup(agent) &&
			!String(this.scopeFor(agent)).startsWith('owner-')
		)
			return undefined;
		return this.recallFor(source);
	}

	// 每轮自动想起：照翻旧事的规则（recallFor），只有有线索、而且范围内找得到的才带。失败就不带，不影响聊天。
	relatedLines(agent, source) {
		if (!this.archive || !source?.text) return [];
		return this.recallFor(source).related;
	}

	// 想起的东西带进提示时附的话：找到了怎么用；明确在问却什么都没找到，就说没找到、不要编。
	recallNote(recall, found) {
		if (!recall?.plan.recall) return '';
		if (found) return FOUND_NOTE;
		return recall.plan.explicit ? NOT_FOUND_NOTE : '';
	}
'''

OLD_PLACE = r'''		// 频道名只用已快取的（这段说明是同步产生的）；私讯的标成私下。
		const place = (row) => {
			if (row.scope === 'dm') return ' 私訊（私下）';
			const name = this.guildOwners.get(`/channels/${row.channel_id}`)?.value
				?.name;
			return name ? ' #' + name : '';
		};
'''
NEW_PLACE = r'''		// 来源标签在算范围时就标好了（recallFor 的 from）：别的频道的写出 #频道名，同一个地方的不标。
		const place = (row) => (row.from ? ` ${row.from}（在那裡聽到的）` : '');
'''

OLD_CONTEXT = r'''				const scope = this.scopeFor(agent);
				let memories = [];
				try {
					// 群聊与拥有者私讯是同一个她：两边的记忆一起找；没绑定来源的会话只找自己的。
					memories = this.memory.recallAcross(
						scope === GROUP_SCOPE || scope.startsWith('owner-')
							? this.memoryScopes(scope)
							: [scope],
						query,
					);
				} catch {
					/* 记忆损坏时不猜测，原档保留供维护。 */
				}
				const related = this.relatedLines(agent, source);
				if (this.isGroup(agent))
					return this.groupContext(
						source,
						memories,
						this.recentSpeakers(source),
						related,
					);
'''
NEW_CONTEXT = r'''				const scope = this.scopeFor(agent);
				// 翻旧事的规则：没有线索就不翻（长期记忆、人物卡、旧讯息都不带）；能翻的只有这一轮看得到的范围。
				// 群聊只找群聊的记忆（私讯的一律看不到）；拥有者私讯找自己的私讯与公开频道的。
				const bound =
					scope === GROUP_SCOPE || scope.startsWith('owner-');
				const recall = this.recallFor(
					source &&
						scope === (source.guildId ? GROUP_SCOPE : 'owner-' + source.authorId)
						? source
						: undefined,
				);
				let memories = [];
				try {
					if (!bound) memories = this.memory.recallAcross([scope], query);
					else if (recall.plan.recall)
						memories = this.memory.recallAcross(
							scope === GROUP_SCOPE ? [GROUP_SCOPE] : [scope, GROUP_SCOPE],
							query,
							Date.now(),
							(record) => this.recordPlace(record, recall),
						);
				} catch {
					/* 记忆损坏时不猜测，原档保留供维护。 */
				}
				const related = bound ? this.relatedLines(agent, recall.where ? source : undefined) : [];
				if (this.isGroup(agent))
					return this.groupContext(
						source,
						memories,
						recall.plan.recall ? this.recentSpeakers(source) : [],
						related,
						recall,
					);
'''

OLD_DM_MEMORIES = r'''						memories: memories.map(
							({ id, at, authorId, authorName, text, scope: from }) => ({
								id,
								at,
								authorId,
								authorName,
								where: from === GROUP_SCOPE ? '群聊' : '私讯',
								text,
							}),
						),
					}),
					this.relatedText(related),
'''
NEW_DM_MEMORIES = r'''						memories: memories.map(
							({ id, at, authorId, authorName, text, scope: kind, from }) => ({
								id,
								at,
								authorId,
								authorName,
								where: kind === GROUP_SCOPE ? '群聊' + (from ? ' ' + from : '') : '私讯',
								text,
							}),
						),
					}),
					this.relatedText(related),
					this.recallNote(recall, memories.length > 0 || related.length > 0),
'''
OLD_DM_LINE = "'这是拥有者的私人会话；记忆与群聊相通（where 标出是群聊还是私讯的事），私讯的事在群里只是知道、不会说出来。'"
NEW_DM_LINE = "'这是拥有者的私人会话；这里想得起公开频道的事（where 标出是哪个频道）；私讯的事只留在私讯，群里看不到。'"
OLD_ROLE_DM = "'当前是拥有者私人会话。你在群里经历的事这里也记得；这里聊的私事会标成「私下」，你在群里知道但不会提。对真实工具和记忆能力要如实说明。'"
NEW_ROLE_DM = "'当前是拥有者私人会话。你在公开频道经历的事这里也记得；这里聊的私事只留在这里，群里看不到也不会提。对真实工具和记忆能力要如实说明。'"

# 她的行为规则（keeper 运行规则里适合当规矩的几条）：群聊与私讯都带，画室不带。
MANNERS = ("\t\tagent.ctx.systemPrompt.section({\n\t\t\tname: 'social-role-boundary',",
           "\t\tagent.ctx.systemPrompt.section({\n"
           "\t\t\tname: 'social-memory-manners',\n"
           "\t\t\torder: 89993,\n"
           "\t\t\tinterpolate: false,\n"
           "\t\t\ttext: () => (this.studios.has(agent.id) ? '' : MEMORY_MANNERS),\n"
           "\t\t});\n"
           "\t\tagent.ctx.systemPrompt.section({\n\t\t\tname: 'social-role-boundary',")

OLD_GROUP_SIGNATURE = r'''	groupContext(source, memories, people = [], related = []) {
		const title = this.config.ownerTitle ?? '主人';
		let notes = '';
		try {
			notes = this.notes.context(people);
		} catch {
			/* 人物卡读不到就不带。 */
		}
'''
NEW_GROUP_SIGNATURE = r'''	groupContext(source, memories, people = [], related = [], recall) {
		const title = this.config.ownerTitle ?? '主人';
		let notes = '';
		let cards = 0;
		try {
			// 人物卡：只带这一轮看得到的条目（「私下」的不带、别的频道的标来源），见 noteLabel。
			notes = this.notes.context(
				people,
				recall
					? {
							admit: (line) => this.noteLabel(line, recall),
							shown: (count) => (cards = count),
						}
					: {},
			);
		} catch {
			/* 人物卡读不到就不带。 */
		}
'''
OLD_GROUP_MEMORIES = r'''					memories
						.map(
							({ at, authorName, text, scope }) =>
								`- ${this.memory.day(Date.parse(at)).slice(5)} ${authorName}${scope && scope !== GROUP_SCOPE ? '（私下跟你說的，群裡別提）' : ''}：${text}`,
						)
						.join('\n')
				: '',
			notes,
			'標了「私下」的是站長私訊時跟你說的事：你知道就好，在群裡不要說出來、不要暗示，有人問起就裝不知道或帶過。',
			this.relatedText(related),
'''
NEW_GROUP_MEMORIES = r'''					memories
						.map(
							({ at, authorName, text, from }) =>
								`- ${this.memory.day(Date.parse(at)).slice(5)} ${authorName}${from ? `（在 ${from} 聽到的）` : ''}：${text}`,
						)
						.join('\n')
				: '',
			notes,
			this.relatedText(related),
			this.recallNote(recall, memories.length > 0 || related.length > 0 || cards > 0),
'''

OLD_CONSOLIDATE_START = "\t// 背景整理：把一段对话交给模型挑出值得记的事，写进人物卡、日记、长期事实提议与召回记录。\n"
OLD_CONSOLIDATE_END = "\t// 服务器与频道的资料（群主、名称），快取 10 分钟。\n"
# 现行 release 的整理：思考深度 off（这条模型路线不接受，2026-11-25 起一直失败）、maxTokens 800、
# 一失败整批就丢、只留一行通用警告、整理出的人物卡新笔记都标成「事实」。整段换掉之前先确认是这一版。
OLD_CONSOLIDATE_MARKS = ("reasoningEffort: 'off',\n\t\t\t\tmaxTokens: 800,", "{ text: note, kind: 'fact', importance: 0.6 }",
                         "const task = this.notesQueue.then(run, run);")
NEW_CONSOLIDATE = r'''	// 背景整理：把一段对话交给模型挑出值得记的事，写进人物卡、日记、长期事实提议与召回记录。
	// 私讯（personal）整理出的东西标成「私下」：人物卡加前缀、召回记录存进拥有者范围，不提议长期事实。
	// 所有写入排在同一个队列。先把这批存进批次帐（notes-batches.mjs），成功才收掉；
	// 失败留下「在哪一步、什么原因」（原因只记类别），之后有限次重试；做过的步骤不再做，不重复写入。
	// 整理出的笔记都是「事件」（用到才找），不再标成常驻的「事实」。
	consolidate(lines, place = '', { private: personal = false } = {}) {
		let batch;
		try {
			batch = this.noteBatches.open(lines, place, personal, this.noteWhere(lines));
		} catch (error) {
			this.noteLog(undefined, 'save', { kind: failureKind(error), attempts: 1 });
			return Promise.reject(Object.assign(new Error('背景整理失败：save'), { stage: 'save' }));
		}
		if (!batch) return Promise.resolve({ skipped: true });
		const task = this.notesQueue.then(() => this.runNoteBatch(batch));
		this.notesQueue = task.catch(() => {}).then(() => this.retryNotes());
		return task;
	}

	// 失败过、到了重试时间的批次，排进同一个队列重跑（一批同时只跑一次）。
	retryNotes() {
		let due = [];
		try {
			due = this.noteBatches.due();
		} catch {
			return;
		}
		for (const { id } of due) {
			if (this.notesRetrying.has(id)) continue;
			this.notesRetrying.add(id);
			const task = this.notesQueue.then(() => {
				const batch = this.noteBatches.read(id);
				return batch ? this.runNoteBatch(batch) : undefined;
			});
			this.notesQueue = task
				.catch(() => {})
				.finally(() => this.notesRetrying.delete(id));
		}
	}

	// 失败只记批次编号、范围、哪一步、原因类别与第几次，不记任何内文。
	noteLog(batch, stage, failure) {
		const where = batch ? (batch.personal ? '私讯' : '群聊') : '?';
		this.ctx.logger?.warn?.(
			`背景整理失败 批次 ${batch?.id ?? '-'}（${where}）：${stage} 这一步，原因 ${failure.kind}，第 ${failure.attempts ?? 1} 次` +
				(failure.gaveUp ? '，不再重试（批次留在 notes/batches/failed）' : '，稍后重试'),
		);
	}

	// 这批讯息在哪个频道（重试前核对删除要用）。
	noteWhere(lines) {
		const ids = new Set(lines.map((line) => String(line.id ?? '')));
		try {
			const row = this.archive?.load().find((entry) => ids.has(entry.id));
			return row ? { channelId: row.channel_id, guildId: row.guild_id ?? null } : undefined;
		} catch {
			return undefined;
		}
	}

	// 重试前再看一次原文帐本：删掉的讯息不整理，改过的用改过的字。回传 { lines, gone }。
	reviseNoteLines(batch) {
		const ids = batch.lines
			.map((line) => String(line.id ?? ''))
			.filter((id) => /^\d{5,25}$/.test(id));
		if (!this.archive || !ids.length) return { lines: batch.lines, gone: new Set() };
		const gone = batch.where
			? this.archive.deletedMessageIds(
					ids.map((id) => ({ id, channelId: batch.where.channelId, guildId: batch.where.guildId })),
				)
			: new Set();
		const wanted = new Set(ids);
		const current = new Map(
			this.archive
				.load()
				.filter((row) => wanted.has(row.id))
				.map((row) => [row.id, row]),
		);
		const lines = batch.lines.flatMap((line) => {
			const id = String(line.id ?? '');
			if (gone.has(id)) return [];
			const row = current.get(id);
			return [row?.edited ? { ...line, text: row.content } : line];
		});
		return { lines, gone };
	}

	async consolidationText(lines, place, speakers) {
		// 每行加行号；模型只回行号，这里再换成真的讯息 id，出处编不出来。
		const transcript = lines
			.map(
				(line, index) =>
					`[${index + 1}] ` +
					(line.role === 'assistant'
						? `你：${line.text}`
						: `${line.name}（id:${line.speaker}）：${line.text}`),
			)
			.join('\n');
		const cards = this.notes.context([...speakers.keys()].map((id) => ({ id })));
		const selection = (() => {
			try {
				return this.ctx.get('agentDefaultModel')?.currentSelection?.();
			} catch {
				return undefined;
			}
		})();
		let text = '';
		let finish;
		// 正式的模型路线（opencode-go 的 deepseek-v4.1-flash）不接受思考深度 off（2026-11-25 起整理每批都失败：
		// UNSUPPORTED_REASONING_EFFORT），也要每次带会话编号（不然 400 MissingSessionID）；跟画室的守门员一样用 low、
		// 另给一个编号。回传的 JSON 给足 2000。
		const go = selection?.provider === 'opencode-go';
		for await (const chunk of this.ctx.get('llm').stream({
			provider: selection?.provider ?? 'deepseek-account',
			model: selection?.model ?? 'deepseek-flash',
			...(go ? { sessionId: `session-${randomUUID()}` } : {}),
			reasoningEffort: go ? 'low' : 'off',
			maxTokens: 2000,
			system: CONSOLIDATE_SYSTEM,
			messages: [
				{
					role: 'user',
					content: [
						{
							type: 'text',
							text:
								(place ? `地點：${place}\n` : '') +
								(cards ? `現有記憶：\n${cards}\n\n` : '') +
								`對話：\n${transcript}`,
						},
					],
				},
			],
		})) {
			if (chunk.type === 'text-delta') text += chunk.text;
			if (chunk.type === 'finish') finish = chunk.reason;
		}
		if (finish?.kind === 'error')
			throw Object.assign(new Error('model'), { kind: 'model-error' });
		if (!text.trim())
			throw Object.assign(new Error('model'), {
				kind: finish?.kind === 'length' ? 'no-text-length' : 'no-text',
			});
		return { text, cut: finish?.kind === 'length' };
	}

	async runNoteBatch(batch) {
		let stage = 'prepare';
		try {
			const personal = batch.personal;
			// 重试前再核对删改（第一次是刚收到的，不用）。
			const { lines, gone } = batch.attempts
				? this.reviseNoteLines(batch)
				: { lines: batch.lines, gone: new Set() };
			const speakers = new Map(
				lines
					.filter((line) => line.role === 'user' && line.speaker)
					.map((line) => [String(line.speaker), line.name]),
			);
			if (!speakers.size) {
				this.noteBatches.finish(batch);
				return { skipped: true };
			}
			const sourceIds = (numbers) => [
				...new Set(
					numbers
						.map((number) => String(lines[number - 1]?.id ?? ''))
						.filter((id) => /^\d{5,25}$/.test(id)),
				),
			];
			let result = batch.step.result;
			if (!result) {
				stage = 'model';
				const { text, cut } = await this.consolidationText(lines, batch.place, speakers);
				stage = 'parse';
				let parsed;
				try {
					parsed = parseConsolidation(text, new Set(speakers.keys()));
				} catch (error) {
					throw Object.assign(error, { kind: cut ? 'cut-json' : 'bad-json' });
				}
				result = {
					people: parsed.people.map((person) => ({
						...person,
						notes: person.notes.map((note) => ({ ...note, src: sourceIds(note.src) })),
					})),
					journal: parsed.journal.map((entry) => ({ ...entry, src: sourceIds(entry.src) })),
					facts: parsed.facts.map((entry) => ({ ...entry, src: sourceIds(entry.src) })),
				};
				stage = 'save';
				this.noteBatches.mark(batch, { result });
			}
			// 重试时出处全被删掉的条目不写（有出处的才算）。
			const alive = (entry) => !entry.src?.length || entry.src.some((id) => !gone.has(id));
			stage = 'write';
			const now = Date.now();
			const owner = [...speakers.keys()].find((id) => this.ownerIds.includes(id));
			const scope = personal && owner ? 'owner-' + owner : GROUP_SCOPE;
			const wanted = new Set(
				result.people.flatMap((person) => person.notes.flatMap((note) => note.src ?? [])),
			);
			const rows = new Map(
				wanted.size && this.archive
					? this.archive
							.load()
							.filter((row) => wanted.has(row.id))
							.map((row) => [row.id, row])
					: [],
			);
			const done = new Set(batch.step.people ?? []);
			let saved = 0;
			for (const person of result.people) {
				if (done.has(person.id)) continue;
				const notes = (
					personal
						? person.notes.map((note) => ({ ...note, text: '（私下）' + note.text }))
						: person.notes
				).filter(alive);
				const name = person.name || speakers.get(person.id);
				const added = this.notes.updateCard(person.id, name, notes, now);
				// 召回记录的来源用这条的第一个出处（原文帐本的讯息 id），带上它的频道。
				const sourceOf = (text) =>
					notes.find((note) => String(note.text).replace(/\s+/g, ' ').trim() === text)
						?.src?.[0];
				for (const note of added)
					try {
						const src = sourceOf(note);
						const row = src ? rows.get(src) : undefined;
						this.memory.remember(
							scope,
							{
								authorId: person.id,
								authorName: name,
								messageId: src ?? 'consolidated',
								...(row ? { channelId: row.channel_id, guildId: row.guild_id ?? undefined } : {}),
							},
							{ text: note, kind: 'event', importance: 0.6 },
							now,
						);
					} catch {
						/* 召回记录写不进去不影响人物卡。 */
					}
				saved += added.length;
				done.add(person.id);
				stage = 'save';
				this.noteBatches.mark(batch, { people: [...done] });
				stage = 'write';
			}
			const journal = result.journal.filter(alive);
			if (!batch.step.journal) {
				this.notes.addJournal(journal, personal ? '私訊（私下）' : batch.place, now);
				stage = 'save';
				this.noteBatches.mark(batch, { journal: true });
				stage = 'write';
			}
			const facts = personal ? [] : result.facts.filter(alive);
			if (!batch.step.facts) {
				for (const fact of facts)
					this.notes.addFact(fact.text, { pending: true, now, src: fact.src });
				stage = 'save';
				this.noteBatches.mark(batch, { facts: true });
			}
			stage = 'finish';
			this.noteBatches.finish(batch);
			return { people: saved, journal: journal.length, facts: facts.length };
		} catch (error) {
			let failure;
			try {
				failure = this.noteBatches.fail(batch, stage, error);
			} catch {
				failure = { kind: failureKind(error), attempts: (batch.attempts ?? 0) + 1 };
			}
			this.noteLog(batch, stage, failure);
			throw Object.assign(new Error(`背景整理失败：${stage}（${failure.kind}）`), {
				stage,
				kind: failure.kind,
			});
		}
	}

'''

# ---- dsh-social-runtime/memory.mjs：整理出的笔记都当「事件」（用到才找），只有标了 pinned 的才常驻；
# 召回先按范围过滤（place 回传来源标签，null 就不参加）。
MEMORY_SIGNATURE = ("\trecallAcross(scopes, query, now = Date.now()) {\n\t\tconst records = scopes.flatMap((scope) =>\n\t\t\tthis.active(scope).map((record) => ({ ...record, scope })),\n\t\t);\n",
                    "\trecallAcross(scopes, query, now = Date.now(), place) {\n\t\tconst records = scopes\n\t\t\t.flatMap((scope) => this.active(scope).map((record) => ({ ...record, scope })))\n"
                    "\t\t\t// 先按范围过滤再找：place 回传来源标签（同一个地方是空字串），null 就不参加。\n"
                    "\t\t\t.flatMap((record) => {\n\t\t\t\tif (!place) return [record];\n\t\t\t\tconst from = place(record);\n"
                    "\t\t\t\treturn from === null || from === undefined ? [] : [{ ...record, from }];\n\t\t\t});\n")
MEMORY_CATEGORY = ("\t\t\tcategory: record.kind === 'fact' ? 'stable_profile' : 'event',\n",
                   "\t\t\t// 只有站长确认、可以公开的少量设定（pinned）才常驻；一般整理笔记（含旧的「事实」）都是事件，用到才找。\n"
                   "\t\t\tcategory: record.pinned === true ? 'stable_profile' : 'event',\n")

# ---- dsh-social-runtime/notes.mjs：人物卡只带这一轮看得到的条目 ----
OLD_NOTES_CONTEXT = r'''	context(people) {
		const parts = [];
'''
NEW_NOTES_CONTEXT = r'''	// admit(line)：这一条能不能带（回传来源标签，同一个地方是空字串；null 就不带）；没给就照旧全带。
	context(people, { admit, shown } = {}) {
		const parts = [];
		const lines = (card) =>
			admit
				? card.notes.flatMap((line) => {
						const from = admit(line);
						return from === null || from === undefined
							? []
							: ['- ' + this.cite(line) + (from ? `（在 ${from} 聽到的）` : '')];
					})
				: card.notes.map((line) => '- ' + this.cite(line));
'''
OLD_NOTES_CARDS = r'''		const cards = people
			.filter(({ id }) => ID.test(String(id)))
			.map(({ id }) => ({ id, ...this.card(id) }))
			.filter((card) => card.notes.length)
			.slice(0, 4)
			.map(
				(card) =>
					`【${card.meta.names?.at(-1) ?? card.id}】（曾用名：${(card.meta.names ?? []).join('、')}）\n` +
					card.notes
						.slice(-12)
						.map((line) => '- ' + this.cite(line))
						.join('\n'),
			);
'''
NEW_NOTES_CARDS = r'''		const cards = people
			.filter(({ id }) => ID.test(String(id)))
			.map(({ id }) => ({ id, ...this.card(id) }))
			.map((card) => ({ ...card, lines: lines(card) }))
			.filter((card) => card.lines.length)
			.slice(0, 4)
			.map(
				(card) =>
					`【${card.meta.names?.at(-1) ?? card.id}】（曾用名：${(card.meta.names ?? []).join('、')}）\n` +
					card.lines.slice(-12).join('\n'),
			);
		shown?.(cards.length);
'''

# ---- dsh-social-runtime/archive.mjs：搜寻之前先按范围过滤（filter），连「多常见」的权重也只算范围内的 ----
ARCHIVE_RELATED_OPT = ("\t\t\tminScore = 9,\n\t\t\twithScore = false,\n\t\t} = {},\n\t) {",
                       "\t\t\tminScore = 9,\n\t\t\twithScore = false,\n\t\t\tfilter,\n\t\t} = {},\n\t) {")
ARCHIVE_RELATED_ROWS = ("\t\t\t.filter((row) => scopes.includes(row.scope) && !row.bot)\n",
                        "\t\t\t.filter((row) => scopes.includes(row.scope) && !row.bot && (!filter || filter(row)))\n")
ARCHIVE_SEARCH_OPT = ("\t\tlimit = 20,\n\t\tnormalize = (text) => String(text).toLowerCase(),\n\t} = {}) {\n\t\tconst words",
                      "\t\tlimit = 20,\n\t\tnormalize = (text) => String(text).toLowerCase(),\n\t\tfilter,\n\t} = {}) {\n\t\tconst words")
ARCHIVE_SEARCH_ROWS = ("\t\t\t\tscopes.includes(row.scope) &&\n\t\t\t\t(!channel || row.channel_id === channel) &&\n",
                       "\t\t\t\tscopes.includes(row.scope) &&\n\t\t\t\t(!filter || filter(row)) &&\n\t\t\t\t(!channel || row.channel_id === channel) &&\n")
ARCHIVE_AROUND = (r'''	around(id, { before = 5, after = 5, scopes = ['group'] } = {}) {
		const all = this.load();
		const target = all.find((row) => row.id === id);
		if (!target || !scopes.includes(target.scope)) return [];
		const same = all.filter((row) => row.channel_id === target.channel_id);
''', r'''	around(id, { before = 5, after = 5, scopes = ['group'], filter } = {}) {
		const all = this.load();
		const target = all.find((row) => row.id === id);
		if (!target || !scopes.includes(target.scope) || (filter && !filter(target)))
			return [];
		const same = all.filter(
			(row) => row.channel_id === target.channel_id && (!filter || filter(row)),
		);
''')

# ---- dsh-social-runtime/chat-tools.mjs：翻纪录的工具照同一套规则 ----
TOOLS_IMPORT = ("const id = { type: 'string', pattern: '^\\\\d{5,25}$' };\n",
                "import { NO_CLUE_TOOL } from './recall-rules.mjs';\n\nconst id = { type: 'string', pattern: '^\\\\d{5,25}$' };\n")
OLD_TOOLS_EXECUTE = r'''			try {
				return { ok: true, message: await run(args, scopes(exec.agent)) };
			} catch (error) {
'''
NEW_TOOLS_EXECUTE = r'''			// 翻旧事的规则（recall-rules.mjs）：这一轮没有线索就不翻；能翻的只有这一轮看得到的范围、平常 6 个月内。
			const recall = runtime.recallScope?.(exec.agent);
			if (recall && !recall.plan.recall) return { ok: true, message: NO_CLUE_TOOL };
			const filter = recall ? (row) => Boolean(recall.admit(row)) : undefined;
			try {
				return { ok: true, message: await run(args, scopes(exec.agent), filter) };
			} catch (error) {
'''
OLD_TOOLS_SEARCH = r'''			async ({ query, author, channel_id, since, until, limit = 20 }, from) => {
				if (!query && !author && !channel_id && !since)
					throw new Error('至少要給關鍵詞、人、頻道或日期其中一個。');
				const rows = runtime.archive.search({
					query,
					author,
					channel: channel_id,
					since: edge(since, false),
					until: edge(until, true),
					scopes: from,
					limit,
					normalize,
				});
				return rows.length ? await render(rows) : '沒找到符合的聊天紀錄。';
			},
'''
NEW_TOOLS_SEARCH = r'''			async ({ query, author, channel_id, since, until, limit = 20 }, from, filter) => {
				if (!query && !author && !channel_id && !since)
					throw new Error('至少要給關鍵詞、人、頻道或日期其中一個。');
				const rows = runtime.archive.search({
					query,
					author,
					channel: channel_id,
					since: edge(since, false),
					until: edge(until, true),
					scopes: from,
					limit,
					normalize,
					filter,
				});
				return rows.length
					? await render(rows)
					: filter
						? '沒找到符合的聊天紀錄（找的是你這裡能翻的範圍）：就照實說沒找到，不要編。'
						: '沒找到符合的聊天紀錄。';
			},
'''
OLD_TOOLS_OPEN = r'''			async ({ message_id, before = 5, after = 5 }, from) => {
				const rows = runtime.archive.around(message_id, {
					before,
					after,
					scopes: from,
				});
'''
NEW_TOOLS_OPEN = r'''			async ({ message_id, before = 5, after = 5 }, from, filter) => {
				const rows = runtime.archive.around(message_id, {
					before,
					after,
					scopes: from,
					filter,
				});
'''

# ---- dsh-plugin-discord/lib/gateway-scopes.mjs：闸道附上的「本频道可核对的记忆」也照同一套规则 ----
OLD_SCOPES_MEMORIES = r'''		try {
			const rows=runtime.memory.active(scope).filter(row=>row.channelId===target.channelId &&
				(row.guildId??null)===target.guildId && id(row.messageId)).slice(-6);
'''
NEW_SCOPES_MEMORIES = r'''		// 翻旧事的规则（dsh-social-runtime/recall-rules.mjs）：这一轮没有线索就不附；平常只附 6 个月内的。
		const live=runtime.turns?.get(target.sessionId);
		const recall=typeof runtime.recallFor==='function' ? runtime.recallFor(live?.channelId===target.channelId ? live : undefined) : null;
		if (recall && !recall.plan.recall) return [];
		try {
			const rows=runtime.memory.active(scope).filter(row=>row.channelId===target.channelId &&
				(row.guildId??null)===target.guildId && id(row.messageId) &&
				(!recall || recall.plan.since<=Date.parse(row.at) && Date.parse(row.at)<=recall.plan.until)).slice(-6);
'''

# ---- dsh-plugin-discord/lib/natural-participation.mjs：失败的批次现在会留着重试 ----
NP_WARN = (".catch(() => this.warn('背景整理记忆失败，这批先略过。'));",
           ".catch(() => this.warn('背景整理记忆失败，这批留着稍后重试（哪一步、原因见上一行）。'));")

# ---- dsh-fish-memory/lib/index.js：设定档不存在就用预设值，只提示一次；损坏或没有权限才报警 ----
OLD_FISH_CFG = r'''function loadCfg() {
  try {
    const raw = JSON.parse(读配置文本())
    return raw && typeof raw === 'object' ? raw : {}
  } catch (error) {
    try { diag('配置文件读不了（可能是 BOM / 写坏了）：' + String((error && error.message) || error)) } catch (error2) {}
    return {}
  }
}
'''
NEW_FISH_CFG = r'''// 设定档本来就是可选的：不存在就用预设值，这个程序只提示一次（以前每一步都写一行 ENOENT）。
// 档案损坏、没有权限这类真的读不了，才每次报警。
let 提示过没有配置 = false
function loadCfg() {
  try {
    const raw = JSON.parse(读配置文本())
    return raw && typeof raw === 'object' ? raw : {}
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      if (!提示过没有配置) {
        提示过没有配置 = true
        try { diag('没有配置文件，用默认值（这个提示只出现一次）') } catch (error2) {}
      }
      return {}
    }
    try { diag('配置文件读不了（可能是 BOM / 写坏了 / 没有权限）：' + String((error && error.code) || (error && error.name) || 'error')) } catch (error2) {}
    return {}
  }
}
'''


def port_runtime(content):
    if "recall-rules.mjs" in content:
        raise ValueError("这份 release 的 index.mjs 已经移植过翻旧事的规则。")
    for kept in ("drawRequestTool", "this.studios.has(agent.id)"):
        if kept not in content:
            raise ValueError("这份 release 的 index.mjs 还没套画室修正（port_typing_fix.py），停止。")
    start = content.index(OLD_CONSOLIDATE_START)
    end = content.index(OLD_CONSOLIDATE_END, start)
    body = content[start:end]
    if not all(mark in body for mark in OLD_CONSOLIDATE_MARKS):
        raise ValueError("现行 release 的 consolidate 和预期不同，须重新审阅。")
    content = content[:start] + NEW_CONSOLIDATE + content[end:]
    for anchor, replacement in (CRYPTO, IMPORTS, CTOR, ADMIT_WARM, (OLD_RELATED, NEW_RELATED), (OLD_PLACE, NEW_PLACE),
                                (OLD_CONTEXT, NEW_CONTEXT), (OLD_DM_MEMORIES, NEW_DM_MEMORIES), (OLD_DM_LINE, NEW_DM_LINE),
                                (OLD_ROLE_DM, NEW_ROLE_DM), MANNERS, (OLD_GROUP_SIGNATURE, NEW_GROUP_SIGNATURE),
                                (OLD_GROUP_MEMORIES, NEW_GROUP_MEMORIES)):
        content = once(content, anchor, replacement)
    for gone in ("reasoningEffort: 'off',\n\t\t\t\tmaxTokens: 800,", "kind: 'fact', importance: 0.6", "（私下跟你說的，群裡別提）"):
        if gone in content:
            raise ValueError("旧的整理设定或私下标记还在，停止。")
    return content


def port_memory(content):
    if "record.pinned === true" in content:
        raise ValueError("这份 release 的 memory.mjs 已经移植过。")
    return once(once(content, *MEMORY_SIGNATURE), *MEMORY_CATEGORY)


def port_notes(content):
    if "context(people, { admit" in content:
        raise ValueError("这份 release 的 notes.mjs 已经移植过。")
    return once(once(content, OLD_NOTES_CONTEXT, NEW_NOTES_CONTEXT), OLD_NOTES_CARDS, NEW_NOTES_CARDS)


def port_archive(content):
    if "(!filter || filter(row))" in content:
        raise ValueError("这份 release 的 archive.mjs 已经移植过。")
    for anchor, replacement in (ARCHIVE_RELATED_OPT, ARCHIVE_RELATED_ROWS, ARCHIVE_SEARCH_OPT, ARCHIVE_SEARCH_ROWS, ARCHIVE_AROUND):
        content = once(content, anchor, replacement)
    return content


def port_tools(content):
    if "NO_CLUE_TOOL" in content:
        raise ValueError("这份 release 的 chat-tools.mjs 已经移植过。")
    for anchor, replacement in (TOOLS_IMPORT, (OLD_TOOLS_EXECUTE, NEW_TOOLS_EXECUTE), (OLD_TOOLS_SEARCH, NEW_TOOLS_SEARCH),
                                (OLD_TOOLS_OPEN, NEW_TOOLS_OPEN)):
        content = once(content, anchor, replacement)
    return content


def port_scopes(content):
    if "recall-rules.mjs" in content:
        raise ValueError("这份 release 的 gateway-scopes.mjs 已经移植过翻旧事的规则。")
    return once(content, OLD_SCOPES_MEMORIES, NEW_SCOPES_MEMORIES)


def port_participation(content):
    if "这批留着稍后重试" in content:
        raise ValueError("这份 release 的 natural-participation.mjs 已经移植过。")
    return once(content, *NP_WARN)


def port_fish(content):
    if "提示过没有配置" in content:
        raise ValueError("这份 release 的 dsh-fish-memory 已经移植过。")
    return once(content, OLD_FISH_CFG, NEW_FISH_CFG)


# 移植后的档案（相对 release 副本根目录）；新增的两个来自仓库。
PATCHED = ("dsh-social-runtime/index.mjs", "dsh-social-runtime/memory.mjs", "dsh-social-runtime/notes.mjs",
           "dsh-social-runtime/archive.mjs", "dsh-social-runtime/chat-tools.mjs", "dsh-plugin-discord/lib/gateway-scopes.mjs",
           "dsh-plugin-discord/lib/natural-participation.mjs", "dsh-fish-memory/lib/index.js")
ADDED = ("dsh-social-runtime/recall-rules.mjs", "dsh-social-runtime/notes-batches.mjs")


def main(argv=None, replace=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("release", type=pathlib.Path)
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    parser.add_argument("--rev", default="HEAD", help="要移植的提交（预设 HEAD）")
    parser.add_argument("--worktree", action="store_true", help="用工作目录里还没提交的版本（只供本机检查）")
    args = parser.parse_args(argv)
    read = lambda name: (args.release / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    source = lambda name: (args.repo / name).read_bytes() if args.worktree else git(args.repo, "show", f"{args.rev}:{name}")
    ports = (port_runtime, port_memory, port_notes, port_archive, port_tools, port_scopes, port_participation, port_fish)
    files = [(args.release / name, port(read(name)).encode("utf-8")) for name, port in zip(PATCHED, ports)]
    files += [(args.release / name, source("deploy/social/" + name.split("/")[-1])) for name in ADDED]
    write_all(files, **({"replace": replace} if replace else {}))
    print("已移植：" + "、".join(name.split("/")[-1] for name in PATCHED) + "，新增 " + "、".join(name.split("/")[-1] for name in ADDED))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print("移植中止，没有写入任何档案：" + str(error), file=sys.stderr)
        sys.exit(1)
