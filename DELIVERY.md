# dsh-mu 交付说明（交接单）

> 给"接手这台机器的人"看的第一份文档。它只说三件事：**这是什么、怎么装起来、怎么证明它是好的**。
> 细节分别在 `docs/` 里（索引见文末）。

## 1. 这是什么

把单机可用的 **DeepSeek Harness** 变成"一台机器上多租户可用"的平台：

- 每个用户一个 **jailed 实例**（mount ns + chroot + cgroup v2，能力全清），各自独立 rootfs、端口、限额；
- 前面是**纯 OpenResty + Lua** 的网关：登录、会话、上游 cookie 代持、用户面板注入、`/admin/` 管理面；
- 管理面 = `dsh-muctl`（CLI，可导入库）+ `dsh-mud`（回环后端）+ 管理页面；
- 以**单个 .apk** 分发，`apk add` 后一条 `dsh-muctl install` 完成落地；升级/卸载**保留数据**；
- 目标环境：Alpine 3.2x + OpenRC，裸机或 LXC 均可。

## 2. 交付物

| 路径 | 内容 |
|---|---|
| `dsh-mu-<ts>.tgz` | 完整源码与文档（本目录打包） |
| `dist/dsh-mu-<ver>-r0.apk` | 构建产物：单文件、**未签名 v3 包**（`apk mkpkg` 生成，无需 abuild/密钥；安装用 `--allow-untrusted`） |
| `docs/01-需求整理.md` … `docs/06-运维手册.md` | 需求 → 架构 → 实测 → 验收 → 打包 → 运维 |
| `tests/` | 31 层验收脚本（见下），可单独运行 |

包内**不含** ~140MB 的 jail 模板（由 `dsh-muctl install` 现场用 apk 构建），也不含任何 `/etc/dsh-mu` 或实例数据。

## 3. 装起来（干净 Alpine）

```sh
# 1) 装包（产物按设计不签名，必须 --allow-untrusted）
apk add --allow-untrusted dsh-mu-0.1.0-r0.apk

# 2) 落地：依赖、全局 DSH、jail 模板、网关片段、服务、管理员账号、自检摘要
dsh-muctl install

# 3) 设管理员口令（管理员 = 宿主系统账号，默认 root；平台账号一律普通用户）
passwd root

# 4) 建第一个用户（自动建同名实例并分配端口）
dsh-muctl user add alice --password '<强口令>'
```

然后浏览器打开 `http://<主机>/`：用户登录即自动拉起自己的实例；`/admin/` 用 `root` + **系统口令**登录。

**灾难恢复**（已在干净 Alpine 上演练过，见第 12 层）：把归档文件拿到新机器上，
`dsh-muctl install` 之后 `dsh-muctl restore <归档> --force` —— 账号（口令散列）、会话密钥、网关视图、
实例 home 文件都会回来，随后服务即可对外。演练断言的 10 项见 `docs/04-验收报告.md` §22。

升级：`apk add --allow-untrusted <新包> && dsh-muctl install`（数据不动）。
卸载：`apk del dsh-mu`（**程序删掉、`/etc/dsh-mu` 与实例数据保留**）。

## 4. 证明它是好的（一条命令）

```sh
sh tests/regression.sh --user <用户> --password <口令> --instance <实例>
# 或在已安装的机器上： sh /usr/local/lib/dsh-mu/tests/regression.sh …
```

它依次跑 32 层并在末尾给一行汇总：

| # | 层 | 内容 |
|---|---|---|
| 1 | `doctor` | 30+ 项前置条件与记录一致性自检 |
| 2 | `selftest.mjs` | 网关契约：登录、转发、`/me`、面板、**WebSocket 保持 75s**、**生效的 3600s 超时**、**3MB 上传不被 413**、**限速第 11 次触发 429**、跨站防护 |
| 3 | `ui-lint.mjs` | 界面自洽：处理器/DOM id/标签/**页面调用与后端路由一致** |
| 4 | `cli-contract.sh` | 错误调用必须**干净失败**（非零 + 人话 + 无 traceback） |
| 5 | `api-contract.py` | 管理 API 的错误必须是 **JSON + 合理状态码**，后端日志无 traceback |
| 6 | `acceptance.sh` | 端到端验收（cgroup 强制、jail 身份、jail 内 apk、沙箱模式、网关、审计） |
| 7 | `fresh-machine.py` | 全新机器清单（建号建实例、跑通模型、清理） |
| 8 | `isolation.sh` | 隔离边界：chroot、能力清零、procfs magic link、跨租户必须 401 |
| 9 | `failure-modes.py` | 后端停机、实例停机、`kill -9` 自愈、热重载零丢包 |
| 10 | `doc-lint.mjs` | 文档不与现实脱节（链接、命令、路径、层数、交接单完整性） |
| 11 | `soak.py` | 持续负载：无重启、无泄漏趋势、无 5xx（2 分钟；`--minutes 10` 可长跑） |
| 12 | `artifact-audit.py` | 交付物审计：包内容（v3 用 `apk manifest` 读）、**未签名状态符合设计**、无模板/密钥、归档清洁、文档承诺一致 |
| 13 | `drift-audit.py` | **部署漂移**：仓库 ↔ 实机 ↔ 包逐字节一致（否则"实机验证"证明的不是交付物） |
| 14 | `doctor-fix.sh` | 修复能力：把四件东西弄坏，断言 doctor 看见且 `--fix`/`sync-state` 真修好 |
| 15 | `log-rotation.sh` | 轮转真的轮转：出现副本、活文件截断、服务继续写、worker 不重启、审计不断 |
| 16 | `user-flows.py` | 账号生命周期：must-change 标记、改密、会话失效、旧口令作废 |
| 17 | `run-uid.sh` | 每实例独立宿主 uid（加固模式）：身份、属主、能力、代价、收尾 |
| 18 | `fresh-rehearsal.sh` | 干净 Alpine 排练：`apk add` → 安装 → 升级/卸载/重装 → **灾难恢复演练（备份→删库→重装→恢复→再起服务）** → 网关运行时冒烟 |
| 19 | `state-lock.py` | `state.json` 并发锁：8 个并发写者不损坏文件，且**锁真的阻塞**（持锁期间写者会等） |
| 20 | `admin-surface.py` | 以**真正的管理员**（临时系统账号）登录并使用 `/admin/`：页面、`me`、overview、建/删用户、审计归属、配置还原 |
| 21 | `user-panel.py` | 普通用户的 `/me` 面板：字段契约 + **面板「启动实例」按钮真的把实例启动起来** |
| 22 | `port-move.py` | 端口迁移：范围/占用/冲突三个守卫 + 重启后只听新端口 + **网关无需 reload 即解析到新端口** |
| 23 | `restore-owner.py` | 恢复后实例卷的属主按**记录**重定（陈旧属主的归档也不会让实例写不了自己的目录） |
| 24 | `restore-plan.py` | `restore --dry-run` 的体检报告：点名会被覆盖的卷、警告归档比现网旧、且**什么都不改** |
| 25 | `log-bounds.py` | 日志接口边界：`lines` 限幅 1..2000、非数字是 400、未知实例是 404 |
| 26 | `config-bounds.py` | 平台配置边界：未知键 400、非法值 400 且文件不动、清空管理员不会锁死、**收缩 PORT_RANGE 越过现有实例会被 doctor 点名** |
| 27 | `name-key-bounds.py` | 账号名一律按 `[a-z][a-z0-9_-]{0,31}` 拒绝（不留记录/目录）、管理员名不可占用、**CLI 与页面同一条 API key 长度规则** |
| 28 | `limit-values.py` | 限额取值：友好形式 `0`=不限（对 cgroup 实证）、**负值一律拒绝**、原始形式 `0` 拒绝、单位换算正确 |
| 29 | `password-bounds.py` | 密码最小长度**在每个入口一致**（CLI / `user add` / 管理员 API）；被拒的修改不动账号；8 字符可真的登录 |
| 30 | `provider-probe.py` | 页面「测试连接」：**200 但没有模型回复不算成功**、密钥绝不回显、慢探针不阻塞接口、`file://` 干净失败 |
| 31 | `login-limit.py` | 登录限速真的触发（第 10 次 429、锁定期间正确口令也被拒）；**排在最后**，因为它消耗按 IP 的失败预算 |

最近一次全量结果（本机实测，见 `docs/04-验收报告.md`）：**`REGRESSION: PASS=31 FAIL=0`**；
单层数字：`doctor 36 PASS/0 FAIL`、`selftest 20`、`ui-lint 7`、`cli-contract 40`、`api-contract 35`、
`acceptance 35`、`fresh-machine 10`、`isolation 23`、`failure-modes 15`、`doc-lint 20`、`soak 7`、
`artifact audit 11`、`drift audit 7`、`doctor fix 12`、`log rotation 13`、`user flows 16`、`per-instance uid 16`、
`排练 62`、`state lock 9`、`管理员面 37`、`用户面板 13`、`端口迁移 11`、`恢复属主 14`、`恢复计划 12`、
`日志边界 11`、`配置边界 6`、`名字与密钥 12`、`限额取值 15`、`密码最小长度 12`、`provider 连接测试 13`、`登录限速 4`。

### 唯一还没做的一件事

**在真·还原过的机器上跑一次不带 `--skip-install` 的 `tests/fresh-machine.py`**：

```sh
python3 /usr/local/lib/dsh-mu/tests/fresh-machine.py            # 会自己建号、建实例、验证、清理
```

第 10 层（chroot 排练）已经覆盖了同一条路径的绝大部分，但它跑在 chroot 里，无法验证 OpenRC 起服务与实例
jail 的真实行为——所以这一步必须在真机上做，需要人工把机器还原成干净 Alpine。

## 5. 组件与落点

| 组件 | 位置 | 说明 |
|---|---|---|
| 网关 | `/usr/local/lib/dsh-mu/lua/*.lua` + `/etc/nginx/{conf.d,http.d}/dsh-mu-*.conf` | 片段化安装，**不覆盖** `nginx.conf`，幂等、`nginx -t` 失败自动回滚 |
| CLI | `/usr/local/sbin/dsh-muctl`（14 行 shim）→ `/usr/local/lib/dsh-mu/python/dshmuctl.py` | 库可导入，CLI 与后端共用同一套逻辑 |
| 后端 | `/usr/local/sbin/dsh-mud`（OpenRC `dsh-mud`） | 只监听 `127.0.0.1:8787`，共享令牌校验，审计 JSONL |
| 管理页 | `/usr/local/lib/dsh-mu/admin/index.html` | 用户/实例/限额/端口/模型接入/平台/自检/审计 |
| 实例 | `/var/lib/dsh-mu/instances/<名>/{rootfs,home,workspace}` + `/etc/dsh-mu/instances/<名>.conf` | 每实例一个 OpenRC 服务 `dsh-mu-<名>` 与 cgroup |
| 状态 | `/etc/dsh-mu/{state.json,dsh-mu.conf,providers.json,gateway/*}` | `flock` 串行化写；`state.json` 0600，网关视图 0640 root:nginx |
| 日志 | `/var/log/dsh-mu/{admin,gateway,audit,<实例>}.log` | logrotate drop-in（daily + maxsize 16M + copytruncate） |

## 6. 日常操作（最常用的十条）

```sh
dsh-muctl doctor                       # 体检（可用 --fix 修可修的）
dsh-muctl user list                    # 谁在用
dsh-muctl user add bob --password '…'  # 加人（自动建实例）
dsh-muctl instance list                # 实例一览（--json 供脚本）
dsh-muctl instance restart alice       # 重启某实例
dsh-muctl instance limits alice --cpu 2 --mem 2048 --pids 512 --cpu max   # 限额（max=不限，立即生效）
dsh-muctl instance port alice --auto --restart                             # 换端口（自动挑空闲）
dsh-muctl instance exec alice -- ls /home/dsh/workspace                    # 进 jail 看现场
dsh-muctl backup --out /var/backups/dsh-mu-$(date +%F).tgz                 # 备份（0600，含密钥）
dsh-muctl doctor --json | jq .summary                                      # 给监控用
```

排障对照表、备份恢复、安全清单见 `docs/06-运维手册.md`。

## 7. 安全姿态（要点）

- **管理员 = 宿主系统账号**（默认 `root`），口令对 `/etc/shadow` 校验；平台账号一律普通用户。
- 会话 cookie `HttpOnly; SameSite=Lax`；登录双提交 CSRF；写操作另有**同源栅栏**（跨站 POST → 403）。
- `/mu-api/` 与 `/mu-internal/` 仅回环，且都必须带共享令牌；上游 DSH 的 trust fence 与每实例 HMAC cookie 由网关代持。
- 实例：能力全清、`no-new-privs`、私有 `/tmp` 与 `/dev`、共享 DSH runtime 只读、
  `/proc/<宿主pid>/root|environ` 实测不可达；**跨租户直连一律 401**。
- **所有变更都有审计**（CLI 与页面都在内），只记实体名与变更类型 + 打码命令行，口令与密钥不落盘。

## 8. 已知限制与取舍

- 实例**不建网络命名空间**：彼此能"连到"端口（拿到 401/403），这是设计取舍；需要更强隔离可
  `dsh-muctl config instance_run_uid auto` 让每实例跑独立宿主 uid。
- 实例**不建 PID 命名空间**：`/proc` 能看到宿主进程号（列表层面），文件/内存仍受 ptrace 检查保护。
- DSH 的「设置」分区在服务页面不可用（上游限制），因此平台自带用户面板承担实例信息/改密/退出。
- apk **按设计不签名**，安装必须 `apk add --allow-untrusted`；本仓库不发布到 Alpine 仓库，产物挂 GitHub。
- 升级 `dshmuctl.py` 后需要 `dsh-muctl admin restart`（后端在 import 时缓存库；`install` 会代做）。

## 9. 文档索引

| 文件 | 什么时候看 |
|---|---|
| `docs/01-需求整理.md` | 想知道"为什么是这样" |
| `docs/02-架构设计.md` | 想知道"怎么实现的"（§10 是当前实现，含威胁模型与跨站防护） |
| `docs/03-M1-实测报告.md` | 最早的可行性实测（历史） |
| `docs/04-验收报告.md` | 逐轮证据：每轮做了什么、怎么验的、结果多少 |
| `docs/05-打包与安装.md` | 打包细节、安装/升级/卸载、排练脚本 |
| `docs/06-运维手册.md` | 日常操作、故障对照表、安全清单 |
| `docs/07-证据索引.md` | **"凭什么说做到了"**：每条主张 → 验证脚本 → 最近结果 → 未证明的边界 |
| `docs/08-验收流程.md` | **验收走查清单**：从空机器到 31 层全绿，每步的命令/期望/证据/失败排查 + 签收表 |
| `docs/09-构建环境.md` | **构建机的环境**：需要什么（只要 apk-tools 3）、怎么从零重建、包怎么验证、发布约定 |
| `docs/10-网关进jail.md` | **网关进 jail 的方案与进度**：挂载/端口/能力、已实现部分、卡点、`dsh-mud` 进 jail 的两种形态 |
| `docs/14-模型代理.md` | key 只在网关的模型代理：拓扑、配置、逃生开关、实测数据 |
| `docs/13-插件.md` | 插件清单与密码规则、装在哪/怎么生效、接口、记账与重放、安全边界 |
| `docs/12-干净机复现清单.md` | **给人照着走**：全新 Alpine 从装包到模型接入，每步的期望输出、三个常见卡点、7 类故障的排查入口 |
| `docs/11-模型接入.md` | **provider/套餐/实例**：配置文件规范（`dsh-muctl provider spec`）、AI 提示词、导入导出与真机实测、套餐层设计 |
| `docs/07-证据索引.md` §5 | **需求追溯**：34 条 FR 的现状；8 条"部分实现"全部已分类（7 条已接受并写明理由/等价做法，1 条待决定） |
| `README.md` | 命令参考与验证矩阵 |
| `CHANGELOG.md` | 版本内所有变更（倒序） |
