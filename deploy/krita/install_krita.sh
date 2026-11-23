#!/bin/sh
# Install Krita 6.0.4 + krita6-mcp bridge + krita-gate on VM 102, isolated from the dsh bot.
# Run by the maintenance account with sudo, from the folder holding the deploy/krita files:
#   sudo sh install_krita.sh <folder with deploy/krita files>
# Installs only; does NOT start or enable anything (see KRITA.md for the phases).
# Fresh install only: if anything it would create already exists, it stops without touching it.
# Pinned downloads are checked with sha256.
set -eu

SRC=$1
KRITA_VERSION=6.0.4
KRITA_URL=https://download.kde.org/stable/krita/6.0.4/krita-6.0.4-x86_64.AppImage
KRITA_SHA256=4ec9a45892308134b0ff5410f30aa9e68ae58d0dae5fb122666cfd27dfd6de9f
MCP_COMMIT=36388b85d96f526c61d97a400989c09b96c5c30a
MCP_SHORT=36388b8
MCP_REPO=https://github.com/fanzhuyifan/krita6-mcp.git
UV_VERSION=0.13.6
UV_URL=https://github.com/astral-sh/uv/releases/download/0.13.6/uv-x86_64-unknown-linux-gnu.tar.gz
UV_SHA256=f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

# Names that become paths: plain names only.
for name in "$KRITA_VERSION" "$MCP_SHORT" "$UV_VERSION"; do
	case $name in '' | .* | */* | *[!0-9A-Za-z._-]*) echo "invalid name: '$name'" >&2; exit 2 ;; esac
done
KRITA_DIR=/opt/krita/$KRITA_VERSION
MCP_DIR=/opt/krita6-mcp/$MCP_SHORT
UV_DIR=/opt/krita-tools/uv-$UV_VERSION

# Fail closed: every location this script creates must not exist yet (not even as a dangling
# link), and the krita user / krita, kritaio groups must not exist. Nothing existing is re-owned,
# re-moded or replaced. To reinstall, roll back first (KRITA.md).
for path in /opt/krita /opt/krita6-mcp /opt/krita-tools /opt/krita-gate /var/lib/krita \
	/srv/dsh/krita-io /srv/krita-io /etc/systemd/system/krita.service /etc/systemd/system/krita-gate.service; do
	if [ -e "$path" ] || [ -L "$path" ]; then
		echo "refusing: $path already exists (not a fresh install; see KRITA.md rollback)" >&2
		exit 1
	fi
done
if getent passwd krita >/dev/null || getent group krita >/dev/null || getent group kritaio >/dev/null; then
	echo "refusing: user krita or group krita/kritaio already exists" >&2
	exit 1
fi
getent passwd dsh >/dev/null || { echo "refusing: no dsh user" >&2; exit 1; }
if [ ! -d /srv/dsh ] || [ -L /srv/dsh ] || [ -L /opt ] || [ -L /var/lib ]; then
	echo "refusing: /srv/dsh missing, or a parent folder is a link" >&2
	exit 1
fi

# On failure, remove only the top folders and temp downloads this run created.
CREATED=
cleanup() {
	status=$?
	if [ "$status" -ne 0 ]; then
		for path in $CREATED; do
			case $path in /opt/krita | /opt/krita6-mcp | /opt/krita-tools | /var/tmp/krita-install.* | /var/tmp/uv-install.*)
				rm -rf -- "$path"; echo "failed; removed $path" >&2 ;;
			esac
		done
		echo "accounts, /var/lib/krita, /srv/dsh/krita-io, units: see KRITA.md rollback if they were created" >&2
	fi
	exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "== 1. system packages (Xvfb; libEGL/Mesa are already on VM 102 and are only checked here)"
# --no-upgrade: never upgrade what the VM already has; needrestart suspended: never restart services.
NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends --no-upgrade xvfb libegl1 libgl1-mesa-dri   # the AppImage needs the system libEGL.so.1

echo "== 2. accounts (new)"
groupadd --system kritaio
useradd --system --user-group --home-dir /var/lib/krita --create-home --shell /usr/sbin/nologin krita
usermod -aG kritaio krita
usermod -aG kritaio dsh   # takes effect for the dsh service at its next restart

echo "== 3. Krita AppImage"
WORK=$(mktemp -d /var/tmp/krita-install.XXXXXX)
CREATED="$CREATED $WORK"
chown krita:krita "$WORK"
curl -fsSL --retry 2 -o "$WORK/krita.AppImage" "$KRITA_URL"
echo "$KRITA_SHA256  $WORK/krita.AppImage" | sha256sum -c --quiet -
chmod 755 "$WORK/krita.AppImage"
# Extract as the unprivileged krita user (no FUSE needed), then hand the tree to root.
( cd "$WORK" && sudo -u krita ./krita.AppImage --appimage-extract >/dev/null )
CREATED="$CREATED /opt/krita"
mkdir -m 0755 /opt/krita
mv "$WORK/squashfs-root" "$KRITA_DIR"
chown -R root:root "$KRITA_DIR"
chmod -R go-w "$KRITA_DIR"
ln -sn "$KRITA_VERSION" /opt/krita/current
rm -rf -- "$WORK"

echo "== 4. krita6-mcp at a pinned commit"
CREATED="$CREATED /opt/krita6-mcp"
mkdir -m 0755 /opt/krita6-mcp
git clone -q "$MCP_REPO" "$MCP_DIR"
git -C "$MCP_DIR" -c advice.detachedHead=false checkout -q "$MCP_COMMIT"
[ "$(git -C "$MCP_DIR" rev-parse HEAD)" = "$MCP_COMMIT" ]
chown -R root:root "$MCP_DIR"
chmod -R go-w "$MCP_DIR"
ln -sn "$MCP_SHORT" /opt/krita6-mcp/current

echo "== 5. uv (only for the upstream probes)"
WORK=$(mktemp -d /var/tmp/uv-install.XXXXXX)
CREATED="$CREATED $WORK"
curl -fsSL --retry 2 -o "$WORK/uv.tar.gz" "$UV_URL"
echo "$UV_SHA256  $WORK/uv.tar.gz" | sha256sum -c --quiet -
CREATED="$CREATED /opt/krita-tools"
mkdir -m 0755 /opt/krita-tools "$UV_DIR"
tar -xzf "$WORK/uv.tar.gz" -C "$UV_DIR" --strip-components=1 --no-same-owner
chown -R root:root /opt/krita-tools
chmod -R go-w /opt/krita-tools
rm -rf -- "$WORK"

echo "== 6. Krita profile with the bridge plugin enabled (new home made by useradd)"
P=/var/lib/krita
install -d -o krita -g krita -m 0750 $P/.config $P/.local $P/.local/share $P/.local/share/krita $P/.cache $P/.local/state
# staging: krita-only copies of the pictures to import (the gate copies them out of in/)
install -d -o krita -g krita -m 0700 $P/.local/state/krita6-mcp $P/staging $P/gate   # gate: stuck marker
install -d -o root -g root -m 0755 $P/.local/share/krita/pykrita
cp -r "$MCP_DIR/plugin/krita6_bridge" $P/.local/share/krita/pykrita/
cp "$MCP_DIR/plugin/krita6_bridge.desktop" $P/.local/share/krita/pykrita/
chown -R root:root $P/.local/share/krita/pykrita
chmod -R go-w $P/.local/share/krita/pykrita
printf '%s\n' '[python]' 'enable_krita6_bridge=true' > $P/.config/kritarc
chown krita:krita $P/.config/kritarc

echo "== 7. shared folder and gate (all new: the preflight refused if any existed)"
# root owns the top so nobody can swap in/ or out/ for a link.
#   in/  dsh puts pictures to import here; only the gate reads them (and copies them to staging).
#   out/ krita writes .kra, PNG and previews; dsh can only read (cannot plant links there).
mkdir -m 0750 /srv/dsh/krita-io && chown root:kritaio /srv/dsh/krita-io
mkdir -m 2750 /srv/dsh/krita-io/in && chown dsh:kritaio /srv/dsh/krita-io/in
mkdir -m 2750 /srv/dsh/krita-io/out && chown krita:kritaio /srv/dsh/krita-io/out
mkdir -m 0755 /srv/krita-io   # mount point for BindPaths
mkdir -m 0755 /opt/krita-gate
install -o root -g root -m 0644 "$SRC/krita_gate.py" /opt/krita-gate/krita_gate.py
install -o root -g root -m 0755 "$SRC/krita_guard.sh" /opt/krita-gate/krita_guard.sh
install -o root -g root -m 0644 "$SRC/measure_krita.py" /opt/krita-gate/measure_krita.py
install -o root -g root -m 0755 "$SRC/run_probe.sh" /opt/krita-gate/run_probe.sh
install -o root -g root -m 0644 "$SRC/netcheck.py" /opt/krita-gate/netcheck.py
install -o root -g root -m 0644 "$SRC/unit_props.py" /opt/krita-gate/unit_props.py
# Probe dependencies, offline: hash-pinned list + the wheels fetched outside the VM (fetch_probe_wheels.sh).
install -o root -g root -m 0644 "$SRC/probe-requirements.txt" /opt/krita-gate/probe-requirements.txt
( cd "$SRC/wheelhouse" && sha256sum -c --quiet ../wheelhouse.sha256 )
install -d -o root -g root -m 0755 /opt/krita-gate/wheelhouse
for whl in "$SRC"/wheelhouse/*.whl; do install -o root -g root -m 0644 "$whl" /opt/krita-gate/wheelhouse/; done
( cd /opt/krita-gate/wheelhouse && sha256sum -c --quiet "$SRC/wheelhouse.sha256" )
install -o root -g root -m 0644 "$SRC/krita.service" /etc/systemd/system/krita.service
install -o root -g root -m 0644 "$SRC/krita-gate.service" /etc/systemd/system/krita-gate.service
systemctl daemon-reload

echo "installed; nothing started. Next: KRITA.md phase 1."
