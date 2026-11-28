#!/bin/sh
# 自动恢复实测：另起一个测试用 guard，只把 dsh 的压力档换成假的；真的 systemd 停 / 开 Krita。
# 先把 measure_cold_start.py 放到 /opt/krita-gate/（krita 读得到），以 root 执行：systemd-run --collect /bin/sh guard_livetest.sh；结果在 /root/guard-live/out。
set -u
D=/root/guard-live; mkdir -p $D; O=$D/out; : > $O
say() { echo "$(date -u +%H:%M:%S) $*" >> $O; }
C=/sys/fs/cgroup/system.slice/krita-gate.service/cpu.stat
a=$(awk '/usage_usec/{print $2}' $C); sleep 10; b=$(awk '/usage_usec/{print $2}' $C)
say "gate cpu delta 10s=$((b-a))"
[ "$((b-a))" -eq 0 ] || { say "gate 有动静，不测"; exit 1; }
say "dsh $(systemctl show dsh -p MainPID -p NRestarts --value | tr '\n' ' ')"
rm -f $D/state.json $D/log
echo "some avg10=30.00 avg60=0.00 avg300=0.00 total=0" > $D/psi
systemd-run --unit=krita-guard-livetest --collect -E KRITA_GUARD_DSH_PSI_FILE=$D/psi -E KRITA_GUARD_STATE_FILE=$D/state.json -E KRITA_GUARD_LOG=$D/log /usr/bin/python3 -I /opt/krita-gate/krita_guard.py
say "测试 guard 启动，假 dsh 压力 30%"
while [ "$(systemctl is-active krita.service)" = active ]; do sleep 1; done
say "krita.service 停了：$(systemctl is-active krita.service)/gate $(systemctl is-active krita-gate.service)"
sleep 3; echo "some avg10=0.00 avg60=0.00 avg300=0.00 total=0" > $D/psi
say "假 dsh 压力改回 0%"
while [ "$(systemctl is-active krita-gate.service)" != active ]; do sleep 1; done
t0=$(date +%s.%N)
say "krita-gate 自动开回来：krita $(systemctl is-active krita.service)"
cd /tmp && runuser -u krita -- env PYTHONPATH=/opt/krita-gate/works-v1.2/plugin:/opt/krita6-mcp/current/src python3 -s /opt/krita-gate/measure_cold_start.py "$t0" >> $O 2>&1
say "socket: $(ls -la /run/krita-gate/gate.sock 2>&1)"
systemctl stop krita-guard-livetest.service
say "测试 guard 已停；dsh $(systemctl show dsh -p MainPID -p NRestarts --value | tr '\n' ' ')"
say "anon=$(awk '/^anon /{print int($2/1048576)}' /sys/fs/cgroup/system.slice/krita.service/memory.stat)MB"
say DONE
