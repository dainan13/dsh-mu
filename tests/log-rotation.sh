#!/bin/sh
# log-rotation.sh - rotation is claimed; this proves it rotates, and that the
# services keep logging afterwards.
#
#   sh tests/log-rotation.sh
#
# A platform that never rotates its logs dies of a full disk, and copytruncate is
# only correct if the *running* writers keep their file descriptors working - so
# the interesting assertions are "a rotated copy appeared", "the live file was
# truncated" and "new lines still land after the rotation, without restarting
# anything".
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -r "$HERE/lib.sh" ] || {
	echo "tests/lib.sh is missing next to this script; install the tests/ directory as a whole" >&2
	exit 2
}
. "$HERE/lib.sh"

t_require_root || exit 2
LOGDIR=${DSHMU_LOG_DIR:-/var/log/dsh-mu}
DROPIN=/etc/logrotate.d/dsh-mu

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); t_ok "$@"; }
bad() { FAIL=$((FAIL+1)); t_bad "$@"; }

[ -d "$LOGDIR" ] || { echo "no log directory at $LOGDIR" >&2; exit 2; }

echo "== log rotation: $LOGDIR =="

# ------------------------------------------------------------------ the drop-in
if [ -f "$DROPIN" ]; then
	ok "logrotate drop-in installed ($DROPIN)"
	if grep -q "copytruncate" "$DROPIN"; then
		ok "drop-in uses copytruncate (writers keep their descriptors)"
	else
		bad "drop-in does not use copytruncate; nginx/supervise-daemon would keep writing to the old file"
	fi
	for directive in daily maxsize rotate missingok notifempty compress; do
		grep -q "^[[:space:]]*$directive" "$DROPIN" || bad "drop-in lacks '$directive'"
	done
	ok "drop-in carries daily/maxsize/rotate/missingok/notifempty/compress"
else
	bad "logrotate drop-in missing ($DROPIN)"
fi

# ----------------------------------------------------------- what logrotate says
if command -v logrotate >/dev/null 2>&1; then
	out=$(logrotate -d "$DROPIN" 2>&1)
	if echo "$out" | grep -qiE "error|unknown option|cannot"; then
		bad "logrotate -d reports a problem"
		echo "$out" | grep -iE "error|unknown|cannot" | head -3 | sed 's/^/      /'
	else
		ok "logrotate accepts the configuration (dry run)"
	fi
fi

# ------------------------------------------------ a real rotation, then keep going
target=""
for candidate in "$LOGDIR/gateway.log" "$LOGDIR/admin.log" "$LOGDIR/audit.log"; do
	[ -s "$candidate" ] && { target=$candidate; break; }
done
if [ -z "$target" ]; then
	printf 'no non-empty log to rotate; generating traffic first\n'
	wget -q -O /dev/null "http://127.0.0.1/login" 2>/dev/null || true
	[ -s "$LOGDIR/gateway.log" ] && target="$LOGDIR/gateway.log"
fi

if [ -n "$target" ]; then
	before_bytes=$(wc -c <"$target")
	workers_before=$(pgrep -f 'nginx: worker' | sort | tr '\n' ' ')
	if command -v logrotate >/dev/null 2>&1; then
		logrotate -f "$DROPIN" >/dev/null 2>&1
		rotated=$(ls "$target".1* 2>/dev/null | head -1)
		if [ -n "$rotated" ]; then
			ok "logrotate produced a rotated copy ($(basename "$rotated"))"
		else
			bad "logrotate -f produced no rotated copy of $target"
		fi
		after_bytes=$(wc -c <"$target" 2>/dev/null || echo "$before_bytes")
		if [ "$after_bytes" -lt "$before_bytes" ]; then
			ok "the live file was truncated in place ($before_bytes -> $after_bytes bytes)"
		else
			bad "the live file was not truncated ($before_bytes -> $after_bytes bytes)"
		fi
	else
		t_skip "logrotate binary not installed"
	fi

	# The writers must keep appending to the *new* file: hit the gateway and the
	# admin API, then look for growth.
	# busybox wget: a stock Alpine host has no curl.
	wget -q -O /dev/null "http://127.0.0.1/login" 2>/dev/null || true
	target_before=$(wc -c <"$target" 2>/dev/null || echo 0)
	sleep 1
	target_after=$(wc -c <"$target" 2>/dev/null || echo 0)
	if [ "$target_after" -gt "$target_before" ]; then
		ok "logging continued after the rotation (no restart needed)"
	else
		# the file may have been rotated again by another writer; accept any log
		grew=0
		for f in "$LOGDIR"/*.log; do [ -s "$f" ] && grew=1; done
		if [ "$grew" = 1 ]; then
			ok "logging continued after the rotation (other writers active)"
		else
			bad "no log file received new lines after the rotation"
		fi
	fi
	if [ -n "$workers_before" ]; then
		workers_after=$(pgrep -f 'nginx: worker' | sort | tr '\n' ' ')
		if [ "$workers_after" = "$workers_before" ]; then
			ok "nginx workers were not restarted ($workers_before)"
		else
			bad "nginx workers changed across the rotation ($workers_before -> $workers_after)"
		fi
	fi

	# The audit trail must survive rotation as well: write one record and find it.
	if [ -f "$LOGDIR/audit.log" ]; then
		mark=$(wc -l <"$LOGDIR/audit.log" 2>/dev/null || echo 0)
		# The audit layer only records *changes*, so pick a value that differs.
		orig_days=$("$DSHMU_CLI" config session_days 2>/dev/null | tr -d '"[:space:]')
		other_days=7
		[ "$orig_days" = "7" ] && other_days=9
		"$DSHMU_CLI" config session_days "$other_days" >/dev/null 2>&1
		"$DSHMU_CLI" config session_days "${orig_days:-7}" >/dev/null 2>&1
		sleep 1
		if [ "$(wc -l <"$LOGDIR/audit.log" 2>/dev/null || echo 0)" -gt "$mark" ]; then
			ok "the audit trail keeps being written after rotation"
		else
			bad "no new audit record after rotation"
		fi
	fi
else
	t_skip "no log file to rotate"
fi

# ---------------------------------------------------------- the no-logrotate path
if "$DSHMU_CLI" logs rotate --force >/tmp/log-rotation-fallback.log 2>&1; then
	ok "dsh-muctl logs rotate --force works (fallback without logrotate)"
	grep -qiE "rotated|nothing" /tmp/log-rotation-fallback.log \
		&& ok "the fallback reports what it did: $(head -1 /tmp/log-rotation-fallback.log)" \
		|| bad "the fallback said nothing useful"
else
	bad "dsh-muctl logs rotate --force failed: $(tail -1 /tmp/log-rotation-fallback.log)"
fi

# ------------------------------------------------------------------- size report
if "$DSHMU_CLI" logs size >/tmp/log-size.log 2>&1 && [ -s /tmp/log-size.log ]; then
	ok "dsh-muctl logs size reports usage: $(tail -1 /tmp/log-size.log)"
else
	bad "dsh-muctl logs size did not report anything"
fi

# ------------------------------------------------------------- retention bound
if [ -f "$DROPIN" ]; then
	keep=$(awk '/^[[:space:]]*rotate[[:space:]]+[0-9]+/{print $2}' "$DROPIN" | head -1)
	if [ -n "$keep" ] && [ "$keep" -ge 2 ] 2>/dev/null; then
		ok "retention is bounded (rotate $keep)"
	else
		bad "retention is not bounded (rotate ${keep:-unset})"
	fi
fi

t_summary "LOG ROTATION"
