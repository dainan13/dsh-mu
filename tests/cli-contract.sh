#!/bin/sh
# cli-contract.sh - the command line must fail cleanly.
#
#   sh tests/cli-contract.sh [instance]
#
# Operators see the CLI's error paths more often than its happy path (typos,
# wrong names, values out of range). This battery runs ~20 deliberately wrong
# invocations and asserts each one
#   * exits non-zero,
#   * prints a human-facing message ("dsh-muctl: ..." or an argparse usage),
#   * never leaks a Python traceback,
# and a handful of read-only invocations that must exit 0 (including --json
# output that has to parse).
#
# Nothing here mutates the platform: every failing case is rejected before any
# state is touched, and the positive cases only read.
set -u

. "$(dirname -- "$0")/lib.sh" 2>/dev/null || {
	echo "tests/lib.sh is missing next to this script; install the tests/ directory as a whole" >&2
	exit 2
}

CLI=$DSHMU_CLI
INSTANCE=${1:-}
[ -n "$CLI" ] && [ -x "$CLI" ] || { echo "dsh-muctl not found (set DSHMU_CLI)" >&2; exit 2; }
[ -n "$INSTANCE" ] || INSTANCE=$(t_first_instance)
[ -n "$INSTANCE" ] || INSTANCE=noinst

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); t_ok "$@"; }
bad() { FAIL=$((FAIL+1)); t_bad "$@"; }

run() { # run <label> -> OUT, RC
	OUT=$("$@" 2>&1)
	RC=$?
}

no_traceback() { # no_traceback <label> <output>
	case "$2" in
		*Traceback*) bad "$1 leaked a Python traceback: $(printf '%s' "$2" | tail -2 | tr '\n' ' ')" ; return 1 ;;
	esac
	return 0
}

expect_error() { # expect_error <label> <args...>
	label=$1; shift
	run "$CLI" "$@"
	if [ "$RC" -eq 0 ]; then
		bad "$label (expected failure, got exit 0)"
		return
	fi
	no_traceback "$label" "$OUT" || return
	case "$OUT" in
		*dsh-muctl:*|*usage:*) ok "$label (exit $RC)" ;;
		*) bad "$label (exit $RC but no readable message: $(printf '%s' "$OUT" | head -1))" ;;
	esac
}

expect_ok() { # expect_ok <label> <args...>
	label=$1; shift
	run "$CLI" "$@"
	if [ "$RC" -ne 0 ]; then
		bad "$label (expected success, got exit $RC: $(printf '%s' "$OUT" | tail -1))"
		return
	fi
	no_traceback "$label" "$OUT" || return
	ok "$label"
}

echo "== CLI contract: $CLI, instance=$INSTANCE =="

# The Alpine release and the npm registry are "detected" in the sense that the
# library derives them (Alpine from /etc/os-release, registry from the config with
# a public default). Ask the library directly instead of trusting a green doctor.
ALPINE_CHECK=$(python3 - "$DSHMU_LIB_DIR" <<'PY'
import importlib.util, os, re, sys
lib = sys.argv[1].rstrip("/") + "/python/dshmuctl.py"
spec = importlib.util.spec_from_file_location("dshmuctl_probe", lib)
mu = importlib.util.module_from_spec(spec); sys.modules["dshmuctl_probe"] = mu
spec.loader.exec_module(mu)
want = None
try:
    with open("/etc/os-release") as fh:
        for line in fh:
            if line.startswith("VERSION_ID="):
                parts = line.split("=", 1)[1].strip().strip('"').split(".")
                want = f"v{parts[0]}.{parts[1]}"
except OSError:
    pass
auto = mu.resolve_alpine_version({"alpine_version": "auto"})
explicit = mu.resolve_alpine_version({"alpine_version": "v9.9"})
host = mu.host_alpine_version()
env_off = mu.npm_env({})
env_on = mu.npm_env({"npm_registry": "https://example.invalid/npm"})
print(f"want={want} auto={auto} explicit={explicit} host={host} "
      f"env_off={env_off} env_on={sorted(env_on)} "
      f"env_on_value={env_on.get('npm_config_registry')}")
PY
)
_want=$(printf '%s' "$ALPINE_CHECK" | sed -n 's/.*want=\([^ ]*\) .*/\1/p')
_auto=$(printf '%s' "$ALPINE_CHECK" | sed -n 's/.*auto=\([^ ]*\) .*/\1/p')
_explicit=$(printf '%s' "$ALPINE_CHECK" | sed -n 's/.*explicit=\([^ ]*\) .*/\1/p')
if [ -z "$_want" ] || [ "$_want" = "None" ]; then
	skip "cannot read /etc/os-release for the Alpine detection check"
elif [ "$_auto" = "$_want" ]; then
	ok "ALPINE_VERSION=auto follows this host ($_auto)"
else
	bad "ALPINE_VERSION=auto is $_auto but the host is $_want"
fi
[ "$_explicit" = "v9.9" ] && ok "an explicit ALPINE_VERSION is used verbatim" \
	|| bad "explicit ALPINE_VERSION was not honoured: $_explicit"
case "$ALPINE_CHECK" in
	*"env_off={}"*) ok "no npm registry configured means no forced registry" ;;
	*) bad "npm_env() forces a registry when none is configured: $ALPINE_CHECK" ;;
esac
case "$ALPINE_CHECK" in
	*"env_on_value=https://example.invalid/npm"*)
		case "$ALPINE_CHECK" in
			*NPM_CONFIG_REGISTRY*npm_config_registry*)
				ok "a configured npm registry is exported to npm and pnpm" ;;
			*) bad "the registry is exported under the wrong names: $ALPINE_CHECK" ;;
		esac ;;
	*) bad "the configured npm registry is not exported: $ALPINE_CHECK" ;;
esac

# ---------------------------------------------------------------- bad input
expect_error "unknown command" bogus-command
expect_error "no arguments" || true
expect_error "instance status of a missing instance" instance status nosuchinstance
expect_error "instance del of a missing instance" instance del nosuchinstance
expect_error "instance start of a missing instance" instance start nosuchinstance
expect_error "instance restart of a missing instance" instance restart nosuchinstance
expect_error "instance exec on a missing instance" instance exec nosuchinstance -- true
expect_error "instance port of a missing instance" instance port nosuchinstance --port 3200
expect_error "port outside the allowed range" instance port "$INSTANCE" --port 99999
expect_error "instance limits of a missing instance" instance limits nosuchinstance --cpu 1
expect_error "non-numeric raw cpu limit" instance limits "$INSTANCE" --cpu notanumber
expect_error "non-numeric memory limit" instance limits "$INSTANCE" --mem-mb lots
expect_error "instance provider of a missing instance" instance provider nosuchinstance deepseek
expect_error "instance api-key of a missing instance" instance api-key nosuchinstance --key abc
expect_error "instance create without a name" instance create
expect_error "provider show of a missing provider" provider show nosuchprovider
# A built-in can only be "removed" if an operator-defined provider shadows its name,
# so pick one that is definitely still built-in.
_BUILTIN=""
for _cand in opencode deepseek uniapi; do
	if [ "$(dsh-muctl provider list 2>/dev/null | awk -v n="$_cand" '$1==n {print $NF; exit}')" != "custom" ]; then
		_BUILTIN="$_cand"
		break
	fi
done
if [ -n "$_BUILTIN" ]; then
	expect_error "removing a built-in provider ($_BUILTIN)" provider rm "$_BUILTIN"
else
	echo "SKIP  removing a built-in provider (every candidate is operator-defined here)"
fi
expect_error "provider model of a missing provider" provider model nosuchprovider gpt
expect_error "unknown config key" config nosuchkey value
expect_error "config value of the wrong kind" config session_days notanumber
expect_error "restoring a backup that does not exist" restore /nonexistent-backup.tgz --dry-run
expect_error "user passwd of a missing user" user passwd nosuchuser --password whatever-123
expect_error "user del of a missing user" user del nosuchuser
expect_error "user kick of a missing user" user kick nosuchuser
expect_error "user kick without a name" user kick

# ------------------------------------------------------- read-only successes
expect_ok "--help" --help
expect_ok "--version" --version
expect_ok "config dump" config
expect_ok "user list" user list
expect_ok "instance list" instance list
expect_ok "instance status --json" instance status --json
expect_ok "provider list" provider list
expect_ok "instance status" instance status "$INSTANCE"
expect_ok "instance exec <instance> -- true" instance exec "$INSTANCE" -- true

# JSON output has to be machine-readable.
if "$CLI" doctor --json >/tmp/cli-contract-doctor.json 2>/dev/null; then
	if python3 -c 'import json,sys; json.load(open("/tmp/cli-contract-doctor.json"))' 2>/dev/null; then
		ok "doctor --json emits valid JSON"
	else
		bad "doctor --json did not emit valid JSON"
	fi
else
	bad "doctor --json exited non-zero"
fi

if "$CLI" instance list >/dev/null 2>&1 && "$CLI" instance list --help >/dev/null 2>&1; then
	ok "instance list --help works"
fi

echo
echo "=== CLI CONTRACT: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" = 0 ] && echo "CLI CONTRACT PASSED" || echo "CLI CONTRACT FAILED"
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
