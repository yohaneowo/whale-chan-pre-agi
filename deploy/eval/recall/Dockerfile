# 记忆召回基线的执行环境：只固定运行时——Node 版本（镜像 digest 钉死）。不装任何 npm 套件（被测的程式只用 Node 内建模组）。
# harness 与被测程式（deploy/social）、鱼记忆引擎 1.3.7、OpenCC 1.4.2 都在执行时以只读方式挂载进来，
# harness 会核对鱼记忆与 OpenCC 的 sha256，并把 deploy/social 里被测档案的 sha256 写进结果标头。
#
# 建立（在 repo 根目录）：
#   docker build -t recall-baseline:1 -f deploy/eval/recall/Dockerfile deploy/eval/recall
# 执行：run.ps1（Windows）或 run.sh（Linux／Git Bash），见 README.md。
FROM node:24-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9
# 备份还原案例用 tar（镜像里本来就有）；没有就建置失败，免得执行时才发现。
RUN tar --version >/dev/null
ENV NODE_ENV=test TZ=UTC
