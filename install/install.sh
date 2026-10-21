#!/bin/sh
# install.sh - deploy the dsh-mu platform on this host (Alpine + OpenRC).
#
#   sh install.sh [--gateway-host HOST] [--model-api-key KEY] [--force-template]
#
# Copies the platform files into place, installs the runtime dependencies and
# then hands over to `dsh-muctl install` for packages, DSH, template and services.
set -eu

if [ "$(id -u)" != 0 ]; then
	echo "install.sh: must run as root" >&2
	exit 1
fi

SRC=$(cd "$(dirname "$0")/.." && pwd)
echo "install.sh: source=$SRC"

# The control plane is a Python program, so python3 has to exist before we can
# call it (everything else is handled by `dsh-muctl install`).
if ! command -v python3 >/dev/null 2>&1; then
	echo "install.sh: installing python3 ..."
	apk add --no-cache python3
fi

mkdir -p /usr/local/sbin /usr/local/lib/dsh-mu/lua /usr/local/lib/dsh-mu/python \
	/usr/local/lib/dsh-mu/nginx /usr/local/lib/dsh-mu/tests /var/log/dsh-mu /etc/dsh-mu

install -m 0644 "$SRC/cli/dshmuctl.py" /usr/local/lib/dsh-mu/python/dshmuctl.py
install -m 0755 "$SRC/cli/dsh-muctl" /usr/local/sbin/dsh-muctl
install -m 0755 "$SRC/jail/dsh-mu-jail" /usr/local/sbin/dsh-mu-jail
install -m 0755 "$SRC/jail/dsh-mu-cgroup-init" /usr/local/sbin/dsh-mu-cgroup-init
install -m 0755 "$SRC/admin/dsh-mud" /usr/local/sbin/dsh-mud
install -d -m 0755 /usr/local/lib/dsh-mu/admin
install -m 0644 "$SRC/admin/index.html" /usr/local/lib/dsh-mu/admin/index.html
for f in "$SRC"/gateway/lua/*.lua; do
	install -m 0644 "$f" "/usr/local/lib/dsh-mu/lua/$(basename "$f")"
done
# nginx fragments (templates): `dsh-muctl gateway install` renders and installs
# them into the host's include directories and patches the managed include into
# /etc/nginx/nginx.conf. The main nginx.conf is never replaced.
for f in "$SRC"/gateway/dsh-mu-*.conf; do
	install -m 0644 "$f" "/usr/local/lib/dsh-mu/nginx/$(basename "$f")"
done
mkdir -p /usr/local/lib/dsh-mu/static
for f in "$SRC"/gateway/static/*; do
	install -m 0644 "$f" "/usr/local/lib/dsh-mu/static/$(basename "$f")"
done
# Documentation lands in /usr/share/doc/dsh-mu so a fresh machine can be operated
# from its own copy (DELIVERY.md is the handoff sheet).
mkdir -p /usr/share/doc/dsh-mu/docs
for doc in README.md DELIVERY.md CHANGELOG.md; do
	[ -f "$SRC/$doc" ] && install -m 0644 "$SRC/$doc" /usr/share/doc/dsh-mu/
done
for f in "$SRC"/docs/*.md; do
	[ -e "$f" ] || continue
	install -m 0644 "$f" "/usr/share/doc/dsh-mu/docs/$(basename "$f")"
done

[ -d "$SRC/tests" ] && for f in "$SRC"/tests/*.mjs "$SRC"/tests/*.sh "$SRC"/tests/*.py; do
	[ -e "$f" ] || continue
	install -m 0644 "$f" "/usr/local/lib/dsh-mu/tests/$(basename "$f")"
done

echo "install.sh: files in place; handing over to dsh-muctl install"
exec /usr/local/sbin/dsh-muctl install "$@"
