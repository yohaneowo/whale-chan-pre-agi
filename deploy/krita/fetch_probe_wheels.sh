#!/bin/sh
# Download the wheels the upstream probes need, ONCE, outside VM 102, into a wheelhouse that is
# then copied to the VM with the stage folder. Every wheel is checked against the sha256 pinned in
# probe-requirements.txt (exported from krita6-mcp's uv.lock at the pinned commit), so the VM can
# install them offline and the krita account never needs internet access.
#
# Run in a throwaway Ubuntu 24.04 container (same Python 3.12 / glibc as the VM), e.g. on the PC:
#   docker run --rm -v <deploy/krita>:/stage:ro -v <out folder>:/out ubuntu:24.04 sh /stage/fetch_probe_wheels.sh
set -eu
apt-get update -qq >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip >/dev/null 2>&1
rm -rf /out/wheelhouse
mkdir -p /out/wheelhouse
python3 -m pip download --quiet --disable-pip-version-check \
	--only-binary=:all: --require-hashes --no-deps -r /stage/probe-requirements.txt -d /out/wheelhouse
( cd /out/wheelhouse && sha256sum *.whl ) > /out/wheelhouse.sha256
echo "wheels: $(ls /out/wheelhouse | wc -l), $(du -sh /out/wheelhouse | cut -f1)"
