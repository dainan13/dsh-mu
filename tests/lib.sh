#!/bin/sh
# lib.sh - shared helpers for the dsh-mu acceptance scripts (sourced, not run).
#
#   . "$(dirname -- "$0")/lib.sh"
#
# Every maintainer-facing detail that used to be copy-pasted into each script
# lives here: where the CLI/jail/state are, how counters are reported, how to
# read an instance's port out of state.json, and how to wait for a port. The
# scripts keep their own aliases (ok/bad/skip/step) so their output format - which
# tests/regression.sh greps - does not change.
#
# Nothing in here writes to the platform.

DSHMU_TESTS_DIR=${DSHMU_TESTS_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)}
DSHMU_LIB_DIR=${DSHMU_LIB_DIR:-/usr/local/lib/dsh-mu}
DSHMU_STATE=${DSHMU_STATE:-/etc/dsh-mu/state.json}
DSHMU_JOURNAL=${DSHMU_JOURNAL:-/var/log/dsh-mu}

t_which() { # t_which <command> [fallback...]
	_cmd=$1; shift
	found=$(command -v "$_cmd" 2>/dev/null) && { printf '%s\n' "$found"; return 0; }
	for candidate in "$@"; do
		[ -x "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
	done
	return 1
}

DSHMU_CLI=${DSHMU_CLI:-$(t_which dsh-muctl /usr/local/sbin/dsh-muctl)}
DSHMU_JAIL=${DSHMU_JAIL:-$(t_which dsh-mu-jail /usr/local/sbin/dsh-mu-jail /usr/local/bin/dsh-mu-jail)}
export DSHMU_CLI DSHMU_JAIL DSHMU_STATE DSHMU_LIB_DIR DSHMU_JOURNAL

# ------------------------------------------------------------------- reporting
T_PASS=0; T_FAIL=0; T_SKIP=0
t_ok()   { T_PASS=$((T_PASS + 1)); printf 'PASS  %s\n' "$*"; }
t_bad()  { T_FAIL=$((T_FAIL + 1)); printf 'FAIL  %s\n' "$*"; }
t_skip() { T_SKIP=$((T_SKIP + 1)); printf 'SKIP  %s\n' "$*"; }
t_step() { printf '\n--- %s ---\n' "$*"; }

t_summary() { # t_summary <LABEL> [PASSED-TEXT]
	_label=$1
	printf '\n=== %s: PASS=%d FAIL=%d' "$_label" "$T_PASS" "$T_FAIL"
	[ "$T_SKIP" -gt 0 ] && printf ' SKIP=%d' "$T_SKIP"
	printf ' ===\n'
	if [ "$T_FAIL" -eq 0 ]; then
		printf '%s\n' "${2:-$_label PASSED}"
		return 0
	fi
	printf '%s\n' "${2:-$_label FAILED}" | sed 's/PASSED/FAILED/'
	return 1
}

# --------------------------------------------------------------------- platform
t_instance_names() { # every instance in state.json, one per line
	python3 - "$DSHMU_STATE" <<'PY' 2>/dev/null || true
import json, sys
try:
    st = json.load(open(sys.argv[1]))
    for name in sorted(st.get("instances") or {}):
        print(name)
except Exception:
    pass
PY
}

t_instance_field() { # t_instance_field <name> <field>
	python3 - "$DSHMU_STATE" "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    st = json.load(open(sys.argv[1]))
    inst = (st.get("instances") or {}).get(sys.argv[2])
    if inst is not None:
        value = inst.get(sys.argv[3])
        if value is not None:
            print(value)
except Exception:
    pass
PY
}

t_instance_port() { t_instance_field "$1" port; }

# The gateway runs either on the host (old mode) or inside its own jail (default).
# Tests that inspect "the effective gateway configuration" must not care which.
t_gateway_mode() {
	# The mode lives in the platform state/config; a rendered platform-jail
	# configuration is the most reliable evidence that jail mode is in use.
	_mode=$(sed -n 's/^GATEWAY_MODE="\{0,1\}\([a-z]*\)"\{0,1\}.*/\1/p' "${DSHMU_CONF:-/etc/dsh-mu/dsh-mu.conf}" 2>/dev/null | tail -1)
	if [ -n "$_mode" ]; then
		printf '%s\n' "$_mode"
		unset _mode
		return 0
	fi
	if [ -f "${DSHMU_GATEWAY_CONF_DIR:-/var/lib/dsh-mu/gateway/conf}/nginx.conf" ]; then
		printf 'jail\n'
	else
		printf 'host\n'
	fi
	unset _mode
}

t_gateway_config() { # like `nginx -T`, but for whichever mode is in use
	if [ "$(t_gateway_mode)" = "jail" ]; then
		cat "${DSHMU_GATEWAY_CONF_DIR:-/var/lib/dsh-mu/gateway/conf}/nginx.conf" \
			"${DSHMU_GATEWAY_CONF_DIR:-/var/lib/dsh-mu/gateway/conf}"/conf.d/*.conf \
			"${DSHMU_GATEWAY_CONF_DIR:-/var/lib/dsh-mu/gateway/conf}"/http.d/*.conf 2>/dev/null
	else
		nginx -T 2>/dev/null
	fi
}

t_gateway_test() { # like `nginx -t`, but for whichever mode is in use
	if [ "$(t_gateway_mode)" = "jail" ]; then
		${DSHMU_JAIL:-/usr/local/sbin/dsh-mu-jail} \
			--conf "${DSHMU_GATEWAY_JAIL_CONF:-/etc/dsh-mu/gateway.conf}" __gateway -- \
			nginx -t -c /etc/dsh-mu-gateway/nginx.conf 2>&1
	else
		nginx -t 2>&1
	fi
}

t_first_instance() { t_instance_names | head -1; }

t_wait_port() { # t_wait_port <port> <timeout-seconds> [open|closed]
	_port=$1; _timeout=${2:-30}; _want=${3:-open}
	python3 - "$_port" "$_timeout" "$_want" <<'PY'
import socket, sys, time
port, timeout, want = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
deadline = time.time() + timeout
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            if want == "open":
                sys.exit(0)
    except OSError:
        if want == "closed":
            sys.exit(0)
    time.sleep(0.3)
sys.exit(1)
PY
}

t_jail() { # t_jail <instance> <command>
	[ -n "$DSHMU_JAIL" ] || return 127
	"$DSHMU_JAIL" "$1" -- "$2"
}

t_jail_pid() { # t_jail_pid <port> - the jailed harness process serving that port
	pgrep -f -- "--port $1" 2>/dev/null | head -1
}

t_require_root() {
	[ "$(id -u)" = 0 ] || { printf 'this check needs root\n' >&2; return 2; }
}

t_require_instance() { # t_require_instance <name>  (dies with a usable message)
	[ -r "$DSHMU_STATE" ] || { printf 'cannot read %s (run as root)\n' "$DSHMU_STATE" >&2; return 2; }
	t_instance_names | grep -qx "$1" || { printf 'no such instance: %s\n' "$1" >&2; return 2; }
}
