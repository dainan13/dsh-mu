# 10 · 网关放进 jail（方案与进度）

> 你的三个决定：① 平台 jail **直接绑宿主 80/443**（没特殊需求的用户就用我们配好的 openresty；
> 有特殊需求的用户把这份配置抄到自己的 http 服务上，不用我们的）；② 配置/日志放 jail 的持久卷；
> ③ `dsh-mud` 也一起放进这个"平台 jail"。
>
> 状态：**已实现、已在实机跑通、并已切换为默认**（`GATEWAY_MODE=jail`）。宿主上不再需要 openresty，
> `apk add dsh-mu` 也不再依赖它；本文件记录设计、过程中的坑与剩余工作。

## 1. 设计

新增一个"平台 jail"，和用户实例共用同一个 runner（`dsh-mu-jail`）与同一套隔离手段
（mount-ns + chroot + cgroup + 能力清空），差别只在挂载与命令：

```
ROOTFS        /var/lib/dsh-mu/gateway/rootfs      ← 由 gatewy 模板（镜像 + openresty + lua-resty-*）复制
INSTANCE_HOME /var/lib/dsh-mu/gateway/home        ← runner 需要，未使用
INSTANCE_WORK /var/lib/dsh-mu/gateway/work        ← 同上
PORT          80（或 --port 指定）
CGROUP_PATH   /sys/fs/cgroup/dsh-mu/gateway
KEEP_CAPS     net_bind_service                    ← 唯一保留的能力，其余全部清空
EXTRA_MOUNTS  /etc/dsh-mu:/etc/dsh-mu:ro
              /usr/local/lib/dsh-mu:/usr/local/lib/dsh-mu:ro
              /var/log/dsh-mu:/var/log/dsh-mu:rw
              /var/lib/dsh-mu/gateway/conf:/etc/dsh-mu-gateway:rw
```

- **配置**：`/var/lib/dsh-mu/gateway/conf/{nginx.conf,conf.d,http.d}`（持久卷，②）。
  主 `nginx.conf` 是我们自己写的（不再去改发行版那份，也不再需要 `http.d/include` 探测）；
  `/etc/nginx` 保持镜像自带，以便 `mime.types` 等仍可引用。
- **日志**：`/var/log/dsh-mu/gateway.log`、`gateway.err.log`（②，与现在同一目录）。
- **端口**：`listen 80 default_server`（①）；`KEEP_CAPS=net_bind_service` 已在 runner 里实现。
  有特殊需求的用户可以把 `/var/lib/dsh-mu/gateway/conf` 整份抄走，用他自己的 http 服务加载。

## 2. 已实现（代码在仓库里，默认不启用）

| 位置 | 内容 |
|---|---|
| `cli/dshmuctl.py` | `GATEWAY_MODE` 配置项；`build_gateway_jail()`、`write_gateway_jail_conf()`、`render_gateway_jail_conf_files()`、`render_gateway_service()`、`gateway_health()`；`gateway build/start/stop/restart/conf/logs/jail-status` 子命令（并入原有 `gateway` 命令，`status` 仍是宿主 nginx 的） |
| `jail/dsh-mu-jail` | 新增 `--conf FILE`（平台 jail 用自己的 conf）与 `KEEP_CAPS=net_bind_service`（只保留这一个能力） |
| 模板 | `build_template(..., dest=, packages=)` 支持第二套模板（`/var/lib/dsh-mu/templates/gateway`，在镜像基础上加 `openresty lua-resty-string lua-resty-hmac lua-resty-openssl lua-resty-core`） |

已验证：配置渲染（`conf/nginx.conf` + `conf.d/dsh-mu-http.conf` + `http.d/dsh-mu-server.conf`）、
service 文件渲染（`command_args` 指向 `--conf … __gateway -- openresty -c …`，占位符全部填好）、
runner 的 `--conf` 与 `KEEP_CAPS` 语法（`sh -n` 通过）。

## 3. 进展与卡点

### 3.1 这轮修掉的三个真问题（都不是"实验性"问题，而是会影响正式安装）

1. **交互提示在脚本里也会触发**：原来只用 `sys.stdin.isatty()` 判断，但 `chroot … > log`
   的 stdin 仍是终端 → 安装会**读走脚本的下一条命令**当"选择模板"。现在要求
   **stdin 与 stdout 都是 TTY** 才提示（`_interactive()`），并把 EOF/^C 当作"用默认值"
   （`_ask()`），同时支持 `DSHMU_NONINTERACTIVE=1`。
2. **宿主没有 `xz`**：`tar -xJf` 在最小 Alpine 上直接失败（`xz: Cannot exec`）——
   这正是之前"模板只有 1 MB / 静默失败"的真正原因。现在用 **Python `tarfile`**（stdlib
   自带 lzma）解压，不再依赖外部 `xz`；已用不装 xz 的机器实测解压成功。
3. **`openresty` 包的二进制叫 `/usr/sbin/nginx`**（不是 `openresty`），且 `-g 'daemon off;'`
   经 OpenRC `command_args` 会被拆掉引号（`nginx: invalid option: "off"`）→ 改为把
   `daemon off;` 写进配置文件、命令行只留 `-c`。

### 3.2 平台 jail 已经能起到这一步（实机验证）

```
模板 built: 214 MB（镜像 + openresty + lua-resty-core/hmac/lrucache/openssl/string）
rootfs 复制成功，/usr/sbin/nginx 存在
jail 启动（runner → chroot → nginx 被 exec 起来）
/etc/dsh-mu 用的是 jail 自己的副本：admin.token / secret.key 的 gid 已改为 jail 内的 nginx gid（101）
```

### 3.3 端到端已经打通（jail 里的网关在 18080 上服务）

```
dsh-muctl gateway start --port 18080   ->  {"http": 200, "ok": true}
GET  /login  -> 200（表单含 csrf）
POST /login  -> 302（以 alice 登录成功）
GET  /me     -> 200  {"gateway":{...},"admin":false,...}
GET  /       -> 200  34941 字节 = 真的代理到了 alice 的实例页面
```

关键设计（这轮定下来的）：

- **平台 jail 内 nginx 以 root 运行**（`user root;`）：jail 本身就是边界；这样 nginx 不需要
  把临时目录 chown 给别的 uid，也就不需要额外能力；
- **只保留 `CAP_NET_BIND_SERVICE`**（`KEEP_CAPS=net_bind_service`）——绑 80/443 用，其余全清；
- `/etc/dsh-mu` 用 **jail 自己的副本**（`/var/lib/dsh-mu/gateway/etc`，只读挂入），
  `secret.key`/`admin.token` 的属组改成 jail 内的 nginx gid —— 避免宿主/jail 的 gid 映射问题；
  该副本在 `sync_all` 里自动刷新（jail 模式）；
- 配置卷 `gateway/conf`（rw）挂到 `/etc/dsh-mu-gateway`，日志挂 `/var/log/dsh-mu`（rw），
  库 `/usr/local/lib/dsh-mu`（ro），cgroup 独立（`/sys/fs/cgroup/dsh-mu/gateway`）。

### 3.4 原来的卡点记录（已解决，保留）

```
nginx: [emerg] chown("/var/tmp/nginx/client_body", 100) failed (1: Operation not permitted)
```

即 nginx 启动时要把它自己的临时目录 `chown` 给 nginx 用户（jail 内 uid 100），
但我给的 `KEEP_CAPS=net_bind_service,chown,setuid,setgid` **没有生效到 nginx 进程**
（`/etc/dsh-mu/gateway.conf` 里参数是对的，问题在 setpriv 的 ambient/inheritable 能力传递，
下一轮用 `capsh`/`/proc/self/status` 直接验证并修正）。

### 3.4 原来的记录（保留）

## 3. 卡点（这轮没跑通）

在 **实机（生产）** 上 `dsh-muctl gateway build --force` 时：

- 网关模板只建出 **1 MB**（应有 ~170 MB），随后 `--force` 重跑**被静默杀掉**（脚本中途终止、无任何输出）；
- 当时机器 **可用内存只有 ~2 GB**（31 GB 里用了 28.7 GB，两个实例在跑）；
- 我的脚本在 `rm -rf` 掉 rootfs 的同时，`dsh-mu-gateway` 服务正被 respawn 循环反复拉起 ✗（自己踩的）。

已处理：停服务、`rc-update del`、删服务文件与半成品目录；生产恢复（`doctor 36/0/0`、alice/bob 在跑、
宿主 `:80` 登录页 200）。

**结论**：这台机当前不适合做这种"重建 170 MB 模板"的实验；下一步应放在
`tests/fresh-rehearsal.sh` 的**干净 chroot** 里做（内存充足、无 respawn 干扰、自带 curl/tar-xz），
跑通后再切默认并更新 doctor 与测试。

## 4. 第 ③ 项（`dsh-mud` 也进 jail）的真实障碍

`dsh-mud` 不只是"一个 HTTP 服务"：它**执行平台变更**，为此要调用宿主的
`rc-service`（启停实例服务）、`/usr/local/sbin/dsh-mu-jail`（拉 jails）、`chroot`、写
`/sys/fs/cgroup/**`、读写 `/var/lib/dsh-mu/instances/**`、以及 `/etc/init.d/*`。
把这些能力关进 jail，就得给平台 jail 反向开放：

```
/etc/dsh-mu           rw      （state.json、admin.token、secret.key）
/var/lib/dsh-mu       rw      （instances、模板、日志）
/sys/fs/cgroup        rw      （实例限额与 cgroup.kill）
/run + /etc/init.d    rw      （OpenRC 控制实例服务）
/usr/local/sbin,lib   ro      （runner 与库）
+ CAP_SYS_ADMIN / CAP_DAC_OVERRIDE 等
```

也就是一个**特权平台 jail**：它对上述路径等价于宿主 root。它能做到"宿主只留 runner 和 init"，
但**平台 jail 自身被攻破就等于宿主被攻破**——隔离收益主要落在"装什么不用问用户"，而不是安全边界。

两个可选形态（等你选）：

- **(A) 特权平台 jail**：openresty + `dsh-mud` 都进同一个 jail（挂载如上一段）。
  好处：宿主上只剩 `dsh-mu-jail` + 几个 init 脚本；坏处：平台 jail ≈ 宿主 root。
- **(B) 只把 openresty 放进 jail**，`dsh-mud` 留在宿主（它本来就是**单文件 Python，零安装步骤**）。
  好处：不需要特权 jail，安全边界干净；坏处：宿主仍跑 Python 后端（但"装什么"的问题已经不存在了）。

我的建议是 **(B)**；如果你要 (A) 我也能做，只是需要明确接受上面那条"平台 jail ≈ 宿主 root"的代价。

## 5. 后续步骤（按顺序）

1. 在排练 chroot 里跑通平台 jail：`gateway build`（在 chroot 内）→ `openresty -t` → 启动 → `:8080` 访问
   `/login`、`/admin/`、以及经 jail 代理到实例；
2. 把 `doctor` 的 nginx 检查分成 host/jail 两种模式；`GATEWAY_MODE=jail` 时跳过宿主 fragment 安装；
3. 切默认到 `jail`（同时 `apk` 依赖里去掉宿主 openresty —— 这样"干净 Alpine 装 dsh-mu"不再需要 nginx）；
4. 更新受影响的测试（`fresh-rehearsal`、`isolation`、`selftest` 6b、`failure-modes`、`log-rotation`、
   `docker/acceptance` 里对 `nginx -T` 的依赖）；
5. 再谈第 (3) 项（抽屉 + provider）。
