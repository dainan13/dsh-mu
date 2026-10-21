#!/usr/bin/env python3
"""dsh-muctl - control plane for the DeepSeek Harness multi-instance platform.

Manages local accounts, per-user jailed DSH instances, cgroup limits and the
OpenRC services that supervise them. The gateway itself is pure OpenResty+Lua;
this CLI is its only writer and the single source of truth (`state.json`).

Everything here is Python standard library only.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import glob
import getpass
import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

def _platform_version() -> str:
    """The package version, as installed next to this file (`/usr/local/lib/dsh-mu/VERSION`)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "VERSION"), os.path.join(here, "..", "VERSION")):
        try:
            with open(cand) as fh:
                text = fh.read().strip()
            if text:
                return text
        except OSError:
            continue
    return "0.0.0-dev"


VERSION = _platform_version()

ETC = "/etc/dsh-mu"
STATE_PATH = f"{ETC}/state.json"
STATE_LOCK_PATH = f"{ETC}/.state.lock"
SECRET_PATH = f"{ETC}/secret.key"
GW_DIR = f"{ETC}/gateway"
INST_CONF_DIR = f"{ETC}/instances"
LIB = "/usr/local/lib/dsh-mu"
LIB_PY = f"{LIB}/python"
VAR = "/var/lib/dsh-mu"
TEMPLATE = f"{VAR}/templates/alpine"
INSTANCES_DIR = f"{VAR}/instances"
LOGS = "/var/log/dsh-mu"
RUN_DIR = "/run"
JAIL = "/usr/local/sbin/dsh-mu-jail"
CGROUP_INIT = "/usr/local/sbin/dsh-mu-cgroup-init"
SELF = "/usr/local/sbin/dsh-muctl"
DSH_RUNTIME = "/usr/local/lib/node_modules/@deepseek-ai/dsh"


def dsh_runtime_dir(cfg: dict | None = None) -> str:
    """Host path of the shared DSH runtime.

    jail mode (the default): inside the gateway rootfs, which carries node/npm, so
    the host needs no node at all and instances bind-mount the package read-only.
    legacy host-nginx mode still uses the host's global npm directory.
    """
    cfg = cfg if cfg is not None else load_config()
    if str(cfg.get("gateway_mode") or "jail") != "jail":
        return DSH_RUNTIME
    return f"{VAR}/gateway/rootfs{DSH_RUNTIME}"

STATE_SCHEMA = 2
CONF_PATH = f"{ETC}/dsh-mu.conf"
PROVIDERS_PATH = f"{ETC}/providers.json"
PROVIDERS_DIR = f"{VAR}/providers"

# ------------------------------------------------------------------ configuration
#
# Platform configuration lives in /etc/dsh-mu/dsh-mu.conf: shell syntax, so
# `sh` scripts (installer, doctor) can source it, parsed here for the CLI and
# the admin backend. `state.json` keeps only data (users, instances).
#
# Each row is (conf variable, config key, kind, default, documentation). The
# schema is the single source of truth: the package installs the rendered
# `dsh-mu.conf.default` from it. Keys not in the schema are ignored (a warning
# is printed for an unsupported value) and are dropped on the next rewrite.
CONF_SCHEMA: list[tuple[str, str, str, object, str]] = [
    ("GATEWAY_HOST", "gateway_host", "str", "dsh-mu",
     "对外主机名 / 域名：登录页展示、/login 跳转目标、实例代理的 Host 归属"),
    ("GATEWAY_LISTEN", "gateway_listen", "int", 80,
     "网关 nginx 监听端口"),
    ("INSTANCE_KEEP_CAPS", "instance_keep_caps", "optstr", "sys_admin,setuid,setgid",
     "实例保留的 capability（逗号分隔，空=全部丢弃）。DSH 的命令工具用 bwrap 沙箱，"
     "在 jail 里需要 sys_admin（建命名空间/挂载）+ setuid/setgid（uid 映射）"),
    ("TLS_ENABLED", "tls_enabled", "bool", False,
     "启用 HTTPS：网关同时监听 443（证书在 /etc/dsh-mu/tls/）"),
    ("TLS_KEEP_HTTP", "tls_keep_http", "bool", True,
     "启用 HTTPS 后是否继续在 80 端口提供服务（关闭则只监听 443）"),
    ("TLS_REDIRECT", "tls_redirect", "bool", False,
     "启用 HTTPS 后把 80 的请求 301 到 https（与 tls_keep_http 互斥时以本项为准）"),
    ("RELAY_ENABLED", "relay_enabled", "bool", True,
     "套餐的 key 走网关代理（实例里只有占位符 `mu-relay`）；关掉则回到直连、真 key 写进实例"),
    ("PROVIDER_TEMPLATE_BASE", "provider_template_base", "str",
     "https://raw.githubusercontent.com/dainan13/dsh-mu/main/provider-packages",
     "线上 provider 模板库地址（https://… 或 file://…；留空用项目仓库默认）"),
    ("RELAY_LISTEN", "relay_listen", "int", 8099,
     "代理监听的 loopback 端口（只给本机 jail 用，不对外）"),
    # 24h by default: an unattended browser tab should not stay signed in for a week.
    # Set to 0 to disable, or raise it if long-running tasks must survive a day idle.
    ("SESSION_IDLE_MINUTES", "session_idle_minutes", "int", 1440,
     "空闲多久自动登出（分钟，0=不启用；默认 1440=24 小时）；会话 cookie 未过期但长时间未使用也会失效"),
    ("SESSION_DAYS", "session_days", "int", 7,
     "平台会话 cookie 有效期（天）"),
    ("ADMIN_SYSTEM_USERS", "admin_system_users", "str", "root",
     "拥有管理权限的系统账号（用系统口令在 /etc/shadow 校验，空格或逗号分隔）"),
    ("PORT_RANGE", "port_range", "range", [3100, 3999],
     "实例端口区间（含两端），实例只监听 127.0.0.1"),
    ("UID_BASE", "uid_base", "int", 60000,
     "INSTANCE_RUN_UID=auto 时，为实例分配专用 uid 的起始值"),
    ("DEFAULT_CPU_MAX", "default_cpu_max", "str", "100000 100000",
     "每实例默认 cpu.max（quota period，单位 us；50000 100000 = 半个核）"),
    ("DEFAULT_MEM_MAX", "default_mem_max", "int", 1610612736,
     "每实例默认 memory.max（字节，硬限，触顶 OOM）"),
    ("DEFAULT_MEM_HIGH", "default_mem_high", "int", 1342177280,
     "每实例默认 memory.high（字节，软限，先压制回收）"),
    ("DEFAULT_PIDS_MAX", "default_pids_max", "int", 512,
     "每实例默认 pids.max（进程/线程数上限）"),
    ("INSTANCE_RUN_UID", "instance_run_uid", "uid", 0,
     "实例在 jail 内以哪个 uid 运行：0=jail root（capability 全丢，apk 可用）；auto=每实例分配专用 uid"),
    ("INSTANCE_PERMISSION_MODE", "instance_permission_mode", "str", "danger-full-access",
     "实例内 DSH agent 沙箱模式：danger-full-access|workspace-write|read-only（本 LXC 无 bwrap/Landlock，故默认全开）"),
    ("DEFAULT_PROVIDER", "default_provider", "str", "deepseek",
     "新实例默认 provider preset（deepseek|opencode-go|opencode）"),
    ("DSH_EXTRA_ARGS", "dsh_extra_args", "str", "",
     "追加到 `dsh web` 的应用参数（原样拼接）"),
    ("DSH_VERSION", "dsh_version", "str", "0.2.0-rc.2",
     "全局安装的 @deepseek-ai/dsh 版本（latest 表示跟随最新）"),
    ("ALPINE_MIRROR", "alpine_mirror", "str", "https://mirrors.tuna.tsinghua.edu.cn/alpine",
     "构建 jail 模板使用的 Alpine 镜像根地址"),
    ("ALPINE_VERSION", "alpine_version", "str", "auto",
     "auto=按本机 /etc/os-release 推断（如 v3.24）；也可写死"),
    ("ALPINE_IMAGE_INDEX", "alpine_image_index", "str",
     "http://download.proxmox.com/images/system",
     "列出可用系统镜像的地址（Proxmox 官方目录；镜像站一般没有目录列表）"),
    ("ALPINE_IMAGE_BASE", "alpine_image_base", "str",
     "http://download.proxmox.com/images/system",
     "下载系统镜像的地址根（可指向镜像站，如 https://mirrors.tuna.tsinghua.edu.cn/proxmox/images/system）"),
    ("ALPINE_TEMPLATE_IMAGE", "alpine_template_image", "optstr", None,
     "指定模板文件名（如 alpine-3.24-default_20260714_amd64.tar.xz）；空=安装时列出来让你选，默认最新"),
    ("ALPINE_TEMPLATE_PACKAGES", "template_packages", "list",
     ["alpine-base", "apk-tools", "busybox", "bash", "git", "util-linux", "ripgrep",
      "ca-certificates", "curl", "bubblewrap", "nodejs", "npm", "pnpm", "procps"],
     "jail 模板内安装的 apk 包（node/npm/pnpm 是实例自己的，DSH 全局包从宿主只读绑定；"
     "ripgrep 是 harness 的 glob/grep 工具需要的，procps 提供 ps）"),
    ("NPM_REGISTRY", "npm_registry", "optstr", None,
     "npm registry（空=官方；国内可填 https://registry.npmmirror.com）"),
    ("MODEL_API_KEY", "model_api_key", "optstr", None,
     "平台统一模型 key（写入每个实例的凭据库；也可用 instance api-key 单独设置）"),
    ("GATEWAY_MODE", "gateway_mode", "str", "jail",
     "网关运行方式：jail=平台 jail 里的 openresty（默认，宿主不需要 nginx）；host=老方式（宿主 openresty）"),
    ("NGINX_HTTP_D_DIR", "nginx_http_d_dir", "str", "/etc/nginx/http.d",
     "server 级 nginx 片段目录（网关 server 块）"),
    ("NGINX_CONF_D_DIR", "nginx_conf_d_dir", "str", "/etc/nginx/conf.d",
     "http 级 nginx 片段目录（lua_shared_dict / map 等 http 级指令）"),
    ("ADMIN_LISTEN", "admin_listen", "str", "127.0.0.1:8787",
     "admin 后端监听地址（仅回环，由网关 /admin/ 反代）"),
    ("PASSWORD_ALGO", "password_algo", "str", "pbkdf2sha256",
     "口令散列算法：pbkdf2sha256|scrypt"),
    ("PBKDF2_ITERATIONS", "pbkdf2_iterations", "int", 20000,
     "pbkdf2sha256 的迭代次数（Lua 侧同参数校验）"),
]

# Legacy `state.json.config` keys that moved or were reshaped.
CONF_LEGACY_MAP = {
    "default_limits": None,  # expanded below
    "nginx_include_dir": None,
}


def conf_defaults() -> dict:
    return {key: default for _var, key, _kind, default, _doc in CONF_SCHEMA}


def _conf_quote(kind: str, value) -> str:
    if kind == "range":
        lo, hi = (value or [None, None])[:2]
        return f'"{lo}-{hi}"'
    if kind == "list":
        return '"' + " ".join(str(v) for v in (value or [])) + '"'
    if value is None:
        return '""'
    if kind == "int":
        return f'"{int(value)}"'
    return '"' + str(value).replace('"', '\\"') + '"'


def _conf_coerce(kind: str, value):
    if kind == "bool":
        # Without this branch a string value fell through to str() and bool("False")
        # is True -- which once turned a disabled TLS switch back on.
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if kind == "int":
        return int(value)
    if kind == "range":
        if isinstance(value, (list, tuple)):
            return [int(value[0]), int(value[1])]
        lo, _, hi = str(value).partition("-")
        return [int(lo), int(hi)]
    if kind == "list":
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [v for v in str(value).split() if v]
    if kind == "optstr":
        value = "" if value is None else str(value)
        return value or None
    if kind == "uid":
        if isinstance(value, str) and value.strip().lower() == "auto":
            return "auto"
        return int(value)
    return str(value)


def parse_conf(text: str) -> dict:
    """Parse a shell-syntax conf file into {VAR: raw string}."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        var, sep, val = line.partition("=")
        if not sep:
            continue
        var, val = var.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].replace('\\"', '"')
        out[var] = val
    return out


def render_conf(cfg: dict, with_help: bool = True) -> str:
    lines = [
        "# dsh-mu platform configuration.",
        "#",
        "# Installed as /etc/dsh-mu/dsh-mu.conf.default; the live file is",
        "# /etc/dsh-mu/dsh-mu.conf. Shell syntax: `sh` scripts may source it, and",
        "# `dsh-muctl config <key> <value>` rewrites it. Keep the keys, edit values.",
        "",
    ]
    for var, key, kind, default, doc in CONF_SCHEMA:
        if with_help:
            lines.append(f"# {doc}")
        lines.append(f"{var}={_conf_quote(kind, cfg.get(key, default))}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _conf_from_parsed(parsed: dict, base: dict) -> dict:
    cfg = dict(base)
    for var, key, kind, _default, _doc in CONF_SCHEMA:
        if var not in parsed:
            continue
        try:
            cfg[key] = _conf_coerce(kind, parsed[var])
        except (TypeError, ValueError):
            info(f"warning: ignoring invalid {var}={parsed[var]!r} in {CONF_PATH}")
    return cfg


def _conf_from_legacy(legacy: dict, base: dict) -> dict:
    cfg = dict(base)
    plain = {key for _v, key, _k, _d, _doc in CONF_SCHEMA}
    for key, value in (legacy or {}).items():
        if key in plain:
            cfg[key] = value
        elif key == "default_limits" and isinstance(value, dict):
            for src, dst in (("cpu_max", "default_cpu_max"), ("mem_max", "default_mem_max"),
                             ("mem_high", "default_mem_high"), ("pids_max", "default_pids_max")):
                if value.get(src) is not None:
                    cfg[dst] = str(value[src])
    # normalize the shapes the schema promises
    for var, key, kind, default, _doc in CONF_SCHEMA:
        try:
            cfg[key] = _conf_coerce(kind, cfg.get(key, default))
        except (TypeError, ValueError):
            cfg[key] = default
    return cfg


def load_config(legacy: dict | None = None) -> dict:
    """Effective configuration: schema defaults < legacy state < conf file."""
    cfg = _conf_from_legacy(legacy or {}, conf_defaults())
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH) as fh:
            cfg = _conf_from_parsed(parse_conf(fh.read()), cfg)
    elif legacy:
        # One-time migration: persist what used to live in state.json so the
        # first save_state() (which drops it) cannot lose configuration.
        save_config(cfg)
        info(f"migrated platform configuration from state.json to {CONF_PATH}")
    # Remember the effective configuration so a later save_config() can report a
    # diff no matter which branch produced it.
    global _CONFIG_SNAPSHOT
    _CONFIG_SNAPSHOT = {k: ("<set>" if k == "model_api_key" and v else v)
                        for k, v in cfg.items()}
    return cfg


def prime_state_snapshot(st: dict) -> None:
    global _STATE_SNAPSHOT
    _STATE_SNAPSHOT = {"users": {n: _user_fingerprint(u) for n, u in st["users"].items()},
                       "instances": {n: _instance_fingerprint(i)
                                     for n, i in st["instances"].items()}}


def save_config(cfg: dict):
    """Write dsh-mu.conf and record which settings changed (never their values)."""
    global _CONFIG_SNAPSHOT
    current = {k: ("<set>" if k == "model_api_key" and v else v) for k, v in cfg.items()}
    if _CONFIG_SNAPSHOT is not None and current != _CONFIG_SNAPSHOT:
        changed = sorted(k for k in set(current) | set(_CONFIG_SNAPSHOT)
                         if current.get(k) != _CONFIG_SNAPSHOT.get(k))
        audit_write("config.change", target="dsh-mu.conf",
                    detail={"cmd": sanitize_argv(), "keys": changed})
    _CONFIG_SNAPSHOT = current
    atomic_write(CONF_PATH, render_conf(cfg), mode=0o600)


def write_default_conf(path: str | None = None):
    """Write the untouched defaults (package installs this as `.default`)."""
    target = path or f"{CONF_PATH}.default"
    atomic_write(target, render_conf(conf_defaults()), mode=0o644)
    return target

# --------------------------------------------------------------------- helpers


class CliError(SystemExit):
    """`die()`'s exception, carrying the human message.

    `SystemExit(code)` alone loses the reason: a caller that catches SystemExit
    (the admin backend does, to turn a refused operation into a 400) would only
    see the exit code and answer `"detail": "1"`.
    """

    def __init__(self, message: str, code: int = 1):
        super().__init__(code)
        self.message = message


def die(msg: str, code: int = 1):
    print(f"dsh-muctl: {msg}", file=sys.stderr)
    raise CliError(msg, code)


def info(msg: str):
    """Progress/diagnostic output: stderr, so command data on stdout stays clean."""
    print(msg, file=sys.stderr, flush=True)


def ensure_root():
    if os.geteuid() != 0:
        die("must run as root")


def run(cmd, check=True, capture=False, **kw):
    if isinstance(cmd, str):
        cmd = ["/bin/sh", "-c", cmd]
    res = subprocess.run(cmd, capture_output=capture, text=True, **kw)
    if check and res.returncode != 0:
        out = (res.stderr or res.stdout or "").strip() if capture else ""
        die(f"command failed ({res.returncode}): {' '.join(cmd)} {out}")
    return res


def run_out(cmd) -> str:
    return run(cmd, capture=True).stdout.strip()


def read_text(path: str) -> str | None:
    """Read a text file, or None when it is missing/unreadable."""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def atomic_write(path: str, data: str, mode: int = 0o644, group: str | None = None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        if group:
            try:
                gid = grp.getgrnam(group).gr_gid
                os.chown(tmp, 0, gid)
            except KeyError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


def hash_password(password: str, algo: str, iterations: int) -> str:
    salt = os.urandom(16)
    if algo == "scrypt":
        dk = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        return f"scrypt$n=16384,r=8,p=1${b64e(salt)}${b64e(dk)}"
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen=32)
    return f"pbkdf2sha256${iterations}${b64e(salt)}${b64e(dk)}"


def verify_password_hash(password: str, stored: str) -> bool:
    """Check a password against our own `pbkdf2sha256$`/`scrypt$` hash."""
    try:
        parts = str(stored or "").split("$")
    except Exception:  # noqa: BLE001
        return False
    if len(parts) != 4:
        return False
    scheme, cost, salt_b64, hash_b64 = parts
    try:
        salt = base64.b64decode(salt_b64 + "=" * (-len(salt_b64) % 4))
        want = base64.b64decode(hash_b64 + "=" * (-len(hash_b64) % 4))
    except Exception:  # noqa: BLE001 - a corrupt hash must not raise
        return False
    if scheme == "scrypt":
        params = {}
        for chunk in cost.split(","):
            k, _, v = chunk.partition("=")
            if k:
                params[k] = int(v or 0)
        try:
            dk = hashlib.scrypt(password.encode(), salt=salt,
                                n=params.get("n", 16384), r=params.get("r", 8),
                                p=params.get("p", 1), dklen=len(want))
        except (ValueError, MemoryError):
            return False
    elif scheme == "pbkdf2sha256":
        try:
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                                     int(cost or 0), dklen=len(want))
        except (ValueError, TypeError):
            return False
    else:
        return False
    return hmac.compare_digest(dk, want)


MIN_PASSWORD_LEN = 8


def require_password(password, what: str = "password") -> str:
    """One minimum for every entrance.

    The page checked the length while the CLI (and the administrator's reset API,
    which calls straight into `cmd_user_passwd`) did not, so an account could end
    up with a 1-character - or empty - password. Both entrances resolve here.
    """
    if not password or len(str(password)) < MIN_PASSWORD_LEN:
        die(f"the {what} must be at least {MIN_PASSWORD_LEN} characters")
    if not str(password).strip():
        die(f"the {what} must not be blank")
    return str(password)


def random_password(n: int = 16) -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(alphabet[b % len(alphabet)] for b in os.urandom(n))


def nginx_group() -> str | None:
    for name in ("nginx", "www-data", "http"):
        try:
            grp.getgrnam(name)
            return name
        except KeyError:
            continue
    return None


# ----------------------------------------------------------------------- state


def default_state() -> dict:
    """A fresh state file holds data only; configuration lives in dsh-mu.conf."""
    return {"version": 1, "schema": STATE_SCHEMA, "users": {}, "instances": {}}


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        die(f"state not initialised ({STATE_PATH} missing); run: dsh-muctl init")
    with open(STATE_PATH) as fh:
        raw = json.load(fh)
    # Configuration is external to the state file; the legacy in-file copy (if
    # any) is migrated into dsh-mu.conf on first load and then dropped here, so
    # the two can never drift apart.
    st = dict(raw)
    st["config"] = load_config(raw.get("config") or {})
    st.setdefault("users", {})
    st.setdefault("instances", {})
    st.setdefault("schema", STATE_SCHEMA)
    if "config" in raw:
        save_state(st)
    else:
        # Remember what was read so the next save_state() can report the diff.
        prime_state_snapshot(st)
    return st


_LOCK_STATE: dict = {"depth": 0, "fh": None}


@contextlib.contextmanager
def state_lock(exclusive: bool = True, timeout: float = 30.0):
    """Serialize state mutations across processes (CLI and the admin backend).

    The lock file is separate from `state.json` because the state itself is
    replaced atomically by rename. Hold the lock across a whole
    read-modify-write cycle; `save_state` takes it on its own for single writes.
    Reentrant within one process, so nesting never self-deadlocks.
    """
    if _LOCK_STATE["depth"] > 0:
        _LOCK_STATE["depth"] += 1
        try:
            yield
        finally:
            _LOCK_STATE["depth"] -= 1
        return

    os.makedirs(ETC, exist_ok=True)
    fh = open(STATE_LOCK_PATH, "a+")
    flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh, flag | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                fh.close()
                die(f"timed out waiting for {STATE_LOCK_PATH}")
            time.sleep(0.1)
    _LOCK_STATE.update(depth=1, fh=fh)
    try:
        yield
    finally:
        _LOCK_STATE.update(depth=0, fh=None)
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


# ------------------------------------------------------------------ audit trail
#
# Every state-changing path funnels through save_state()/save_config()/the
# provider writer, so those three record *what changed* instead of relying on
# each command to remember to audit. Records carry entity names and change
# kinds only - never passwords, API keys, cookies or session material - plus a
# sanitised copy of the command line that caused the change.

AUDIT_PATH = f"{LOGS}/audit.log"
_STATE_SNAPSHOT: dict | None = None
_CONFIG_SNAPSHOT: dict | None = None
_PROVIDER_SNAPSHOT: dict | None = None

_SECRET_FLAGS = {"--password", "-p", "--api-key", "--key", "-k", "--admin-token",
                 "--token", "--model-api-key", "--secret", "--cookie"}


def cli_actor() -> str:
    return os.environ.get("DSHMU_ACTOR") or f"cli:{getpass.getuser()}@{socket.gethostname()}"


def sanitize_argv(argv: list[str] | None = None) -> str:
    """The command line with anything that looks like a secret redacted."""
    words = list(argv if argv is not None else sys.argv)
    out: list[str] = []
    redact_next = False
    for word in words:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        if word in _SECRET_FLAGS:
            out.append(word)
            redact_next = True
            continue
        # A long opaque token is probably a key; paths and file names are not.
        if (len(word) >= 24 and re.fullmatch(r"[A-Za-z0-9_\-+=]{24,}", word)
                and not word.startswith("/")):
            out.append("***")          # looks like a key or a hash
            continue
        out.append(word)
    # `instance api-key <name> <key>` takes the key positionally, and a *short*
    # key slips past the length heuristic above - so redact it by position.
    positional_secret = None
    for index, word in enumerate(words):
        if word in ("api-key",) and index + 2 < len(words):
            positional_secret = index + 2
            break
    if positional_secret is not None and positional_secret < len(out):
        out[positional_secret] = "***"
    return " ".join(out[:24]) + (" …" if len(out) > 24 else "")


def audit_write(action: str, target: str = "", detail: dict | None = None,
                actor: str = "", ip: str = "cli") -> dict:
    record = {"ts": int(time.time()), "actor": actor or cli_actor(), "action": action,
              "target": target, "detail": detail or {}, "ip": ip}
    try:
        os.makedirs(LOGS, exist_ok=True)
        new = not os.path.exists(AUDIT_PATH)
        with open(AUDIT_PATH, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        if new:
            os.chmod(AUDIT_PATH, 0o640)
    except OSError:
        return record
    return record


def _user_fingerprint(u: dict) -> tuple:
    # `epoch` is in here on purpose: kicking an account's sessions changes nothing
    # else, and an unlogged "force logout" would be a blind spot in the audit trail.
    return (u.get("role", "user"), bool(u.get("enabled", True)), u.get("instance") or "",
            bool(u.get("must_change")), int(u.get("epoch", 0) or 0), str(u.get("pw", ""))[:16])


def _instance_fingerprint(i: dict) -> tuple:
    return (i.get("user") or "", int(i.get("port", 0) or 0), i.get("state") or "",
            i.get("cpu_max") or "", i.get("mem_max") or "", i.get("pids_max") or "",
            json.dumps(i.get("provider") or {}, sort_keys=True),
            bool(((i.get("credentials") or {}).get("api_key"))
                 or (i.get("credentials") or {}).get("key")))


def _diff_entities(before: dict, after: dict, fingerprint) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after)
                     if fingerprint(before[name]) != fingerprint(after[name]))
    return {k: v for k, v in (("added", added), ("removed", removed), ("changed", changed)) if v}


def audit_state_change(st: dict) -> None:
    """Record what a save_state() call changed since the last observation.

    The loopback backend sets DSHMU_BACKEND and writes its own, richer records
    (with the real admin and client IP), so it skips these generic ones.
    """
    if os.environ.get("DSHMU_BACKEND"):
        return
    global _STATE_SNAPSHOT
    current = {"users": {n: _user_fingerprint(u) for n, u in st["users"].items()},
               "instances": {n: _instance_fingerprint(i) for n, i in st["instances"].items()}}
    if _STATE_SNAPSHOT is not None and current != _STATE_SNAPSHOT:
        detail = {"cmd": sanitize_argv()}
        users = _diff_entities(_STATE_SNAPSHOT["users"], current["users"], lambda x: x)
        instances = _diff_entities(_STATE_SNAPSHOT["instances"], current["instances"], lambda x: x)
        if users:
            detail["users"] = users
        if instances:
            detail["instances"] = instances
        if detail.get("users") or detail.get("instances"):
            audit_write("state.change", target="platform", detail=detail)
    _STATE_SNAPSHOT = current


def save_state(st: dict):
    st["updated_at"] = int(time.time())
    st["schema"] = st.get("schema", STATE_SCHEMA)
    # Configuration lives in dsh-mu.conf, never in the state file.
    data = {k: v for k, v in st.items() if k != "config"}
    audit_state_change(st)
    with state_lock():
        atomic_write(STATE_PATH, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def secret_key() -> bytes:
    with open(SECRET_PATH) as fh:
        return bytes.fromhex(fh.read().strip())


# ------------------------------------------------------------------ rendering


def ensure_tls_dir():
    with contextlib.suppress(OSError):
        os.makedirs(TLS_DIR, exist_ok=True)
        os.chmod(TLS_DIR, 0o750)


def render_gateway(st: dict):
    ensure_tls_dir()
    """Write the two read-only JSON views the OpenResty workers consume."""
    users = {
        name: {
            # The password hash is needed by the gateway worker to verify logins.
            # The worker already holds the session signing key, so this adds no
            # new exposure; the file is 0640 root:nginx.
            "pw": u.get("pw", ""),
            "enabled": bool(u.get("enabled", True)),
            "epoch": int(u.get("epoch", 0)),
            "role": u.get("role", "user"),
            "instance": u.get("instance") or "",
            "must_change": bool(u.get("must_change")),
        }
        for name, u in st["users"].items()
    }
    insts = {}
    for name, inst in st["instances"].items():
        provider = inst.get("provider") or {}
        insts[name] = {
            "user": inst.get("user") or "",
            "host": f"127.0.0.1:{int(inst['port'])}",
            "port": int(inst["port"]),
            "cookie": inst.get("upstream", {}).get("cookie", ""),
            "state": inst.get("state", "stopped"),
            "provider": {"preset": provider.get("preset", "deepseek"),
                         "model": provider.get("model", "")},
        }
    group = nginx_group() if os.geteuid() == 0 else None
    os.makedirs(GW_DIR, exist_ok=True)
    atomic_write(
        f"{GW_DIR}/users.json",
        json.dumps({"version": 1, "users": users}, sort_keys=True) + "\n",
        mode=0o640, group=group,
    )
    atomic_write(
        f"{GW_DIR}/upstreams.json",
        json.dumps({"version": 1, "instances": insts}, sort_keys=True) + "\n",
        mode=0o640, group=group,
    )
    # Gateway-side tunables (session lifetime, public host) without any secrets.
    public_cfg = {
        "gateway_host": st["config"].get("gateway_host", "dsh-mu"),
        "gateway_listen": st["config"].get("gateway_listen", 80),
        "session_days": st["config"].get("session_days", 7),
        "tls_enabled": bool(st["config"].get("tls_enabled", False)),
        "tls_redirect": bool(st["config"].get("tls_redirect", False)),
        "session_idle_minutes": st["config"].get("session_idle_minutes", 0),
        # Needed by the gateway worker to recognise host administrators.
        "admin_system_users": st["config"].get("admin_system_users", "root"),
    }
    atomic_write(f"{GW_DIR}/config.json", json.dumps(public_cfg, sort_keys=True) + "\n",
                 mode=0o640, group=group)
    with contextlib.suppress(OSError):
        write_relay_document(st)
    if str(st["config"].get("gateway_mode") or "jail") == "jail":
        # The gateway jail reads its own copy of these files; refresh it on every
        # state change, otherwise the jail keeps serving a stale cookie/state.
        with contextlib.suppress(OSError):
            sync_gateway_etc()
        # Its runner config (bind list, port) and nginx fragments live on the host
        # too, and a `config` change (TLS on/off, port) must reach them.
        with contextlib.suppress(OSError):
            write_gateway_jail_conf(st["config"])
        with contextlib.suppress(OSError):
            render_gateway_jail_conf_files(st["config"])
        # nginx chowns its temp dirs at startup; with only CAP_NET_BIND_SERVICE the
        # call fails if the dirs are owned by the worker user, so recreate them.
        with contextlib.suppress(OSError):
            tmpdir = f"{gateway_paths()['rootfs']}/var/tmp/nginx"
            if os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
            os.makedirs(tmpdir, exist_ok=True)
    try:
        if group:
            os.chown(GW_DIR, 0, grp.getgrnam(group).gr_gid)
            os.chmod(GW_DIR, 0o750)
    except Exception:
        pass


# --------------------------------------------------------------------------- mounts
# Host directories mapped into a jail. The launcher already understands
# EXTRA_MOUNTS="host:jail[:ro],..."; this is the validated editing surface.
MOUNT_FORBIDDEN_HOST = ("/proc", "/sys", "/dev", "/boot", "/etc", "/root", "/bin", "/sbin",
                        "/lib", "/lib64", "/usr", "/var/lib/dsh-mu")
MOUNT_FORBIDDEN_JAIL = ("/", "/proc", "/sys", "/dev", "/boot")


def parse_mount_specs(inst: dict) -> list[dict]:
    """The instance's mount table as structured rows."""
    rows = []
    for raw in str(inst.get("extra_mounts") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        host, _, rest = raw.partition(":")
        jail, _, mode = rest.partition(":")
        rows.append({"host": host, "jail": jail or host,
                     "mode": mode if mode in ("ro", "rw") else "ro", "spec": raw})
    return rows


def validate_mount(host: str, jail: str, mode: str, instances: dict) -> list[str]:
    """Everything a bind mount must satisfy before it goes near a jail."""
    problems = []
    host = os.path.abspath(host or "")
    jail = "/" + (jail or "").strip("/")
    if not host.startswith("/") or host == "/":
        problems.append("host path must be an absolute path other than /")
    if not os.path.exists(host):
        problems.append(f"host path does not exist: {host}")
    elif not os.path.isdir(host):
        problems.append(f"host path is not a directory: {host}")
    for bad in MOUNT_FORBIDDEN_HOST:
        if host == bad or host.startswith(bad + "/"):
            problems.append(f"refusing to expose platform-internal path: {bad}")
    for other, inst in (instances or {}).items():
        rootfs = str(inst.get("rootfs") or "")
        if rootfs and (host == rootfs or host.startswith(rootfs.rstrip("/") + "/")):
            problems.append(f"host path is inside instance {other}'s jail rootfs")
    if jail in MOUNT_FORBIDDEN_JAIL:
        problems.append("jail path cannot be /, /proc, /sys, /dev or /boot")
    if jail.startswith("/home/dsh/.dsh") or jail.startswith("/usr/local/lib"):
        problems.append(f"jail path would shadow platform files: {jail}")
    if mode not in ("ro", "rw"):
        problems.append("mode must be ro or rw")
    return problems


def set_instance_mounts(name: str, specs: list[tuple[str, str, str]]) -> list[dict]:
    """Replace an instance's mount table (validated, normalised, deduplicated)."""
    st = load_state()
    inst = st["instances"].get(name) or die(f"no such instance: {name}")
    seen: dict[str, dict] = {}
    for host, jail, mode in specs:
        host = os.path.abspath(host)
        jail = "/" + (jail or host).strip("/")
        mode = "rw" if mode == "rw" else "ro"
        for problem in validate_mount(host, jail, mode, st["instances"]):
            die(f"{host} -> {jail}: {problem}")
        seen[jail] = {"host": host, "jail": jail, "mode": mode,
                      "spec": f"{host}:{jail}:{mode}"}
    rows = list(seen.values())
    inst["extra_mounts"] = ",".join(r["spec"] for r in rows)
    save_state(st)
    return rows


def render_instance_conf(st: dict, name: str):
    inst = st["instances"][name]
    base = f"{INSTANCES_DIR}/{name}"
    lines = [
        "# generated by dsh-muctl - do not edit",
        f"NAME={name}",
        f"ROOTFS={base}/rootfs",
        f"INSTANCE_HOME={base}/home",
        f"INSTANCE_WORK={base}/workspace",
        f"PORT={int(inst['port'])}",
        f"INST_UID={int(inst.get('uid', 0))}",
        f"INST_GID={int(inst.get('gid', inst.get('uid', 0)))}",
        f"CPU_MAX=\"{inst.get('cpu_max', '')}\"",
        f"MEM_MAX=\"{inst.get('mem_max', '')}\"",
        f"MEM_HIGH=\"{inst.get('mem_high', '')}\"",
        f"PIDS_MAX=\"{inst.get('pids_max', '')}\"",
        f"CGROUP_PATH=/sys/fs/cgroup/dsh-mu/{name}",
        f"DSH_HOST=127.0.0.1",
        f"DSH_HOME_IN_JAIL=/home/dsh/.dsh",
        f"DSH_WORK_IN_JAIL=/home/dsh/workspace",
        f"DSH_RUNTIME_DIR={dsh_runtime_dir(st['config'])}",
        f"DSH_EXTRA_ARGS=\"{st['config'].get('dsh_extra_args', '')}\"",
        # Always load the patch: it also carries the sandbox-policy entry.
        "DSH_LAUNCH_ARGS=\"--patch /home/dsh/.dsh-mu-provider.yml\"",
        f"KEEP_CAPS=\"{st['config'].get('instance_keep_caps') or ''}\"",
        f"DSH_PERMISSION_MODE=\"{st['config'].get('instance_permission_mode', 'danger-full-access')}\"",
        f"EXTRA_MOUNTS=\"{inst.get('extra_mounts', '')}\"",
        "",
    ]
    atomic_write(f"{INST_CONF_DIR}/{name}.conf", "\n".join(lines), mode=0o644)


INIT_TEMPLATE = """#!/sbin/openrc-run
# generated by dsh-muctl - one jailed DeepSeek Harness instance

name="dsh-mu instance @NAME@"
description="DeepSeek Harness instance '@NAME@' (mount-ns + chroot + cgroup jail)"

command="@JAIL@"
command_args="@NAME@"
command_user="root:root"
pidfile="/run/dsh-mu-@NAME@.pid"
output_log="/var/log/dsh-mu/@NAME@.log"
error_log="/var/log/dsh-mu/@NAME@.err.log"

supervisor="supervise-daemon"
respawn_delay=5
respawn_max=0
respawn_period=120

depend() {
	need net
	need dsh-mu-cgroup
	after openresty
}

start_pre() {
	checkpath --directory --mode 0755 /var/log/dsh-mu
	checkpath --directory --mode 0755 @BASE@
}

stop_post() {
	# supervise-daemon only signals its direct child; the harness runs as a
	# grandchild inside the instance cgroup, so reap the whole group.
	if [ -w @CGROUP@/cgroup.kill ]; then
		echo 1 > @CGROUP@/cgroup.kill 2>/dev/null
	fi
	return 0
}
"""


def render_service(st: dict, name: str):
    base = f"{INSTANCES_DIR}/{name}"
    text = (INIT_TEMPLATE.replace("@NAME@", name)
                       .replace("@JAIL@", JAIL)
                       .replace("@BASE@", base)
                       .replace("@CGROUP@", f"/sys/fs/cgroup/dsh-mu/{name}"))
    atomic_write(f"/etc/init.d/dsh-mu-{name}", text, mode=0o755)


CGROUP_INIT_TEMPLATE = """#!/sbin/openrc-run
# generated by dsh-muctl - delegate cgroup v2 controllers to the instance tree

name="dsh-mu cgroup delegation"
description="Make /sys/fs/cgroup/dsh-mu usable for per-instance resource limits"

# One-shot service: OpenRC must not treat the immediate exit as a daemon crash.
start() {
	ebegin "Delegating cgroup v2 controllers to /sys/fs/cgroup/dsh-mu"
	@CGROUP_INIT@
	eend $?
}

stop() {
	ebegin "cgroup delegation is left in place"
	eend 0
}

status() {
	if [ -r /sys/fs/cgroup/dsh-mu/cgroup.subtree_control ]; then
		einfo "delegated controllers: $(cat /sys/fs/cgroup/dsh-mu/cgroup.subtree_control)"
		return 0
	fi
	einfo "no delegation present"
	return 3
}
"""


def ensure_cgroup2_mounted() -> tuple[bool, str]:
    """Mount the unified cgroup hierarchy if the host has not done it yet.

    Alpine does not mount cgroup2 unless the `cgroup` service (or a manual mount)
    has run; a fresh VM therefore fails every limit-related check. `install` and
    `doctor --fix` call this, and it is a no-op when /sys/fs/cgroup is already
    the unified hierarchy.
    """
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return True, "already mounted"
    try:
        with open("/proc/filesystems") as fh:
            if "cgroup2" not in fh.read():
                return False, "this kernel has no cgroup2 filesystem"
    except OSError as exc:
        return False, f"cannot read /proc/filesystems: {exc}"
    os.makedirs("/sys/fs/cgroup", exist_ok=True)
    res = run(["mount", "-t", "cgroup2", "cgroup2", "/sys/fs/cgroup"], check=False, capture=True)
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        # make it survive a reboot through OpenRC's cgroup service when present
        if os.path.exists("/etc/init.d/cgroup"):
            with contextlib.suppress(OSError):
                rc = f"{ETC}/rc.conf"
                text = read_text(rc) or ""
                text = re.sub(r"^#?\s*rc_cgroup_mode=.*$", 'rc_cgroup_mode="unified"',
                              text, count=1, flags=re.M)
                atomic_write(rc, text, mode=0o644)
            run(["/sbin/rc-update", "add", "cgroup", "boot"], check=False)
        return True, "mounted now"
    detail = (res.stderr or res.stdout or "").strip()[:200]
    return False, detail or "mount -t cgroup2 failed"


def render_cgroup_service(register: bool = True):
    mounted, why = ensure_cgroup2_mounted()
    if not mounted:
        info(f"cgroup v2: {why} (CPU/memory limits will not be enforceable)")
    atomic_write("/etc/init.d/dsh-mu-cgroup",
                 CGROUP_INIT_TEMPLATE.replace("@CGROUP_INIT@", CGROUP_INIT), mode=0o755)
    if register:
        run(["/sbin/rc-update", "add", "dsh-mu-cgroup", "boot"], check=False)


NGINX_USER = "nginx"
NGINX_TEMP_DIRS = ("/var/tmp/nginx/client_body", "/var/tmp/nginx/proxy",
                   "/var/tmp/nginx/fastcgi", "/var/tmp/nginx/uwsgi", "/var/tmp/nginx/scgi")


def nginx_runtime_gaps() -> list[str]:
    """What the openresty package's install scripts would have provided.

    The workers run as the compiled-in `nginx` user and need the temp directories;
    a package whose post-install scripts could not run (restricted container,
    nested chroot) leaves both missing and nginx then refuses to start.
    """
    gaps = []
    if not _group_exists(NGINX_USER):
        gaps.append("nginx group")
    if not _user_exists(NGINX_USER):
        gaps.append("nginx user")
    for path in NGINX_TEMP_DIRS:
        if not os.path.isdir(path):
            gaps.append(path)
            break
        # A directory nginx cannot write into is just as broken as a missing one
        # - and unlike a missing one, nginx will not silently recreate it.
        if _user_exists(NGINX_USER):
            try:
                entry = pwd.getpwnam(NGINX_USER)
                stat = os.stat(path)
                if stat.st_uid != entry.pw_uid or stat.st_gid != entry.pw_gid:
                    gaps.append(f"{path} (owner)")
                    break
                if not stat.st_mode & 0o300:
                    gaps.append(f"{path} (not writable)")
                    break
            except (KeyError, OSError):
                pass
    return gaps


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def ensure_nginx_runtime(fix: bool = False) -> list[str]:
    """Report (and optionally repair) the nginx user/group and temp directories."""
    gaps = nginx_runtime_gaps()
    if not gaps or not fix:
        return gaps
    if "nginx group" in gaps:
        run(["addgroup", "-S", NGINX_USER], check=False)
    if "nginx user" in gaps:
        run(["adduser", "-S", "-D", "-H", "-s", "/sbin/nologin", "-G", NGINX_USER, NGINX_USER],
            check=False)
    for path in NGINX_TEMP_DIRS:
        os.makedirs(path, exist_ok=True)
        if _user_exists(NGINX_USER):
            try:
                entry = pwd.getpwnam(NGINX_USER)
                os.chown(path, entry.pw_uid, entry.pw_gid)
                os.chmod(path, 0o700)
            except (KeyError, OSError):
                pass
    return gaps


def ensure_cgroup_service():
    """(Re)write the delegation service and keep it registered at boot.

    Registration is unconditional because it is idempotent: a package upgrade
    (or an `apk del` that removed the runlevel link) must not leave instances
    without cgroup delegation after a reboot.
    """
    render_cgroup_service(register=True)


def reconcile_instances(st: dict) -> list[str]:
    """Start the services of instances that the platform records as running.

    `apk del dsh-mu` (or a crash) leaves instance services stopped while
    state.json still says running; `dsh-muctl install` and `doctor --fix` call
    this so an upgrade/reinstall converges back to the recorded intent.
    """
    started = []
    for name, inst in sorted(st["instances"].items()):
        if inst.get("state") != "running":
            continue
        if port_in_use(int(inst.get("port", 0))):
            continue
        script = f"/etc/init.d/dsh-mu-{name}"
        if not os.path.exists(script):
            continue
        info(f"starting instance {name} (recorded as running but its port is closed)")
        run(["/sbin/rc-service", f"dsh-mu-{name}", "start"], check=False)
        started.append(name)
    return started


def ensure_instance_autostart(st: dict):
    """Keep every instance's service in the default runlevel (idempotent)."""
    for name in st["instances"]:
        script = f"/etc/init.d/dsh-mu-{name}"
        link = f"/etc/runlevels/default/dsh-mu-{name}"
        if os.path.exists(script) and not os.path.exists(link):
            run(["/sbin/rc-update", "add", f"dsh-mu-{name}", "default"], check=False)


# --------------------------------------------------------------- admin backend

ADMIN_BIN = "/usr/local/sbin/dsh-mud"
ADMIN_SERVICE = "/etc/init.d/dsh-mud"
ADMIN_TOKEN_PATH = f"{ETC}/admin.token"

ADMIN_SERVICE_TEMPLATE = """#!/sbin/openrc-run
# generated by dsh-muctl - loopback JSON admin API (the gateway proxies /admin/)

name="dsh-mu admin backend"
description="Loopback admin API for the dsh-mu platform (proxied by the gateway)"

command="@ADMIN_BIN@"
command_args="--listen @ADMIN_LISTEN@"
command_user="root:root"
pidfile="/run/dsh-mud.pid"
output_log="/var/log/dsh-mu/admin.log"
error_log="/var/log/dsh-mu/admin.err.log"

supervisor="supervise-daemon"
respawn_delay=5
respawn_max=0
respawn_period=120

depend() {
	need net
	after openresty
}

start_pre() {
	checkpath --directory --mode 0755 /var/log/dsh-mu
}
"""


def ensure_admin_token() -> str:
    """Shared secret the gateway presents to the admin backend (0640 root:nginx)."""
    if not os.path.exists(ADMIN_TOKEN_PATH):
        atomic_write(ADMIN_TOKEN_PATH, os.urandom(24).hex() + "\n",
                     mode=0o640, group=nginx_group())
        info(f"generated {ADMIN_TOKEN_PATH}")
    return ADMIN_TOKEN_PATH


def render_admin_service(cfg: dict, register: bool = True):
    listen = str(cfg.get("admin_listen") or "127.0.0.1:8787")
    atomic_write(ADMIN_SERVICE,
                 ADMIN_SERVICE_TEMPLATE.replace("@ADMIN_BIN@", ADMIN_BIN)
                                       .replace("@ADMIN_LISTEN@", listen),
                 mode=0o755)
    if register:
        run(["/sbin/rc-update", "add", "dsh-mud", "default"], check=False)


def install_admin(cfg: dict, restart: bool = True) -> bool:
    """Install the token, the service and (optionally) start it."""
    ensure_admin_token()
    render_admin_service(cfg)
    if not os.path.exists(ADMIN_BIN):
        info(f"warning: {ADMIN_BIN} missing; the admin API cannot start")
        return False
    if restart:
        res = run(["/sbin/rc-service", "dsh-mud", "status"], check=False, capture=True)
        running = "started" in ((res.stdout or "") + (res.stderr or ""))
        run(["/sbin/rc-service", "dsh-mud", "restart" if running else "start"], check=False)
    return True


def sync_all(st: dict):
    os.makedirs(INST_CONF_DIR, exist_ok=True)
    # /etc/dsh-mu/dsh-mu.conf is the single source of truth: a `config` write must
    # be visible to every render even when the caller still holds an older state
    # snapshot (that mismatch once left a stale TLS block in the nginx fragment).
    st["config"] = load_config(st["config"])
    # Keep a platform record for every configured system administrator so the
    # gateway can validate their sessions (and demote stale platform admins).
    ensure_system_admins(st)
    ensure_cgroup_service()
    for name in st["instances"]:
        render_instance_conf(st, name)
        render_service(st, name)
        # Model plans (provider + key per user) are rendered as launch patches.
        with contextlib.suppress(OSError):
            sync_instance_plans(st, name)
    ensure_instance_autostart(st)
    render_gateway(st)
    if str(st["config"].get("gateway_mode") or "host") == "jail":
        with contextlib.suppress(OSError):
            sync_gateway_etc()


# ------------------------------------------------------------- template / rootfs


def host_alpine_version() -> str | None:
    """Release branch of this host as an apk repository path component (v3.24)."""
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("VERSION_ID="):
                    raw = line.split("=", 1)[1].strip().strip('"')
                    parts = raw.split(".")
                    if len(parts) >= 2:
                        return f"v{parts[0]}.{parts[1]}"
    except OSError:
        pass
    return None


def resolve_alpine_version(cfg: dict) -> str:
    """`ALPINE_VERSION=auto` follows this host; anything else is used verbatim."""
    configured = str(cfg.get("alpine_version") or "auto").strip()
    if configured.lower() != "auto":
        return configured
    detected = host_alpine_version()
    if not detected:
        die("could not detect the Alpine release from /etc/os-release; "
            "set ALPINE_VERSION in /etc/dsh-mu/dsh-mu.conf explicitly")
    return detected


def community_repo_enabled() -> bool:
    """Whether this host's own apk repositories include community."""
    try:
        with open("/etc/apk/repositories") as fh:
            return "community" in fh.read()
    except OSError:
        return False


def npm_env(cfg: dict) -> dict:
    """Environment additions for npm/pnpm calls (mirror support)."""
    registry = cfg.get("npm_registry")
    if not registry:
        return {}
    return {"npm_config_registry": registry, "NPM_CONFIG_REGISTRY": registry}


def _interactive(default: bool | None = None) -> bool:
    """Prompt only when a human is really there (a chroot or a pipe is not).

    Checking stdin alone is not enough: `chroot ... > log` keeps stdin on the
    terminal, so an install in a script would eat the next command as its answer.
    """
    if os.environ.get("DSHMU_NONINTERACTIVE"):
        return False
    if default is not None:
        return default
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str, default: str = "") -> str:
    """input() that survives EOF/^C by falling back to the default."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def host_arch() -> str:
    """Proxmox image suffix for this machine's architecture."""
    machine = os.uname().machine
    return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf"}.get(machine, machine)


def _http_get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"dsh-mu/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read(2 * 1024 * 1024).decode("utf-8", "replace")


def proxmox_images(cfg: dict, arch: str | None = None) -> list[dict]:
    """Alpine images listed by the Proxmox image index, newest first.

    The index is the *only* thing that has to be listable, which is why it stays
    on download.proxmox.com: plain directory listings on mirrors are often
    unavailable (the tuna mirror, for instance, cannot be listed).
    """
    arch = arch or host_arch()
    index = str(cfg.get("alpine_image_index") or "").rstrip("/")
    if not index:
        die("ALPINE_IMAGE_INDEX is empty")
    try:
        html = _http_get(index + "/")
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        die(f"cannot list {index}: {type(exc).__name__}: {exc} "
            f"(set ALPINE_TEMPLATE_IMAGE to skip the listing)")
    found = []
    for name in re.findall(r'href="(alpine-[^"]+?_' + arch + r'\.tar\.xz)"', html):
        ver = re.match(r"alpine-([0-9.]+)-default_(\d{8})_", name)
        found.append({"name": name,
                      "version": ver.group(1) if ver else "",
                      "date": ver.group(2) if ver else "",
                      "arch": arch})
    found.sort(key=lambda x: (x["date"], x["version"]), reverse=True)
    return found


def pick_template_image(cfg: dict, interactive: bool | None = None) -> dict:
    """`ALPINE_TEMPLATE_IMAGE` if set, otherwise a numbered list (default: newest)."""
    wanted = str(cfg.get("alpine_template_image") or "").strip()
    if wanted:
        ver = re.match(r"alpine-([0-9.]+)-default_", wanted)
        return {"name": wanted, "version": ver.group(1) if ver else "", "date": "", "arch": host_arch()}
    images = proxmox_images(cfg)
    if not images:
        die(f"no alpine images for {host_arch()} at {cfg.get('alpine_image_index')}")
    if not _interactive(interactive):
        info(f"using the newest image: {images[0]['name']}")
        return images[0]
    shown = images[:10]
    print(f"\n可用 Alpine 模板（{host_arch()}，来自 {cfg['alpine_image_index']}）：")
    for i, img in enumerate(shown, 1):
        print(f"  {i}) {img['name']:<46} {img['version']}")
    raw = _ask(f"选择模板 [1-{len(shown)}，回车={shown[0]['name']}]: ").strip()
    if not raw:
        return shown[0]
    try:
        idx = int(raw)
    except ValueError:
        die(f"invalid choice: {raw!r}")
    if not 1 <= idx <= len(shown):
        die(f"choice out of range: {idx}")
    return shown[idx - 1]


_REGION: dict[str, bool | None] = {}


def detect_cn_region(force: bool = False) -> bool | None:
    """True/False for a CN public address, None when the lookup is unavailable.

    Cached per process: the answer picks the apk mirror, the image mirror *and* the
    npm registry, and we must not ask three times (or twice) during one install.
    """
    if "cn" in _REGION and not force:
        return _REGION["cn"]
    result: bool | None = None
    for url, pick in (("https://ipinfo.io/country", lambda s: s.strip().upper() == "CN"),
                      ("https://ifconfig.co/country-iso", lambda s: s.strip().upper() == "CN")):
        try:
            result = bool(pick(_http_get(url, timeout=4)))
            break
        except Exception:  # noqa: BLE001 - offline / blocked is fine
            continue
    _REGION["cn"] = result
    return result


_MIRROR_DECISION: dict = {}


def choose_mirrors(cfg: dict, interactive: bool | None = None,
                   force: bool = False) -> dict:
    """Pick the repo mirror and the image download base for this network.

    Only *defaults* are overridden: an explicitly configured ALPINE_MIRROR or
    ALPINE_IMAGE_BASE always wins, and the decision is printed with its reason.
    """
    defaults = {"alpine_mirror": "https://mirrors.tuna.tsinghua.edu.cn/alpine",
                "alpine_image_base": "http://download.proxmox.com/images/system"}
    if _MIRROR_DECISION and not force:
        return dict(_MIRROR_DECISION)
    mirror = str(cfg.get("alpine_mirror") or defaults["alpine_mirror"]).rstrip("/")
    base = str(cfg.get("alpine_image_base") or defaults["alpine_image_base"]).rstrip("/")
    cn = detect_cn_region()
    if cn is None:
        reason = "region detection unavailable; keeping the configured mirrors"
    elif cn:
        mirror = "https://mirrors.tuna.tsinghua.edu.cn/alpine"
        base = "https://mirrors.tuna.tsinghua.edu.cn/proxmox/images/system"
        reason = "public address looks like CN; using the Tsinghua mirrors"
    else:
        mirror = "https://dl-cdn.alpinelinux.org/alpine"
        base = "http://download.proxmox.com/images/system"
        reason = "public address looks non-CN; using the upstream mirrors"
    info(f"mirrors: {reason}")
    info(f"  apk repo:   {mirror}")
    info(f"  image base: {base}")
    if _interactive(interactive):
        answer = _ask("用这两个地址继续？[Y/n]: ").strip().lower()
        if answer in ("n", "no"):
            mirror = defaults["alpine_mirror"] if "tuna" in defaults["alpine_mirror"] else mirror
            base = defaults["alpine_image_base"]
            info("kept the defaults from dsh-mu.conf")
    decision = {"alpine_mirror": mirror, "alpine_image_base": base, "reason": reason}
    _MIRROR_DECISION.update(decision)
    return decision


NPM_MIRROR = "https://registry.npmmirror.com"
NPM_OFFICIAL = "https://registry.npmjs.org"


def pick_npm_registry() -> tuple[str | None, str]:
    """Choose an npm registry: the region first, then a latency race.

    The geographic lookup can be blocked (it is inside some LXC/CN networks), and
    then a cold `npm install -g @deepseek-ai/dsh` crawls against the official
    registry. Probing both `/-/ping` endpoints takes a couple of seconds.
    """
    cn = detect_cn_region()
    if cn is True:
        return NPM_MIRROR, "public address looks like CN"
    if cn is False:
        return None, "public address looks non-CN"
    best = None
    for url in (NPM_MIRROR, NPM_OFFICIAL):
        started = time.time()
        ok, _why = _http_ok(f"{url}/-/ping", timeout=5)
        if not ok:
            continue
        elapsed = time.time() - started
        if best is None or elapsed < best[1]:
            best = (url, elapsed)
    if best and best[0] == NPM_MIRROR:
        return NPM_MIRROR, f"region unknown; {NPM_MIRROR} answered fastest ({best[1] * 1000:.0f} ms)"
    if best:
        return None, f"region unknown; {NPM_OFFICIAL} answered fastest ({best[1] * 1000:.0f} ms)"
    return None, "region unknown; neither registry answered"


def _download_with_python(url: str, dest: str, timeout: float = 300.0) -> tuple[bool, str]:
    """Download with urllib (python3 is a hard dependency; curl is not).

    busybox wget answers 403 on some mirrors where curl succeeds, and a fresh
    Alpine has no curl at all, so this is the primary transport.
    """
    req = urllib.request.Request(url, headers={"User-Agent": f"dsh-mu/{VERSION}"})
    deadline = time.time() + timeout
    total = 0
    done = 0
    started = time.time()
    last_report = 0.0
    try:
        with urllib.request.urlopen(req, timeout=30) as res, open(dest, "wb") as fh:
            with contextlib.suppress(TypeError, ValueError):
                total = int(res.headers.get("Content-Length") or 0)
            while True:
                if time.time() > deadline:
                    return False, "timed out"
                chunk = res.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_report >= 2.0 or (total and done >= total):
                    last_report = now
                    rate = done / max(0.001, now - started) / 1048576
                    if total:
                        info(f"  {done / 1048576:.1f} MB / {total / 1048576:.1f} MB "
                             f"({done * 100 // total}%, {rate:.1f} MB/s)")
                    else:
                        info(f"  {done / 1048576:.1f} MB ({rate:.1f} MB/s)")
    except Exception as exc:  # noqa: BLE001 - report and try the next mirror
        with contextlib.suppress(OSError):
            os.unlink(dest)
        return False, f"{type(exc).__name__}: {exc}"
    if total and done != total:
        return False, f"short read ({done} of {total} bytes)"
    info(f"  downloaded {done / 1048576:.1f} MB in {time.time() - started:.1f}s")
    return True, ""


def download_image(cfg: dict, image: str) -> str:
    """Download a template image once and keep it under VAR/images."""
    cache = f"{VAR}/images"
    os.makedirs(cache, exist_ok=True)
    dest = f"{cache}/{image}"
    if os.path.exists(dest) and os.path.getsize(dest) > 1024 * 1024:
        info(f"using the cached image: {dest} ({os.path.getsize(dest) // 1024} KB)")
        return dest
    official = "http://download.proxmox.com/images/system"
    # Mirrors first: the official host can be very slow from CN networks, and a
    # configured base that IS the official host must not be tried before them.
    candidates = []
    for candidate in ("https://mirrors.tuna.tsinghua.edu.cn/proxmox/images/system",
                      "https://mirrors.ustc.edu.cn/proxmox/images/system",
                      str(cfg.get("alpine_image_base") or "").rstrip("/"),
                      official):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    # No pre-probe: a Range/HEAD probe gets 403 from some mirrors while a plain GET
    # works (busybox wget also 403s where curl succeeds), so probing produced false
    # negatives. Try each candidate for real, with a bounded time, and say why a
    # candidate was dropped.
    tmp = dest + ".part"
    last = ""
    for base in candidates:
        url = f"{base}/{image}"
        info(f"downloading {url}")
        ok = False
        if shutil.which("python3"):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            good, why = _download_with_python(url, tmp, timeout=180.0)
            if good and os.path.exists(tmp) and os.path.getsize(tmp) > 1024 * 1024:
                ok = True
            else:
                last = f"python: {why}"
        commands = [["curl", "-fL", "--retry", "1", "--connect-timeout", "10",
                     "--max-time", "300", "-o", tmp, url],
                    ["wget", "-q", "-T", "60", "-O", tmp, url]]
        for cmd in commands:
            if ok or not shutil.which(cmd[0]):
                continue
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            res = run(cmd, check=False, capture=True)
            if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1024 * 1024:
                ok = True
                break
            last = (res.stderr or res.stdout or "").strip()[:200]
        if ok:
            last = ""
        if ok:
            os.replace(tmp, dest)
            info(f"downloaded {dest} ({os.path.getsize(dest) // 1024 // 1024} MB)")
            return dest
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    die(f"could not download {image} (last error: {last})")


def build_template(st: dict, force: bool = False, interactive: bool | None = None,
                   image: str | None = None, dest: str | None = None,
                   packages: list[str] | None = None):
    """Build the jail rootfs from a Proxmox system-image tarball.

    Flow (rjail-style): list/pick an image -> download -> extract -> point the
    rootfs at ALPINE_MIRROR -> `chroot <rootfs> apk add <packages>`. Installing
    inside a real chroot means apk's post-install scripts actually run, so the
    old workaround for missing busybox applets is gone with the old build method.
    """
    cfg = st["config"]
    dest = dest or TEMPLATE
    _what = ("网关模板（平台 jail 用，含 openresty）"
             if os.path.abspath(dest) == os.path.abspath(TEMPLATE_GATEWAY)
             else "运行环境模板（用户实例用）")
    if os.path.isdir(dest) and not force:
        info(f"{_what} already present: {dest} (use --force to rebuild)")
        return

    chosen = choose_mirrors(cfg, interactive=interactive)
    cfg = {**cfg, "alpine_mirror": chosen["alpine_mirror"],
           "alpine_image_base": chosen["alpine_image_base"]}
    pick = pick_template_image(cfg, interactive=interactive) if not image else {
        "name": image,
        "version": (re.match(r"alpine-([0-9.]+)-default_", image) or [None, ""])[1]}
    if not pick.get("version"):
        pick["version"] = ""

    if force and os.path.exists(dest):
        shutil.rmtree(dest)
    rootfs = dest
    os.makedirs(rootfs, exist_ok=True)

    archive = download_image(cfg, pick["name"])
    info(f"extracting {os.path.basename(archive)} -> {rootfs}")
    # Extract with Python's tarfile: it reads .tar.xz through stdlib lzma, so the
    # target does not need the `xz` binary (a minimal Alpine host has none, and
    # `tar -xJf` then fails with "xz: Cannot exec").
    try:
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(rootfs, filter="fully_trusted")
    except (tarfile.TarError, OSError, EOFError) as exc:
        die(f"could not extract {archive}: {type(exc).__name__}: {exc}")
    for required in ("bin/sh", "sbin/apk", "etc/apk/keys"):
        if not os.path.exists(f"{rootfs}/{required}"):
            die(f"the extracted image has no {required}; is it an Alpine rootfs?")

    # The image ships repositories pointing at dl-cdn; use the chosen mirror and
    # give the rootfs the host's keys and resolver for the install below.
    version = f"v{pick['version']}" if pick["version"] else resolve_alpine_version(cfg)
    mirror = str(cfg["alpine_mirror"]).rstrip("/")
    atomic_write(f"{rootfs}/etc/apk/repositories",
                 f"{mirror}/{version}/main\n{mirror}/{version}/community\n")
    for key in glob.glob("/etc/apk/keys/*"):
        shutil.copy2(key, f"{rootfs}/etc/apk/keys/{os.path.basename(key)}")
    with contextlib.suppress(OSError):
        shutil.copy2("/etc/resolv.conf", f"{rootfs}/etc/resolv.conf")

    pkgs = " ".join(packages or cfg["template_packages"])
    info(f"installing into the template inside a chroot: {pkgs}")
    res = run(f"chroot {rootfs} /sbin/apk add --no-cache --keys-dir /etc/apk/keys {pkgs}",
              check=False, capture=True)
    required = ("bin/sh", "usr/bin/node", "etc/apk/keys")
    missing = [p for p in required if not os.path.exists(f"{rootfs}/{p}")]
    if not any(os.path.exists(f"{rootfs}{p}") for p in ("/usr/bin/setpriv", "/bin/setpriv", "/usr/sbin/setpriv")):
        missing.append("setpriv")
    if missing:
        die(f"jail template build failed: missing {', '.join(missing)} "
            f"(apk exit {res.returncode}; {(res.stderr or '').strip()[:200]})")
    if res.returncode != 0 and not missing:
        info(f"warning: apk exited {res.returncode} but the rootfs is complete")

    # DSH's own defaults expect a UTF-8 locale and a usable PATH.
    atomic_write(f"{rootfs}/etc/profile.d/dsh-mu.sh", 'export LANG=C.UTF-8\nexport PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n')
    os.makedirs(f"{rootfs}/home/dsh/workspace", exist_ok=True)
    os.makedirs(f"{rootfs}/usr/local/bin", exist_ok=True)
    # The global DSH package is bind-mounted in at runtime; inside the jail it is
    # reached through the standard CLI path.
    with contextlib.suppress(OSError):
        os.makedirs(f"{rootfs}/usr/local/bin", exist_ok=True)
        bash_link = f"{rootfs}/usr/local/bin/bash"
        if not os.path.lexists(bash_link):
            os.symlink("/bin/bash", bash_link)
    link = f"{rootfs}/usr/local/bin/dsh"
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink("../lib/node_modules/@deepseek-ai/dsh/lib/bin.js", link)
    size = int(run_out(f"du -sm {rootfs}").split()[0])
    _what = ("网关模板（平台 jail 用，含 openresty）"
             if os.path.abspath(rootfs) == os.path.abspath(TEMPLATE_GATEWAY)
             else "运行环境模板（用户实例用）")
    info(f"{_what} 已就绪: {rootfs} ({size} MB) from {pick['name']}")


def inject_jail_user(rootfs: str, uid: int, gid: int, name: str = "dsh"):
    for path, line in (
        (f"{rootfs}/etc/passwd", f"{name}:x:{uid}:{gid}:DSH User:/home/dsh:/bin/bash\n"),
        (f"{rootfs}/etc/group", f"{name}:x:{gid}:\n"),
    ):
        text = ""
        if os.path.exists(path):
            with open(path) as fh:
                text = fh.read()
        if f":{uid}:" not in text:
            with open(path, "a") as fh:
                if text and not text.endswith("\n"):
                    fh.write("\n")
                fh.write(line)


def seed_dsh_home(st: dict, inst: dict, home: str):
    dsh_home = f"{home}/.dsh"
    os.makedirs(dsh_home, exist_ok=True)
    cred = f"{dsh_home}/.credentials.yaml"
    key = st["config"].get("model_api_key")
    if key and not os.path.exists(cred):
        atomic_write(cred, f"version: 1\nrefs:\n  DEEPSEEK_API_KEY: {key}\n", mode=0o600)
    # npm registry mirror for the instance's own npm/pnpm (profile bootstrap and
    # plugin installs run inside the jail).
    registry = st["config"].get("npm_registry")
    npmrc = f"{home}/.npmrc"
    if registry:
        atomic_write(npmrc, f"registry={registry}\n", mode=0o644)
    elif os.path.exists(npmrc):
        os.unlink(npmrc)
    uid, gid = inst.get("uid", 0), inst.get("gid", inst.get("uid", 0))
    for path in (home, dsh_home, f"{home}/workspace", f"{INSTANCES_DIR}/{inst['name']}/workspace"):
        if os.path.isdir(path):
            os.chown(path, uid, gid)
    if os.path.exists(cred):
        os.chmod(cred, 0o600)
        os.chown(cred, uid, gid)
    if os.path.exists(npmrc):
        os.chown(npmrc, uid, gid)
    # sessions/storages are created lazily by dsh; make sure the parent is writable.
    return dsh_home


# ------------------------------------------------------------------- instances


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def allocate_port(st: dict, want: int | None = None) -> int:
    used = {int(i["port"]) for i in st["instances"].values()}
    lo, hi = st["config"]["port_range"]
    if want:
        if want in used or port_in_use(want):
            die(f"port {want} unavailable")
        return want
    for port in range(int(lo), int(hi) + 1):
        if port not in used and not port_in_use(port):
            return port
    die(f"no free port in {lo}-{hi}")


def allocate_uid(st: dict) -> int:
    used = {int(i.get("uid", 0)) for i in st["instances"].values()}
    base = int(st["config"]["uid_base"])
    uid = base + 1
    while uid in used or _uid_exists(uid):
        uid += 1
    return uid


def _uid_exists(uid: int) -> bool:
    try:
        pwd.getpwuid(uid)
        return True
    except KeyError:
        return False


def service_action(name: str, action: str, check: bool = True):
    svc = f"dsh-mu-{name}"
    if not os.path.exists(f"/etc/init.d/{svc}"):
        if action in ("stop", "status"):
            return
        die(f"service {svc} not installed")
    run(["/sbin/rc-service", svc, action], check=check)


def mint_cookie(port: int, token: str, timeout: float = 30.0) -> str:
    """Exchange a launch token for the instance's own signed browser cookie.

    Retries while the instance is still coming up (the token is printed before
    the listener is ready on a cold start).
    """
    host = f"127.0.0.1:{port}"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):  # noqa: D102
            return None

    opener = urllib.request.build_opener(NoRedirect)
    tokens = [t for t in (token if isinstance(token, (list, tuple)) else [token]) if t]
    deadline = time.time() + timeout
    last = ""
    while True:
        for tok in tokens:
            req = urllib.request.Request(f"http://{host}/?token={tok}", headers={"Host": host})
            try:
                with opener.open(req, timeout=min(10.0, timeout)) as res:
                    # Some builds answer the exchange with 200 plus Set-Cookie.
                    cookie = res.headers.get("Set-Cookie") or ""
                    if cookie:
                        return cookie.split(";")[0].strip()
                    last = "the harness accepted the token but set no cookie"
            except urllib.error.HTTPError as err:
                cookie = err.headers.get("Set-Cookie") or ""
                if cookie:
                    return cookie.split(";")[0].strip()
                last = f"token exchange returned {err.code} without a cookie"
            except Exception as exc:  # connection refused while starting up
                last = str(exc)
        if time.time() >= deadline:
            info(f"instance on port {port} did not accept the token exchange: {last}")
            return ""
        time.sleep(1.0)


def recent_launch_tokens(name: str, port: int, limit: int = 3) -> list[str]:
    """The newest launch tokens the harness printed (most recent first)."""
    log = f"{LOGS}/{name}.log"
    if not os.path.exists(log):
        return []
    pattern = re.compile(rf"http://127\.0\.0\.1:{port}/\?token=([A-Za-z0-9_-]+)")
    with open(log, errors="replace") as fh:
        found = pattern.findall(fh.read())
    out: list[str] = []
    for tok in reversed(found):
        if tok not in out:
            out.append(tok)
        if len(out) >= limit:
            break
    return out


def wait_for_banner(name: str, port: int, timeout: float = 90.0) -> str | None:
    """Wait for the instance to print its one-time launch URL in its log."""
    log = f"{LOGS}/{name}.log"
    pattern = re.compile(rf"http://127\.0\.0\.1:{port}/\?token=([A-Za-z0-9_-]+)")
    deadline = time.time() + timeout
    announced = False
    while time.time() < deadline:
        if os.path.exists(log):
            with open(log, errors="replace") as fh:
                m = None
                for m in pattern.finditer(fh.read()):
                    pass
                if m:
                    return m.group(1)
        if not announced:
            info(f"instance {name}: waiting for the harness launch banner (first boot installs the profile; "
                 f"this can take a few minutes) ...")
            announced = True
        elif time.time() % 15 < 0.5:
            info(f"instance {name}: still waiting ({int(deadline - time.time())}s left)")
        time.sleep(0.5)
    return None


def startup_failure_reason(name: str, lines: int = 12) -> str:
    """The last `dsh: startup failed` reason from the instance's stderr, if any."""
    text = read_text(f"{LOGS}/{name}.err.log") or ""
    idx = text.rfind("startup failed")
    if idx < 0:
        return ""
    tail = text[idx:].splitlines()[:lines]
    picked = [ln.strip() for ln in tail if ln.strip() and not ln.startswith("        at ")]
    return " | ".join(picked[:4])[:400]


def cookie_works(port: int, cookie: str, timeout: float = 5.0) -> bool:
    """Does the harness accept this cookie right now?

    The launch banner is printed before the profile finishes loading, so a cookie
    minted at that moment can be rejected until the harness settles; probing is the
    only reliable check.
    """
    host = f"127.0.0.1:{port}"
    req = urllib.request.Request(f"http://{host}/", headers={"Host": host, "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status in (200, 302, 303)
    except urllib.error.HTTPError as err:
        return err.code in (200, 302, 303)
    except Exception:
        return False


def refresh_cookie(st: dict, name: str, timeout: float = 60.0) -> str:
    inst = st["instances"][name]
    port = int(inst["port"])
    token = wait_for_banner(name, port, timeout=timeout)
    if not token:
        reason = startup_failure_reason(name)
        if reason:
            info(f"instance {name}: the harness refused to start -> {reason}")
        else:
            info(f"instance {name}: no live launch token found in {LOGS}/{name}.log yet; "
                 f"retry later with: dsh-muctl instance refresh-cookie {name}")
        return ""
    # A restart prints a new token; older ones may still be the valid one, so try
    # the last few instead of only the newest.
    recent = recent_launch_tokens(name, port, limit=3) or [token]
    cookie = mint_cookie(port, recent)
    if not cookie:
        # The harness prints a new token on every start; a token read a moment too
        # early is already stale. Re-read the log for a while before giving up.
        deadline = time.time() + 45.0
        while not cookie and time.time() < deadline:
            time.sleep(5.0)
            fresh = recent_launch_tokens(name, port, limit=3)
            if fresh and fresh != recent:
                recent = fresh
            cookie = mint_cookie(port, recent, timeout=5.0)
    if not cookie:
        info(f"instance {name}: could not mint a cookie now; keeping the previous one "
             f"(retry: dsh-muctl instance refresh-cookie {name})")
        return ""
    # Verify it: a cookie minted while the profile is still loading is rejected.
    deadline = time.time() + 45.0
    verified = cookie_works(port, cookie)
    while not verified and time.time() < deadline:
        time.sleep(2.0)
        if cookie_works(port, cookie):
            verified = True
            break
        again = mint_cookie(port, recent_launch_tokens(name, port, limit=3) or recent,
                            timeout=10.0)
        if again:
            cookie = again
    if not verified:
        info(f"instance {name}: the minted cookie still does not verify; "
             f"the instance may need another restart")
    inst.setdefault("upstream", {})["cookie"] = cookie
    inst["upstream"]["minted_at"] = int(time.time())
    inst["upstream"]["host"] = f"127.0.0.1:{port}"
    save_state(st)
    render_gateway(st)
    _fp = hashlib.sha256(cookie.encode()).hexdigest()[:8] if cookie else "none"
    info(f"instance {name}: upstream cookie refreshed "
         f"({cookie.split('=')[0] if cookie else 'none'}, value fingerprint {_fp})")
    return cookie


def ensure_owner(st: dict, name: str):
    """Make the instance data writable by the uid the instance runs as.

    A jail-root instance has every capability dropped, so it cannot bypass file
    permissions: data left owned by another uid (e.g. after switching
    instance_run_uid) has to be re-owned explicitly.
    """
    inst = st["instances"][name]
    uid = int(inst.get("uid", 0))
    base = f"{INSTANCES_DIR}/{name}"
    for path in (f"{base}/home", f"{base}/workspace"):
        if not os.path.isdir(path):
            continue
        try:
            current = os.stat(path).st_uid
        except OSError:
            current = -1
        args = ["chown"] + (["-R"] if current != uid else []) + [f"{uid}:{uid}", path]
        run(args, check=False)


_RESOURCE_SAMPLES: dict[str, tuple[int, float]] = {}


def instance_resources(name: str) -> dict:
    """Live cgroup v2 usage for one instance (memory / cpu / pids).

    CPU percent needs a delta, so the previous sample is kept in-process: the first
    call after a restart reports null rather than a bogus 0%.
    """
    base = f"/sys/fs/cgroup/dsh-mu/{name}"
    out: dict = {"cgroup": os.path.isdir(base)}

    def read(rel: str) -> str | None:
        try:
            with open(f"{base}/{rel}") as fh:
                return fh.read().strip()
        except OSError:
            return None

    def as_int(raw: str | None) -> int | None:
        if raw is None or raw == "max":
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    mem_used = as_int(read("memory.current"))
    mem_max = as_int(read("memory.max"))
    pids_now = as_int(read("pids.current"))
    pids_max = as_int(read("pids.max"))
    cpu_raw = read("cpu.stat") or ""
    usage_usec = None
    for line in cpu_raw.splitlines():
        if line.startswith("usage_usec"):
            with contextlib.suppress(ValueError):
                usage_usec = int(line.split()[1])
    cpu_pct = None
    now = time.time()
    if usage_usec is not None:
        prev = _RESOURCE_SAMPLES.get(name)
        if prev and now > prev[1] and usage_usec >= prev[0]:
            cpu_pct = round((usage_usec - prev[0]) / 1e6 / (now - prev[1]) * 100.0, 1)
        _RESOURCE_SAMPLES[name] = (usage_usec, now)
    out.update({
        "mem_used": mem_used,
        "mem_max": mem_max,
        "cpu_pct": cpu_pct,
        "pids": pids_now,
        "pids_max": pids_max,
    })
    if mem_used is not None and mem_max:
        out["mem_pct"] = round(mem_used / mem_max * 100.0, 1)
    return out


def kill_cgroup(name: str):
    """Reap the whole jailed process tree through cgroup.kill.

    supervise-daemon only signals its direct child, while the harness lives in
    grandchildren inside the instance cgroup; without this, a stopped instance
    keeps holding its port.
    """
    kill = f"/sys/fs/cgroup/dsh-mu/{name}/cgroup.kill"
    if os.path.exists(kill):
        try:
            with open(kill, "w") as fh:
                fh.write("1")
        except OSError:
            pass


def start_instance(st: dict, name: str, timeout: float = 900.0):
    inst = st["instances"][name]
    ensure_owner(st, name)
    render_instance_conf(st, name)
    service_action(name, "start")
    inst["state"] = "running"
    inst["started_at"] = int(time.time())
    save_state(st)
    render_gateway(st)
    if timeout > 0:
        refresh_cookie(st, name, timeout=timeout)


def stop_instance(st: dict, name: str, mark: bool = True):
    service_action(name, "stop", check=False)
    kill_cgroup(name)
    if mark:
        st["instances"][name]["state"] = "stopped"
        save_state(st)
        render_gateway(st)


def instance_status(name: str) -> str:
    if not os.path.exists(f"/etc/init.d/dsh-mu-{name}"):
        return "absent"
    res = run(["/sbin/rc-service", f"dsh-mu-{name}", "status"], check=False, capture=True)
    text = (res.stdout or "") + (res.stderr or "")
    return "running" if "started" in text else "stopped"


# --------------------------------------------------------------------- commands


def cmd_init(args):
    ensure_root()
    for d in (ETC, GW_DIR, INST_CONF_DIR, VAR, INSTANCES_DIR, LOGS, LIB):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(SECRET_PATH):
        atomic_write(SECRET_PATH, os.urandom(32).hex() + "\n", mode=0o640, group=nginx_group())
        info(f"generated {SECRET_PATH}")
    if not os.path.exists(STATE_PATH):
        save_state(default_state())
        info(f"generated {STATE_PATH}")
    # Configuration file: created from defaults (migrating any legacy copy that
    # state.json still carries), plus an untouched .default for reference and
    # for the package's conffile handling.
    st = load_state()  # migrates state.json config into dsh-mu.conf when needed
    if not os.path.exists(CONF_PATH):
        save_config(st["config"])
        info(f"generated {CONF_PATH}")
    write_default_conf()
    sync_all(st)
    info("state initialised and gateway views rendered")


def installed_dsh_version() -> str | None:
    """Version of the shared DSH runtime (gateway rootfs), else the old host copy."""
    for pkg_json in (f"{dsh_runtime_dir()}/package.json",
                     f"/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json"):
        try:
            doc = json.loads(read_text(pkg_json) or "{}")
            version = str(doc.get("version") or "").strip()
            if version:
                return version
        except (OSError, ValueError):
            continue
    if shutil.which("dsh"):
        res = run(["dsh", "--version"], check=False, capture=True)
        m = re.search(r"(\d+\.\d+\.\d+[-\w.]*)", (res.stdout or "") + (res.stderr or ""))
        return m.group(1) if m else None
    return None


def install_dsh_runtime(st: dict, version: str, force: bool = False,
                        skip: bool = False) -> str:
    """Install/upgrade the shared DSH runtime *inside the gateway rootfs*.

    The host has no node; this chroots into the gateway template (which carries
    node/npm) and runs the npm install there. Instances see the result through a
    read-only bind mount, so an upgrade reaches them on their next restart.
    """
    rootfs = f"{VAR}/gateway/rootfs"
    target = f"{rootfs}{DSH_RUNTIME}"
    if skip:
        info("skipping the DSH runtime install (--skip-dsh)")
        return target
    # Only the copy *inside the gateway template* counts here: the host may still
    # carry an old global install (migration), and trusting it would skip the very
    # install that makes the host-independent setup work.
    current = ""
    try:
        doc = json.loads(read_text(f"{target}/package.json") or "{}")
        current = str(doc.get("version") or "")
    except (OSError, ValueError):
        current = ""
    if current and not force and (version == "latest" or current == version):
        info(f"@deepseek-ai/dsh@{current} already installed in the gateway template "
             f"(skip; use --force-dsh to reinstall)")
        return target
    npm = f"{rootfs}/usr/bin/npm"
    if not os.path.exists(npm):
        die("the gateway template has no npm yet; run `dsh-muctl install "
            "--force-template` first (the template now carries nodejs/npm)")
    registry = st["config"].get("npm_registry")
    cmd = ["chroot", rootfs, "/usr/bin/npm", "install", "-g", f"@deepseek-ai/dsh@{version}"]
    if registry:
        cmd += ["--registry", registry]
    # npm inside the chroot needs a writable cache; keep it in the rootfs.
    env = {**os.environ, "NPM_CONFIG_CACHE": "/root/.npm",
           "HOME": "/root", "npm_config_update_notifier": "false"}
    info(f"installing @deepseek-ai/dsh@{version} into the gateway template "
         f"(this takes a few minutes) ...")
    run(cmd, env=env)
    installed = installed_dsh_version()
    info(f"shared runtime ready: {target} (@deepseek-ai/dsh@{installed or '?'})")
    return target


# ------------------------------------------------------------------ nginx glue
#
# The gateway is installed as two fragments (HTTP-level + server) pulled in by a
# managed `include` line inside the main nginx.conf's http{} block. The main
# configuration is never replaced, so a host that already runs nginx keeps its
# sites. Fragment templates ship in <LIB>/nginx/.

NGINX_TEMPLATE_DIR = f"{LIB}/nginx"
TLS_DIR = "/etc/dsh-mu/tls"
TLS_CERT = f"{TLS_DIR}/fullchain.pem"
TLS_KEY = f"{TLS_DIR}/privkey.pem"
NGINX_CONF = "/etc/nginx/nginx.conf"
NGINX_CONF_DEFAULT = "/etc/nginx/nginx.conf.default"
NGINX_INCLUDE_MARKER = "# dsh-mu: gateway fragments (managed by dsh-muctl)"
# Signatures of the pre-fragment install, which replaced nginx.conf wholesale.
LEGACY_MONOLITH_MARKERS = (
    "Reverse proxy for DeepSeek Harness Web UI",
    "lua_shared_dict             dshmu",
    "/usr/local/lib/dsh-mu/lua/auth.lua",
)


def local_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses of this host, for the server_name list."""
    out = run(["ip", "-4", "-o", "addr", "show", "scope", "global"], check=False, capture=True).stdout or ""
    return [line.split()[3].split("/")[0] for line in out.splitlines() if len(line.split()) > 3]


def detect_hostname() -> str:
    """Short host name, used as the default GATEWAY_HOST on a fresh install."""
    return (socket.gethostname() or "").split(".")[0] or "dsh-mu"


# ------------------------------------------------------------ platform jail
# The gateway can run on the host (`GATEWAY_MODE=host`, the current default) or
# inside its own jail (`jail`). The jail form needs no host nginx at all: the
# whole nginx.conf is ours, the fragments live in the jail's volume, and the
# process is supervised by the same jailed runner the instances use.
GATEWAY_DIR = f"{VAR}/gateway"
TEMPLATE_GATEWAY = f"{VAR}/templates/gateway"
GATEWAY_CONF = f"{ETC}/gateway.conf"
GATEWAY_SERVICE = "/etc/init.d/dsh-mu-gateway"
GATEWAY_CONF_IN_JAIL = "/etc/dsh-mu-gateway"
GATEWAY_TLS_IN_JAIL = "/etc/dsh-mu-gateway-tls"
GATEWAY_PACKAGES = ["openresty", "lua-resty-string", "lua-resty-hmac",
                    "lua-resty-openssl", "lua-resty-core",
                    # The shared DSH runtime is installed *inside* this jail, so the
                    # host needs no node at all; instances bind-mount it read-only.
                    "nodejs", "npm", "pnpm"]

GATEWAY_NGINX_MAIN = """# dsh-mu gateway (inside the platform jail) - generated by dsh-muctl.
# The package's /etc/nginx stays untouched so mime.types and friends resolve.
# Inside its own jail the gateway runs as root: the jail is the boundary, and
# this keeps nginx from having to chown/setuid into another uid (which would
# need extra capabilities). Only CAP_NET_BIND_SERVICE is kept.
user  root;
worker_processes auto;
# Run in the foreground: the service supervisor (OpenRC supervise-daemon) owns the
# process, and passing "-g 'daemon off;'" through command_args loses its quoting.
daemon off;
error_log  /var/log/dsh-mu/gateway.err.log warn;
pid        /run/dsh-mu-gateway.pid;

events { worker_connections 1024; }

http {
    include            /etc/nginx/mime.types;
    default_type       application/octet-stream;
    sendfile           on;
    tcp_nopush         on;
    keepalive_timeout  65;
    client_max_body_size 512m;
    proxy_read_timeout   3600s;
    proxy_send_timeout   3600s;

    include            @CONF_DIR@/conf.d/*.conf;
    include            @CONF_DIR@/http.d/*.conf;
}
"""


def jails_supported() -> bool:
    """Can this kernel/container create the mount namespace a jail needs?"""
    res = run(["unshare", "--mount", "true"], check=False, capture=True)
    return res.returncode == 0


def gateway_paths() -> dict:
    paths = {"rootfs": f"{GATEWAY_DIR}/rootfs", "conf": f"{GATEWAY_DIR}/conf",
             "logs": f"{GATEWAY_DIR}/logs", "home": f"{GATEWAY_DIR}/home",
             "work": f"{GATEWAY_DIR}/work", "etc": f"{GATEWAY_DIR}/etc"}
    for path in paths.values():
        os.makedirs(path, exist_ok=True)
    os.makedirs(f"{paths['conf']}/conf.d", exist_ok=True)
    os.makedirs(f"{paths['conf']}/http.d", exist_ok=True)
    return paths


def jail_nginx_gid(paths: dict | None = None) -> int:
    """The gid of the `nginx` group *inside* the jail template."""
    paths = paths or gateway_paths()
    try:
        with open(f"{paths['rootfs']}/etc/group", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(":")
                if len(parts) >= 4 and parts[0] == "nginx":
                    return int(parts[2])
    except (OSError, ValueError):
        pass
    return 0


def sync_gateway_etc() -> str:
    """Copy the files the gateway Lua reads into the jail's own /etc/dsh-mu.

    The jail mounts this directory read-only at /etc/dsh-mu. Keeping a copy (with
    the jail's gid on the secret) avoids depending on host<->jail gid mapping.
    """
    paths = gateway_paths()
    etc = paths["etc"]
    os.makedirs(f"{etc}/gateway", exist_ok=True)
    for name in ("users.json", "upstreams.json", "config.json", "relay.json"):
        src = f"{GW_DIR}/{name}"
        if os.path.exists(src):
            shutil.copy2(src, f"{etc}/gateway/{name}")
            os.chmod(f"{etc}/gateway/{name}", 0o640)
    gid = jail_nginx_gid(paths)
    for name, mode in (("secret.key", 0o640), ("admin.token", 0o640)):
        src = f"{ETC}/{name}"
        if not os.path.exists(src):
            continue
        dst = f"{etc}/{name}"
        shutil.copy2(src, dst)
        os.chmod(dst, mode)
        with contextlib.suppress(PermissionError, OSError):
            os.chown(dst, 0, gid)
    return etc


def write_gateway_jail_conf(cfg: dict, port: int | None = None) -> str:
    """The runner config for the platform jail (same format as an instance conf)."""
    paths = gateway_paths()
    port = int(port or cfg.get("gateway_listen", 80))
    mount_list = [
        f"{paths['etc']}:{ETC}:ro",
        f"{LIB}:{LIB}:ro",
        f"{LOGS}:{LOGS}:rw",
        f"{paths['conf']}:{GATEWAY_CONF_IN_JAIL}:rw",
    ]
    ensure_tls_dir()
    mount_list.append(f"{TLS_DIR}:{GATEWAY_TLS_IN_JAIL}:ro")
    mounts = ",".join(mount_list)
    body = f"""# generated by dsh-muctl - platform (gateway) jail
ROOTFS={paths['rootfs']}
INSTANCE_HOME={paths['home']}
INSTANCE_WORK={paths['work']}
PORT={port}
INST_UID=0
INST_GID=0
CGROUP_PATH=/sys/fs/cgroup/dsh-mu/gateway
EXTRA_MOUNTS={mounts}
KEEP_CAPS=net_bind_service
DSH_RUNTIME_DIR={paths['rootfs']}/nonexistent
"""
    atomic_write(GATEWAY_CONF, body, mode=0o644)
    return GATEWAY_CONF


def render_gateway_jail_conf_files(cfg: dict, port: int | None = None) -> dict:
    """Write the whole nginx configuration into the jail's volume."""
    paths = gateway_paths()
    port = int(port or cfg.get("gateway_listen", 80))
    jail_cfg = {**cfg, "gateway_listen": port,
                "nginx_conf_d_dir": f"{paths['conf']}/conf.d",
                "nginx_http_d_dir": f"{paths['conf']}/http.d"}
    # Our own main file owns the http{} block, so default_server is always free.
    rendered = render_gateway_fragments(jail_cfg, quiet=True)
    for path, text in rendered.items():
        atomic_write(path, text, mode=0o644)
    main = GATEWAY_NGINX_MAIN.replace("@CONF_DIR@", GATEWAY_CONF_IN_JAIL)
    atomic_write(f"{paths['conf']}/nginx.conf", main, mode=0o644)

    # Loopback-only relay for model plan traffic: instances send the placeholder and
    # the real provider key is injected here (it never enters an instance jail).
    if relay_enabled(cfg):
        relay_port = int(cfg.get("relay_listen", 8099) or 8099)
        resolver = "127.0.0.1"
        with contextlib.suppress(OSError):
            for line in (read_text("/etc/resolv.conf") or "").splitlines():
                if line.startswith("nameserver"):
                    resolver = line.split()[1]
                    break
        relay_conf = f"""# dsh-mu relay (generated): model plan keys are injected here.
server {{
    listen 127.0.0.1:{relay_port};
    server_name relay.local;
    access_log /var/log/dsh-mu/relay.log;
    client_max_body_size 512m;
    resolver {resolver} ipv6=off valid=120s;

    location ^~ /mu-llm/ {{
        set $relay_target "";
        set $relay_host "";
        set $relay_scheme "https";
        set $relay_auth "";
        set $relay_xkey "";
        set $relay_plan "";
        set $relay_instance "";
        access_by_lua_file {LIB}/lua/relay.lua;

        proxy_pass $relay_target;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $relay_host;
        proxy_set_header Authorization $relay_auth;
        proxy_set_header x-api-key $relay_xkey;
        proxy_set_header X-Relay-Plan $relay_plan;
        proxy_set_header X-Relay-Instance $relay_instance;
        proxy_ssl_server_name on;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding on;
        gzip off;
    }}
}}
"""
        atomic_write(f"{paths['conf']}/conf.d/relay.conf", relay_conf, mode=0o644)
        rendered[f"{paths['conf']}/conf.d/relay.conf"] = relay_conf
    else:
        with contextlib.suppress(OSError):
            os.unlink(f"{paths['conf']}/conf.d/relay.conf")
    return {"conf": paths["conf"], "port": port,
            "files": sorted(os.path.basename(p) for p in rendered)}


def render_gateway_service() -> str:
    jail = JAIL if os.path.exists(JAIL) else "dsh-mu-jail"
    body = (INIT_TEMPLATE
            .replace("@NAME@", "__gateway")
            .replace("@JAIL@", jail)
            .replace("@BASE@", GATEWAY_DIR)
            .replace("@CGROUP@", "/sys/fs/cgroup/dsh-mu/gateway")
            .replace("dsh-mu instance __gateway", "dsh-mu gateway (platform jail)")
            .replace("DeepSeek Harness instance '__gateway' (mount-ns + chroot + cgroup jail)",
                     "dsh-mu gateway: openresty inside a mount-ns + chroot + cgroup jail"))
    # The runner takes the conf through --conf, so pass it right after the jail.
    cmd = f"nginx -c {GATEWAY_CONF_IN_JAIL}/nginx.conf"
    body = body.replace('command_args="__gateway"',
                        'command_args="--conf ' + GATEWAY_CONF + ' __gateway -- ' + cmd + '"')
    atomic_write(GATEWAY_SERVICE, body, mode=0o755)
    return GATEWAY_SERVICE


def build_gateway_jail(st: dict, force: bool = False, port: int | None = None,
                       interactive: bool | None = None) -> dict:
    """Template + volume + config + service for the platform jail."""
    cfg = st["config"]
    paths = gateway_paths()
    if not jails_supported():
        # A chroot/CI container cannot create mount namespaces, so a jail rootfs
        # would be 214 MB nobody can start. Render the configuration and service
        # only; the target machine builds the rootfs on its first real install.
        info("mount namespaces unavailable here: writing the gateway configuration "
             "and service, skipping the jail rootfs")
        sync_gateway_etc()
        rendered = render_gateway_jail_conf_files(cfg, port)
        service = render_gateway_service()
        return {"rootfs": None, "conf": rendered, "service": service,
                "skipped": "no mount namespaces"}
    build_template(st, force=force, interactive=interactive, dest=TEMPLATE_GATEWAY,
                   packages=list(cfg["template_packages"]) + GATEWAY_PACKAGES)
    if not os.path.exists(f"{paths['rootfs']}/usr/sbin/nginx") or force:
        if os.path.exists(paths["rootfs"]):
            shutil.rmtree(paths["rootfs"])
        info(f"copying the gateway template -> {paths['rootfs']} ...")
        run(["cp", "-a", TEMPLATE_GATEWAY, paths["rootfs"]])
    sync_gateway_etc()
    rendered = render_gateway_jail_conf_files(cfg, port)
    write_gateway_jail_conf(cfg, port)
    service = render_gateway_service()
    run(["/sbin/rc-update", "add", "dsh-mu-gateway", "default"], check=False)
    return {"rootfs": paths["rootfs"], "conf": rendered, "service": service}


def gateway_health(port: int) -> dict:
    """Is something alive on this port that looks like our gateway?"""
    code = 0
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/login",
                                     headers={"User-Agent": f"dsh-mu/{VERSION}"})
        with urllib.request.urlopen(req, timeout=5) as res:
            code = res.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception:  # noqa: BLE001 - down is a normal answer here
        code = 0
    return {"port": port, "http": code, "ok": code in (200, 302, 401, 403)}


def cmd_gateway_jail(args):
    """`dsh-muctl gateway build|start|stop|restart|conf|logs` (platform jail)."""
    ensure_root()
    st = load_state()
    cfg = st["config"]
    sub = args.action
    port = int(getattr(args, "port", 0) or cfg.get("gateway_listen", 80))
    if sub == "build":
        info(f"building the gateway jail (port {port}) ...")
        result = build_gateway_jail(st, force=args.force, port=port)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if sub == "conf":
        print(json.dumps(render_gateway_jail_conf_files(cfg, port), indent=2, ensure_ascii=False))
        return 0
    if sub in ("start", "restart"):
        run(["/sbin/rc-service", "dsh-mu-gateway", sub], check=False)
        time.sleep(2)
        health = gateway_health(port)
        print(json.dumps({"service": sub, **health}, indent=2, ensure_ascii=False))
        return 0 if health["ok"] else 1
    if sub == "stop":
        run(["/sbin/rc-service", "dsh-mu-gateway", "stop"], check=False)
        return 0
    if sub == "jail-status":
        started = bool(run(["pgrep", "-f", f"{GATEWAY_CONF}"], check=False, capture=True).stdout.strip())
        print(json.dumps({"started": started, **gateway_health(port)}, indent=2, ensure_ascii=False))
        return 0
    if sub == "logs":
        path = f"{LOGS}/__gateway.err.log"
        try:
            with open(path, errors="replace") as fh:
                lines = fh.readlines()[-max(1, int(getattr(args, "lines", 100))):]
        except OSError:
            lines = []
        print("".join(lines).rstrip() or f"(no log yet: {path})")
        return 0
    die(f"unknown gateway subcommand: {sub}")


def panel_cache_key() -> str:
    """A short key that changes whenever the panel assets change."""
    parts = []
    for name in ("panel.js", "icon.png"):
        path = f"{LIB}/static/{name}"
        try:
            st = os.stat(path)
            parts.append(f"{st.st_size:x}{int(st.st_mtime):x}")
        except OSError:
            parts.append("0")
    return "".join(parts) or "0"


def tls_enabled(cfg: dict) -> bool:
    return bool(cfg.get("tls_enabled", False))


def tls_cert_ready(cfg: dict) -> bool:
    """Both files must exist, be readable, and the key must be mode 0600-0640."""
    if not (os.path.isfile(TLS_CERT) and os.path.isfile(TLS_KEY)):
        return False
    try:
        with open(TLS_CERT, "rb") as fh:
            if b"BEGIN CERTIFICATE" not in fh.read(200):
                return False
        with open(TLS_KEY, "rb") as fh:
            if b"PRIVATE KEY" not in fh.read(100):
                return False
    except OSError:
        return False
    return True


def tls_cert_info() -> dict:
    """Subject / issuer / not-after of the installed certificate (best effort)."""
    info = {"subject": "", "issuer": "", "not_after": "", "days_left": None}
    if not os.path.isfile(TLS_CERT):
        return info
    text = run(["openssl", "x509", "-in", TLS_CERT, "-noout", "-subject", "-issuer",
                "-enddate"], check=False, capture=True)
    out = (text.stdout or "") + (text.stderr or "")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("subject="):
            info["subject"] = line.split("=", 1)[1].strip()
        elif line.startswith("issuer="):
            info["issuer"] = line.split("=", 1)[1].strip()
        elif line.startswith("notAfter="):
            info["not_after"] = line.split("=", 1)[1].strip()
    if info["not_after"]:
        with contextlib.suppress(Exception):
            from datetime import datetime, timezone
            when = datetime.strptime(info["not_after"], "%b %d %H:%M:%S %Y %Z")
            delta = when.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
            info["days_left"] = max(0, delta.days)
    return info


def listen_directives(cfg: dict, port: int, listen_default: str) -> tuple[str, str]:
    """Return (listen lines for the main server, extra 80-redirect server).

    The main server owns `port` unless plain HTTP is redirected to TLS; the
    template therefore carries no listen line of its own (two `listen 80` lines in
    one server make nginx refuse to start).
    """
    tls = tls_enabled(cfg)
    redirect = tls and bool(cfg.get("tls_redirect", False))
    lines = []
    if not (tls and redirect):
        lines.append(f"    listen                  {port}{listen_default};\n")
    redirect_server = ""
    if tls:
        base = GATEWAY_TLS_IN_JAIL
        lines.append("    listen                  443 ssl;\n")
        lines.append(f"    ssl_certificate         {base}/fullchain.pem;\n")
        lines.append(f"    ssl_certificate_key     {base}/privkey.pem;\n")
        lines.append("    ssl_protocols           TLSv1.2 TLSv1.3;\n")
        lines.append("    ssl_ciphers             HIGH:!aNULL:!MD5;\n")
        lines.append("    ssl_session_cache       shared:dshmu_tls:10m;\n")
        lines.append("    ssl_session_timeout     1h;\n")
        lines.append("    ssl_prefer_server_ciphers off;\n")
        if redirect:
            redirect_server = (
                "server {\n"
                f"    listen                  {port};\n"
                "    server_name             _;\n"
                "    access_log              off;\n"
                "    location / { return 301 https://$host$request_uri; }\n"
                "}\n"
            )
    return "".join(lines), redirect_server


def render_gateway_fragments(cfg: dict, quiet: bool = False) -> dict[str, str]:
    http_dir = cfg["nginx_conf_d_dir"]
    server_dir = cfg["nginx_http_d_dir"]
    listen = int(cfg.get("gateway_listen", 80))
    # `localhost` is deliberately absent: a stock Alpine nginx already claims it,
    # and duplicating it only produces a "conflicting server name" warning while
    # the default_server flag already routes every other Host here.
    names = [str(cfg.get("gateway_host", "dsh-mu")), detect_hostname(), "127.0.0.1"] + local_ipv4_addresses()
    # Preserve order, drop duplicates.
    names = list(dict.fromkeys(n for n in names if n))

    server_tpl = read_text(f"{NGINX_TEMPLATE_DIR}/dsh-mu-server.conf")
    if not server_tpl:
        raise FileNotFoundError(f"gateway template missing: {NGINX_TEMPLATE_DIR}/dsh-mu-server.conf")
    http_tpl = read_text(f"{NGINX_TEMPLATE_DIR}/dsh-mu-http.conf")
    if not http_tpl:
        raise FileNotFoundError(f"gateway template missing: {NGINX_TEMPLATE_DIR}/dsh-mu-http.conf")

    if port_has_default_server(listen, cfg):
        listen_default, note = "", " (another config already owns default_server; matching by server_name)"
    else:
        listen_default, note = " default_server", ""
    listen_block, tls_redirect_server = listen_directives(cfg, listen, listen_default)
    rendered = (server_tpl
                .replace("@LISTEN_DIRECTIVES@", listen_block)
                .replace("@SERVER_NAMES@", " ".join(names))
                # Cache-buster for the panel UI: /mu-static has a 10 min cache, and
                # a new release must not keep serving the old panel.js. Derive it
                # from the files themselves so any edit changes the URL.
                .replace("@VERSION@", panel_cache_key()))
    if not quiet:
        info(f"gateway server fragment: listen {listen}{note}, server_name {' '.join(names)}")
    files = {f"{http_dir}/dsh-mu-http.conf": http_tpl,
             f"{server_dir}/dsh-mu-server.conf": rendered}
    redirect_path = f"{server_dir}/dsh-mu-redirect.conf"
    if tls_redirect_server:
        files[redirect_path] = tls_redirect_server
    elif os.path.exists(redirect_path):
        with contextlib.suppress(OSError):
            os.unlink(redirect_path)
    return files


def port_has_default_server(port: int, cfg: dict) -> bool:
    """Whether some *other* config already declares default_server on this port."""
    # Comments are stripped first, then a listen directive may span lines.
    pattern = re.compile(rf"\blisten\b[^;]*\b{port}\b[^;]*default_server", re.S)
    candidates = [NGINX_CONF]
    for directory in (cfg["nginx_conf_d_dir"], cfg["nginx_http_d_dir"],
                      "/etc/nginx/sites-enabled", "/etc/nginx/vhosts.d"):
        for path in glob.glob(f"{directory}/*"):
            if os.path.isfile(path) and not os.path.basename(path).startswith(("dsh-mu-", ".")):
                candidates.append(path)
    for path in candidates:
        text = read_text(path)
        if text is None:
            continue
        stripped = re.sub(r"#[^\n]*", "", text)
        if pattern.search(stripped):
            return True
    return False


def ensure_nginx_include(cfg: dict) -> tuple[str, str | None]:
    """Insert the managed include line into the main http{} block (idempotent).

    Returns (status, backup_path): status is "present" or "inserted"; the backup
    path is the pre-edit copy so the caller can roll back a rejected config.
    """
    text = read_text(NGINX_CONF)
    if text is None:
        die(f"nginx configuration not found: {NGINX_CONF} (is openresty/nginx installed?)")
    if NGINX_INCLUDE_MARKER in text:
        return "present", None
    conf_glob = f"{cfg['nginx_conf_d_dir']}/dsh-mu-*.conf"
    http_glob = f"{cfg['nginx_http_d_dir']}/dsh-mu-*.conf"
    m = re.search(r"^([ \t]*)http[ \t]*\{[^\n]*\n", text, re.M)
    if not m:
        die(f"could not find an `http {{` block in {NGINX_CONF}; add these two lines inside it "
            f"yourself:\n  include {conf_glob};\n  include {http_glob};")
    backup = f"{NGINX_CONF}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(NGINX_CONF, backup)
    insert = (f"{m.group(0)}"
              f"{m.group(1)}{NGINX_INCLUDE_MARKER}\n"
              f"{m.group(1)}include {conf_glob};\n"
              f"{m.group(1)}include {http_glob};\n")
    atomic_write(NGINX_CONF, text[:m.start()] + insert + text[m.end():], mode=0o644)
    info(f"patched {NGINX_CONF} with the managed include (backup: {backup})")
    return "inserted", backup


def restore_stock_nginx_conf() -> bool:
    """Undo the legacy whole-file install so fragments can take over."""
    text = read_text(NGINX_CONF) or ""
    if not any(marker in text for marker in LEGACY_MONOLITH_MARKERS):
        return False
    if not os.path.exists(NGINX_CONF_DEFAULT):
        die(f"{NGINX_CONF} looks like the legacy dsh-mu install but {NGINX_CONF_DEFAULT} is missing; "
            f"restore your own nginx.conf manually, then re-run `dsh-muctl gateway install`")
    backup = f"{NGINX_CONF}.dshmu-legacy-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(NGINX_CONF, backup)
    shutil.copy2(NGINX_CONF_DEFAULT, NGINX_CONF)
    info(f"replaced the legacy whole-file gateway config with the distribution default "
         f"(legacy copy kept at {backup})")
    return True


def install_gateway_fragments(cfg: dict, reload: bool = True) -> bool:
    """Install both fragments, wire the include, validate and reload openresty.

    The main nginx.conf is only ever touched by the managed include line, and
    that edit is rolled back when nginx rejects the resulting configuration.
    """
    restore_stock_nginx_conf()
    try:
        fragments = render_gateway_fragments(cfg)
    except OSError as exc:
        die(f"{exc}; reinstall the dsh-mu package")
    for path, content in fragments.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write(path, content, mode=0o644)
        info(f"wrote {path}")
    _status, backup = ensure_nginx_include(cfg)
    check = run(["/usr/sbin/nginx", "-t"], check=False, capture=True)
    if check.returncode != 0:
        info("nginx configuration test FAILED:")
        info((check.stderr or check.stdout or "").strip())
        if backup:
            shutil.copy2(backup, NGINX_CONF)
            info(f"rolled {NGINX_CONF} back to {backup}")
        return False
    info("nginx configuration test passed")
    if reload and os.path.exists("/etc/init.d/openresty"):
        run(["/sbin/rc-service", "openresty", "reload"], check=False)
    return True


def cmd_gateway_install(args):
    ensure_root()
    st = load_state()
    ok = install_gateway_fragments(st["config"], reload=args.reload)
    if not ok:
        die("gateway fragments installed but nginx rejected the configuration")


def system_admin_names(cfg: dict) -> list[str]:
    """Host accounts that administer the platform (passwords live in /etc/shadow)."""
    raw = str(cfg.get("admin_system_users") or "root")
    return [n.strip() for n in raw.replace(",", " ").split() if n.strip()] or ["root"]


def shadow_password_state(user: str) -> tuple[bool, str]:
    """Whether `user` exists in /etc/shadow with a usable password set."""
    try:
        with open("/etc/shadow", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split(":")
                if parts and parts[0] == user:
                    stored = parts[1] if len(parts) > 1 else ""
                    if not stored:
                        return False, "no password set"
                    if stored[0] in "!*":
                        return False, "locked"
                    return True, "password set"
    except OSError as exc:
        return False, str(exc)
    return False, "no such system account"


def ensure_system_admins(st: dict) -> list[dict]:
    """Keep a platform record for each system admin account (role=admin, no password).

    Admins authenticate with the host password, so their record carries no
    platform credential: it only exists so sessions can be validated (epoch,
    enabled) like any other account.
    """
    cfg = st["config"]
    report = []
    changed = False
    for name in system_admin_names(cfg):
        ready, why = shadow_password_state(name)
        report.append({"user": name, "password_ready": ready, "detail": why})
        record = st["users"].get(name)
        if record is None:
            st["users"][name] = {
                "pw": "", "role": "admin", "enabled": True, "epoch": 0,
                "must_change": False, "instance": "", "system": True,
                "created_at": int(time.time()), "updated_at": int(time.time()),
            }
            changed = True
        else:
            if record.get("role") != "admin" or not record.get("system"):
                record["role"] = "admin"
                record["system"] = True
                record["pw"] = ""
                record["updated_at"] = int(time.time())
                changed = True
    # Any other account that still carries role=admin is demoted: admin is
    # locked to the host accounts above.
    for name, record in st["users"].items():
        if name not in system_admin_names(cfg) and record.get("role") == "admin":
            record["role"] = "user"
            record["updated_at"] = int(time.time())
            changed = True
            report.append({"user": name, "demoted": True,
                           "detail": "admin is reserved for system accounts"})
    if changed:
        save_state(st)
    return report


def ensure_default_kilo_plan() -> str | None:
    """Make the keyless Kilo preset usable out of the box.

    Creates (once) a team plan `kilo-free` owned by "*" so every user sees it,
    makes it the platform default when nothing else is set, and leaves operator
    plans untouched.
    """
    pid = "plan_kilo_free"
    existing = load_plan(pid)
    if existing is None:
        plan = {
            "schema": PLAN_SCHEMA,
            "id": pid,
            "name": "Kilo Free（默认）",
            "provider": "kilo",
            "owner": "*",
            "visible_to": ["*"],
            "enabled": True,
            "models": ["kilo-auto/free"],
            "default_model": "kilo-auto/free",
            "keyless": True,
        }
        save_plan(plan)
        info("default plan created: kilo-free (keyless, model kilo-auto/free)")
    cfg = load_config()
    if not str(cfg.get("default_plan") or ""):
        cfg["default_plan"] = pid
        save_config(cfg)
        info("default_plan = " + pid)
    return pid


def cmd_install(args):
    ensure_root()
    cfg_missing = not os.path.exists(STATE_PATH)
    # A read-only command (`doctor`, which our own docs suggest running first)
    # can already have created state.json, so decide by the artefacts instead:
    # without the session secret the gateway cannot sign a single session.
    if cfg_missing or not os.path.exists(SECRET_PATH) or not os.path.exists(CONF_PATH):
        cmd_init(args)
    st = load_state()
    if args.model_api_key:
        st["config"]["model_api_key"] = args.model_api_key
    if args.gateway_host:
        st["config"]["gateway_host"] = args.gateway_host
    elif cfg_missing:
        # Fresh install: publish the gateway under this machine's own name.
        st["config"]["gateway_host"] = detect_hostname()
    if args.model_api_key or args.gateway_host or cfg_missing:
        save_config(st["config"])

    # Region detection happens once, here, because it decides three things: the
    # apk mirror, the image mirror and the npm registry used for the global DSH
    # install (the slowest step of a cold install when it points abroad).
    if st["config"].get("npm_registry"):
        info(f"npm registry: {st['config']['npm_registry']} (configured)")
    else:
        chosen, why = pick_npm_registry()
        if chosen:
            st["config"]["npm_registry"] = chosen
            save_config(st["config"])
            info(f"npm registry: {chosen} ({why})")
        else:
            info(f"npm registry: default ({why})")

    mode_now = str(st["config"].get("gateway_mode") or "jail")
    # No node on the host any more: the DSH runtime is installed inside the
    # gateway template (which carries nodejs/npm).
    pkgs = ["util-linux", "python3", "logrotate", "curl"]
    if mode_now != "jail":
        # Only the legacy host-nginx mode needs these on the host; in jail mode the
        # gateway's openresty lives inside its own rootfs.
        pkgs += ["openresty", "lua-resty-string", "lua-resty-hmac", "lua-resty-openssl"]
    info(f"installing platform packages ({mode_now} mode) ...")
    run(["apk", "add", "--no-cache"] + pkgs)
    version = st["config"].get("dsh_version") or "latest"
    if mode_now != "jail" and not getattr(args, "skip_dsh", False):
        # legacy host-nginx mode keeps the runtime in the host's global npm dir
        registry = st["config"].get("npm_registry")
        cmd = ["npm", "install", "-g", f"@deepseek-ai/dsh@{version}"]
        if registry:
            cmd += ["--registry", registry]
        run(cmd, env={**os.environ, **npm_env(st["config"])})
    for d in (f"{LIB}/lua", f"{LIB}/www", LOGS):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(CONF_PATH):
        save_config(st["config"])
        info(f"generated {CONF_PATH}")
    info(f"wrote {write_default_conf()}")
    build_template(st, force=args.force_template,
                   interactive=False if getattr(args, "assume_yes", False) else None,
                   image=getattr(args, "template_image", None))
    render_cgroup_service()
    run(["/sbin/rc-service", "dsh-mu-cgroup", "start"], check=False)
    sync_all(st)
    if str(st["config"].get("gateway_mode") or "host") == "jail":
        # The gateway runs inside its own jail; the host needs no openresty at all.
        info("gateway_mode=jail: building the platform jail (openresty inside)")
        host_nginx = os.path.exists("/etc/init.d/openresty")
        if host_nginx:
            # Free the port first: the jail has to bind it now. If the jail does not
            # come up, the host openresty is started again below (rollback).
            run(["/sbin/rc-update", "del", "openresty", "default"], check=False)
            run(["/sbin/rc-service", "openresty", "stop"], check=False)
            info("host openresty stopped so the jail can take the port")
        build_gateway_jail(st, force=args.force_template)
        install_dsh_runtime(st, version, force=args.force_dsh,
                            skip=getattr(args, "skip_dsh", False))
        run(["/sbin/rc-service", "dsh-mu-gateway", "restart"], check=False)
        health = gateway_health(int(st["config"].get("gateway_listen", 80)))
        if health["ok"]:
            info(f"gateway jail is answering on port {health['port']} (HTTP {health['http']})")
        else:
            info(f"warning: the gateway jail is not answering on port {health['port']} "
                 f"(see `dsh-muctl gateway logs`)")
            if host_nginx:
                run(["/sbin/rc-update", "add", "openresty", "default"], check=False)
                run(["/sbin/rc-service", "openresty", "start"], check=False)
                info("rolled back: the host openresty was started again")
    rot = install_logrotate(st["config"])
    info(f"log rotation: {rot}" if rot else
         "log rotation: logrotate not present; schedule `dsh-muctl logs rotate` yourself "
         "(apk add logrotate for the daily drop-in)")
    install_admin(st["config"])
    for entry in ensure_system_admins(load_state()):
        if entry.get("demoted"):
            info(f"note: {entry['user']} is no longer an administrator ({entry['detail']})")
        elif entry.get("password_ready"):
            info(f"admin account {entry['user']} authenticates with the operating system password")
        else:
            info(f"WARNING: admin account {entry['user']} cannot log in yet: {entry['detail']}; "
                 f"set one with `passwd {entry['user']}`")
    # Out-of-the-box model access: a keyless team plan on Kilo's free gateway, so a
    # fresh install can talk to a model before the operator configures any key.
    with contextlib.suppress(Exception):
        ensure_default_kilo_plan()
        sync_all(load_state())
    started = reconcile_instances(load_state())
    if started:
        info("restarted instances recorded as running: " + ", ".join(started))
    checks = doctor_checks(load_state())
    fails = [c for c in checks if c["status"] == DOCTOR_FAIL]
    warns = [c for c in checks if c["status"] == DOCTOR_WARN]
    info(f"doctor: {sum(1 for c in checks if c['status'] == DOCTOR_PASS)} PASS, {len(warns)} WARN, {len(fails)} FAIL")
    for c in fails:
        info(f"  FAIL {c['check']}: {c['detail']} -> {c['fix']}")
    for c in warns:
        info(f"  WARN {c['check']}: {c['detail']}")
    info("install complete")


def cmd_user_add(args):
    ensure_root()
    st = load_state()
    name = args.name
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        die("username must match [a-z][a-z0-9_-]{0,31}")
    if name in st["users"]:
        die(f"user {name} already exists")
    password = args.password
    if not password:
        if sys.stdin.isatty():
            import getpass
            password = getpass.getpass("password: ")
            confirm = getpass.getpass("repeat: ")
            if password != confirm:
                die("passwords do not match")
        else:
            password = sys.stdin.readline().strip()
    if not password:
        password = random_password()
        info(f"generated password for {name}: {password}")
    require_password(password)
    cfg = st["config"]
    st["users"][name] = {
        "pw": hash_password(password, cfg["password_algo"], cfg["pbkdf2_iterations"]),
        "role": args.role,
        "enabled": True,
        "epoch": 0,
        "must_change": bool(getattr(args, "must_change", False)),
        # `none`/`auto` are CLI keywords: `none` means "this account has no
        # instance of its own" and is stored as an empty string.
        "instance": ("" if args.instance == "none"
                     else (name if not args.instance or args.instance == "auto" else args.instance)),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    save_state(st)
    created = False
    if args.instance != "none":
        if args.instance not in (None, "auto") and args.instance != name:
            st["users"][name]["instance"] = args.instance
            save_state(st)
        created = False
        if name not in st["instances"] and st["users"][name]["instance"] == name:
            create_instance(st, name, user=name, cpu=None, mem=None, pids=None, port=None)
            created = True
    sync_all(st)
    # Start it right away: a stopped instance has no upstream cookie, and a login
    # against one only says "实例凭据未就绪" (the user then has to mint it by hand).
    if created and name in st["instances"] and not getattr(args, "no_start", False):
        start_instance(st, name)
    info(f"user {name} created (role={args.role}, instance={st['users'][name]['instance'] or '-'})")


def cmd_user_list(args):
    st = load_state()
    rows = [("USER", "ROLE", "ENABLED", "EPOCH", "MUST-CHANGE", "INSTANCE", "INST-STATE")]
    for name, u in sorted(st["users"].items()):
        inst = u.get("instance") or ""
        state = st["instances"].get(inst, {}).get("state", "-") if inst else "-"
        rows.append((name, u.get("role", "user"), "yes" if u.get("enabled", True) else "no",
                     str(u.get("epoch", 0)), "yes" if u.get("must_change") else "-",
                     inst or "-", state))
    width = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(width[i]) for i, c in enumerate(r)))


def cmd_user_passwd(args):
    ensure_root()
    st = load_state()
    u = st["users"].get(args.name) or die(f"no such user: {args.name}")
    password = args.password or (sys.stdin.readline().strip() if not sys.stdin.isatty() else None)
    if password is None:
        import getpass
        password = getpass.getpass("password: ")
    require_password(password)
    cfg = st["config"]
    u["pw"] = hash_password(password, cfg["password_algo"], cfg["pbkdf2_iterations"])
    u["epoch"] = int(u.get("epoch", 0)) + 1  # invalidates every existing session
    u["must_change"] = bool(getattr(args, "must_change", False))
    u["updated_at"] = int(time.time())
    save_state(st)
    sync_all(st)
    info(f"password updated for {args.name}; existing sessions invalidated"
         + ("; the holder will be asked to change it" if u["must_change"] else ""))


def cmd_user_kick(args):
    """Invalidate every session of an account without touching its password."""
    ensure_root()
    st = load_state()
    u = st["users"].get(args.name) or die(f"no such user: {args.name}")
    u["epoch"] = int(u.get("epoch", 0)) + 1
    u["updated_at"] = int(time.time())
    save_state(st)
    sync_all(st)
    info(f"sessions of {args.name} invalidated (epoch {u['epoch']}); "
         "the password is unchanged, so they can log in again")


def cmd_user_toggle(args, enabled: bool):
    ensure_root()
    st = load_state()
    u = st["users"].get(args.name) or die(f"no such user: {args.name}")
    u["enabled"] = enabled
    u["epoch"] = int(u.get("epoch", 0)) + 1
    u["updated_at"] = int(time.time())
    save_state(st)
    sync_all(st)
    info(f"user {args.name} {'enabled' if enabled else 'disabled'}")


def cmd_user_del(args):
    ensure_root()
    st = load_state()
    if args.name not in st["users"]:
        die(f"no such user: {args.name}")
    inst = st["users"][args.name].get("instance")
    del st["users"][args.name]
    save_state(st)
    sync_all(st)
    if inst:
        if getattr(args, "with_instance", False):
            ns = argparse.Namespace(name=inst, purge=True)
            cmd_instance_del(ns)
            info(f"user {args.name} deleted together with instance {inst}")
        else:
            info(f"user {args.name} deleted (instance {inst} kept; "
                 f"delete with: dsh-muctl instance del {inst} --purge)")
    else:
        info(f"user {args.name} deleted")


def create_instance(st: dict, name: str, user: str, cpu, mem, pids, port,
                    cpu_cores=None, mem_mb=None, pids_num=None) -> dict:
    if name in st["instances"]:
        die(f"instance {name} already exists")
    if cpu_cores:
        cpu = parse_cpu_max(cpu_cores)
    if mem_mb:
        mem = parse_mem_mb(mem_mb)
    if pids_num:
        pids = parse_pids_max(pids_num)
    if not os.path.isdir(TEMPLATE):
        die(f"template missing ({TEMPLATE}); run: dsh-muctl install")
    # The instance runs as - and owns its data with - this uid. `0` is jail-root
    # with every capability dropped; `auto` allocates a dedicated host uid.
    mode = st["config"].get("instance_run_uid", 0)
    uid = allocate_uid(st) if mode == "auto" else int(mode)
    iport = allocate_port(st, int(port) if port else None)

    base = f"{INSTANCES_DIR}/{name}"
    os.makedirs(base, exist_ok=True)
    info(f"creating instance {name}: copying template -> {base}/rootfs ...")
    rootfs = f"{base}/rootfs"
    if os.path.exists(rootfs):
        shutil.rmtree(rootfs)
    run(["cp", "-a", TEMPLATE, rootfs])
    os.makedirs(f"{base}/home", exist_ok=True)
    os.makedirs(f"{base}/workspace", exist_ok=True)
    inject_jail_user(rootfs, uid, uid)
    inst = {
        "name": name, "user": user, "port": iport, "uid": uid, "gid": uid,
        "cpu_max": (parse_cpu_max(cpu_cores) if cpu_cores else (cpu or st["config"]["default_cpu_max"])),
        "mem_max": (parse_mem_mb(mem_mb) if mem_mb else (mem or str(st["config"]["default_mem_max"]))),
        "mem_high": str(st["config"]["default_mem_high"]),
        "pids_max": (parse_pids_max(pids_num) if pids_num else (pids or str(st["config"]["default_pids_max"]))),
        "state": "stopped", "service": f"dsh-mu-{name}",
        "upstream": {"host": f"127.0.0.1:{iport}", "cookie": "", "minted_at": 0},
        "created_at": int(time.time()),
    }
    st["instances"][name] = inst
    seed_dsh_home(st, inst, f"{base}/home")
    os.chown(f"{base}/home", uid, uid)
    os.chown(f"{base}/workspace", uid, uid)
    save_state(st)
    sync_all(st)
    run(["/sbin/rc-update", "add", f"dsh-mu-{name}", "default"], check=False)
    size = run_out(f"du -sm {base}").split()[0]
    info(f"instance {name} created: port={iport} uid={uid} disk={size}MB")
    return inst


def cmd_instance_create(args):
    ensure_root()
    st = load_state()
    user = args.user or args.name
    if user not in st["users"]:
        die(f"no such user: {user} (create it first)")
    create_instance(st, args.name, user, args.cpu, args.mem, args.pids, args.port,
                    cpu_cores=getattr(args, "cpu_cores", None),
                    mem_mb=getattr(args, "mem_mb", None),
                    pids_num=getattr(args, "pids_num", None))
    if not getattr(args, "no_start", False):
        start_instance(st, args.name)


def cmd_instance_start(args):
    ensure_root()
    st = load_state()
    timeout = 0.0 if getattr(args, "no_wait", False) else 300.0
    for name in args.names:
        if name not in st["instances"]:
            die(f"no such instance: {name}")
        start_instance(st, name, timeout=timeout)
        info(f"instance {name} started (port {st['instances'][name]['port']})")


def cmd_instance_stop(args):
    ensure_root()
    st = load_state()
    for name in args.names:
        if name not in st["instances"]:
            die(f"no such instance: {name}")
        stop_instance(st, name)
        info(f"instance {name} stopped")


def cmd_instance_restart(args):
    ensure_root()
    st = load_state()
    for name in args.names:
        if name not in st["instances"]:
            die(f"no such instance: {name}")
    for name in args.names:
        stop_instance(st, name, mark=False)
        start_instance(st, name)
        info(f"instance {name} restarted (port {st['instances'][name]['port']})")


def cmd_instance_status(args):
    st = load_state()
    names = args.names or sorted(st["instances"])
    rows = [("INSTANCE", "USER", "PORT", "UID", "STATE", "SERVICE", "SIZE(MB)")]
    missing = []
    for name in names:
        inst = st["instances"].get(name)
        if not inst:
            missing.append(name)
            continue
        base = f"{INSTANCES_DIR}/{name}"
        size = run_out(f"du -sm {base}").split()[0] if os.path.isdir(base) else "-"
        rows.append((name, inst.get("user", "-"), str(inst["port"]), str(inst.get("uid", 0)),
                     instance_status(name),
                     "ok" if os.path.exists(f"/etc/init.d/dsh-mu-{name}") else "missing", size))
    if getattr(args, "json", False):
        keys = [k.lower().replace("(mb)", "_mb") for k in rows[0]]
        print(json.dumps([dict(zip(keys, r)) for r in rows[1:]], indent=2, ensure_ascii=False))
    else:
        width = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for r in rows:
            print("  ".join(c.ljust(width[i]) for i, c in enumerate(r)))
    # Asking about an instance that does not exist is an error: printing nothing
    # would look like success to a script.
    if missing:
        die("no such instance: " + ", ".join(missing))


def cmd_instance_list(args):
    """`instance list` is what people type; it is `status` without names."""
    return cmd_instance_status(argparse.Namespace(names=[], json=getattr(args, "json", False)))


def cmd_instance_del(args):
    ensure_root()
    st = load_state()
    if args.name not in st["instances"]:
        die(f"no such instance: {args.name}")
    stop_instance(st, args.name, mark=False)
    run(["/sbin/rc-update", "del", f"dsh-mu-{args.name}", "default"], check=False)
    for path in (f"/etc/init.d/dsh-mu-{args.name}", f"{INST_CONF_DIR}/{args.name}.conf",
                 f"{RUN_DIR}/dsh-mu-{args.name}.pid", f"/sys/fs/cgroup/dsh-mu/{args.name}"):
        if os.path.isdir(path) and not os.path.islink(path):
            try:
                os.rmdir(path)
            except OSError:
                pass
        elif os.path.exists(path):
            os.unlink(path)
    if args.purge:
        shutil.rmtree(f"{INSTANCES_DIR}/{args.name}", ignore_errors=True)
        info(f"instance {args.name} purged (data deleted)")
    del st["instances"][args.name]
    for u in st["users"].values():
        if u.get("instance") == args.name:
            u["instance"] = ""
    save_state(st)
    sync_all(st)
    info(f"instance {args.name} deleted" + ("" if args.purge else "; data kept under " + f"{INSTANCES_DIR}/{args.name}"))


def cmd_instance_logs(args):
    log = f"{LOGS}/{args.name}.log"
    if not os.path.exists(log):
        die(f"no log for {args.name} ({log})")
    if args.follow:
        os.execvp("tail", ["tail", "-n", str(args.lines), "-f", log])
    run(["tail", "-n", str(args.lines), log], check=False)


def cmd_instance_shell(args):
    ensure_root()
    st = load_state()
    if args.name not in st["instances"]:
        die(f"no such instance: {args.name}")
    os.execv(JAIL, [JAIL, args.name, "--shell"])


PLUGIN_PKG_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
PLUGIN_VER_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")


def plugins_catalog(st: dict) -> list[dict]:
    """The administrator's curated plugin list (plain data, no switches)."""
    return sorted(st.get("plugins_catalog") or [],
                  key=lambda e: str(e.get("pkg") or ""))


def instances_dir(name: str) -> str:
    return f"{INSTANCES_DIR}/{name}"


def instance_home(name: str) -> str:
    return f"{instances_dir(name)}/home"


def instance_profiles(name: str) -> list[str]:
    base = f"{instance_home(name)}/.dsh/profiles"
    try:
        return sorted(d for d in os.listdir(base)
                      if os.path.isdir(os.path.join(base, d)))
    except OSError:
        return []


def instance_profile(name: str) -> str:
    """The profile the instance actually boots (usually `web`)."""
    profiles = instance_profiles(name)
    for want in ("web", "tui"):
        if want in profiles:
            return want
    return profiles[0] if profiles else "web"


def instance_plugins(name: str) -> list[dict]:
    """Installed plugins for one instance, merged with our record of who added them."""
    profile = instance_profile(name)
    pkg_path = f"{instance_home(name)}/.dsh/profiles/{profile}/package.json"
    deps: dict[str, str] = {}
    try:
        doc = json.loads(read_text(pkg_path) or "{}")
        deps = {str(k): str(v) for k, v in (doc.get("dependencies") or {}).items()}
    except (OSError, ValueError):
        deps = {}
    st = load_state()
    recorded = {str(e.get("pkg")): e for e in
                ((st["instances"].get(name) or {}).get("plugins") or [])}
    curated = {str(e.get("pkg")) for e in plugins_catalog(st)}
    rows = []
    for pkg, version in sorted(deps.items()):
        rec = recorded.get(pkg) or {}
        rows.append({"pkg": pkg, "version": version,
                     "source": "curated" if pkg in curated else "self",
                     "by": rec.get("by", ""), "at": int(rec.get("at", 0))})
    return rows


def plugin_exec(name: str, verb: str, pkg: str, version: str | None = None) -> tuple[bool, str]:
    """Run `dsh plugin --profile <p> <verb> <pkg>[@ver]` inside the instance jail."""
    profile = instance_profile(name)
    spec = pkg if not version else f"{pkg}@{version}"
    cmd = f"dsh plugin --profile {profile} {verb} {spec}"
    info(f"running inside {name}: {cmd}")
    res = subprocess.run([JAIL, name, "--", cmd], capture_output=True, text=True, timeout=900)
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    return res.returncode == 0, out[-2000:]


def record_plugin(st: dict, name: str, pkg: str, version: str, source: str,
                  by: str, present: bool = True) -> None:
    inst = st["instances"].setdefault(name, {})
    rows = [e for e in (inst.get("plugins") or []) if str(e.get("pkg")) != pkg]
    if present:
        rows.append({"pkg": pkg, "version": version, "source": source,
                     "by": by, "at": int(time.time())})
    inst["plugins"] = rows
    save_state(st)


def replay_instance_plugins(name: str) -> list[str]:
    """Re-install the recorded plugins after a rebuild (records survive, rootfs does not)."""
    st = load_state()
    rows = (st["instances"].get(name) or {}).get("plugins") or []
    installed = []
    for row in rows:
        ok, out = plugin_exec(name, "add", str(row.get("pkg")),
                              str(row.get("version") or "") or None)
        if ok:
            installed.append(str(row.get("pkg")))
        else:
            info(f"could not replay {row.get('pkg')} for {name}: {out[:200]}")
    return installed


def cmd_plugin(args):
    """Maintain the curated catalog: the packages users may install without a password."""
    ensure_root()
    st = load_state()
    catalog = [dict(e) for e in (st.get("plugins_catalog") or [])]
    if args.action == "list":
        if getattr(args, "json", False):
            print(json.dumps({"plugins": plugins_catalog(st)}, indent=2, ensure_ascii=False))
            return 0
        rows = sorted(catalog, key=lambda e: str(e.get("pkg")))
        if not rows:
            info("the curated plugin list is empty (users may still ask an administrator "
                 "to install something, with the administrator password)")
            return 0
        print(f"{'PACKAGE':44} {'VERSION':12} NOTE")
        for e in rows:
            print(f"{str(e.get('pkg')):44} {str(e.get('version') or 'latest'):12} {e.get('note', '')}")
        return 0
    pkg = str(getattr(args, "pkg", "") or "").strip()
    if not pkg:
        die("plugin add/remove needs a package name")
    if not PLUGIN_PKG_RE.match(pkg):
        die(f"not a valid npm package name: {pkg}")
    version = str(getattr(args, "version", "") or "").strip()
    if version and not PLUGIN_VER_RE.match(version):
        die(f"not a valid version: {version}")
    with state_lock():
        st = load_state()
        catalog = [e for e in (st.get("plugins_catalog") or [])
                   if str(e.get("pkg")) != pkg]
        if args.action == "add":
            catalog.append({"pkg": pkg, "version": version,
                            "note": str(getattr(args, "note", "") or "").strip(),
                            "added_at": int(time.time())})
            st["plugins_catalog"] = catalog
            save_state(st)
            info(f"curated: {pkg}{'@' + version if version else ''}")
        else:
            if len(catalog) == len(st.get("plugins_catalog") or []):
                die(f"not in the catalog: {pkg}")
            st["plugins_catalog"] = catalog
            save_state(st)
            info(f"removed from the catalog: {pkg} (installed copies stay until removed)")
    return 0


def cmd_instance_plugin(args):
    """list | add | remove plugins inside one instance, recording who added what."""
    ensure_root()
    words = list(args.names)
    action = words[0] if words else "list"
    if action not in ("list", "add", "remove", "replay"):
        die("usage: dsh-muctl instance plugin list <name> | add|remove <name> <pkg>[@ver]")
    if len(words) < 2:
        die("which instance?")
    name = words[1]
    st = load_state()
    if name not in st["instances"]:
        die(f"no such instance: {name}")
    if action == "list":
        rows = instance_plugins(name)
        if getattr(args, "json", False):
            print(json.dumps({"instance": name, "plugins": rows}, indent=2, ensure_ascii=False))
            return 0
        if not rows:
            print("(no plugins installed in this instance)")
            return 0
        print(f"{'PACKAGE':44} {'VERSION':14} SOURCE")
        for r in rows:
            print(f"{r['pkg']:44} {r['version']:14} {r['source']}")
        return 0
    if action == "replay":
        got = replay_instance_plugins(name)
        info(f"replayed {len(got)} plugin(s) into {name}: {', '.join(got) or '(none recorded)'}")
        return 0
    if len(words) < 3:
        die("which package?")
    spec = words[2]
    pkg, _, version = spec.partition("@")
    if pkg.startswith("@"):                     # scoped: @scope/name@1.2.3
        pkg, _, version = spec.rsplit("@", 1) if spec.count("@") > 1 else (spec, "", "")
    if not PLUGIN_PKG_RE.match(pkg):
        die(f"not a valid npm package name: {pkg}")
    if version and not PLUGIN_VER_RE.match(version):
        die(f"not a valid version: {version}")
    ok, out = plugin_exec(name, "add" if action == "add" else "remove", pkg,
                          version or None)
    if not ok:
        die(f"dsh plugin {action} failed for {pkg}: {out[:400]}")
    curated = {str(e.get("pkg")) for e in plugins_catalog(st)}
    record_plugin(load_state(), name, pkg,
                  version or _installed_version(name, pkg),
                  "curated" if pkg in curated else "self", "cli",
                  present=(action == "add"))
    info(f"{'installed' if action == 'add' else 'removed'} {pkg} in {name}; "
         f"restart the instance to apply")
    return 0


def _installed_version(name: str, pkg: str) -> str:
    for row in instance_plugins(name):
        if row["pkg"] == pkg:
            return row["version"]
    return ""


def cmd_instance_exec(args):
    """Run a command inside an instance's jail and pass its output through.

    Useful for support work ("what does the jail actually see?") without opening
    an interactive shell. The exit status of the command is what this returns.
    """
    ensure_root()
    st = load_state()
    if args.name not in st["instances"]:
        die(f"no such instance: {args.name}")
    command = " ".join(args.command)
    if not command.strip():
        die("nothing to run (usage: dsh-muctl instance exec <name> -- <command>)")
    audit_write("instance.exec", target=args.name,
                detail={"cmd": sanitize_argv()})
    return subprocess.call([JAIL, args.name, "--", command])


def write_instance_api_key(st: dict, name: str, key: str | None) -> str:
    """Insert, replace or remove `refs.DEEPSEEK_API_KEY` in an instance's store.

    The file is the DSH credential store; the browser-session record in it must
    survive byte-for-byte, so this only touches the key line.
    """
    # One rule for every entrance: the page already refused short keys while the
    # CLI wrote them, so the same call produced different results depending on how
    # it was made. A one-character key is a configuration mistake worth catching
    # before it reaches the instance.
    if key is not None and len(str(key)) < 8:
        die(f"the API key looks too short ({len(str(key))} characters; at least 8)")
    inst = st["instances"][name]
    path = f"{INSTANCES_DIR}/{name}/home/.dsh/.credentials.yaml"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = ""
    if os.path.exists(path):
        with open(path) as fh:
            text = fh.read()
    lines = text.splitlines() or ["version: 1"]
    has_key_line = any(re.match(r"^\s*DEEPSEEK_API_KEY:", line) for line in lines)
    out: list[str] = []
    inserted = False
    for line in lines:
        if re.match(r"^\s*DEEPSEEK_API_KEY:", line):
            if key and not inserted:
                out.append(f"  DEEPSEEK_API_KEY: {key}")
                inserted = True
            continue
        out.append(line)
        if key and not has_key_line and not inserted and re.match(r"^refs:\s*$", line):
            out.append(f"  DEEPSEEK_API_KEY: {key}")
            inserted = True
    if key and not inserted:
        insert_at = 1 if out and out[0].startswith("version:") else 0
        out[insert_at:insert_at] = ["refs:", f"  DEEPSEEK_API_KEY: {key}"]
    atomic_write(path, "\n".join(out).rstrip("\n") + "\n", mode=0o600)
    uid = int(inst.get("uid", 0))
    os.chown(path, uid, uid)
    return path


def cmd_instance_api_key(args):
    ensure_root()
    st = load_state()
    if args.name not in st["instances"]:
        die(f"no such instance: {args.name}")
    key = None if args.remove else (args.key or st["config"].get("model_api_key"))
    if not key and not args.remove:
        die("no key given and config model_api_key is empty")
    path = write_instance_api_key(st, args.name, key)
    # The secret itself stays in the 0600 credential file, but the *fact* that an
    # instance has one must be recorded: without this the change never reached
    # state.json, so it produced no audit record at all (and the audit
    # fingerprint's credential component could never become true).
    inst = st["instances"][args.name]
    marker = dict(inst.get("credentials") or {})
    if args.remove or not key:
        marker.pop("api_key", None)
    else:
        marker["api_key"] = True
    inst["credentials"] = marker
    inst["updated_at"] = int(time.time())
    save_state(st)
    sync_all(st)
    info(f"instance {args.name}: model API key {'removed from' if args.remove else 'written to'} {path}")
    info(f"restart to apply: dsh-muctl instance restart {args.name}")


# ------------------------------------------------------------------- providers
#
# DSH's DeepSeek plugin speaks the Anthropic Messages protocol and takes a
# configurable baseURL plus a model catalog, so any Messages-compatible endpoint
# can serve an instance. The override is delivered as a launcher `--patch` file
# (not the DSH_HOME user layer, which DSH itself owns) so it survives plugin
# installs and profile reboots.

PROVIDER_PRESETS: dict[str, dict] = {
    "deepseek": {
        "kind": "deepseek",
        "base_url": None,  # DSH default: https://api.deepseek.com/anthropic
        "credentials_ref": "DEEPSEEK_API_KEY",
        "model": "deepseek-flash",
        # Reasoning levels for DeepSeek's *official* endpoint, verified against the
        # live API (2026-10-20):
        #   output_config.effort accepts none|minimal|low|medium|high|max
        #   `none` answers with text only (thinking off); the rest return thinking
        #   `off` is NOT a DeepSeek variant (422) -- that is why the wire value for
        #   the "off" level below is `none`.
        # Models come from https://api.deepseek.com/models: deepseek-flash,
        # deepseek-v4-pro.
        "models": [
            {
                "id": "deepseek-flash",
                "name": "DeepSeek V4.1 Flash",
                "reasoningEfforts": {"off": "none", "low": "low", "medium": "medium",
                                     "high": "high", "max": "max"},
            },
            {
                "id": "deepseek-v4-pro",
                "name": "DeepSeek V4 Pro",
                "reasoningEfforts": {"off": "none", "low": "low", "medium": "medium",
                                     "high": "high", "max": "max"},
            },
        ],
    },
    # Kilo AI Gateway: a public "free" channel that needs NO account and does not
    # check an API key on the free tier. Verified 2026-10-20 against
    # https://api.kilo.ai/api/gateway:
    #   * keyless works; sending an INVALID key returns 401, so we must send none
    #   * tools/tool_calls work (finish_reason=tool_calls), SSE streaming works
    #   * reasoning_effort accepts none/minimal/low/medium/high/max
    # credentials_ref is None on purpose: pi-ai treats a profile without apiKeyEnv
    # as "configured but keyless".
    "kilo": {
        "kind": "pi-ai",
        "route": "kilo",
        "api": "openai-completions",
        "base_url": "https://api.kilo.ai/api/gateway",
        "credentials_ref": None,
        "model": "kilo-auto/free",
        "models": [
            {
                "id": "kilo-auto/free",
                "name": "Kilo Auto (free)",
                "reasoningEfforts": {"off": "none", "low": "low", "medium": "medium",
                                     "high": "high", "max": "max"},
            },
        ],
    },
    # opencode.ai serves several products on different protocols. The *Go*
    # subscription (product=go) answers only OpenAI Chat Completions, while the
    # Anthropic Messages endpoint belongs to the standard product and bills
    # separately. route "opencode-go" therefore goes through dsh-llm-pi-ai.
    "opencode-go": {
        "kind": "pi-ai",
        "route": "opencode-go",
        "api": "openai-completions",
        "base_url": "https://opencode.ai/inference/go/v1",
        "credentials_ref": "DEEPSEEK_API_KEY",
        "session_header": True,
        "model": "deepseek-v4.1-flash",
        # Only models that OpenCode Go serves on /go/v1/chat/completions: the
        # Go catalog splits models across chat/completions, /messages and
        # /responses, and one route speaks exactly one protocol.
        "models": [
            {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "glm-5.3", "name": "GLM 5.3 (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "kimi-k3", "name": "Kimi K3 (opencode go)",
             "contextWindow": 262144, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "kimi-k2.7-code", "name": "Kimi K2.7 Code (opencode go)",
             "contextWindow": 262144, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "minimax-m2.7", "name": "MiniMax M2.7 (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "longcat-2.0", "name": "LongCat 2.0 (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "mimo-v2.6-flash", "name": "MiMo V2.6 Flash (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "space-bunny", "name": "Space Bunny (opencode go)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "longcat-2.5-preview-free", "name": "LongCat 2.5 Preview (free)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "step-5-preview-free", "name": "Step 5 Preview (free)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
        ],
    },
    # Same host, Messages protocol, standard product (needs a funded balance).
    "opencode": {
        "kind": "deepseek",
        "base_url": "https://opencode.ai/inference/anthropic",
        "credentials_ref": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4.1-flash",
        "models": [
            {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash (opencode)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text", "image"]},
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro (opencode)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "glm-5.3", "name": "GLM 5.3 (opencode)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "kimi-k3", "name": "Kimi K3 (opencode)",
             "contextWindow": 262144, "maxTokens": 32768, "inputModalities": ["text", "image"]},
            {"id": "minimax-m3", "name": "MiniMax M3 (opencode)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
            {"id": "qwen3.8-flash", "name": "Qwen 3.8 Flash (opencode)",
             "contextWindow": 131072, "maxTokens": 32768, "inputModalities": ["text"]},
        ],
    },
}


def load_custom_providers() -> dict:
    """Operator-defined providers from /etc/dsh-mu/providers.json."""
    text = read_text(PROVIDERS_PATH)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        info(f"warning: ignoring malformed {PROVIDERS_PATH}: {exc}")
        return {}
    providers = data if isinstance(data, dict) else {}
    global _PROVIDER_SNAPSHOT
    _PROVIDER_SNAPSHOT = {name: json.dumps(defn, sort_keys=True)[:400]
                          for name, defn in providers.items()}
    return providers


def save_custom_providers(providers: dict):
    """Write the provider catalogue and audit which definitions changed."""
    global _PROVIDER_SNAPSHOT
    if os.environ.get("DSHMU_BACKEND"):
        atomic_write(PROVIDERS_PATH, json.dumps(providers, indent=2, sort_keys=True),
                     mode=0o600, group=nginx_group())
        return
    fingerprint = {name: json.dumps(defn, sort_keys=True)[:400]
                   for name, defn in providers.items()}
    if _PROVIDER_SNAPSHOT is not None and fingerprint != _PROVIDER_SNAPSHOT:
        summary = _diff_entities(_PROVIDER_SNAPSHOT, fingerprint, lambda x: x)
        audit_write("provider.change", target="providers.json",
                    detail={"cmd": sanitize_argv(), "providers": summary})
    _PROVIDER_SNAPSHOT = fingerprint
    atomic_write(PROVIDERS_PATH, json.dumps(providers, indent=2, sort_keys=True),
                 mode=0o600, group=nginx_group())


def all_providers() -> dict:
    """Built-in presets overlaid with operator definitions (custom wins)."""
    merged = {name: json.loads(json.dumps(defn)) for name, defn in PROVIDER_PRESETS.items()}
    for name, defn in load_custom_providers().items():
        base = merged.get(name, {})
        merged[name] = {**base, **defn, "custom": True}
    return merged


def normalize_provider(name: str, defn: dict) -> dict:
    """Validate and fill a provider definition (used by the API and the CLI)."""
    if not re.match(r"^[\w.-]+$", name or ""):
        die("provider name may only contain letters, digits, dot, dash and underscore")
    kind = defn.get("kind") or ("pi-ai" if defn.get("route") else "deepseek")
    if kind not in ("pi-ai", "deepseek"):
        die(f"provider kind must be 'pi-ai' (OpenAI Chat Completions) or 'deepseek' (Anthropic Messages), got {kind!r}")
    out = {
        "kind": kind,
        "base_url": defn.get("base_url") or None,
        "credentials_ref": defn.get("credentials_ref") or "DEEPSEEK_API_KEY",
        "model": defn.get("model") or "",
        "models": [],
        "display_name": defn.get("display_name") or name,
    }
    if kind == "pi-ai":
        route = defn.get("route") or name
        out["route"] = route
        out["api"] = defn.get("api") or "openai-completions"
        out["session_header"] = bool(defn.get("session_header"))
    headers = defn.get("headers") or {}
    if headers:
        if not isinstance(headers, dict):
            die("provider headers must be an object")
        out["headers"] = {str(k): str(v) for k, v in headers.items()}
    models = defn.get("models") or []
    if isinstance(models, str):
        models = parse_model_lines(models)
    for model in models:
        if isinstance(model, str):
            model = {"id": model}
        if not isinstance(model, dict) or not model.get("id"):
            die("each model needs at least an id")
        entry = {"id": str(model["id"])}
        for key in ("name", "contextWindow", "maxTokens", "inputModalities"):
            if model.get(key):
                entry[key] = model[key]
        # Reasoning levels: a dict maps level -> wire spelling, False means "not a
        # reasoning model" (pi-ai treats an omitted field as "keep the catalog's").
        efforts = model.get("reasoningEfforts", model.get("reasoning_efforts"))
        if efforts is not None:
            entry["reasoningEfforts"] = efforts if isinstance(efforts, (dict, bool)) \
                else parse_efforts(str(efforts))
        out["models"].append(entry)
    if not out["model"]:
        out["model"] = out["models"][0]["id"] if out["models"] else "deepseek-flash"
    return out


def parse_efforts(value: str):
    """Parse a reasoning-effort spec into pi-ai's `reasoningEfforts` shape.

    `off,high`            -> {"off": "", "high": "high"}   (level name == wire)
    `off:off,high:ultra`  -> {"off": "off", "high": "ultra"} (rename the wire spelling)
    `false` / `none` / `-` -> False  (declare a non-reasoning model)
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.lower() in ("false", "none", "-", "no"):
        return False
    out: dict[str, str] = {}
    for piece in text.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        level, sep, wire = piece.partition(":")
        level = level.strip()
        if not level:
            continue
        out[level] = wire.strip() if sep else level
    return out or None


def parse_model_lines(text: str) -> list[dict]:
    """`id | name | contextWindow | maxTokens | efforts` per line (admin UI).

    The optional 5th field declares the model's thinking levels; see parse_efforts.
    """
    models = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        model: dict = {"id": parts[0]}
        for key, value in zip(("name", "contextWindow", "maxTokens"), parts[1:]):
            if not value:
                continue
            model[key] = int(value) if key in ("contextWindow", "maxTokens") and value.isdigit() else value
        if len(parts) > 4 and parts[4]:
            efforts = parse_efforts(parts[4])
            if efforts is not None:
                model["reasoningEfforts"] = efforts
        models.append(model)
    return models


def provider_patch_yaml(prov: dict, instance: str = "") -> str:
    """Render the instance's provider override as a DSH patch-list overlay."""
    lines = [
        "# generated by dsh-muctl - instance model provider override",
        "# applied as a launcher --patch layer, after every bundle layer",
    ]
    models = prov.get("models") or []
    if prov.get("kind") == "pi-ai":
        route = prov["route"]
        lines += [
            "- id: llm-pi-ai",
            "  config:",
            "    providers:",
            f"      {route}:",
            f'        displayName: {json.dumps(prov.get("display_name") or ("OpenCode Go (" + route + ")"))}',
            f'        apiKeyEnv: {prov["credentials_ref"]}',
            f'        api: {prov["api"]}',
            f'        baseURL: {json.dumps(prov["base_url"])}',
        ]
        headers = dict(prov.get("headers") or {})
        if prov.get("session_header"):
            # OpenCode Go requires a stable per-conversation session id; the
            # instance name is stable for the life of the instance.
            headers["x-opencode-session"] = f"dsh-mu-{instance}" if instance else "dsh-mu"
        if headers:
            lines.append("        headers:")
            for header, value in headers.items():
                lines.append(f'          {header}: {json.dumps(value)}')
        if models:
            lines.append("        models:")
            for m in models:
                lines.append(f'          - id: {json.dumps(m["id"])}')
                for key, val in (("name", m.get("name")), ("contextWindow", m.get("contextWindow")),
                                 ("maxTokens", m.get("maxTokens"))):
                    if val:
                        lines.append(f'            {key}: {json.dumps(val) if isinstance(val, str) else val}')
        default_provider = route
    else:
        lines += ["- id: llm-deepseek", "  config:"]
        if prov.get("base_url"):
            lines.append(f'    baseURL: {json.dumps(prov["base_url"])}')
        if models:
            lines.append("    models:")
            for m in models:
                lines.append(f'      - id: {json.dumps(m["id"])}')
                if m.get("name"):
                    lines.append(f'        name: {json.dumps(m["name"])}')
                if m.get("contextWindow"):
                    lines.append(f'        contextWindow: {int(m["contextWindow"])}')
                if m.get("maxTokens"):
                    lines.append(f'        maxTokens: {int(m["maxTokens"])}')
                modalities = m.get("inputModalities")
                if modalities:
                    lines.append("        inputModalities: [" + ", ".join(modalities) + "]")
        default_provider = "deepseek-official"
    lines += [
        "- id: agent-default-model",
        "  config:",
        f'    provider: {json.dumps(default_provider)}',
        f'    model: {json.dumps(prov["model"])}',
        "",
    ]
    return "\n".join(lines)


def read_instance_api_key(st: dict, name: str) -> str | None:
    """The instance's own credential, if its store has one."""
    path = f"{INSTANCES_DIR}/{name}/home/.dsh/.credentials.yaml"
    text = read_text(path) or ""
    match = re.search(r"^\s*([A-Z0-9_]+):\s*(\S+)\s*$", text, re.M)
    for line in text.splitlines():
        hit = re.match(r"^\s*DEEPSEEK_API_KEY:\s*(\S+)\s*$", line)
        if hit:
            return hit.group(1)
    return None


def provider_credentials(st: dict, name: str, ref: str = "DEEPSEEK_API_KEY") -> tuple[str | None, str]:
    """Resolve the key for an instance: instance store first, then platform."""
    own = read_instance_api_key(st, name)
    if own:
        return own, "instance"
    platform = str(st["config"].get("model_api_key") or "")
    if platform:
        return platform, "platform"
    return None, "none"


def provider_probe(prov: dict, key: str | None, model: str | None = None,
                   timeout: float = 25.0) -> dict:
    """Send one tiny completion to the provider and report what came back.

    Never includes the credential in the result.
    """
    model = model or prov.get("model") or ""
    if not model:
        return {"ok": False, "status": 0, "detail": "no model selected", "endpoint": ""}
    if prov.get("kind") == "pi-ai":
        base = (prov.get("base_url") or "").rstrip("/")
        endpoint = f"{base}/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
                   "max_tokens": 4, "stream": False}
        headers = {"Content-Type": "application/json", "User-Agent": f"dsh-mu/{VERSION}"}
        headers.update(prov.get("headers") or {})
        if prov.get("session_header"):
            headers["x-opencode-session"] = "dsh-mu-probe"
        if key:
            headers["Authorization"] = f"Bearer {key}"
    else:
        base = (prov.get("base_url") or "https://api.deepseek.com/anthropic").rstrip("/")
        endpoint = f"{base}/v1/messages"
        payload = {"model": model, "max_tokens": 4,
                   "messages": [{"role": "user", "content": "ping"}]}
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                   "User-Agent": f"dsh-mu/{VERSION}"}
        headers.update(prov.get("headers") or {})
        if key:
            headers["x-api-key"] = key
    started = time.time()
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read(2000).decode(errors="replace")
            status = res.status
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode(errors="replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - the probe reports any failure
        return {"ok": False, "status": 0, "endpoint": endpoint, "model": model,
                "ms": int((time.time() - started) * 1000), "detail": f"{type(exc).__name__}: {exc}"}
    ms = int((time.time() - started) * 1000)
    detail = ""
    replied = False
    if 200 <= status < 300:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {}
        choices = parsed.get("choices") or parsed.get("content") or []
        replied = bool(choices)
        # A 2xx is not enough: an endpoint that answers 200 with something that is
        # not a model reply is broken, and colouring it green would send the
        # administrator looking in the wrong place.
        if replied:
            detail = "reply ok"
        else:
            snippet = re.sub(r"\s+", " ", body)[:120] or "empty body"
            detail = f"HTTP 200 but the body carries no model reply ({snippet})"
    else:
        detail = re.sub(r"\s+", " ", body)[:300] or f"HTTP {status}"
    return {"ok": 200 <= status < 300 and replied, "status": status, "endpoint": endpoint,
            "model": model, "ms": ms, "detail": detail}


def resolved_provider(st: dict, name: str) -> dict:
    """What the instance will actually use, plus where its credentials come from."""
    inst = st["instances"].get(name) or die(f"no such instance: {name}")
    prov = inst.get("provider")
    if not prov:
        prov = json.loads(json.dumps(PROVIDER_PRESETS["deepseek"]))
        prov["preset"] = "deepseek"
        prov["implicit"] = True
    key, source = provider_credentials(st, name, prov.get("credentials_ref", "DEEPSEEK_API_KEY"))
    patch_path = f"{INSTANCES_DIR}/{name}/home/.dsh-mu-provider.yml"
    return {
        "provider": prov,
        "key_source": source,
        "key_set": bool(key),
        "key_length": len(key) if key else 0,
        "patch_path": patch_path,
        "patch": read_text(patch_path) or "",
    }


def write_provider(st: dict, name: str, preset: str, model: str | None = None) -> dict:
    catalog = all_providers()
    if preset not in catalog:
        die(f"unknown provider preset: {preset} (have: {', '.join(sorted(catalog))})")
    inst = st["instances"][name]
    prov = json.loads(json.dumps(catalog[preset]))  # deep copy
    prov.pop("custom", None)
    prov["preset"] = preset
    if model:
        known = {m["id"] for m in prov.get("models") or []}
        if prov.get("models") and model not in known:
            die(f"model {model} is not in the {preset} catalog ({', '.join(sorted(known))})")
        prov["model"] = model
    inst["provider"] = prov
    patch_path = f"{INSTANCES_DIR}/{name}/home/.dsh-mu-provider.yml"
    atomic_write(patch_path, provider_patch_yaml(prov, name), mode=0o644)
    uid = int(inst.get("uid", 0))
    os.chown(patch_path, uid, uid)
    save_state(st)
    return prov


def cmd_instance_provider(args):
    ensure_root()
    st = load_state()
    inst = st["instances"].get(args.name) or die(f"no such instance: {args.name}")
    if args.preset == "show":
        print(json.dumps(inst.get("provider") or {"preset": "deepseek", "note": "DSH default"},
                         indent=2, sort_keys=True))
        return
    if args.preset == "clear":
        inst.pop("provider", None)
        patch = f"{INSTANCES_DIR}/{args.name}/home/.dsh-mu-provider.yml"
        if os.path.exists(patch):
            os.unlink(patch)
        save_state(st)
        sync_all(st)
        info(f"instance {args.name}: provider override removed; restart to apply")
        return
    key = None
    if args.key_file:
        with open(args.key_file) as fh:
            key = fh.read().strip()
    elif args.key:
        key = args.key
    if args.preset == "opencode-go" and not key and "provider" not in inst:
        info("note: no API key supplied; the credential store must already hold the opencode key")
    prov = write_provider(st, args.name, args.preset, args.model)
    if key:
        write_instance_api_key(st, args.name, key)
    sync_all(st)
    info(f"instance {args.name}: provider={args.preset} base_url={prov.get('base_url') or 'DSH default'} "
         f"default_model={prov['model']} catalog={len(prov.get('models') or [])}")
    info(f"restart to apply: dsh-muctl instance restart {args.name}")


def cmd_instance_owner(args):
    """Show or change the uid an instance runs as (and re-own its data)."""
    ensure_root()
    st = load_state()
    inst = st["instances"].get(args.name) or die(f"no such instance: {args.name}")
    if args.uid is not None:
        inst["uid"] = int(args.uid)
        inst["gid"] = int(args.uid)
        save_state(st)
        sync_all(st)
    ensure_owner(st, args.name)
    info(f"instance {args.name}: runs as uid {inst['uid']} "
         f"(0 = jail-root with capabilities dropped)")


UNLIMITED = {"max", "none", "unlimited", "no", "不限", "-"}


def parse_cpu_max(value) -> str:
    """`2` / `2.5` (cores) / `max` / a raw `quota period` string -> cpu.max value."""
    if value is None:
        die("cpu value is required")
    raw = str(value).strip()
    if raw.lower() in UNLIMITED:
        return "max"
    if " " in raw:
        return raw                      # already a raw cgroup cpu.max
    try:
        cores = float(raw)
    except ValueError:
        die(f"cpu must be a number of cores or 'max', got {value!r}")
    if cores < 0:
        die(f"cpu must not be negative (use 0 or 'max' for unlimited), got {value!r}")
    if cores == 0:
        return "max"
    return f"{int(cores * 100000)} 100000"


def parse_mem_max(value) -> str:
    """`2048` / `2G` / `512M` / `max` / raw bytes -> memory.max value."""
    if value is None:
        die("memory value is required")
    raw = str(value).strip()
    if raw.lower() in UNLIMITED:
        return "max"
    mult = 1
    suffix = raw[-1:].lower()
    if suffix in ("k", "m", "g", "t"):
        mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[suffix]
        raw = raw[:-1]
    try:
        amount = int(float(raw) * mult)
    except ValueError:
        die(f"memory must be bytes, or use K/M/G/T or 'max', got {value!r}")
    return str(amount) if amount > 0 else "max"


def parse_pids_max(value) -> str:
    if value is None:
        die("pids value is required")
    raw = str(value).strip()
    if raw.lower() in UNLIMITED:
        return "max"
    try:
        count = int(raw)
    except ValueError:
        die(f"pids must be an integer or 'max', got {value!r}")
    if count < 0:
        die(f"pids must not be negative (use 0 or 'max' for unlimited), got {value!r}")
    return str(count) if count > 0 else "max"


def parse_mem_mb(value) -> str:
    """Like parse_mem_max, but a bare number means *megabytes* (the UI's unit)."""
    if value is None:
        die("memory value is required")
    raw = str(value).strip()
    if raw.lower() in UNLIMITED:
        return "max"
    if raw[-1:].lower() in ("k", "m", "g", "t"):
        return parse_mem_max(raw)       # an explicit unit always wins
    try:
        mb = float(raw)
    except ValueError:
        die(f"memory must be megabytes (or 512M/2G/'max'), got {value!r}")
    if mb < 0:
        # `0` is the documented spelling of "unlimited"; a negative number is a
        # typo, and silently removing the limit is the dangerous reading of it.
        die(f"memory must not be negative (use 0 or 'max' for unlimited), got {value!r}")
    return parse_mem_max(f"{mb}M") if mb > 0 else "max"


def describe_cpu_max(value: str | None) -> str:
    if not value or str(value).strip().lower() == "max":
        return "不限"
    raw = str(value).split()
    try:
        quota = int(raw[0])
        period = int(raw[1]) if len(raw) > 1 else 100000
    except (ValueError, IndexError):
        return str(value)
    cores = quota / period
    return f"{cores:g} 核" if cores else "不限"


def describe_mem_max(value: str | None) -> str:
    if not value or str(value).strip().lower() == "max":
        return "不限"
    try:
        amount = int(value)
    except ValueError:
        return str(value)
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if amount >= size:
            return f"{amount / size:.1f} {unit}".replace(".0 ", " ")
    return f"{amount} B"


def describe_pids_max(value: str | None) -> str:
    if not value or str(value).strip().lower() == "max":
        return "不限"
    return str(value)


def human_limits(inst: dict) -> dict:
    return {"cpu": describe_cpu_max(inst.get("cpu_max")),
            "memory": describe_mem_max(inst.get("mem_max")),
            "pids": describe_pids_max(inst.get("pids_max"))}


def apply_limits_live(inst: dict) -> list[str]:
    """Write the limits into a running instance's cgroup (they apply immediately).

    cgroup v2 accepts raising and lowering cpu.max / memory.max / pids.max on a
    populated cgroup, so no restart is needed when the instance is up.
    """
    path = f"/sys/fs/cgroup/dsh-mu/{inst.get('name', '')}"
    applied = []
    if not os.path.isdir(path):
        return applied
    for field, key in (("cpu.max", "cpu_max"), ("memory.max", "mem_max"), ("pids.max", "pids_max")):
        value = str(inst.get(key) or "").strip()
        if not value:
            continue
        try:
            with open(f"{path}/{field}", "w") as fh:
                fh.write(value)
            applied.append(f"{field}={value}")
        except OSError:
            continue
    return applied


def validate_raw_limits(updates: dict) -> None:
    """Reject nonsense in the raw cgroup-valued flags (`--cpu`, `--mem`, `--pids`).

    A typo used to be stored verbatim and only fail later when written into the
    cgroup, so validate the pass-through forms here.
    """
    patterns = {
        "cpu_max": r"^(max|\d+\s+\d+)$",
        "mem_max": r"^(max|\d+)$",
        "mem_high": r"^(max|\d+)$",
        "pids_max": r"^(max|\d+)$",
    }
    for key, value in updates.items():
        if value is None:
            continue
        raw = str(value).strip()
        if not re.match(patterns[key], raw):
            die(f"{key.replace('_max', '').replace('_high', '')}: invalid value {raw!r} "
                f"(use a number, 'max', or the raw cgroup form like '200000 100000')")
        # These flags are the *raw* cgroup forms, so 0 means what the kernel means by
        # 0 - a zero-byte memory limit or a process limit of none. That kills the
        # instance instead of protecting it; "unlimited" is spelled `max` here (the
        # friendly --mem-mb/--pids-num forms map 0 to `max` for the UI).
        if raw == "0":
            die(f"{key.replace('_max', '').replace('_high', '')}: 0 is not 'unlimited' in the "
                f"raw cgroup form (it allows nothing) - use 'max', or the --mem-mb/--pids-num "
                f"form where 0 means unlimited")


def cmd_instance_limits(args):
    ensure_root()
    st = load_state()
    inst = st["instances"].get(args.name) or die(f"no such instance: {args.name}")
    updates = {
        "cpu_max": args.cpu if args.cpu is not None else (
            parse_cpu_max(args.cpu_cores) if args.cpu_cores is not None else None),
        "mem_max": args.mem if args.mem is not None else (
            parse_mem_mb(args.mem_mb) if args.mem_mb is not None else None),
        "pids_max": args.pids if args.pids is not None else (
            parse_pids_max(args.pids_num) if args.pids_num is not None else None),
    }
    validate_raw_limits(updates)
    changed = False
    for key, val in updates.items():
        if val is not None:
            inst[key] = str(val)
            changed = True
    if not changed or args.show:
        payload = {k: inst.get(k) for k in ("cpu_max", "mem_max", "mem_high", "pids_max")}
        payload["human"] = human_limits(inst)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if not changed:
            info("nothing changed (pass --cpu-cores/--mem-mb/--pids-num, or raw --cpu/--mem/--pids)")
        return
    save_state(st)
    sync_all(st)
    applied = apply_limits_live(dict(inst, name=args.name))
    if applied:
        info(f"limits updated for {args.name} and applied immediately: {', '.join(applied)}")
    else:
        info(f"limits updated for {args.name}; they apply on the next start "
             f"(instance is not running with a cgroup here)")


def service_started(name: str) -> bool:
    """Whether the instance's OpenRC service (supervise-daemon) is up.

    The harness needs a few seconds to bind its port after the service starts, so
    "service started" is the right signal for "should be running" during boot.
    """
    script = f"/etc/init.d/dsh-mu-{name}"
    if not os.path.exists(script):
        return False
    res = run(["/sbin/rc-service", f"dsh-mu-{name}", "status"], check=False, capture=True)
    text = ((res.stdout or "") + (res.stderr or "")).lower()
    return "started" in text and "stopped" not in text


def wait_for_port(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_in_use(port):
            return True
        time.sleep(1.5)
    return port_in_use(port)


def change_instance_port(st: dict, name: str, want: int | None, restart: bool = False,
                         dry_run: bool = False) -> dict:
    """Move an instance to another loopback port (validated, conflict-checked).

    The port is bound when the harness starts, so a change needs a restart: the
    caller decides whether to do it now (`restart`) or on the next start.
    """
    inst = st["instances"].get(name) or die(f"no such instance: {name}")
    current = int(inst["port"])
    lo, hi = (int(x) for x in st["config"]["port_range"])
    if want is None:
        want = allocate_port(st)
    want = int(want)
    if want == current:
        return {"instance": name, "port": current, "changed": False,
                "detail": "already on this port"}
    if not (lo <= want <= hi):
        die(f"port {want} is outside the configured range {lo}-{hi} "
            f"(change PORT_RANGE in dsh-mu.conf if you really need it)")
    for other, info in st["instances"].items():
        if other != name and int(info.get("port", 0)) == want:
            die(f"port {want} is already assigned to instance {other}")
    if port_in_use(want):
        die(f"port {want} is already in use by another process on this host")
    if dry_run:
        return {"instance": name, "from": current, "to": want, "changed": True,
                "detail": "dry run"}
    inst["port"] = want
    inst["upstream"] = inst.get("upstream") or {}
    save_state(st)
    sync_all(st)
    restart_required = port_in_use(current) or service_started(name)
    listening = False
    if restart and restart_required:
        run(["/sbin/rc-service", f"dsh-mu-{name}", "restart"], check=False)
        listening = wait_for_port(want)
    if restart or listening:
        fresh = load_state()
        fresh["instances"][name]["state"] = "running" if (listening or restart_required) else "stopped"
        save_state(fresh)
        render_gateway(fresh)
    return {"instance": name, "from": current, "to": want, "changed": True,
            "restarted": bool(restart), "listening": listening,
            "previous_port_listening": restart_required,
            "detail": ("restarted and listening on the new port" if listening
                       else ("restarted on the new port" if restart else "restart to apply"))}


def cmd_instance_port(args):
    ensure_root()
    st = load_state()
    if args.name not in st["instances"]:
        die(f"no such instance: {args.name}")
    want = args.port
    if want is None and not args.auto:
        result = {"instance": args.name, "port": int(st["instances"][args.name]["port"])}
        print(json.dumps(result, indent=2))
        info("pass --port N (or --auto) to move the instance")
        return 0
    result = change_instance_port(st, args.name, want, restart=args.restart,
                                  dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if result.get("changed"):
            info(f"instance {args.name}: port {result['from']} -> {result['to']}"
                 + (" (restarted)" if result.get("restarted") else "; restart the instance to apply"))
        else:
            info(f"instance {args.name}: {result['detail']}")
    return 0


def cmd_instance_refresh_cookie(args):
    ensure_root()
    st = load_state()
    for name in args.names:
        if name not in st["instances"]:
            die(f"no such instance: {name}")
        refresh_cookie(st, name)


def cmd_gateway(args):
    ensure_root()
    st = load_state()
    if args.action in ("build", "start", "stop", "restart", "conf", "logs", "jail-status"):
        return cmd_gateway_jail(args)
    if args.action == "install":
        return cmd_gateway_install(args)
    if args.action == "reload":
        sync_all(st)
        res = run(["/usr/sbin/nginx", "-t"], check=False, capture=True)
        if res.returncode != 0:
            die(f"nginx config invalid:\n{res.stderr}")
        run(["/sbin/rc-service", "openresty", "reload"], check=False)
        info("gateway configuration reloaded")
    elif args.action == "status":
        res = run(["/sbin/rc-service", "openresty", "status"], check=False, capture=True)
        print(((res.stdout or "") + (res.stderr or "")).strip())
        print(f"users={len(st['users'])} instances={len(st['instances'])} "
              f"running={sum(1 for i in st['instances'].values() if i.get('state') == 'running')}")
        for name, inst in sorted(st["instances"].items()):
            cookie = inst.get("upstream", {}).get("cookie", "")
            print(f"  {name:16s} port={inst['port']:5d} state={inst.get('state','?'):8s} "
                  f"cookie={'yes' if cookie else 'NO'}  live={instance_status(name)}")


def cmd_sync(args):
    ensure_root()
    st = load_state()
    sync_all(st)
    info("gateway views and instance configs re-rendered")


# ------------------------------------------------------------------- log files
#
# Instance logs are written by supervise-daemon, which keeps the file descriptor
# open for the life of the service, so rotation must be copytruncate rather than
# rename. Alpine's logrotate (installed with the platform) drives the schedule;
# `dsh-muctl logs rotate` is the dependency-free fallback and emergency tool.

LOGROTATE_CONF_DIR = "/etc/logrotate.d"
LOGROTATE_CONF = f"{LOGROTATE_CONF_DIR}/dsh-mu"
DEFAULT_LOG_MAX_MB = 16
DEFAULT_LOG_KEEP = 4


def render_logrotate_conf(max_mb: int = DEFAULT_LOG_MAX_MB, keep: int = DEFAULT_LOG_KEEP) -> str:
    return f"""# generated by dsh-muctl - rotation for the gateway and every instance log.
#
# copytruncate: supervise-daemon (and openresty) keep their log file
# descriptors open, so each file is copied and then truncated in place instead
# of being renamed away. Alpine runs /etc/periodic/daily/logrotate from crond.
{LOGS}/*.log {{
	daily
	maxsize {max_mb}M
	rotate {keep}
	missingok
	notifempty
	compress
	delaycompress
	copytruncate
	su root root
}}
"""


def install_logrotate(cfg: dict) -> str | None:
    """Install the logrotate drop-in (only when logrotate is present)."""
    if not os.path.isdir(LOGROTATE_CONF_DIR) or not shutil.which("logrotate"):
        return None
    atomic_write(LOGROTATE_CONF, render_logrotate_conf(), mode=0o644)
    return LOGROTATE_CONF


def platform_log_files() -> list[str]:
    files = []
    for pattern in ("*.log",):
        files += [p for p in glob.glob(f"{LOGS}/{pattern}") if os.path.isfile(p)]
    return sorted(set(files))


def rotate_logs(max_mb: int = DEFAULT_LOG_MAX_MB, keep: int = DEFAULT_LOG_KEEP,
                force: bool = False) -> list[dict]:
    """Size-based copytruncate rotation; returns what was rotated."""
    rotated = []
    threshold = max_mb * 1024 * 1024
    for path in platform_log_files():
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < threshold and not force:
            continue
        if size == 0:
            continue
        # shift .N-1 -> .N, dropping the oldest
        with contextlib.suppress(OSError):
            os.unlink(f"{path}.{keep}")
        for index in range(keep - 1, 0, -1):
            src, dst = f"{path}.{index}", f"{path}.{index + 1}"
            if os.path.exists(src):
                with contextlib.suppress(OSError):
                    os.replace(src, dst)
        with contextlib.suppress(OSError):
            shutil.copy2(path, f"{path}.1")
        with open(path, "w"):
            pass  # truncate in place, keeping the writer's descriptor valid
        with contextlib.suppress(OSError):
            os.chmod(path, 0o640)
        rotated.append({"file": path, "bytes": size})
    return rotated


# ------------------------------------------------------------------- backup

BACKUP_FORMAT = 1
BACKUP_MANIFEST = "MANIFEST.json"


def backup_members(with_instances: bool = False) -> list[str]:
    """Paths (relative to /) that make up a platform backup.

    Derived from the actual locations rather than hard-coded, so the same code
    works in tests and in a relocated installation.
    """
    def rel(path: str) -> str:
        return os.path.relpath(path, "/")

    members = [rel(f"{ETC}/{name}") for name in
               ("state.json", "dsh-mu.conf", "dsh-mu.conf.default", "providers.json",
                "admin.token", "secret.key")]
    members += [rel(f"{GW_DIR}/{name}") for name in
                ("users.json", "upstreams.json", "config.json")]
    if with_instances:
        for name in sorted(load_state()["instances"]):
            # Both persistent volumes. `home` is DSH_HOME (sessions, credentials,
            # profiles); `workspace` is the *live* working directory - the jail
            # bind-mounts it at /home/dsh/workspace, shadowing home/workspace, so
            # backing up only `home` silently lost everything users wrote.
            for volume in ("home", "workspace"):
                path = f"{INSTANCES_DIR}/{name}/{volume}"
                if os.path.isdir(path):
                    members.append(rel(path))
    return members


def create_backup(out: str, with_instances: bool = False) -> dict:
    """Write a tar.gz with the platform records (optionally instance homes)."""
    st = load_state()
    members = [m for m in backup_members(with_instances) if os.path.exists(f"/{m}")]
    manifest = {
        "format": BACKUP_FORMAT,
        "created": int(time.time()),
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": socket.gethostname(),
        "dsh_mu_version": VERSION,
        "state_schema": STATE_SCHEMA,
        "alpine": host_alpine_version(),
        "dsh_version": installed_dsh_version(),
        "users": sorted(st["users"]),
        "instances": sorted(st["instances"]),
        "ports": {name: int(inst.get("port", 0)) for name, inst in st["instances"].items()},
        "with_instances": bool(with_instances),
        "members": members,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    staging = tempfile.mkdtemp(prefix="dshmu-backup-")
    os.chmod(staging, 0o700)
    try:
        with open(f"{staging}/{BACKUP_MANIFEST}", "w") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
        # Mirror the tree inside the staging dir so a single `tar -C` suffices:
        # BusyBox tar (Alpine) only honours one -C. Directories are symlinked and
        # archived with -h, so instance homes are not copied twice.
        tops = {BACKUP_MANIFEST}
        for member in members:
            src, dst = f"/{member}", f"{staging}/{member}"
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src) and not os.path.islink(src):
                os.symlink(src, dst)
            else:
                shutil.copy2(src, dst)
            tops.add(member.split("/")[0])
        res = run(["tar", "-c", "-z", "-h", "-f", os.path.abspath(out), "-C", staging]
                  + sorted(tops), check=False, capture=True)
        if res.returncode != 0:
            die(f"backup failed: {(res.stderr or res.stdout or '').strip()[:300]}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    os.chmod(out, 0o600)          # the archive holds the session secret and tokens
    manifest["bytes"] = os.path.getsize(out)
    manifest["path"] = os.path.abspath(out)
    return manifest


def read_backup_manifest(path: str) -> dict:
    if not os.path.exists(path):
        die(f"no such backup: {path}")
    res = run(["tar", "xzOf", path, BACKUP_MANIFEST], check=False, capture=True)
    if res.returncode != 0 or not res.stdout.strip():
        die(f"{path} is not a dsh-mu backup (no {BACKUP_MANIFEST})")
    try:
        manifest = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        die(f"{path}: unreadable manifest ({exc})")
    if int(manifest.get("format") or 0) != BACKUP_FORMAT:
        die(f"{path}: unsupported backup format {manifest.get('format')!r}")
    return manifest


def restore_plan(path: str) -> dict:
    """Compare a backup with the live platform without changing anything."""
    manifest = read_backup_manifest(path)
    current = load_state()
    archived_users = set(manifest.get("users") or [])
    archived_instances = set(manifest.get("instances") or [])
    live_users = set(current["users"])
    live_instances = set(current["instances"])
    conf_hash = None
    res = run(["tar", "xzOf", path, "etc/dsh-mu/dsh-mu.conf"], check=False, capture=True)
    if res.returncode == 0 and res.stdout:
        conf_hash = hashlib.sha256(res.stdout.encode()).hexdigest()
    live_conf_hash = None
    if os.path.exists(CONF_PATH):
        live_conf_hash = hashlib.sha256(read_text(CONF_PATH).encode()).hexdigest()
    # What the archive would actually write, and what that would replace. With
    # --with-instances a restore overwrites live user data, so "instance files:
    # yes" is not enough for an operator to decide.
    archived_volumes = []
    for member in (manifest.get("members") or []):
        rel_member = member.lstrip("./")
        if f"{INSTANCES_DIR.lstrip('/')}/" in rel_member:
            archived_volumes.append(rel_member)
    overwrites = sorted(v for v in archived_volumes if os.path.exists(f"/{v}"))
    archive_created = int(manifest.get("created") or 0)
    live_updated = int(current.get("updated_at") or 0)
    return {
        "backup": path,
        "manifest": manifest,
        "users": {"added": sorted(archived_users - live_users),
                  "removed": sorted(live_users - archived_users)},
        "instances": {"added": sorted(archived_instances - live_instances),
                      "removed": sorted(live_instances - archived_instances)},
        "config_differs": (conf_hash != live_conf_hash) if conf_hash else False,
        "with_instances": bool(manifest.get("with_instances")),
        "archive_volumes": sorted(archived_volumes),
        "overwrites": overwrites,
        "archive_created": archive_created,
        "live_updated_at": live_updated,
        "archive_is_older": bool(archive_created and live_updated
                                 and archive_created < live_updated),
    }


def apply_restore(path: str, plan: dict) -> dict:
    """Replace /etc/dsh-mu with the archive's copy (after backing up the current one)."""
    safety = f"{ETC}.bak-{time.strftime('%Y%m%d-%H%M%S')}.tgz"
    with contextlib.suppress(OSError):
        create_backup(safety, with_instances=False)
    res = run(["tar", "xzf", path, "-C", "/"], check=False, capture=True)
    if res.returncode != 0:
        die(f"restore failed while unpacking: {(res.stderr or '').strip()[:300]}")
    # Permissions inside the archive are preserved, but make sure the gateway can
    # still read its views and nobody else can.
    group = nginx_group()
    for name in ("secret.key", "admin.token", "state.json", "providers.json",
                 "dsh-mu.conf"):
        p = f"{ETC}/{name}"
        if os.path.exists(p):
            with contextlib.suppress(OSError):
                os.chmod(p, 0o640 if name in ("secret.key", "admin.token") else 0o600)
                if group and name in ("secret.key", "admin.token"):
                    os.chown(p, 0, grp.getgrnam(group).gr_gid)
    st = load_state()          # migrates the restored state if it is older
    save_config(st["config"])
    sync_all(st)
    # `tar x` restores the ownership *recorded in the archive*. That is right for
    # a same-host backup, but an archive from before a uid change (or from another
    # host) leaves instance volumes owned by the wrong uid - and a jailed instance
    # has no capabilities to repair that itself, so it could not write its own
    # home/workspace until something re-owned them. The record is authoritative
    # here, exactly as it is for a live instance.
    reowned = []
    for name in sorted(st["instances"]):
        ensure_owner(st, name)
        reowned.append(name)
    return {"restored": path, "safety_backup": safety, "plan": plan,
            "reowned": reowned}


def cmd_backup(args):
    ensure_root()
    out = args.out or f"/var/backups/dsh-mu-{time.strftime('%Y%m%d-%H%M%S')}.tgz"
    manifest = create_backup(out, with_instances=args.with_instances)
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        info(f"backup written: {manifest['path']} ({manifest['bytes']} bytes, mode 0600)")
        info(f"  users: {', '.join(manifest['users']) or '-'}")
        info(f"  instances: {', '.join(manifest['instances']) or '-'}"
             + (" (including their home directories)" if manifest["with_instances"] else
                " (records only; add --with-instances for their files)"))
    return 0


def cmd_restore(args):
    ensure_root()
    plan = restore_plan(args.file)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    m = plan["manifest"]
    print(f"backup:    {plan['backup']}")
    print(f"created:   {m.get('created_iso')} on {m.get('host')} "
          f"(dsh-mu {m.get('dsh_mu_version')}, schema {m.get('state_schema')})")
    print(f"users:     +{plan['users']['added'] or '[]'} -{plan['users']['removed'] or '[]'}")
    print(f"instances: +{plan['instances']['added'] or '[]'} "
          f"-{plan['instances']['removed'] or '[]'}")
    print(f"config:    {'differs from the live one' if plan['config_differs'] else 'identical'}")
    print(f"instance files included: {plan['with_instances']}")
    volumes = plan.get("archive_volumes") or []
    if volumes:
        overwrites = set(plan.get("overwrites") or [])
        for volume in volumes:
            mark = "OVERWRITES live data" if volume in overwrites else "new"
            print(f"  volume: {volume}  ({mark})")
    if plan.get("archive_is_older"):
        print("warning:   this archive is OLDER than the live platform "
              "(restoring it discards newer changes)")
    if args.dry_run:
        print("dry run: nothing was changed")
        return 0
    if not args.force:
        die("refusing to overwrite the live platform without --force "
            "(re-run with --force; the current state is backed up first)")
    result = apply_restore(args.file, plan)
    info(f"restored {result['restored']}; previous state saved to {result['safety_backup']}")
    info("gateway views re-rendered; run `dsh-muctl doctor` and restart instances if needed")
    return 0


def cmd_logs_size(args):
    rows = []
    for path in platform_log_files():
        try:
            rows.append((os.path.basename(path), os.path.getsize(path)))
        except OSError:
            continue
    rows.sort(key=lambda r: -r[1])
    if getattr(args, "json", False):
        print(json.dumps({"files": [{"file": n, "bytes": b} for n, b in rows],
                          "total_bytes": sum(b for _n, b in rows)}, indent=2, sort_keys=True))
        return 0
    for name, size in rows:
        print(f"{size / 1024 / 1024:9.2f} MB  {name}")
    print(f"{sum(b for _n, b in rows) / 1024 / 1024:9.2f} MB  total ({len(rows)} files)")
    return 0


def cmd_logs_rotate(args):
    ensure_root()
    rotated = rotate_logs(max_mb=args.max_mb, keep=args.keep, force=args.force)
    if getattr(args, "json", False):
        print(json.dumps({"rotated": rotated}, indent=2, sort_keys=True))
        return 0
    if not rotated:
        info(f"nothing to rotate (threshold {args.max_mb} MB)")
    for item in rotated:
        info(f"rotated {item['file']} ({item['bytes'] / 1024 / 1024:.1f} MB -> .1)")
    return 0


def cmd_provider(args):
    """Manage the provider catalog (built-in presets + operator definitions)."""
    ensure_root()
    action = args.action
    custom = load_custom_providers()
    if action == "spec":
        print(PROVIDER_SPEC_TEXT)
        return 0
    if action in ("validate", "import"):
        path = args.pkg_file or args.name
        if not path:
            die("usage: dsh-muctl provider validate|import <file.json>")
        try:
            doc = json.loads(read_text(path) or "")
        except (OSError, json.JSONDecodeError) as exc:
            die(f"could not read {path}: {exc}")
        pkg, problems = validate_provider_package(doc)
        report = {"file": path, "valid": not problems, "problems": problems,
                  "package": pkg if not problems else None}
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        elif problems:
            for item in problems:
                print(f"  - {item}")
            print(f"{path}: {len(problems)} problem(s)")
        else:
            print(f"{path}: OK ({pkg['id']}, {len(pkg['models'])} model(s), "
                  f"default {pkg['default_model']})")
            _bases = {m["base_url"] for m in pkg["models"]}
            _apis = sorted({m["api"] for m in pkg["models"]})
            print(f"  base url(s): {', '.join(sorted(_bases))}")
            print(f"  protocol(s): {', '.join(_apis)}")
            print("  discover: " + (pkg["discover"]["url"] if pkg.get("discover") else "-"))
            print("  usage:    " + (pkg["usage"]["url"] if pkg.get("usage") else "-"))
        if problems:
            return 1
        if action == "import":
            target = save_provider_package(pkg)
            custom[pkg["id"]] = provider_package_to_defn(pkg)
            save_custom_providers(custom)
            info(f"provider {pkg['id']} imported -> {target}")
        return 0
    if action == "export":
        pid = args.name or ""
        pkg = load_provider_package(pid)
        if not pkg:
            die(f"no imported package named {pid!r} (dsh-muctl provider import <file>)")
        print(json.dumps(package_for_display(pkg), indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    if action in ("models", "usage"):
        pid = args.name or ""
        pkg = load_provider_package(pid)
        if not pkg:
            die(f"no imported package named {pid!r}")
        key = args.key or ""
        if not key:
            st_now = load_state()
            first = next(iter(st_now["instances"]), None)
            key = (provider_credentials(st_now, first)[0] if first else None) or \
                  str(st_now["config"].get("model_api_key") or "")
        if not key:
            die("no credential: pass --key, or set a platform model key / instance key")
        if action == "models":
            res = provider_discover(pkg, key)
            if not res["ok"]:
                die(f"discover failed ({res.get('status')}): {res.get('error')}")
            known = {m["id"] for m in pkg["models"]}
            merged = list(pkg["models"])
            added = 0
            for row in res["models"]:
                if row["id"] not in known:
                    merged.append({"id": row["id"], "api": pkg["api"],
                                   "base_url": pkg["base_url"],
                                   **{k: row[k] for k in ("name", "contextWindow", "maxTokens")
                                      if k in row}})
                    added += 1
                elif args.refresh:
                    for m in merged:
                        if m["id"] == row["id"]:
                            for k in ("name", "contextWindow", "maxTokens"):
                                if k in row:
                                    m[k] = row[k]
            if args.refresh and getattr(args, "detect_api", False):
                unknown = [m for m in merged if m["id"] not in known]
                for m in unknown:
                    api = provider_detect_api(pkg, key, m["id"])
                    if api:
                        m["api"] = api
                if args.json:
                    info(f"protocols detected for {sum(1 for m in unknown if m['api'])}"
                         f"/{len(unknown)} new model(s)")
            if args.refresh:
                pkg["models"] = merged
                save_provider_package(pkg)
            if args.json:
                print(json.dumps({"provider": pid, "found": len(res["models"]),
                                  "added": added, "saved": args.refresh,
                                  "models": merged}, indent=2, sort_keys=True))
            else:
                for m in sorted(merged, key=lambda x: x["id"]):
                    print(f"  {m['id']:32} {m.get('api',''):20} {m.get('contextWindow','')}")
                print(f"{len(merged)} model(s); new from provider: {added}; "
                      f"{'saved' if args.refresh else 'dry run (--refresh to save)'}")
            return 0
        res = provider_usage(pkg, key)
        if not res["ok"]:
            die(f"usage failed ({res.get('status')}): {res.get('error')}")
        if args.json:
            print(json.dumps({"provider": pid, "windows": res["windows"]},
                             indent=2, sort_keys=True))
        else:
            for name, row in res["windows"].items():
                print(f"  {name:8} {row.get('percent', '?')}%  "
                      f"{row.get('status','')}  resets {row.get('resets_at','?')}")
        return 0
    custom = load_custom_providers()
    if action == "list":
        catalog = all_providers()
        rows = [("NAME", "KIND", "ROUTE/API", "BASE URL", "MODELS", "DEFAULT", "SOURCE")]
        for name in sorted(catalog):
            defn = catalog[name]
            rows.append((name, defn.get("kind", "deepseek"),
                         defn.get("route") or ("messages" if defn.get("kind") != "pi-ai" else "-"),
                         defn.get("base_url") or "(DSH default)",
                         str(len(defn.get("models") or [])), defn.get("model", "?"),
                         "custom" if name in custom else "builtin"))
        if args.json:
            print(json.dumps({n: catalog[n] for n in sorted(catalog)}, indent=2, sort_keys=True))
            return 0
        width = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(c.ljust(width[i]) for i, c in enumerate(row)))
        return 0
    if action == "show":
        catalog = all_providers()
        defn = catalog.get(args.name) or die(f"no such provider: {args.name}")
        print(json.dumps(defn, indent=2, sort_keys=True))
        return 0
    if action in ("add", "set"):
        if args.from_file:
            defn = json.loads(read_text(args.from_file) or "{}")
        else:
            defn = {
                "kind": args.kind,
                "route": args.route,
                "api": args.api,
                "base_url": args.base_url,
                "credentials_ref": args.credentials_ref,
                "model": args.model,
                "session_header": args.session_header,
                "display_name": args.display_name,
                "headers": json.loads(args.headers) if args.headers else None,
                "models": parse_model_lines(args.models) if args.models else None,
            }
        normalized = normalize_provider(args.name, defn)
        custom[args.name] = normalized
        save_custom_providers(custom)
        info(f"provider {args.name} saved to {PROVIDERS_PATH} "
             f"({len(normalized['models'])} model(s), default {normalized['model']})")
        return 0
    if action == "rm":
        # Two kinds of "custom" exist: the legacy providers.json entries and the
        # imported package files (the model used by plans and by template imports).
        # Only the second branch was missing, so an imported provider could never be
        # removed - not by the operator and not by the test that imports one.
        removed = False
        if args.name in custom:
            custom.pop(args.name)
            save_custom_providers(custom)
            removed = True
        package_path = f"{PROVIDERS_DIR}/{args.name}.json"
        if os.path.exists(package_path):
            os.unlink(package_path)
            removed = True
        if not removed:
            die(f"{args.name} is neither an imported package nor an operator-defined "
                f"provider (imported: {', '.join(sorted(list_provider_packages())) or 'none'}; "
                f"custom: {', '.join(sorted(custom)) or 'none'})")
        info(f"provider {args.name} removed")
        return 0
    die(f"unknown provider action: {action}")


PROVIDER_PACKAGE_SCHEMA = "dsh-mu.provider/v1"
PROVIDER_APIS = ("openai-completions", "openai-responses", "anthropic-messages")

PROVIDER_SPEC_TEXT = """\
# provider 配置文件规范（dsh-mu.provider/v1）

一个 JSON 文件，描述"一家模型供应商怎么接进来"：协议、地址、鉴权、模型目录，
以及可选的"列模型"和"查配额"接口。**平台自己会校验**，你（AI）不需要跑任何命令。

## 结构

```json
{
  "schema": "dsh-mu.provider/v1",
  "id": "opencode-go",
  "label": "OpenCode Go",
  "docs": "https://opencode.ai/docs/go/",
  "auth": { "type": "bearer" },
  "headers": { "x-opencode-session": "{session}" },
  "base_url": "https://opencode.ai/inference/go/openai/v1",
  "api": "openai-completions",
  "default_model": "glm-5.3-flash",
  "models": [
    { "id": "glm-5.3-flash", "name": "GLM 5.3 Flash",
      "context_window": 131072, "max_tokens": 32768 },
    { "id": "grok-4.6", "api": "openai-responses" },
    { "id": "claude-haiku-5-5", "api": "anthropic-messages",
      "base_url": "https://opencode.ai/inference/go/anthropic" }
  ],
  "discover": {
    "url": "https://opencode.ai/inference/go/v1/models",
    "items": "data", "id": "id", "name": "name",
    "context_window": "context_window", "max_tokens": "max_output_tokens"
  },
  "usage": {
    "url": "https://opencode.ai/inference/go/v1/usage",
    "windows": { "rolling": "usage.rolling", "monthly": "usage.monthly" },
    "percent": "percent", "resets_at": "resetsAt", "status": "status"
  }
}
```

## 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema` | 是 | 固定 `dsh-mu.provider/v1` |
| `id` | 是 | 小写字母/数字/连字符，平台内唯一 |
| `label` | 是 | 给人看的名字 |
| `docs` | 否 | 供应商文档链接 |
| `auth.type` | 是 | `bearer`（`Authorization: Bearer <key>`）或 `x-api-key` |
| `headers` | 否 | 额外请求头；`{session}` 会被替换成实例名 |
| `base_url` | 是 | **默认地址的前缀**（见下） |
| `api` | 是 | 默认协议，三种之一 |
| `default_model` | 否 | 默认模型 id，必须是 `models` 里的 |
| `models` | 是 | 至少一个；`id` 必填，可覆盖 `api`/`base_url`，可带 `name`/`context_window`/`max_tokens`/**`input_modalities`** |
| `discover` | 否 | 让平台去上游拉模型列表（`items` 是 JSON 里模型数组的路径） |
| `usage` | 否 | 让平台读配额（`windows` 是"窗口名 → JSON 路径"） |

## 协议与 URL 拼接（最容易错的地方）

平台**不改写** `base_url`，只按协议在它后面拼路径：

| `api` | 实际请求 |
|---|---|
| `openai-completions` | `POST {base_url}/chat/completions` |
| `openai-responses` | `POST {base_url}/responses` |
| `anthropic-messages` | `POST {base_url}/v1/messages` |

模型条目还有一个可选的 **`input_modalities`**（`["text"]` / `["text","image"]`）；不写就按**纯文本**处理，
发图会被拒绝（"当前模型不支持图片"）。`discover` 也能把它从上游读进来（`input_modalities` 字段）。

所以 `base_url` **只能是前缀**，不要带 `/v1`、`/chat/completions` 这类会被再拼一次的段。
同一家供应商常常按模型分协议，这时就在模型上写 `api`（必要时再写它的 `base_url`）。
只支持这三种协议；Gemini 的 `:generateContent` 不在其中。

## 给 AI 的提示词（整段复制，后面接上供应商文档）

> 你是模型接入配置生成器。请阅读我随后提供的供应商文档，输出**一个 JSON 对象**，
> 严格遵守 `dsh-mu.provider/v1` 规范：
>
> 1. 顶层：`schema`（固定 `dsh-mu.provider/v1`）、`id`、`label`、`auth.type`、`base_url`、
>    `api`、`models`（至少一个）；可选 `docs`、`headers`、`default_model`、`discover`、`usage`。
> 2. `auth.type`：文档要求 `Authorization: Bearer <key>` 就写 `bearer`；要求 `x-api-key` 就写 `x-api-key`。
> 3. **URL 拼接**：`openai-completions` → `{base_url}/chat/completions`、
>    `openai-responses` → `{base_url}/responses`、`anthropic-messages` → `{base_url}/v1/messages`。
>    `base_url` 只写**前缀**，不要把 `/v1` 或 `/chat/completions` 写进去。
> 4. 不同模型走不同协议或不同地址时，把 `api`（必要时还有 `base_url`）写在**该模型**上；
>    不要拆成多个文件。
> 5. `models`：能从文档确定 id 的都列上；能确定就补 `name`、`context_window`、`max_tokens`；
>    不确定的字段**省略**，不要编造。
>    **支持图片输入的模型要写 `"input_modalities": ["text", "image"]`**（纯文本写 `["text"]`），
>    否则平台会按纯文本处理，用户发图会被拒绝。
> 6. 文档里有"列出模型"的接口（通常是 `GET …/models`）就写进 `discover`：
>    `url` 填完整地址；`items` 是 JSON 里模型数组的路径（如 `data`）；`id`/`name`/
>    `context_window`/`max_tokens` 是对应字段名，不确定就省略。
> 7. 文档里有配额/用量接口就写进 `usage`：`url` 完整地址；`windows` 是"窗口名 → JSON 路径"；
>    `percent`/`resets_at`/`status` 是该窗口对象里的字段名。
> 8. 只输出 JSON 本身：不要解释、不要 Markdown 代码围栏、不要省略号、不要注释。
>    我把它粘进平台后，平台会自己校验并告诉我哪里不对。

## 运维命令（给人用的，不必写进提示词）

```sh
dsh-muctl provider spec                        # 本规范 + 提示词
dsh-muctl provider validate my.json            # 只校验（与网页上的"只校验"等价）
dsh-muctl provider import   my.json            # 校验并入库
dsh-muctl provider export   opencode-go        # 导出（含刷新过的模型）
dsh-muctl provider models   opencode-go --key K --refresh --detect-api
dsh-muctl provider usage    opencode-go --key K
```
"""


def _json_path(doc, path: str):
    """Follow a dotted path into a JSON document (lists by index)."""
    cur = doc
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def validate_provider_package(doc) -> tuple[dict, list[str]]:
    """Normalise a provider package; return (normalised, problems)."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return {}, ["the file must contain a JSON object"]
    if doc.get("schema") != PROVIDER_PACKAGE_SCHEMA:
        problems.append(f"schema must be {PROVIDER_PACKAGE_SCHEMA!r}")
    pid = str(doc.get("id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", pid):
        problems.append("id must be lowercase letters/digits/hyphen (max 63)")
    label = str(doc.get("label") or pid).strip()
    auth = doc.get("auth") if isinstance(doc.get("auth"), dict) else {}
    auth_type = str(auth.get("type") or "bearer").strip()
    if auth_type not in ("bearer", "x-api-key"):
        problems.append("auth.type must be 'bearer' or 'x-api-key'")
    base_url = str(doc.get("base_url") or doc.get("baseURL") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        problems.append("base_url must be an http(s) URL")
    api = str(doc.get("api") or "openai-completions").strip()
    if api not in PROVIDER_APIS:
        problems.append("api must be one of: " + ", ".join(PROVIDER_APIS))
    headers = doc.get("headers") if isinstance(doc.get("headers"), dict) else {}
    models_in = doc.get("models")
    models: list[dict] = []
    if not isinstance(models_in, list) or not models_in:
        problems.append("models must be a non-empty list")
    else:
        seen: set[str] = set()
        for i, item in enumerate(models_in):
            if isinstance(item, str):
                item = {"id": item}
            if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                problems.append(f"models[{i}] needs an id")
                continue
            mid = str(item["id"]).strip()
            if mid in seen:
                problems.append(f"models[{i}] duplicates id {mid!r}")
                continue
            seen.add(mid)
            row = {"id": mid, "api": str(item.get("api") or api).strip(),
                   "base_url": str(item.get("base_url") or item.get("baseURL")
                                   or base_url).strip()}
            if row["api"] not in PROVIDER_APIS:
                problems.append(f"models[{i}].api must be one of: " + ", ".join(PROVIDER_APIS))
            _tail = {"anthropic-messages": ("/v1", "/v1/messages"),
                     "openai-completions": ("/chat/completions",),
                     "openai-responses": ("/responses",)}.get(row["api"], ())
            if _tail and row["base_url"].rstrip("/").endswith(_tail):
                problems.append(
                    f"models[{i}].base_url already contains {row['api']}'s path; "
                    f"give only the prefix (the platform appends it)")
            if item.get("name"):
                row["name"] = str(item["name"])
            # Accept both spellings: `load_provider_package` re-validates the
            # normalised file (camelCase), and reading only snake_case silently
            # dropped contextWindow/maxTokens on every load.
            for src, dst in (("context_window", "contextWindow"), ("max_tokens", "maxTokens")):
                raw = item.get(src, item.get(dst))
                try:
                    if raw is not None:
                        row[dst] = int(raw)
                except (TypeError, ValueError):
                    problems.append(f"models[{i}].{src} must be an integer")
            # Accept both spellings: `export` used to emit the internal
            # `inputModalities`, and re-importing that silently dropped the field.
            # Reasoning levels (pi-ai model profile): {level: wire} or false.
            efforts = item.get("reasoning_efforts", item.get("reasoningEfforts"))
            if efforts is not None:
                if isinstance(efforts, bool):
                    row["reasoningEfforts"] = efforts
                elif isinstance(efforts, dict) and all(
                        isinstance(k, str) for k in efforts):
                    row["reasoningEfforts"] = {str(k): ("" if v is None else str(v))
                                               for k, v in efforts.items()}
                elif isinstance(efforts, str):
                    parsed = parse_efforts(efforts)
                    if parsed is not None:
                        row["reasoningEfforts"] = parsed
                else:
                    problems.append(
                        f"models[{i}].reasoning_efforts must be an object (level: wire) or false")
            modalities = item.get("input_modalities", item.get("inputModalities"))
            if modalities is not None:
                if not isinstance(modalities, list) or not modalities:
                    problems.append(f"models[{i}].input_modalities must be a non-empty list")
                else:
                    allowed = [str(m).strip() for m in modalities
                               if str(m).strip() in ("text", "image")]
                    if not allowed:
                        problems.append(f"models[{i}].input_modalities needs 'text' and/or 'image'")
                    else:
                        row["inputModalities"] = allowed
            models.append(row)
    default_model = str(doc.get("default_model") or "").strip() or (models[0]["id"] if models else "")
    if default_model and models and default_model not in {m["id"] for m in models}:
        problems.append("default_model must be one of the declared model ids")
    discover = None
    if isinstance(doc.get("discover"), dict):
        d = doc["discover"]
        deref = str(d.get("url") or "").strip()
        if not deref.startswith(("http://", "https://")):
            problems.append("discover.url must be an http(s) URL")
        discover = {"url": deref, "api": str(d.get("api") or api).strip(),
                    "items": str(d.get("items") or "data"),
                    "id": str(d.get("id") or "id"),
                    "name": str(d.get("name") or "name"),
                    "context_window": str(d.get("context_window") or "context_window"),
                    "max_tokens": str(d.get("max_tokens") or "max_output_tokens"),
                    "input_modalities": str(d.get("input_modalities") or "input_modalities")}
    usage = None
    if isinstance(doc.get("usage"), dict):
        u = doc["usage"]
        uref = str(u.get("url") or "").strip()
        if not uref.startswith(("http://", "https://")):
            problems.append("usage.url must be an http(s) URL")
        windows = u.get("windows") if isinstance(u.get("windows"), dict) else {}
        if not windows:
            problems.append("usage.windows must map at least one window name to a JSON path")
        usage = {"url": uref,
                 "windows": {str(k): str(v) for k, v in windows.items()},
                 "percent": str(u.get("percent") or "percent"),
                 "resets_at": str(u.get("resets_at") or "resetsAt"),
                 "status": str(u.get("status") or "status")}
    normalised = {"schema": PROVIDER_PACKAGE_SCHEMA, "id": pid, "label": label,
                  "docs": str(doc.get("docs") or ""), "auth": {"type": auth_type},
                  "headers": {str(k): str(v) for k, v in headers.items()},
                  "base_url": base_url, "api": api, "default_model": default_model,
                  "models": models}
    if discover:
        normalised["discover"] = discover
    if usage:
        normalised["usage"] = usage
    return normalised, problems


def provider_auth_headers(pkg: dict, key: str, instance: str | None = None) -> dict:
    """The headers one authenticated call to this provider needs."""
    headers = {}
    for name, value in (pkg.get("headers") or {}).items():
        headers[name] = value.replace("{session}", f"dsh-mu-{instance}" if instance else "dsh-mu")
    if (pkg.get("auth") or {}).get("type") == "x-api-key":
        headers["x-api-key"] = key
        headers.setdefault("anthropic-version", "2023-06-01")
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def provider_http_json(method: str, url: str, pkg: dict, key: str, *,
                       body: dict | None = None, instance: str | None = None,
                       timeout: float = 25.0) -> tuple[int, dict | str]:
    """One request to the provider; returns (status, parsed-json-or-error-text)."""
    headers = provider_auth_headers(pkg, key, instance)
    headers.setdefault("User-Agent", f"dsh-mu/{VERSION}")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(2 * 1024 * 1024).decode("utf-8", "replace")
            status = res.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        return exc.code, raw[:800]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw[:800]


def provider_discover(pkg: dict, key: str, instance: str | None = None) -> dict:
    """Ask the provider for its model list, using the package's `discover` block."""
    disc = pkg.get("discover")
    if not disc:
        return {"ok": False, "error": "this provider has no discover block"}
    status, payload = provider_http_json("GET", disc["url"], pkg, key, instance=instance)
    if status != 200 or not isinstance(payload, dict):
        return {"ok": False, "status": status,
                "error": payload if isinstance(payload, str) else json.dumps(payload)[:300]}
    items = _json_path(payload, disc["items"])
    if items is None and isinstance(payload.get("data"), list):
        items = payload["data"]
    if isinstance(items, dict):
        items = [dict(v if isinstance(v, dict) else {}, id=k) for k, v in items.items()]
    if not isinstance(items, list):
        return {"ok": False, "status": status, "error": "the response had no model list"}
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = _json_path(item, disc["id"])
        if not mid:
            continue
        row = {"id": str(mid)}
        name = _json_path(item, disc["name"])
        if name:
            row["name"] = str(name)
        for src, dst in ((disc["context_window"], "contextWindow"),
                         (disc["max_tokens"], "maxTokens")):
            val = _json_path(item, src)
            if isinstance(val, (int, float)) and val > 0:
                row[dst] = int(val)
        mods = _json_path(item, disc.get("input_modalities") or "input_modalities")
        if isinstance(mods, list):
            picked = [str(m) for m in mods if str(m) in ("text", "image")]
            if picked:
                row["inputModalities"] = picked
        out.append(row)
    return {"ok": bool(out), "status": status, "models": out}


def provider_detect_api(pkg: dict, key: str, model_id: str,
                        instance: str | None = None) -> str | None:
    """Ask the provider which protocol serves this model (one tiny request each).

    The model list endpoints do not say, and the protocols are not interchangeable
    (OpenCode Go answers `ModelProtocolUnsupported`), so the only reliable answer
    comes from trying them in order and keeping the one that is not rejected.
    """
    base = next((m["base_url"] for m in pkg["models"] if m["id"] == model_id),
                pkg["base_url"]).rstrip("/")
    payload = {"model": model_id, "max_tokens": 1, "stream": False,
               "messages": [{"role": "user", "content": "ping"}]}
    attempts = (("openai-completions", "POST", f"{base}/chat/completions"),
                ("openai-responses", "POST", f"{base}/responses"),
                ("anthropic-messages", "POST", f"{base}/v1/messages"))
    for api, method, url in attempts:
        body = dict(payload)
        if api == "anthropic-messages":
            body["max_tokens"] = 1
        status, raw = provider_http_json(method, url, pkg, key, body=body, instance=instance)
        if status in (200, 201):
            return api
        text = raw if isinstance(raw, str) else json.dumps(raw)
        # A protocol the model does not serve is the one answer we trust to skip.
        if status in (400, 404, 405) and ("rotocol" in text or "not supported" in text.lower()
                                          or "not found" in text.lower()):
            continue
        if status in (401, 403):
            return None  # the credential is wrong; stop guessing
    return None


def provider_usage(pkg: dict, key: str, instance: str | None = None) -> dict:
    """Read the quota windows declared by the package's `usage` block."""
    use = pkg.get("usage")
    if not use:
        return {"ok": False, "error": "this provider has no usage block"}
    status, payload = provider_http_json("GET", use["url"], pkg, key, instance=instance)
    if status != 200 or not isinstance(payload, dict):
        return {"ok": False, "status": status,
                "error": payload if isinstance(payload, str) else json.dumps(payload)[:300]}
    windows = {}
    for name, path in use["windows"].items():
        node = _json_path(payload, path)
        if not isinstance(node, dict):
            continue
        windows[name] = {k: node[v] for k, v in
                         (("percent", use["percent"]), ("resets_at", use["resets_at"]),
                          ("status", use["status"])) if v in node}
    return {"ok": bool(windows), "status": status, "windows": windows}


PLAN_SCHEMA = "dsh-mu.plan/v1"
PLANS_DIR = f"{VAR}/plans"


def plan_id_for(name: str) -> str:
    """A stable, filesystem-safe id for a plan name."""
    digest = hashlib.sha256(name.strip().encode()).hexdigest()[:8]
    return f"plan_{digest}"


def plan_key_env(plan_id: str) -> str:
    """The credential reference a plan uses inside the instance."""
    return "DSHMU_" + plan_id.upper().replace("-", "_")


def load_plan(pid: str) -> dict | None:
    text = read_text(f"{PLANS_DIR}/{pid}.json")
    if not text:
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def list_plans() -> dict[str, dict]:
    out = {}
    try:
        names = sorted(os.listdir(PLANS_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name.endswith(".key.json"):
            continue
        plan = load_plan(name[:-5])
        if plan:
            out[plan["id"]] = plan
    return out


def plan_key_path(pid: str) -> str:
    return f"{PLANS_DIR}/{pid}.key"


def save_plan(plan: dict, key: str | None = None) -> dict:
    """Persist a plan (and its credential, if one was supplied)."""
    os.makedirs(PLANS_DIR, exist_ok=True)
    plan = dict(plan)
    plan.setdefault("schema", PLAN_SCHEMA)
    plan.setdefault("visible_to", ["*"])
    plan.setdefault("enabled", True)
    plan.setdefault("owner", "*")
    if key is not None:
        path = plan_key_path(plan["id"])
        atomic_write(path, key.strip() + "\n", mode=0o600)
        os.chmod(path, 0o600)
        plan["has_key"] = bool(key.strip())
    plan.setdefault("has_key", os.path.exists(plan_key_path(plan["id"])))
    atomic_write(f"{PLANS_DIR}/{plan['id']}.json",
                 json.dumps(plan, indent=2, sort_keys=True) + "\n", mode=0o600)
    return plan


def plan_key(plan: dict) -> str:
    return (read_text(plan_key_path(plan["id"])) or "").strip()


def plan_visible_to_user(plan: dict, user: str) -> bool:
    """Team plans are forced visible; personal plans belong to one user."""
    if not plan.get("enabled", True):
        return False
    owner = str(plan.get("owner") or "*")
    if owner != "*":
        return owner == user
    visible = plan.get("visible_to") or ["*"]
    if "*" in visible:
        return True
    return user in visible


def plans_for_user(user: str) -> list[dict]:
    return [p for p in list_plans().values() if plan_visible_to_user(p, user)]


def plan_package(plan: dict) -> dict | None:
    """The provider package a plan points at (imported package first, then preset)."""
    name = str(plan.get("provider") or "")
    return load_provider_package(name) or preset_as_package(name)


RELAY_PLACEHOLDER = "mu-relay"

# The instance already runs inside our jail + cgroup; DSH's own confinement runner
# (bwrap, then Landlock) cannot create a namespace there and fails closed, which
# broke the bash tool *and* the ripgrep-backed file search. Full access is correct
# here: the jail is the boundary.
SANDBOX_PATCH = """- id: sandbox-policy
  config:
    defaultMode: danger-full-access
- id: permission
  config:
    defaultPreset: danger-full-access
"""


def relay_enabled(cfg: dict) -> bool:
    return bool(cfg.get("relay_enabled", True))


def relay_token(st: dict, name: str) -> str:
    """The instance's relay token (pure read: rendering must not mutate state)."""
    return str(((st.get("instances") or {}).get(name) or {}).get("relay_token") or "")


def ensure_relay_token(st: dict, name: str) -> str:
    """Return the instance's relay token, creating it on the caller's state object.

    This used to save from inside the rendering helpers, and the caller then wrote
    its own older state back over it -- so the patch and relay.json ended up with
    different tokens and every request was refused with 403.
    """
    inst = st["instances"].setdefault(name, {})
    token = str(inst.get("relay_token") or "")
    if len(token) != 32:
        token = secrets.token_hex(16)
        inst["relay_token"] = token
    return token


def relay_auth_type(route: dict) -> str:
    """How the provider wants its key: `bearer` or `x-api-key`."""
    headers = {k.lower(): v for k, v in (route.get("headers") or {}).items()}
    for key in headers:
        if key in ("x-api-key", "api-key"):
            return "x-api-key"
    return "x-api-key" if route.get("api") == "anthropic-messages" else "bearer"


def relay_document(st: dict) -> dict:
    """What the gateway needs to inject keys: token -> route -> upstream/auth/key.

    Written to `<ETC>/gateway/relay.json` (copied into the jail's /etc/dsh-mu). The
    instance jails never see this file, so a plan key cannot be read from inside.
    """
    tokens: dict[str, dict] = {}
    for name, inst in sorted((st.get("instances") or {}).items()):
        user = str(inst.get("user") or name)
        routes = {}
        for plan in plans_for_user(user):
            key = plan_key(plan)
            for route in plan_routes(plan, user):
                keyless = bool(route.get("keyless"))
                if not key and not keyless:
                    continue
                routes[route["route"]] = {
                    "plan": plan["id"],
                    "upstream": route["base_url"].rstrip("/"),
                    # Keyless: no auth at all. relay.lua sets an empty header value,
                    # and nginx's `proxy_set_header X ""` omits the header entirely --
                    # which is what the Kilo free gateway needs (a bogus value is 401).
                    "auth": "" if keyless else relay_auth_type(route),
                    "key": "" if keyless else key,
                }
        if routes:
            tokens[relay_token(st, name)] = {"instance": name, "routes": routes}
    return {"version": 1, "placeholder": RELAY_PLACEHOLDER, "tokens": tokens}


def write_relay_document(st: dict) -> str:
    path = f"{GW_DIR}/relay.json"
    group = nginx_group() if os.geteuid() == 0 else None
    doc = relay_document(st)
    atomic_write(path, json.dumps(doc, sort_keys=True) + "\n", mode=0o640, group=group)
    return path


def plan_available_set(plan: dict) -> set[str] | None:
    """Models the plan's key was proven to use, or None when never probed."""
    rows = plan.get("available_models")
    if rows is None:
        return None
    return {str(m) for m in rows if m}


def plan_effective_models(plan: dict) -> list[str]:
    """The models a plan will actually advertise (subset ∩ available)."""
    subset = [str(m) for m in (plan.get("models") or []) if m]
    available = plan_available_set(plan)
    if available is None:
        return subset
    if subset:
        return [m for m in subset if m in available]
    return sorted(available)


def plan_probe_models(plan: dict) -> dict:
    """Ask the provider which models this plan's credential can really use."""
    pkg = plan_package(plan)
    if not pkg:
        return {"ok": False, "error": "plan has no provider package"}
    key = plan_key(plan)
    if not key:
        return {"ok": False, "error": "plan has no credential (set a key first)"}
    res = provider_discover(pkg, key)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "discovery failed",
                "status": res.get("status")}
    known = {m["id"] for m in pkg["models"]}
    discovered = [str(row.get("id") or "") for row in res.get("models") or []]
    discovered = [m for m in dict.fromkeys(discovered) if m]
    available = [m for m in discovered if m in known]
    unknown = [m for m in discovered if m not in known]
    previous = plan_available_set(plan)
    before = sorted(previous) if previous is not None else []
    added = [m for m in available if m not in before]
    lost = [m for m in before if m not in available]
    plan["available_models"] = available
    plan["probe_unknown"] = unknown
    plan["probed_at"] = int(time.time())
    plan["probe_error"] = ""
    # Keep a human-readable record of what changed: without it the UI can only say
    # "29 usable" and the user cannot tell whether that is news or the same as before.
    plan["last_probe"] = {
        "at": plan["probed_at"],
        "first": previous is None,
        "catalog": len(known),
        "discovered": len(discovered),
        "available": len(available),
        "before": len(before),
        "added": added,
        "lost": lost,
        "unknown": unknown,
    }
    save_plan(plan)
    return {"ok": True, "available": available, "unknown": unknown,
            "catalog": len(known), "discovered": len(discovered),
            "added": added, "lost": lost, "first": previous is None,
            "before": len(before), "last_probe": plan["last_probe"]}


def plan_probe_usage(plan: dict) -> dict:
    """Quota / rate-limit windows reported by the provider for this plan's key."""
    pkg = plan_package(plan)
    if not pkg:
        return {"ok": False, "error": "plan has no provider package"}
    key = plan_key(plan)
    if not key:
        return {"ok": False, "error": "plan has no credential (set a key first)"}
    return provider_usage(pkg, key)


def set_plan_models(plan: dict, models: list[str], select_all_available: bool = False,
                    clear: bool = False) -> dict:
    """Replace the plan's model subset, validated against the provider catalog."""
    pkg = plan_package(plan)
    if not pkg:
        return {"ok": False, "error": "plan has no provider package"}
    catalog = [m["id"] for m in pkg["models"]]
    available = plan_available_set(plan)
    if clear:
        chosen: list[str] = []
    elif select_all_available and available is not None:
        chosen = [m for m in catalog if m in available]
    else:
        wanted = [str(m) for m in models if m]
        bad = [m for m in wanted if m not in catalog]
        if bad:
            return {"ok": False, "error": "not in the provider catalog",
                    "unknown": bad, "catalog": catalog}
        if available is not None:
            unusable = [m for m in wanted if m not in available]
            if unusable:
                return {"ok": False, "error": "this plan's key cannot use these models",
                        "unusable": unusable}
        chosen = wanted
    plan["models"] = chosen
    save_plan(plan)
    return {"ok": True, "models": chosen, "count": len(chosen)}


def plan_routes(plan: dict, user: str) -> list[dict]:
    """Turn one plan into the pi-ai routes it needs (one per base URL)."""
    pkg = plan_package(plan)
    if not pkg:
        return []
    subset = [m for m in (plan.get("models") or []) if m]
    available = plan_available_set(plan)
    models = []
    for m in pkg["models"]:
        if subset and m["id"] not in subset:
            continue
        # A probed plan only exposes models its key could actually use; the rest are
        # dropped here so the instance never offers a model that will 401.
        if available is not None and m["id"] not in available:
            continue
        models.append(m)
    if not models:
        return []
    by_base: dict[str, list[dict]] = {}
    for m in models:
        by_base.setdefault(m["base_url"], []).append(m)
    routes = []
    multi = len(by_base) > 1
    # Route ids must be lowercase hyphenated identifiers (DSH rejects others);
    # plan ids carry an underscore, so translate it.
    route_base = str(plan["id"]).replace("_", "-")
    for idx, (base, rows) in enumerate(by_base.items()):
        route = route_base if not multi else f"{route_base}-{idx + 1}"
        label = plan.get("name") or plan["id"]
        if multi:
            # A plan whose models live on several base URLs needs several routes
            # (pi-ai fixes one baseURL per provider), so the group names must differ:
            # suffix them with the protocol rather than the host (usually the same).
            _api_label = {"openai-completions": "chat",
                          "openai-responses": "responses",
                          "anthropic-messages": "messages"}.get(rows[0]["api"], rows[0]["api"])
            label = f"{label} · {_api_label}"
        routes.append({
            "route": route,
            "display_name": label,
            "base_url": base,
            "api": rows[0]["api"],
            # A keyless package (e.g. the Kilo free gateway) has no real credential.
            # Through the relay the instance still needs *an* apiKeyEnv, otherwise DSH
            # refuses with "No API key for provider: …"; the value it holds is the
            # relay placeholder and the relay then sends no Authorization upstream.
            # Without the relay a keyless route can only be declared literally keyless.
            "api_key_env": (plan_key_env(plan["id"])
                            if (not pkg.get("keyless") or relay_enabled(load_config()))
                            else None),
            "keyless": bool(pkg.get("keyless")),
            "headers": dict(pkg.get("headers") or {}),
            "models": [{"id": m["id"], "api": m["api"], "name": m.get("name"),
                        "contextWindow": m.get("contextWindow"),
                        "maxTokens": m.get("maxTokens"),
                        "inputModalities": m.get("inputModalities"),
                        # Reasoning levels must survive plan_routes, otherwise the
                        # instance never learns the model can think (no 推理等级 in
                        # the selector) even when the catalog declares it.
                        "reasoningEfforts": m.get("reasoningEfforts")} for m in rows],
            "default_model": (plan.get("default_model")
                              or pkg.get("default_model") or rows[0]["id"]),
            "plan": plan,
        })
    return routes


def plans_patch_yaml(user: str, instance: str,
                     default_plan: dict | None = None,
                     relay_token_value: str = "") -> tuple[str, dict]:
    """Render every plan visible to this user as one pi-ai provider per base URL."""
    plans = plans_for_user(user)
    cfg = load_config()
    relay = relay_enabled(cfg)
    token = relay_token_value if relay else ""
    relay_port = int(cfg.get("relay_listen", 8099) or 8099)
    routes: list[dict] = []
    credentials: dict[str, str] = {}
    for plan in sorted(plans, key=lambda p: (str(p.get("name") or p["id"]))):
        key = plan_key(plan)
        for route in plan_routes(plan, user):
            if relay:
                # The real key stays in the gateway; the instance gets a URL that
                # points at the local relay plus the placeholder as its credential.
                route = dict(route)
                route["base_url"] = (f"http://127.0.0.1:{relay_port}/mu-llm/"
                                     f"{token}/{route['route']}")
            routes.append(route)
            if route.get("keyless"):
                # DSH refuses to select a model whose provider has no API key at all
                # ("No API key for provider: …"), so the instance still gets the relay
                # placeholder; relay.json keeps key="" and the relay then sends no
                # Authorization header upstream (Kilo 401s on a bogus one).
                if relay and route.get("api_key_env"):
                    credentials[route["api_key_env"]] = RELAY_PLACEHOLDER
            elif relay:
                credentials[route["api_key_env"]] = RELAY_PLACEHOLDER
            elif key:
                credentials[route["api_key_env"]] = key
    if not routes:
        return "", {}
    default_route = None
    if default_plan:
        wanted = plan_key_env(default_plan["id"])
        default_route = next((r for r in routes if r["api_key_env"] == wanted), None)
    lines = [SANDBOX_PATCH,
             "# generated by dsh-muctl - model plans visible to this user",
             "# one pi-ai route per plan (displayName is the selector group name)",
             "# the built-in DeepSeek route keeps an empty catalog: plans are the only",
             "# way to pick a model, so nobody hits `no API key for deepseek-official`.",
             "- id: llm-deepseek",
             "  config:",
             "    models: []"]
    lines += ["- id: llm-pi-ai", "  config:", "    providers:"]
    for route in routes:
        lines += [
            f"      {route['route']}:",
            f"        displayName: {json.dumps(route['display_name'], ensure_ascii=False)}",
            *([] if (route.get("keyless") and not relay) or not route.get("api_key_env")
              else [f"        apiKeyEnv: {route['api_key_env']}"]),
            f"        api: {route['api']}",
            f"        baseURL: {json.dumps(route['base_url'])}",
        ]
        headers = dict(route["headers"])
        if headers:
            lines.append("        headers:")
            for name, value in headers.items():
                _v = value.replace("{session}", f"dsh-mu-{instance}")
                lines.append(f"          {name}: {json.dumps(_v, ensure_ascii=False)}")
        lines.append("        models:")
        for m in route["models"]:
            lines.append(f"          - id: {json.dumps(m['id'], ensure_ascii=False)}")
            if m.get("api") and m["api"] != route["api"]:
                lines.append(f"            api: {m['api']}")
            for key_name, field in (("name", "name"), ("contextWindow", "contextWindow"),
                                    ("maxTokens", "maxTokens")):
                if m.get(key_name):
                    _val = (json.dumps(m[key_name], ensure_ascii=False)
                            if isinstance(m[key_name], str) else m[key_name])
                    lines.append(f"            {field}: {_val}")
            efforts = m.get("reasoningEfforts")
            if efforts is False:
                lines.append("            reasoningEfforts: false")
            elif isinstance(efforts, dict) and efforts:
                lines.append("            reasoningEfforts:")
                for level, wire in efforts.items():
                    # pi-ai's README shows `off:` (no wire value) and `high: high`
                    # (explicit); only an empty value may be omitted.
                    if wire in ("", None):
                        lines.append(f"              {level}:")
                    else:
                        lines.append(f"              {level}: {json.dumps(str(wire), ensure_ascii=False)}")
            if m.get("inputModalities"):
                # pi-ai's model profile calls this `input` (PiAiModelProfile.input);
                # `inputModalities` is the built-in DeepSeek adapter's field and is
                # ignored on a pi-ai route -- declaring images here is what makes a
                # hand-declared vision model usable.
                lines.append("            input: [" +
                             ", ".join(m["inputModalities"]) + "]")
    if default_route:
        lines += ["- id: agent-default-model", "  config:",
                  f"    provider: {json.dumps(default_route['route'])}",
                  f"    model: {json.dumps(default_route['default_model'])}"]
    return "\n".join(lines) + "\n", credentials


def sync_instance_plans(st: dict, name: str) -> dict:
    """Render the plans this instance can use into its patch and credential store."""
    inst = st["instances"].get(name)
    if not inst:
        return {"plans": 0}
    user = str(inst.get("user") or name)
    plans = plans_for_user(user)
    patch_path = f"{INSTANCES_DIR}/{name}/home/.dsh-mu-provider.yml"
    if not plans:
        # No visible plans: still write the sandbox patch (the instance needs it),
        # and drop only the plan refs, never the rest of the credential store.
        os.makedirs(os.path.dirname(patch_path), exist_ok=True)
        atomic_write(patch_path, SANDBOX_PATCH + "\n", mode=0o644)
        with contextlib.suppress(OSError):
            os.chown(patch_path, int(inst.get("uid", 0)), int(inst.get("uid", 0)))
        cred_path = f"{INSTANCES_DIR}/{name}/home/.dsh/.credentials.yaml"
        if os.path.exists(cred_path):
            with contextlib.suppress(OSError):
                write_instance_refs(cred_path, {}, int(inst.get("uid", 0)))
        return {"plans": 0, "patch": None}
    default_id = str(st["config"].get("default_plan") or "")
    default = next((p for p in plans if p["id"] == default_id), None)
    if default is None and plans:
        default = sorted(plans, key=lambda p: str(p.get("name") or p["id"]))[0]
    # Mint the relay token on *this* state object before rendering; the renderer
    # only reads it, so patch and relay.json can never disagree again.
    relay = relay_enabled(st["config"])
    token = ensure_relay_token(st, name) if relay else ""
    if relay:
        save_state(st)
    yaml, credentials = plans_patch_yaml(user, name, default_plan=default,
                                         relay_token_value=token)
    if default:
        # Compatibility: conversations created before the plans existed still name
        # `deepseek-official`. With the relay on, that route also gets the
        # placeholder -- the instance must never hold the real key.
        _default_key = RELAY_PLACEHOLDER if relay else plan_key(default)
        if _default_key:
            credentials.setdefault("DEEPSEEK_API_KEY", _default_key)
    uid = int(inst.get("uid", 0))
    os.makedirs(os.path.dirname(patch_path), exist_ok=True)
    atomic_write(patch_path, yaml, mode=0o644)
    with contextlib.suppress(OSError):
        os.chown(patch_path, uid, uid)
    cred_path = f"{INSTANCES_DIR}/{name}/home/.dsh/.credentials.yaml"
    write_instance_refs(cred_path, credentials, uid)
    return {"plans": len(plans), "patch": patch_path, "default_plan": default["id"],
            "refs": sorted(credentials)}


def write_instance_refs(cred_path: str, extra: dict, uid: int) -> None:
    """Rewrite ONLY the `refs:` section of the harness credential store.

    That file also holds `records:` (the web client's browser-session signing
    secret) and possibly future sections. Dropping those logs the user out and the
    gateway's minted cookie stops verifying -- a silent 401 with no config error.
    """
    existing = read_text(cred_path) or ""
    kept_refs: list[tuple[str, str]] = []
    other_sections: list[str] = []
    section = None
    for line in existing.splitlines():
        top = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", line)
        if top and not line.startswith((" ", "\t")):
            section = top.group(1)
            if section != "refs":
                other_sections.append(line)
            continue
        if section == "refs":
            hit = re.match(r"^\s+([A-Z0-9_]+):\s*(\S+)\s*$", line)
            if hit and not hit.group(1).startswith("DSHMU_PLAN_"):
                kept_refs.append((hit.group(1), hit.group(2)))
        elif section is not None:
            other_sections.append(line)
    # Merge by name: writing a ref twice makes the harness refuse the whole file
    # (`DUPLICATE_KEY`) and the instance then never starts.
    merged: dict[str, str] = {}
    for ref, value in kept_refs:
        merged[ref] = value
    for ref, value in extra.items():
        merged[ref] = value            # an explicit value wins over the kept one
    refs_lines = ["refs:"]
    for ref, value in merged.items():
        refs_lines.append(f"  {ref}: {value}")
    head = ["version: 1"] if "version:" not in "\n".join(other_sections) else []
    lines = head + refs_lines + other_sections
    os.makedirs(os.path.dirname(cred_path), exist_ok=True)
    atomic_write(cred_path, "\n".join(lines) + "\n", mode=0o600)
    with contextlib.suppress(OSError):
        os.chown(cred_path, uid, uid)


_CRYPT_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# The final packing order from the sha-crypt specification ($6$): each triple is
# emitted as four base64 chars, little-endian, and the last byte yields two.
_CRYPT_ORDER = [
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26),
    (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
    (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57), (37, 58, 16),
    (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41),
]


def sha512_crypt(password: str, salt: str, rounds: int = 5000) -> str:
    """sha512-crypt (`$6$`) exactly as /etc/shadow stores it on Alpine.

    Pure Python on purpose: the `crypt` module was removed in 3.13, busybox
    `cryptpw` does not reproduce `$6$` here, and openssl is not guaranteed.
    """
    pwd = password.encode()
    slt = salt[:16].encode()
    # B = SHA512(password + salt + password)
    b = hashlib.sha512(pwd + slt + pwd).digest()
    # A = SHA512(password + salt + B repeated/sliced for len(password))
    a = hashlib.sha512()
    a.update(pwd)
    a.update(slt)
    remaining = len(pwd)
    while remaining > 0:
        a.update(b[: min(64, remaining)])
        remaining -= 64
    bits = len(pwd)
    while bits:
        a.update(b if bits & 1 else pwd)
        bits >>= 1
    d_a = a.digest()
    # P = SHA512(password repeated len(password) times), repeated to len(password)
    dp = hashlib.sha512(pwd * len(pwd)).digest()
    p_str = (dp * (len(pwd) // 64 + 1))[: len(pwd)]
    # S = SHA512(salt repeated (16 + d_a[0]) times), repeated to len(salt)
    ds = hashlib.sha512(slt * (16 + d_a[0])).digest()
    s_str = (ds * (len(slt) // 64 + 1))[: len(slt)]
    prev = d_a
    for i in range(rounds):
        c = hashlib.sha512()
        c.update(p_str if i & 1 else prev)
        if i % 3:
            c.update(s_str)
        if i % 7:
            c.update(p_str)
        c.update(prev if i & 1 else p_str)
        prev = c.digest()
    out = []
    for b2, b1, b0 in _CRYPT_ORDER:
        w = (prev[b2] << 16) | (prev[b1] << 8) | prev[b0]
        out.append(_CRYPT_B64[w & 0x3F])
        out.append(_CRYPT_B64[(w >> 6) & 0x3F])
        out.append(_CRYPT_B64[(w >> 12) & 0x3F])
        out.append(_CRYPT_B64[(w >> 18) & 0x3F])
    w = prev[63]
    out.append(_CRYPT_B64[w & 0x3F])
    out.append(_CRYPT_B64[(w >> 6) & 0x3F])
    return "$6$" + salt[:16] + "$" + "".join(out)


def verify_shadow_password(stored: str, password: str) -> bool:
    """Check a password against a /etc/shadow hash without external tools."""
    if not stored.startswith("$6$"):
        return False
    parts = stored.split("$")
    if len(parts) < 4:
        return False
    salt = parts[2]
    return sha512_crypt(password, salt) == stored


def provider_package_to_defn(pkg: dict) -> dict:
    """Turn a validated package into the internal provider definition."""
    # One pi-ai route per distinct base URL; `api` is per model, which is what
    # lets one group carry chat-completions, responses and messages models.
    by_base: dict[str, list[dict]] = {}
    for m in pkg["models"]:
        by_base.setdefault(m["base_url"], []).append(m)
    first_base = next(iter(by_base))
    models = []
    for base, rows in by_base.items():
        for m in rows:
            entry = {"id": m["id"], "api": m["api"]}
            if m.get("name"):
                entry["name"] = m["name"]
            if m.get("contextWindow"):
                entry["contextWindow"] = m["contextWindow"]
            if m.get("maxTokens"):
                entry["maxTokens"] = m["maxTokens"]
            if m.get("inputModalities"):
                entry["inputModalities"] = list(m["inputModalities"])
            models.append(entry)
    return {
        "kind": "pi-ai",
        "route": pkg["id"],
        "api": pkg["models"][0]["api"] if pkg["models"] else "openai-completions",
        "base_url": first_base,
        "credentials_ref": "DEEPSEEK_API_KEY",
        "model": pkg.get("default_model") or (models[0]["id"] if models else ""),
        "models": models,
        "display_name": pkg.get("label") or pkg["id"],
        "headers": dict(pkg.get("headers") or {}),
        "package": pkg,
    }


def package_for_display(pkg: dict) -> dict:
    """Turn a normalised package back into the documented `dsh-mu.provider/v1` shape.

    `export` must emit what the spec describes (`input_modalities`), otherwise a
    export -> edit -> import round-trip loses the modalities.
    """
    out = json.loads(json.dumps(pkg))
    for model in out.get("models") or []:
        for internal, documented in (("inputModalities", "input_modalities"),
                                     ("contextWindow", "context_window"),
                                     ("maxTokens", "max_tokens")):
            value = model.pop(internal, None)
            if value is not None:
                model[documented] = value
        # keep the file close to what a human wrote: no normalised defaults
        for key in ("api", "base_url"):
            if key in model and model[key] == out.get(key):
                model.pop(key, None)
    return out


def save_provider_package(pkg: dict) -> str:
    """Persist a package next to the custom providers (one JSON file per id)."""
    target = f"{PROVIDERS_DIR}/{pkg['id']}.json"
    os.makedirs(PROVIDERS_DIR, exist_ok=True)
    atomic_write(target, json.dumps(pkg, indent=2, sort_keys=True) + "\n", mode=0o644)
    return target


def fill_from_preset(pkg: dict) -> dict:
    """Let an imported package inherit the built-in preset's *capability* metadata.

    An operator-imported package carries their base_url/key/headers, but a bundle
    upgrade can learn new facts about the vendor (e.g. DeepSeek's reasoning levels).
    Only fields the package leaves empty are filled; nothing the operator set moves.
    """
    preset = PROVIDER_PRESETS.get(str(pkg.get("id") or ""))
    if not preset:
        return pkg
    by_id = {str(m.get("id")): m for m in preset.get("models") or []}
    for row in pkg.get("models") or []:
        src = by_id.get(str(row.get("id")))
        if not src:
            continue
        for key in ("reasoningEfforts", "inputModalities"):
            if not row.get(key) and src.get(key):
                row[key] = src[key]
    return pkg


# ---------------------------------------------------------------- online templates
# Provider templates live in the project repo (provider-packages/). An operator picks
# one from the admin UI instead of hand-writing JSON. Everything downloaded is
# validated by validate_provider_package() before it lands in PROVIDERS_DIR, and an
# import never overwrites an existing package unless explicitly forced.
DEFAULT_TEMPLATE_BASE = ("https://raw.githubusercontent.com/dainan13/dsh-mu/main/"
                         "provider-packages")


def provider_template_base(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get("provider_template_base") or DEFAULT_TEMPLATE_BASE).rstrip("/")


def _template_fetch(url: str, timeout: int = 20) -> bytes:
    """GET a template file. Supports https:// and file:// (file:// keeps this
    testable on a machine without internet)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        with open(parsed.path, "rb") as fh:
            return fh.read()
    if parsed.scheme != "https":
        raise ValueError(f"only https:// and file:// template sources are allowed, got {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "dsh-mu-template/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status} for {url}")
        return resp.read()


def _github_api_from_raw(base: str) -> str | None:
    """Turn a raw.githubusercontent.com directory URL into its GitHub contents API URL."""
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", base)
    if not m:
        return None
    owner, repo, ref, path = m.groups()
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"


def provider_template_index(cfg: dict | None = None) -> list[dict]:
    """List available templates. Prefers index.json (no API limits); falls back to
    the GitHub contents API when the directory has no index."""
    base = provider_template_base(cfg)
    try:
        raw = _template_fetch(f"{base}/index.json")
        doc = json.loads(raw.decode("utf-8"))
        rows = doc.get("templates") if isinstance(doc, dict) else doc
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("id") or "").strip()
            if not name:
                continue
            out.append({"name": name, "label": row.get("label") or name,
                        "description": row.get("description") or "",
                        "api": row.get("api") or "", "models": row.get("models") or 0})
        if out:
            return out
    except Exception as exc:  # noqa: BLE001 - fall through to the API listing
        index_error = exc
    else:
        index_error = None

    api = _github_api_from_raw(base)
    if not api:
        raise RuntimeError(f"template index unavailable at {base}/index.json: {index_error}")
    doc = json.loads(_template_fetch(api).decode("utf-8"))
    if not isinstance(doc, list):
        raise RuntimeError(f"GitHub did not return a directory listing for {base}")
    out = []
    for row in doc:
        name = str(row.get("name") or "")
        if not name.endswith(".json") or name == "index.json":
            continue
        out.append({"name": name[:-5], "label": name[:-5], "description": "",
                    "api": "", "models": 0})
    return out


def fetch_provider_template(name: str, cfg: dict | None = None) -> tuple[dict, str]:
    """Download and validate one template. Returns (package, sha256)."""
    name = str(name or "").strip()
    if not name or "/" in name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("template name may only contain letters, digits, dot, dash, underscore")
    base = provider_template_base(cfg)
    raw = _template_fetch(f"{base}/{name}.json")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"template {name}.json is not valid JSON: {exc}") from exc
    pkg, problems = validate_provider_package(doc)
    if problems:
        raise ValueError(f"template {name}.json failed validation: " + "; ".join(problems[:3]))
    return pkg, digest


def import_provider_template(name: str, cfg: dict | None = None,
                             force: bool = False) -> dict:
    pkg, digest = fetch_provider_template(name, cfg)
    existing = load_provider_package(pkg["id"])
    if existing and not force:
        raise ValueError(f"provider {pkg['id']} already exists "
                         f"(use --force to replace it)")
    save_provider_package(pkg)
    return {"package": pkg, "sha256": digest, "replaced": bool(existing)}


def load_provider_package(pid: str) -> dict | None:
    """Load a package and normalise it (each model carries its own api/base_url)."""
    path = f"{PROVIDERS_DIR}/{pid}.json"
    text = read_text(path)
    if not text:
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    pkg, problems = validate_provider_package(doc)
    if problems:
        return None
    return fill_from_preset(pkg)


def preset_as_package(name: str) -> dict | None:
    """Expose a built-in preset in package shape (so plans can point at presets)."""
    defn = PROVIDER_PRESETS.get(name)
    if not defn:
        return None
    base = defn.get("base_url") or {
        "deepseek": "https://api.deepseek.com/anthropic",
        "opencode-go": "https://opencode.ai/inference/go/v1",
        "opencode": "https://opencode.ai/inference/anthropic",
    }.get(name)
    api = defn.get("api") or ("anthropic-messages" if defn.get("kind") == "deepseek"
                              else "openai-completions")
    if not base:
        return None
    models = []
    for m in defn.get("models") or []:
        row = {"id": m.get("id"), "api": m.get("api") or api, "base_url": base}
        for src, dst in (("name", "name"), ("contextWindow", "contextWindow"),
                         ("maxTokens", "maxTokens")):
            if m.get(src):
                row[dst] = m[src]
        # Loader whitelist: reasoning levels must survive, or a catalog entry that
        # declares them never reaches the instance patch.
        efforts = m.get("reasoningEfforts", m.get("reasoning_efforts"))
        if efforts is not None:
            row["reasoningEfforts"] = efforts if isinstance(efforts, (dict, bool)) \
                else parse_efforts(str(efforts))
        if m.get("inputModalities") or m.get("input"):
            row["inputModalities"] = m.get("inputModalities") or m.get("input")
        models.append(row)
    if not models:
        models = [{"id": defn.get("model") or "default", "api": api, "base_url": base}]
    return {"schema": PROVIDER_PACKAGE_SCHEMA, "id": name, "label": defn.get("display_name") or name,
            "keyless": not bool(defn.get("credentials_ref")),
            "auth": {"type": "x-api-key" if api == "anthropic-messages" else "bearer"},
            "headers": dict(defn.get("headers") or {}), "base_url": base, "api": api,
            "default_model": defn.get("model") or models[0]["id"], "models": models}


def list_provider_packages() -> dict[str, dict]:
    out = {}
    try:
        names = sorted(os.listdir(PROVIDERS_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        pid = name[:-5]
        pkg = load_provider_package(pid)
        if pkg:
            out[pid] = pkg
    return out


def cmd_plan(args):
    """Team plans and personal plans: provider + one credential + a display name."""
    ensure_root()
    st = load_state()
    action = args.action
    if action == "list":
        plans = list_plans()
        if args.json:
            print(json.dumps({"plans": plans, "default": st["config"].get("default_plan") or ""},
                             indent=2, sort_keys=True, ensure_ascii=False))
            return 0
        if not plans:
            print("no plans yet (dsh-muctl plan add --name ... --provider ... --key-file ...)")
            return 0
        default = str(st["config"].get("default_plan") or "")
        for pid, plan in sorted(plans.items(), key=lambda kv: str(kv[1].get("name") or kv[0])):
            scope = "团队" if str(plan.get("owner") or "*") == "*" else f"个人:{plan['owner']}"
            mark = " *默认" if pid == default else ""
            print(f"{pid:18} {str(plan.get('name') or ''):24} {plan.get('provider',''):16} "
                  f"{scope:12} key={'yes' if plan.get('has_key') else 'no':3} "
                  f"models={len(plan.get('models') or []) or 'all':4} "
                  f"{'on' if plan.get('enabled', True) else 'off'}{mark}")
        return 0
    if action == "add":
        if not args.name or not args.provider:
            die("plan add needs --name and --provider")
        pkg = load_provider_package(args.provider) or preset_as_package(args.provider)
        if not pkg:
            die(f"no such provider: {args.provider} (dsh-muctl provider list)")
        pid = plan_id_for(args.name)
        owner = str(args.user or "*")
        visible = [v.strip() for v in (args.visible or "").replace(",", " ").split() if v.strip()]
        if owner != "*" and not visible:
            visible = [owner]
        models = [m.strip() for m in (args.models or "").replace(",", " ").split() if m.strip()]
        known = {m["id"] for m in pkg["models"]}
        bad = [m for m in models if m not in known]
        if bad:
            die(f"model(s) not in provider {args.provider}: {', '.join(bad)}")
        key = None
        if args.key_file:
            key = (read_text(args.key_file) or "").strip()
        elif args.key:
            key = args.key.strip()
        if not key:
            die("plan add needs a credential: --key-file <file> or --key <key>")
        plan = {"schema": PLAN_SCHEMA, "id": pid, "name": args.name.strip(),
                "provider": args.provider, "owner": owner,
                "visible_to": visible or ["*"], "enabled": True,
                "models": models, "default_model": args.model or ""}
        saved = save_plan(plan, key=key)
        if args.default:
            if owner != "*":
                die("only team plans can be the platform default")
            st["config"]["default_plan"] = saved["id"]
            save_config(st["config"])
        sync_all(st)
        info(f"plan {saved['id']} saved ({len(plan_routes(saved, 'x'))} route(s)); "
             f"restart the affected instances to apply")
        return 0
    if action in ("rm", "delete"):
        if not args.id:
            die("plan rm needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        with contextlib.suppress(OSError):
            os.unlink(f"{PLANS_DIR}/{args.id}.json")
        with contextlib.suppress(OSError):
            os.unlink(plan_key_path(args.id))
        if str(st["config"].get("default_plan") or "") == args.id:
            st["config"].pop("default_plan", None)
            save_config(st["config"])
        sync_all(st)
        info(f"plan {args.id} ({plan.get('name')}) removed")
        return 0
    if action in ("enable", "disable"):
        if not args.id:
            die(f"plan {action} needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        plan["enabled"] = action == "enable"
        save_plan(plan)
        sync_all(st)
        info(f"plan {args.id} {'enabled' if plan['enabled'] else 'disabled'}")
        return 0
    if action == "key":
        if not args.id:
            die("plan key needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        if args.key_file:
            key = (read_text(args.key_file) or "").strip()
        elif getattr(args, "key", None):
            key = str(args.key).strip()
        else:
            key = getpass.getpass(f"credential for plan {args.id}: ").strip()
        if not key:
            die("the credential must not be empty")
        plan["key"] = key
        plan["has_key"] = True
        save_plan(plan)
        sync_all(st)
        info(f"plan {args.id} ({plan.get('name')}): credential rotated "
             f"(…{key[-4:]}); restart the instances that use it")
        return 0
    if action == "default":
        if not args.id:
            die("plan default needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        if str(plan.get("owner") or "*") != "*":
            die("only team plans can be the platform default")
        st["config"]["default_plan"] = args.id
        save_config(st["config"])
        sync_all(st)
        info(f"default plan: {args.id} ({plan.get('name')})")
        return 0
    if action == "show":
        if not args.id:
            die("plan show needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        print(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    if action == "probe":
        if not args.id:
            die("plan probe needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        res = plan_probe_models(plan)
        if not res.get("ok"):
            die(f"probe failed: {res.get('error')}")
        info(f"plan {args.id}: {len(res['available'])} usable model(s) "
             f"out of {res['catalog']} in the catalog (provider listed {res['discovered']})")
        if res["unknown"]:
            info("not in the provider catalog (add them there first): "
                 + ", ".join(res["unknown"][:8]))
        eff = plan_effective_models(load_plan(args.id) or plan)
        info("currently selected: " + (", ".join(eff) if eff else "(none)"))
        info("restart the instances that use this plan: dsh-muctl instance restart <name>")
        return 0
    if action == "models":
        if not args.id:
            die("plan models needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        if args.all:
            res = set_plan_models(plan, [], select_all_available=True)
        elif args.clear:
            res = set_plan_models(plan, [], clear=True)
        elif args.set:
            res = set_plan_models(plan, [m.strip() for m in args.set.split(",") if m.strip()])
        else:
            catalog = [m["id"] for m in plan_package(plan)["models"]] if plan_package(plan) else []
            available = plan_available_set(plan)
            info("plan    : " + str(plan.get("id")))
            info("probed  : " + ("yes" if available is not None else "no (run: plan probe)"))
            for m in catalog:
                mark = "*" if (not plan.get("models") or m in plan["models"]) else " "
                usable = "" if available is None else ("  ok" if m in available else "  UNUSABLE")
                info(f"  {mark} {m}{usable}")
            return 0
        if not res.get("ok"):
            detail = res.get("unusable") or res.get("unknown") or []
            die(f"{res.get('error')}" + (f": {', '.join(detail[:8])}" if detail else ""))
        info(f"plan {args.id}: {res['count']} model(s) selected")
        info("restart the instances that use this plan: dsh-muctl instance restart <name>")
        return 0
    if action == "usage":
        if not args.id:
            die("plan usage needs the plan id")
        plan = load_plan(args.id) or die(f"no such plan: {args.id}")
        res = plan_probe_usage(plan)
        if not res.get("ok"):
            die(f"usage probe failed: {res.get('error')}")
        for window in res.get("windows") or []:
            info(f"{window.get('name') or window.get('id')}: "
                 f"{window.get('remaining')}/{window.get('limit')} remaining")
        return 0
    if action == "sync":
        for name in st["instances"]:
            res = sync_instance_plans(st, name)
            info(f"{name}: {res.get('plans', 0)} plan(s)"
                 + (f", default {res['default_plan']}" if res.get("default_plan") else ""))
        return 0
    die(f"unknown plan action: {action}")


def cmd_template(args):
    """Browse and import provider templates published in the project repo."""
    ensure_root()
    cfg = load_config()
    action = args.action
    if action == "source":
        info("template source: " + provider_template_base(cfg))
        if args.url:
            cfg["provider_template_base"] = args.url.rstrip("/")
            save_config(cfg)
            info("updated: " + cfg["provider_template_base"])
        return 0
    if action == "list":
        info("source: " + provider_template_base(cfg))
        try:
            rows = provider_template_index(cfg)
        except Exception as exc:  # noqa: BLE001 - offline / 404 / bad JSON
            die(f"cannot read the template index: {exc}")
        have = set(list_provider_packages())
        if not rows:
            info("no templates found")
            return 0
        for row in rows:
            state = "imported" if row["name"] in have or row["name"] == "index" else ""
            extra = f" [{row['api']}]" if row["api"] else ""
            info(f"  {row['name']:<18} {row['label']}{extra} {state}")
            if row.get("description"):
                info(f"    {row['description']}")
        info("import with: dsh-muctl template import <name>")
        return 0
    if action == "import":
        if not args.name:
            die("usage: dsh-muctl template import <name> [--force]")
        try:
            res = import_provider_template(args.name, cfg, force=args.force)
        except Exception as exc:  # noqa: BLE001
            die(f"import failed: {exc}")
        pkg = res["package"]
        audit_write("provider.template.import", target=pkg["id"],
                    detail={"sha256": res["sha256"], "replaced": res["replaced"]})
        info(f"imported provider {pkg['id']} ({pkg.get('label')}), "
             f"{len(pkg.get('models') or [])} model(s)")
        info(f"sha256 {res['sha256']}")
        if res["replaced"]:
            info("an existing package with this id was replaced")
        info("create a plan that uses it: dsh-muctl plan add --provider "
             f"{pkg['id']} --name <name> --key <key>")
        info("restart the instances that will use the new plan")
        return 0
    die(f"unknown template action: {action}")


def cmd_mount(args):
    """Manage host directories bind-mounted into an instance jail."""
    ensure_root()
    st = load_state()
    action = args.action
    if action == "list":
        names = [args.instance] if args.instance else sorted(st["instances"])
        any_rows = False
        for name in names:
            inst = st["instances"].get(name)
            if not inst:
                die(f"no such instance: {name}")
            rows = parse_mount_specs(inst)
            info(f"{name}: {len(rows)} mount(s)")
            for r in rows:
                exists = "ok" if os.path.isdir(r["host"]) else "MISSING"
                info(f"  {r['host']} -> {r['jail']} [{r['mode']}] ({exists})")
            any_rows = any_rows or bool(rows)
        if not any_rows:
            info("no mounts configured; add one with: dsh-muctl mount add <host> <jail> [--rw]")
        return 0
    if not args.instance:
        die("mount add/rm needs --instance <name> (or --all to apply to every instance)")
    targets = (sorted(st["instances"]) if args.all else [args.instance])
    if action in ("add", "set"):
        if not args.host:
            die("usage: dsh-muctl mount add <host-path> [jail-path] [--rw] --instance <name>")
        jail = args.jail or args.host
        for name in targets:
            rows = [dict(r) for r in parse_mount_specs(st["instances"][name])]
            rows = [r for r in rows if r["jail"] != "/" + jail.strip("/")]
            rows.append({"host": args.host, "jail": jail,
                         "mode": "rw" if args.rw else "ro"})
            set_instance_mounts(name, [(r["host"], r["jail"], r["mode"]) for r in rows])
            info(f"{name}: {jail} mounted from {args.host} "
                 f"({'rw' if args.rw else 'ro'})")
        info("restart the affected instances to apply: dsh-muctl instance restart <name>")
        return 0
    if action in ("rm", "delete"):
        if not args.host:
            die("usage: dsh-muctl mount rm <host-path> --instance <name>")
        for name in targets:
            rows = [r for r in parse_mount_specs(st["instances"][name])
                    if r["host"] != os.path.abspath(args.host)]
            set_instance_mounts(name, [(r["host"], r["jail"], r["mode"]) for r in rows])
            info(f"{name}: removed {args.host} ({len(rows)} left)")
        info("restart the affected instances to apply")
        return 0
    die(f"unknown mount action: {action}")


def cmd_provider_test(args):
    """Probe a provider endpoint with the instance's (or a supplied) key."""
    ensure_root()
    st = load_state()
    if args.instance:
        resolved = resolved_provider(st, args.instance)
        prov = resolved["provider"]
        key = args.key or provider_credentials(st, args.instance)[0]
        source = resolved["key_source"] if not args.key else "argument"
    else:
        catalog = all_providers()
        prov = json.loads(json.dumps(catalog.get(args.name) or die(f"no such provider: {args.name}")))
        key = args.key or str(st["config"].get("model_api_key") or "") or None
        source = "argument" if args.key else ("platform" if key else "none")
        prov["preset"] = args.name
    if not key:
        info("warning: no credential available for this probe; the endpoint may reject it")
    result = provider_probe(prov, key, model=args.model or prov.get("model"))
    result["key_source"] = source
    result["provider"] = prov.get("preset") or prov.get("route") or args.name
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        state = "OK " if result["ok"] else "FAIL"
        print(f"{state} {result['provider']} model={result['model']} "
              f"status={result['status']} {result['ms']}ms")
        print(f"     endpoint: {result['endpoint']}")
        print(f"     key: {source}" + (f" (len {len(key)})" if key else " (none)"))
        print(f"     detail: {result['detail']}")
    return 0 if result["ok"] else 1


def cmd_instance_patch(args):
    resolved = resolved_provider(load_state(), args.name)
    if args.json:
        print(json.dumps({k: v for k, v in resolved.items() if k != "patch"}, indent=2, sort_keys=True))
        return 0
    print(f"# {resolved['patch_path']}")
    print(f"# preset={resolved['provider'].get('preset')} "
          f"model={resolved['provider'].get('model')} "
          f"key_source={resolved['key_source']} key_len={resolved['key_length']}")
    print(resolved["patch"] or "# (no patch file: the instance uses DSH defaults)")
    return 0


def cmd_bootstrap(args):
    """Prepare the host administrator account(s) the platform delegates to."""
    ensure_root()
    st = load_state()
    report = ensure_system_admins(st)
    # Persist config so keys added by a newer version land in dsh-mu.conf.
    save_config(st["config"])
    sync_all(load_state())
    for entry in report:
        if entry.get("demoted"):
            print(f"  demoted {entry['user']}: {entry['detail']}")
        elif entry.get("password_ready"):
            print(f"  {entry['user']}: ready (uses the operating system password)")
        else:
            print(f"  {entry['user']}: NOT ready -- {entry['detail']}")
            print(f"      fix: passwd {entry['user']}")
    if args.json:
        print(json.dumps({"system_admins": report,
                          "configured": system_admin_names(st["config"])},
                         indent=2, sort_keys=True))
        return 0 if all(e.get("password_ready") or e.get("demoted") for e in report) else 1
    print()
    print("administrators are the host's own accounts (ADMIN_SYSTEM_USERS in dsh-mu.conf);")
    print("log in at /login with that username and its system password.")
    return 0 if all(e.get("password_ready") or e.get("demoted") for e in report) else 1


def cmd_admin(args):
    ensure_root()
    st = load_state()
    if args.action == "install":
        ok = install_admin(st["config"], restart=args.reload)
        info("admin backend installed" if ok else "admin backend installed, but the binary is missing")
        return 0
    if args.action == "token":
        if args.rotate and os.path.exists(ADMIN_TOKEN_PATH):
            os.unlink(ADMIN_TOKEN_PATH)
        path = ensure_admin_token()
        info(f"{path}: {'rotated' if args.rotate else 'present'}; restart the gateway and admin "
             f"service to pick it up")
        return 0
    if args.action == "status":
        res = run(["/sbin/rc-service", "dsh-mud", "status"], check=False, capture=True)
        state = ((res.stdout or "") + (res.stderr or "")).strip().splitlines()
        print(state[-1] if state else "unknown")
        listen = str(st["config"].get("admin_listen") or "127.0.0.1:8787")
        host, _, port = listen.partition(":")
        alive = _http_ok(f"http://{host or '127.0.0.1'}:{port or 8787}/api/health",
                         headers=({"X-DSH-MU-Admin": (read_text(ADMIN_TOKEN_PATH) or "").strip()}
                                  if os.path.exists(ADMIN_TOKEN_PATH) else None))
        print(f"http://{listen}/api/health -> {alive[1]}")
        return 0 if alive[0] else 1
    if args.action == "restart":
        render_admin_service(st["config"], register=False)
        run(["/sbin/rc-service", "dsh-mud", "restart"], check=False)
        info("admin backend restarted")
        return 0
    die(f"unknown admin action: {args.action}")


def cmd_sync_state(args):
    """Reconcile stored instance state with what is actually listening."""
    ensure_root()
    st = load_state()
    changes = []
    for name, inst in sorted(st["instances"].items()):
        port = int(inst.get("port", 0))
        # "running" means the service is up: the harness may still be binding its
        # port for a few seconds right after a (re)start, which must not look
        # like a stopped instance.
        alive = port_in_use(port) or service_started(name)
        target = "running" if alive else "stopped"
        declared = inst.get("state", "?")
        if declared != target:
            changes.append({"instance": name, "from": declared, "to": target, "port": port})
            inst["state"] = target
    if changes:
        save_state(st)
    sync_all(st)
    if getattr(args, "json", False):
        print(json.dumps({"changes": changes, "instances": len(st["instances"])}, indent=2, sort_keys=True))
        return 0
    if not changes:
        info(f"state already matches reality ({len(st['instances'])} instance(s))")
    else:
        for c in changes:
            info(f"instance {c['instance']}: {c['from']} -> {c['to']} (port {c['port']})")
        info(f"corrected {len(changes)} instance state(s); gateway views re-rendered")
    return 0


def cmd_selftest(args):
    ensure_root()
    script = f"{LIB}/tests/selftest.mjs"
    if not os.path.exists(script):
        die(f"selftest script missing: {script}")
    argv = ["node", script]
    if args.host:
        argv += ["--host", args.host]
    if args.user:
        argv += ["--user", args.user]
    if args.password:
        argv += ["--pass", args.password]
    if args.ws_hold:
        argv += ["--ws-hold", str(args.ws_hold)]
    os.execvp("node", argv)


def cmd_acceptance(args):
    """Run the platform acceptance suite (tests/acceptance.sh) from the CLI."""
    ensure_root()
    script = f"{LIB}/tests/acceptance.sh"
    if not os.path.exists(script):
        die(f"acceptance script missing: {script}")
    positional = list(getattr(args, "rest", []) or [])
    instance = args.instance or (positional[0] if len(positional) > 0 else None)
    user = args.user or (positional[1] if len(positional) > 1 else None)
    password = args.password or (positional[2] if len(positional) > 2 else None)
    argv = ["sh", script]
    if instance:
        argv.append(instance)
    if user:
        argv.append(user)
    if password:
        argv.append(password)
    if args.fresh:
        fresh = f"{LIB}/tests/fresh-machine.py"
        if not os.path.exists(fresh):
            die(f"fresh-machine script missing: {fresh}")
        os.execvp("python3", ["python3", fresh, "--user", user or "smoke",
                             "--password", password or ""])
    os.execvp("sh", argv)


def migration_plan() -> dict:
    """What `dsh-muctl migrate` would do, without doing it."""
    raw = read_text(STATE_PATH)
    plan = {"state_path": STATE_PATH, "conf_path": CONF_PATH, "exists": raw is not None,
            "schema": None, "actions": []}
    if raw is None:
        plan["actions"].append("create state (nothing to migrate)")
        return plan
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        plan["actions"].append(f"REFUSE: {STATE_PATH} is not valid JSON ({exc})")
        return plan
    plan["schema"] = data.get("schema")
    if int(data.get("schema") or 1) < STATE_SCHEMA:
        plan["actions"].append(f"upgrade state schema {data.get('schema') or 1} -> {STATE_SCHEMA}")
    if "config" in data:
        keys = sorted(data["config"].keys())
        plan["actions"].append(f"move {len(keys)} legacy key(s) from state.json into {CONF_PATH}")
        plan["legacy_keys"] = keys
    if not os.path.exists(CONF_PATH):
        plan["actions"].append(f"write {CONF_PATH} from defaults")
    for name, inst in sorted((data.get("instances") or {}).items()):
        if not inst.get("provider"):
            continue
        preset = (inst.get("provider") or {}).get("preset")
        if preset and preset not in all_providers():
            plan["actions"].append(
                f"instance {name} references unknown provider {preset!r} "
                f"(define it in {PROVIDERS_PATH} or switch it)")
    if not plan["actions"]:
        plan["actions"].append("nothing to do: state and configuration are current")
    return plan


def cmd_migrate(args):
    ensure_root()
    plan = migration_plan()
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    print(f"state:  {plan['state_path']}"
          + (f" (schema {plan['schema']})" if plan["exists"] else " (absent)"))
    print(f"config: {plan['conf_path']}"
          + ("" if not os.path.exists(plan["conf_path"]) else " (present)"))
    for action in plan["actions"]:
        print(f"  - {action}")
    if args.dry_run:
        print("dry run: nothing was changed")
        return 0
    if any(a.startswith("REFUSE") for a in plan["actions"]):
        die("refusing to migrate: fix the problem above first")
    load_state()           # performs the migration and rewrites state.json
    save_config(load_state()["config"])
    sync_all(load_state())
    info("migration applied"
         + ("" if plan["actions"][0].startswith("nothing") else "; state and conf rewritten"))
    return 0


def gateway_config_valid() -> tuple[bool, str]:
    """Run `nginx -t` against the rendered gateway config *inside* the jail."""
    if str(load_config().get("gateway_mode") or "jail") != "jail":
        res = run(["/usr/sbin/nginx", "-t"], check=False, capture=True)
        return res.returncode == 0, ((res.stderr or "") + (res.stdout or "")).strip()[-600:]
    res = run([JAIL if os.path.exists(JAIL) else "dsh-mu-jail",
               "--conf", GATEWAY_CONF, "gateway", "--",
               "/usr/sbin/nginx", "-t", "-c", f"{GATEWAY_CONF_IN_JAIL}/nginx.conf"],
              check=False, capture=True)
    text = ((res.stderr or "") + (res.stdout or "")).strip()
    return res.returncode == 0, text[-600:]


def cmd_tls(args):
    """Manage the gateway certificate and the HTTPS switch."""
    action = args.action
    cfg = load_config()
    os.makedirs(TLS_DIR, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(TLS_DIR, 0o750)

    if action == "status":
        info("TLS dir    : " + TLS_DIR)
        info("enabled    : " + ("yes" if tls_enabled(cfg) else "no"))
        info("keep http  : " + ("yes" if cfg.get("tls_keep_http", True) else "no"))
        info("redirect   : " + ("yes" if cfg.get("tls_redirect", False) else "no"))
        if os.path.isfile(TLS_CERT):
            meta = tls_cert_info()
            info(f"certificate: {TLS_CERT}")
            info(f"  subject  : {meta['subject'] or '(unknown)'}")
            info(f"  issuer   : {meta['issuer'] or '(unknown)'}")
            info(f"  notAfter : {meta['not_after'] or '(unknown)'}"
                 + (f"  ({meta['days_left']} days left)" if meta["days_left"] is not None else ""))
        else:
            info(f"certificate: missing ({TLS_CERT})")
        info("key        : " + (TLS_KEY if os.path.isfile(TLS_KEY) else f"missing ({TLS_KEY})"))
        return 0

    if action == "self-sign":
        name = args.cn or detect_hostname() or "dsh-mu"
        extra = [n for n in (args.hosts or []) if n]
        san = [f"DNS:{name}"] + [f"DNS:{n}" for n in extra]
        san += [f"IP:{ip}" for ip in (local_ipv4_addresses() or [])]
        san.append("IP:127.0.0.1")
        san = list(dict.fromkeys(san))
        days = int(args.days or 825)
        tmp_key = TLS_KEY + ".new"
        tmp_crt = TLS_CERT + ".new"
        res = run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                   "-keyout", tmp_key, "-out", tmp_crt, "-days", str(days),
                   "-subj", f"/CN={name}",
                   "-addext", "subjectAltName=" + ",".join(san),
                   "-addext", "basicConstraints=critical,CA:FALSE",
                   "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
                   "-addext", "extendedKeyUsage=serverAuth"],
                  check=False, capture=True)
        if res.returncode != 0 or not os.path.isfile(tmp_crt):
            detail = ((res.stderr or "") + (res.stdout or "")).strip()[:300]
            die(f"openssl could not create a certificate: {detail or 'unknown error'}")
        os.replace(tmp_crt, TLS_CERT)
        os.replace(tmp_key, TLS_KEY)
        os.chmod(TLS_CERT, 0o644)
        os.chmod(TLS_KEY, 0o600)
        info(f"self-signed certificate created for {name} ({days} days)")
        info("SAN: " + ", ".join(san))
        info("next: dsh-muctl tls enable && dsh-muctl gateway restart")
        return 0

    if action == "import":
        if not args.cert or not args.key:
            die("tls import needs --cert <fullchain.pem> and --key <privkey.pem>")
        for path in (args.cert, args.key):
            if not os.path.isfile(path):
                die(f"no such file: {path}")
        crt = (read_text(args.cert) or "").strip()
        key = (read_text(args.key) or "").strip()
        if "BEGIN CERTIFICATE" not in crt:
            die(f"{args.cert} does not look like a PEM certificate chain")
        if "PRIVATE KEY" not in key:
            die(f"{args.key} does not look like a PEM private key")
        atomic_write(TLS_CERT, crt + "\n", mode=0o644)
        atomic_write(TLS_KEY, key + "\n", mode=0o600)
        info(f"certificate imported into {TLS_DIR}")
        return 0

    if action in ("enable", "disable"):
        want = action == "enable"
        if want and not tls_cert_ready(cfg):
            die(f"no usable certificate in {TLS_DIR}; run `dsh-muctl tls self-sign` "
                f"or `dsh-muctl tls import --cert … --key …` first")
        st = load_state()
        st["config"] = cfg
        cfg["tls_enabled"] = want
        save_config(cfg)
        sync_all(st)
        # Validate before anyone restarts: a rejected nginx config would take the
        # whole gateway down, and the old one is always still valid.
        ok, detail = gateway_config_valid()
        if not ok:
            cfg["tls_enabled"] = not want
            save_config(cfg)
            st["config"] = cfg
            sync_all(st)
            die("nginx rejected the new configuration, rolled back:\n" + detail)
        info("HTTPS " + ("enabled (443)" if want else "disabled"))
        info("run: dsh-muctl gateway restart")
        return 0

    if action in ("redirect", "keep-http"):
        key = "tls_redirect" if action == "redirect" else "tls_keep_http"
        value = str(args.value or "").lower() in ("1", "on", "yes", "true", "enable")
        cfg[key] = value
        if key == "tls_redirect" and value:
            cfg["tls_keep_http"] = False
        if key == "tls_keep_http" and value:
            cfg["tls_redirect"] = False
        st = load_state()
        st["config"] = cfg
        save_config(cfg)
        sync_all(st)
        info(f"{key} = {value}")
        info("run: dsh-muctl gateway restart")
        return 0

    die(f"unknown tls action: {action}")


def cmd_config(args):
    ensure_root()
    st = load_state()
    cfg = st["config"]
    if getattr(args, "write_default", False):
        info(f"wrote {write_default_conf()}")
        return
    if args.key is None:
        info(f"effective configuration from {CONF_PATH}")
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return
    if args.value is None:
        print(json.dumps(cfg.get(args.key), indent=2))
        return
    if args.key not in cfg:
        die(f"unknown config key: {args.key} (see /etc/dsh-mu/dsh-mu.conf.default)")
    kind = next(k for _v, key, k, _d, _doc in CONF_SCHEMA if key == args.key)
    try:
        value = _conf_coerce(kind, args.value)
    except (TypeError, ValueError):
        die(f"invalid value for {args.key}: {args.value!r} (kind: {kind})")
    cfg[args.key] = value
    save_config(cfg)
    if args.key == "model_api_key" and value:
        for name in st["instances"]:
            write_instance_api_key(st, name, str(value))
        info(f"model_api_key written into {len(st['instances'])} instance(s); "
             f"restart them to apply (dsh-muctl instance restart <name>)")
    sync_all(st)
    info(f"config {args.key} = {json.dumps(value)}")


# ---------------------------------------------------------------------- doctor
#
# `dsh-muctl doctor` turns every prerequisite this platform depends on into a
# check with a concrete fix. It is the first command to run on a new machine:
# almost all install failures on a fresh Alpine box come from one of these
# (kernel/container capabilities, cgroup delegation, apk keys, reachability,
# nginx integration). Probes are read-only or use throwaway paths.

DOCTOR_PASS, DOCTOR_WARN, DOCTOR_FAIL, DOCTOR_SKIP = "PASS", "WARN", "FAIL", "SKIP"


def _probe(cmd: list[str], timeout: float = 20.0) -> tuple[bool, str]:
    """Run a shell-free probe; return (ok, combined output)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    return res.returncode == 0, out


def _http_ok(url: str, timeout: float = 8.0, headers: dict | None = None) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": f"dsh-mu/{VERSION}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return True, f"HTTP {res.status}"
    except urllib.error.HTTPError as exc:
        # 403/404 still proves the host answered; treat 2xx/3xx/403 as reachable.
        return exc.code in (200, 204, 206, 301, 302, 403), f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - diagnosis reports any failure
        return False, str(exc)


def _du_mb(path: str) -> int:
    res = run(["du", "-sm", path], check=False, capture=True)
    try:
        return int((res.stdout or "0").split()[0])
    except (IndexError, ValueError):
        return 0


def _disk_free_mb(path: str) -> int:
    try:
        st = os.statvfs(path)
        return int(st.f_bavail * st.f_frsize / (1024 * 1024))
    except OSError:
        return -1


def _landlock_abi() -> int:
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        return int(libc.syscall(444, None, 0, 1))
    except Exception:  # noqa: BLE001
        return -1


def doctor_checks(st: dict) -> list[dict]:
    cfg = st["config"]
    checks: list[dict] = []

    def add(name, status, detail="", fix=""):
        checks.append({"check": name, "status": status, "detail": detail, "fix": fix})

    # ------------------------------------------------------------------ mounts
    for inst_name, inst in sorted(st["instances"].items()):
        for row in parse_mount_specs(inst):
            if os.path.isdir(row["host"]):
                add(f"mount {inst_name}:{row['jail']}", DOCTOR_PASS,
                    f"{row['host']} ({row['mode']})")
            else:
                add(f"mount {inst_name}:{row['jail']}", DOCTOR_WARN,
                    f"host path is missing: {row['host']}",
                    f"recreate it, or drop the mount (dsh-muctl mount rm {row['host']} "
                    f"--instance {inst_name})")

    # ------------------------------------------------------------------ state
    if os.path.exists(STATE_PATH):
        add("platform state", DOCTOR_PASS, STATE_PATH)
    else:
        add("platform state", DOCTOR_WARN, f"{STATE_PATH} not initialised yet",
            "dsh-muctl init   # creates state, session secret and dsh-mu.conf")
    if os.path.exists(CONF_PATH):
        add("platform config", DOCTOR_PASS, CONF_PATH)
    else:
        add("platform config", DOCTOR_WARN, f"{CONF_PATH} not written yet",
            "dsh-muctl init   # writes defaults from dsh-mu.conf.default")

    # ---------------------------------------------------------------- platform
    rel = host_alpine_version()
    if os.path.exists("/etc/os-release") and rel:
        add("alpine release", DOCTOR_PASS, f"{rel} (auto-detected)")
    else:
        add("alpine release", DOCTOR_FAIL, "/etc/os-release missing or unparsable",
            "this platform targets Alpine Linux 3.2x; set ALPINE_VERSION explicitly if detection fails")
    resolved = rel or "?"
    if str(cfg.get("alpine_version", "auto")).lower() == "auto":
        add("configured alpine version", DOCTOR_PASS, f"auto -> {resolved}")
    else:
        add("configured alpine version", DOCTOR_PASS, f"pinned {cfg['alpine_version']}")
    if community_repo_enabled():
        add("community repository", DOCTOR_PASS, "enabled in /etc/apk/repositories")
    else:
        add("community repository", DOCTOR_FAIL, "community missing",
            "community carries openresty/pnpm: append <mirror>/<ver>/community to /etc/apk/repositories")

    # In jail mode the gateway's nginx lives inside the platform jail, so the host
    # must not be required to have one.
    jail_mode = str(cfg.get("gateway_mode") or "jail") == "jail"
    required = ["apk", "python3", "unshare", "chroot", "setpriv", "ip",
                "rc-service", "rc-update"]
    if not jail_mode:
        required.append("nginx")
    missing = [b for b in required if not shutil.which(b)]
    if missing:
        add("required commands", DOCTOR_FAIL, "missing: " + ", ".join(missing),
            "dsh-muctl install   # installs python3/nodejs/npm/pnpm/util-linux"
            + ("" if jail_mode else "/openresty"))
    else:
        add("required commands", DOCTOR_PASS,
            " ".join(required)
            + (" (node/npm/nginx live in the platform jail)" if jail_mode else ""))

    # The shared DSH runtime must exist where instances mount it from.
    runtime = dsh_runtime_dir(cfg)
    if os.path.exists(f"{runtime}/package.json"):
        add("shared dsh runtime", DOCTOR_PASS, f"{runtime}")
    else:
        add("shared dsh runtime", DOCTOR_FAIL, f"missing: {runtime}",
            "dsh-muctl install (the gateway template carries node/npm)")

    if relay_enabled(cfg):
        port = int(cfg.get("relay_listen", 8099) or 8099)
        doc_ok = os.path.exists(f"{GW_DIR}/relay.json")
        sock = socket.socket()
        sock.settimeout(1.5)
        alive = sock.connect_ex(("127.0.0.1", port)) == 0
        sock.close()
        if doc_ok and alive:
            add("model relay", DOCTOR_PASS,
                f"127.0.0.1:{port} (keys are injected in the gateway; "
                f"instances hold `{RELAY_PLACEHOLDER}`)")
        else:
            add("model relay", DOCTOR_WARN,
                f"relay.json={doc_ok} listening={alive} port={port}",
                "dsh-muctl sync && dsh-muctl gateway restart   "
                "# or set RELAY_ENABLED=false to go direct")

    if relay_enabled(cfg):
        # The patch, the state and the jail's relay.json must agree on the token;
        # a mismatch shows up as a blanket 403 ("API 密钥无效") in every conversation.
        doc_path = f"{GW_DIR}/relay.json"
        mismatched = []
        try:
            doc = json.loads(read_text(doc_path) or "{}")
            by_instance = {str(v.get("instance")): k for k, v in (doc.get("tokens") or {}).items()}
            for inst_name, inst in (st.get("instances") or {}).items():
                want = str(inst.get("relay_token") or "")
                if not want:
                    continue
                patch = f"{INSTANCES_DIR}/{inst_name}/home/.dsh-mu-provider.yml"
                text = read_text(patch) or ""
                in_patch = re.findall(r"/mu-llm/([0-9a-f]{32})/", text)
                if in_patch and set(in_patch) != {want}:
                    mismatched.append(f"{inst_name}: patch")
                if by_instance.get(inst_name) != want:
                    mismatched.append(f"{inst_name}: relay.json")
        except (OSError, ValueError):
            mismatched.append("relay.json unreadable")
        if mismatched:
            add("relay tokens", DOCTOR_FAIL, ", ".join(sorted(set(mismatched))),
                "dsh-muctl plan sync   # re-renders the patch and relay.json from state")
        else:
            add("relay tokens", DOCTOR_PASS, "patch / state / relay.json agree")

    current_dsh = installed_dsh_version()
    want = str(cfg.get("dsh_version") or "latest")
    if current_dsh and (want == "latest" or current_dsh == want):
        add("global dsh runtime", DOCTOR_PASS, f"@deepseek-ai/dsh@{current_dsh}")
    elif current_dsh:
        add("global dsh runtime", DOCTOR_WARN, f"installed {current_dsh}, configured {want}",
            f"dsh-muctl install --force-dsh   # or: dsh-muctl config dsh_version {current_dsh}")
    else:
        add("global dsh runtime", DOCTOR_FAIL, f"not installed in {runtime}",
            "dsh-muctl install   # chroots into the gateway template and runs npm install -g")

    for entry in ensure_system_admins(st):
        if entry.get("demoted"):
            continue
        if entry.get("password_ready"):
            add(f"admin account {entry['user']}", DOCTOR_PASS,
                "host account with a password; log in at /login")
        else:
            add(f"admin account {entry['user']}", DOCTOR_FAIL,
                f"{entry['detail']} (from ADMIN_SYSTEM_USERS)",
                f"passwd {entry['user']}   # administrators use the operating system password")

    if os.path.exists(SECRET_PATH):
        info_secret = os.stat(SECRET_PATH)
        mode = info_secret.st_mode & 0o777
        try:
            group = grp.getgrgid(info_secret.st_gid).gr_name
        except KeyError:
            group = str(info_secret.st_gid)
        world = mode & 0o007
        # 0640 root:nginx is intentional: the nginx worker must read the secret.
        add("session secret", DOCTOR_WARN if world else DOCTOR_PASS,
            f"{SECRET_PATH} mode {oct(mode)} root:{group}",
            "" if not world else f"chmod 0640 {SECRET_PATH}; chgrp nginx {SECRET_PATH}")
    else:
        add("session secret", DOCTOR_FAIL, f"{SECRET_PATH} missing", "dsh-muctl init")

    # ------------------------------------------------------------------- jail
    ok, out = _probe(["unshare", "--mount", "--uts", "--propagation", "private", "true"])
    add("mount+UTS namespace", DOCTOR_PASS if ok else DOCTOR_FAIL, out[:200] or "ok",
        "" if ok else "unprivileged/LXC container without CAP_SYS_ADMIN: the jail cannot be built")
    probe_dir = tempfile.mkdtemp(prefix="dshmu-doctor-")
    try:
        ok, out = _probe(["unshare", "--mount", "--propagation", "private", "sh", "-c",
                          f"mount -t tmpfs none {probe_dir} && umount {probe_dir}"])
        add("bind/tmpfs mount in namespace", DOCTOR_PASS if ok else DOCTOR_FAIL, out[:200] or "ok",
            "" if ok else "mount inside a new mount namespace failed; check seccomp/apparmor of the container")
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    ok, out = _probe(["setpriv", "--reuid", "65534", "--regid", "65534", "--clear-groups",
                      "--bounding-set", "-all", "--inh-caps", "-all", "--ambient-caps", "-all",
                      "--no-new-privs", "true"])
    add("capability drop (setpriv)", DOCTOR_PASS if ok else DOCTOR_FAIL, out[:200] or "ok",
        "" if ok else "util-linux `setpriv` is required (busybox's does not support capability "
                      "dropping); install: apk add util-linux")
    ok_userns, out_userns = _probe(["unshare", "-U", "-m", "true"])
    add("unprivileged user namespace", DOCTOR_PASS if ok_userns else DOCTOR_WARN,
        out_userns[:200] or "ok",
        "" if ok_userns else "expected inside LXC: bwrap (DSH's own sandbox) is then unusable and "
                             "instances run with INSTANCE_PERMISSION_MODE=danger-full-access")
    abi = _landlock_abi()
    perm_mode = str(cfg.get("instance_permission_mode", ""))
    if abi > 0:
        add("landlock LSM", DOCTOR_PASS, f"ABI {abi} (second sandbox backend available)")
    elif perm_mode == "danger-full-access":
        add("landlock LSM", DOCTOR_PASS,
            "unavailable, and instances are configured for it "
            "(INSTANCE_PERMISSION_MODE=danger-full-access; the jail is the boundary)")
    else:
        add("landlock LSM", DOCTOR_WARN, "unavailable while instances expect DSH's own sandbox",
            "set INSTANCE_PERMISSION_MODE=danger-full-access: bwrap needs unprivileged userns "
            "(blocked in LXC) and landlock is missing, so agent tools would fail closed")

    if os.path.isdir(TEMPLATE):
        sub = []
        if not os.path.exists(f"{TEMPLATE}/bin/sh"):
            sub.append("bin/sh")
        if not any(os.path.exists(f"{TEMPLATE}{p}") for p in
                   ("/usr/bin/setpriv", "/bin/setpriv", "/usr/sbin/setpriv")):
            sub.append("setpriv")
        if not os.path.exists(f"{TEMPLATE}/usr/bin/node"):
            sub.append("usr/bin/node")
        # The dsh CLI link points into the runtime bind mount, so it is dangling
        # inside the template by design: lexists is the right test here.
        if not os.path.lexists(f"{TEMPLATE}/usr/local/bin/dsh"):
            sub.append("usr/local/bin/dsh link")
        size = _du_mb(TEMPLATE)
        add("jail template", DOCTOR_PASS if not sub else DOCTOR_WARN,
            f"{TEMPLATE} ({size} MB)" + (f", missing: {', '.join(sub)}" if sub else ""),
            "" if not sub else "dsh-muctl install --force-template")
    else:
        add("jail template", DOCTOR_WARN, f"{TEMPLATE} not built",
            "dsh-muctl install   # downloads a Proxmox alpine image, then adds "
                "ALPINE_TEMPLATE_PACKAGES inside it")

    # ----------------------------------------------------------------- cgroup
    # ------------------------------------------------------------------ TLS
    if tls_enabled(cfg):
        ready = tls_cert_ready(cfg)
        add("HTTPS certificate", DOCTOR_PASS if ready else DOCTOR_FAIL,
            f"{TLS_CERT} + {TLS_KEY}" if ready else f"missing or unreadable: {TLS_DIR}",
            "run: dsh-muctl tls self-sign  (or: dsh-muctl tls import --cert … --key …)")
        if ready:
            meta = tls_cert_info()
            days = meta.get("days_left")
            if days is None:
                add("HTTPS certificate expiry", DOCTOR_PASS, meta.get("not_after") or "unknown")
            elif days <= 0:
                add("HTTPS certificate expiry", DOCTOR_FAIL,
                    "expired: " + (meta.get("not_after") or "?"),
                    "renew it: dsh-muctl tls self-sign  (or import a new chain)")
            elif days < 14:
                add("HTTPS certificate expiry", DOCTOR_WARN,
                    f"{days} day(s) left ({meta.get('not_after')})",
                    "renew soon")
            else:
                add("HTTPS certificate expiry", DOCTOR_PASS,
                    f"{days} day(s) left ({meta.get('not_after')})")
        listening = port_in_use(443)
        add("HTTPS listening on 443", DOCTOR_PASS if listening else DOCTOR_WARN,
            "yes" if listening else "not yet (run: dsh-muctl gateway restart)")
    else:
        add("HTTPS", DOCTOR_PASS, "disabled (http only)")

    add("cgroup v2 mounted", DOCTOR_PASS if os.path.exists("/sys/fs/cgroup/cgroup.controllers") else DOCTOR_FAIL,
        "/sys/fs/cgroup", "" if os.path.exists("/sys/fs/cgroup/cgroup.controllers") else "cgroup v2 required")
    controllers = (read_text("/sys/fs/cgroup/cgroup.controllers") or "").split()
    want_ctl = [c for c in ("cpu", "memory", "pids") if c in controllers]
    add("cgroup controllers", DOCTOR_PASS if len(want_ctl) == 3 else DOCTOR_FAIL,
        " ".join(controllers) or "none",
        "" if len(want_ctl) == 3 else "kernel must provide cpu, memory and pids")
    # Delegation survives a reboot only if the service is registered at boot.
    # Losing that link (an `apk del` that removed the runlevel entry, a manual
    # rc-update, a restored /etc) is invisible to the runtime checks above until
    # the next reboot - so check the registration itself.
    boot_links = glob.glob("/etc/runlevels/boot/*")
    registered = any(os.path.basename(p) == "dsh-mu-cgroup" for p in boot_links)
    if os.path.exists("/etc/init.d/dsh-mu-cgroup") and registered:
        add("cgroup service at boot", DOCTOR_PASS, "dsh-mu-cgroup registered in the boot runlevel")
    else:
        add("cgroup service at boot", DOCTOR_WARN,
            "dsh-mu-cgroup is not registered for boot"
            if os.path.exists("/etc/init.d/dsh-mu-cgroup") else "dsh-mu-cgroup service is missing",
            "dsh-muctl doctor --fix   # rewrites and re-registers the delegation service")

    delegated = (read_text("/sys/fs/cgroup/dsh-mu/cgroup.subtree_control") or "").split()
    if all(c in delegated for c in want_ctl):
        add("cgroup delegation", DOCTOR_PASS, f"/sys/fs/cgroup/dsh-mu: {' '.join(delegated)}")
    else:
        add("cgroup delegation", DOCTOR_FAIL,
            f"/sys/fs/cgroup/dsh-mu subtree_control: {' '.join(delegated) or 'empty'}",
            "rc-service dsh-mu-cgroup start   # drains the container's cgroup root, then delegates")
    leaf = "/sys/fs/cgroup/dsh-mu/_doctor"
    try:
        os.makedirs(leaf, exist_ok=True)
        wrote = []
        for name, value in (("memory.max", "268435456"), ("cpu.max", "100000 100000"), ("pids.max", "64")):
            try:
                with open(f"{leaf}/{name}", "w") as fh:
                    fh.write(value)
                wrote.append(name)
            except OSError:
                pass
        add("cgroup limits writable", DOCTOR_PASS if len(wrote) == 3 else DOCTOR_FAIL,
            ", ".join(wrote) + " writable" if wrote else "no limit files",
            "" if len(wrote) == 3 else "delegation incomplete; instances cannot carry limits")
        with open(f"{leaf}/cgroup.kill", "w") as fh:
            fh.write("1")
    except OSError as exc:
        add("cgroup limits writable", DOCTOR_FAIL, str(exc),
            "rc-service dsh-mu-cgroup start")
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(leaf)

    # ------------------------------------------------------------ disk / ports
    free = _disk_free_mb(VAR if os.path.isdir(VAR) else "/")
    template_mb = _du_mb(TEMPLATE)
    if template_mb <= 0:
        add("disk space", DOCTOR_PASS if free > 2048 else DOCTOR_WARN,
            f"{free} MB free (template not built yet; ~200 MB per instance afterwards)",
            "" if free > 2048 else "free space before building the jail template")
    else:
        per_instance = template_mb + 40
        if free < per_instance * 2:
            add("disk space", DOCTOR_FAIL, f"{free} MB free, ~{per_instance} MB per instance",
                "free space or trim ALPINE_TEMPLATE_PACKAGES")
        else:
            add("disk space", DOCTOR_PASS,
                f"{free} MB free (~{free // per_instance} instances at {per_instance} MB)")

    listen = int(cfg.get("gateway_listen", 80))
    if port_in_use(listen):
        add(f"gateway port {listen}", DOCTOR_PASS, "in use (expected when openresty runs)")
    else:
        add(f"gateway port {listen}", DOCTOR_WARN, "nothing listening",
            "rc-service openresty start   # or change GATEWAY_LISTEN in dsh-mu.conf")

    # ----------------------------------------------------------- reachability
    mirror = str(cfg["alpine_mirror"]).rstrip("/")
    ver = resolve_alpine_version(cfg) if cfg.get("alpine_version") else resolved
    ok, out = _http_ok(f"{mirror}/{ver}/main/x86_64/APKINDEX.tar.gz",
                       headers={"Range": "bytes=0-0"})
    add("alpine mirror", DOCTOR_PASS if ok else DOCTOR_WARN, f"{mirror}/{ver} -> {out}",
        "" if ok else "set ALPINE_MIRROR to a reachable mirror")
    registry = cfg.get("npm_registry") or "https://registry.npmjs.org"
    ok, out = _http_ok(f"{registry.rstrip('/')}/-/ping")
    add("npm registry", DOCTOR_PASS if ok else DOCTOR_WARN, f"{registry} -> {out}",
        "" if ok else "set NPM_REGISTRY (e.g. https://registry.npmmirror.com) before installing dsh")

    # ------------------------------------------------------------ nginx glue
    mode = str(cfg.get("gateway_mode") or "host")
    if mode == "jail":
        paths = {"rootfs": f"{GATEWAY_DIR}/rootfs", "conf": f"{GATEWAY_DIR}/conf"}
        have_openresty = os.path.exists(f"{paths['rootfs']}/usr/sbin/nginx")
        if have_openresty:
            add("gateway jail rootfs", DOCTOR_PASS, paths["rootfs"])
        elif not jails_supported():
            add("gateway jail rootfs", DOCTOR_WARN,
                f"{paths['rootfs']} not built (mount namespaces unavailable here)",
                "install on the real host: dsh-muctl install")
        else:
            add("gateway jail rootfs", DOCTOR_FAIL,
                f"{paths['rootfs']} (no openresty inside)",
                "dsh-muctl gateway build --force")
        try:
            want = render_gateway_fragments(
                {**cfg, "nginx_conf_d_dir": f"{paths['conf']}/conf.d",
                 "nginx_http_d_dir": f"{paths['conf']}/http.d"}, quiet=True)
        except OSError as exc:
            want = {}
            add("gateway jail config", DOCTOR_FAIL, str(exc), "reinstall the dsh-mu package")
        drift = []
        for path, content in want.items():
            disk = read_text(path)
            if disk is None:
                drift.append(f"{os.path.basename(path)} missing")
            elif disk != content:
                drift.append(f"{os.path.basename(path)} differs")
        if not want:
            pass
        elif drift:
            # The fragments are generated from the host's names/addresses, so an
            # install that changed them (or a re-render the installer skipped) is
            # not a fault: rewrite them and say so instead of failing the check.
            try:
                with contextlib.suppress(OSError):
                    render_gateway_jail_conf_files(cfg, int(cfg.get("gateway_listen", 80)))
                add("gateway jail config", DOCTOR_WARN,
                    "re-rendered: " + "; ".join(drift))
            except OSError as exc:
                add("gateway jail config", DOCTOR_FAIL, f"{exc}; {'; '.join(drift)}",
                    "dsh-muctl gateway conf")
        else:
            add("gateway jail config", DOCTOR_PASS, f"{paths['conf']}/nginx.conf + fragments")
        health = gateway_health(int(cfg.get("gateway_listen", 80)))
        add("gateway jail answering", DOCTOR_PASS if health["ok"] else DOCTOR_FAIL,
            f"http://127.0.0.1:{health['port']}/login -> {health['http'] or 'no answer'}",
            "" if health["ok"] else "dsh-muctl gateway start   # or: gateway logs")
        if health["ok"]:
            # `nginx -t` creates the listening sockets, so it cannot run while the
            # live jail holds the port; a jail that answers has obviously loaded
            # its configuration, which is the stronger statement anyway.
            add("gateway jail configuration", DOCTOR_PASS,
                "loaded by the running jail (nginx -t would fight for the port)")
        elif have_openresty and os.path.exists(GATEWAY_CONF):
            res = run([JAIL, "--conf", GATEWAY_CONF, "__gateway", "--",
                       "nginx", "-t", "-c", f"{GATEWAY_CONF_IN_JAIL}/nginx.conf"],
                      check=False, capture=True, timeout=60)
            out = ((res.stdout or "") + (res.stderr or "")).strip().splitlines()
            okc = res.returncode == 0 and any("syntax is ok" in line for line in out)
            add("gateway jail configuration", DOCTOR_PASS if okc else DOCTOR_FAIL,
                out[-1] if out else "(no output)",
                "" if okc else "dsh-muctl gateway conf   # then: dsh-muctl gateway restart")
    else:
        text = read_text(NGINX_CONF) or ""
        if NGINX_INCLUDE_MARKER in text:
            add("nginx managed include", DOCTOR_PASS, NGINX_CONF)
        else:
            add("nginx managed include", DOCTOR_FAIL, f"missing in {NGINX_CONF}",
                "dsh-muctl gateway install")
        try:
            want_frags = render_gateway_fragments(cfg, quiet=True)
        except OSError as exc:
            want_frags = {}
            add("nginx fragments", DOCTOR_FAIL, str(exc),
                "reinstall the dsh-mu package (fragment templates live in /usr/local/lib/dsh-mu/nginx)")
        drift = []
        for path, content in want_frags.items():
            disk = read_text(path)
            if disk is None:
                drift.append(f"{os.path.basename(path)} missing")
            elif disk != content:
                drift.append(f"{os.path.basename(path)} differs")
        if drift:
            add("nginx fragments", DOCTOR_FAIL, "; ".join(drift), "dsh-muctl gateway install")
        else:
            add("nginx fragments", DOCTOR_PASS, ", ".join(os.path.basename(p) for p in want_frags))
        ok, out = _probe(["/usr/sbin/nginx", "-t"], timeout=15)
        add("nginx configuration", DOCTOR_PASS if ok else DOCTOR_FAIL, out.splitlines()[-1] if out else "ok",
            "" if ok else "dsh-muctl gateway install (it rolls back a rejected edit)")
        lua_dicts = len(re.findall(r"^\s*lua_shared_dict\s+dshmu\b", text, re.M))
        add("lua_shared_dict dshmu", DOCTOR_PASS if lua_dicts == 0 else DOCTOR_WARN,
            f"defined {lua_dicts}x in nginx.conf (fragment provides it)",
            "" if lua_dicts == 0 else "legacy whole-file config still present: dsh-muctl gateway install")

    # --------------------------------------------------------- runtime state
    for view in ("users", "upstreams", "config"):
        path = f"{GW_DIR}/{view}.json"
        if not os.path.exists(path):
            add(f"gateway view {view}.json", DOCTOR_FAIL, f"{path} missing", "dsh-muctl sync")
    users_view = None
    try:
        users_view = json.loads(read_text(f"{GW_DIR}/users.json") or "{}")
    except json.JSONDecodeError:
        pass
    if users_view is not None:
        n_view = len(users_view.get("users", {}))
        add("gateway views in sync", DOCTOR_PASS if n_view == len(st["users"]) else DOCTOR_WARN,
            f"users.json has {n_view}, state has {len(st['users'])}",
            "" if n_view == len(st["users"]) else "dsh-muctl sync")

    # A live instance whose recorded port falls outside PORT_RANGE is a state the
    # platform should not have accepted silently: a new instance could not be
    # given that port, and `instance limits --port` would refuse to move one back.
    lo_r, hi_r = (int(x) for x in (st["config"].get("port_range") or [0, 0]))
    for name, inst in sorted(st["instances"].items()):
        try:
            port_r = int(inst.get("port", 0))
        except (TypeError, ValueError):
            port_r = 0
        if port_r and not (lo_r <= port_r <= hi_r):
            add(f"instance {name} port range", DOCTOR_WARN,
                f"port {port_r} is outside PORT_RANGE {lo_r}-{hi_r}",
                f"dsh-muctl instance limits {name} --port <port inside the range>   "
                "# or widen PORT_RANGE in dsh-mu.conf")

    for name, inst in sorted(st["instances"].items()):
        port = int(inst.get("port", 0))
        alive = port_in_use(port)
        declared = inst.get("state", "?")
        starting = service_started(name) and not alive
        if declared == "running" and not alive and starting:
            add(f"instance {name}", DOCTOR_WARN,
                f"service is up but 127.0.0.1:{port} is not bound yet (starting, or crash-looping)",
                f"wait a few seconds; if it persists: dsh-muctl instance logs {name}")
        elif declared == "running" and not alive:
            add(f"instance {name}", DOCTOR_FAIL, f"state=running but 127.0.0.1:{port} is closed",
                f"dsh-muctl instance logs {name}; dsh-muctl instance restart {name}")
        elif declared != "running" and alive:
            add(f"instance {name}", DOCTOR_WARN, f"state={declared} but 127.0.0.1:{port} answers",
                "dsh-muctl sync-state   # align the stored state with reality")
        else:
            add(f"instance {name}", DOCTOR_PASS, f"state={declared}, port {port} {'open' if alive else 'closed'}")
        if declared == "running" and not inst.get("upstream", {}).get("cookie"):
            add(f"instance {name} cookie", DOCTOR_WARN, "no upstream cookie",
                f"dsh-muctl instance refresh-cookie {name}")
        log = f"{LOGS}/{name}.log"
        if os.path.exists(log):
            size_mb = os.path.getsize(log) // (1024 * 1024)
            if size_mb >= 64:
                add(f"instance {name} log size", DOCTOR_WARN, f"{size_mb} MB",
                    "check rotation: dsh-muctl logs size; dsh-muctl logs rotate --force")

    # -------------------------------------------------------- limit syntax
    for name, inst in sorted(st["instances"].items()):
        bad_keys = []
        for key in ("cpu_max", "mem_max", "mem_high", "pids_max"):
            value = str(inst.get(key) or "").strip()
            if not value:
                continue
            pattern = r"^(max|\d+\s+\d+)$" if key == "cpu_max" else r"^(max|\d+)$"
            if not re.match(pattern, value):
                bad_keys.append(f"{key}={value!r}")
        if bad_keys:
            add(f"instance {name} limits", DOCTOR_FAIL,
                "recorded limit value is not valid cgroup syntax: " + ", ".join(bad_keys),
                f"dsh-muctl instance limits {name} --cpu '200000 100000'   # or --cpu max "
                f"(the running cgroup still holds the real value)")
        else:
            add(f"instance {name} limits", DOCTOR_PASS, "recorded limits are well-formed")

    # ------------------------------------------------------ record consistency
    for name, user in sorted(st["users"].items()):
        inst_name = user.get("instance") or ""
        # "none" is the sentinel written by `user add --instance none`.
        if inst_name in ("", "none"):
            continue
        if inst_name not in st["instances"]:
            add(f"user {name} instance", DOCTOR_WARN,
                f"the account points at instance {inst_name!r}, which does not exist",
                f"dsh-muctl user add {name} --instance auto   # or point it somewhere else")
    for name, inst in sorted(st["instances"].items()):
        owner = inst.get("user") or ""
        if owner and owner not in st["users"]:
            add(f"instance {name} owner", DOCTOR_WARN,
                f"instance exists but its user {owner!r} does not",
                f"dsh-muctl user add {owner} --password …   # or: dsh-muctl instance del {name}")
        rootfs = f"{INSTANCES_DIR}/{name}/rootfs"
        if not os.path.isdir(rootfs):
            add(f"instance {name} rootfs", DOCTOR_FAIL, f"{rootfs} missing",
                f"dsh-muctl instance del {name} --purge && dsh-muctl instance create {name} "
                f"--user {owner or name}")
        home = f"{INSTANCES_DIR}/{name}/home"
        if not os.path.isdir(home):
            add(f"instance {name} home", DOCTOR_FAIL, f"{home} missing",
                f"dsh-muctl instance del {name} --purge && dsh-muctl instance create {name} "
                f"--user {owner or name}")

    # ------------------------------------------------------------- log rotation
    if os.path.exists(LOGROTATE_CONF):
        add("log rotation", DOCTOR_PASS, f"{LOGROTATE_CONF} (daily, copytruncate, keep "
                                        f"{DEFAULT_LOG_KEEP})")
    elif shutil.which("logrotate"):
        add("log rotation", DOCTOR_WARN, "logrotate installed but no drop-in for dsh-mu",
            "dsh-muctl install   # writes /etc/logrotate.d/dsh-mu")
    else:
        add("log rotation", DOCTOR_WARN, "logrotate not installed; nothing rotates the logs",
            "apk add logrotate   # or schedule: dsh-muctl logs rotate")
    if str(cfg.get("gateway_mode") or "host") != "jail":
        gaps = nginx_runtime_gaps()
        if not gaps:
            add("nginx runtime", DOCTOR_PASS, f"user {NGINX_USER} and temp directories present")
        else:
            add("nginx runtime", DOCTOR_WARN, "missing: " + ", ".join(gaps),
                "dsh-muctl doctor --fix   # creates the nginx user/group and temp dirs "
                "(normally done by the openresty package)")

    total_mb = sum(os.path.getsize(p) for p in platform_log_files() if os.path.exists(p)) // (1024 * 1024)
    add("log volume", DOCTOR_PASS if total_mb < 512 else DOCTOR_WARN,
        f"{total_mb} MB across {len(platform_log_files())} files",
        "" if total_mb < 512 else "dsh-muctl logs rotate --force")
    return checks


def cmd_doctor(args):
    ensure_root()
    if os.path.exists(STATE_PATH):
        st = load_state()
    else:
        # Must work before `dsh-muctl init`: that is the whole point on a new host.
        st = {"version": 1, "schema": STATE_SCHEMA, "users": {}, "instances": {},
              "config": load_config(None)}
    if getattr(args, "fix", False):
        fixes = []
        if os.path.exists(NGINX_CONF):
            install_gateway_fragments(st["config"], reload=True)
            fixes.append("gateway install")
        sync_all(st)
        fixes.append("sync")
        # Rewrite + re-register first (idempotent), then start: `--fix` must repair
        # a lost boot registration, not just nudge a running service.
        ensure_cgroup_service()
        run(["/sbin/rc-service", "dsh-mu-cgroup", "start"], check=False)
        fixes.append("dsh-mu-cgroup registered at boot + started")
        gaps = ensure_nginx_runtime(fix=True)
        if gaps:
            fixes.append("nginx runtime (" + ", ".join(gaps) + ")")
        started = reconcile_instances(load_state())
        if started:
            fixes.append("started: " + ", ".join(started))
        info("doctor --fix applied: " + ", ".join(fixes))
    checks = doctor_checks(st)
    counts = {DOCTOR_PASS: 0, DOCTOR_WARN: 0, DOCTOR_FAIL: 0, DOCTOR_SKIP: 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    if args.json:
        print(json.dumps({"checks": checks, "summary": counts,
                          "config_file": CONF_PATH, "version": VERSION}, indent=2, sort_keys=True))
    else:
        width = max(len(c["check"]) for c in checks) if checks else 10
        for c in checks:
            line = f"{c['status']:4}  {c['check']:<{width}}  {c['detail']}"
            print(line)
            if c["fix"] and c["status"] in (DOCTOR_FAIL, DOCTOR_WARN):
                print(f"      {'':<{width}}  fix: {c['fix']}")
        print()
        print(f"doctor: {counts[DOCTOR_PASS]} PASS, {counts[DOCTOR_WARN]} WARN, {counts[DOCTOR_FAIL]} FAIL")
        if counts[DOCTOR_FAIL]:
            print("doctor: blocking problems found — fix them before installing instances "
                  "(try: dsh-muctl doctor --fix)")
    return 1 if counts[DOCTOR_FAIL] else 0


# ------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dsh-muctl", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"dsh-muctl {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="create directories, secret key and state file")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("install", help="install packages, DSH runtime, template and services")
    sp.add_argument("--gateway-host", help="public hostname of the gateway")
    sp.add_argument("--model-api-key", help="platform-wide model API key injected into new instances")
    sp.add_argument("--force-template", action="store_true")
    sp.add_argument("--template-image",
                    help="exact Proxmox template file (skips the interactive list)")
    sp.add_argument("--yes", dest="assume_yes", action="store_true",
                    help="accept defaults (newest template, detected mirrors) without asking")
    sp.add_argument("--skip-dsh", dest="skip_dsh", action="store_true",
                    help="do not install/refresh the global @deepseek-ai/dsh runtime")
    sp.add_argument("--force-dsh", action="store_true", help="reinstall the global DSH runtime")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("user", help="manage accounts")
    usub = sp.add_subparsers(dest="subcommand", required=True)
    a = usub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--role", choices=["user", "admin"], default="user")
    a.add_argument("--password")
    a.add_argument("--instance", default="auto", help="auto | none | <instance-name>")
    a.add_argument("--must-change", dest="must_change", action="store_true",
                   help="ask the account holder to change the password (reported to the user panel)")
    a.add_argument("--no-start", dest="no_start", action="store_true",
                   help="do not start the freshly created instance")
    a.set_defaults(func=cmd_user_add)
    usub.add_parser("list").set_defaults(func=cmd_user_list)
    a = usub.add_parser("passwd")
    a.add_argument("name")
    a.add_argument("--password")
    a.add_argument("--must-change", dest="must_change", action="store_true",
                   help="ask the account holder to change the password again after this reset")
    a.set_defaults(func=cmd_user_passwd)
    a = usub.add_parser("kick")
    a.add_argument("name")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_user_kick)
    for name, fn in (("enable", lambda a: cmd_user_toggle(a, True)), ("disable", lambda a: cmd_user_toggle(a, False))):
        a = usub.add_parser(name)
        a.add_argument("name")
        a.set_defaults(func=fn)
    a = usub.add_parser("del")
    a.add_argument("name")
    a.add_argument("--with-instance", dest="with_instance", action="store_true",
                   help="also delete (purge) the instance this account owns")
    a.set_defaults(func=cmd_user_del)

    sp = sub.add_parser("instance", help="manage jailed instances")
    isub = sp.add_subparsers(dest="subcommand", required=True)
    a = isub.add_parser("create")
    a.add_argument("name")
    a.add_argument("--user")
    a.add_argument("--cpu")
    a.add_argument("--mem")
    a.add_argument("--pids")
    a.add_argument("--port", type=int)
    a.add_argument("--start", action="store_true", help="(deprecated: starting is the default)")
    a.add_argument("--no-start", dest="no_start", action="store_true",
                   help="create the record and the rootfs but do not start it")
    a.set_defaults(func=cmd_instance_create)
    for cmd_name, fn in (("start", cmd_instance_start), ("stop", cmd_instance_stop), ("restart", cmd_instance_restart)):
        a = isub.add_parser(cmd_name)
        a.add_argument("names", nargs="+")
        if cmd_name in ("start", "restart"):
            a.add_argument("--no-wait", action="store_true",
                           help="do not block until the harness banner appears")
        a.set_defaults(func=fn)
    a = isub.add_parser("status")
    a.add_argument("names", nargs="*")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_instance_status)
    a = isub.add_parser("list", help="list all instances (same as status without names)")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_instance_list)
    a = isub.add_parser("del")
    a.add_argument("name")
    a.add_argument("--purge", action="store_true")
    a.set_defaults(func=cmd_instance_del)
    a = isub.add_parser("logs")
    a.add_argument("name")
    a.add_argument("-n", "--lines", type=int, default=100)
    a.add_argument("-f", "--follow", action="store_true")
    a.set_defaults(func=cmd_instance_logs)
    a = isub.add_parser("shell")
    a.add_argument("name")
    a.set_defaults(func=cmd_instance_shell)
    a = isub.add_parser("exec", help="run one command inside the jail (use -- before it)")
    a.add_argument("name")
    a.add_argument("command", nargs=argparse.REMAINDER,
                   help="command line to run inside the instance")
    a.set_defaults(func=cmd_instance_exec)
    a = isub.add_parser("port", help="show or move an instance's loopback port")
    a.add_argument("name")
    a.add_argument("--port", type=int, help="target port (must be inside PORT_RANGE)")
    a.add_argument("--auto", action="store_true", help="pick the next free port automatically")
    a.add_argument("--restart", action="store_true", help="restart the instance right away")
    a.add_argument("--dry-run", dest="dry_run", action="store_true", help="check only")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_instance_port)
    a = isub.add_parser("limits", help="show or set cpu/memory/pids limits (max = 不限)")
    a.add_argument("name")
    a.add_argument("--cpu-cores", dest="cpu_cores",
                   help="CPU 核数（如 0.5 / 2）或 max=不限")
    a.add_argument("--mem-mb", dest="mem_mb", help="内存（MB，可写 512M/2G）或 max=不限")
    a.add_argument("--pids-num", dest="pids_num", help="进程数上限或 max=不限")
    a.add_argument("--cpu", help="原样写入 cgroup 的 cpu.max（如 '200000 100000'）")
    a.add_argument("--mem", help="原样写入 cgroup 的 memory.max（字节）")
    a.add_argument("--pids", help="原样写入 cgroup 的 pids.max")
    a.add_argument("--show", action="store_true", help="只显示当前限额")
    a.set_defaults(func=cmd_instance_limits)
    a = isub.add_parser("owner", help="show or change the uid the instance runs as")
    a.add_argument("name")
    a.add_argument("--uid", type=int)
    a.set_defaults(func=cmd_instance_owner)
    a = isub.add_parser("api-key", help="set/remove the platform model API key for an instance")
    a.add_argument("name")
    a.add_argument("key", nargs="?")
    a.add_argument("--remove", action="store_true")
    a.set_defaults(func=cmd_instance_api_key)
    a = isub.add_parser("plugin", help="plugins installed in this instance (install/remove/list)")
    a.add_argument("names", nargs="+", help="`list <name>` or `add|remove <name> <pkg>[@ver]`")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_instance_plugin)
    a = isub.add_parser("provider", help="wire an instance to a model provider preset")
    a.add_argument("name")
    # No hard-coded choices: the catalog is extensible via /etc/dsh-mu/providers.json
    # and `write_provider` validates the name against `all_providers()`.
    a.add_argument("preset", help="preset name, or `show`/`clear`")
    a.add_argument("--model", help="override the preset's default model id")
    a.add_argument("--key", help="API key to store (prefer --key-file: argv is visible in ps)")
    a.add_argument("--key-file", help="file containing the API key")
    a.set_defaults(func=cmd_instance_provider)
    a = isub.add_parser("refresh-cookie")
    a.add_argument("names", nargs="+")
    a.set_defaults(func=cmd_instance_refresh_cookie)

    sp = sub.add_parser("gateway", help="gateway operations (host nginx, or the platform jail)")
    sp.add_argument("action", choices=["reload", "status", "install",
                                       "build", "start", "stop", "restart", "conf", "logs",
                                       "jail-status"])
    sp.add_argument("--no-reload", dest="reload", action="store_false", default=True,
                    help="with `install`: write fragments but do not reload openresty")
    sp.add_argument("--force", action="store_true", help="with `build`: rebuild template and rootfs")
    sp.add_argument("--port", type=int, help="with build/start/status: the jail's listen port")
    sp.add_argument("--lines", type=int, default=100, help="with `logs`: how many lines")
    sp.set_defaults(func=cmd_gateway)

    sub.add_parser("sync", help="re-render gateway views and instance configs").set_defaults(func=cmd_sync)
    sp = sub.add_parser("bootstrap",
                        help="prepare the host administrator account(s) used to reach /admin/")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_bootstrap)
    sp = sub.add_parser("provider", help="provider catalog: presets, packages and import/export")
    sp.add_argument("action", choices=["list", "show", "add", "set", "rm",
                                       "spec", "validate", "import", "export",
                                       "models", "usage"])
    sp.add_argument("name", nargs="?", help="provider name")
    sp.add_argument("--kind", choices=["pi-ai", "deepseek"], help="pi-ai=OpenAI Chat Completions, deepseek=Anthropic Messages")
    sp.add_argument("--route", help="route id (pi-ai only; defaults to the provider name)")
    sp.add_argument("--api", default="openai-completions")
    sp.add_argument("--base-url", dest="base_url")
    sp.add_argument("--credentials-ref", dest="credentials_ref")
    sp.add_argument("--model", help="default model id")
    sp.add_argument("--models", help="one model per line: id | name | contextWindow | maxTokens")
    sp.add_argument("--session-header", dest="session_header", action="store_true",
                    help="send x-opencode-session (required by OpenCode Go)")
    sp.add_argument("--display-name", dest="display_name")
    sp.add_argument("--headers", help="extra headers as JSON, e.g. '{\"x-foo\":\"bar\"}'")
    sp.add_argument("--from-file", dest="from_file", help="load the definition from a JSON file")
    sp.add_argument("--file", dest="pkg_file", help="provider package file (validate/import)")
    sp.add_argument("--key", help="credential for models/usage/test (never stored)")
    sp.add_argument("--refresh", action="store_true",
                    help="with `models`: fetch the list from the provider and save it")
    sp.add_argument("--detect-api", dest="detect_api", action="store_true",
                    help="with `models --refresh`: probe each new model to learn its protocol")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_provider)
    sp = sub.add_parser("plugin", help="curated plugin catalog (what users may install)")
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("pkg", nargs="?", help="npm package name, e.g. @scope/dsh-plugin-x")
    sp.add_argument("--version", help="pin a version (default: latest)")
    sp.add_argument("--note", help="one line shown in the panel")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_plugin)
    sp = sub.add_parser("plan", help="model plans: provider + credential + display name")
    sp.add_argument("action", choices=["list", "add", "rm", "delete", "enable", "disable",
                                       "default", "show", "sync", "key",
                                       "probe", "models", "usage"])
    sp.add_argument("--set", help="comma separated model ids (plan models)")
    sp.add_argument("--all", action="store_true", help="select every model the key can use")
    sp.add_argument("--clear", action="store_true", help="clear the model selection")
    sp.add_argument("id", nargs="?", help="plan id (plan_xxxxxxxx)")
    sp.add_argument("--name", help="display name (becomes the selector group name)")
    sp.add_argument("--provider", help="provider id (an imported package or a preset)")
    sp.add_argument("--user", help="owner username; omit for a team plan")
    sp.add_argument("--visible", help="comma/space separated users a team plan is visible to")
    sp.add_argument("--models", help="restrict the plan to these model ids (all by default)")
    sp.add_argument("--model", help="default model id for this plan")
    sp.add_argument("--key-file", dest="key_file", help="file containing the credential")
    sp.add_argument("--key", help="credential on the command line (use --key-file instead)")
    sp.add_argument("--default", action="store_true", help="make this team plan the default")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_plan)
    sp = sub.add_parser("template", help="import provider templates from the project repo")
    sp.add_argument("action", choices=["list", "import", "source"])
    sp.add_argument("name", nargs="?", help="template name (without .json)")
    sp.add_argument("--force", action="store_true", help="replace an existing package")
    sp.add_argument("--url", help="change the template source (base URL, https:// or file://)")
    sp.set_defaults(func=cmd_template)

    sp = sub.add_parser("mount", help="bind host directories into an instance jail")
    sp.add_argument("action", choices=["list", "add", "rm", "delete", "set"])
    sp.add_argument("host", nargs="?", help="host directory")
    sp.add_argument("jail", nargs="?", help="path inside the jail (default: same as host)")
    sp.add_argument("--instance", help="instance name")
    sp.add_argument("--all", action="store_true", help="apply to every instance")
    sp.add_argument("--rw", action="store_true", help="read-write (default: read-only)")
    sp.set_defaults(func=cmd_mount)

    sp = sub.add_parser("provider-test", help="probe a provider endpoint (real request, tiny completion)")
    sp.add_argument("--instance", help="use this instance's provider and credential")
    sp.add_argument("--name", help="probe a catalog entry instead of an instance")
    sp.add_argument("--key", help="override the credential for this probe (never stored)")
    sp.add_argument("--model", help="override the model")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_provider_test)
    sp = sub.add_parser("instance-patch", help="show the resolved provider patch for an instance")
    sp.add_argument("name")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_instance_patch)
    sp = sub.add_parser("admin", help="admin backend (loopback JSON API behind the gateway)")
    sp.add_argument("action", choices=["status", "install", "restart", "token"])
    sp.add_argument("--rotate", action="store_true", help="with `token`: generate a new token")
    sp.add_argument("--no-reload", dest="reload", action="store_false", default=True,
                    help="with `install`: do not restart the service")
    sp.set_defaults(func=cmd_admin)
    sp = sub.add_parser("backup", help="write a platform backup (records, optionally instance files)")
    sp.add_argument("--out", help="output file (default /var/backups/dsh-mu-<ts>.tgz)")
    sp.add_argument("--with-instances", dest="with_instances", action="store_true",
                    help="also archive each instance's home directory")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_backup)
    sp = sub.add_parser("restore", help="restore a platform backup")
    sp.add_argument("file")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true", help="show the plan only")
    sp.add_argument("--force", action="store_true", help="actually overwrite the live platform")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_restore)
    sp = sub.add_parser("logs", help="platform log maintenance")
    lsub = sp.add_subparsers(dest="subcommand", required=True)
    sub_size = lsub.add_parser("size", help="show log file sizes")
    sub_size.add_argument("--json", action="store_true")
    sub_size.set_defaults(func=cmd_logs_size)
    sub_rot = lsub.add_parser("rotate", help="size-based copytruncate rotation (fallback for logrotate)")
    sub_rot.add_argument("--max-mb", type=int, default=DEFAULT_LOG_MAX_MB)
    sub_rot.add_argument("--keep", type=int, default=DEFAULT_LOG_KEEP)
    sub_rot.add_argument("--force", action="store_true", help="rotate even below the threshold")
    sub_rot.add_argument("--json", action="store_true")
    sub_rot.set_defaults(func=cmd_logs_rotate)
    sp = sub.add_parser("sync-state", help="reconcile stored instance state with what is listening")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sync_state)
    sp = sub.add_parser("doctor", help="diagnose host prerequisites (run this first on a new machine)")
    sp.add_argument("--json", action="store_true", help="machine-readable report (for the admin backend)")
    sp.add_argument("--fix", action="store_true", help="apply the safe fixes it knows (gateway install, sync, cgroup)")
    sp.set_defaults(func=cmd_doctor)
    sp = sub.add_parser("selftest", help="end-to-end verification")
    sp.add_argument("--host")
    sp.add_argument("--user", help="account used for the authenticated checks")
    sp.add_argument("--password")
    sp.add_argument("--ws-hold", type=int, help="milliseconds to hold the WebSocket (default 15000)")
    sp.set_defaults(func=cmd_selftest)
    sp = sub.add_parser("acceptance", help="run the acceptance suite (optionally the fresh-machine checklist)")
    # Optional positionals mirror tests/acceptance.sh: <instance> <user> <password>
    sp.add_argument("rest", nargs="*", help="[instance [user [password]]]")
    sp.add_argument("--instance")
    sp.add_argument("--user")
    sp.add_argument("--password")
    sp.add_argument("--fresh", action="store_true",
                    help="run tests/fresh-machine.py: end-to-end checklist for a new machine")
    sp.set_defaults(func=cmd_acceptance)
    sp = sub.add_parser("migrate", help="migrate state/configuration to the current schema")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true", help="report only")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_migrate)
    sp = sub.add_parser("tls", help="manage the HTTPS certificate and switch")
    sp.add_argument("action", choices=["status", "self-sign", "import", "enable",
                                       "disable", "redirect", "keep-http"])
    sp.add_argument("--cn", help="common name for self-sign (default: hostname)")
    sp.add_argument("--hosts", nargs="*", help="extra DNS names for the SAN list")
    sp.add_argument("--days", type=int, help="validity in days (default 825)")
    sp.add_argument("--cert", help="certificate chain (PEM) for import")
    sp.add_argument("--key", help="private key (PEM) for import")
    sp.add_argument("value", nargs="?", help="on|off for redirect / keep-http")
    sp.set_defaults(func=cmd_tls)
    sp = sub.add_parser("config", help="show or set platform configuration (dsh-mu.conf)")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--write-default", action="store_true",
                    help=f"rewrite {CONF_PATH}.default from the schema and exit")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = args.func(args)
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
