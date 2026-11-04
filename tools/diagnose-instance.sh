#!/bin/sh
# Find out why one instance will not start, and try the safe repairs in order.
# Usage: sh diagnose-instance.sh <instance-name>
set -u
N="${1:-}"
[ -n "$N" ] || { echo "usage: $0 <instance-name>"; dsh-muctl instance list 2>/dev/null | awk 'NR>1 && $1!="" {print "  "$1}'; exit 1; }
B=/var/lib/dsh-mu/instances/$N
P=$B/home/.dsh/profiles/web/cordis.patch.yml
O=$B/home/.dsh-mu-provider.yml

say() { printf '\n=== %s ===\n' "$*"; }

say "1) 服务与监听"
dsh-muctl instance status "$N" 2>&1 | head -6
ss -ltn 2>/dev/null | grep -E ":$(dsh-muctl instance status "$N" 2>/dev/null | awk 'NR==2{print $3}') " || echo "  端口没有监听 ✗"

say "2) 启动错误（最关键 ✓）"
tail -30 /var/log/dsh-mu/$N.err.log 2>/dev/null | sed 's/^/  /'

say "3) 两处 patch 的结构"
echo "  --- 旧 overlay: $O"
tail -5 "$O" 2>/dev/null | sed 's/^/    /'
echo "  --- profile patch: $P"
head -3 "$P" 2>/dev/null | sed 's/^/    /'
grep -nE "agent-default-model|provider:|model:" "$P" 2>/dev/null | tail -6 | sed 's/^/    /'

say "4) 该实例的默认套餐/用户覆盖（是否指向已删的套餐）"
python3 - "$N" <<'PY'
import sys, json
sys.path.insert(0, '/usr/local/lib/dsh-mu/python')
import dshmuctl as m
name = sys.argv[1]
st = m.load_state()
inst = st['instances'].get(name) or {}
user = str(inst.get('user') or name)
print('  user:', user)
plans = {p['id']: p for p in m.plans_for_user(user)}
print('  可见套餐:', list(plans))
cfg_default = str((st.get('config') or {}).get('default_plan') or '')
print('  config.default_plan:', cfg_default or '(空)')
override = m._with_user_override(st, user) if hasattr(m, '_with_user_override') else {}
print('  用户覆盖:', json.dumps(override, ensure_ascii=False)[:200])
PY

say "5) 尝试修复：sync → 重启"
dsh-muctl sync 2>&1 | grep -E "$N.*(wrote|refus|WARNING)" | sed 's/^/  /'
dsh-muctl instance restart "$N" 2>&1 | tail -3 | sed 's/^/  /'
sleep 15
PORT=$(dsh-muctl instance status "$N" 2>/dev/null | awk 'NR==2{print $3}')
printf '  当前: port %s -> ' "${PORT:-?}"
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:${PORT:-0}/" 2>/dev/null || echo "?"
say "6) 如果还是 closed：看这一段"
tail -12 /var/log/dsh-mu/$N.err.log 2>/dev/null | sed 's/^/  /'
