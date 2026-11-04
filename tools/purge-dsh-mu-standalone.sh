#!/bin/sh
# Remove every trace of dsh-mu from this machine, so a reinstall starts clean.
#
# Self-contained: it does NOT need the dsh-mu package (so it works on a half-broken
# machine). Default is a DRY RUN; pass --yes to actually remove things.
#
# What it does, in order:
#   1. stops the OpenRC services and unregisters them
#   2. kills what survived the stop (jail wrappers, jailed nginx, DSH node, admin backend)
#   3. removes the cgroup tree
#   4. removes every directory and file the platform created
#   5. leaves OS accounts alone (instances run as uid 0; users live in state.json)
set -eu
YES=0
[ "${1:-}" = "--yes" ] && YES=1

say() { printf '%s\n' "$*"; }
run() {
	if [ "$YES" = 1 ]; then
		"$@" >/dev/null 2>&1 || true
	else
		say "    would run: $*"
	fi
}
remove() {
	for p in "$@"; do
		[ -e "$p" ] || continue
		if [ "$YES" = 1 ]; then
			rm -rf "$p" && say "  removed $p"
		else
			say "  would remove $p"
		fi
	done
}

say "=== dsh-mu purge -- $( [ "$YES" = 1 ] && echo EXECUTE || echo 'DRY RUN (add --yes)' ) ==="

say ""
say "1) services"
for f in /etc/init.d/dsh-mu-* /etc/init.d/dsh-mud; do
	[ -e "$f" ] || continue
	n=$(basename "$f")
	run rc-service "$n" stop
	run rc-update del "$n" boot
	run rc-update del "$n" default
	say "  handled $n"
done
[ -e /etc/init.d/dsh-mud ] && run rc-service dsh-mud stop

say ""
say "2) surviving processes"
say "  before:"
ps aux 2>/dev/null | grep -E "[d]sh-mu-jail|[n]ginx: master|[@]deepseek-ai/dsh/lib/bin.js|[d]sh-mud --" | sed 's/^/    /' || true
if [ "$YES" = 1 ]; then
	for d in /sys/fs/cgroup/dsh-mu/*/; do
		[ -w "${d}cgroup.kill" ] && echo 1 > "${d}cgroup.kill" 2>/dev/null || true
	done
	pkill -9 -f "dsh-mu-jail" 2>/dev/null || true
	pkill -9 -f "@deepseek-ai/dsh/lib/bin.js" 2>/dev/null || true
	pkill -9 -f "nginx: master" 2>/dev/null || true
	pkill -9 -f "nginx: worker" 2>/dev/null || true
	pkill -9 -f "dsh-mud --" 2>/dev/null || true
	sleep 2
	left=$(ps aux 2>/dev/null | grep -cE "[d]sh-mu-jail|[n]ginx: master|[@]deepseek-ai/dsh/lib/bin.js|[d]sh-mud --" || true)
	say "  after: $left process(es) left"
else
	say "  would cgroup.kill each tree, then pkill: dsh-mu-jail / dsh lib/bin.js / nginx / dsh-mud"
fi

say ""
say "3) cgroup tree"
for d in /sys/fs/cgroup/dsh-mu/*/; do
	[ -d "$d" ] || continue
	if [ "$YES" = 1 ]; then
		rmdir "$d" 2>/dev/null && say "  removed $d" || say "  kept $d (busy)"
	else
		say "  would remove $d"
	fi
done
if [ -d /sys/fs/cgroup/dsh-mu ]; then
	if [ "$YES" = 1 ]; then
		rmdir /sys/fs/cgroup/dsh-mu 2>/dev/null && say "  removed /sys/fs/cgroup/dsh-mu" \
			|| say "  kept /sys/fs/cgroup/dsh-mu (has other content?)"
	else
		say "  would remove /sys/fs/cgroup/dsh-mu"
	fi
fi

say ""
say "4) directories and files"
remove /etc/dsh-mu \
       /var/lib/dsh-mu \
       /var/log/dsh-mu \
       /usr/local/lib/dsh-mu \
       /usr/local/sbin/dsh-muctl \
       /usr/local/sbin/dsh-mu-jail \
       /usr/local/sbin/dsh-mu-cgroup-init \
       /usr/local/sbin/dsh-mud \
       /usr/share/doc/dsh-mu \
       /etc/logrotate.d/dsh-mu \
       /etc/nginx/conf.d/dsh-mu-http.conf \
       /etc/nginx/conf.d/dsh-mu-server.conf \
       /etc/nginx/http.d/dsh-mu-http.conf \
       /etc/nginx/http.d/dsh-mu-server.conf \
       /run/dsh-mu \
       /var/backups/dsh-mu-*
for f in /var/backups/dsh-mu-*.tgz; do [ -e "$f" ] || continue; remove "$f"; done
for f in /tmp/dsh-mu-jail.*; do [ -e "$f" ] || continue; remove "$f"; done

say ""
say "5) the package itself (skip if already removed)"
if [ "$YES" = 1 ]; then
	apk del dsh-mu >/dev/null 2>&1 || true
	say "  apk del dsh-mu  (done)"
else
	say "  would run: apk del dsh-mu"
fi

say ""
say "6) left alone on purpose"
say "  · OS accounts (instances run as uid 0; platform users live in state.json)"
say "  · the openresty/nodejs packages themselves (shared with other things)"

say ""
if [ "$YES" = 1 ]; then
	say "=== purge done. Reinstall: apk add --allow-untrusted <apk> && dsh-muctl install ==="
else
	say "=== dry run finished. Re-run with --yes to perform it. ==="
fi
