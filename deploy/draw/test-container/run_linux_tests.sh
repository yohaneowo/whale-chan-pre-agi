#!/bin/sh
# 一行跑完整的 Linux 测试（root 与 dsh 各一次）。结束码跟测试结果一致：都过是 0，有失败是 1。
#   sh deploy/draw/test-container/run_linux_tests.sh [样本资料夹]
# 在 Git Bash、WSL、Linux、macOS 都可以；PowerShell 用同一个资料夹里的 run_linux_tests.ps1。
# 映像不存在时：先 `docker load -i <whaledraw-test-0.2.2.tar.gz>`，或用 Dockerfile 开头写的 docker build 指令建。
# 样本资料夹（选配）放 whale_sheet.png；本机共享的在 C:\Users\USER\OpenBot\Shared\whale-draw-acceptance-20261101\samples。
# 环境变量（选配）：STAGE_DIR 换成别的 deploy/draw 副本来测；TESTS 指定要跑的测试模块（预设 test_whaledraw）。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
STAGE=${STAGE_DIR:-$(dirname "$HERE")}
SAMPLES=${1:-}
# Git Bash 的路径（/c/Users/...）Docker 看不懂：有 cygpath 就转成 Windows 路径，并且不要让 MSYS 再改一次。
if command -v cygpath >/dev/null 2>&1; then
	STAGE=$(cygpath -w "$STAGE")
	[ -z "$SAMPLES" ] || SAMPLES=$(cygpath -w "$SAMPLES")
fi
export MSYS_NO_PATHCONV=1
set -- --rm -v "$STAGE:/stage:ro"
[ -z "$SAMPLES" ] || set -- "$@" -v "$SAMPLES:/samples:ro"
[ -z "${TESTS:-}" ] || set -- "$@" -e "TESTS=$TESTS"
docker run "$@" whaledraw-test:0.2.2
