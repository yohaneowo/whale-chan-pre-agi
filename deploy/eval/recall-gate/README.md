# 回忆守门员的真模型实测

「这一则要不要想起更早以前的事」由回忆守门员（`deploy/social/recall-rules.mjs` 的 `createRecallGate`）判断，不靠关键字。这里是它的真模型题目与统计。题目全是合成的对话，没有群友资料。

- `cases.mjs`：40 题，分打招呼、情绪、提到人、明确问、含蓄追问（成对：前十分钟有没有在聊那件事）、没写进提示词的新题（held-out）、操控。
- `input.mjs`：把题目、回想题库（`deploy/eval/recall`）的问题、群聊人设整理成 `gate-input.json`。
- `report.mjs`：读沙盒印出的 `[sbx]` 行，印出每类准确度、两轮一不一致、延迟、用量与参考成本，并把题库问题的判断写成 `deploy/eval/recall/gate-verdicts.json`（题库不呼叫模型，照这些真模型判断跑）。

## 怎么跑（VM 沙盒，一次只跑一件事）

```sh
node deploy/eval/recall-gate/input.mjs <extra>/gate-input.json      # 本机产生题目
# VM 上（root），金钥只放环境变数；新版 10 个档在 <files>：
OPENCODE_GO_API_KEY=… PROBE=recall-gate-sandbox-probe.mjs PROBE_FILES=<extra> SBX_ROUNDS=2 \
  bash notes-sandbox.sh new <files> /tmp/gate-sbx 1200
grep -a '\[sbx\]' /tmp/gate-sbx/run.log > sbx.txt                   # 跑完（印出 {"k":"done"}）就停掉沙盒的 scope
node deploy/eval/recall-gate/report.mjs sbx.txt <extra>/gate-input.json --verdicts deploy/eval/recall/gate-verdicts.json
```

探针（`deploy/dsh/recall-gate-sandbox-probe.mjs`）用真宿主、正式的模型路线一题一题问；另有对照组「不设守门员，让聊天模型自己决定要不要叫 `chat_search`」（人设 + 行为规则 + 工具，只看第一步）。价格：opencode-go 的单价查不到（TBD），统计里用 DeepSeek 官方 `deepseek-flash` 的美元价当参考（`docs/deepseek-notes.md`）。

结果与选定的做法记在 `deploy/dsh/memory-rules-port.md` 的「回忆守门员实测」。
