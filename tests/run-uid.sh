#!/bin/sh
# run-uid.sh - the documented hardening mode: one host uid per instance.
#
#   sh tests/run-uid.sh
#
# `instance_run_uid auto` is what the threat model points at when you want more
# than "jail-root with every capability dropped": each instance gets its own host
# uid, so even an escape attempt lands as a different user than root. The docs
# claim it works, so this creates a throwaway account+instance in that mode,
# checks the identity the jail really runs as, checks the data ownership and the
# isolation still hold, notes the trade-off (no in-jail apk, because the rootfs
# is not owned by that uid) and cleans everything up again.
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -r "$HERE/lib.sh" ] || {
	echo "tests/lib.sh is missing next to this script; install the tests/ directory as a whole" >&2
	exit 2
}
. "$HERE/lib.sh"

t_require_root || exit 2
[ -n "$DSHMU_CLI" ] || { echo "dsh-muctl not found" >&2; exit 2; }

NAME="uidcheck$$"
ORIG_MODE=$("$DSHMU_CLI" config instance_run_uid 2>/dev/null | tr -d '"[:space:]')
[ -n "$ORIG_MODE" ] || ORIG_MODE=0

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); t_ok "$@"; }
bad() { FAIL=$((FAIL+1)); t_bad "$@"; }

cleanup() {
	"$DSHMU_CLI" instance del "$NAME" --purge >/dev/null 2>&1 || true
	"$DSHMU_CLI" user del "$NAME" >/dev/null 2>&1 || true
	"$DSHMU_CLI" config instance_run_uid "$ORIG_MODE" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "== per-instance uid: name=$NAME (current mode: $ORIG_MODE) =="

# ------------------------------------------------------------------ run as auto
if "$DSHMU_CLI" config instance_run_uid auto >/dev/null 2>&1; then
	ok "switched instance_run_uid to auto"
else
	bad "could not switch instance_run_uid to auto"
fi

if "$DSHMU_CLI" user add "$NAME" --password 'run-uid-drill-password' --instance auto \
	>/tmp/run-uid-create.log 2>&1; then
	ok "created the account and its instance in the new mode"
else
	bad "could not create the account: $(tail -1 /tmp/run-uid-create.log)"
	cleanup
	t_summary "PER-INSTANCE UID"
	exit 1
fi

UID_ALLOC=$(t_instance_field "$NAME" uid)
if [ -n "$UID_ALLOC" ] && [ "$UID_ALLOC" != "0" ]; then
	ok "the instance got a dedicated host uid ($UID_ALLOC)"
else
	bad "the instance still runs as uid ${UID_ALLOC:-?} (auto did not allocate one)"
fi

if grep -q "^INST_UID=$UID_ALLOC$" "/etc/dsh-mu/instances/$NAME.conf" 2>/dev/null; then
	ok "the service configuration carries INST_UID=$UID_ALLOC"
else
	bad "INST_UID is not set in the instance configuration"
fi

for dir in home workspace; do
	owner=$(stat -c %u "/var/lib/dsh-mu/instances/$NAME/$dir" 2>/dev/null || echo "missing")
	if [ "$owner" = "$UID_ALLOC" ]; then
		ok "the $dir volume is owned by that uid"
	else
		bad "the $dir volume is owned by $owner (expected $UID_ALLOC)"
	fi
done

# ------------------------------------------------------- it really runs that way
if "$DSHMU_CLI" instance start "$NAME" >/tmp/run-uid-start.log 2>&1; then
	ok "the instance started in the per-uid mode"
else
	bad "the instance did not start: $(tail -2 /tmp/run-uid-start.log | tr '\n' ' ')"
fi

PORT=$(t_instance_port "$NAME")
if [ -n "$PORT" ] && t_wait_port "$PORT" 90 open; then
	ok "the harness listens on 127.0.0.1:$PORT"
else
	bad "nothing is listening on port ${PORT:-?}"
fi

inside_uid=$("$DSHMU_CLI" instance exec "$NAME" -- 'id -u' 2>/dev/null | tail -1 | tr -d '[:space:]')
if [ "$inside_uid" = "$UID_ALLOC" ]; then
	ok "the jail really runs as uid $inside_uid (not root)"
else
	bad "the jail reports uid ${inside_uid:-?} (expected $UID_ALLOC)"
fi

caps=$("$DSHMU_CLI" instance exec "$NAME" -- \
	'grep -h Cap /proc/self/status | tr -d " \t" | tr "\n" " "' 2>/dev/null)
case "$caps" in
	*CapEff:0000000000000000*CapBnd:0000000000000000*)
		ok "capabilities are still fully dropped" ;;
	*) bad "capabilities are not dropped: ${caps:-<no output>}" ;;
esac

if "$DSHMU_CLI" instance exec "$NAME" -- \
	'head -c1 /proc/1/root/etc/shadow >/dev/null 2>&1' 2>/dev/null; then
	bad "the jail could read /proc/1/root/etc/shadow"
else
	ok "procfs magic links to host files stay closed"
fi

# ------------------------------------------------------------- the trade-off
# The rootfs stays root-owned, so in-jail package management is not available in
# this mode - that is the documented price of the stricter uid.
if "$DSHMU_CLI" instance exec "$NAME" -- 'touch /etc/apk/.uidcheck && rm -f /etc/apk/.uidcheck' \
	>/dev/null 2>&1; then
	ok "in-jail writes to the rootfs still work (rootfs is owned by the instance)"
else
	ok "as documented, the rootfs is not writable by the instance uid (no in-jail apk)"
fi
if "$DSHMU_CLI" instance exec "$NAME" -- 'touch "$HOME/.uidcheck" && rm -f "$HOME/.uidcheck"' \
	>/dev/null 2>&1; then
	ok "the instance home is writable by the instance uid"
else
	bad "the instance cannot write to its own home"
fi

# ---------------------------------------------------------------- back to normal
"$DSHMU_CLI" instance del "$NAME" --purge >/dev/null 2>&1
"$DSHMU_CLI" user del "$NAME" >/dev/null 2>&1
if "$DSHMU_CLI" user list 2>/dev/null | grep -q "$NAME"; then
	bad "the throwaway account is still listed"
else
	ok "the throwaway account and instance are gone"
fi
"$DSHMU_CLI" config instance_run_uid "$ORIG_MODE" >/dev/null 2>&1
now=$("$DSHMU_CLI" config instance_run_uid 2>/dev/null | tr -d '"[:space:]')
if [ "$now" = "$ORIG_MODE" ]; then
	ok "the instance_run_uid setting is back to $ORIG_MODE"
else
	bad "instance_run_uid is $now (expected $ORIG_MODE)"
fi
trap - EXIT INT TERM

line=$("$DSHMU_CLI" doctor 2>/dev/null | grep -E '^doctor:' | tail -1)
if echo "$line" | grep -q "0 FAIL"; then
	ok "doctor is clean after the drill ($line)"
else
	bad "doctor reports failures after the drill ($line)"
	"$DSHMU_CLI" doctor 2>/dev/null | grep -E '^FAIL' | sed 's/^/      /'
fi

t_summary "PER-INSTANCE UID"
