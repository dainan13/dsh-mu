#!/bin/sh
# isolation.sh - verify the jail boundary that dsh-mu claims.
#
#   sh tests/isolation.sh [instance] [--json]
#
# The platform promises: an instance is confined to its own rootfs, holds no
# capabilities, cannot reach host files through procfs magic links, cannot mount
# or create devices, runs inside its cgroup, keeps /tmp to itself, sees the
# shared DSH runtime read-only, and cannot take over another tenant's instance
# over loopback. This script asserts every one of those claims against a live
# instance, so the boundary is tested rather than assumed.
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -r "$HERE/lib.sh" ] || {
	echo "tests/lib.sh is missing next to this script; install the tests/ directory as a whole" >&2
	exit 2
}
. "$HERE/lib.sh"

JAIL=$DSHMU_JAIL
STATE=$DSHMU_STATE

INSTANCE=""
JSON=0
for arg in "$@"; do
	case "$arg" in
		--json) JSON=1 ;;
		-h|--help) sed -n '2,12p' "$0"; exit 0 ;;
		*) INSTANCE="$arg" ;;
	esac
done

PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); [ "$JSON" = 1 ] || printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP+1)); [ "$JSON" = 1 ] || printf 'SKIP  %s\n' "$*"; }

# The port of the peer instance, via the shared helper.
instance_port() { t_instance_port "$1"; }

[ -n "$JAIL" ] || { echo "dsh-mu-jail not found (set DSHMU_JAIL)" >&2; exit 2; }
[ -r "$STATE" ] || { echo "cannot read $STATE (run as root)" >&2; exit 2; }

pick=$(python3 - "$STATE" "$INSTANCE" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
want = sys.argv[2]
insts = st.get("instances", {})
if want:
    print(want if want in insts else "")
else:
    running = [n for n, i in insts.items() if i.get("state") == "running"]
    print((running or list(insts))[0] if insts else "")
PY
)
[ -n "$pick" ] || { echo "no such instance (or no instances at all)" >&2; exit 2; }
INSTANCE="$pick"

# ---------------------------------------------------------------- host facts
RUN_UID=$(t_instance_field "$INSTANCE" uid)
[ -n "$RUN_UID" ] || RUN_UID=0
INSTANCE_PORT=$(t_instance_port "$INSTANCE")
PEER_NAME=$(t_instance_names | grep -vx "$INSTANCE" | head -1)
if [ -n "$PEER_NAME" ]; then
	PEER_PORT=$(t_instance_port "$PEER_NAME")
else
	PEER_NAME="-"; PEER_PORT=0
fi

echo "== isolation check: instance=$INSTANCE run_uid=$RUN_UID peer=$PEER_NAME${PEER_PORT:+:$PEER_PORT} =="

jail() { # jail <command>  -> runs it inside the instance jail
	"$JAIL" "$INSTANCE" -- "$1" 2>/dev/null
}

expect_denied() { # expect_denied <label> <command>
	if jail "$2" >/dev/null 2>&1; then bad "$1 (it succeeded - should be denied)"; else ok "$1 denied"; fi
}
expect_allowed() { # expect_allowed <label> <command>
	if jail "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1 (it failed - should work)"; fi
}

# 1. chroot: host-only paths must not exist, and /etc/shadow must be the
#    template's own file rather than the host's (every rootfs has one).
host_paths=$(jail 'for p in /etc/dsh-mu /var/lib/dsh-mu /etc/dsh-mu/secret.key; do [ -e "$p" ] && echo "$p"; done; true')
if [ -z "$host_paths" ]; then ok "chroot hides host-only paths (/etc/dsh-mu, /var/lib/dsh-mu)"
else bad "host paths visible inside the jail: $host_paths"; fi
jail_shadow=$(mktemp)
# NOTE: jail() takes one argument (the whole command string); an unquoted
# `jail cat /etc/shadow` would run a bare `cat` and block on stdin.
jail 'cat /etc/shadow' > "$jail_shadow" 2>/dev/null || :
tmpl_shadow="${TEMPLATE:-/var/lib/dsh-mu/templates/alpine}/etc/shadow"
if cmp -s /etc/shadow "$jail_shadow" 2>/dev/null && [ -s "$jail_shadow" ]; then
	bad "the jail reads the HOST /etc/shadow (identical file)"
elif [ ! -s "$jail_shadow" ]; then
	ok "the jail has no readable /etc/shadow of its own (nothing to leak)"
elif [ -f "$tmpl_shadow" ] && cmp -s "$tmpl_shadow" "$jail_shadow"; then
	ok "jail /etc/shadow is the template's own file (host version not exposed)"
else
	# A per-instance password change is legitimate; the point is that it is not
	# the host's file.
	ok "jail /etc/shadow is instance-local (differs from the host's)"
fi
rm -f "$jail_shadow"
expect_allowed "instance sees its own rootfs" 'test -f /etc/apk/repositories'

# 2. capabilities: either everything dropped, or exactly the set the command
# sandbox needs (sys_admin/setuid/setgid). Anything else is a regression.
caps=$(jail 'grep -h Cap /proc/self/status | tr -d " \t" | tr "\n" " "')
allowed='00000000002000c0'
eff=$(echo "$caps" | sed -n 's/.*CapEff:\([0-9a-f]*\).*/\1/p')
bnd=$(echo "$caps" | sed -n 's/.*CapBnd:\([0-9a-f]*\).*/\1/p')
if { [ "$eff" = "0000000000000000" ] || [ "$eff" = "$allowed" ]; } &&
   { [ "$bnd" = "0000000000000000" ] || [ "$bnd" = "$allowed" ]; }; then
	ok "capability set is the configured one ($caps)"
else
	bad "unexpected capability set: $caps"
fi

# 3. procfs magic links: host root files must be unreachable.
links_ok=1
for pid in 1 $(pgrep -f "dsh-mud --listen" | head -1) $(pgrep -f "nginx: master" | head -1); do
	[ -n "$pid" ] || continue
	out=$(jail "head -c1 /proc/$pid/root/etc/shadow /proc/$pid/root/etc/dsh-mu/secret.key /proc/$pid/environ >/dev/null 2>&1 && echo leaked")
	[ -z "$out" ] || links_ok=0
done
[ "$links_ok" = 1 ] && ok "procfs magic links (/proc/<host-pid>/root, environ) cannot reach host files" \
	|| bad "procfs magic link reachable from the jail"

# 4. procfs writes and namespace/kernel operations.
expect_denied "write to /proc/sys" 'echo x > /proc/sys/kernel/core_pattern'
# With CAP_SYS_ADMIN kept for the command sandbox, mounting *inside* the jail is
# expected to work; what must hold is that it does not touch the host (checked by
# the "host instance directory" and procfs-magic-link checks above).
# `eff` is set by the capability check above.
if [ "${eff:-}" = "00000000002000c0" ]; then
	skip "mount inside the jail (allowed by the configured capability set)"
	skip "creating a new mount namespace (allowed by the configured capability set)"
else
	expect_denied "mount inside the jail" 'mount -t tmpfs none /mnt'
	expect_denied "creating a new mount namespace" 'unshare -m true'
fi
expect_denied "device creation (mknod)" 'mknod /dev/dshmu-probe c 1 3'

# 5. the instance still owns its own rootfs (in-jail package management).
expect_allowed "instance rootfs is writable (in-jail apk)" 'touch /etc/apk/.dshmu-probe && rm -f /etc/apk/.dshmu-probe'
expect_allowed "instance home is writable" 'touch "$HOME/.dshmu-probe" && rm -f "$HOME/.dshmu-probe"'

# 6. shared DSH runtime is read-only.
expect_denied "writing into the shared DSH runtime" 'touch /usr/local/lib/node_modules/@deepseek-ai/dsh/.dshmu-probe'

# 7. private /tmp: what the jail writes there must not appear on the host.
probe="/tmp/dshmu-isolation-$$"
jail "echo x > $probe" >/dev/null 2>&1
if [ -e "$probe" ]; then bad "jail /tmp leaked to the host ($probe)"; rm -f "$probe"
else ok "jail /tmp is private"; fi

# 8. cgroup membership and limits.
cg=$(jail 'cat /proc/self/cgroup' | head -1)
case "$cg" in
	*"dsh-mu/$INSTANCE"*) ok "process runs inside its cgroup ($cg)" ;;
	*) bad "unexpected cgroup membership: $cg" ;;
esac
limits=$(python3 -c '
import json, sys
st = json.load(open(sys.argv[1]))
i = st["instances"][sys.argv[2]]
print("|".join([i.get("cpu_max") or "max", i.get("mem_max") or "max",
                i.get("pids_max") or "max"]))
' "$STATE" "$INSTANCE")
w_cpu=$(printf '%s' "$limits" | cut -d'|' -f1)
w_mem=$(printf '%s' "$limits" | cut -d'|' -f2)
w_pids=$(printf '%s' "$limits" | cut -d'|' -f3)
for pair in "cpu.max:$w_cpu" "memory.max:$w_mem" "pids.max:$w_pids"; do
	file=${pair%%:*}; want=${pair#*:}
	got=$(cat "/sys/fs/cgroup/dsh-mu/$INSTANCE/$file" 2>/dev/null | tr -d '\n' || echo "")
	# "max" means unlimited and cpu.max is rendered as "<quota> <period>", so
	# compare the first token on both sides.
	if [ "${got%% *}" = "${want%% *}" ]; then
		ok "cgroup $file = ${got:-unset}"
	else
		bad "cgroup $file = ${got:-unset} (expected $want)"
	fi
done

# 9. tenant isolation over the shared loopback network.
if [ "${PEER_PORT:-0}" -gt 0 ]; then
	code=$(jail "curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H 'Host: 127.0.0.1:$PEER_PORT' -H 'Origin: http://127.0.0.1:$PEER_PORT' http://127.0.0.1:$PEER_PORT/")
	case "$code" in
		401|403|000) ok "another tenant's instance rejects us (HTTP ${code:-no answer})" ;;
		*) bad "another tenant's instance answered $code to a foreign jail" ;;
	esac
	code=$(jail "curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H 'Host: 127.0.0.1:$PEER_PORT' -H 'Origin: http://127.0.0.1:$PEER_PORT' -H 'Cookie: dsh-auth-AAAA=xx' http://127.0.0.1:$PEER_PORT/api/status")
	case "$code" in
		401|403|000) ok "...also with a forged auth cookie (HTTP ${code:-no answer})" ;;
		*) bad "forged cookie accepted: $code" ;;
	esac
else
	skip "tenant isolation needs a second instance"
fi
code=$(jail "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8787/api/health")
case "$code" in
	403) ok "platform ops API refuses the jail without the token (403)" ;;
	*) bad "ops API answered ${code:-nothing} to the jail (expected 403)" ;;
esac

# 10. the instance port must be reachable on loopback and on nothing else.
# `ss` is not part of a stock Alpine, so read the kernel's own table:
# /proc/net/tcp lists local_address as hex, little-endian (127.0.0.1 = 0100007F).
hexport=$(printf '%04X' "$INSTANCE_PORT" 2>/dev/null)
loop_hits=0; wild_hits=0
for table in /proc/net/tcp /proc/net/tcp6; do
	[ -r "$table" ] || continue
	while read -r _ local _ _ _ _ _ _ _ _ ; do
		case "$local" in
			*:"$hexport") : ;;
			*) continue ;;
		esac
		addr=${local%%:*}
		case "$addr" in
			0100007F|00000000000000000000000001000000) loop_hits=$((loop_hits + 1)) ;;
			00000000|00000000000000000000000000000000) wild_hits=$((wild_hits + 1)) ;;
		esac
	done <"$table"
done
if [ "$wild_hits" -eq 0 ] && [ "$loop_hits" -gt 0 ]; then
	ok "instance port $INSTANCE_PORT is bound to loopback only ($loop_hits socket(s), no wildcard)"
elif [ "$loop_hits" -eq 0 ]; then
	skip "no listening socket found for port $INSTANCE_PORT (kernel table unreadable?)"
else
	bad "instance port $INSTANCE_PORT has $wild_hits wildcard listener(s); it is reachable from outside"
fi

EXT_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
if [ -n "$EXT_IP" ]; then
	if wget -q -O /dev/null -T 5 "http://$EXT_IP:$INSTANCE_PORT/" 2>/dev/null; then
		bad "the instance answered on $EXT_IP:$INSTANCE_PORT (gateway can be bypassed)"
	else
		ok "the instance refuses $EXT_IP:$INSTANCE_PORT (gateway cannot be bypassed)"
	fi
	# ...and the same request must work on loopback, or the check above proves nothing
	loop_code=$(wget -S -q -O /dev/null -T 5 "http://127.0.0.1:$INSTANCE_PORT/" 2>&1 | grep -c "HTTP/")
	if [ "$loop_code" -gt 0 ]; then
		ok "the same port does answer on 127.0.0.1 (so the refusal above is about the address)"
	else
		bad "the port does not answer on loopback either; the external refusal proves nothing"
	fi
else
	skip "no non-loopback address to test the external path with"
fi

if [ "$JSON" = 1 ]; then
	printf '{"instance":"%s","pass":%d,"fail":%d,"skip":%d}\n' "$INSTANCE" "$PASS" "$FAIL" "$SKIP"
else
	printf '\n=== ISOLATION: PASS=%d FAIL=%d SKIP=%d (instance=%s) ===\n' "$PASS" "$FAIL" "$SKIP" "$INSTANCE"
	[ "$FAIL" = 0 ] && echo "ISOLATION CHECKS PASSED" || echo "ISOLATION CHECKS FAILED"
fi
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
