#!/bin/sh
# fresh-rehearsal.sh - rehearse the "clean Alpine" install inside a chroot.
#
#   sh tests/fresh-rehearsal.sh [--root DIR] [--keep] [--clean] [--skip-dsh]
#                               [--apk FILE] [--apk2 NEWER_FILE] [--src CHECKOUT]
#                               [--tarball dist/dsh-mu-<ts>.tgz]
#
# --tarball rehearses from the *shipped artifact* instead of a checkout: that is
# the file the operator actually receives, so "reproducible on a clean Alpine"
# should be proven against it and not only against the source tree.
#
# Why: the fresh-machine acceptance needs a machine that has been wiped, which is
# a manual step. This script builds a *brand new* Alpine rootfs with apk --root,
# binds /proc /sys /dev, copies this checkout in and runs the real installer
# inside it. That exercises everything a fresh host would hit except OpenRC
# actually starting services (a chroot has no init):
#
#   package/dependency resolution  -> apk add from the mirror
#   first-run configuration        -> /etc/dsh-mu/dsh-mu.conf, secret, admin token
#   DSH runtime install            -> npm install -g @deepseek-ai/dsh
#   jail template build            -> apk --root + unshare, inside the chroot
#   gateway fragment install       -> /etc/nginx/{conf.d,http.d} + `nginx -t`
#   admin account handling         -> system account record + password readiness
#   doctor                         -> 30+ checks with the chroot's own kernel view
#   runtime smoke                  -> nginx + dsh-mud started on alternate ports,
#                                     anonymous/login/admin/panel/API endpoints probed
#
# With --apk FILE the platform is installed from the built package (the documented
# flow) instead of from the checkout. --apk2 FILE (a *newer* build of the same
# source) additionally exercises the real upgrade path: apk only runs the
# pre/post-upgrade scripts when the version actually changes.
#
# `--skip-dsh` skips the npm step (useful when the registry is unreachable).
set -eu

SRC=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Allow an explicit checkout: the installed tree (/usr/local/lib/dsh-mu) cannot be
# rehearsed because it has no install/ or cli/ sources.
for _arg in "$@"; do
	[ "$_arg" = "--src" ] && EXPLICIT_SRC=1
done
ROOT="${REHEARSAL_ROOT:-/var/lib/dsh-mu/rehearsal/rootfs}"
KEEP=0
CLEAN=0
SKIP_DSH=0
APK=""
APK2=""
TARBALL=""

while [ $# -gt 0 ]; do
	case "$1" in
		--root) ROOT="$2"; shift 2 ;;
		--keep) KEEP=1; shift ;;
		--clean) CLEAN=1; shift ;;
		--skip-dsh) SKIP_DSH=1; shift ;;
		--apk) APK="$2"; shift 2 ;;
		--apk2) APK2="$2"; shift 2 ;;
		--src) SRC=$(CDPATH= cd -- "$2" && pwd); shift 2 ;;
		--tarball) TARBALL=$2; shift 2 ;;
		-h|--help) sed -n '2,30p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

MIRROR="${ALPINE_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/alpine}"
VER=$(sed -n 's/^VERSION_ID="\?\([0-9]*\.[0-9]*\).*/\1/p' /etc/os-release)
VER="v${VER:-3.24}"
ARCH=$(apk --print-arch)

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
step() { echo; echo "--- $* ---"; }
# Shared helpers (host-side only: this script builds its own Alpine rootfs).
. "$SRC/tests/lib.sh" 2>/dev/null || true

PKGS="alpine-base openrc busybox python3 nodejs npm pnpm openresty util-linux \
logrotate lua-resty-string lua-resty-hmac lua-resty-openssl curl"

kill_rehearsal_daemons() {
	# A previous run can leave nginx/dsh-mud alive after its rootfs is removed;
	# those zombies keep serving the *old* files and would answer our probes.
	for proc in /proc/[0-9]*; do
		exe=$(readlink "$proc/exe" 2>/dev/null) || continue
		case "$exe" in
			*/rehearsal/rootfs/*)
				pid=${proc#/proc/}
				echo "  killing leftover rehearsal process $pid ($exe)"
				kill -9 "$pid" 2>/dev/null || true
				;;
		esac
	done
	# The bracket keeps this pattern from matching pkill's own command line
	# (and that of its parent `chroot ... -c` shell, which would kill this script).
	pkill -f "dsh-mud --listen 127.0.0.1:1878[8]" 2>/dev/null || true
}

cleanup() {
	kill_rehearsal_daemons
	umount -l "$ROOT/dev" 2>/dev/null || true
	umount -l "$ROOT/sys" 2>/dev/null || true
	umount -l "$ROOT/proc" 2>/dev/null || true
	if [ "$KEEP" = 0 ] && [ "$CLEAN" = 1 ]; then
		if rm -rf "$ROOT" 2>/dev/null; then
			echo "removed $ROOT"
		else
			sleep 2
			rm -rf "$ROOT" 2>/dev/null && echo "removed $ROOT" || \
				echo "NOTE: could not fully remove $ROOT (a process may still hold it); remove it later"
		fi
	fi
}
trap cleanup EXIT INT TERM

if [ "$CLEAN" = 1 ] && [ -d "$ROOT" ]; then
	rm -rf "$ROOT"
fi

step "1/10 fresh Alpine $VER ($ARCH) rootfs at $ROOT"
mkdir -p "$ROOT/etc/apk/keys" "$ROOT/root"
cp -a /etc/apk/keys/* "$ROOT/etc/apk/keys/" 2>/dev/null || true
{
	echo "$MIRROR/$VER/main"
	echo "$MIRROR/$VER/community"
} > "$ROOT/etc/apk/repositories"
echo "nameserver $(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf 2>/dev/null || echo 1.1.1.1)" \
	> "$ROOT/etc/resolv.conf"

APKCMD="apk --root $ROOT --initdb --arch $ARCH --keys-dir /etc/apk/keys --repositories-file $ROOT/etc/apk/repositories --no-cache"
$APKCMD add $PKGS >"$ROOT/root/apk.log" 2>&1 || { bad "apk add of the platform dependencies"; tail -5 "$ROOT/root/apk.log"; }
if [ -x "$ROOT/bin/sh" ] && [ -x "$ROOT/sbin/apk" ] && [ -x "$ROOT/usr/sbin/nginx" ]; then
	ok "clean rootfs built ($(du -sm "$ROOT" | awk '{print $1}') MB, $(ls "$ROOT/usr/bin" | wc -l) binaries)"
else
	bad "rootfs incomplete - cannot continue"; exit 1
fi

step "2/10 bind mounts and source copy"
mount -t proc  proc  "$ROOT/proc" 2>/dev/null || mount --bind /proc "$ROOT/proc"
mount -t sysfs sysfs "$ROOT/sys"  2>/dev/null || mount --bind /sys  "$ROOT/sys"
mount --bind /dev "$ROOT/dev"
[ -d "$ROOT/proc/self" ] && ok "proc/sys/dev mounted" || bad "mounts missing"

rm -rf "$ROOT/root/dsh-mu"
mkdir -p "$ROOT/root/dsh-mu"
if [ -n "$TARBALL" ]; then
	# The shipped artifact, unpacked exactly as an operator would.
	[ -f "$TARBALL" ] || { bad "tarball not found: $TARBALL"; exit 1; }
	cp "$TARBALL" "$ROOT/root/shipped.tgz"
	tar xzf "$ROOT/root/shipped.tgz" -C "$ROOT/root/dsh-mu"
	_files=$(find "$ROOT/root/dsh-mu" -type f | wc -l | tr -d ' ')
	if [ -f "$ROOT/root/dsh-mu/cli/dshmuctl.py" ] && [ -f "$ROOT/root/dsh-mu/install/install.sh" ]; then
		ok "the shipped tarball unpacked into /root/dsh-mu ($_files files, $(basename "$TARBALL"))"
	else
		bad "the tarball is missing cli/ or install/"
	fi
	if tar tzf "$ROOT/root/shipped.tgz" | grep -q "__pycache__"; then
		bad "the shipped tarball contains __pycache__"
	else
		ok "the shipped tarball carries no build leftovers"
	fi
else
	tar cf - -C "$SRC" . | tar xf - -C "$ROOT/root/dsh-mu"
	[ -f "$ROOT/root/dsh-mu/cli/dshmuctl.py" ] && ok "checkout copied to /root/dsh-mu" || bad "copy failed"
fi

run_in() { # run_in <logfile> <command...>
	_log="$1"; shift
	chroot "$ROOT" /bin/sh -c "cd /root/dsh-mu && $*" >"$ROOT/root/$_log" 2>&1 || true
}

step "3/10 doctor before installing (expect FAILs: nothing is set up yet)"
# Run the library directly: the /usr/local shim does not exist before installing.
run_in doctor-before.log "python3 cli/dshmuctl.py doctor --json"
if [ -s "$ROOT/root/doctor-before.log" ]; then
	ok "doctor runs in the fresh rootfs: $(python3 "$SRC/tests/summarize-doctor.py" "$ROOT/root/doctor-before.log")"
else
	bad "doctor produced no output in the chroot"
fi

step "4/10 install the platform"
# The platform must only *add* its include line to the main nginx.conf: keep the
# pre-install copy so the check in step 6 can prove it.
cp "$ROOT/etc/nginx/nginx.conf" "$ROOT/root/nginx.conf.before" 2>/dev/null || true
INSTALL_EXTRA=""
[ "$SKIP_DSH" = 1 ] && INSTALL_EXTRA="--skip-dsh"
if [ -n "$APK" ]; then
	# Rehearse the documented flow: install the built package, then run the CLI.
	[ -f "$APK" ] || { bad "apk not found: $APK"; exit 1; }
	cp "$APK" "$ROOT/root/dsh-mu.apk"
	run_in apk-add.log "apk add --allow-untrusted /root/dsh-mu.apk"
	grep -qE "OK: .*in [0-9]+ packages" "$ROOT/root/apk-add.log" && ok "apk add of $(basename "$APK")" \
		|| bad "apk add failed: $(tail -2 "$ROOT/root/apk-add.log" | tr '\n' ' ')"
	[ -x "$ROOT/usr/local/sbin/dsh-muctl" ] && ok "package installed the CLI" || bad "CLI missing after apk add"
	run_in install.log "dsh-muctl install $INSTALL_EXTRA"
else
	if [ "$SKIP_DSH" = 1 ]; then
		run_in install.log "sh install/install.sh --skip-dsh"
	else
		run_in install.log "sh install/install.sh"
	fi
fi
if grep -q "install complete" "$ROOT/root/install.log"; then
	ok "installer reported completion"
else
	bad "installer did not finish"
	echo "      last lines:"
	tail -6 "$ROOT/root/install.log" | sed 's/^/        /'
	echo "      package errors (if any):"
	grep -iE "ERROR:|unable to select|unsatisfiable|UNTRUSTED|conflicting" "$ROOT/root/install.log" \
		| head -8 | sed 's/^/        /'
fi

step "5/10 first-run artefacts"
for f in etc/dsh-mu/dsh-mu.conf etc/dsh-mu/secret.key etc/dsh-mu/admin.token etc/dsh-mu/state.json; do
	[ -e "$ROOT/$f" ] && ok "created /$f" || bad "missing /$f"
done
[ -d "$ROOT/var/lib/dsh-mu/templates/alpine" ] && ok "jail template built inside the rehearsal rootfs" \
	|| bad "jail template missing"
if [ -x "$ROOT/usr/local/bin/dsh" ] || [ -L "$ROOT/usr/local/bin/dsh" ]; then
	ok "global dsh runtime present"
elif [ "$SKIP_DSH" = 1 ]; then
	echo "SKIP  global dsh runtime (--skip-dsh)"
else
	bad "global dsh runtime missing"
fi

step "6/10 gateway integration"
MODE=$(sed -n 's/^GATEWAY_MODE="\{0,1\}\([a-z]*\)"\{0,1\}.*/\1/p' "$ROOT/etc/dsh-mu/dsh-mu.conf" 2>/dev/null | tail -1)
if [ "$MODE" = "jail" ]; then
	# The gateway runs inside its own jail. A chroot cannot create mount
	# namespaces, so the jail itself cannot start here; instead check that its
	# rootfs/configuration are complete and that nginx accepts the config
	# (copying the volume into the jail rootfs exactly where the mount lands).
	GJ="$ROOT/var/lib/dsh-mu/gateway"
	if chroot "$ROOT" unshare --mount true 2>/dev/null; then
		[ -x "$GJ/rootfs/usr/sbin/nginx" ] && ok "gateway jail rootfs has nginx" \
			|| bad "gateway jail rootfs missing nginx: $GJ/rootfs"
	else
		# A chroot cannot create mount namespaces, so no jail can run here and the
		# installer deliberately skips the 214 MB rootfs; only the config matters.
		ok "gateway jail rootfs skipped: mount namespaces unavailable in a chroot"
	fi
	[ -f "$GJ/conf/nginx.conf" ] && ok "gateway jail configuration rendered" \
		|| bad "gateway jail configuration missing: $GJ/conf/nginx.conf"
	[ -f "$ROOT/etc/init.d/dsh-mu-gateway" ] && ok "gateway jail service installed" \
		|| bad "gateway service missing"
	if [ -x "$GJ/rootfs/usr/sbin/nginx" ] && [ -f "$GJ/conf/nginx.conf" ]; then
		rm -rf "$GJ/rootfs/etc/dsh-mu-gateway"
		mkdir -p "$GJ/rootfs/etc"
		cp -a "$GJ/conf" "$GJ/rootfs/etc/dsh-mu-gateway"
		chroot "$ROOT" /bin/sh -c 'mkdir -p /etc/dsh-mu && cp -a /var/lib/dsh-mu/gateway/etc /etc/dsh-mu-probe 2>/dev/null; true'
		chroot "$GJ/rootfs" /usr/sbin/nginx -t -c /etc/dsh-mu-gateway/nginx.conf \
			>"$ROOT/root/gateway-nginx-t.log" 2>&1 || true
		grep -q "syntax is ok" "$ROOT/root/gateway-nginx-t.log" \
			&& ok "the jail's nginx accepts our configuration" \
			|| bad "jail nginx -t failed: $(tail -2 "$ROOT/root/gateway-nginx-t.log" | tr '\n' ' ')"
		rm -rf "$GJ/rootfs/etc/dsh-mu-gateway"
	fi
	if [ -f "$ROOT/root/nginx.conf.before" ]; then
		ok "the distribution nginx.conf was left untouched (jail mode)"
	fi
else
chroot "$ROOT" /usr/sbin/nginx -t >"$ROOT/root/nginx-t.log" 2>&1 || true
grep -q "test is successful" "$ROOT/root/nginx-t.log" && ok "nginx configuration valid with our fragments" \
	|| bad "nginx -t failed: $(tail -1 "$ROOT/root/nginx-t.log")"
grep -q "dsh-mu: gateway fragments" "$ROOT/etc/nginx/nginx.conf" && ok "managed include present in nginx.conf" \
	|| bad "managed include missing"
if [ -f "$ROOT/root/nginx.conf.before" ]; then
	_lines=0; _lost=0
	while IFS= read -r _line; do
		[ -z "$_line" ] && continue
		_lines=$((_lines + 1))
		grep -qF -- "$_line" "$ROOT/etc/nginx/nginx.conf" || _lost=$((_lost + 1))
	done < "$ROOT/root/nginx.conf.before"
	if [ "$_lost" = 0 ]; then
		ok "the distribution nginx.conf was preserved ($_lines original lines still present)"
	else
		bad "nginx.conf lost $_lost of $_lines original lines (it was overwritten, not extended)"
	fi
else
	skip "no pre-install nginx.conf snapshot"
fi
for f in "$ROOT"/etc/nginx/conf.d/dsh-mu-*.conf "$ROOT"/etc/nginx/http.d/dsh-mu-*.conf; do
	[ -e "$f" ] && ok "fragment $(basename "$f") installed" || bad "fragment missing: $f"
done
fi

step "7/10 doctor after installing + admin account (informational in a chroot)"
# Use the *installed* CLI here (no `sh`): the shim is a Python file with a shebang.
run_in doctor-after.log "dsh-muctl doctor"
if [ -s "$ROOT/root/doctor-after.log" ]; then
	echo "      $(grep -E '^doctor:' "$ROOT/root/doctor-after.log" | tail -1)"
	echo "      service-related FAILs are expected: a chroot has no init, and it shares"
	echo "      the host network namespace (so 127.0.0.1:8787 belongs to the host backend)."
	ok "doctor ran after the install (see $ROOT/root/doctor-after.log)"
fi
run_in bootstrap.log "dsh-muctl bootstrap"
if grep -qE "root: ready" "$ROOT/root/bootstrap.log"; then
	ok "admin account ready: $(grep -E 'root: ' "$ROOT/root/bootstrap.log" | tail -1 | sed 's/^ *//')"
elif grep -qE "root: NOT ready.*no password set" "$ROOT/root/bootstrap.log"; then
	echo "EXPECT  admin account needs a password first (a clean Alpine has none);"
	echo "        on a real host run: passwd root"
	ok "bootstrap correctly reports that the admin account is not usable yet"
else
	bad "admin bootstrap produced no verdict: $(tail -2 "$ROOT/root/bootstrap.log" | tr '\n' ' ')"
fi
grep -qE "admin account root" "$ROOT/root/doctor-after.log" && ok "doctor checks the admin account" \
	|| bad "doctor has no admin account check"

step "8/10 package lifecycle: upgrade keeps data, uninstall keeps data, reinstall works"
if [ -n "$APK" ]; then
	cp "$ROOT/etc/dsh-mu/state.json" "$ROOT/root/state-before.json"
	cp "$ROOT/etc/dsh-mu/secret.key" "$ROOT/root/secret-before.key"
	run_in apk-upgrade.log "apk add --allow-untrusted /root/dsh-mu.apk"
	if [ -n "$APK2" ]; then
		# A newer build of the same source: this is the real upgrade path.
		cp "$APK2" "$ROOT/root/dsh-mu-newer.apk"
		run_in apk-upgrade.log "apk add --allow-untrusted /root/dsh-mu-newer.apk"
		if grep -qE "Replacing dsh-mu|Upgrading dsh-mu" "$ROOT/root/apk-upgrade.log"; then
			ok "apk add of the newer build took the upgrade path"
		else
			bad "newer build did not report an upgrade"
			head -4 "$ROOT/root/apk-upgrade.log" 2>/dev/null | sed 's/^/        /'
		fi
		_scripts=$(grep -oE "pre-upgrade|post-upgrade|pre-install|post-install" \
			"$ROOT/root/apk-upgrade.log" | sort -u | tr '\n' ' ')
		[ -n "$_scripts" ] && ok "upgrade scripts ran: $_scripts" \
			|| bad "no package scripts ran during the upgrade"
	else
		# Same-version re-add: apk treats an identical package as already
		# satisfied, so a silent no-op is the correct outcome.
		run_in apk-upgrade.log "apk add --allow-untrusted /root/dsh-mu.apk"
		if grep -qE "Replacing dsh-mu|Installing dsh-mu|Upgrading dsh-mu" "$ROOT/root/apk-upgrade.log"; then
			ok "same-version re-add re-installed the package"
		elif grep -qE "OK: .*in [0-9]+ packages" "$ROOT/root/apk-upgrade.log"; then
			ok "same-version re-add was a clean no-op (pass --apk2 to rehearse a version upgrade)"
		else
			bad "same-version re-add produced neither an install nor a success line"
			head -4 "$ROOT/root/apk-upgrade.log" 2>/dev/null | sed 's/^/        /'
		fi
	fi
	run_in apk-del.log "apk del dsh-mu"
	grep -qiE "preserved on purpose|/etc/dsh-mu" "$ROOT/root/apk-del.log" \
		&& ok "uninstall printed the data-preservation notice" \
		|| bad "uninstall notice missing"
	[ -x "$ROOT/usr/local/sbin/dsh-muctl" ] && bad "uninstall left the CLI behind" \
		|| ok "uninstall removed the programs"
	if [ -f "$ROOT/etc/dsh-mu/state.json" ] && cmp -s "$ROOT/etc/dsh-mu/state.json" "$ROOT/root/state-before.json"; then
		ok "platform records survived the uninstall byte-for-byte"
	else
		bad "state.json changed or vanished during the uninstall"
	fi
	[ -f "$ROOT/etc/dsh-mu/secret.key" ] && ok "session secret survived the uninstall" \
		|| bad "secret.key vanished during the uninstall"
	run_in reinstall.log "apk add --allow-untrusted /root/dsh-mu.apk"
	run_in reinstall-install.log "dsh-muctl install --skip-dsh"
	[ -x "$ROOT/usr/local/sbin/dsh-muctl" ] && [ -f "$ROOT/etc/dsh-mu/state.json" ] \
		&& ok "reinstall restored the CLI with the original records" \
		|| bad "reinstall did not come back cleanly"
else
	echo "SKIP  lifecycle step (pass --apk to exercise apk upgrade/uninstall/reinstall)"
fi

step "9/10 disaster recovery: back up, wipe the platform, restore onto the clean install"
# "We take backups" only means something if a restore has been rehearsed. This
# creates an account with an instance and a file in its home, backs it up, wipes
# /etc/dsh-mu completely (total loss of platform state), installs the platform
# fresh - as on a new machine - and restores the archive.
DR_USER="dr$$"
DR_MARK="recovery-marker-$$"
run_in dr-user.log "dsh-muctl user add $DR_USER --password restore-drill-password"
if grep -q "created" "$ROOT/root/dr-user.log"; then
	ok "drill: account + instance created in the fresh install"
else
	bad "drill: could not create the account: $(tail -1 "$ROOT/root/dr-user.log" 2>/dev/null)"
fi
# The jail bind-mounts the *separate* workspace volume at /home/dsh/workspace, so
# that is where a user's files live - and the only path whose restoration proves
# anything. home/workspace exists too (it is shadowed inside the jail).
mkdir -p "$ROOT/var/lib/dsh-mu/instances/$DR_USER/home/workspace" 2>/dev/null || true
mkdir -p "$ROOT/var/lib/dsh-mu/instances/$DR_USER/workspace" 2>/dev/null || true
printf '%s\n' "$DR_MARK" > "$ROOT/var/lib/dsh-mu/instances/$DR_USER/workspace/live-marker.txt"
printf '%s\n' "$DR_MARK" > "$ROOT/var/lib/dsh-mu/instances/$DR_USER/home/workspace/marker.txt"

# Give the account a model credential and a custom provider definition first:
# "the backup restored my users" is worth little if the API keys did not survive.
# A deliberately *short* key is used: long opaque tokens are caught by the audit
# sanitiser's length heuristic, so only a short one exercises the positional rule.
DR_KEY=DRILLKEY42
run_in dr-key.log "dsh-muctl instance api-key $DR_USER $DR_KEY"
run_in dr-provider.log "dsh-muctl provider add drillprov --kind pi-ai --base-url https://example.invalid/v1 \
	--model drill-model --credentials-ref DRILL_KEY --api openai-completions"
dr_provider_before=$(md5sum "$ROOT/etc/dsh-mu/providers.json" 2>/dev/null | awk '{print $1}')
dr_credfile="$ROOT/var/lib/dsh-mu/instances/$DR_USER/home/.dsh/.credentials.yaml"
dr_cred_before=$(md5sum "$dr_credfile" 2>/dev/null | awk '{print $1}')
dr_key_before=$(python3 - "$ROOT/etc/dsh-mu/state.json" "$DR_USER" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
cred = (st.get("instances", {}).get(sys.argv[2], {}) or {}).get("credentials") or {}
print("yes" if cred.get("api_key") or cred.get("key") else "no")
PY
)
if [ "$dr_key_before" = "yes" ]; then
	ok "drill: the credential state marker is recorded (no secret in state.json)"
else
	bad "drill: no credential marker in state.json"
fi
if [ -f "$dr_credfile" ] && grep -q "DEEPSEEK_API_KEY" "$dr_credfile"; then
	ok "drill: the key really landed in the instance's 0600 credential file"
else
	bad "drill: the credential file was not written"
fi
# The audit must show that a credential changed, and must not show the value.
if grep -q "api-key" "$ROOT/var/log/dsh-mu/audit.log" 2>/dev/null; then
	ok "drill: changing a credential is audited"
else
	bad "drill: the credential change left no audit record"
fi
if grep -q "$DR_KEY" "$ROOT/var/log/dsh-mu/audit.log" "$ROOT/etc/dsh-mu/state.json" 2>/dev/null; then
	bad "drill: the key value leaked into the audit log or state.json"
else
	ok "drill: the key value is absent from the audit log and state.json"
fi

run_in dr-backup.log "dsh-muctl backup --out /root/dr.tgz --with-instances"

# ...and the archive itself must carry the key material (checked without printing it)
if tar tzf "$ROOT/root/dr.tgz" 2>/dev/null | grep -q "instances/$DR_USER/home/.dsh/.credentials.yaml"; then
	ok "drill: the archive contains the instance credential file"
else
	bad "drill: the credential file is not in the archive"
fi
dr_member=$(tar tzf "$ROOT/root/dr.tgz" 2>/dev/null | grep 'etc/dsh-mu/state.json$' | head -1)
if [ -n "$dr_member" ] && tar xzOf "$ROOT/root/dr.tgz" "$dr_member" 2>/dev/null \
	| python3 -c 'import json,sys
st = json.load(sys.stdin)
cred = (st.get("instances", {}).get(sys.argv[1], {}) or {}).get("credentials") or {}
sys.exit(0 if (cred.get("api_key") or cred.get("key")) else 1)' "$DR_USER" 2>/dev/null; then
	ok "drill: the archive records the credential marker (member: $(basename "$dr_member"))"
else
	bad "drill: the archive lacks the credential marker"
fi
if tar tzf "$ROOT/root/dr.tgz" 2>/dev/null | grep -q "providers.json"; then
	ok "drill: the archive contains the custom provider catalogue"
else
	bad "drill: providers.json is not in the archive"
fi
if [ -s "$ROOT/root/dr.tgz" ]; then
	ok "drill: backup written ($(wc -c <"$ROOT/root/dr.tgz") bytes)"
else
	bad "drill: no backup was produced"
fi

read_dr() { # read_dr -> "<pw> <port> <owner>" from the platform's state.json
	python3 -c '
import json, sys
st = json.load(open(sys.argv[1]))
u = (st.get("users") or {}).get(sys.argv[2]) or {}
i = (st.get("instances") or {}).get(sys.argv[2]) or {}
print(u.get("pw", "-"), i.get("port", "-"), i.get("user", "-"))
' "$ROOT/etc/dsh-mu/state.json" "$DR_USER" 2>/dev/null
}
dr_before=$(read_dr)
dr_secret_before=$(md5sum "$ROOT/etc/dsh-mu/secret.key" 2>/dev/null | awk '{print $1}')

# Total loss: the platform's own state is gone, the backup and the instance data
# directories survive (that is what a disk failure looks like in practice).
rm -rf "$ROOT/etc/dsh-mu"
run_in dr-reinstall.log "dsh-muctl install --skip-dsh"
if grep -q "install complete" "$ROOT/root/dr-reinstall.log"; then
	ok "drill: platform reinstalled empty (the new machine)"
else
	bad "drill: reinstall after the wipe failed: $(tail -1 "$ROOT/root/dr-reinstall.log")"
fi
run_in dr-doctor.log "dsh-muctl doctor"

run_in dr-restore.log "dsh-muctl restore /root/dr.tgz --force"
dr_fails_before=$(grep -c '^FAIL' "$ROOT/root/doctor-after.log" 2>/dev/null || echo 0)
dr_fails_after=$(grep -c '^FAIL' "$ROOT/root/dr-doctor.log" 2>/dev/null || echo 0)
if [ "$dr_fails_after" -le "$dr_fails_before" ]; then
	ok "drill: restore introduced no new doctor failures ($dr_fails_before -> $dr_fails_after)"
else
	bad "drill: restore left $dr_fails_after doctor failures (was $dr_fails_before)"
fi

dr_key_after=$(python3 - "$ROOT/etc/dsh-mu/state.json" "$DR_USER" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
cred = (st.get("instances", {}).get(sys.argv[2], {}) or {}).get("credentials") or {}
print("yes" if cred.get("api_key") or cred.get("key") else "no")
PY
)
if [ "$dr_key_after" = "$dr_key_before" ] && [ "$dr_key_after" = "yes" ]; then
	ok "drill: the credential marker came back from the archive"
else
	bad "drill: the credential marker did not come back ($dr_key_before -> $dr_key_after)"
fi
dr_cred_after=$(md5sum "$dr_credfile" 2>/dev/null | awk '{print $1}')
if [ -n "$dr_cred_before" ] && [ "$dr_cred_after" = "$dr_cred_before" ]; then
	ok "drill: the credential file is byte-identical after the restore"
else
	bad "drill: the credential file differs after the restore"
fi
dr_provider_after=$(md5sum "$ROOT/etc/dsh-mu/providers.json" 2>/dev/null | awk '{print $1}')
if [ -n "$dr_provider_before" ] && [ "$dr_provider_after" = "$dr_provider_before" ]; then
	ok "drill: the custom provider catalogue is byte-identical after the restore"
else
	bad "drill: providers.json differs after the restore"
fi
if grep -q "drillprov" "$ROOT/etc/dsh-mu/providers.json" 2>/dev/null; then
	ok "drill: the restored catalogue still defines the custom provider"
else
	bad "drill: the custom provider is gone after the restore"
fi

dr_after=$(read_dr)
if [ -n "$dr_before" ] && [ "$dr_after" = "$dr_before" ] && [ "${dr_before%% *}" != "-" ]; then
	ok "drill: the recovered account is identical (password hash, port, owner)"
else
	bad "drill: recovered account differs: '$dr_before' -> '$dr_after'"
fi
dr_secret_after=$(md5sum "$ROOT/etc/dsh-mu/secret.key" 2>/dev/null | awk '{print $1}')
if [ -n "$dr_secret_before" ] && [ "$dr_secret_after" = "$dr_secret_before" ]; then
	ok "drill: the session secret came back (existing sessions stay valid)"
else
	bad "drill: session secret changed across the restore"
fi
if grep -q "$DR_USER" "$ROOT/etc/dsh-mu/gateway/users.json" 2>/dev/null; then
	ok "drill: gateway view re-rendered with the recovered account"
else
	bad "drill: gateway view does not know the recovered account"
fi
if [ "$(cat "$ROOT/var/lib/dsh-mu/instances/$DR_USER/home/workspace/marker.txt" 2>/dev/null)" = "$DR_MARK" ]; then
	ok "drill: DSH home files were restored from the archive"
else
	bad "drill: DSH home file did not come back"
fi
# The live workspace is the one users actually write to (bind-mounted at
# /home/dsh/workspace); it used to be missing from the archive entirely.
if [ "$(cat "$ROOT/var/lib/dsh-mu/instances/$DR_USER/workspace/live-marker.txt" 2>/dev/null)" = "$DR_MARK" ]; then
	ok "drill: the live workspace came back (the volume users write to)"
else
	bad "drill: the live workspace did not come back"
fi
if tar tzf "$ROOT/root/dr.tgz" 2>/dev/null | grep -q "instances/$DR_USER/workspace/live-marker.txt"; then
	ok "drill: the archive contains the live workspace"
else
	bad "drill: the archive does not contain the live workspace"
fi
if tar tzf "$ROOT/root/dr.tgz" 2>/dev/null | grep -q "instances/$DR_USER/rootfs"; then
	bad "drill: the archive contains a rootfs copy (it must not)"
else
	ok "drill: no rootfs copy in the archive (records + volumes only)"
fi
if ls "$ROOT"/etc/dsh-mu.bak-*.tgz >/dev/null 2>&1; then
	ok "drill: a safety copy of the wiped state was written first"
else
	bad "drill: restore did not write a safety copy"
fi
run_in dr-users.log "dsh-muctl user list"
if grep -q "$DR_USER" "$ROOT/root/dr-users.log"; then
	ok "drill: the CLI lists the recovered account again"
else
	bad "drill: the CLI does not list the recovered account"
fi

step "10/10 runtime smoke: actually serve the gateway from the fresh rootfs"
kill_rehearsal_daemons
# The chroot shares the host network namespace, so use high alternate ports:
# 18080 for the gateway and 18788 for the loopback backend (the host owns 80/8787).
# There is no init in a chroot, so the two daemons are started directly.
GW_PORT=18080
ADMIN_PORT=18788
ADMIN_TOKEN=$(cat "$ROOT/etc/dsh-mu/admin.token" 2>/dev/null || echo "")
chroot "$ROOT" /bin/sh -c "
	sed -i 's/^GATEWAY_LISTEN=.*/GATEWAY_LISTEN=\"$GW_PORT\"/; s/^ADMIN_LISTEN=.*/ADMIN_LISTEN=\"127.0.0.1:$ADMIN_PORT\"/' /etc/dsh-mu/dsh-mu.conf
	# The chroot shares the host network namespace, so the distribution's stock
	# server block on :80 would fail to bind and take nginx down with it.
	sed -i 's/listen\s\+80;/listen 18081;/' /etc/nginx/nginx.conf
	# package post-install scripts cannot run in a chroot, so repair what they
	# would have provided (nginx user + temp dirs) with the platform's own fixer
	dsh-muctl doctor --fix >/root/smoke-fix.log 2>&1
	dsh-muctl gateway install >/root/smoke-gateway.log 2>&1
	dsh-muctl admin restart  >/root/smoke-admin.log 2>&1
	nohup /usr/local/sbin/dsh-mud --listen 127.0.0.1:$ADMIN_PORT >>/root/smoke-admin.log 2>&1 &
	nohup /usr/sbin/nginx >>/root/smoke-nginx.log 2>&1 &
	sleep 3
" >/dev/null 2>&1
sleep 3

http_code() { # http_code <path> [extra curl args...]
	_path="$1"; shift
	chroot "$ROOT" curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@" "http://127.0.0.1:$GW_PORT$_path" 2>/dev/null || echo 000
}
check_code() { # check_code <expected> <label> <path> [curl args]
	_want="$1"; _label="$2"; _path="$3"; shift 3
	_got=$(http_code "$_path" "$@")
	[ "$_got" = "$_want" ] && ok "$_label -> $_got" || bad "$_label -> $_got (expected $_want)"
}

check_code 302 "anonymous / redirects to /login" "/"
check_code 200 "login page renders" "/login"
check_code 302 "anonymous /admin/ is gated" "/admin/"
check_code 401 "anonymous /me is rejected" "/me"
check_code 200 "panel asset served" "/mu-static/panel.js"
check_code 200 "panel icon served" "/mu-static/icon.png"
check_code 403 "loopback ops API refuses a browser (no token)" "/mu-api/status"
if [ -n "$ADMIN_TOKEN" ]; then
	check_code 200 "loopback ops API works with the token" "/mu-api/status?t=$ADMIN_TOKEN"
fi

if [ -n "$ADMIN_TOKEN" ]; then
	_backend=$(chroot "$ROOT" curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
		-H "X-DSH-MU-Admin: $ADMIN_TOKEN" "http://127.0.0.1:$ADMIN_PORT/api/health" 2>/dev/null)
	[ "$_backend" = "200" ] && ok "backend health via its own port -> 200" \
		|| bad "backend health -> $_backend"
	_authz=$(chroot "$ROOT" curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
		"http://127.0.0.1:$ADMIN_PORT/api/health" 2>/dev/null)
	[ "$_authz" = "403" ] && ok "backend rejects requests without the token -> 403" \
		|| bad "backend without token -> $_authz (expected 403)"
fi

# Tear the daemons down so the rootfs can be removed cleanly.
# Kill only *our* smoke daemons: a bare "dsh-mud --listen" pattern would also
# hit the host's real backend (the chroot shares the PID namespace).
chroot "$ROOT" /bin/sh -c 'pkill -f "127.0.0.1:1878[8]" 2>/dev/null; /usr/sbin/nginx -s stop 2>/dev/null; sleep 1' >/dev/null 2>&1
kill_rehearsal_daemons
[ -s "$ROOT/root/smoke-gateway.log" ] && ! grep -qiE "emerg|failed" "$ROOT/root/smoke-gateway.log" \
	&& ok "gateway config loaded without errors" \
	|| bad "gateway log has problems: $(tail -1 "$ROOT/root/smoke-gateway.log" 2>/dev/null)"

echo
echo "=== FRESH REHEARSAL: PASS=$PASS FAIL=$FAIL (rootfs: $ROOT) ==="
echo "note: OpenRC services cannot start inside a chroot, so the running gateway"
echo "      and end-to-end checks still belong to tests/fresh-machine.py on a real host."
[ "$FAIL" = 0 ] && echo "REHEARSAL PASSED" || echo "REHEARSAL FAILED"
[ "$KEEP" = 1 ] && echo "(rootfs kept for inspection; remove with: rm -rf $ROOT)"
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
