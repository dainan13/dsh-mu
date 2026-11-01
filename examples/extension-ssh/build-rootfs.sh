#!/bin/sh
# Build this extension's own rootfs. The host stays untouched: everything the
# appliance needs (python3 in this example) is installed *inside* the rootfs.
#
#   sh build-rootfs.sh                 # 从 Alpine 镜像构建（需要网络）
#   DSHMU_EXT_TEST_ROOTFS=/path \
#     sh build-rootfs.sh               # 开发期复用平台已有的 jail 模板（不联网）
set -eu
EXT=data_placeholder
NAME=ssh
TARGET=/var/lib/dsh-mu/extensions/$NAME/rootfs
ALPINE=https://dl-cdn.alpinelinux.org/alpine
VER=${ALPINE_VERSION:-3.22}
MIRROR=${ALPINE_MIRROR:-$ALPINE/v$VER}

if [ -n "${DSHMU_EXT_TEST_ROOTFS:-}" ]; then
	echo "linking $TARGET -> $DSHMU_EXT_TEST_ROOTFS (test mode)"
	mkdir -p "$(dirname "$TARGET")"
	rm -rf "$TARGET"
	ln -s "$DSHMU_EXT_TEST_ROOTFS" "$TARGET"
	exit 0
fi

mkdir -p /var/lib/dsh-mu/extensions/$NAME
rm -rf "$TARGET"; mkdir -p "$TARGET"
TMP=$(mktemp -d)
curl -fsSL "$MIRROR/releases/$(uname -m)/alpine-minirootfs-$VER.0-$(uname -m).tar.gz" -o "$TMP/r.tgz"
tar xzf "$TMP/r.tgz" -C "$TARGET"
cp /etc/resolv.conf "$TARGET/etc/resolv.conf"
chroot "$TARGET" /bin/sh -c "
  echo '$MIRROR/main' > /etc/apk/repositories
  echo '$MIRROR/community' >> /etc/apk/repositories
  apk add --no-cache python3 py3-asyncssh openssh-client tzdata
"
rm -rf "$TMP"
echo "rootfs ready: $TARGET"
