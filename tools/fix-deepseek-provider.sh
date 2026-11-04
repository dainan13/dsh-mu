#!/bin/sh
# Rebuild the local `mu-deepseek` provider from the platform preset.
#
# Why this exists: an earlier hand-written `mu-deepseek` omitted the reasoning levels
# (so the drawer showed no effort picker) and could carry a text-only modality (so
# attachments were refused). The preset has the five wire-verified levels and no
# modality declaration, which is what we want: DSH then forwards images and the upstream
# decides.
#
# Usage: sh fix-deepseek-provider.sh [--yes]
set -eu
YES=0
[ "${1:-}" = "--yes" ] && YES=1
say() { printf '%s\n' "$*"; }
say "mu-deepseek repair  ($( [ "$YES" = 1 ] && echo EXECUTE || echo 'dry run' ))"
python3 - "$YES" <<'PY'
import sys
sys.path.insert(0, "/usr/local/lib/dsh-mu/python")
import dshmuctl as m

yes = sys.argv[1] == "1"
pkg = m.preset_as_package("deepseek")
pkg["id"] = "mu-deepseek"
pkg["label"] = "DeepSeek 官方（Anthropic 端点）"
pkg["origin"] = "custom"
for model in pkg.get("models") or []:
    # DeepSeek V4.1 Flash / V4 Pro accept images (this is what the harness itself runs
    # on). An earlier hand-written copy declared text-only, which is what made the
    # composer refuse attachments.
    model["inputModalities"] = ["text", "image"]

old = m.load_provider_package("mu-deepseek") or {}
print(f"  before: api={old.get('api')} models={[x['id'] for x in old.get('models') or []]}")
print(f"  after : api={pkg.get('api')} models={[x['id'] for x in pkg.get('models')]}")
for model in pkg["models"]:
    print(f"    {model['id']}: efforts={list((model.get('reasoningEfforts') or {}).keys())}")
if not yes:
    sys.exit(0)
m.save_provider_package(pkg)
# Every plan that pointed at the old definition keeps working: the id did not change.
print("  written")
PY
if [ "$YES" = 1 ]; then
	say ""
	say "--- sync + restart every instance ---"
	dsh-muctl sync 2>&1 | grep -E "wrote" | head -12
	# Do not yank an instance out from under someone mid-turn: a restart during a run
	# leaves the session marked "busy" and the next prompt is rejected with
	# session/agent-busy. Sessions touched in the last ten minutes count as active.
	for n in $(dsh-muctl instance list 2>/dev/null | awk 'NR>1 && $1!="" {print $1}'); do
		active=$(find "/var/lib/dsh-mu/instances/$n/home/.dsh/sessions" -type f -mmin -10 			2>/dev/null | head -1)
		if [ -n "${active:-}" ]; then
			say "  SKIPPED $n (session active in the last 10 minutes; restart it yourself later)"
			continue
		fi
		dsh-muctl instance restart "$n" >/dev/null 2>&1 && say "  restarted $n"
	done
	say ""
	say "--- verify (reasoningEfforts must appear in the patch) ---"
	grep -c "reasoningEfforts" /var/lib/dsh-mu/instances/*/home/.dsh/profiles/web/cordis.patch.yml 2>/dev/null | head -3
	dsh-muctl doctor 2>&1 | grep -E "plan ↔ instance patch|patch freshness|^FAIL|doctor:"
else
	say "  (dry run; re-run with --yes)"
fi
