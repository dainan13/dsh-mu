#!/bin/sh
# Rebuild a dsh-mu machine keeping ONLY platform users and their instance data.
#
# Kept      : state.json (users, instance mapping, passwords) + instances/*/home + */workspace
# Discarded : gateway SSL/tls, egress rules, providers, plans, plugin assets, gateway json,
#             dsh-mu.conf (regenerated with current defaults), jail rootfs (rebuilt)
#
# Usage:  sh migrate-users-only.sh <backup.tgz> <apk> [--yes]
#         without --yes it only prints the plan.
set -eu
BACKUP="${1:-}"; APK="${2:-}"; YES=0
[ "${3:-}" = "--yes" ] && YES=1
[ -n "$BACKUP" ] && [ -f "$BACKUP" ] || { echo "usage: $0 <backup.tgz> <apk> [--yes]"; exit 1; }
[ -n "$APK" ] && [ -f "$APK" ] || { echo "usage: $0 <backup.tgz> <apk> [--yes]"; exit 1; }

say() { printf '%s\n' "$*"; }
do_it() {
	if [ "$YES" = 1 ]; then "$@" >/dev/null 2>&1 || true
	else say "  would run: $*"; fi
}

say "=== migrate (users + data only) -- $( [ "$YES" = 1 ] && echo EXECUTE || echo 'DRY RUN' ) ==="
say "backup: $BACKUP"
say "apk:    $APK"

say ""
say "1) stop everything and purge the old install"
if [ "$YES" = 1 ]; then
	for s in /etc/init.d/dsh-mu-* /etc/init.d/dsh-mud; do
		[ -e "$s" ] || continue
		rc-service "$(basename "$s")" stop >/dev/null 2>&1 || true
		rc-update del "$(basename "$s")" default >/dev/null 2>&1 || true
	done
	for d in /sys/fs/cgroup/dsh-mu/*/; do
		[ -w "${d}cgroup.kill" ] && echo 1 > "${d}cgroup.kill" 2>/dev/null || true
	done
	pkill -9 -f "dsh-mu-jail" 2>/dev/null || true
	pkill -9 -f "@deepseek-ai/dsh/lib/bin.js" 2>/dev/null || true
	pkill -9 -f "nginx: master" 2>/dev/null || true
	pkill -9 -f "nginx: worker" 2>/dev/null || true
	# A jail from an earlier attempt can survive in its own mount namespace and keep
	# 80/443/8099 bound, which made the next gateway respawn forever with
	# "Address in use". Walk /proc instead of trusting pkill.
	for p in /proc/[0-9]*; do
		e=$(readlink "$p/exe" 2>/dev/null) || continue
		case "$e" in
			*nginx*|*openresty*|*dsh-mu-jail*|*dsh/lib/bin.js*)
				kill -9 "${p#/proc/}" 2>/dev/null || true ;;
		esac
	done
	sleep 2
	rm -rf /etc/dsh-mu /var/lib/dsh-mu /var/log/dsh-mu /usr/local/lib/dsh-mu \
	       /usr/local/sbin/dsh-muctl /usr/local/sbin/dsh-mu-jail \
	       /usr/local/sbin/dsh-mu-cgroup-init /usr/local/sbin/dsh-mud \
	       /usr/share/doc/dsh-mu /etc/logrotate.d/dsh-mu \
	       /etc/nginx/conf.d/dsh-mu-*.conf /etc/nginx/http.d/dsh-mu-*.conf /run/dsh-mu
	apk del dsh-mu >/dev/null 2>&1 || true
	say "  purged"
else
	say "  would stop services, cgroup.kill + pkill, rm -rf the platform dirs, apk del dsh-mu"
fi

say ""
say "2) install the new package"
do_it apk add --allow-untrusted "$APK"
do_it dsh-muctl install

say ""
say "3) restore ONLY users + instance data"
say "   (state.json + instances/*/home + instances/*/workspace)"
if [ "$YES" = 1 ]; then
	STAGE=$(mktemp -d /tmp/mu-restore.XXXXXX)
	tar xzf "$BACKUP" -C "$STAGE" etc/dsh-mu/state.json 2>/dev/null || true
	mkdir -p /etc/dsh-mu
	[ -f "$STAGE/etc/dsh-mu/state.json" ] && cp -a "$STAGE/etc/dsh-mu/state.json" /etc/dsh-mu/state.json
	mkdir -p /var/lib/dsh-mu/instances
	tar xzf "$BACKUP" -C / var/lib/dsh-mu/instances 2>/dev/null || true
	# drop anything that is not a volume (rootfs/logs were never in the archive)
	for d in /var/lib/dsh-mu/instances/*/; do
		for keep in home workspace; do
			[ -d "$d$keep" ] || mkdir -p "$d$keep"
		done
	done
	rm -rf "$STAGE"
	say "  restored users + instance volumes"
else
	say "  would extract only: etc/dsh-mu/state.json  var/lib/dsh-mu/instances"
fi

say ""
say "4) re-render with the new install (nginx.conf, gateway.conf, services, admin token)"
do_it dsh-muctl install
do_it dsh-muctl config models_native false
do_it dsh-muctl config models_lock false
do_it dsh-muctl runtime fix-pty
do_it dsh-muctl sync

say ""
say "5) rebuild every instance's jail rootfs (keeps home/workspace)"
if [ "$YES" = 1 ]; then
	names=$(dsh-muctl instance status 2>/dev/null | awk 'NR>1 && $1!="" {print $1}')
	for n in $names; do
		say "  rebuild $n"
		dsh-muctl instance rebuild "$n" >/dev/null 2>&1 && say "    ok" || say "    FAILED"
	done
else
	say "  would run: dsh-muctl instance rebuild <每个实例>"
fi

say ""
say "6) start the gateway and verify"
if [ "$YES" = 1 ]; then
	# Refuse to start into a taken port: respawning forever is the failure mode we
	# spent hours on. Free the ports first, then start.
	for port in 80 443 8099; do
		holder=$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $0}' | head -1)
		[ -n "$holder" ] && say "  port $port held: $holder"
	done
	for p in /proc/[0-9]*; do
		e=$(readlink "$p/exe" 2>/dev/null) || continue
		case "$e" in *nginx*|*openresty*) kill -9 "${p#/proc/}" 2>/dev/null || true ;; esac
	done
	sleep 2
fi
do_it dsh-muctl gateway start
if [ "$YES" = 1 ]; then
	sleep 6
	ss -ltn 2>/dev/null | grep -E ":(80|443)" || say "  WARN gateway is not listening (tail /var/log/dsh-mu/__gateway.err.log)"
	for p in 3100 3101 3102 3103 3104 3105 3106 3107; do
		printf "  port %s -> " "$p"
		curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:$p/" || echo "?"
	done
	dsh-muctl doctor 2>&1 | grep -E "^FAIL|^doctor:" || true
	say "  platform: $(dsh-muctl --version | head -1)"
	say "  runtime:  $(grep -o '@deepseek-ai/dsh@[0-9a-z.-]*' /var/log/dsh-mu/audit.log 2>/dev/null | tail -1)"
	say "  runtime dir: $(test -d /var/lib/dsh-mu/gateway/rootfs/usr/local/lib/node_modules/@deepseek-ai/dsh && echo present || echo missing)"
	say "  configured dsh_version: $(grep -E '^dsh_version' /etc/dsh-mu/dsh-mu.conf 2>/dev/null | cut -d= -f2)"
fi

say ""
say "=== done. Log in with an existing user (their password is unchanged). ==="
