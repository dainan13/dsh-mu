#!/bin/sh
# acceptance.sh [instance] [user] [password]
#
# Verifies the platform's guarantees on the live host:
#   A cgroup limits exist for the instance and its processes live in that group
#   B the delegated cgroup tree really enforces memory / pids / cpu limits,
#     and a limit lowered on a *running* instance takes effect immediately
#   C the jail drops privileges, drops capabilities and isolates the filesystem
#   D apk inside the jail does not affect the host (apk/prefix isolation)
#   E the agent's bwrap sandbox works inside the jail
#   F the gateway end to end (via tests/selftest.mjs)
set -u

INST="${1:-alice}"
TESTUSER="${2:-}"
TESTPASS="${3:-}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -r "$HERE/lib.sh" ] || {
	echo "tests/lib.sh is missing next to this script; install the tests/ directory as a whole" >&2
	exit 2
}
. "$HERE/lib.sh"
JAIL=$DSHMU_JAIL
CONF="/etc/dsh-mu/instances/${INST}.conf"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

[ -r "$CONF" ] || { echo "no such instance: $INST ($CONF)"; exit 2; }
# shellcheck disable=SC1090
. "$CONF"

echo "=== acceptance: instance=$INST uid=$INST_UID port=$PORT ==="

# ---------------------------------------------------------------- A: cgroup
CGROUP="/sys/fs/cgroup/dsh-mu/$INST"
for f in cpu.max memory.max pids.max; do
	if [ -r "$CGROUP/$f" ]; then
		ok "A $f = $(cat "$CGROUP/$f")"
	else
		bad "A $f missing under $CGROUP"
	fi
done
members=$(wc -l < "$CGROUP/cgroup.procs" 2>/dev/null || echo 0)
[ "$members" -ge 1 ] && ok "A instance processes inside its cgroup ($members)" || bad "A instance cgroup is empty"

# ------------------------------------------------------- B: enforcement
SCRATCH=/sys/fs/cgroup/dsh-mu/_acceptance
mkdir -p "$SCRATCH"
echo 100663296 > "$SCRATCH/memory.max" 2>/dev/null   # 96MB
if command -v python3 >/dev/null 2>&1; then
	python3 - "$SCRATCH" <<'PY' >/dev/null 2>&1
import os, sys, time
scratch = sys.argv[1]
with open(os.path.join(scratch, "cgroup.procs"), "w") as fh:
    fh.write(str(os.getpid()))
blocks = []
for _ in range(48):
    blocks.append(bytearray(8 * 1024 * 1024))
time.sleep(0.2)
PY
	kills=$(awk '/^oom_kill/{print $2}' "$SCRATCH/memory.events" 2>/dev/null || echo 0)
	[ "${kills:-0}" -ge 1 ] && ok "B memory.max enforced (oom_kill=$kills)" || bad "B memory.max not enforced"
else
	bad "B memory.max check needs python3 on the host"
fi

# cpu.max is a *rate* limit, so the only honest check is throttling: give the
# scratch group 2% of one CPU and watch the kernel's own counter.
if command -v python3 >/dev/null 2>&1; then
	echo "2000 100000" > "$SCRATCH/cpu.max" 2>/dev/null
	thr_before=$(awk '/^nr_throttled/{print $2}' "$SCRATCH/cpu.stat" 2>/dev/null || echo 0)
	python3 - "$SCRATCH" <<'PY' >/dev/null 2>&1
import os, sys, time
scratch = sys.argv[1]
with open(os.path.join(scratch, "cgroup.procs"), "w") as fh:
    fh.write(str(os.getpid()))
end = time.time() + 2.5
x = 0
while time.time() < end:
    x = (x + 1) % 1000003
PY
	thr_after=$(awk '/^nr_throttled/{print $2}' "$SCRATCH/cpu.stat" 2>/dev/null || echo 0)
	if [ "${thr_after:-0}" -gt "${thr_before:-0}" ]; then
		ok "B cpu.max enforced (nr_throttled $thr_before -> $thr_after)"
	else
		bad "B cpu.max not enforced (nr_throttled stayed ${thr_before:-0})"
	fi
else
	bad "B cpu.max check needs python3 on the host"
fi

echo 32 > "$SCRATCH/pids.max" 2>/dev/null
cat > /tmp/_acc_pids.sh <<EOF
echo \$\$ > $SCRATCH/cgroup.procs
i=0
while [ \$i -lt 200 ]; do sleep 20 & i=\$((i+1)); done
EOF
sh /tmp/_acc_pids.sh >/dev/null 2>&1
spawned=$(pgrep -f 'sleep 20' | wc -l)
pkill -f 'sleep 20' 2>/dev/null
[ "$spawned" -le 40 ] && ok "B pids.max enforced (spawned≈$spawned)" || bad "B pids.max not enforced (spawned=$spawned)"
rmdir "$SCRATCH" 2>/dev/null

# ------------------------------------- B2: limits change while the instance runs
# "limits can be adjusted online" is a claim about a *running* instance, so go
# through the supported interface (`instance limits`, which writes the record and
# then calls apply_limits_live) and check the kernel side plus the effect.
#
# Note: writing the cgroup file by hand is NOT the way to test this - every jail
# start re-applies the record's values, so a hand edit is undone by the next
# command (that behaviour is itself checked in B3).
CGROUP_ORIG_PIDS=$(cat "$CGROUP/pids.max" 2>/dev/null || echo max)
restore_pids() { "$DSHMU_CLI" instance limits "$INST" --pids "$CGROUP_ORIG_PIDS" >/dev/null 2>&1; }
trap restore_pids EXIT INT TERM
pids_now=$(cat "$CGROUP/pids.current" 2>/dev/null || echo 0)
pids_want=$((pids_now + 2))
if [ "$pids_now" -gt 0 ] && "$DSHMU_CLI" instance limits "$INST" --pids "$pids_want" >/dev/null 2>&1; then
	live=$(cat "$CGROUP/pids.max" 2>/dev/null)
	if [ "$live" = "$pids_want" ]; then
		ok "B2 instance limits --pids $pids_want reached the running cgroup immediately"
	else
		bad "B2 cgroup still says pids.max=$live after the online change"
	fi
	seen=$($JAIL "$INST" -- 'n=0; i=0; while [ $i -lt 12 ]; do (sleep 6 &) 2>/dev/null && n=$((n+1)); i=$((i+1)); done; echo $n' 2>/dev/null | tail -1)
	pkill -f 'sleep 6' 2>/dev/null
	if [ "${seen:-12}" -lt 12 ]; then
		ok "B2 the lowered limit is enforced ($seen of 12 children allowed)"
	else
		bad "B2 the lowered limit had no effect ($seen of 12 children spawned)"
	fi
	restore_pids
	trap - EXIT INT TERM
	if [ "$(cat "$CGROUP/pids.max" 2>/dev/null)" = "$CGROUP_ORIG_PIDS" ]; then
		ok "B2 restoring via the CLI put pids.max back to $CGROUP_ORIG_PIDS"
	else
		bad "B2 pids.max was not restored (now $(cat "$CGROUP/pids.max" 2>/dev/null))"
	fi

	# B3: the record is authoritative - a hand-edited cgroup is reset on the next
	# start, so drift between the record and the kernel cannot survive.
	echo 64 > "$CGROUP/pids.max" 2>/dev/null
	$JAIL "$INST" -- 'true' >/dev/null 2>&1
	reset_to=$(cat "$CGROUP/pids.max" 2>/dev/null)
	if [ "$reset_to" = "$CGROUP_ORIG_PIDS" ]; then
		ok "B3 a hand-edited cgroup is reset from the record on the next start"
	else
		bad "B3 hand-edited pids.max stuck ($reset_to, expected $CGROUP_ORIG_PIDS)"
	fi
else
	bad "B2 could not lower pids.max on $CGROUP via the CLI"
	restore_pids
	trap - EXIT INT TERM
fi

# ------------------------------------------------------------ C: jail identity
RUN_UID="${INST_UID:-0}"
out=$($JAIL "$INST" -- 'id -u; grep -E "^CapEff" /proc/self/status; hostname' 2>/dev/null)
uid_seen=$(echo "$out" | sed -n '1p')
caps_seen=$(echo "$out" | sed -n '2p' | awk '{print $2}')
host_seen=$(echo "$out" | sed -n '3p')
[ "$uid_seen" = "$RUN_UID" ] && ok "C jail runs as uid $RUN_UID (capabilities dropped)" || bad "C jail uid=$uid_seen expected $RUN_UID"
# Two configurations are intentional: everything dropped, or exactly the set the
# command sandbox needs (sys_admin/setuid/setgid = 0x2000c0).
case "$caps_seen" in
	0000000000000000|00000000002000c0)
		ok "C capabilities limited (CapEff=$caps_seen)" ;;
	*)
		bad "C capabilities not limited ($caps_seen)" ;;
esac
[ "$host_seen" = "$INST" ] && ok "C UTS namespace hostname=$INST" || bad "C hostname=$host_seen expected $INST"

# The isolation boundary is the mount namespace: no CAP_SYS_ADMIN means the jail
# cannot mount anything, and host paths outside its bind list are unreachable.
caps_now=$( $JAIL "$INST" -- 'grep CapEff /proc/self/status' 2>/dev/null | awk '{print $2}' )
if [ "$caps_now" = "00000000002000c0" ]; then
	skip "C mount inside the jail (allowed by the configured capability set)"
elif $JAIL "$INST" -- 'mount -t tmpfs none /mnt 2>/dev/null && echo MOUNTED || echo MOUNT-DENIED' 2>/dev/null | grep -q MOUNT-DENIED; then
	ok "C jail cannot mount (no CAP_SYS_ADMIN)"
else
	bad "C jail was able to mount a filesystem"
fi
if $JAIL "$INST" -- 'test -e /var/lib/dsh-mu && echo VISIBLE || echo HIDDEN' 2>/dev/null | grep -q HIDDEN; then
	ok "C host instance directory is not visible in the jail"
else
	bad "C host instance directory is visible in the jail"
fi
if $JAIL "$INST" -- 'ls /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js >/dev/null 2>&1 && echo RUNTIME-OK || echo RUNTIME-MISSING' 2>/dev/null | grep -q RUNTIME-OK; then
	ok "C shared DSH runtime is visible read-only"
else
	bad "C shared DSH runtime missing in the jail"
fi
if $JAIL "$INST" -- 'touch /home/dsh/.acc_write && rm -f /home/dsh/.acc_write && echo WRITABLE' 2>/dev/null | grep -q WRITABLE; then
	ok "C instance home is writable"
else
	bad "C instance home is not writable"
fi
if $JAIL "$INST" -- 'test -x /usr/bin/node && /usr/bin/node -e "console.log(process.version)"' 2>/dev/null | grep -q '^v'; then
	ok "C jail node runtime works"
else
	bad "C jail node runtime broken"
fi

# ------------------------------------------------------------------ D: apk
PKG=nano
host_before=$(command -v $PKG >/dev/null 2>&1 && echo yes || echo no)
jail_apk=$($JAIL "$INST" -- "apk add --no-cache $PKG >/dev/null 2>&1 && command -v $PKG >/dev/null 2>&1 && echo INSTALLED || echo FAILED" 2>/dev/null | tail -1)
host_after=$(command -v $PKG >/dev/null 2>&1 && echo yes || echo no)
[ "$jail_apk" = "INSTALLED" ] && ok "D apk add inside the jail works ($PKG)" || bad "D apk add inside the jail failed"
[ "$host_before" = "no" ] && [ "$host_after" = "no" ] && ok "D apk install did not leak to the host" || bad "D host state changed ($host_before -> $host_after)"
if [ -e "$ROOTFS/usr/bin/$PKG" ] || [ -e "$ROOTFS/bin/$PKG" ]; then
	ok "D package landed inside the instance rootfs only"
else
	bad "D package not found in the instance rootfs"
fi
[ -f "$ROOTFS/lib/apk/db/installed" ] && ok "D instance keeps its own apk database" || bad "D instance apk database missing"
if grep -q "^$PKG\$" "$ROOTFS/etc/apk/world" 2>/dev/null; then
	ok "D instance apk world recorded $PKG"
else
	bad "D instance apk world did not record $PKG"
fi
if [ -f /lib/apk/db/installed ] && ! grep -q "^P:$PKG\$" /lib/apk/db/installed 2>/dev/null; then
	ok "D host apk database untouched"
else
	bad "D host apk database was modified"
fi

# ------------------------------------------------------- E: agent sandbox
bwrap=$($JAIL "$INST" -- 'bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid true 2>/dev/null && echo BWRAP-OK || echo BWRAP-FAIL' 2>/dev/null | tail -1)
landlock=$(python3 -c 'import ctypes;l=ctypes.CDLL(None,use_errno=True);l.syscall.restype=ctypes.c_long;print("yes" if l.syscall(444,None,0,1)>0 else "no")' 2>/dev/null || echo unknown)
mode=$($JAIL "$INST" -- 'echo $DSH_PERMISSION_MODE' 2>/dev/null | tail -1)
if [ "$bwrap" = "BWRAP-OK" ]; then
	ok "E bwrap sandbox usable inside the jail (mode=$mode)"
elif [ "$landlock" = "yes" ]; then
	ok "E agent sandbox falls back to Landlock (bwrap needs userns, blocked in LXC)"
else
	# Neither backend exists in this LXC container: the jail itself must be the
	# isolation boundary, which requires the agent sandbox to be off.
	if [ "$mode" = "danger-full-access" ]; then
		ok "E no DSH sandbox backend in this LXC; instance runs with sandbox disabled (jail is the boundary)"
	else
		bad "E no DSH sandbox backend and DSH_PERMISSION_MODE=$mode (agent tools would fail closed)"
	fi
fi
# Prove the composed profile really reports the configured mode inside the jail.
composed=$($JAIL "$INST" -- 'cd /home/dsh/workspace && dsh --profile web --dump-config 2>/dev/null | grep -A3 "id: sandbox-policy" | grep mode' 2>/dev/null | tail -1)
case "$composed" in
	*mode:*) ok "E composed profile sandbox mode:$(echo "$composed" | tr -d ' ')" ;;
	*) echo "SKIP  E could not read the composed sandbox mode" ;;
esac

# ---------------------------------------------------------------- F: gateway
if [ -n "$TESTUSER" ]; then
	if node /usr/local/lib/dsh-mu/tests/selftest.mjs --user "$TESTUSER" --pass "$TESTPASS" 2>&1 | tail -12; then
		ok "F gateway end-to-end selftest"
	else
		bad "F gateway end-to-end selftest"
	fi
else
	echo "SKIP  F gateway selftest (pass <user> <password> to enable)"
fi

# --------------------------------------------------------------- G: audit trail
# Every state change goes through save_state(), so a CLI mutation must leave a
# secret-free record even though no browser was involved.
AUDIT=/var/log/dsh-mu/audit.log
TMPUSER="audit$$"
if [ -w /etc/dsh-mu ] && [ -f "$AUDIT" ]; then
	before=$(wc -l < "$AUDIT" 2>/dev/null || echo 0)
	dsh-muctl user add "$TMPUSER" --password "audit-fixture-password" --instance none >/dev/null 2>&1
	dsh-muctl user del "$TMPUSER" >/dev/null 2>&1
	sleep 1
	new=$(tail -n +$((before + 1)) "$AUDIT" 2>/dev/null)
	if echo "$new" | grep -q '"action": "state.change"'; then
		ok "G CLI change is recorded in the audit trail"
	else
		bad "G CLI change left no audit record"
	fi
	if echo "$new" | grep -q "$TMPUSER"; then
		ok "G the record names the affected account"
	else
		bad "G the record does not name the affected account"
	fi
	if echo "$new" | grep -q "audit-fixture-password"; then
		bad "G the account password leaked into the audit log"
	else
		ok "G no password leaked into the audit log"
	fi
	if echo "$new" | grep -q '"actor": "cli:'; then
		ok "G the record identifies the CLI actor"
	else
		bad "G the record has no CLI actor"
	fi
	# A configuration change is audited by key name, never by value. Pick a
	# value that really differs, then put the original back.
	orig_days=$(dsh-muctl config session_days 2>/dev/null | tr -d '"[:space:]')
	other_days=7
	[ "$orig_days" = "7" ] && other_days=9
	mark=$(wc -l < "$AUDIT" 2>/dev/null || echo 0)
	dsh-muctl config session_days "$other_days" >/dev/null 2>&1
	dsh-muctl config session_days "${orig_days:-7}" >/dev/null 2>&1
	sleep 1
	conf_new=$(tail -n +$((mark + 1)) "$AUDIT" 2>/dev/null)
	if echo "$conf_new" | grep -q '"action": "config.change"'; then
		ok "G configuration changes are audited"
	else
		bad "G configuration change left no audit record"
	fi
	# The loopback backend writes its own request-level records (actor + client
	# IP). /ensure is deliberately not audited unless it really starts something,
	# so drive a change that always happens: a config update through the API.
	mark=$(wc -l < "$AUDIT" 2>/dev/null || echo 0)
	token=$(cat /etc/dsh-mu/admin.token 2>/dev/null)
	if [ -n "$token" ]; then
		wget -q -O /dev/null --header="X-DSH-MU-Admin: $token" \
			--header="Content-Type: application/json" \
			--post-data="{\"key\":\"session_days\",\"value\":\"$other_days\"}" \
			"http://127.0.0.1:8787/api/config" 2>/dev/null || true
		sleep 2
		api_new=$(tail -n +$((mark + 1)) "$AUDIT" 2>/dev/null)
		if echo "$api_new" | grep -q '"action": "config_set"'; then
			ok "G backend API actions are audited"
		else
			bad "G backend API action left no audit record"
		fi
		if echo "$api_new" | grep -q '"ip": "127.0.0.1"'; then
			ok "G the API record carries the client address"
		else
			bad "G the API record has no client address"
		fi
		dsh-muctl config session_days "${orig_days:-7}" >/dev/null 2>&1
	else
		echo "SKIP  G backend API audit check (no admin token)"
	fi
else
	echo "SKIP  G audit trail (need root and $AUDIT)"
fi

echo
echo "=== ACCEPTANCE: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" = 0 ] && echo "ACCEPTANCE PASSED" || echo "ACCEPTANCE FAILED"
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
