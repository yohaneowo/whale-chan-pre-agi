#!/bin/sh
# 在固定镜像里执行记忆召回基线（Linux／Git Bash／WSL）。
#   [EXTRA='--ledger 800 --memory 50'] sh deploy/eval/recall/run.sh [run|twice|ablate|scale|test] FISH_DIR OPENCC_DIR [OUT_DIR]
# 镜像不存在时先建：docker build -t recall-baseline:1 -f deploy/eval/recall/Dockerfile deploy/eval/recall
set -eu
mode=${1:-run}
fish=${2:?"要给鱼记忆 1.3.7 的目录（里面有 lib/index.js）"}
opencc=${3:?"要给已审阅的 opencc-js 1.4.2 套件目录（里面有 dist/esm/t2cn.js）"}
out=${4-}
repo=$(cd "$(dirname "$0")/../../.." && pwd)
mounts="-v $repo/deploy:/deploy:ro -v $fish:/fish:ro -v $opencc:/opencc:ro"
json=
if [ -n "$out" ]; then mkdir -p "$out"; mounts="$mounts -v $out:/out"; json="--json /out/result.json"; fi
base="docker run --rm $mounts -e SOCIAL_DIR=/deploy/social --workdir /deploy/eval/recall --entrypoint node recall-baseline:1"
case $mode in
	run) $base run.mjs $json ${EXTRA-} ;;
	twice) $base run.mjs --twice $json ${EXTRA-} ;;
	ablate) $base run.mjs --ablate $json ${EXTRA-} ;;
	scale) $base scale.mjs $json ${EXTRA-} ;;
	test) $base --test harness.test.mjs ;;
	*) echo "未知模式 $mode" >&2; exit 2 ;;
esac
