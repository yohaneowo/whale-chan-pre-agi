#!/bin/sh
# krita-guard: protect the dsh bot while Krita is being tried. Run as root in the background
# during every Krita phase:
#   sudo nohup /opt/krita-gate/krita_guard.sh >/dev/null 2>&1 &
# Every 5 s it logs memory and pressure to /var/log/krita-guard.log, and STOPS krita.service and
# krita-gate.service (and any probe scope) at once if the bot is affected:
#   - dsh restarted, stopped, or had an OOM kill
#   - VM MemAvailable below 450 MB
#   - memory pressure (some avg10) above 20 %, or CPU pressure (some avg10) above 60 %
set -u
LOG=/var/log/krita-guard.log
CG=/sys/fs/cgroup/system.slice

restarts() { systemctl show dsh -p NRestarts --value; }
ooms() { awk '/^oom_kill /{print $2}' $CG/dsh.service/memory.events 2>/dev/null || echo 0; }
avail() { awk '/^MemAvailable/{print int($2/1024)}' /proc/meminfo; }
pressure() { awk -F'[ =]' '/^some/{print int($3)}' /proc/pressure/$1; }
peak() { cat $CG/$1/memory.peak 2>/dev/null | awk '{print int($1/1048576)}' || echo -; }

start_restarts=$(restarts)
start_ooms=$(ooms)
echo "$(date -Is) guard start: dsh restarts=$start_restarts ooms=$start_ooms avail=$(avail)MB" >> $LOG

stop_all() {
	echo "$(date -Is) STOP: $1" >> $LOG
	systemctl stop krita-gate.service krita.service 2>>$LOG
	systemctl stop 'krita-probe-*.service' 2>>$LOG
	exit 1
}

while :; do
	a=$(avail); m=$(pressure memory); c=$(pressure cpu)
	echo "$(date -Is) avail=${a}MB psi_mem=${m}% psi_cpu=${c}% dsh=$(systemctl is-active dsh)/$(peak dsh.service)MBpeak krita=$(systemctl is-active krita)/$(peak krita.service)MBpeak" >> $LOG
	[ "$(systemctl is-active dsh)" = active ] || stop_all "dsh is not active"
	[ "$(restarts)" = "$start_restarts" ] || stop_all "dsh restarted"
	[ "$(ooms)" = "$start_ooms" ] || stop_all "dsh had an OOM kill"
	[ "$a" -ge 450 ] || stop_all "MemAvailable ${a}MB < 450MB"
	[ "$m" -le 20 ] || stop_all "memory pressure ${m}%"
	[ "$c" -le 60 ] || stop_all "CPU pressure ${c}%"
	sleep 5
done
