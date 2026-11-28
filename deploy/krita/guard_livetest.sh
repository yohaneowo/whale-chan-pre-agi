#!/bin/sh
# 真机验证 guard 和闲置重启（不画图、不灌记忆体）：另起一个测试用 guard，只把 dsh 的压力档换成假的
# （$D/psi），状态和日志也另放；互锁用正式的那把锁。真的 systemd 停 / 开 Krita，所以先确认 gate 闲置。
# 以 root 执行：systemd-run --unit=krita-guard-livetest-driver --collect /bin/sh guard_livetest.sh
# 结果在 /root/guard-live/out，每一项印 PASS / FAIL，最后一行是 DONE 或 ABORT。
#
# 1. 单一次尖峰（一笔 30%，下一笔就退）：不停。
# 2. 持续 30%：约 15 秒停。同时跑一次测试用的闲置重启（门槛设成一定要重启、看的是测试 guard 的状态）：
#    guard 在它最后确认的 20 秒里把 Krita 停掉，它不能把 Krita 拉回来。
# 3. 压力回 0%：约 90 秒自动开回。
# 4. 第 2 次：先用 flock 占住互锁 30 秒（模拟闲置重启正在重启），guard 要等锁放开才停。之后自动开回。
# 5. 第 3 次：停的那一刻就 LOCKED，压力回 0% 等 120 秒也不开回。
# 6. 测试 guard 停着、状态档是 locked 时手动开回 Krita，再跑测试用的闲置重启：它看到 guard 锁住，不重启。
#    再把测试 guard 开起来：它看到 gate 在跑，记 MANUAL。
set -u
D=/root/guard-live; mkdir -p $D; O=$D/out; : > $O
LOCK=/var/lib/krita-guard/interlock.lock
G=/opt/krita-gate
say() { echo "$(date -u +%H:%M:%S) $*" >> $O; }
pass() { say "PASS $*"; }
fail() { say "FAIL $*"; }
psi() { echo "some avg10=$1 avg60=0.00 avg300=0.00 total=0" > $D/psi; }
act() { systemctl is-active "$1"; }
# wait_until <秒> <shell 条件>：条件成立回 0，逾时回 1
wait_until() { n=0; while [ $n -lt "$1" ]; do sh -c "$2" && return 0; sleep 1; n=$((n + 1)); done; return 1; }
abort() { say "ABORT: $*"; systemctl stop krita-guard-livetest.service 2>/dev/null; exit 1; }

C=/sys/fs/cgroup/system.slice/krita-gate.service/cpu.stat
a=$(awk '/usage_usec/{print $2}' $C); sleep 10; b=$(awk '/usage_usec/{print $2}' $C)
[ "$((b - a))" -eq 0 ] || abort "gate 10 秒内有 CPU 用量（有人在画），不测"
[ "$(ss -xH state connected | grep -c /run/krita-gate/gate.sock)" -eq 0 ] || abort "gate 有连线，不测"
[ "$(act krita-gate.service)" = active ] || abort "krita-gate 没在跑"
dsh0=$(systemctl show dsh -p MainPID -p NRestarts --value | tr '\n' ' ')
say "开始。dsh(MainPID NRestarts)=$dsh0"

rm -f $D/state.json $D/log $D/idle.json $D/idle-log
psi 0.00
systemd-run --unit=krita-guard-livetest --collect -E KRITA_GUARD_DSH_PSI_FILE=$D/psi \
	-E KRITA_GUARD_STATE_FILE=$D/state.json -E KRITA_GUARD_LOG=$D/log /usr/bin/python3 -I $G/krita_guard.py
sleep 8

# ---- 1. 单一次尖峰 ----
psi 30.00; sleep 5; psi 0.00; sleep 25
if [ "$(act krita.service)" = active ] && ! grep -q "STOP" $D/log; then pass "1 单一次尖峰不停"; else fail "1 单一次尖峰被停了"; fi

# ---- 2. 持续 → 停；同时闲置重启不能把它拉回来 ----
cat > $D/idle_test.py <<EOF
import sys
sys.path.insert(0, "$G")
import krita_idle_restart as k
k.GUARD_STATE, k.STATE, k.LOG = "$D/state.json", "$D/idle.json", "$D/idle-log"
k.ANON_LIMIT_MB, k.IDLE_MINUTES = 0, 0  # 一定想重启，只靠最后确认和互锁挡下来
sys.argv = ["idle_test"]
sys.exit(k.main())
EOF
/usr/bin/python3 -I $D/idle_test.py >> $D/idle-out 2>&1 &
idle=$!
t0=$(date +%s); psi 30.00
if wait_until 40 '[ "$(systemctl is-active krita.service)" != active ]'; then
	t=$(($(date +%s) - t0)); if [ $t -ge 14 ] && [ $t -le 25 ]; then pass "2 持续 30% 第 $t 秒停"; else fail "2 停的时间不对：第 $t 秒"; fi
else fail "2 持续 30% 40 秒都没停"; fi
wait $idle
if [ "$(act krita.service)" != active ] && [ "$(act krita-gate.service)" != active ] && grep -q "这次不动" $D/idle-log; then
	pass "2 闲置重启没有把 guard 停掉的 Krita 拉回来：$(grep '这次不动' $D/idle-log | tail -n 1 | cut -c1-160)"
else fail "2 闲置重启：krita=$(act krita.service) gate=$(act krita-gate.service) log=$(tail -n 2 $D/idle-log 2>/dev/null)"; fi

# ---- 3. 自动恢复 ----
t0=$(date +%s); psi 0.00
if wait_until 150 '[ "$(systemctl is-active krita-gate.service)" = active ]'; then
	pass "3 压力回 0% 后 $(($(date +%s) - t0)) 秒自动开回"
else fail "3 150 秒内没自动开回"; fi
wait_until 60 '[ -S /run/krita-gate/gate.sock ]'; sleep 5

# ---- 4. 互锁：闲置重启拿着锁时 guard 等它 ----
flock $LOCK sleep 30 &
holder=$!
stops=$(grep -c "STOP" $D/log)
sleep 1; t0=$(date +%s); psi 30.00
sleep 24
if [ "$(act krita.service)" = active ] && [ "$(grep -c "STOP" $D/log)" -gt "$stops" ]; then pass "4 拿不到互锁时 guard 已决定停但还没动手（第 24 秒 krita 仍 active）"
else fail "4 互锁没挡住：第 24 秒 krita=$(act krita.service)"; fi
wait $holder
if wait_until 15 '[ "$(systemctl is-active krita.service)" != active ]'; then pass "4 锁放开后 $(($(date +%s) - t0)) 秒停"; else fail "4 锁放开后没停"; fi
psi 0.00
wait_until 150 '[ "$(systemctl is-active krita-gate.service)" = active ]' && pass "4 自动开回" || fail "4 没自动开回"

# ---- 5. 第 3 次：停的那一刻锁住 ----
psi 30.00
wait_until 40 '[ "$(systemctl is-active krita.service)" != active ]' || fail "5 第 3 次没停"
if grep -q "LOCKED" $D/log && grep -q '"mode": "locked"' $D/state.json; then pass "5 第 3 次停的那一刻 LOCKED：$(grep LOCKED $D/log | tail -n 1 | cut -c27-120)"
else fail "5 没有 LOCKED"; fi
psi 0.00; sleep 120
if [ "$(act krita-gate.service)" != active ]; then pass "5 锁住后 120 秒没自动开回"; else fail "5 锁住了还自动开回"; fi

# ---- 6. guard 锁住时闲置重启不动；手动开回记 MANUAL ----
systemctl stop krita-guard-livetest.service
systemctl start krita-gate.service
wait_until 60 '[ -S /run/krita-gate/gate.sock ]'; sleep 5
pid0=$(systemctl show krita-gate.service -p MainPID --value)
/usr/bin/python3 -I $D/idle_test.py >> $D/idle-out 2>&1
pid1=$(systemctl show krita-gate.service -p MainPID --value)
if grep -q "guard 的状态是 locked" $D/idle-out && [ "$pid0" = "$pid1" ]; then pass "6 guard 锁住时闲置重启不重启（gate MainPID $pid0 没变）"
else fail "6 闲置重启：gate MainPID $pid0 → $pid1，$(tail -n 1 $D/idle-out)"; fi
systemd-run --unit=krita-guard-livetest --collect -E KRITA_GUARD_DSH_PSI_FILE=$D/psi 	-E KRITA_GUARD_STATE_FILE=$D/state.json -E KRITA_GUARD_LOG=$D/log /usr/bin/python3 -I $G/krita_guard.py
if wait_until 30 'grep -q MANUAL /root/guard-live/log'; then pass "6 测试 guard 看到手动开回，记 MANUAL"; else fail "6 没记 MANUAL"; fi
systemctl stop krita-guard-livetest.service
wait_until 60 '[ -S /run/krita-gate/gate.sock ]' && pass "socket 在：$(stat -c '%A %U:%G' /run/krita-gate/gate.sock)" || fail "socket 不在"
dsh1=$(systemctl show dsh -p MainPID -p NRestarts --value | tr '\n' ' ')
[ "$dsh1" = "$dsh0" ] && pass "dsh 没变：$dsh1" || fail "dsh 变了：$dsh0 → $dsh1"
say "正式 guard：$(act krita-works-guard.service) $(tail -n 1 /var/log/krita-guard.log | grep -o 'mode=[a-z]*')"
say DONE
