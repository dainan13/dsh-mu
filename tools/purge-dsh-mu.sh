#!/bin/sh
# Remove everything this platform put on the machine, so a reinstall starts clean.
#
# Defaults to a DRY RUN: it prints what it would delete. Add --yes to actually delete.
set -eu
YES=0
[ "${1:-}" = "--yes" ] && YES=1

say() { printf '%s\n' "$*"; }
act() {
	if [ "$YES" = 1 ]; then
		rm -rf "$1" && say "  removed $1"
	else
		say "  would remove $1"
	fi
}

say "dsh-mu purge -- $( [ "$YES" = 1 ] && echo EXECUTE || echo 'dry run (add --yes)' )"
say ""
say "1) stop services"
for f in /etc/init.d/dsh-mu-*; do
	[ -e "$f" ] || continue
	n=$(basename "$f")
	if [ "$YES" = 1 ]; then
		rc-service "$n" stop >/dev/null 2>&1 || true
		rc-update del "$n" default >/dev/null 2>&1 || true
		rc-update del "$n" boot >/dev/null 2>&1 || true
		say "  stopped+unregistered $n"
	else
		say "  would stop+unregister $n"
	fi
done

say ""
say "2) package + program files"
if [ "$YES" = 1 ]; then
	apk del dsh-mu >/dev/null 2>&1 || true
fi
for p in /usr/local/lib/dsh-mu /usr/local/sbin/dsh-mu-jail /usr/local/sbin/dsh-mu-cgroup-init \
         /etc/init.d/dsh-mu-cgroup /etc/logrotate.d/dsh-mu /etc/nginx/conf.d/dsh-mu-*.conf \
         /etc/nginx/http.d/dsh-mu-*.conf /usr/share/doc/dsh-mu; do
	[ -e "$p" ] && act "$p"
done

say ""
say "3) platform state, instances, gateway jail, logs, backups"
for p in /etc/dsh-mu /var/lib/dsh-mu /var/log/dsh-mu /var/backups/dsh-mu-*; do
	[ -e "$p" ] && act "$p"
done

say ""
say "4) leftover cgroup tree (recreated by the service on the next install)"
if [ -d /sys/fs/cgroup/dsh-mu ]; then
	if [ "$YES" = 1 ]; then
		rmdir /sys/fs/cgroup/dsh-mu 2>/dev/null && say "  removed /sys/fs/cgroup/dsh-mu" \
			|| say "  /sys/fs/cgroup/dsh-mu left in place (busy?)"
	else
		say "  would remove /sys/fs/cgroup/dsh-mu"
	fi
fi

say ""
say "5) OS users created for instances are left alone (their passwords are theirs)"
say ""
if [ "$YES" = 1 ]; then
	say "done. reinstall: apk add --allow-untrusted <apk> && dsh-muctl install"
else
	say "re-run with --yes to perform the removals above."
fi
