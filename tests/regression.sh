#!/bin/sh
# regression.sh - run every acceptance layer in one go and print a summary.
#
#   sh tests/regression.sh                 # live platform checks + fresh-Alpine rehearsal
#   sh tests/regression.sh --fast          # skip the rehearsal (no chroot build)
#   sh tests/regression.sh --user alice --password alice-test-123 --instance alice
#
# Layers:
#   1. dsh-muctl doctor            host prerequisites and record consistency
#   2. tests/selftest.mjs          gateway: login, proxy, /me, panel, websocket, logout
#   3. tests/ui-lint.mjs           page/backend contract: handlers, ids, routes
#   4. tests/cli-contract.sh       every wrong invocation must fail cleanly
#   5. tests/api-contract.py       every wrong admin API request must fail cleanly
#   6. tests/acceptance.sh         end-to-end acceptance suite
#   7. tests/fresh-machine.py      fresh-machine checklist (skipped without credentials)
#   8. tests/isolation.sh          jail boundary: chroot, caps, procfs, cgroup, tenants
#   9. tests/failure-modes.py      backend down, instance down/crash, reload under load
#  10. tests/doc-lint.mjs          the documentation must not drift from the tree
#  11. tests/soak.py               sustained load: no leaks, no restarts, no 5xx
#  12. tests/artifact-audit.py     the built package and archive honour the promises
#  13. tests/drift-audit.py        the machine and the package run this checkout's code
#  14. tests/doctor-fix.sh         breaking things on purpose, then repairing them
#  15. tests/log-rotation.sh       rotation really rotates and writers keep writing
#  16. tests/user-flows.py         account lifecycle: must-change, password change, kick
#  17. tests/run-uid.sh            per-instance host uid (the stricter hardening mode)
#  18. tests/fresh-rehearsal.sh    clean-Alpine install, lifecycle, recovery and runtime smoke
#  19. tests/state-lock.py         concurrent writers cannot corrupt state.json, and the
#                                  lock really blocks
#  20. tests/admin-surface.py       logging in as a real administrator and using /admin/
#  21. tests/user-panel.py          the normal user's own panel: /me contract and the
#                                  start-my-instance button
#  22. tests/port-move.py           moving an instance to another port (guards + the
#                                  gateway resolving the new port without a reload)
#  23. tests/restore-owner.py       a restore re-owns instance volumes from the record
#  24. tests/restore-plan.py       the dry-run report names the volumes it would
#                                  overwrite and warns about a stale archive
#  25. tests/log-bounds.py         the log tail clamp and its two error paths
#  26. tests/config-bounds.py       the platform-config endpoint's edges (and that a
#                                  dangerous-but-valid value shows up in doctor)
#  27. tests/name-key-bounds.py     account names become paths and API keys end up in
#                                  jails: one validation rule at every entrance
#  28. tests/limit-values.py        memory/CPU/pids: 0 means unlimited, negatives are
#                                  refused, units land as the right cgroup values
#  29. tests/password-bounds.py     the password minimum at every entrance (and that a
#                                  refused change leaves the account untouched)
#  30. tests/provider-probe.py      the page's "test connection": a 200 without a model
#                                  reply is not success, keys are never echoed
#  31. tests/plans.py              model plans: the patch, the credential refs and the
#                                  browser-session `records:` block surviving a sync
#  32. tests/provider-templates.py provider templates: listing, import, validation, and
#                                  the guards (no overwrite, no path traversal)
#  33. tests/login-limit.py        the failed-login limit really engages (runs last:
#                                  it consumes the per-client-IP failure budget)
#
# Exit code 0 only when every layer passed.
set -u
# Never let a layer inherit a tty on stdin: a stray `cat` would block forever.
exec </dev/null

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC=$(CDPATH= cd -- "$HERE/.." && pwd)

# The rehearsal needs a real source checkout (install/, cli/, packaging/). When
# this runner was installed under /usr/local/lib/dsh-mu, look for one elsewhere.
CHECKOUT=""
for cand in "$SRC" "$PWD" /root/dsh-mu-src /usr/src/dsh-mu /opt/dsh-mu-src; do
	if [ -f "$cand/cli/dshmuctl.py" ] && [ -d "$cand/install" ]; then
		CHECKOUT="$cand"
		break
	fi
done
LIB=/usr/local/lib/dsh-mu
GW_ROOTFS=/var/lib/dsh-mu/gateway/rootfs
FAST=0
USER=""
PASSWORD=""
INSTANCE=""
APK=""
APK2=""

while [ $# -gt 0 ]; do
	case "$1" in
		--fast) FAST=1; shift ;;
		--user) USER="$2"; shift 2 ;;
		--password) PASSWORD="$2"; shift 2 ;;
		--instance) INSTANCE="$2"; shift 2 ;;
		--apk) APK="$2"; shift 2 ;;
		--apk2) APK2="$2"; shift 2 ;;
		--src) CHECKOUT=$(CDPATH= cd -- "$2" && pwd); shift 2 ;;
		-h|--help) sed -n '2,18p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
skip() { printf 'SKIP  %s\n' "$*"; }
step() { printf '\n=== %s ===\n' "$*"; }
# lib.sh provides the shared platform helpers (instance ports, waiting on ports,
# running something inside a jail); the runner keeps its own banner-style step()
# because its output is the human-facing summary.
. "$HERE/lib.sh" 2>/dev/null || true
have() { command -v "$1" >/dev/null 2>&1; }

step "1/33 doctor"
if have dsh-muctl; then
	if out=$(dsh-muctl doctor 2>&1); then
		ok "dsh-muctl doctor: $(printf '%s\n' "$out" | grep -E '^doctor:' | tail -1)"
	else
		bad "dsh-muctl doctor"
		printf '%s\n' "$out" | grep -E '^FAIL' | sed 's/^/      /'
	fi
else
	bad "dsh-muctl is not installed"
fi

# The host deliberately has no node any more (the DSH runtime lives in the gateway
# template), so node-driven layers run that interpreter through chroot when needed.
# `run_node <script> [args...]`: the host has no node, so node-driven layers use the
# gateway template's interpreter. Anything they need on disk is staged inside the
# template first (a chroot cannot see the host's paths).
NODE_STAGE="$GW_ROOTFS/tmp/dsh-mu-suite"
run_node() {
	if command -v node >/dev/null 2>&1; then
		node "$@"
		return $?
	fi
	if [ ! -x "$GW_ROOTFS/usr/bin/node" ]; then
		echo "SKIP: no node on the host and none in the gateway template" >&2
		return 127
	fi
	_src_root="${CHECKOUT:-${SRC:-$PWD}}"
	_suite="$GW_ROOTFS/tmp/dsh-mu-suite"
	mkdir -p "$_suite"
	# Bind instead of copy: a bind keeps the exact paths node is told about, so a
	# stale or partial copy can never make the module look missing.
	bind_ro() { # <host path> <name inside the suite>
		[ -e "$1" ] || return 0
		mkdir -p "$_suite/$2"
		# A mount left over from a previous run can point at a replaced tree, which
		# looks empty; always detach before binding again.
		mountpoint -q "$_suite/$2" 2>/dev/null && umount "$_suite/$2" 2>/dev/null
		mount --bind "$1" "$_suite/$2" 2>/dev/null || true
	}
	bind_ro "$LIB/tests" tests
	bind_ro "$_src_root" src
	bind_ro /var/lib/dsh-mu/gateway/conf conf
	_script="$1"
	shift
	staged() {
		case "$1" in
			# $LIB/tests is itself bind-mounted at $_suite/tests, so strip that whole
			# prefix (a bare $LIB/ prefix would produce tests/tests/...).
			"$LIB/tests"/*) printf '%s/tests/%s' "$_suite" "${1#"$LIB/tests"/}" ;;
			"$LIB"/*) printf '%s/%s' "$_suite" "${1#"$LIB"/}" ;;
			# A bare source directory (no trailing slash) is a valid argument too.
			"$_src_root") printf '%s/src' "$_suite" ;;
			"$_src_root"/*) printf '%s/src/%s' "$_suite" "${1#"$_src_root"/}" ;;
			*) printf '%s' "$1" ;;
		esac
	}
	# Inside the chroot the suite lives at /tmp/... (the host sees it under
	# $GW_ROOTFS), so strip the rootfs prefix from every single path.
	_in_jail() {
		case "$1" in
			"$GW_ROOTFS"/*) printf '%s' "${1#"$GW_ROOTFS"}" ;;
			*) printf '%s' "$1" ;;
		esac
	}
	_script=$(_in_jail "$(staged "$_script")")
	_staged_args=""
	for _a in "$@"; do _staged_args="$_staged_args $(_in_jail "$(staged "$_a")")"; done
	export DSH_MU_CONF_DIR="$(_in_jail "$_suite/conf")"
	# The gateway reads the token from its own copy inside the jail, so use that one.
	for _tok in "$GW_ROOTFS/etc/dsh-mu/admin.token" /etc/dsh-mu/admin.token; do
		if [ -r "$_tok" ]; then DSH_MU_OPS_TOKEN=$(cat "$_tok"); export DSH_MU_OPS_TOKEN; break; fi
	done
	printf 'run_node: src_root=[%s] suite=[%s] script=[%s] args=[%s]\n' \
		"$_src_root" "$_suite" "$_script" "$_staged_args" >&2
	# shellcheck disable=SC2086
	chroot "$GW_ROOTFS" /usr/bin/node "$_script" $_staged_args
}

step "2/33 gateway selftest"
if [ -f "$LIB/tests/selftest.mjs" ] && [ -n "$USER" ]; then
	if run_node "$LIB/tests/selftest.mjs" --user "$USER" --pass "$PASSWORD" >/tmp/regression-selftest.log 2>&1; then
		ok "selftest: $(grep -c '^PASS' /tmp/regression-selftest.log) checks passed"
	else
		bad "selftest (see /tmp/regression-selftest.log)"
		grep -E '^FAIL' /tmp/regression-selftest.log | sed 's/^/      /'
	fi
else
	skip "selftest needs --user/--password and $LIB/tests/selftest.mjs"
fi

step "3/33 UI consistency (admin page, panel, backend routes)"
if [ -f "$CHECKOUT/tests/ui-lint.mjs" ] || [ -f "$SRC/tests/ui-lint.mjs" ]; then
	_ui="$CHECKOUT/tests/ui-lint.mjs"; [ -f "$_ui" ] || _ui="$SRC/tests/ui-lint.mjs"
	if run_node "$_ui" "${CHECKOUT:-$SRC}" >/tmp/regression-ui.log 2>&1; then
		ok "ui lint: $(grep -E 'UI LINT:' /tmp/regression-ui.log | tail -1)"
	else
		bad "ui lint (see /tmp/regression-ui.log)"
		grep -E '^FAIL' /tmp/regression-ui.log | sed 's/^/      /'
	fi
else
	skip "ui-lint.mjs not found in the checkout"
fi

step "4/33 CLI contract (clean errors, no tracebacks)"
_cli="$LIB/tests/cli-contract.sh"; [ -f "$_cli" ] || _cli="$HERE/cli-contract.sh"
if [ -f "$_cli" ]; then
	if sh "$_cli" ${INSTANCE:+"$INSTANCE"} >/tmp/regression-cli.log 2>&1; then
		ok "cli contract: $(grep -E 'CLI CONTRACT:' /tmp/regression-cli.log | tail -1)"
	else
		bad "cli contract (see /tmp/regression-cli.log)"
		grep -E '^FAIL' /tmp/regression-cli.log | sed 's/^/      /'
	fi
else
	skip "cli-contract.sh not installed"
fi

step "5/33 admin API contract (JSON errors, no tracebacks)"
_api="$LIB/tests/api-contract.py"; [ -f "$_api" ] || _api="$HERE/api-contract.py"
if [ -f "$_api" ]; then
	if python3 "$_api" ${INSTANCE:+--instance "$INSTANCE"} >/tmp/regression-api.log 2>&1; then
		ok "api contract: $(grep -E 'API CONTRACT:' /tmp/regression-api.log | tail -1)"
	else
		bad "api contract (see /tmp/regression-api.log)"
		grep -E '^FAIL' /tmp/regression-api.log | sed 's/^/      /'
	fi
else
	skip "api-contract.py not installed"
fi

step "6/33 acceptance suite"
if [ -f "$LIB/tests/acceptance.sh" ] && [ -n "$USER" ]; then
	if sh "$LIB/tests/acceptance.sh" "${INSTANCE:-$USER}" "$USER" "$PASSWORD" >/tmp/regression-acceptance.log 2>&1; then
		ok "acceptance: $(grep -E 'ACCEPTANCE: ' /tmp/regression-acceptance.log | tail -1)"
	else
		bad "acceptance (see /tmp/regression-acceptance.log)"
		grep -E '^FAIL' /tmp/regression-acceptance.log | sed 's/^/      /'
	fi
else
	skip "acceptance needs --user/--password"
fi

# The rehearsal shells out to node too; point it at the staged suite.
export DSH_MU_NODE="chroot $GW_ROOTFS /usr/bin/node"
export DSH_MU_TEST_DIR="/tmp/dsh-mu-suite/tests"
step "7/33 fresh-machine checklist"
if [ -f "$LIB/tests/fresh-machine.py" ]; then
	if [ -n "$USER" ]; then
		if python3 "$LIB/tests/fresh-machine.py" --skip-install --user "$USER" --password "$PASSWORD" \
			${INSTANCE:+--instance "$INSTANCE"} >/tmp/regression-fresh.log 2>&1; then
			ok "fresh-machine: $(grep -E 'FRESH-MACHINE RESULT' /tmp/regression-fresh.log | tail -1)"
		else
			bad "fresh-machine (see /tmp/regression-fresh.log)"
			grep -E '^FAIL' /tmp/regression-fresh.log | sed 's/^/      /'
		fi
	else
		if python3 "$LIB/tests/fresh-machine.py" --skip-install >/tmp/regression-fresh.log 2>&1; then
			ok "fresh-machine (self-provisioning): $(grep -E 'FRESH-MACHINE RESULT' /tmp/regression-fresh.log | tail -1)"
		else
			bad "fresh-machine (see /tmp/regression-fresh.log)"
		fi
	fi
else
	skip "fresh-machine.py not installed"
fi

step "8/33 isolation boundary"
if [ -f "$LIB/tests/isolation.sh" ] || [ -f "$HERE/isolation.sh" ]; then
	_iso="$LIB/tests/isolation.sh"; [ -f "$_iso" ] || _iso="$HERE/isolation.sh"
	if sh "$_iso" ${INSTANCE:+"$INSTANCE"} >/tmp/regression-isolation.log 2>&1; then
		ok "isolation: $(grep -E 'ISOLATION: ' /tmp/regression-isolation.log | tail -1)"
	else
		bad "isolation (see /tmp/regression-isolation.log)"
		grep -E '^FAIL' /tmp/regression-isolation.log | sed 's/^/      /'
	fi
else
	skip "isolation.sh not installed"
fi

step "9/33 failure modes"
if [ -f "$LIB/tests/failure-modes.py" ] || [ -f "$HERE/failure-modes.py" ]; then
	_fm="$LIB/tests/failure-modes.py"; [ -f "$_fm" ] || _fm="$HERE/failure-modes.py"
	if [ -n "$USER" ]; then
		if python3 "$_fm" --user "$USER" --password "$PASSWORD" ${INSTANCE:+--instance "$INSTANCE"} \
			>/tmp/regression-failures.log 2>&1; then
			ok "failure modes: $(grep -E 'FAILURE MODES: ' /tmp/regression-failures.log | tail -1)"
		else
			bad "failure modes (see /tmp/regression-failures.log)"
			grep -E '^FAIL' /tmp/regression-failures.log | sed 's/^/      /'
		fi
	else
		skip "failure modes need --user/--password"
	fi
else
	skip "failure-modes.py not installed"
fi

step "10/33 documentation consistency"
if [ -f "$CHECKOUT/tests/doc-lint.mjs" ] || [ -f "$SRC/tests/doc-lint.mjs" ]; then
	_doc="$CHECKOUT/tests/doc-lint.mjs"; [ -f "$_doc" ] || _doc="$SRC/tests/doc-lint.mjs"
	if run_node "$_doc" "${CHECKOUT:-$SRC}" >/tmp/regression-doc.log 2>&1; then
		ok "doc lint: $(grep -E 'DOC LINT:' /tmp/regression-doc.log | tail -1)"
	else
		bad "doc lint (see /tmp/regression-doc.log)"
		grep -E '^FAIL' /tmp/regression-doc.log | sed 's/^/      /'
	fi
else
	skip "doc-lint.mjs not found in the checkout"
fi

step "11/33 sustained load (soak)"
if [ -f "$LIB/tests/soak.py" ] || [ -f "$HERE/soak.py" ]; then
	_soak="$LIB/tests/soak.py"; [ -f "$_soak" ] || _soak="$HERE/soak.py"
	if [ -n "$USER" ]; then
		if python3 "$_soak" --minutes 2 --user "$USER" --password "$PASSWORD" \
			${INSTANCE:+--instance "$INSTANCE"} >/tmp/regression-soak.log 2>&1; then
			ok "soak: $(grep -E 'SOAK:' /tmp/regression-soak.log | tail -1)"
		else
			bad "soak (see /tmp/regression-soak.log)"
			grep -E '^FAIL' /tmp/regression-soak.log | sed 's/^/      /'
		fi
	else
		skip "soak needs --user/--password"
	fi
else
	skip "soak.py not installed"
fi

step "12/33 release artifact audit"
if [ -f "$CHECKOUT/tests/artifact-audit.py" ] || [ -f "$SRC/tests/artifact-audit.py" ]; then
	_audit="$CHECKOUT/tests/artifact-audit.py"; [ -f "$_audit" ] || _audit="$SRC/tests/artifact-audit.py"
	# audit the package built from *this* tree, not a leftover from an earlier run
	rm -f "${CHECKOUT:-$SRC}"/dist/*.apk 2>/dev/null || true
	if python3 "$_audit" "${CHECKOUT:-$SRC}" ${DSHMU_TARBALL:+--tarball "$DSHMU_TARBALL"} \
		>/tmp/regression-artifact.log 2>&1; then
		ok "artifact audit: $(grep -E 'ARTIFACT AUDIT:' /tmp/regression-artifact.log | tail -1)"
	else
		bad "artifact audit (see /tmp/regression-artifact.log)"
		grep -E '^FAIL' /tmp/regression-artifact.log | sed 's/^/      /'
	fi
else
	skip "artifact-audit.py not found in the checkout"
fi

step "13/33 deploy drift (is the machine running this code?)"
if [ -f "$LIB/tests/drift-audit.py" ] || [ -f "$HERE/drift-audit.py" ]; then
	_drift="$LIB/tests/drift-audit.py"; [ -f "$_drift" ] || _drift="$HERE/drift-audit.py"
	_pkg=$(ls -t "${CHECKOUT:-$SRC}"/dist/*.apk 2>/dev/null | head -1)
	if python3 "$_drift" "${CHECKOUT:-$SRC}" ${_pkg:+--apk "$_pkg"} \
		>/tmp/regression-drift.log 2>&1; then
		ok "deploy drift: $(grep -E 'DRIFT AUDIT:' /tmp/regression-drift.log | tail -1)"
	else
		bad "deploy drift (see /tmp/regression-drift.log)"
		grep -E '^FAIL|^        ' /tmp/regression-drift.log | sed 's/^/      /' | head -8
	fi
else
	skip "drift-audit.py not installed"
fi

step "14/33 doctor repair (break it, then fix it)"
if [ -f "$LIB/tests/doctor-fix.sh" ] || [ -f "$HERE/doctor-fix.sh" ]; then
	_fix="$LIB/tests/doctor-fix.sh"; [ -f "$_fix" ] || _fix="$HERE/doctor-fix.sh"
	if sh "$_fix" >/tmp/regression-doctorfix.log 2>&1; then
		ok "doctor fix: $(grep -E 'DOCTOR FIX:' /tmp/regression-doctorfix.log | tail -1)"
	else
		bad "doctor fix (see /tmp/regression-doctorfix.log)"
		grep -E '^FAIL' /tmp/regression-doctorfix.log | sed 's/^/      /'
	fi
else
	skip "doctor-fix.sh not installed"
fi

step "15/33 log rotation"
if [ -f "$LIB/tests/log-rotation.sh" ] || [ -f "$HERE/log-rotation.sh" ]; then
	_rot="$LIB/tests/log-rotation.sh"; [ -f "$_rot" ] || _rot="$HERE/log-rotation.sh"
	if sh "$_rot" >/tmp/regression-logrotate.log 2>&1; then
		ok "log rotation: $(grep -E 'LOG ROTATION:' /tmp/regression-logrotate.log | tail -1)"
	else
		bad "log rotation (see /tmp/regression-logrotate.log)"
		grep -E '^FAIL' /tmp/regression-logrotate.log | sed 's/^/      /'
	fi
else
	skip "log-rotation.sh not installed"
fi

step "16/33 account lifecycle"
if [ -f "$LIB/tests/user-flows.py" ] || [ -f "$HERE/user-flows.py" ]; then
	_uf="$LIB/tests/user-flows.py"; [ -f "$_uf" ] || _uf="$HERE/user-flows.py"
	if python3 "$_uf" >/tmp/regression-userflows.log 2>&1; then
		ok "user flows: $(grep -E 'USER FLOWS:' /tmp/regression-userflows.log | tail -1)"
	else
		bad "user flows (see /tmp/regression-userflows.log)"
		grep -E '^FAIL' /tmp/regression-userflows.log | sed 's/^/      /'
	fi
else
	skip "user-flows.py not installed"
fi

step "17/33 per-instance uid"
if [ -f "$LIB/tests/run-uid.sh" ] || [ -f "$HERE/run-uid.sh" ]; then
	_uid="$LIB/tests/run-uid.sh"; [ -f "$_uid" ] || _uid="$HERE/run-uid.sh"
	if sh "$_uid" >/tmp/regression-runuid.log 2>&1; then
		ok "per-instance uid: $(grep -E 'PER-INSTANCE UID:' /tmp/regression-runuid.log | tail -1)"
	else
		bad "per-instance uid (see /tmp/regression-runuid.log)"
		grep -E '^FAIL' /tmp/regression-runuid.log | sed 's/^/      /'
	fi
else
	skip "run-uid.sh not installed"
fi

step "18/33 clean-Alpine rehearsal"
if [ "$FAST" = 1 ]; then
	skip "rehearsal skipped (--fast)"
elif [ -z "$CHECKOUT" ]; then
	skip "rehearsal needs a source checkout (run from it, or pass --src DIR)"
	skip "  the installed tree cannot be rehearsed: it has no install/ or cli/ sources"
elif [ -f "$CHECKOUT/tests/fresh-rehearsal.sh" ]; then
	if [ -z "$APK" ] && [ -x "$CHECKOUT/packaging/make-apk.sh" ] && have abuild; then
		# Deterministic artifact paths: parsing make-apk's output is fragile.
		( cd "$CHECKOUT" && sh packaging/make-apk.sh --version 0.1.0 >/dev/null 2>&1 )
		( cd "$CHECKOUT" && sh packaging/make-apk.sh --version 0.1.1 >/dev/null 2>&1 )
		[ -f "$CHECKOUT/dist/dsh-mu-0.1.0-r0.apk" ] && APK="$CHECKOUT/dist/dsh-mu-0.1.0-r0.apk"
		[ -f "$CHECKOUT/dist/dsh-mu-0.1.1-r0.apk" ] && APK2="$CHECKOUT/dist/dsh-mu-0.1.1-r0.apk"
	fi
	if [ -f "$SRC/tests/fresh-rehearsal.sh" ]; then
		if sh "$CHECKOUT/tests/fresh-rehearsal.sh" --clean --skip-dsh --src "$CHECKOUT" \
			${APK:+--apk "$APK"} ${APK2:+--apk2 "$APK2"} \
			>/tmp/regression-rehearsal.log 2>&1; then
			ok "rehearsal: $(grep -E 'FRESH REHEARSAL' /tmp/regression-rehearsal.log | tail -1)"
		else
			bad "rehearsal (see /tmp/regression-rehearsal.log)"
			grep -E '^FAIL' /tmp/regression-rehearsal.log | sed 's/^/      /'
		fi
	else
		skip "rehearsal needs the source checkout at $SRC"
	fi
else
	skip "rehearsal script not found next to this runner"
fi

step "19/33 state lock under contention"
if [ -f "$LIB/tests/state-lock.py" ] || [ -f "$HERE/state-lock.py" ]; then
	_lock="$LIB/tests/state-lock.py"; [ -f "$_lock" ] || _lock="$HERE/state-lock.py"
	# It holds the platform lock for ~3s, so it must not run beside other writers.
	if python3 "$_lock" >/tmp/regression-statelock.log 2>&1; then
		ok "state lock: $(grep -E 'STATE LOCK:' /tmp/regression-statelock.log | tail -1)"
	else
		bad "state lock (see /tmp/regression-statelock.log)"
		grep -E '^FAIL' /tmp/regression-statelock.log | sed 's/^/      /' | head -5
	fi
else
	skip "state-lock.py not installed"
fi

step "20/33 admin surface as a real administrator"
if [ -f "$LIB/tests/admin-surface.py" ] || [ -f "$HERE/admin-surface.py" ]; then
	_adm="$LIB/tests/admin-surface.py"; [ -f "$_adm" ] || _adm="$HERE/admin-surface.py"
	# Creates a temporary host account, adds it to ADMIN_SYSTEM_USERS, logs in and
	# restores everything; safe to run beside nothing else (it edits the config).
	if python3 "$_adm" >/tmp/regression-adminsurface.log 2>&1; then
		ok "admin surface: $(grep -E 'ADMIN SURFACE:' /tmp/regression-adminsurface.log | tail -1)"
	else
		bad "admin surface (see /tmp/regression-adminsurface.log)"
		grep -E '^FAIL' /tmp/regression-adminsurface.log | sed 's/^/      /' | head -5
	fi
else
	skip "admin-surface.py not installed"
fi

step "21/33 user panel (what a normal account sees)"
if [ -f "$LIB/tests/user-panel.py" ] || [ -f "$HERE/user-panel.py" ]; then
	_panel="$LIB/tests/user-panel.py"; [ -f "$_panel" ] || _panel="$HERE/user-panel.py"
	# Creates its own account+instance, stops it, starts it through /me, deletes both.
	if python3 "$_panel" >/tmp/regression-userpanel.log 2>&1; then
		ok "user panel: $(grep -E 'USER PANEL:' /tmp/regression-userpanel.log | tail -1)"
	else
		bad "user panel (see /tmp/regression-userpanel.log)"
		grep -E '^FAIL' /tmp/regression-userpanel.log | sed 's/^/      /' | head -5
	fi
else
	skip "user-panel.py not installed"
fi

step "22/33 port move (guards + no reload)"
if [ -f "$LIB/tests/port-move.py" ] || [ -f "$HERE/port-move.py" ]; then
	_pm="$LIB/tests/port-move.py"; [ -f "$_pm" ] || _pm="$HERE/port-move.py"
	if python3 "$_pm" >/tmp/regression-portmove.log 2>&1; then
		ok "port move: $(grep -E 'PORT MOVE:' /tmp/regression-portmove.log | tail -1)"
	else
		bad "port move (see /tmp/regression-portmove.log)"
		grep -E '^FAIL' /tmp/regression-portmove.log | sed 's/^/      /' | head -5
	fi
else
	skip "port-move.py not installed"
fi

step "23/33 restore ownership"
if [ -f "$LIB/tests/restore-owner.py" ] || [ -f "$HERE/restore-owner.py" ]; then
	_ro="$LIB/tests/restore-owner.py"; [ -f "$_ro" ] || _ro="$HERE/restore-owner.py"
	# Creates a dedicated-uid instance, stages a stale owner, backs up, wipes and
	# restores: the volumes must come back owned by the instance's uid.
	if python3 "$_ro" >/tmp/regression-restoreowner.log 2>&1; then
		ok "restore owner: $(grep -E 'RESTORE OWNER:' /tmp/regression-restoreowner.log | tail -1)"
	else
		bad "restore owner (see /tmp/regression-restoreowner.log)"
		grep -E '^FAIL' /tmp/regression-restoreowner.log | sed 's/^/      /' | head -5
	fi
else
	skip "restore-owner.py not installed"
fi

step "24/33 restore plan (what a restore would overwrite)"
if [ -f "$LIB/tests/restore-plan.py" ] || [ -f "$HERE/restore-plan.py" ]; then
	_rp="$LIB/tests/restore-plan.py"; [ -f "$_rp" ] || _rp="$HERE/restore-plan.py"
	# Builds an archive, makes the live platform newer, then checks the dry-run
	# report names the volumes it would overwrite and warns about staleness.
	if python3 "$_rp" >/tmp/regression-restoreplan.log 2>&1; then
		ok "restore plan: $(grep -E 'RESTORE PLAN:' /tmp/regression-restoreplan.log | tail -1)"
	else
		bad "restore plan (see /tmp/regression-restoreplan.log)"
		grep -E '^FAIL' /tmp/regression-restoreplan.log | sed 's/^/      /' | head -5
	fi
else
	skip "restore-plan.py not installed"
fi

step "25/33 log endpoint edges"
if [ -f "$LIB/tests/log-bounds.py" ] || [ -f "$HERE/log-bounds.py" ]; then
	_lb="$LIB/tests/log-bounds.py"; [ -f "$_lb" ] || _lb="$HERE/log-bounds.py"
	# The tail clamp plus the two error paths: a typo must be a 400, an unknown
	# instance a 404.
	if python3 "$_lb" >/tmp/regression-logbounds.log 2>&1; then
		ok "log bounds: $(grep -E 'LOG BOUNDS:' /tmp/regression-logbounds.log | tail -1)"
	else
		bad "log bounds (see /tmp/regression-logbounds.log)"
		grep -E '^FAIL' /tmp/regression-logbounds.log | sed 's/^/      /' | head -5
	fi
else
	skip "log-bounds.py not installed"
fi

step "26/33 platform config edges"
if [ -f "$LIB/tests/config-bounds.py" ] || [ -f "$HERE/config-bounds.py" ]; then
	_cb="$LIB/tests/config-bounds.py"; [ -f "$_cb" ] || _cb="$HERE/config-bounds.py"
	# Unknown key / bad value / emptying the admin list / narrowing PORT_RANGE past
	# a live instance; restores the config and leaves doctor clean.
	if python3 "$_cb" >/tmp/regression-configbounds.log 2>&1; then
		ok "config bounds: $(grep -E 'CONFIG BOUNDS:' /tmp/regression-configbounds.log | tail -1)"
	else
		bad "config bounds (see /tmp/regression-configbounds.log)"
		grep -E '^FAIL' /tmp/regression-configbounds.log | sed 's/^/      /' | head -5
	fi
else
	skip "config-bounds.py not installed"
fi

step "27/33 account names and API keys"
if [ -f "$LIB/tests/name-key-bounds.py" ] || [ -f "$HERE/name-key-bounds.py" ]; then
	_nk="$LIB/tests/name-key-bounds.py"; [ -f "$_nk" ] || _nk="$HERE/name-key-bounds.py"
	# 11 invalid names (no records, no directories), an admin's name, and the API
	# key length rule - which must be the same from the CLI and from the page.
	if python3 "$_nk" >/tmp/regression-namekey.log 2>&1; then
		ok "name/key bounds: $(grep -E 'NAME KEY BOUNDS:' /tmp/regression-namekey.log | tail -1)"
	else
		bad "name/key bounds (see /tmp/regression-namekey.log)"
		grep -E '^FAIL' /tmp/regression-namekey.log | sed 's/^/      /' | head -5
	fi
else
	skip "name-key-bounds.py not installed"
fi

step "28/33 limit values (what 0, negative and unit forms really do)"
if [ -f "$LIB/tests/limit-values.py" ] || [ -f "$HERE/limit-values.py" ]; then
	_lv="$LIB/tests/limit-values.py"; [ -f "$_lv" ] || _lv="$HERE/limit-values.py"
	# 0 = unlimited in the friendly forms (verified against the live cgroup), raw 0
	# refused, negatives refused, units land as the right bytes/quota.
	if python3 "$_lv" >/tmp/regression-limitvalues.log 2>&1; then
		ok "limit values: $(grep -E 'LIMIT VALUES:' /tmp/regression-limitvalues.log | tail -1)"
	else
		bad "limit values (see /tmp/regression-limitvalues.log)"
		grep -E '^FAIL' /tmp/regression-limitvalues.log | sed 's/^/      /' | head -5
	fi
else
	skip "limit-values.py not installed"
fi

step "29/33 password minimum on every entrance"
if [ -f "$LIB/tests/password-bounds.py" ] || [ -f "$HERE/password-bounds.py" ]; then
	_pb="$LIB/tests/password-bounds.py"; [ -f "$_pb" ] || _pb="$HERE/password-bounds.py"
	# CLI, `user add` and the administrator's API must share one minimum; a refused
	# change must leave the account untouched, and 8 characters must really log in.
	if python3 "$_pb" >/tmp/regression-passwordbounds.log 2>&1; then
		ok "password bounds: $(grep -E 'PASSWORD BOUNDS:' /tmp/regression-passwordbounds.log | tail -1)"
	else
		bad "password bounds (see /tmp/regression-passwordbounds.log)"
		grep -E '^FAIL' /tmp/regression-passwordbounds.log | sed 's/^/      /' | head -5
	fi
else
	skip "password-bounds.py not installed"
fi

step "30/33 provider connection test"
if [ -f "$LIB/tests/provider-probe.py" ] || [ -f "$HERE/provider-probe.py" ]; then
	_pp="$LIB/tests/provider-probe.py"; [ -f "$_pp" ] || _pp="$HERE/provider-probe.py"
	# A local stub endpoint: a real reply, a 200 with junk, a 500, a refused port,
	# a file:// URL - plus "the key is never echoed" and "a slow probe does not
	# block the rest of the admin API".
	if python3 "$_pp" >/tmp/regression-providerprobe.log 2>&1; then
		ok "provider probe: $(grep -E 'PROVIDER PROBE:' /tmp/regression-providerprobe.log | tail -1)"
	else
		bad "provider probe (see /tmp/regression-providerprobe.log)"
		grep -E '^FAIL' /tmp/regression-providerprobe.log | sed 's/^/      /' | head -5
	fi
else
	skip "provider-probe.py not installed"
fi

step "31/33 model plans (patch + credential refs + the browser-session record)"
_pl="$LIB/tests/plans.py"; [ -f "$_pl" ] || _pl="$HERE/plans.py"
if [ -f "$_pl" ]; then
	if python3 "$_pl" >/tmp/regression-plans.log 2>&1; then
		ok "plans: $(tail -1 /tmp/regression-plans.log)"
	else
		bad "plans (see /tmp/regression-plans.log)"
		grep -E "^FAIL" /tmp/regression-plans.log | head -5 | sed 's/^/      /'
	fi
else
	skip "tests/plans.py not installed"
fi

step "32/33 provider templates (list / import / validate / guards, offline)"
_tpl="$LIB/tests/provider-templates.py"; [ -f "$_tpl" ] || _tpl="$HERE/provider-templates.py"
if [ -f "$_tpl" ]; then
	if python3 "$_tpl" >/tmp/regression-templates.log 2>&1; then
		ok "provider templates: $(grep -E 'PROVIDER TEMPLATES:' /tmp/regression-templates.log | tail -1)"
	else
		bad "provider templates (see /tmp/regression-templates.log)"
		grep -E "^FAIL" /tmp/regression-templates.log | head -5 | sed 's/^/      /'
	fi
else
	skip "provider-templates.py not installed"
fi

step "33/33 login rate limit (runs last: it burns the per-IP failure budget)"
if [ -f "$LIB/tests/login-limit.py" ] || [ -f "$HERE/login-limit.py" ]; then
	_lim="$LIB/tests/login-limit.py"; [ -f "$_lim" ] || _lim="$HERE/login-limit.py"
	if python3 "$_lim" >/tmp/regression-limit.log 2>&1; then
		ok "login limit: $(grep -E 'LOGIN LIMIT:' /tmp/regression-limit.log | tail -1)"
	else
		bad "login limit (see /tmp/regression-limit.log)"
		grep -E '^FAIL' /tmp/regression-limit.log | sed 's/^/      /' | head -5
	fi
else
	skip "login-limit.py not installed"
fi

printf '\n=== REGRESSION: PASS=%d FAIL=%d ===\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] && echo "ALL LAYERS PASSED" || echo "SOME LAYERS FAILED"
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
