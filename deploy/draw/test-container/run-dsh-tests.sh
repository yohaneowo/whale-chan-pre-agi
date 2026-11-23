#!/bin/sh
# 映像里执行：跑 deploy/dsh 的测试（configure_social、pack_stage、DEPLOY.md 里的 shell 片段、prepare_discord_plugin），
# 先以 root、再以 dsh 各跑一次。要把整个 deploy 目录挂载到 /deploy（唯读）：
#   docker run --rm -v <repo>/deploy:/deploy:ro whaledraw-test:0.2.2 /usr/local/bin/run-dsh-tests.sh
# 结束码：两个身分都通过才是 0；任何一个失败（测试失败、出错、没跑起来）就是 1。
# dsh 身分下，要 root 的片段测试（test_deploy_snippets）会跳过；root 身分下不该有任何一项因为缺 PyYAML 而跳过。
set -u
[ -f /deploy/dsh/test_configure_social.py ] || { echo "要把整个 deploy 目录挂载到 /deploy" >&2; exit 2; }
MODULES=${TESTS:-"test_configure_social test_pack_stage test_deploy_snippets test_prepare_discord_plugin"}
python3 -c "import yaml; print('PyYAML', yaml.__version__)" || { echo "映像里没有 PyYAML（YAML 严格测试会被跳过）" >&2; exit 1; }
rm -rf /root/d /tmp/dd
cp -r /deploy /root/d
cp -r /deploy /tmp/dd && chown -R dsh /tmp/dd

run_as() {
	who=$1
	shift
	"$@" > "/tmp/out-dsh-$who.txt" 2>&1
	status=$?
	grep -E "^(ERROR|FAIL):|^Ran|^OK|^FAILED|skipped|Traceback|ModuleNotFoundError|ImportError" "/tmp/out-dsh-$who.txt"
	if [ "$status" -ne 0 ] && ! grep -qE "^(FAILED|OK)" "/tmp/out-dsh-$who.txt"; then
		tail -20 "/tmp/out-dsh-$who.txt"
	fi
	return $status
}

echo "== root"
run_as root sh -c "cd /root/d/dsh && python3 -B -m unittest $MODULES"
root_status=$?
echo "== dsh"
run_as dsh su dsh -s /bin/sh -c "cd /tmp/dd/dsh && python3 -B -m unittest $MODULES"
dsh_status=$?
echo "== 结果：root 结束码 $root_status，dsh 结束码 $dsh_status"
if [ "$root_status" -ne 0 ] || [ "$dsh_status" -ne 0 ]; then
	echo "测试没有全部通过" >&2
	exit 1
fi
exit 0
