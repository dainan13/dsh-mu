#!/bin/sh
# make-apk.sh - build the dsh-mu platform as a single Alpine package.
#
#   sh packaging/make-apk.sh [-o OUTDIR] [--version X.Y.Z] [--signed]
#
# Default: **unsigned** v3 package, built with `apk mkpkg` from apk-tools 3.
#   * no signing key, no abuild, no network needed
#   * works on any host with apk-tools >= 3 (this checkout's Alpine, a CI
#     container, ...), so the package we ship is the package we test
#   * install it with:  apk add --allow-untrusted dsh-mu-<ver>-r0.apk
#
# --signed: use Alpine's abuild with packaging/APKBUILD instead (generates a
#   local key). Only useful if you want plain `apk add` to work on the build
#   host; the published artifact stays unsigned either way.
#
# The old hand-written v2 packer is gone on purpose: apk-tools 3 (Alpine 3.22+)
# rejects v2 packages with "v2 package format error", so it could not produce
# anything installable on a current target.
#
# The ~140 MB jail template is deliberately NOT part of the package: it is
# downloaded and built on the target by `dsh-muctl install`.
set -eu

SRC=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT="$SRC/dist"
VERSION=""
PKGNAME="dsh-mu"
MODE="mkpkg"
PKGREL="r0"

while [ $# -gt 0 ]; do
	case "$1" in
		-o|--out) OUT="$2"; shift 2 ;;
		--version) VERSION="$2"; shift 2 ;;
		--signed|--abuild) MODE="abuild"; shift ;;
		-h|--help) sed -n '2,22p' "$0"; exit 0 ;;
		*) echo "make-apk.sh: unknown argument: $1" >&2; exit 2 ;;
	esac
done

if [ -z "$VERSION" ]; then
	if [ -f "$SRC/VERSION" ]; then
		VERSION=$(cat "$SRC/VERSION")
	elif command -v git >/dev/null 2>&1 && git -C "$SRC" rev-parse --short HEAD >/dev/null 2>&1; then
		VERSION="0.1.0_git$(git -C "$SRC" rev-parse --short HEAD)"
	else
		VERSION="0.1.0"
	fi
fi

say() { printf 'make-apk: %s\n' "$*"; }
die() { printf 'make-apk: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

mkdir -p "$OUT"

# ------------------------------------------------------------------- abuild
build_with_abuild() {
	have abuild || die "abuild not found (apk add abuild), or drop --signed"

	if ! ls "$HOME"/.abuild/*.rsa >/dev/null 2>&1; then
		say "generating an abuild signing key (abuild-keygen -a -n)"
		abuild-keygen -a -n >/dev/null 2>&1 || die "abuild-keygen failed"
	fi
	key=$(ls "$HOME"/.abuild/*.rsa 2>/dev/null | head -1) || true
	if [ -n "${key:-}" ]; then
		mkdir -p "$HOME/.abuild"
		if ! grep -qs PACKAGER_PRIVKEY "$HOME/.abuild/abuild.conf" 2>/dev/null; then
			printf 'PACKAGER_PRIVKEY=%s\n' "$key" > "$HOME/.abuild/abuild.conf"
			say "wrote PACKAGER_PRIVKEY to ~/.abuild/abuild.conf"
		fi
	fi
	[ -f "$SRC/packaging/APKBUILD" ] && sed -i "s/^pkgver=.*/pkgver=$VERSION/" "$SRC/packaging/APKBUILD"

	say "building with abuild (packaging/APKBUILD, signed)"
	repodest="${TMPDIR:-/tmp}/dsh-mu-repo.$$"
	rm -rf "$repodest"; mkdir -p "$repodest"
	( cd "$SRC/packaging" && abuild -F -f -q -P "$repodest" rootpkg ) || die "abuild rootpkg failed"
	built=$(find "$repodest" -name "${PKGNAME}-*.apk" | head -1)
	[ -n "$built" ] || die "abuild produced no package under $repodest"
	target="$OUT/${PKGNAME}-${VERSION}-${PKGREL}.apk"
	cp "$built" "$target"
	rm -rf "$repodest"
	say "done: $target ($(du -h "$target" | awk '{print $1}'))"
}

# -------------------------------------------------------- unsigned v3 (mkpkg)
build_unsigned() {
	have apk || die "apk not found: the unsigned packer needs apk-tools 3"
	# `apk mkpkg --help` exits 1, so probe the subcommand list instead.
	apk --help 2>&1 | grep -q 'mkpkg' || die "this apk has no mkpkg (apk-tools < 3?)"

	STAGE=$(mktemp -d "${TMPDIR:-/tmp}/dsh-mu-apk.XXXXXX")
	trap 'rm -rf "$STAGE"' EXIT INT TERM
	ROOT="$STAGE/root"

	install_file() { # install_file <mode> <src> <dest-inside-package>
		_dst="$ROOT$3"
		mkdir -p "$(dirname "$_dst")"
		install -m "$1" "$2" "$_dst"
	}

	install_file 0755 "$SRC/cli/dsh-muctl"           /usr/local/sbin/dsh-muctl
	install_file 0755 "$SRC/jail/dsh-mu-jail"        /usr/local/sbin/dsh-mu-jail
	install_file 0755 "$SRC/jail/dsh-mu-cgroup-init" /usr/local/sbin/dsh-mu-cgroup-init
	install_file 0755 "$SRC/admin/dsh-mud"           /usr/local/sbin/dsh-mud
	install_file 0644 "$SRC/cli/dshmuctl.py"         /usr/local/lib/dsh-mu/python/dshmuctl.py
	for f in "$SRC"/gateway/lua/*.lua; do
		install_file 0644 "$f" "/usr/local/lib/dsh-mu/lua/$(basename "$f")"
	done
	for f in "$SRC"/gateway/dsh-mu-*.conf; do
		install_file 0644 "$f" "/usr/local/lib/dsh-mu/nginx/$(basename "$f")"
	done
	install_file 0644 "$SRC/admin/index.html" /usr/local/lib/dsh-mu/admin/index.html
	for f in "$SRC"/gateway/static/*; do
		install_file 0644 "$f" "/usr/local/lib/dsh-mu/static/$(basename "$f")"
	done
	# Every test file ships: the python layers (fresh-machine.py, drift-audit.py,
	# acceptance.sh's helpers, ...) were silently left out before, which would have
	# made a clean installed machine unable to run the acceptance on itself.
	for f in "$SRC"/tests/*; do
		[ -f "$f" ] || continue
		install_file 0644 "$f" "/usr/local/lib/dsh-mu/tests/$(basename "$f")"
	done
	for doc in README.md DELIVERY.md CHANGELOG.md; do
		[ -f "$SRC/$doc" ] && install_file 0644 "$SRC/$doc" "/usr/share/doc/dsh-mu/$doc"
	done
	if [ -d "$SRC/docs" ]; then
		for f in "$SRC"/docs/*.md; do
			[ -e "$f" ] || continue
			install_file 0644 "$f" "/usr/share/doc/dsh-mu/docs/$(basename "$f")"
		done
	fi

	target="$OUT/${PKGNAME}-${VERSION}-${PKGREL}.apk"
	say "packing $target (unsigned v3, apk mkpkg)"

	# One -I per metadata field. -s TYPE:<file> embeds the script *content*
	# (verified: the package still runs its scripts after the source is deleted).
	set -- \
		-I "name:${PKGNAME}" \
		-I "version:${VERSION}-${PKGREL}" \
		-I "description:DeepSeek Harness multi-instance platform (gateway, jail runner, admin API)" \
		-I "url:https://github.com/deepseek-ai/dsh" \
		-I "license:MIT" \
		-I "arch:noarch" \
		-I "origin:${PKGNAME}" \
		-I "maintainer:dsh-mu maintainers <maintainers@dsh-mu.invalid>"
	# openresty and the lua-resty-* modules live inside the platform jail (built
	# from the image + ALPINE_TEMPLATE_PACKAGES), so the host does not need them.
	# NOTE: apk mkpkg keeps only the LAST `-I depends:` value, and a comma list is
	# parsed as one dependency name -- it must be a single space-separated value.
	# node is NOT a host dependency any more: the DSH runtime is installed inside
	# the gateway template (see install_dsh_runtime()).
	set -- "$@" -I "depends:python3 util-linux logrotate curl ca-certificates openssl"
	for action in pre-install post-install pre-upgrade post-upgrade pre-deinstall post-deinstall; do
		[ -f "$SRC/packaging/dsh-mu.$action" ] && set -- "$@" -s "$action:$SRC/packaging/dsh-mu.$action"
	done

	apk mkpkg -F "$ROOT" -o "$target" "$@" || die "apk mkpkg failed"
	say "done: $target ($(du -h "$target" | awk '{print $1}'))"
	say "install with: apk add --allow-untrusted $target"
}

case "$MODE" in
	abuild) build_with_abuild ;;
	mkpkg) build_unsigned ;;
esac
