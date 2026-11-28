"""把「正在输入不停」的修正移植到 VM 现行 release 的副本（不整档覆盖）。

现行 release（social64-trust-krita53-books-cef077-discord-bounded-1126）有仓库里没有的补丁：
gatewayScopes、history-view、drain、承诺、Krita 对帐。这里只替换本次修正需要的接点，其余原样保留；
任何接点对不上就整个停下，不写任何档案。

    python deploy/dsh/port_typing_fix.py <release 副本根目录> [--repo <仓库根目录>] [--base a86bd2c]

release 副本根目录底下要有 dsh-plugin-discord/lib/ 与 dsh-social-runtime/（VM 上是
web/node_modules/ 底下那两个套件）。步骤与验收见 typing-hang-port.md。
"""

import argparse
import pathlib
import subprocess
import sys

import social_patch as sp

BASE = "a86bd2c"  # 现行 release 的 natural-participation.mjs 与 bridge.mjs 是从这一版改出来的

# 现行 release 的 runPrompt 有两段等待：whenIdle 之后再 settleSocialTurn；两段要一起换掉，
# 只换 whenIdle 时，已经正常回话的回合还会被旧的 settle 卡住（第 1 轮审查）。
RELEASE_WAIT = "await agent.whenIdle();\n\t\t\tawait settleSocialTurn(agent);"
# 现行 release 才有的：承诺追问那一句也是这一轮的答覆。
COMMITMENT_REPLY = ("participation.recordReply(message.channel_id, text, sent.id, message.author.id, message.id);",
                    "participation.recordReply(message.channel_id, text, sent.id, message.author.id, message.id, true, naturalTicket);")
PARTICIPATION_LOG = ("  warn: (text) => logger.warn(text),\n  guildOwner:",
                     "  warn: (text) => logger.warn(text),\n  log: (text) => logger.info(text),\n  guildOwner:")
SUBSCRIBE_ANCHOR = "async function hydrateSameChannelReply(message) {"
OLD_MESSAGE_FAILED = '.catch(() => logger.warn("群聊行为处理未完成，保持安静。"))'
OLD_EDIT_FAILED = '.catch(() => logger.warn("编辑讯息处理未完成，保持安静。"))'

BRIDGE_IMPORT = ("import { currentRuntime } from './index.mjs';",
                 "import { currentRuntime } from './index.mjs';\nimport { foldSocialReply } from './turn-guard.mjs';")
BRIDGE_EXTRACT_START = "export function extractSocialReply(events, promptSeq) {"
BRIDGE_EXTRACT_KEEP = "\tconst messageId = sourceOf(events.find((event) => event.seq === promptSeq))?.discordMessageId;"
BRIDGE_EXTRACT_NEW = BRIDGE_EXTRACT_START + "\n\tconst { text, reasonKind, errorMessage } = foldSocialReply(events, promptSeq);\n"
BRIDGE_EXPORT = "\n// 一轮的期限、取消、归帐与取回答（typing-hang 修正）。\nexport * from './turn-guard.mjs';\n"


def port_index(content):
    once = sp.once
    if "runSocialTurn" in content:
        raise ValueError("这份 release 已经移植过。")
    content = sp.TURN_IMPORT + content
    for anchor, replacement in sp.LIFECYCLE:
        content = once(content, anchor, replacement)
    content = once(content, sp.FOLLOWUP[0], sp.FOLLOWUP[1])
    content = once(content, RELEASE_WAIT, sp.WAIT)
    for anchor, replacement in (sp.NOTICE_TICKET, sp.RECORD_TICKET, COMMITMENT_REPLY, sp.TRACKED_REST, PARTICIPATION_LOG):
        content = once(content, anchor, replacement)
    content = once(content, SUBSCRIBE_ANCHOR, sp.SUBSCRIBE + SUBSCRIBE_ANCHOR)
    content = once(content, OLD_MESSAGE_FAILED, sp.MESSAGE_FAILED)
    content = once(content, OLD_EDIT_FAILED, sp.EDIT_FAILED)
    # 保留现行 release 自己的补丁（gatewayScopes、history-view、drain、承诺、Krita）。
    for kept in ("this.gatewayScopes.turn(agent,channelId,messageId)", "historyCanDeliver(agent,messageId)",
                 "historyView.note(", "dsh.maintenance-drain.root.v1", "parseCommitmentReply(chunk)",
                 "await prepareSocialTurn(agent, channelId, messageId);"):
        if kept not in content:
            raise ValueError("移植后少了现行 release 的补丁，停止。")
    for gone in ("await agent.whenIdle();", "await settleSocialTurn(agent);", "findPromptSeq(events, messageId)",
                 "String(error)}`);\n\t\t\t\t\t\tbreak;"):
        if gone in content:
            raise ValueError("旧的等待或日志还在，停止。")
    return content


def port_bridge(content, develop_bridge):
    if "turn-guard.mjs" in content:
        raise ValueError("这份 release 的 bridge.mjs 已经移植过。")
    content = sp.once(content, *BRIDGE_IMPORT)
    start = content.index(BRIDGE_EXTRACT_START)
    end = content.index(BRIDGE_EXTRACT_KEEP, start)
    # 要换掉的那段必须和 develop 的 extractSocialReply 本体一样（只有结尾 Krita 对帐那行不同）。
    develop_start = develop_bridge.index(BRIDGE_EXTRACT_START)
    develop_end = develop_bridge.index("\treturn { text, reasonKind, errorMessage };", develop_start)
    if content[start:end] != develop_bridge[develop_start:develop_end]:
        raise ValueError("现行 release 的 extractSocialReply 和 develop 不同，须重新审阅。")
    content = content[:start] + BRIDGE_EXTRACT_NEW + content[end:]
    return content.rstrip("\n") + "\n" + BRIDGE_EXPORT


def apply_hunks(content, patch):
    """逐段套用 unified diff：每段（上下文加删掉的行）必须在档案里刚好出现一次，否则停止。

    现行 release 在 develop 之外改过几行（承诺），碰到的那几段会少带一点上下文再找，
    但仍然要刚好出现一次才套。
    """
    hunks = []
    for line in patch.split("\n"):
        if line.startswith("@@"):
            hunks.append([])
        elif hunks and line[:1] in (" ", "-", "+"):
            hunks[-1].append((line[:1], line[1:]))
    if not hunks:
        raise ValueError("没有要套用的改动。")
    content = "\n" + content
    for hunk in hunks:
        lead = next(i for i, (tag, _) in enumerate(hunk) if tag != " ")
        trail = next(i for i, (tag, _) in enumerate(reversed(hunk)) if tag != " ")
        trims = sorted(((x, y) for x in range(lead + 1) for y in range(trail + 1)), key=sum)
        for x, y in trims:
            part = hunk[x:len(hunk) - y]
            # 至少留两行上下文（或有要删的行），免得只凭一行巧合对上；前后都对齐到整行。
            if sum(tag == " " for tag, _ in part) < 2 and all(tag != "-" for tag, _ in part):
                continue
            before = "\n" + "\n".join(text for tag, text in part if tag != "+") + "\n"
            after = "\n" + "\n".join(text for tag, text in part if tag != "-") + "\n"
            # 相邻两段会共用换行，str.count 不算重叠的，这里逐一找。
            first = content.find(before)
            if first >= 0 and content.find(before, first + 1) < 0:
                content = content.replace(before, after, 1)
                break
        else:
            raise ValueError("natural-participation.mjs 有一段改动在现行 release 里对不上：" + hunk[lead][1].strip()[:60])
    return content[1:]


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, check=True).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("release", type=pathlib.Path)
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--rev", default="HEAD", help="要移植的提交（预设 HEAD）")
    parser.add_argument("--worktree", action="store_true", help="用工作目录里还没提交的版本（只供本机检查）")
    args = parser.parse_args(argv)
    plugin = args.release / "dsh-plugin-discord/lib"
    runtime = args.release / "dsh-social-runtime"
    index_path, np_path, bridge_path = plugin / "index.js", plugin / "natural-participation.mjs", runtime / "bridge.mjs"
    read = lambda path: path.read_text(encoding="utf-8").replace("\r\n", "\n")
    index = port_index(read(index_path))
    develop_bridge = git(args.repo, "show", f"{args.base}:deploy/social/bridge.mjs").decode("utf-8")
    bridge = port_bridge(read(bridge_path), develop_bridge)
    # natural-participation.mjs：现行 release = develop + 承诺的几行，把本分支对 develop 的改动逐段套上去
    # （每段的上下文都要在现行 release 里刚好出现一次）。
    patch = git(args.repo, "diff", "-U3", args.base, *([] if args.worktree else [args.rev]), "--", "deploy/dsh/natural-participation.mjs").decode("utf-8")
    participation = apply_hunks(read(np_path), patch)
    guard = (args.repo / "deploy/social/turn-guard.mjs").read_bytes() if args.worktree else git(args.repo, "show", f"{args.rev}:deploy/social/turn-guard.mjs")
    # 全部检查过才写档。
    np_path.write_text(participation, encoding="utf-8", newline="\n")
    index_path.write_text(index, encoding="utf-8", newline="\n")
    bridge_path.write_text(bridge, encoding="utf-8", newline="\n")
    (runtime / "turn-guard.mjs").write_bytes(guard)
    print("已移植：index.js、natural-participation.mjs、bridge.mjs，新增 turn-guard.mjs")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print("移植中止，没有写入任何档案：" + str(error), file=sys.stderr)
        sys.exit(1)
