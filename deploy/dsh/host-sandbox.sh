#!/bin/bash
# 部署前在 VM 上（root）跑：把现行 release 与「现行 + 新版档案」各复制进沙盒，用 nobody 身分、临时目录、
# 另一个连接埠、没有 Discord token 与模型金钥、碰不到正式 socket，走真的 trusted-entry 启动链，
# 再由 host-sandbox-probe.mjs 用真宿主建聊天与画室会话。用法：bash host-sandbox.sh <新版档案目录> [秒数]
# PROBE=huanqi-sandbox-probe.mjs HUANQI_CREDIT=0：换气额度 0 时模型回空白回合，看程序会不会整个结束（第 4 轮）。
# 新版档案目录里放 dsh-plugin-discord/…、dsh-social-runtime/…、dsh-huanqi/…（port_typing_fix.py 产生的那些档）。
set -euo pipefail
OLD=$(readlink -f /opt/dsh/heartbeat-trust-db2df/current)
FILES=$1     # 新版档案的目录
SECS=${2:-80}
SBX=$(mktemp -d /tmp/dsh-sbx.XXXXXX); chmod 755 $SBX
echo "SBX=$SBX"
mkdir -p $SBX/state $SBX/home/.dsh $SBX/srv/discord $SBX/srv/owner $SBX/drain $SBX/work
PROBE=${PROBE:-host-sandbox-probe.mjs}
case "$PROBE" in host-sandbox-probe.mjs|huanqi-sandbox-probe.mjs) ;; *) echo "不认得的探针 $PROBE"; exit 2;; esac
cp "$(dirname "$0")/$PROBE" $SBX/probe.mjs; chmod 644 $SBX/probe.mjs
mkdir -p $SBX/home/.dsh/state
# 换气额度（帐本在 $DSH_HOME/state/dsh-huanqi.json）：有给就从这个数开始。
# old、new 各跑之前都重写一次（两次跑的起点一样）。
[ -z "${HUANQI_CREDIT:-}" ] || [[ "$HUANQI_CREDIT" =~ ^[0-6]$ ]] || { echo "HUANQI_CREDIT 要是 0～6"; exit 2; }
seed_ledger() {
  [ -n "${HUANQI_CREDIT:-}" ] || return 0
  printf '{"credit": %s, "creditMax": 6, "normalTurns": 0, "stats": {"continuations": 0, "circuitBreaks": 0, "anomalies": 0}, "turnsSinceOutput": 0, "lastAnomalyAt": 0}' "$HUANQI_CREDIT" > $SBX/home/.dsh/state/dsh-huanqi.json
  chown nobody:nogroup $SBX/home/.dsh/state/dsh-huanqi.json
}
for which in old new; do
  cp -a $OLD $SBX/$which
  chmod -R u+w $SBX/$which
  if [ $which = new ]; then
    (cd $FILES && find . -type f) | while read f; do cp $FILES/$f $SBX/new/web/node_modules/$f; done
  fi
  python3 - $SBX $which <<'PY'
import sys, json
sbx, which = sys.argv[1], sys.argv[2]
rel = f'{sbx}/{which}'
p = f'{rel}/web/cordis.patch.yml'
s = open(p, encoding='utf-8').read()
for old, new in [
    ('stateFile: /var/lib/dsh/.dsh/profiles/web/discord-bridge-state.json', f'stateFile: {sbx}/state/{which}-discord-bridge-state.json'),
    ('gatewayAuthoritySocket: /run/dsh-hb-books/books.sock', f'gatewayAuthoritySocket: {sbx}/none.sock'),
    ('bookBrokerSocket: /run/dsh-hb-books/books.sock', f'bookBrokerSocket: {sbx}/none.sock'),
    ('stateRoot: /var/lib/dsh/.dsh/social-runtime', f'stateRoot: {sbx}/state/{which}-social-runtime'),
    ('dataDir: /var/lib/dsh/.dsh/heartbeat-discord-go', f'dataDir: {sbx}/state/{which}-heartbeat'),
]:
    assert s.count(old) == 1, old
    s = s.replace(old, new)
assert s.count('/srv/dsh/discord') == 2
s = s.replace('/srv/dsh/discord', f'{sbx}/srv/discord')
assert '/var/lib/dsh' not in s and '/run/dsh-hb-books' not in s, 'leftover production path'
s += f'- insert:\n  - id: sandbox-probe\n    name: {sbx}/probe.mjs\n'
open(p, 'w', encoding='utf-8').write(s)
h = json.load(open(f'{rel}/host.json'))
h['webArgs'] = ['--no-open', '--host', '127.0.0.1', '--port', '3199']
json.dump(h, open(f'{rel}/host.json', 'w'))
PY
done
chown -R nobody:nogroup $SBX/state $SBX/home $SBX/srv $SBX/drain $SBX/work
for which in old new; do
  echo "===== $which ====="
  seed_ledger
  cd $SBX/work
  set +e
  runuser -u nobody -- env -i HOME=$SBX/home DSH_HOME=$SBX/home/.dsh PATH=/opt/dsh/node/bin:/usr/bin:/bin \
    NODE_OPTIONS=--max-old-space-size=768 TZ=Asia/Taipei DSH_HOST_MODULES=/opt/dsh/app/node_modules/@deepseek-ai \
    DSH_DRAIN_CONTROL=/run/dsh-drain DSH_DRAIN_STATUS=$SBX/drain INVOCATION_ID=$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n') \
    SBX_RELEASE=$SBX/$which SBX_GROUP_CWD=$SBX/srv/discord \
    timeout -s TERM $SECS /opt/dsh/node/bin/node $SBX/$which/trusted-entry.mjs > $SBX/$which.log 2>&1
  echo "exit=$?"
  set -e
  grep -v -E "^\s*$" $SBX/$which.log | sed -E 's#https?://[^ ]*#[url]#g' | cut -c1-400 | head -80
done
echo "沙盒日志留在 $SBX（只有沙盒资料）；看完 rm -rf $SBX"
