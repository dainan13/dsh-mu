#!/bin/sh
# M1 feasibility probe for dsh-mu instance jails.
# Verifies the isolation primitives the platform depends on, on the real target.
# Usage: sh m1-probe.sh [--keep]
set -u

TARGET="${TARGET:-https://mirrors.tuna.tsinghua.edu.cn/alpine}"
VER="${VER:-v3.24}"
PROBE="/var/lib/dsh-mu/probe"
R="$PROBE/rootfs"
CG="/sys/fs/cgroup/dsh-mu/probe"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

PASS=0; FAIL=0; SKIP=0
ok()  { PASS=$((PASS+1)); echo "PASS  $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL  $*"; }
skip(){ SKIP=$((SKIP+1)); echo "SKIP  $*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
ms()  { date +%s%N 2>/dev/null | tail -c 10; }

echo "=== M1 probe on $(hostname) ($(uname -r)) ==="
mkdir -p "$PROBE" /var/lib/dsh-mu/instances
# cgroup delegation: drain the populated cgroup namespace root, then hand the
# controllers to /sys/fs/cgroup/dsh-mu (see dsh-mu-cgroup-init).
if [ -x /root/dsh-mu-cgroup-init ]; then
    sh /root/dsh-mu-cgroup-init || true
else
    mkdir -p /sys/fs/cgroup/init
    for p in $(cat /sys/fs/cgroup/cgroup.procs 2>/dev/null); do
        echo "$p" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
    done
    for c in cpu memory pids; do echo "+$c" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true; done
    mkdir -p /sys/fs/cgroup/dsh-mu
    for c in cpu memory pids; do echo "+$c" > /sys/fs/cgroup/dsh-mu/cgroup.subtree_control 2>/dev/null || true; done
fi

# ---------------------------------------------------------------- T0 sanity
[ "$(id -u)" = 0 ] && ok "T0 running as root" || bad "T0 running as root"
mount | grep -q "cgroup2 on /sys/fs/cgroup" && ok "T0 cgroup2 mounted" || bad "T0 cgroup2 mounted"
have unshare && have setpriv && have chroot && ok "T0 util-linux present" || bad "T0 util-linux present"

# ---------------------------------------------------------------- T1 cgroup
echo "--- T1 cgroup v2 limits"
mkdir -p "$CG" 2>/dev/null && ok "T1 cgroup create" || bad "T1 cgroup create"
echo "+cpu +memory +pids" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null
if [ -w "$CG/memory.max" ]; then
    echo $((192*1024*1024)) > "$CG/memory.max" && ok "T1 memory.max writable" || bad "T1 memory.max writable"
else
    bad "T1 memory.max writable"
fi
echo 32 > "$CG/pids.max" 2>/dev/null && ok "T1 pids.max writable" || bad "T1 pids.max writable"
echo "50000 100000" > "$CG/cpu.max" 2>/dev/null && ok "T1 cpu.max writable" || bad "T1 cpu.max writable"

# T1a memory: allocate 512MB inside a 192MB cgroup -> expect OOM kill
if have node; then
    node -e '
      const fs=require("fs");
      fs.writeFileSync(process.argv[1]+"/cgroup.procs", String(process.pid));
      const a=[]; for(let i=0;i<64;i++) a.push(Buffer.alloc(8*1024*1024).fill(1));
      console.log("allocated", a.length*8, "MB");
    ' "$CG" >"$PROBE/t1a.out" 2>&1
    rc=$?
    killed=$(cat "$CG/memory.events" 2>/dev/null | awk '/^oom_kill/{print $2}')
    if [ "$rc" -ne 0 ] && [ "${killed:-0}" -ge 1 ]; then
        ok "T1a memory.max enforced (oom_kill=$killed rc=$rc)"
    else
        bad "T1a memory.max enforced (oom_kill=${killed:-0} rc=$rc) $(tail -1 "$PROBE/t1a.out")"
    fi
else
    skip "T1a memory.max enforced (node missing)"
fi

# T1b pids: fork until refusal inside pids.max=32
cat > "$PROBE/t1b.sh" <<EOF
echo \$\$ > $CG/cgroup.procs
i=0
while [ \$i -lt 200 ]; do
  sleep 30 &
  i=\$((i+1))
  echo "i=\$i"
done
EOF
sh "$PROBE/t1b.sh" >"$PROBE/t1b.out" 2>&1
maxforks=$(grep -c '^i=' "$PROBE/t1b.out")
if [ "$maxforks" -lt 40 ]; then
    ok "T1b pids.max enforced (spawned $maxforks <= ~32)"
else
    bad "T1b pids.max enforced (spawned $maxforks)"
fi
pkill -f 'sleep 30' 2>/dev/null

# T1c cpu: busy loop inside cpu.max=0.5 vs outside
busy() { i=0; while [ $i -lt 400000 ]; do i=$((i+1)); done; }
if have node; then
    node -e '
      const fs=require("fs"),cg=process.argv[1],inside=process.argv[2]==="1";
      if(inside) fs.writeFileSync(cg+"/cgroup.procs",String(process.pid));
      const t=Date.now(); let x=0; while(Date.now()-t<3000) x=Math.sqrt(x+1);
      console.log(Date.now()-t, x>0);
    ' "$CG" 0 > "$PROBE/t1c.out" 2>&1
    a=$(head -1 "$PROBE/t1c.out" | cut -d' ' -f1)
    node -e '
      const fs=require("fs"),cg=process.argv[1],inside=process.argv[2]==="1";
      if(inside) fs.writeFileSync(cg+"/cgroup.procs",String(process.pid));
      const t=Date.now(); let x=0; while(Date.now()-t<3000) x=Math.sqrt(x+1);
      console.log(Date.now()-t, x>0);
    ' "$CG" 1 > "$PROBE/t1c2.out" 2>&1
    b=$(head -1 "$PROBE/t1c2.out" | cut -d' ' -f1)
    # node prints elapsed wall; measure throughput instead: use /proc/self/stat utime? keep simple: run fixed iterations
    ok "T1c cpu.max set (throughput probe a=${a}ms b=${b}ms; see T1c-fixed)"
    # fixed-work comparison
    node -e '
      const fs=require("fs"),cg=process.argv[1],inside=process.argv[2]==="1";
      if(inside) fs.writeFileSync(cg+"/cgroup.procs",String(process.pid));
      const t=Date.now(); let x=0; for(let i=0;i<3e7;i++) x+=Math.sqrt(i);
      console.log(Date.now()-t, x>0);
    ' "$CG" 0 > "$PROBE/t1c3.out" 2>&1
    a=$(head -1 "$PROBE/t1c3.out" | cut -d' ' -f1)
    node -e '
      const fs=require("fs"),cg=process.argv[1],inside=process.argv[2]==="1";
      if(inside) fs.writeFileSync(cg+"/cgroup.procs",String(process.pid));
      const t=Date.now(); let x=0; for(let i=0;i<3e7;i++) x+=Math.sqrt(i);
      console.log(Date.now()-t, x>0);
    ' "$CG" 1 > "$PROBE/t1c4.out" 2>&1
    b=$(head -1 "$PROBE/t1c4.out" | cut -d' ' -f1)
    if [ "${b:-0}" -gt $(( ${a:-1} * 3 / 2 )) ]; then
        ok "T1c cpu.max throttles (free ${a}ms vs capped ${b}ms)"
    else
        bad "T1c cpu.max throttles (free ${a}ms vs capped ${b}ms)"
    fi
else
    skip "T1c cpu.max throttles (node missing)"
fi

# ------------------------------------------------ T2 rootfs build via apk --root
echo "--- T2 build jail rootfs with apk --root"
mkdir -p "$R/etc/apk"
printf '%s/%s/main\n%s/%s/community\n' "$TARGET" "$VER" "$TARGET" "$VER" > "$R/etc/apk/repositories"
if apk add --root "$R" --initdb --no-cache --arch x86_64 --keys-dir /etc/apk/keys \
        alpine-base apk-tools busybox util-linux bubblewrap >"$PROBE/t2.out" 2>&1; then
    ok "T2 apk --root --initdb builds rootfs"
else
    bad "T2 apk --root --initdb builds rootfs ($(tail -2 "$PROBE/t2.out" | tr '\n' ' '))"
fi
[ -x "$R/bin/busybox" ] && ok "T2 rootfs has busybox" || bad "T2 rootfs has busybox"
[ -f "$R/etc/apk/world" ] && ok "T2 rootfs has own apk world" || bad "T2 rootfs has own apk world"
du -sm "$R" 2>/dev/null | awk '{print "      rootfs size: "$1" MB"}'

# ------------------------------------------------ T3 chroot + namespace + apk isolation
echo "--- T3 namespace + chroot + apk isolation"
cat > "$PROBE/t3.sh" <<EOF
set -e
mount --rbind /proc "$R/proc"
mount --rbind /sys "$R/sys"; mount --make-rslave "$R/sys"
mount --rbind /dev "$R/dev"; mount --make-rslave "$R/dev"
mkdir -p "$R/etc"; cp /etc/resolv.conf "$R/etc/resolv.conf" 2>/dev/null || true
mount -t tmpfs -o size=32m,mode=1777 tmpfs "$R/tmp"
chroot "$R" /bin/sh -c 'cat /etc/alpine-release; hostname; apk add --no-cache hello >/dev/null 2>&1 && hello || echo APK_INSIDE_FAIL'
EOF
if unshare --mount --uts --propagation private --fork sh "$PROBE/t3.sh" >"$PROBE/t3.out" 2>&1; then
    grep -q "Hello, world" "$PROBE/t3.out" && ok "T3 apk add inside jail works" || bad "T3 apk add inside jail ($(tail -2 "$PROBE/t3.out" | tr '\n' ' '))"
else
    bad "T3 unshare+chroot ($(tail -2 "$PROBE/t3.out" | tr '\n' ' '))"
fi
if [ -e /usr/bin/hello ] || [ -e /bin/hello ]; then
    bad "T3 jail apk leaked to host"
else
    ok "T3 jail apk did not leak to host"
fi
[ -f "$R/usr/bin/hello" ] && ok "T3 package installed inside rootfs only" || bad "T3 package installed inside rootfs only"

# ------------------------------------------------ T4 non-root + caps + userns/bwrap
echo "--- T4 drop to non-root, drop caps, userns/bwrap"
cat > "$PROBE/t4.sh" <<EOF
set -e
mount --rbind /proc "$R/proc"
mount --rbind /sys "$R/sys"; mount --make-rslave "$R/sys"
mount --rbind /dev "$R/dev"; mount --make-rslave "$R/dev"
chroot "$R" /bin/sh -c '
  apk add --no-cache bubblewrap >/dev/null 2>&1 || true
  setpriv --reuid=1000 --regid=1000 --clear-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs /bin/sh -c "
    echo \"    id: \$(id)\"
    if touch /etc/_probe_write 2>/dev/null; then echo \"    WRITE_ETC_OK(BAD)\"; rm -f /etc/_probe_write; else echo \"    WRITE_ETC_DENIED(good)\"; fi
    if unshare -U -m true 2>/dev/null; then echo \"    USERNS_OK\"; else echo \"    USERNS_FAIL\"; fi
    if command -v bwrap >/dev/null 2>&1; then
      if bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid true 2>/dev/null; then echo \"    BWRAP_OK\"; else echo \"    BWRAP_FAIL\"; fi
    else echo \"    BWRAP_MISSING\"; fi
  "
'
EOF
unshare --mount --uts --propagation private --fork sh "$PROBE/t4.sh" >"$PROBE/t4.out" 2>&1
sed -n '1,20p' "$PROBE/t4.out" | sed 's/^/      /'
grep -q WRITE_ETC_DENIED "$PROBE/t4.out" && ok "T4 non-root cannot write jail /etc" || bad "T4 non-root cannot write jail /etc"
if grep -q USERNS_OK "$PROBE/t4.out"; then ok "T4 unprivileged userns available"; else bad "T4 unprivileged userns available"; fi
if grep -q BWRAP_OK "$PROBE/t4.out"; then ok "T4 bwrap sandbox usable in jail"; else bad "T4 bwrap sandbox usable in jail"; fi
if grep -q WRITE_ETC_OK "$PROBE/t4.out"; then bad "T4 write isolation"; else ok "T4 write isolation"; fi

# ------------------------------------------------ T5 cgroup placement of a jailed tree
echo "--- T5 cgroup placement"
cat > "$PROBE/t5.sh" <<EOF
echo \$\$ > $CG/cgroup.procs
sleep 8 &
wait
EOF
unshare --mount --fork sh "$PROBE/t5.sh" &
sleep 1
members=$(cat "$CG/cgroup.procs" 2>/dev/null | wc -l)
[ "$members" -ge 1 ] && ok "T5 processes land in instance cgroup (members=$members)" || bad "T5 processes land in instance cgroup (members=$members)"
wait 2>/dev/null

# ---------------------------------------------------------------- summary
echo "--- cleanup"
if [ "$KEEP" = 0 ]; then
    pkill -f 'sleep 30' 2>/dev/null
    umount -R "$R/proc" 2>/dev/null; umount -R "$R/sys" 2>/dev/null; umount -R "$R/dev" 2>/dev/null; umount "$R/tmp" 2>/dev/null
    rmdir "$CG" 2>/dev/null
fi
rm -rf "$PROBE" 2>/dev/null

echo
echo "=== M1 RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIP ==="
[ "$FAIL" = 0 ] && echo "M1 OK" || echo "M1 HAS FAILURES"
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
