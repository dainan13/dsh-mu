#!/bin/sh
# doctor-fix.sh - does the repair button actually repair?
#
#   sh tests/doctor-fix.sh
#
# `dsh-muctl doctor --fix` (and `sync-state`) claim to bring a drifted platform
# back in line. This breaks four repairable things, asserts doctor *reports* each
# one, runs the documented repair, and asserts doctor goes clean again - then puts
# everything back. A repair nobody tested is a promise, not a feature.
#
# Breakages (all reversible, all on a live host):
#   1. the cgroup delegation service loses its boot registration
#   2. the gateway configuration is tampered with (host nginx temp dir or the jail's fragment)
#   3. a rendered gateway view is deleted
#   4. an instance record says "stopped" while the instance is actually running
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -r "$HERE/lib.sh" ] || {
	echo "tests/lib.sh is missing next to this script; install the tests/ directory as a whole" >&2
	exit 2
}
. "$HERE/lib.sh"

t_require_root || exit 2
[ -n "$DSHMU_CLI" ] || { echo "dsh-muctl not found" >&2; exit 2; }

INSTANCE=$(t_first_instance)
[ -n "$INSTANCE" ] || { echo "no instances to test with" >&2; exit 2; }
PORT=$(t_instance_port "$INSTANCE")

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); t_ok "$@"; }
bad() { FAIL=$((FAIL+1)); t_bad "$@"; }

doctor_line() { # doctor_line -> "N PASS, N WARN, N FAIL"
	"$DSHMU_CLI" doctor 2>/dev/null | grep -E '^doctor:' | tail -1
}
doctor_has() { # doctor_has <pattern> [status]
	if [ -n "${2:-}" ]; then
		"$DSHMU_CLI" doctor 2>/dev/null | grep -E "^($2)[[:space:]]+$1" >/dev/null
	else
		"$DSHMU_CLI" doctor 2>/dev/null | grep -E "$1" >/dev/null
	fi
}

echo "== doctor --fix: repair verification (instance=$INSTANCE:$PORT) =="
echo "   starting state: $(doctor_line)"

# ---------------------------------------------------------------- 1. boot link
had_link=0
[ -e /etc/runlevels/boot/dsh-mu-cgroup ] && had_link=1
rm -f /etc/runlevels/boot/dsh-mu-cgroup
if doctor_has "cgroup service at boot" "WARN|FAIL"; then
	ok "1 doctor notices the missing boot registration"
else
	bad "1 doctor did not notice the missing boot registration"
fi
"$DSHMU_CLI" doctor --fix >/dev/null 2>&1
if [ -e /etc/runlevels/boot/dsh-mu-cgroup ]; then
	ok "1 doctor --fix re-registered dsh-mu-cgroup for boot"
else
	bad "1 doctor --fix did not re-register the boot link"
fi
doctor_has "cgroup service at boot" "PASS" && ok "1 the check is green again" \
	|| bad "1 the check is still not green"

# ------------------------------------------------------------- 2. nginx runtime
# Deleting a temp directory is no good as a test: nginx recreates it the moment
# nginx runs. Corrupt something the check really inspects instead - and which one
# depends on where the gateway runs (host nginx, or its own jail).
MODE=$(sed -n 's/^GATEWAY_MODE="\{0,1\}\([a-z]*\)"\{0,1\}.*/\1/p' /etc/dsh-mu/dsh-mu.conf 2>/dev/null | tail -1)
if [ "$MODE" = "jail" ]; then
	frag=/var/lib/dsh-mu/gateway/conf/http.d/dsh-mu-server.conf
	if [ -f "$frag" ]; then
		cp "$frag" "$frag.bak"
		printf '\n# tampered by doctor-fix test\n' >> "$frag"
		if doctor_has "gateway jail config" "FAIL|WARN"; then
			ok "2 doctor notices a tampered gateway-jail fragment"
		else
			bad "2 doctor did not notice a tampered gateway-jail fragment"
		fi
		"$DSHMU_CLI" gateway conf >/dev/null 2>&1 || true
		doctor_has "gateway jail config" "PASS" && ok "2 re-rendering the config turns the check green" \
			|| bad "2 the jail config check is still not green"
		rm -f "$frag.bak"
	else
		t_skip "2 no gateway-jail fragment to tamper with"
	fi
else
broken_dir=$(ls -d /var/lib/nginx/tmp/* 2>/dev/null | head -1)
if [ -n "$broken_dir" ]; then
	chown root:root "$broken_dir" 2>/dev/null || true
	chmod 0700 "$broken_dir" 2>/dev/null || true
	if doctor_has "nginx runtime" "WARN|FAIL"; then
		ok "2 doctor notices a temp directory nginx cannot write to ($broken_dir)"
	else
		bad "2 doctor did not notice the unusable nginx temp directory"
	fi
	"$DSHMU_CLI" doctor --fix >/dev/null 2>&1
	owner=$(stat -c %U "$broken_dir" 2>/dev/null)
	if [ "$owner" = "nginx" ]; then
		ok "2 doctor --fix restored ownership ($owner)"
	else
		bad "2 doctor --fix left the owner as $owner"
	fi
	doctor_has "nginx runtime" "PASS" && ok "2 the check is green again" \
		|| bad "2 the check is still not green"
else
	t_skip "2 no nginx temp directory to break"
fi
fi

# -------------------------------------------------------------- 3. gateway view
view=/etc/dsh-mu/gateway/users.json
view_backup=$(mktemp)
cp "$view" "$view_backup" 2>/dev/null || :
rm -f "$view"
if doctor_has "gateway views in sync" "WARN|FAIL"; then
	ok "3 doctor notices the missing gateway view"
else
	bad "3 doctor did not notice the missing gateway view"
fi
"$DSHMU_CLI" doctor --fix >/dev/null 2>&1
if [ -s "$view" ]; then
	ok "3 doctor --fix re-rendered the view"
else
	bad "3 doctor --fix did not re-render the view"
fi
doctor_has "gateway views in sync" "PASS" && ok "3 the check is green again" \
	|| bad "3 the check is still not green"
rm -f "$view_backup"

# ----------------------------------------------------------- 4. drifted record
# The instance is running; claim it is stopped, which is exactly the drift a
# crash or a hand-edited state leaves behind.
if [ -n "$(t_jail_pid "$PORT")" ] || t_wait_port "$PORT" 5 open; then
	python3 - "$DSHMU_STATE" "$INSTANCE" <<'PY'
import json, sys
path, name = sys.argv[1], sys.argv[2]
st = json.load(open(path))
st["instances"][name]["state"] = "stopped"
json.dump(st, open(path, "w"), indent=2, sort_keys=True)
PY
	if doctor_has "instance $INSTANCE " "WARN|FAIL"; then
		ok "4 doctor notices a record that contradicts reality"
	else
		bad "4 doctor did not notice the drifted instance record"
	fi
	"$DSHMU_CLI" sync-state >/dev/null 2>&1
	if doctor_has "instance $INSTANCE " "PASS"; then
		ok "4 sync-state reconciled the record"
	else
		bad "4 sync-state did not reconcile the record"
	fi
else
	t_skip "4 instance $INSTANCE is not running, cannot create the drift"
fi

# ------------------------------------------------------------------- final state
line=$(doctor_line)
if echo "$line" | grep -q "0 FAIL" && echo "$line" | grep -q "0 WARN"; then
	ok "the platform is clean again ($line)"
else
	bad "doctor is not clean after the repairs ($line)"
	"$DSHMU_CLI" doctor 2>/dev/null | grep -E '^(WARN|FAIL)' | sed 's/^/      /'
fi

t_summary "DOCTOR FIX"
