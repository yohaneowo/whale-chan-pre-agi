#!/bin/bash
# 部署前在 VM 上（root）用真模型跑一次背景整理记忆（不画图、不开 Krita 闸道、没有 Discord token）：
# 现行 release 复制一份（new 再换上新版档案），用 nobody 身分、临时目录、另一个连接埠，走真的 trusted-entry 启动链，
# 由 notes-sandbox-probe.mjs 跑一次真的 consolidate，看人物卡与日记有没有真的写出来。整个沙盒放进记忆体上限 1G 的 scope。
# 用法：OPENCODE_GO_API_KEY 由呼叫的人放在环境变数里（不写进任何档案），
#   bash notes-sandbox.sh <old|new> <新版档案目录> <沙盒目录> [秒数]
# 一次只跑一个（old 跑完再跑 new）；可用记忆体不到 1.6G 就不跑。
set -euo pipefail
WHICH=$1
FILES=$2
SBX=$3
SECS=${4:-300}
case "$WHICH" in old|new) ;; *) echo "第一个参数要是 old 或 new"; exit 2;; esac
OLD=$(readlink -f /opt/dsh/heartbeat-trust-db2df/current)
: "${OPENCODE_GO_API_KEY:?要先设 OPENCODE_GO_API_KEY（只放环境变数）}"
avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
if [ "$avail" -lt 1600 ]; then echo "可用记忆体只有 ${avail} MiB，不跑"; exit 3; fi
rm -rf $SBX; mkdir -p $SBX; chmod 755 $SBX
mkdir -p $SBX/state $SBX/home/.dsh/state $SBX/srv/discord $SBX/srv/owner $SBX/drain $SBX/work
cp "$(dirname "$0")/notes-sandbox-probe.mjs" $SBX/probe.mjs; chmod 644 $SBX/probe.mjs
cp -a $OLD $SBX/rel; chmod -R u+w $SBX/rel
if [ $WHICH = new ]; then
  (cd $FILES && find . -type f) | while read f; do cp $FILES/$f $SBX/rel/web/node_modules/$f; done
fi
python3 - $SBX <<'PY'
import sys, json
sbx = sys.argv[1]
rel = f'{sbx}/rel'
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
s += f'- insert:\n  - id: notes-sandbox-probe\n    name: {sbx}/probe.mjs\n'
open(p, 'w', encoding='utf-8').write(s)
h = json.load(open(f'{rel}/host.json'))
h['webArgs'] = ['--no-open', '--host', '127.0.0.1', '--port', '3199']
json.dump(h, open(f'{rel}/host.json', 'w'))
PY
chown -R nobody:nogroup $SBX/state $SBX/home $SBX/srv $SBX/drain $SBX/work
cd $SBX/work
set +e
# 金钥只经环境变数往下传（不放进任何指令列参数：systemd 会把 scope 的指令记进日志）。
export SBX SECS SBX_INVOCATION=$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')
systemd-run --scope --quiet --unit=notes-sbx-$$ --description=notes-sandbox -p MemoryMax=1G -p MemorySwapMax=0 -- \
  setpriv --reuid=65534 --regid=65534 --clear-groups -- /bin/bash -c '
  exec env -i HOME="$SBX/home" DSH_HOME="$SBX/home/.dsh" PATH=/opt/dsh/node/bin:/usr/bin:/bin \
    OPENCODE_GO_API_KEY="$OPENCODE_GO_API_KEY" \
    NODE_OPTIONS=--max-old-space-size=768 TZ=Asia/Taipei DSH_HOST_MODULES=/opt/dsh/app/node_modules/@deepseek-ai \
    DSH_DRAIN_CONTROL=/run/dsh-drain DSH_DRAIN_STATUS="$SBX/drain" INVOCATION_ID="$SBX_INVOCATION" \
    SBX_RELEASE="$SBX/rel" SBX_GROUP_CWD="$SBX/srv/discord" \
    timeout -s TERM "$SECS" /opt/dsh/node/bin/node "$SBX/rel/trusted-entry.mjs"' > $SBX/run.log 2>&1
echo "exit=$?"
set -e
if grep -qF "$OPENCODE_GO_API_KEY" $SBX/run.log; then sed -i "s#$OPENCODE_GO_API_KEY#[已省略]#g" $SBX/run.log; echo "日志里出现金钥，已抹掉"; fi
grep -a -E '\[sbx\]|背景整理' $SBX/run.log | cut -c1-2000 || true
# fish-memory 的诊断日志：设定档不存在的提示应该只有一行（新版）。
echo "fish-memory 设定档提示：$(grep -c '配置文件' $SBX/home/.dsh/fish-memory-diag.log 2>/dev/null || echo 0) 行"
echo "沙盒留在 $SBX（只有沙盒资料）；看完 rm -rf $SBX"
