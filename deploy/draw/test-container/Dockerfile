# 复跑 whaledraw 测试用的 Linux 映像：把依赖（Pillow、numpy、scipy 的 wheel，已核对 sha256）、系统字型和 dsh 帐号
# 都装好，所以跑测试时不用再下载任何东西（下载 59 MB 的 wheel 与 apt 套件是以前最慢的部分）。
#
# 建立（在 repo 根目录；会下载一次 wheel）：
#   docker build -t whaledraw-test:0.2.2 -f deploy/draw/test-container/Dockerfile deploy/draw
# 不想建的话，用别人已经建好的映像：docker load -i whaledraw-test-0.2.2.tar.gz
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 curl ca-certificates fonts-noto-cjk >/dev/null \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m dsh && mkdir -p /opt/dsh && chmod 755 /opt/dsh
# 依赖：用跟 VM 同一支安装脚本（照 wheels-vm.txt 下载、核对 sha256、解压），只留 lib/
COPY install_whaledraw.sh wheels-vm.txt /tmp/build/
COPY whaledraw /tmp/build/whaledraw
COPY halloween.py test_whaledraw.py API.md /tmp/build/
RUN sh /tmp/build/install_whaledraw.sh /tmp/build deps \
 && mv /opt/dsh/whaledraw-deps/lib /opt/whaledraw-deps-lib \
 && rm -rf /opt/dsh/whaledraw-deps /tmp/build
# 测试用的 PyYAML（固定版本加 sha256，只放在映像里；VM 用的是 wheels-vm.txt）：解到系统 python 的 dist-packages，root 与 dsh 都找得到
COPY test-container/wheels-test.txt /tmp/build/wheels-test.txt
RUN grep -v '^#' /tmp/build/wheels-test.txt | while read -r sum url; do \
      curl -fsSL --retry 2 -o "/tmp/build/$(basename "$url")" "$url" \
      && echo "$sum  /tmp/build/$(basename "$url")" | sha256sum -c --quiet - \
      && python3 -I -m zipfile -e "/tmp/build/$(basename "$url")" /usr/local/lib/python3.12/dist-packages/ \
      || exit 1; \
    done \
 && chmod -R go+rX /usr/local/lib/python3.12/dist-packages \
 && python3 -c "import yaml; print('PyYAML', yaml.__version__)" \
 && rm -rf /tmp/build
COPY test-container/run-tests.sh test-container/run-dsh-tests.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/run-tests.sh /usr/local/bin/run-dsh-tests.sh
# 用法（whaledraw 测试）：docker run --rm -v <deploy/draw>:/stage:ro -v <样本资料夹>:/samples:ro whaledraw-test:0.2.2
# 用法（deploy/dsh 的测试）：docker run --rm -v <repo>/deploy:/deploy:ro whaledraw-test:0.2.2 /usr/local/bin/run-dsh-tests.sh
CMD ["/usr/local/bin/run-tests.sh"]
