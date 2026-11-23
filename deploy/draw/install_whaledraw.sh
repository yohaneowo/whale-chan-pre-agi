#!/bin/sh
# Install whaledraw read-only for the dsh user. Run by the maintenance account with sudo on VM 102:
#   sudo sh install_whaledraw.sh <folder with the deploy/draw files> [version]
# The VM has no pip, so each wheel is downloaded, checked against the sha256 in wheels-vm.txt,
# and unpacked into lib/ (a wheel is a zip; these need no install scripts).
# Nothing is overwritten: a new version always goes into a new folder.
set -eu

BASE=/opt/dsh
SRC=$1
VERSION=${2-0.1.0}   # an explicit empty version is rejected below

# The version becomes part of a path that root deletes and writes, so it must be a plain name:
# letters, digits, dot, underscore, dash; no slash; not empty; not starting with a dot.
case $VERSION in
	'' | .* | */* | *[!0-9A-Za-z._-]*)
		echo "invalid version: '$VERSION' (allowed: [0-9A-Za-z._-], not starting with '.')" >&2
		exit 2
		;;
esac
DEST=$(realpath -m "$BASE/whaledraw-$VERSION")
PART=$(realpath -m "$BASE/whaledraw-$VERSION.part")
case $DEST in "$BASE"/whaledraw-?*) ;; *) echo "refusing: $DEST is not under $BASE/" >&2; exit 2 ;; esac
case $PART in "$BASE"/whaledraw-?*.part) ;; *) echo "refusing: $PART is not under $BASE/" >&2; exit 2 ;; esac
[ "$(dirname "$DEST")" = "$BASE" ] && [ "$(dirname "$PART")" = "$BASE" ] || { echo "refusing: not directly under $BASE" >&2; exit 2; }

if [ -e "$DEST" ]; then
	echo "already installed: $DEST" >&2
	exit 1
fi

# On any failure, remove only the .part folder this run created.
created=
cleanup() {
	status=$?
	if [ "$status" -ne 0 ] && [ -n "$created" ] && [ -d "$PART" ]; then
		rm -rf -- "$PART"
		echo "failed; removed $PART" >&2
	fi
	exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

rm -rf -- "$PART"   # a leftover from an earlier interrupted run; path checked above
created=1           # set before mkdir, so a half-made .part is cleaned up too
mkdir -p "$PART/lib" "$PART/wheels"

grep -v '^#' "$SRC/wheels-vm.txt" | while read -r sum url; do
	[ -n "$sum" ] || continue
	file="$PART/wheels/$(basename "$url")"
	curl -fsSL --retry 2 -o "$file" "$url"
	echo "$sum  $file" | sha256sum -c --quiet -
	python3 -I -m zipfile -e "$file" "$PART/lib/"
done
rm -rf -- "$PART/wheels"

cp -r "$SRC/whaledraw" "$PART/"
cp "$SRC/halloween.py" "$SRC/test_whaledraw.py" "$SRC/API.md" "$PART/"
find "$PART" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$PART"
find "$PART" -type d -exec chmod 755 {} +
find "$PART" -type f -exec chmod 644 {} +
mv "$PART" "$DEST"
created=
echo "installed $DEST"
