#!/bin/sh
# Verify both gateway pages for real: stop the admin backend so `ensure` fails, then check
# that "/" shows the failure page, and that it goes back to "starting" once the backend is up.
# Creates and removes a temporary account; safe to re-run (it cleans up first).
set -u
J=/tmp/ft.jar; rm -f $J
say() { printf '%s\n' "$*"; }

say "=== 0) 清理上次残留 ==="
dsh-muctl instance del failtest >/dev/null 2>&1
dsh-muctl user del failtest >/dev/null 2>&1

say "=== 1) 建临时用户/实例 ==="
dsh-muctl user add failtest --password 'Ft-2026-x7q' 2>&1 | tail -1
sleep 3
PORT=$(dsh-muctl instance status failtest 2>/dev/null | awk '/failtest/{print $3}')
say "port=$PORT"

say "=== 2) 网关登录 ==="
CSRF=$(curl -s -c $J -b $J http://127.0.0.1/login | grep -o 'name="csrf" value="[^"]*"' | sed 's/.*value="//;s/"//')
curl -s -c $J -b $J -o /dev/null -X POST http://127.0.0.1/login \
  --data-urlencode "username=failtest" --data-urlencode "password=Ft-2026-x7q" \
  --data-urlencode "csrf=$CSRF" --data-urlencode "next=/"
say "cookie bytes: $(wc -c < $J)"

say "=== 3) 停实例 ==="
dsh-muctl instance stop failtest >/dev/null 2>&1
sleep 2

say "=== 4) 停 admin 后端 → ensure 失败 → 期望「实例启动失败」页 ==="
rc-service dsh-mud stop >/dev/null 2>&1
sleep 2
curl -s -c $J -b $J http://127.0.0.1/ -o /tmp/fail.html
grep -o "<title>[^<]*</title>" /tmp/fail.html
grep -oE "实例启动失败|没能启动|重试|退出登录|instance logs" /tmp/fail.html | sort -u | tr '\n' ' '; say ""

say "=== 5) 恢复 admin → 期望回到「实例正在启动」页 ==="
rc-service dsh-mud start >/dev/null 2>&1
sleep 3
curl -s -c $J -b $J http://127.0.0.1/ -o /tmp/wait.html
grep -o "<title>[^<]*</title>" /tmp/wait.html
grep -oE "实例正在启动|每 3 秒自动重试" /tmp/wait.html | sort -u | tr '\n' ' '; say ""

say "=== 6) 清理 ==="
dsh-muctl instance start failtest >/dev/null 2>&1
sleep 8
curl -s -o /dev/null -w "failtest=%{http_code}\n" "http://127.0.0.1:$PORT/" 2>/dev/null
dsh-muctl instance del failtest 2>&1 | tail -1
dsh-muctl user del failtest 2>&1 | tail -1
say "done"
