#!/bin/sh
# Make web_search work on a machine whose DeepSeek plan points at an OpenAI-protocol
# package: search needs api.deepseek.com + anthropic-messages.
#
# It never touches the existing `deepseek` package (imported from DSH) or the plan's key:
# it adds a sibling package `mu-deepseek` (same key, Anthropic endpoint) and repoints the
# plan at it. Idempotent; dry run by default.
set -eu
PLAN="${1:-}"
YES=0
[ "${2:-}" = "--yes" ] && YES=1
[ -n "$PLAN" ] || { echo "usage: $0 <plan-id> [--yes]"; echo "  plan ids:"; python3 -c "
import sys;sys.path.insert(0,'/usr/local/lib/dsh-mu/python');import dshmuctl as m
for pid,p in sorted(m.list_plans().items()):
    pkg=m.plan_package(p) or {}
    print('   ',pid,p.get('name'),'provider=',p.get('provider'),'api=',pkg.get('api'))"; exit 1; }

say() { printf '%s\n' "$*"; }
say "plan: $PLAN  ($( [ $YES = 1 ] && echo EXECUTE || echo 'DRY RUN' ))"

python3 - "$PLAN" "$YES" <<'PY'
import json, sys
sys.path.insert(0, '/usr/local/lib/dsh-mu/python')
import dshmuctl as m

plan_id, yes = sys.argv[1], sys.argv[2] == "1"
plan = m.load_plan(plan_id)
if not plan:
    sys.exit(f"no such plan: {plan_id}")
pkg = m.plan_package(plan) or {}
print(f"  current provider={plan.get('provider')} api={pkg.get('api')} base={pkg.get('base_url')}")

sibling = "mu-deepseek"
pkg_new = {
    "schema": "dsh-mu.provider/v1",
    "id": sibling,
    "label": "DeepSeek 官方（Anthropic 端点）",
    "docs": "https://api-docs.deepseek.com/",
    "auth": {"type": "x-api-key"},
    "api": "anthropic-messages",
    "base_url": "https://api.deepseek.com/anthropic",
    "default_model": "deepseek-flash",
    "models": [
        {"id": "deepseek-flash", "name": "DeepSeek V4.1 Flash"},
        {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
    ],
    "origin": "custom",
}
if not yes:
    print(f"  would write provider package {sibling} (anthropic-messages)")
    print(f"  would repoint plan {plan_id}: {plan.get('provider')} -> {sibling}")
    sys.exit(0)
m.save_provider_package(pkg_new)
plan = dict(plan)
plan["provider"] = sibling
m.save_plan(plan)
print(f"  wrote provider {sibling}; plan {plan_id} -> {sibling}")
PY

say ""
say "--- sync + restart every instance ---"
if [ "$YES" = 1 ]; then
	dsh-muctl sync 2>&1 | grep -E "profile patch: .*wrote" | head -10
	for n in $(dsh-muctl instance list 2>/dev/null | awk 'NR>1 && $1!="" {print $1}'); do
		dsh-muctl instance restart "$n" >/dev/null 2>&1 && say "  restarted $n"
	done
	sleep 15
	say ""
	say "--- verify ---"
	grep -c "__search" /var/lib/dsh-mu/instances/*/home/.dsh/profiles/web/cordis.patch.yml 2>/dev/null | head -3
	dsh-muctl doctor 2>&1 | grep -E "web search|patch freshness|^FAIL|doctor:"
	for p in 3100 3101 3102 3103 3104 3105 3106 3107; do
		printf "%s:%s " "$p" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/")"
	done; echo
else
	say "  (dry run; re-run with --yes)"
fi
