# dsh-mu — DeepSeek Harness 多实例托管平台

一个统一网关 + 服务端 CLI，把多个 DeepSeek Harness 实例托管在同一台 Alpine 主机上：

- 用户只访问 `http://<网关>/`，未登录跳登录页，登录后按会话被反向代理到**自己的实例**；
- 每个实例跑在独立的 **jail**（mount namespace + UTS namespace + chroot + cgroup v2）里，
  有各自的 Alpine 根文件系统、各自的 `apk` 数据库与 `/etc`，网络与内核共享（不做 netns，符合"软隔离"定位）；
- 实例的 DSH 启动 token 由网关**代持**：用户在浏览器里永远看不到 token，也不需要书签取 token；
- 全部管理动作在终端完成（`dsh-muctl`），网关侧只有 OpenResty + Lua，无常驻 Node 服务。

> 设计与实测记录：`docs/01-需求整理.md`、`docs/02-架构设计.md`、`docs/03-M1-实测报告.md`

## 组件

> 网关（openresty）运行在**平台 jail** 里：宿主不需要 nginx，配置/日志在
> `/var/lib/dsh-mu/gateway/{conf,logs}`，平台 jail 只保留 `CAP_NET_BIND_SERVICE`（绑 80/443）与自己的 cgroup。
> `GATEWAY_MODE=host` 可回到老方式。详见 `docs/10-网关进jail.md`；
> 运维命令：`dsh-muctl gateway build|start|stop|restart|jail-status|logs`。

| 组件 | 位置 | 说明 |
|---|---|---|
| 网关 | `/etc/nginx/{conf.d,http.d}/dsh-mu-*.conf` + `/usr/local/lib/dsh-mu/lua/*.lua` | OpenResty：登录页、会话 cookie、按会话选上游、上游 cookie 注入、WebSocket 反代 |
| 控制面 CLI | `/usr/local/sbin/dsh-muctl` | Python3 标准库；用户、实例、限额、日志、渲染状态视图、OpenRC 服务 |
| jail 运行器 | `/usr/local/sbin/dsh-mu-jail` | POSIX sh；unshare → bind 挂载 → chroot → 丢 capability → exec `dsh web` |
| cgroup 委派 | `/usr/local/sbin/dsh-mu-cgroup-init` + OpenRC `dsh-mu-cgroup` | 每次开机把 cgroup 控制器委派给 `/sys/fs/cgroup/dsh-mu` |

## 安装

### 一行安装（从 GitHub Release，推荐）

目标机是 Alpine（3.22+，apk-tools 3），并且能访问外网时：

```sh
cd /tmp
wget -O dsh-mu.apk \
  https://github.com/dainan13/dsh-mu/releases/latest/download/dsh-mu.apk
apk add --allow-untrusted ./dsh-mu.apk
```

> ⚠ `apk add <URL>` **不行** —— apk-tools 3 只接受**本地文件路径或仓库里的包名**，
> 所以必须先 `wget`（或 `curl -LO`）下载成本地文件再装。这一点在 Alpine 3.24 + apk-tools 3.0.6 上实测过。

- `--allow-untrusted` 是**必需**的：包是**未签名**的（签名分发见文末"已知限制"）。
- 这个 URL **永远指向最新版本**，并且**文件名固定为 `dsh-mu.apk`** —— 所以可以原样写进脚本 / cloud-init / 装机清单。
- 想校验就顺手下载 `SHA256SUMS`：`wget -O SHA256SUMS .../SHA256SUMS && sha256sum -c SHA256SUMS`
  （GitHub 的 asset 名就是 `SHA256SUMS` ✓）。
- 装完 `apk add` 只安**程序**；**平台本身**（账号、实例、网关）还需要跑一次：

```sh
dsh-muctl install --gateway-host <这台机器的主机名或 IP>
```

要固定某个版本就把 `latest/download/dsh-mu.apk` 换成
`download/v0.15.1/dsh-mu-0.15.1-r0.apk`（每个 Release 里同时挂着这两个名字 + `SHA256SUMS`）。

> 内网/离线机器：先在一台能上网的机器 `wget` 下 `dsh-mu.apk`，`scp` 上去，
> 再 `apk add --allow-untrusted ./dsh-mu.apk`（同样是本地文件路径）。

### 从源码安装（开发机打包）

```sh
# 在开发机上打包
tar czf dsh-mu-src.tgz -C dsh-mu .
# 上传到目标机（scp / sftp），然后
ssh root@dsh-mu
rm -rf /root/dsh-mu-src && mkdir -p /root/dsh-mu-src
tar xzf /root/dsh-mu-src.tgz -C /root/dsh-mu-src
sh /root/dsh-mu-src/install/install.sh --gateway-host dsh-mu
```

`install.sh` 会：装 `python3` → 复制程序到 `/usr/local/{sbin,lib}`（含 nginx 片段模板）→
交给 `dsh-muctl install` 装依赖、全局安装 `@deepseek-ai/dsh`、构建 jail 模板、
**把网关以片段方式装进 nginx**（不覆盖 `nginx.conf`）、注册并启动 `dsh-mu-cgroup`、重载 openresty。

nginx 集成方式（可重复执行，幂等）：

```sh
dsh-muctl gateway install          # 渲染片段 + 插入受管 include + nginx -t + reload
dsh-muctl gateway install --no-reload   # 只写文件、只做校验
```

1. 片段模板在 `/usr/local/lib/dsh-mu/nginx/`，渲染后写到
   `<NGINX_CONF_D_DIR>/dsh-mu-http.conf`（http 级：`lua_shared_dict`、`map`）
   与 `<NGINX_HTTP_D_DIR>/dsh-mu-server.conf`（server 级：监听端口、`server_name`、路由）；
2. 在 `/etc/nginx/nginx.conf` 的 `http {` 之后**插入一行受管 include**（带标记，幂等）；
   编辑前自动备份，`nginx -t` 失败会自动回滚；
3. 若目标机已有别的配置占用 `default_server`，片段就退化为按 `server_name` 匹配（安装日志会说明）；
4. 检测到历史遗留的"整文件覆盖"安装时会自动还原发行版默认配置，再走片段流程。

首次构建 jail 模板需要几分钟（模板内含 node/npm/pnpm/git/bash，约 200+ MB）。

## 常用命令

```sh
# 账号
dsh-muctl user add alice                       # 自动生成密码并打印一次；自动建同名实例
dsh-muctl user add boss --role admin --instance none
dsh-muctl user list
dsh-muctl user passwd alice                    # 改密并让该用户所有旧会话立即失效
dsh-muctl user disable alice

# 实例
dsh-muctl instance create alice --cpu "100000 100000" --mem 1610612736 --pids 512
dsh-muctl instance start alice                 # 首次启动会安装 profile，可能几分钟
dsh-muctl instance status
dsh-muctl instance logs alice -n 200
dsh-muctl instance restart alice
dsh-muctl instance limits alice --mem 3221225472
dsh-muctl instance api-key alice                 # 用平台统一 key 注入（或 --remove 移除）
dsh-muctl instance provider alice opencode --key-file /root/oc-key.txt   # 切到 opencode 推理端点
dsh-muctl instance provider alice show | clear   # 查看 / 撤销 provider 覆盖
dsh-muctl instance owner alice --uid 0           # 查看/切换实例运行 uid
dsh-muctl instance shell alice                 # 管理员进 jail 排查
dsh-muctl instance refresh-cookie alice        # 上游 cookie 失效时重取
dsh-muctl instance del alice [--purge]

# 平台
dsh-muctl config                               # 查看配置
dsh-muctl config model_api_key sk-xxxx         # 平台统一模型 key（写入新实例的 DSH_HOME）
dsh-muctl gateway status
dsh-muctl gateway reload
dsh-muctl sync                                 # 改过配置后重新渲染网关视图
dsh-muctl sync-state [--json]                  # 按端口实测纠偏实例状态（网关/doctor 用同一套判断）
dsh-muctl doctor [--json|--fix]                # 新机器第一步：31 项前置条件自检
dsh-muctl logs size [--json]                   # 看日志体积
dsh-muctl logs rotate [--force]                # 手工轮转（logrotate 的兜底）
dsh-muctl backup [--out F] [--with-instances]  # 平台备份（账号/实例记录/配置/密钥，0600）
dsh-muctl restore F [--dry-run|--force]        # 恢复（先看计划；执行前自动存安全副本）
dsh-muctl admin status|install|restart|token   # 管理后端的服务与令牌
dsh-muctl bootstrap                            # 无任何账号时创建首个 admin（密码只打印一次）
dsh-muctl provider list|show|add|set|rm        # provider 目录（内置 + 自定义）
dsh-muctl provider-test --instance <名>        # 发一个最小请求验证端点/凭据（不打印 key）
dsh-muctl instance-patch <名> [--json]         # 该实例最终生效的 provider patch 与密钥来源
dsh-muctl acceptance [实例 用户 密码] [--fresh]  # 验收套件；--fresh = 全新机器清单
dsh-muctl migrate [--dry-run|--json]           # state/配置 schema 迁移（可先 dry-run 排雷）
dsh-muctl selftest --user alice --password 'xxx'
```

## 退出登录

- UI 左下角、DSH 自带「设置」按钮**正上方**有一个淡色的「退出登录」按钮（悬停变亮）：
  网关在代理索引页时注入的（只作用在索引请求，不影响 `/api`、`/plugins` 与 WebSocket 的流式路径）。
  位置是固定定位（`left:12px; bottom:52px`），如果 DSH 改版后仍与「设置」重叠，改
  `gateway/dsh-mu-server.conf` 里 `location = /` 的 `bottom` 值并重跑
  `dsh-muctl gateway install` 即可。
- 或者直接访问 `http://<网关>/logout`（可收藏）。
- 退出只清掉**网关会话 cookie**，不会停止实例、不动实例数据；重新登录仍回到同一个实例。
- 其他标签页/设备：页面请求会被跳回登录页，`/api` 请求返回 401。
- 想让**所有设备**上的会话立即失效：`dsh-muctl user passwd <名>`（自增 epoch，旧 cookie 全部作废）；
  想直接停用账号：`dsh-muctl user disable <名>`。

## 工作原理

```
浏览器 ──> openresty :80
             ├─ /login  /logout        Lua：平台账号 + 会话 cookie（HMAC 签名，无状态可撤销）
             ├─ /mu-api/*              Lua：仅 loopback 的运维 JSON API
             └─ /                      auth.lua 解析会话 → 实例 → proxy_pass
                                        Host/Origin 归一化为 127.0.0.1:<实例端口>
                                        Cookie 替换为该实例自己的 dsh-auth-*
                                                    │
                                                    ▼
                                       jail 内 dsh web（只监听 127.0.0.1:<端口>）
```

- **为什么不改 DSH 源码**：DSH 没有"关闭鉴权"的开关；网关用实例的 launch token 换取实例自己的
  30 天签名 cookie 并保存，浏览器只持有平台会话 cookie。cookie 的 authority 恒为 `127.0.0.1:<端口>`，
  与转发时的 Host 一致，因此长期有效；实例重启（`DSH_HOME` 不变）也不需要重取。
- **为什么不做路径前缀路由**：DSH 的 UI 使用 `/api/*`、`/plugins/*` 等绝对路径，且 CLI 无路径前缀配置。
  路由粒度就是"一个会话 → 一个上游"。
- **上游信任栅栏**：转发时 `Host` 与 `Origin` 都改写成 loopback authority，
  DSH 的 `/api` 栅栏自动放行，无需 `--trusted-host`。
- **apk 与目录隔离**：每个实例一份模板拷贝；jail 内 `apk add` 只写自己的 rootfs。
  DSH 全局包（依赖树很大）从宿主只读绑定进 jail，节省磁盘。

## 隔离边界（务必理解）

每个实例由 `/usr/local/sbin/dsh-mu-jail` 组装，边界是**这一组东西**，缺一不可：

| 机制 | 作用 |
|---|---|
| mount namespace（`unshare --mount --uts --propagation private`） | 挂载表私有，实例内的挂载不影响宿主；宿主的其它路径根本不可达 |
| chroot 到实例 rootfs | 只有实例自己的 Alpine 根文件系统 + 显式绑定进来的目录可见 |
| **capability 全部丢弃**（`setpriv --bounding-set=-all --inh-caps=-all --ambient-caps=-all`） | 实例内无法 mount、无法 mknod、无法 ptrace 宿主、无法绕过文件权限 |
| `no_new_privs` | 不能再通过 setuid 程序提权 |
| cgroup v2 子组 | `cpu.max` / `memory.max` / `memory.high` / `pids.max` 限额，`cgroup.kill` 精确回收整棵进程树 |
| 私有 `/dev`（tmpfs + 最小设备集 + 独立 devpts/shm） | 实例不能删改宿主设备节点 |
| 私有 `/proc`（新 procfs 实例）、`/sys` 只读 | 不能写宿主的 proc/sys |
| 网络共享（**无 netns**）+ **出网策略** | 实例只监听 `127.0.0.1:<port>`，外部无法绕过网关；出网由 nftables **按 cgroup** 限制：默认**禁内网**，只放行自身端口 / relay / DNS / 公网（可配白名单，见下） |

实例**以 jail-root（uid 0）运行**，这与 FreeBSD jail 的惯例一致，也是 jail 内 `apk add` 可用的前提。
注意一个反直觉的细节：capability 全丢之后，uid 0 **不再拥有 `CAP_DAC_OVERRIDE`**，
所以实例数据目录必须属于实例运行 uid（`dsh-muctl instance owner <名字> --uid 0`），否则实例写不了自己的 `DSH_HOME`。

如果想改成非 root 运行（`dsh-muctl config instance_run_uid auto`），需要满足两个前提：
user namespace 可用（本 LXC 容器内 **chroot 之后不可用**，实测 `EPERM`），
且 apk 能以该 uid 写自己的数据库（实际不可行）。因此默认保持 jail-root。

DSH 自身的 agent 沙箱在此环境不可用（bwrap 需要非特权 userns → 被 LXC 禁；Landlock → 内核/容器不支持，
实测 `landlock_create_ruleset` 返回 `EOPNOTSUPP`），而 DSH 的沙箱策略是 fail-closed。
因此实例默认带 `DSH_PERMISSION_MODE=danger-full-access` 运行——**jail 本身就是沙箱**。
可用 `dsh-muctl config instance_permission_mode workspace-write` 改回受限模式（那样 agent 的 bash 工具会拒绝执行）。

## 出网策略（0.19.0 起默认禁内网）

实例共享宿主网络，但**出网受策略约束**（`dsh-muctl egress` / 后台实例行「配置」）：

| 策略 | 公网 | 内网 | 特殊规则 |
|---|---|---|---|
| **全限制** | 拒 | 拒 | 只允许列出的目标 |
| **禁内网**（默认） | 放行 | 拒 | 例外放行 |
| **不限制** | 放行 | 放行 | 不生效 |

- 目标支持 `ip` / `ip/24` / `ip1-ip2`，可带 `:443` 或 `:10000-11000`；特殊规则可以挂在**全部实例**上；
- 自身端口、`relay`、DNS 永远放行（否则实例自己就不可用）；
- `nftables` 是 **apk 依赖**（0.21.3 起随包安装），内核 ≥ 5.13；改完**立即生效**，不用重启实例；
- 详见 [docs/06-运维手册.md](docs/06-运维手册.md) ✓

## 插件：平台不管"谁能装什么"，只管两个开关

用户在**自己实例的 DSH 界面**里装插件 ✓。平台提供两个独立策略（后台「插件」页 / `dsh-muctl plugin`）：

- **受保护**：`node_modules/<pkg>` 在 jail 内只读绑定（内核 EROFS），删不掉也停不掉；
- **强制安装**：新实例自动装上。

做法与边界见 [docs/13-插件.md](docs/13-插件.md) ✓。

## 模型 provider 接入

DSH 有两个适配层，平台都支持：

| preset | 走哪个适配层 | 协议 / 端点 | 适用 |
|---|---|---|---|
| `opencode-go` | `dsh-llm-pi-ai`（多 provider） | `openai-completions` → `https://opencode.ai/inference/go/v1` | **OpenCode Go 订阅**（$10/$40 月），当前实例用的就是这个 |
| `opencode` | `dsh-llm-deepseek` | `anthropic-messages` → `https://opencode.ai/inference/anthropic` | opencode 标准产品（按量付费余额） |
| `deepseek` | `dsh-llm-deepseek` | `anthropic-messages` → `https://api.deepseek.com/anthropic` | DeepSeek 官方 key |

```sh
dsh-muctl instance provider alice opencode-go --key-file /root/oc-key.txt   # 首次接入要带 key
dsh-muctl instance provider alice opencode-go --model kimi-k3              # 只换默认模型
dsh-muctl instance provider alice show | clear                            # 查看 / 撤销
dsh-muctl instance restart alice
```

`instance provider` 做三件事：

1. 把 key 写进该实例的 DSH 凭据库（等价于 `instance api-key`，**不会动** 浏览器会话签名记录）；
2. 在实例 home 生成补丁文件 `.dsh-mu-provider.yml`：`opencode-go` 写入 `llm-pi-ai` 的
   provider 路由（baseURL / `api: openai-completions` / 模型目录 / `x-opencode-session` 头）
   并改 `agent-default-model`；`deepseek`/`opencode` 则改 `llm-deepseek` 的 `baseURL`+`models`；
3. 在实例配置里打开 `DSH_LAUNCH_ARGS="--patch /home/dsh/.dsh-mu-provider.yml"`，jail 启动时以
   `dsh --patch <文件> --profile web …` 组合。补丁放在启动层而不是 `DSH_HOME` 用户层，
   插件安装/Profile 重建都不会覆盖它。

### 接 opencode 时踩到的三个点（都已解决）

1. **产品与协议是分开的**。同一个 key：`/inference/anthropic/v1/messages`（standard，我们的 Messages 适配层）
   返回 `402`；`/inference/go/v1/chat/completions`（product=go，OpenAI Chat 协议）返回 `200`。
   所以 Go 订阅必须走 `dsh-llm-pi-ai` 的 `openai-completions`，不是 `dsh-llm-deepseek`。
2. **必须发 `x-opencode-session`**，否则 `400 MissingSessionID`。该头由路由的 `headers` 提供，
   值取 `dsh-mu-<实例名>`（实例期内稳定，满足"每个会话一个稳定 ID"的路由/缓存优化要求）。
3. **一个 route 只能说一种协议**，而 Go 的模型分散在三种端点：`/chat/completions`（DeepSeek、GLM、Kimi、
   LongCat、MiMo、Hy3、Space Bunny、免费的 LongCat 2.5/Step 5）、`/messages`（MiniMax、Qwen、Claude Haiku）、
   `/responses`（Grok、GPT、Muse Spark）。`opencode-go` 预设只收录前者；要用 Qwen/Claude 这类，
   需要再加一个 `anthropic-messages` 路由指向 `https://opencode.ai/inference/go/v1`。

另注：opencode 的 Cloudflare 会拦**没有 User-Agent** 的请求（`403 error code: 1010`）；
DSH 自带 UA（`deepseek-harness/…`）不受影响，脚本自测时要记得带 UA。
`*-free` 模型在服务器直连会被拒（`403 FreeTierError`：免费额度仅限 opencode 官方客户端）。

**实测结果**：jail 内 `dsh --patch … --profile headless "Reply with exactly one word: PONG"` → 返回 `PONG`；
网关 Web UI 自检 `ALL CHECKS PASSED`。

## 文档

| 文件 | 内容 |
|---|---|
| [docs/01-需求整理.md](docs/01-需求整理.md) | 需求与已确认决策 |
| [docs/02-架构设计.md](docs/02-架构设计.md) | 架构与实现契约（§10 为当前实现） |
| [docs/03-M1-实测报告.md](docs/03-M1-实测报告.md) | M1 实测记录 |
| [docs/04-验收报告.md](docs/04-验收报告.md) | 逐轮验收证据 |
| [docs/05-打包与安装.md](docs/05-打包与安装.md) | 打包、安装、升级、卸载 |
| [docs/06-运维手册.md](docs/06-运维手册.md) | 日常操作、故障对照表、安全清单、**出网策略 / 插件策略 / 套餐隐藏与顺序** |
| [docs/07-证据索引.md](docs/07-证据索引.md) | 每条能力主张指向可复现的验证脚本与最近结果 |
| [docs/11-模型接入.md](docs/11-模型接入.md) | provider 目录、套餐、推理等级、默认链、**搜索套餐 / 隐藏与顺序** |
| [docs/13-插件.md](docs/13-插件.md) | **插件：受保护 / 强制安装**（平台不再管"谁能装什么"） |
| [docs/08-验收流程.md](docs/08-验收流程.md) | **验收走查清单**：从空机器到 18 层全绿，含签收表 |
| [docs/07-证据索引.md](docs/07-证据索引.md) §5 | **需求追溯**：34 条 FR 逐条现状（26 条已实测 / **7 条已接受的偏差** / 1 条待你决定，无静默丢弃） |

## 会话与跨站防护

- 平台会话 cookie：`HttpOnly; SameSite=Lax`（Lua 统一签发），登录表单另有**双提交 CSRF** 校验。
- 写操作还有第二道**同源栅栏**（`dshmu.origin_ok()`）：浏览器发出的 `POST/PUT/PATCH/DELETE` 若带
  `Origin` 且与 `Host` 不一致 → **403**；`GET/HEAD` 不拦（安全方法）；不带 `Origin` 的客户端
  （CLI、探针、`curl`）放行，因此自动化不受影响。`/me`（改密码、启动实例）与 `/admin/` 全路径都经过它。
- 上游 DSH 侧另有它自己的 trust fence（`Host` 必须是回环/受信主机且 `Origin` 等于 `Host`），
  网关代持每实例独有的 HMAC cookie，用户拿不到也伪造不了。
- 实测（`tests/selftest.mjs` 5f）：跨站 `POST /me` → 403；跨站 `GET /me` → 200；同站 POST → 通过；
  无 `Origin` 的 POST → 通过。

## 审计留痕

`/var/log/dsh-mu/audit.log`（JSONL）记录**所有**变更，包括没有经过浏览器的那种：

```json
{"action": "state.change",  "actor": "cli:root@dsh-mu", "target": "platform",
 "detail": {"cmd": "…/dsh-muctl user add carol --password *** --instance none",
            "users": {"added": ["carol"]}}, "ip": "cli", "ts": 1792132309}
{"action": "config.change", "actor": "cli:root@dsh-mu", "target": "dsh-mu.conf",
 "detail": {"cmd": "…/dsh-muctl config session_days 7", "keys": ["session_days"]}, "ip": "cli"}
{"action": "config_set", "actor": "root", "target": "", "ip": "127.0.0.1", "detail": {"status": 200}}
```

- **怎么做到不漏**：所有状态变更都经过 `save_state()`／`save_config()`／`save_custom_providers()`，
  这三处比较"改动前后快照"并写一条差异记录（`added`/`removed`/`changed` 的账号、实例；改动的配置键；
  增删改的 provider）。因此**任何**命令——包括将来新增的——都会留痕，不依赖每个命令自觉。
- **不记秘密**：只记录实体名与变更类型，外加一条**打码后的命令行**（`--password/--api-key/--token` 等
  后面的值、以及长随机串一律替换为 `***`）；口令、模型 key、会话 cookie 永不落盘。验收脚本里有一条
  检查专门断言"用真实口令建号后，日志里搜不到该口令"。
- **谁在改**：CLI 记 `cli:<系统用户>@<主机>`（可用 `DSHMU_ACTOR` 覆盖）；`/admin/` 页面与运维 API 由
  `dsh-mud` 记**请求级**记录（真实管理员账号 + 客户端 IP + 状态码 + 请求体，敏感字段剔除）。
  `/api/instances/<n>/ensure` 只在**真的拉起实例**时记录，避免每次翻页都刷日志。
- 日志按 `logrotate` 轮转（daily + maxsize 16M + copytruncate，保留 14 份）。

## 备份与恢复

```sh
dsh-muctl backup --out /var/backups/dsh-mu.tgz                 # 记录：账号/实例/配置/密钥/网关视图
dsh-muctl backup --out /a.tgz --with-instances                 # 另加每个实例的 home（不含 jail rootfs）
dsh-muctl restore /var/backups/dsh-mu.tgz --dry-run            # 只显示计划：将新增/移除哪些账号与实例、配置是否不同
dsh-muctl restore /var/backups/dsh-mu.tgz --force              # 执行；先自动把当前 /etc/dsh-mu 存成 <ETC>.bak-<ts>.tgz
```

- 归档内容：`MANIFEST.json`（格式版本、时间、主机、dsh-mu/DSH/Alpine 版本、schema、账号与实例清单、端口表）+ `etc/dsh-mu/**`
  + 可选 `var/lib/dsh-mu/instances/<名>/home/**`；**永不包含 jail rootfs**（那是可重建的 140MB 副本）。
- 归档权限强制 `0600`（内含 `secret.key` 与 `admin.token`）；恢复后自动校正这些文件的属主/权限（`0640 root:nginx`）。
- 管理页「平台」标签有「下载平台备份」按钮（`GET /admin/api/backup?with_instances=0|1`，走管理员会话）。

实测（dsh-mu）：备份 4539 字节 0600、清单完整；`--dry-run` 报 `config: identical`；真执行一次恢复后
安全副本生成、敏感文件权限仍为 `0640 root:nginx` / `0600`、`doctor 33 PASS`、账号与实例完好；
管理 API 下载得到的归档同样带 `MANIFEST.json`。

## 日志与轮转

- 网关日志与每个实例的 stdout/stderr 都在 `/var/log/dsh-mu/`（`gateway.log`、`<name>.log`、`<name>.err.log`）。
- `dsh-muctl install` 会写 `/etc/logrotate.d/dsh-mu`：**daily + `maxsize 16M` + 保留 4 份 + 压缩 + `copytruncate`**，
  由 Alpine 自带的 `crond`（`/etc/periodic/daily/logrotate`）驱动。
- 必须是 `copytruncate`：`supervise-daemon`（实例）与 openresty 都长期持有日志 fd，重命名文件后它们仍会写旧 inode。
- 没有 logrotate 的环境可用 `dsh-muctl logs rotate`（纯 Python 的 copytruncate，`--max-mb/--keep/--force/--json`），
  也可自己挂到 cron；`dsh-muctl logs size` 随时看体积。
- `doctor` 会检查 drop-in 是否存在、日志总量是否异常（>512MB 提醒）。

## 管理后端（`dsh-mud`）与 JSON API

`dsh-mud` 是**只监听回环**的管理后端，浏览器侧由网关 `/admin/` 做会话与 `role=admin` 校验后反代（2.2 接上）。
它不是一个独立控制面：所有写操作都走 CLI 用的同一个库（`dshmuctl.py`）与同一把 `state_lock`，
所以 CLI 与页面永远不会各说各话。

```sh
dsh-muctl admin install     # 生成 /etc/dsh-mu/admin.token(0640 root:nginx) + 服务 + 入 default runlevel + 启动
dsh-muctl admin status      # 服务状态 + /api/health 实测
dsh-muctl admin restart
dsh-muctl admin token --rotate   # 轮换令牌（网关与后端需重载）
```

信任模型：仅回环 + 可选共享令牌（网关读同一个 `admin.token` 并以 `X-DSH-MU-Admin` 带上）；
后端**拒绝**绑定非回环地址（`--listen 0.0.0.0:8788` 直接被拒），并对非回环来源返回 403。

| 方法 | 端点 | 说明 |
|---|---|---|
| GET | `/api/health` `/api/overview` `/api/doctor` | 健康、概览（计数/磁盘/doctor 摘要/实例一览）、完整 doctor |
| GET | `/api/users` `/api/instances` `/api/instances/<名>` `/api/instances/<名>/logs?lines=` | 用户（**不含口令散列**）、实例（含 `live` 端口实测）、日志尾部 |
| GET | `/api/config` `/api/providers` `/api/audit?limit=` | 配置、provider 预设与模型目录、审计记录 |
| POST | `/api/users` `/api/users/<名>/password` `/api/users/<名>/enable\|disable` | 建用户（可不给密码，返回一次性随机密码）、改密（epoch+1 踢下线）、启停用 |
| POST | `/api/instances` `/api/instances/<名>/start\|stop\|restart\|refresh-cookie` `/api/instances/<名>/provider` | 建实例、启停、重取 cookie、切 provider |
| PATCH / DELETE | `/api/instances/<名>` `/api/users/<名>` `/api/instances/<名>?purge=1` | 改配额、删用户、删实例（可选清数据） |

审计：所有写操作追加 `/var/log/dsh-mu/audit.log`（JSONL：时间/操作者/动作/目标/详情/来源 IP），
由同一份 logrotate 配置轮转；`GET /api/audit` 可查。

实测（dsh-mu）：8 个 GET 全部 200；无令牌 403；`--listen 0.0.0.0` 被拒；
`overview` 显示 `counts={users:2, instances:2, running:2}`、`doctor={PASS:31}`、`disk={free_mb:9926, template_mb:140}`。

## 登录后的用户面板（页面左下角探出的角色图标）

普通用户打开自己的实例后，**窗口最左侧会探出一个角色形象**（`gateway/static/icon.png`），
鼠标移上去完全露出，点击从左侧滑出抽屉：

- **我的实例**：名称、端口、运行状态（网关实时端口探测）、provider / 模型；未运行时按钮一键「启动实例」；
- **修改密码**：平台账号改自己的密码（改完所有会话失效，跳回登录）；
- **退出登录**：与顶部一致的 `/logout`。

实现要点：面板由 `gateway/static/panel.js` 注入（`sub_filter` 只加一个容器 + `<script>`），
跑在 **Shadow DOM** 里，既不与 Harness 的样式互相污染，也不会被它的 CSS 影响；
数据来自新增的 `GET /me`（会话级 JSON，只返回调用者自己的信息），
改密/启动实例走 `POST /me`，由网关经**令牌保护的内部通道**转给以 root 运行的回环后端。

**登录即自动启动实例**：当实例没在监听（崩溃、被停掉、刚重启），访问页面不再给 503，
而是自动请求后端拉起，并显示「实例正在启动…」页（每 3 秒自刷新）。
实测：停掉 alice 的实例后再刷新页面，**3 秒内直接进入 Harness**。

## 管理权限：系统账号 + 系统口令

管理权限**锁死在宿主系统账号**上（`ADMIN_SYSTEM_USERS`，默认 `root`）：

- 登录时用户名若是配置的系统账号，口令由以 root 运行的回环后端对 `/etc/shadow` 校验
  （`openssl passwd -6 -stdin` / `busybox cryptpw -P 0`，**口令不经 argv**）；
- 平台账号（`user add` 建的）**一律是普通用户**，改 role 已无意义，进 `/admin/` 一律 403；
- `dsh-muctl bootstrap` 不再生成随机管理员密码，而是检查系统账号是否已设口令（未设则报 FAIL 并给 `passwd` 命令）；
- `dsh-muctl doctor` 有一项专门检查该系统账号；`install` 结束时也会提示。

```sh
dsh-muctl bootstrap          # 检查管理员账号是否可用（root 有系统口令即 PASS）
dsh-muctl config admin_system_users "root ops"   # 需要多个管理员时
```

## 管理页面 `/admin/`

浏览器访问 `http://<网关>/admin/`（`/admin` 自动 302 到 `/admin/`）。门禁在 `gateway/lua/admin.lua`：

| 访问者 | `/admin/` 页面 | `/admin/api/*` |
|---|---|---|
| 未登录 | 302 → `/login?next=/admin/` | 401 JSON（页面能提示而不是跳转） |
| 已登录但非 admin | 403 页「需要管理员权限」 | 403 JSON |
| admin | 200 页面 | 注入 `X-DSH-MU-Admin`（读 `/etc/dsh-mu/admin.token`，缓存 30s）与 `X-DSH-MU-User`（审计用），剥掉浏览器 Cookie 后反代到 127.0.0.1:8787 |

页面（`admin/index.html`，原生 HTML+JS，无构建步骤、无外部依赖）分六个标签：
**概览**（计数/磁盘/日志/自检摘要 + 实例表与启停、重启、重取 cookie、看日志）、
**用户**（建/删/启停用/改密，随机密码只显示一次）、
**实例**（建实例、改限额、切 provider、看日志、删实例可选清数据）、
**平台**（`dsh-mu.conf` 逐项编辑，密钥字段标注）、
**自检**（doctor 31 项完整表格 + 修复命令）、
**审计**（最近 200 条）。

慢操作（start/restart/refresh-cookie 要等 harness 打印启动横幅）**异步受理**：后端返回 `202 {"accepted":true}`，
在线程里执行并在完成时写审计（`detail.async=true`），页面自动轮询 20 秒 —— 不会把浏览器请求挂住。
实测：`POST /admin/api/instances/bob/refresh-cookie` → **202 in 0.00s**，随后审计出现
`ops instance_refresh-cookie bob {'async': True, 'status': 200}`。

### 管理员从哪来（首装）

**没有"首装随机管理员口令"这回事**：管理员**就是宿主系统账号**（`ADMIN_SYSTEM_USERS`，默认 `root`），
口令用系统口令 —— 所以装完先 `passwd root`，再用 `root` + 系统口令登录 `/admin/`。
平台账号（`dsh-muctl user add`）**永远是普通用户**，不会被提升为管理员。
`dsh-muctl install` / `bootstrap` 只维护这些系统账号的记录，并如实报告"系统账号还没有口令"这类状态
（`doctor` 也会 FAIL 提醒）。

### 要求用户改密

`user add --must-change` 或 `user passwd <名> --password … --must-change` 会给账号打标记：
该标记会随 `/me` 传给用户面板，面板顶部提示"首次登录：请立即修改密码"并提供改密框。
**它是提示而不是锁**（平台用户不会被拦截在实例之外）；改密成功后标记自动清除、
且**所有既有会话立即失效**（epoch 自增）。`tests/user-flows.py` 端到端验证了这条链路。

## 实例端口

```sh
dsh-muctl instance port alice                # 看当前端口
dsh-muctl instance port alice --port 3110    # 迁移（校验：范围 [PORT_RANGE]、与其他实例冲突、本机占用）
dsh-muctl instance port alice --auto         # 自动挑一个空闲端口
dsh-muctl instance port alice --port 3110 --restart   # 立刻重启并在新端口就绪后才返回
```

端口在启动时绑定，所以迁移需要重启；加 `--restart` 会等到新端口真正可连接才返回（避免"刚重启还没监听"被误判为故障）。
管理页「实例 → 限额」弹窗里也能改端口（会先确认是否立即重启）。

状态判定现在是**服务感知**的：`sync-state` 与 `doctor` 把"OpenRC 服务在跑"也算 running，
所以重启后的启动窗口不会被误报成 stopped/FAIL（doctor 会显示为 WARN「服务已起、端口尚未绑定」）。

## 实例限额（人话单位 + 「不限」）

```sh
dsh-muctl instance limits alice --show                 # 当前值（raw + 人话）
dsh-muctl instance limits alice --cpu-cores 2 --mem-mb 2048 --pids-num 512
dsh-muctl instance limits alice --cpu-cores max --mem-mb max --pids-num max   # 全部不限
dsh-muctl instance create bob --user bob --cpu-cores 0.5 --mem-mb 512 --pids-num 128
dsh-muctl instance limits alice --cpu '200000 100000'  # 仍可直写 cgroup 原值
```

| 输入 | 落到 cgroup v2 | 界面显示 |
|---|---|---|
| `--cpu-cores 2` | `cpu.max = 200000 100000` | `2 核` |
| `--cpu-cores 0.5` | `cpu.max = 50000 100000` | `0.5 核` |
| `--cpu-cores max` | `cpu.max = max` | `不限` |
| `--mem-mb 2048`（也接受 `2G`/`512M`） | `memory.max = 2147483648` | `2 GB` |
| `--mem-mb max` | `memory.max = max` | `不限` |
| `--pids-num max` | `pids.max = max` | `不限` |

实例**在运行时改限额会立即生效**（cgroup v2 允许在线调整 `cpu.max`/`memory.max`/`pids.max`），
CLI 会打印 `applied immediately: ...`，管理页会显示「已保存并立即生效」；实例未运行时写在配置里、下次启动生效。
管理页「实例」标签的限额列直接显示人话值，点「限额」弹出三个下拉（不限 / 0.5 核 / 1 核 / 2 核 / 4 核 / 8 核 等）。

## 模型 Provider：内置预设 + 可编辑目录

Provider 目录 = 内置预设（`deepseek` / `opencode-go` / `opencode`）**叠加** `/etc/dsh-mu/providers.json`
（0600 root:nginx，同名可覆盖内置，便于改 base_url）。每个条目的字段：

| 字段 | 说明 |
|---|---|
| `kind` | `pi-ai`（OpenAI Chat Completions，走 `dsh-llm-pi-ai`）或 `deepseek`（Anthropic Messages，走 `dsh-llm-deepseek`） |
| `route` / `api` | pi-ai 的路由 id（默认同名）与协议（`openai-completions`） |
| `base_url` | 端点前缀；pi-ai 会拼 `/chat/completions`，Messages 会拼 `/v1/messages` |
| `credentials_ref` | 凭据库里的键名（默认 `DEEPSEEK_API_KEY`） |
| `session_header` | 是否发送 `x-opencode-session`（OpenCode Go 必需） |
| `headers` | 任意额外静态 header（例如自建网关的 `x-tenant`） |
| `models` | 模型目录：`id | 名称 | 上下文 | 最大输出`，用于实例选择与 UI |
| `model` | 默认模型 id |

```sh
dsh-muctl provider add lab-llm --kind pi-ai --base-url https://llm.example/v1 \
  --model lab-model --session-header --headers '{"x-tenant":"acme"}' \
  --models 'lab-model | Lab Model | 64000 | 8192'
dsh-muctl instance provider alice lab-llm      # 自定义 provider 与内置一样可选
dsh-muctl instance-patch alice                 # 看最终 patch（含密钥来源与长度，不含密钥本体）
```

**凭据优先级**：实例凭据库（`<实例>/home/.dsh/.credentials.yaml`，0600，按实例 uid chown）优先；
没有则回落平台 `MODEL_API_KEY`（写进 `dsh-mu.conf`，改一次对所有实例生效）。
`dsh-muctl provider-test` / 页面「测试连通性」会**真发一个最小请求**，返回状态码、耗时与首段错误，
便于区分「端点不通 / key 无效 / 模型不在目录」；探测结果与日志里都不会出现凭据。

实测（dsh-mu，真实端点）：

```
provider-test --instance alice   -> OK  opencode-go  status=200  1279ms  key=instance(len 51)  detail: reply ok
provider-test --instance alice --key sk-invalid-…  -> FAIL status=401  "Invalid credential"
provider-test --instance alice（key 被覆盖成假值后恢复）  -> OK status=200
jail 内真实往返：dsh-mu-jail alice -- dsh --patch /home/dsh/.dsh-mu-provider.yml --profile headless "…PONG"  -> PONG
```

管理页面新增 **模型 Provider** 标签：目录表（内置/自定义、协议、base_url、模型数）+ 表单（含模型目录与额外 header）
+ 实例分配表（切 provider、改模型、设置/清除实例 key、测试连通性、查看 patch）。平台 `model_api_key`
在 API 与页面上以 `***` 呈现，提交 `***` 表示不修改。

## 网关不盲信 state.json

`state.json` 由 CLI 写入，可能与现实漂移（实例崩溃、被 `rc-service` 手工停掉、正在重启）。
因此网关在转发前会**实测上游端口**（非阻塞 connect，超时 400ms；结果缓存：活着 5s、不通 2s）：

- 端口不通 → 返回**我们自己的 503 页**（写明是"没在监听"还是"未运行"，并给出 `sync-state`/`restart` 命令），
  而不是让 nginx 抛一个裸 502 / WebSocket 升级失败重连；
- 端口通但 `state.json` 写的是 stopped → **照常放行**（活着的进程优先），并打一条 WARN 提醒跑 `sync-state`；
- `dsh-muctl sync-state` 按同一判断回写状态与网关视图；`doctor` 也用它报告"声明状态 vs 端口实测"。

实测（bob 被 `rc-service` 停掉、state 仍为 running）：
`GET /` → 503，`<title>实例没有响应</title>`，页面含 `sync-state`/`restart` 提示；
恢复后 `sync-state` 自动纠偏回 `running`，服务 200。

## 先跑 doctor（新机器第一步）

```sh
dsh-muctl doctor          # 30+ 项检查（随实例数增长）：内核/容器能力、cgroup 委派、apk、镜像、nginx 集成、运行态一致性
dsh-muctl doctor --json   # 机器可读（admin 页面复用）
dsh-muctl doctor --fix    # 顺手执行安全修复：gateway install + sync + dsh-mu-cgroup start
```

输出 `PASS/WARN/FAIL` 与每项的修复命令，有 `FAIL` 时退出码为 1（可被脚本/CI 当门禁用）。
检查项覆盖了本平台在 Alpine 上最容易踩的坑：

| 组 | 检查 |
|---|---|
| 平台 | Alpine 版本（auto 探测）、community 仓库、必需命令、全局 dsh 版本、state/conf 是否已初始化、secret 权限 |
| jail | `unshare --mount/--uts`、命名空间内 tmpfs 挂载、util-linux `setpriv` 丢 capability、非特权 userns、Landlock、模板完整性（`bin/sh`/`setpriv`/`node`/dsh 软链） |
| cgroup | cgroup v2 是否 rw、控制器、`dsh-mu` 委派状态、限额文件实测可写（含 `cgroup.kill`） |
| 资源 | 磁盘剩余与可放实例数估算、网关端口占用 |
| 网络 | `ALPINE_MIRROR` 可达（Range 请求，不下载索引）、npm registry 可达 |
| nginx | 受管 include、片段与模板是否漂移、`nginx -t`、`lua_shared_dict` 是否重复、`default_server` 归属 |
| 运行态 | 网关视图与 state 是否同步、每个实例"声明状态 vs 端口实测"、上游 cookie、日志体积与轮转配置 |

## 全新机器排练（不需要清空机器）

```sh
sh tests/regression.sh --user alice --password … --instance alice   # 一键跑全部验收层
sh tests/fresh-rehearsal.sh --clean                      # 现搭全新 Alpine + 完整安装 + 运行时冒烟
sh tests/fresh-rehearsal.sh --clean --apk dist/dsh-mu-0.1.0-r0.apk \
                                --apk2 dist/dsh-mu-0.1.1-r0.apk      # 走文档流程 + 真实版本升级
sh tests/fresh-rehearsal.sh --keep --skip-dsh            # 保留 rootfs 便于排查；跳过 npm 装 DSH
sh tests/fresh-rehearsal.sh --clean --skip-dsh --tarball dist/dsh-mu-<ts>.tgz   # 用交付包复现（交付前推荐）
```

`tests/regression.sh` 依次跑 33 层并在末尾给一行汇总：`doctor` → 网关 `selftest` → **界面自洽性** →
**CLI 契约** → **管理 API 契约** → `acceptance` → `fresh-machine.py` → **隔离边界自检** → **故障与恢复** →
**文档一致性** → **持续负载** → **交付物审计** → **部署漂移审计** → **修复能力验证** → **日志轮转验证** → **账号生命周期** → **每实例独立 uid** → 干净 Alpine 排练 → **state.json 并发锁** → **管理员面**（以真正的管理员走一遍 `/admin/`）→ **用户面板**（普通账号看到的 `/me` 与「启动我的实例」）→ **端口迁移**（守卫 + 网关无需 reload）→ **恢复后的属主** → **恢复计划（覆盖前体检）** → **日志接口边界** → **平台配置边界** → **账号名与 API key 的校验** → **限额取值** → **密码最小长度（各入口一致）** → **provider 连接测试** → **线上 provider 模板**（列表/导入/校验/防护，完全离线）→ **登录限速**（**排在最后**：它会消耗按 IP 的失败预算）
（`--fast` 可跳过最后一层）。它自动构建 0.1.0/0.1.1 两个包，好让排练能真正走一遍 `apk` 升级路径。

**每实例独立 uid**（`sh tests/run-uid.sh`）：把 `instance_run_uid` 切到 `auto`，建一个临时账号+实例，
验证它拿到专用宿主 uid（实测 60001）、服务配置与数据卷属主一致、**jail 内 `id -u` 就是这个 uid**、
能力仍全清、procfs magic link 仍关闭、home 可写而 **rootfs 不可写**（该模式的代价：没有 jail 内 apk），
最后删干净并恢复配置。实测 PASS=16。

**账号生命周期**（`python3 tests/user-flows.py`）：建号（`--must-change --instance none`）→ 登录落到实例根 →
`/me` 报告标记 → 面板确有改密入口 → `POST /me` 改密 → **标记清除、既有会话失效、旧口令再也登不进、新口令可用** →
删号收尾。实测 PASS=11。

  ⚠ 两个运维事实（都在这个测试里被验证）：① 网关把渲染好的用户视图**缓存 2 秒**
  （`lua/dshmu.lua` 的 `STATE_TTL`），所以改密/禁用/启用**最多 2 秒后**才对登录生效；
  ② 账号没有实例时（`--instance none`）登录会**建立会话但落到 503 提示页**，不是 302。

**日志轮转验证**（`sh tests/log-rotation.sh`）：确认 drop-in 用 `copytruncate`（nginx/`supervise-daemon`
持有 fd，重命名会让它们写进旧文件）、`logrotate -d` 无报错、真跑一次 `-f` 后**出现轮转副本**且
**活文件被原地截断**、随后**服务继续写日志且 nginx worker 没有重启**、审计日志轮转后仍在记录、
兜底 `logs rotate --force` 可用、保留份数有界。实测 PASS=13。

**部署漂移审计**（`python3 tests/drift-audit.py [源码树] [--apk F]`）：把**仓库 ↔ 实机 ↔ 包**三者逐字节对照
（CLI 库与 shim、jail 与 cgroup 初始化、管理后端与页面、网关 Lua、静态资源、测试）。它回答的是
"实机验证通过的到底是不是交付的代码"——本轮首次运行：核心组件与网关 Lua **全部一致**。

**修复能力验证**（`sh tests/doctor-fix.sh`）：故意破坏四件可修的东西——删掉 cgroup 委派服务的
**boot 注册**、删掉 nginx 临时目录、删掉渲染好的网关视图、把**运行中的实例**在记录里改成 `stopped`
——然后断言 `doctor` 报告了每一处、`doctor --fix`/`sync-state` **真的修好**，最后整体恢复 0 FAIL / 0 WARN。

**交付物审计**（`python3 tests/artifact-audit.py [源码树] [--tarball F]`，只读、不安装）：
用**当前源码树**重新打包后核对——`apk verify` 通过、包内**该有的都在**（程序/库/网关 Lua/管理页/面板资源/
测试/文档，含 `/usr/share/doc/dsh-mu/DELIVERY.md`）、**不该有的都没有**（jail 模板、实例数据、
`state.json`/`secret.key`/`admin.token`/`providers.json`、`__pycache__`、`dist/`）、单文件不超 400KB、
没有 world-writable 文件；源码归档同样查这些，并核对 `DELIVERY.md` 的承诺与实际一致（这是可签字的交付核对单）。

**持续负载**（`python3 tests/soak.py --minutes 2`，回归里跑 2 分钟）：多个并发客户端持续打真实流量
（登录页、实例转发、`/me`、管理 API），每 15 秒采样一次进程，断言：后端**没有被重启过**、
nginx worker 数不变、后端 RSS **预热后无上升趋势**（≤1 MB/min）且绝对增长受限、
文件描述符不增长、**零 5xx / 零客户端错误**、实例进程 RSS 平稳。2 分钟实测 1735 请求
（p50 1ms / p95 3ms），8 分钟长跑用于排查泄漏。

测试脚本共用 **`tests/lib.sh`**（取实例端口/字段、等端口、进 jail、计数与汇总），
12 个脚本里重复的逻辑都收敛在这里；**单独拷走某个脚本而不带 `lib.sh` 会明确报错并退出 2**，
而不是给出莫名其妙的结果。

**文档一致性**（`node tests/doc-lint.mjs [源码树]`，纯静态，19 项）：每个相对链接都能解析、文档里提到的每条
`dsh-muctl` 子命令**真的存在**、引用到的文件路径真实存在（运行时生成物除外）、文档写的回归层数与
`regression.sh` 实际层数一致、层索引与实际步骤一一对应、`DELIVERY.md` 交接单要素齐全。
**它把"文档过时"也变成了会失败的检查**——本轮加一层后它立刻报出 README 仍写着 10 层。

**CLI 契约**（`sh tests/cli-contract.sh [实例]`）：跑 ~40 个**故意写错**的调用（外加 4 条探测/导出的正面断言），断言每个都
非零退出、给出人话错误（`dsh-muctl: …` 或 argparse usage）、且**绝不吐 Python traceback**；
另跑一批只读调用必须成功（含 `--json` 必须能解析）。实测 34 项全过。

**管理 API 契约**（`python3 tests/api-contract.py [--instance …]`）：用 20+ 个错误请求打后端
（无/错令牌、未知路由、未知实例、畸形 JSON、非对象 JSON、越界端口、未知 provider、删内置 provider、
错口令、非法限额……），断言每个都返回 **JSON + 合理状态码 + `error` 字段**（不会变成 HTML 或 500），
并检查后端日志在这批请求期间**没有 traceback**；再验证 9 个只读端点 200。实测 35 项全过。

**界面自洽性**（`node tests/ui-lint.mjs [源码树]`，纯静态、秒级）：管理页里每个 `onclick/onchange`
处理器都真的有定义、每个 `getElementById/#id` 都有对应标记、每个 `data-tab` 都有面板、**每个页面 API 调用
都能在后端路由表里找到**、用户面板请求的每个路径都被网关 location 覆盖。实测 7 项全过
（23 个处理器 / 31 个 id / 7 个标签 / 22 个 API 调用）。

**故障与恢复**（`python3 tests/failure-modes.py --user … --password … [--instance …]`）：停掉管理后端、
停掉实例、`kill -9` 实例进程、并在 4 个并发请求下执行 `dsh-muctl gateway install`，断言
①后端挂掉时匿名 `/admin/` 仍走登录页且 `nginx -T` 里确实配有运维提示页；②`/login`、实例转发、
运维 API 不受影响；③实例停掉时 `/` 给出"启动中"页而不是卡死；④SIGKILL 后 supervisor 自动拉起；
⑤热重载零丢包。脚本**总会把服务恢复原状**，最后再断言整体健康。实测 `PASS=15 FAIL=0`
（热重载实测 5900+ 个在途请求无一失败）。

**隔离边界自检**（`sh tests/isolation.sh [实例] [--json]`）：在活着的实例内部执行探针，逐条断言
chroot、capability 清零、procfs magic link、`mount`/`mknod`/`unshare`、共享 runtime 只读、私有 `/tmp`、
cgroup 归属与限额、以及**跨租户**（直连别人的端口必须 401、运维 API 必须 403）。实测 alice/bob 各
`PASS=20 FAIL=0`。

它用 `apk --root` 从镜像现搭一个干净 rootfs（约 240MB），绑定 `/proc /sys /dev`，把当前 checkout 拷进去，
然后**在里面真的跑一遍** `install/install.sh`：依赖解析 → 首次配置（`dsh-mu.conf`/`secret.key`/`admin.token`/`state.json`）
→ jail 模板构建 → 网关片段安装 + `nginx -t` → `doctor` / `bootstrap`。

第 8 步是**包生命周期**：用更高版本的包做一次真实升级（`pre/post-upgrade` 脚本必须执行）、
`apk del` 后校验 `/etc/dsh-mu` 与 `secret.key` **逐字节保留**、再重装并确认记录完好。
第 9 步是**灾难恢复演练**：建账号+实例 → 备份（含实例 home）→ **`rm -rf /etc/dsh-mu`（平台状态全丢）**
→ 像新机器一样重装 → `restore --force`，然后断言**口令散列/端口/属主逐字节一致**、会话密钥回来了
（老会话仍有效）、网关视图重新渲染、实例 home 文件回来了、安全副本已生成、CLI 能重新列出该账号；
紧接着的第 10 步运行时冒烟就跑在**恢复后的平台**上。

第 10 步是**运行时冒烟**：在 chroot 里真的把网关与后端拉起来（用高位端口避开宿主），
验证匿名 `/` 302、`/login` 200、`/admin/` 门禁、`/me` 401、面板资源 200、运维 API **无令牌 403 / 有令牌 200**、
后端健康检查与令牌校验。chroot 没有 init、也不与宿主共享网络，所以**实例（jail/cgroup）与端到端仍归
`tests/fresh-machine.py`（真机）**；两个脚本合起来覆盖"干净机器复现"的两半。

实测（dsh-mu，`--apk` + `--apk2` 模式）：**`PASS=37 FAIL=0`，退出码 0**。排练先后抓出三个真问题：
① 干净机器上先跑 `doctor`（文档第一步）会写出 `state.json`，使 `install` 跳过 `secret.key` 生成 →
   网关无法签发任何会话；
② 包安装脚本跑不了的环境里 busybox applet 软链缺失 → 模板不可用；
③ `/mu-api/` 只做 `allow 127.0.0.1`，而本机浏览器同样是回环 → **运维 API 无需凭据即可访问**（已改为必须带共享令牌）。

## 全新机器验收（一键）

`docs/05-打包与安装.md` §6 的清单是可执行的：

```sh
python3 /usr/local/lib/dsh-mu/tests/fresh-machine.py            # 自动建一个 smoke 账号+实例，验完自清理
python3 /usr/local/lib/dsh-mu/tests/fresh-machine.py --skip-install --user alice --password '…' --instance alice
python3 /usr/local/lib/dsh-mu/tests/fresh-machine.py --skip-install --user ops --password '…'   # 纯管理员账号
python3 /usr/local/lib/dsh-mu/tests/fresh-machine.py --json      # 机器可读报告
```

覆盖：doctor 31 项 → 管理员账号存在性/自动 bootstrap → 建号建实例并等端口就绪 → 匿名 `/` 302 →
登录后 `/` 200（真实 harness）→ `/mu-api/status` → `/admin/` 门禁与管理员页/API →
`selftest` + `acceptance` 全套 → 清理。任一 FAIL 退出码非 0。

实测（dsh-mu，平台已装）：真实账号模式 `PASS=10 FAIL=0`；自动建号模式 `PASS=10 FAIL=0`；
纯管理员账号模式 `PASS=8 FAIL=0`（并断言"无实例账号得到网关 503 页"这一行为）。

## 打成 Alpine 包（apk）

```sh
sh packaging/make-apk.sh --version 0.1.0          # -> dist/dsh-mu-0.1.0-r0.apk（未签名）
# 构建环境（需要什么、重装后怎么重建）见 docs/09-构建环境.md
# 默认走 `apk mkpkg`（apk-tools 3 自带）：不需要 abuild、不需要签名密钥、不联网，
# 在工作区这台 Alpine、CI 容器里都能构建；安装时一律 `apk add --allow-untrusted`。
apk add --allow-untrusted ./dist/dsh-mu-0.1.0-r0.apk
dsh-muctl doctor && dsh-muctl install
```

- **默认产物是未签名的 v3 包**（`apk mkpkg`，apk-tools 3 / Alpine 3.22+ 格式），安装需要
  `--allow-untrusted`；这是刻意的：本项目不发布到 Alpine 仓库，只在 GitHub 挂产物。
- 需要"本机 `apk add` 不带 `--allow-untrusted`"时可加 `--signed`：走 Alpine 官方 `abuild` +
  `packaging/APKBUILD` 并自动 `abuild-keygen`（仅构建机需要 abuild）。
- 旧的 `--legacy-v2` 手写打包器已删除：apk-tools 3 会以 `v2 package format error` 拒绝 v2 包，
  它在当前目标上不可能产出可安装的东西。
- 包**不含** ~140 MB 的 jail 模板、`/etc/dsh-mu/*`、实例数据与日志 —— 这些都是首次 `install` 生成或在运行时产生。
- `apk del dsh-mu` 只删程序：`pre-deinstall` 停服务，`post-deinstall` 打印保留清单；
  实测卸载前后 `state.json`/`dsh-mu.conf`/模板 md5 不变、审计日志保留。
- 重装后 `dsh-muctl install`（或 `doctor --fix`）会**重新拉起 state 中记为 running 的实例**。
- 详见 [docs/05-打包与安装.md](docs/05-打包与安装.md)（含 apk-tools 3 的格式坑与干净机器验收清单）。

## 目录布局（目标机）

```
/etc/dsh-mu/state.json                唯一事实源（含口令散列，0600）
/etc/dsh-mu/secret.key                会话签名密钥（0640 root:nginx）
/etc/dsh-mu/gateway/{users,upstreams,config}.json   网关只读视图（0640 root:nginx）
/etc/dsh-mu/instances/<name>.conf     jail 运行器配置
/etc/init.d/dsh-mu-{cgroup,<name>}     OpenRC 服务（实例随开机自启、崩溃自愈）
/usr/local/sbin/{dsh-muctl,dsh-mu-jail,dsh-mu-cgroup-init}
/usr/local/lib/dsh-mu/{lua,python,tests,nginx}
/usr/local/sbin/{dsh-muctl,dsh-mu-jail,dsh-mu-cgroup-init}
/usr/local/lib/dsh-mu/python/dshmuctl.py          控制面库（CLI 与 dsh-mud 共用）
/usr/local/lib/dsh-mu/nginx/dsh-mu-*.conf         网关片段模板
/etc/dsh-mu/dsh-mu.conf                           平台配置（脚本可 source 的 shell 语法）
/etc/dsh-mu/dsh-mu.conf.default                   出厂默认值（随包安装）
/etc/nginx/conf.d/dsh-mu-http.conf                http 级片段（lua_shared_dict/map）
/etc/nginx/http.d/dsh-mu-server.conf              server 级片段（监听/路由）
/var/lib/dsh-mu/templates/alpine      jail 模板
/var/lib/dsh-mu/instances/<name>/{rootfs,home,workspace}
/var/log/dsh-mu/{gateway*,cgroup*,install,<name>}.log
/sys/fs/cgroup/dsh-mu/<name>          实例 cgroup（cpu/memory/pids）
```

## cgroup 说明（重要）

容器内的 cgroup namespace 根是一个**普通且被占用的** cgroup（PID 1 与 console getty 在里面），
直接用 `echo +cpu > cgroup.subtree_control` 会 `EBUSY`。因此平台在开机时由 `dsh-mu-cgroup` 服务：

1. 把根组里的进程挪进 `/sys/fs/cgroup/init`；
2. 打开根组的 `subtree_control`；
3. 把 `cpu memory pids io` 委派给 `/sys/fs/cgroup/dsh-mu`。

之后每个实例用 `/sys/fs/cgroup/dsh-mu/<name>/{cpu.max,memory.max,memory.high,pids.max}` 限制资源，
并且实例服务都 `need dsh-mu-cgroup`。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 打开网关 503「实例未运行」 | 实例没启动 | `dsh-muctl instance start <name>` |
| 503「实例凭据未就绪」 | 上游 cookie 没生成（首次启动尚未完成 / `DSH_HOME` 重建） | `dsh-muctl instance logs <name>`；`dsh-muctl instance refresh-cookie <name>` |
| 登录一直失败 | 用户被禁用 / 密码错 / 触发限速 | `dsh-muctl user list`；等 10 分钟或改密 |
| 502/504 | 请求已到上游、但上游中途失败 | `dsh-muctl instance logs <name>`；`tail /var/log/dsh-mu/<name>.err.log` |
| 503「实例没有响应」 | 网关端口探测发现实例没在监听（崩溃/正在重启/被手工停掉），而 `state.json` 还写着 running | `dsh-muctl sync-state`；`dsh-muctl instance restart <name>` |
| 503「实例未运行」 | 实例确实没启动（state=stopped） | `dsh-muctl instance start <name>` |
| `instance start` 卡在等待 banner | 首次启动在装 profile；或 pnpm 拉包慢 | 看日志；可先 `--no-wait`，稍后 `refresh-cookie` |
| jail 内 `apk add` 报 UNTRUSTED signature | 模板缺签名密钥 | 重建模板：`dsh-muctl install --force-template` |
| 限额不生效 | `dsh-mu-cgroup` 没跑 | `rc-service dsh-mu-cgroup start`；`cat /sys/fs/cgroup/dsh-mu/cgroup.subtree_control` |
| 网关卡在 401 | 会话过期或被 epoch 失效 | 重新登录；`dsh-muctl gateway status` |

## 已知限制

- **实例内「设置」分区显示 unavailable**：DSH 客户端按浏览器地址栏 hostname 判断 loopback
  （`ownsHost` 只对自建 transport 的 shell 开放），网关场景下无法通过服务端配置绕过。
  平台侧用 `dsh-muctl config` 与实例内的配置文件管理模型、插件。需要界面时用 SSH 隧道
  `ssh -L 8080:127.0.0.1:<实例端口>` 并访问 `http://127.0.0.1:8080/`。
- **无 Web 管理界面**（本期）：管理动作走 CLI；`/mu-api/status` 提供只读 JSON。
- **实例内 agent 沙箱**：见上「隔离边界」。jail 就是安全边界，因此实例默认关闭 DSH 自身沙箱。
- **端口**：实例只监听 `127.0.0.1`，外部无法绕过网关直连；端口从配置区间（默认 3100–3999）自动分配。
- **单实例磁盘**：模板 140MB + 每实例一份 rootfs 拷贝 140MB（把 rootfs 从 18MB 撑大的是 node/npm/pnpm/git/bash）；
  另有实例 `DSH_HOME`（profile 与依赖，实测首次启动后约 1–20MB）。11GB 可用空间下可放约 40 个实例。
