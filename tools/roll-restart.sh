#!/bin/sh
# Rolling restart: one instance at a time, verify 401 after each, stop at the first failure.
set -u
python3 - <<'PY' > /tmp/list.txt
import sys
sys.path.insert(0, "/usr/local/lib/dsh-mu/python")
import dshmuctl as m
st = m.load_state()
for n, i in sorted(st["instances"].items()):
    print(n, i["port"])
PY
echo "--- instances ---"
cat /tmp/list.txt
failed=""
while read -r n p; do
  printf '=== %s (%s) ===\n' "$n" "$p"
  dsh-muctl instance restart "$n" 2>&1 | tail -1
  ok=0
  i=1
  while [ "$i" -le 6 ]; do
    sleep 6
    c=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/")
    if [ "$c" = "401" ]; then ok=1; echo "  -> 401 ok after $((i*6))s"; break; fi
    i=$((i + 1))
  done
  if [ "$ok" = "0" ]; then
    failed="$n"
    echo "  -> FAILED; last log lines:"
    tail -6 "/var/log/dsh-mu/$n.err.log"
    break
  fi
done < /tmp/list.txt
echo "--- final ---"
while read -r n p; do
  printf '%s:%s ' "$n" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/")"
done < /tmp/list.txt
echo
[ -n "$failed" ] && echo "STOPPED AT: $failed"
