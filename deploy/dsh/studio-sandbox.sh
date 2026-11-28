#!/bin/bash
# 部署前在 VM 上（root）用真模型跑一次画室与聊天：现行 release 复制一份、换上新版档案，用 nobody 身分
# （只多 kritaio 群组，能连 Krita 闸道）、临时目录、另一个连接埠、没有 Discord token，走真的 trusted-entry 启动链，
# 再由 studio-sandbox-probe.mjs 跑情境（假 Discord，图存进沙盒目录）。整个沙盒放进记忆体上限 1G 的 scope，
# 挤不到正式的 dsh.service。
# 用法：OPENCODE_GO_API_KEY 由呼叫的人放在环境变数里（不写进任何档案），
#   bash studio-sandbox.sh <新版档案目录> <沙盒目录> [秒数] [情境，逗号分隔，预设 all]
set -euo pipefail
OLD=$(readlink -f /opt/dsh/heartbeat-trust-db2df/current)
FILES=$1
SBX=$2
SECS=${3:-2400}
SCENARIOS=${4:-all}
: "${OPENCODE_GO_API_KEY:?要先设 OPENCODE_GO_API_KEY（只放环境变数）}"
avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
if [ "$avail" -lt 1600 ]; then echo "可用记忆体只有 ${avail} MiB，不跑"; exit 3; fi
mkdir -p $SBX; chmod 755 $SBX
mkdir -p $SBX/state $SBX/home/.dsh/state $SBX/srv/discord $SBX/srv/owner $SBX/drain $SBX/work $SBX/out
cp "$(dirname "$0")/studio-sandbox-probe.mjs" $SBX/probe.mjs; chmod 644 $SBX/probe.mjs
# gate 情境（要不要画的守门员）用的正反例；跟探针一样只在沙盒里。
cp "$(dirname "$0")/draw-intent-cases.mjs" $SBX/cases.mjs; chmod 644 $SBX/cases.mjs
rm -rf $SBX/new; cp -a $OLD $SBX/new; chmod -R u+w $SBX/new
(cd $FILES && find . -type f) | while read f; do cp $FILES/$f $SBX/new/web/node_modules/$f; done
python3 - $SBX <<'PY'
import sys, json
sbx = sys.argv[1]
rel = f'{sbx}/new'
p = f'{rel}/web/cordis.patch.yml'
s = open(p, encoding='utf-8').read()
for old, new in [
    ('stateFile: /var/lib/dsh/.dsh/profiles/web/discord-bridge-state.json', f'stateFile: {sbx}/state/discord-bridge-state.json'),
    ('gatewayAuthoritySocket: /run/dsh-hb-books/books.sock', f'gatewayAuthoritySocket: {sbx}/none.sock'),
    ('bookBrokerSocket: /run/dsh-hb-books/books.sock', f'bookBrokerSocket: {sbx}/none.sock'),
    ('stateRoot: /var/lib/dsh/.dsh/social-runtime', f'stateRoot: {sbx}/state/social-runtime'),
    ('dataDir: /var/lib/dsh/.dsh/heartbeat-discord-go', f'dataDir: {sbx}/state/heartbeat'),
]:
    assert s.count(old) == 1, old
    s = s.replace(old, new)
assert s.count('/srv/dsh/discord') == 2
s = s.replace('/srv/dsh/discord', f'{sbx}/srv/discord')
assert '/var/lib/dsh' not in s and '/run/dsh-hb-books' not in s, 'leftover production path'
s += f'- insert:\n  - id: studio-sandbox-probe\n    name: {sbx}/probe.mjs\n'
open(p, 'w', encoding='utf-8').write(s)
h = json.load(open(f'{rel}/host.json'))
h['webArgs'] = ['--no-open', '--host', '127.0.0.1', '--port', '3199']
json.dump(h, open(f'{rel}/host.json', 'w'))
# 换气额度从 0 开始（跟 2026-11-28 正式环境一样），才看得到换气会不会打断画室。
json.dump({'credit': 0, 'creditMax': 6, 'normalTurns': 0,
           'stats': {'continuations': 0, 'circuitBreaks': 0, 'anomalies': 0},
           'turnsSinceOutput': 0, 'lastAnomalyAt': 0},
          open(f'{sbx}/home/.dsh/state/dsh-huanqi.json', 'w'))
PY
chown -R nobody:nogroup $SBX/state $SBX/home $SBX/srv $SBX/drain $SBX/work $SBX/out
KRITAIO=$(getent group kritaio | cut -d: -f3)
# 正式的 Krita 闸道只收正式 dsh.service 的 node（安全设计），沙盒另开一个临时闸道：同一个 Krita，
# 作品与登记放沙盒的临时目录，只收 nobody 的连线（studio-sandbox-gate.py）。沙盒里的 krita-tools 改指到它。
rm -rf $SBX/kgate; mkdir -p $SBX/kgate/out $SBX/kgate/registry $SBX/kgate/staging
cp "$(dirname "$0")/studio-sandbox-gate.py" $SBX/kgate.py; chmod 644 $SBX/kgate.py
chown -R krita:kritaio $SBX/kgate; chmod 2770 $SBX/kgate; chmod 700 $SBX/kgate/out $SBX/kgate/registry $SBX/kgate/staging
KT=$SBX/new/web/node_modules/dsh-social-runtime/krita-tools.mjs
[ "$(grep -c "^const SOCKET = '/run/krita-gate/gate.sock';" $KT)" = 1 ] || { echo "krita-tools 的 SOCKET 对不上"; exit 4; }
sed -i "s#^const SOCKET = '/run/krita-gate/gate.sock';#const SOCKET = '$SBX/kgate/gate.sock';#" $KT
# Krita 只准存到它设定好的作品目录（KRITA6_MCP_OUTPUT_ROOTS），所以作品档放同一个目录（每张有自己的编号目录，
# 不会撞到正式作品）；登记表用沙盒自己的，正式的 recent 看不到。跑完删掉沙盒新增的作品目录。
WORKS=/var/lib/krita/works-files
ls $WORKS > $SBX/kgate/works-before.txt
# 只删「跑之前没有、而且沙盒会话里出现过编号」的作品目录（登记表里的编号不是明文，不能拿来比对）。
cleanup_works() {
  local made
  made=$(find $SBX/home/.dsh/sessions -name 'session.v4.jsonl.zstd' -exec zstd -dcq {} \; 2>/dev/null | grep -o 'work-[0-9a-f]\{24\}' | sort -u)
  for d in $(ls $WORKS); do
    grep -qx "$d" $SBX/kgate/works-before.txt && continue
    echo "$made" | grep -qx "$d" && rm -rf "${WORKS:?}/$d" && echo "删掉沙盒作品目录 $d"
  done
}
runuser -u krita -g kritaio -- env -i PATH=/usr/bin:/bin \
  PYTHONPATH=/opt/krita-gate:/opt/krita-gate/works-v1.2/plugin:/opt/krita6-mcp/current/src:/opt/krita-gate/pillow-13.0.0 \
  KRITA6_MCP_STATE_DIR=/var/lib/krita/.local/state/krita6-mcp KRITA6_MCP_SRC=/opt/krita6-mcp/current/src \
  KRITA6_MCP_PLUGIN=/opt/krita6-mcp/current/plugin KRITA_GATE_IN=/srv/krita-io/in \
  KRITA_GATE_OUT=$WORKS KRITA_WORK_REGISTRY=$SBX/kgate/registry KRITA_GATE_STAGING=$SBX/kgate/staging \
  KRITA_GATE_STUCK=$SBX/kgate/stuck.json KRITA_GATE_SOCKET=$SBX/kgate/gate.sock SBX_PEER_UID=65534 \
  python3 -s $SBX/kgate.py > $SBX/kgate.log 2>&1 &
GATE_PID=$!
trap 'kill $GATE_PID 2>/dev/null || true; cleanup_works' EXIT
for i in $(seq 1 50); do [ -S $SBX/kgate/gate.sock ] && break; sleep 0.2; done
[ -S $SBX/kgate/gate.sock ] || { echo "临时 Krita 闸道没起来："; tail -5 $SBX/kgate.log; exit 5; }
cd $SBX/work
set +e
# 金钥只经环境变数往下传（不放进任何指令列参数：systemd 会把 scope 的指令记进日志）。
export SBX SECS SCENARIOS SBX_GATE_RUNS=${SBX_GATE_RUNS:-2} SBX_EFFORT=${SBX_EFFORT:-off} SBX_INVOCATION=$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')
systemd-run --scope --quiet --unit=studio-sbx-$$ --description=studio-sandbox -p MemoryMax=1G -p MemorySwapMax=0 -- \
  setpriv --reuid=65534 --regid=65534 --groups=$KRITAIO -- /bin/bash -c '
  exec env -i HOME="$SBX/home" DSH_HOME="$SBX/home/.dsh" PATH=/opt/dsh/node/bin:/usr/bin:/bin \
    OPENCODE_GO_API_KEY="$OPENCODE_GO_API_KEY" \
    NODE_OPTIONS=--max-old-space-size=768 TZ=Asia/Taipei DSH_HOST_MODULES=/opt/dsh/app/node_modules/@deepseek-ai \
    DSH_DRAIN_CONTROL=/run/dsh-drain DSH_DRAIN_STATUS="$SBX/drain" INVOCATION_ID="$SBX_INVOCATION" \
    SBX_RELEASE="$SBX/new" SBX_GROUP_CWD="$SBX/srv/discord" SBX_OUT="$SBX/out" SBX_SCENARIOS="$SCENARIOS" \
    SBX_DIR="$SBX" SBX_GATE_RUNS="$SBX_GATE_RUNS" \
    SBX_EFFORT="${SBX_EFFORT:-off}" DISCORD_DRAW_DEADLINE_MS=600000 \
    timeout -s TERM "$SECS" /opt/dsh/node/bin/node "$SBX/new/trusted-entry.mjs"' > $SBX/run.log 2>&1
echo "exit=$?"
set -e
# 日志里不能有金钥。
if grep -qF "$OPENCODE_GO_API_KEY" $SBX/run.log; then sed -i "s#$OPENCODE_GO_API_KEY#[已省略]#g" $SBX/run.log; echo "日志里出现金钥，已抹掉"; fi
grep -a '\[sbx\]' $SBX/run.log | cut -c1-1500 || true
echo "沙盒留在 $SBX（只有沙盒资料与测试图）；看完 rm -rf $SBX"
