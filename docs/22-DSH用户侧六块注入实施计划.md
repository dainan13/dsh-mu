# 22 · DSH 用户侧「六块注入」实施计划

状态：**待做**（平台侧 admin 与网关四页已完成）　记录：2026-11-08

## 0. 依据

设计侧权威文件（同仓库 `uidesign/cockpit/`）：

| 文件 | 用途 |
| --- | --- |
| `dsh-native.html`（653 KB，已拷到 `dsh-mu/design-ref/`） | **真机快照**：登录态真实 DSH DOM/CSS + 我们注入的六块，**代码可直接照搬** |
| `dsh.html` | 设计稿（近似），多页签设置等更完整 |
| `README.md` §"真机快照版" | 六块清单、折叠态实测数据（36×36 / 12px / `gap:12px`） |
| `DESIGN.md` §378 | 六块范围一句话 |
| `preview/native-*.png` | 四张验收截图 |

**原则**：宿主（DSH）自己的 DOM/样式不动 ✗；我们只**注入**，且渲染在 **shadow root** 里，
颜色/圆角/描边全部吃宿主的 `--dsw-*` 变量 ✓ —— 与现有 `/mu-static/panel.js`（F2 面板）同一套做法 ✓。

## 1. 四块要改造的东西（用户给出的范围）

| # | 块 | 说明 | 现状 |
| --- | --- | --- | --- |
| ① | **会话外壳 · 运行状态**（侧栏实例卡） | 左侧栏里我们那张卡：展开 280 / 折叠 56 两套形态；折叠态塞进 `.hHd-Xa_panelList`（与 Plugins / Automation tasks 同一列表，吃 `gap:12px`） | 现在是 F2 面板里的"● dainan · 运行中"一行，未做成侧栏卡 |
| ② | **更多与我的账号** | 「更多」菜单注入三项（设置 / 我的账号 / 退出登录）；「我的账号」弹层由我们渲染 | 未做（DSH 自带菜单里没有） |
| ③ | **设置 · 模型 (mu)** | DSH 设置弹窗的壳是宿主的，**右栏**是我们画的；页签名直接叫「模型」 | 现在是 client plugin 的 `settings.section`，样式未对齐设计 |
| ④ | **F2 控制中心** | 实例状态 · 最近 50 行日志 · 重启 / 退出登录；**「运行状态」被点也打开它**（F2 = 入口之二） | `/mu-static/panel.js` 已有雏形，需按设计重排 |

## 2. 数据从哪来（都已存在，不需要新接口）

| 块 | 接口 |
| --- | --- |
| 运行状态 / F2 | `GET /mu-api/status`（jail 内已实现）或 `/api/instances` 同源代理的轻量版；日志：`/api/instances/<n>/logs?limit=50`；重启：`/api/instances/<n>/restart` |
| 我的账号 | `/api/me`（当前用户 + 套餐 + 密码修改入口） |
| 设置 · 模型 (mu) | `/api/plans`（可用套餐/模型）+ `/api/me/plans`（个人默认） |
| 更多菜单三项 | 复用上面三个入口 |

## 3. 注入点（真机快照里实测到的选择器）

| 目标 | 选择器 |
| --- | --- |
| 侧栏外壳 grid | `.pI_x6G_frame`（展开 `280px`，折叠 `56px`） |
| 折叠态面板列表 | `.hHd-Xa_panelList`（状态项插这里，`gap` 直接继承） |
| 侧栏底部（更多/账号） | `.hHd-Xa_footArea` / `.yHnPSG_trigger` |
| 设置弹窗壳 | DSH 自己的 settings modal；我们只填右栏内容 |
| 全局遮罩层 | `.pI_x6G_overlayLayer`（F2 挂这里） |

## 4. 顺序（按依赖与风险）

```
①面板骨架复用：把现有 panel.js 改成"一块 shadow root 承载 F2 + 运行状态两处入口"
②运行状态（侧栏卡）：先做展开态（280）→ 再做折叠态（56，塞 panelList）
③F2：与②共用同一个 openF2()（点卡片 / 按 F2 / 点折叠态图标，三条入口一个函数）
④更多菜单三项：注入到 DSH「更多」菜单（设置 / 我的账号 / 退出登录）
⑤我的账号：独立弹层（shadow root）
⑥设置 · 模型 (mu)：改现有 settings.section 的外观（页签文案直接叫「模型」）
```

## 5. 关键坑（已经在别处踩过，写在这里防复发）

- **不要因为刷新数据就重建容器**：重建会打断 iframe/焦点（扩展页刚踩过 ✗）。
  正确做法：只更新文本/属性，容器只建一次。
- 折叠态是**宿主重新渲染**侧栏子树时触发的（品牌从 brand 变 railMark）→
  我们要用 `MutationObserver` 观察 `.pI_x6G_frame` 的 grid 宽度，而不是轮询 ✗。
- 所有样式进 shadow root，**禁止**写死颜色 ✗（用 `--dsw-alias-*`）。
- 图标用宿主同一套描边（16 网格），不和邻居"不一样细"。

## 6. 验收（照设计侧清单）

- [ ] 侧栏展开/折叠两套形态都对，折叠态在 Plugins 上方/下方按设计（新会话按钮下面）
- [ ] 折叠态图标 36×36、圆角 12px、hover 才出灰底，与邻居同一节奏（上下各 12px）
- [ ] 点侧栏卡 / 点折叠态图标 / 按 F2，打开的是**同一个**控制中心
- [ ] 更多菜单里有三项，且样式与宿主菜单一致
- [ ] 我的账号弹层：用户名、套餐、改密入口
- [ ] 设置里页签叫「模型」，右栏是我们渲染的模型列表（不是宿主的）
- [ ] 主题切换（浅/深）后这三处都跟着变，且**不重载页面**
- [ ] 任何一处都没有我们自己的颜色值/字体/圆角

## 7. 实施记录与踩过的坑（2026-11-08/09，1.7.x → 1.8.25）

做完的：侧栏实例卡（展开/折叠两套，真实数据）、F2 控制中心（含 boot 重启动画与 50 行日志）、
更多菜单三项、我的账号面板、设置·模型右栏；客户机 135 由 0.96.1 升到 1.8.25。

| # | 坑 | 结论 |
| --- | --- | --- |
| 1 | 插件的 `jsx` 是自带封装（≠ React.createElement）：子节点只认 **props.children** | 写成 `jsx.jsxs('div', {children: [...]})`；写第三个参数会渲染成**空行**（4 行空格子就这么来的） |
| 2 | provider 包里的 `models` 是**对象数组**（`{id,name,base_url,…}`） | 渲染前必须归一化成 id 字符串，否则 React #31（objects are not valid as a React child） |
| 3 | 套餐 `models`/`effective_models` 都为空 = **该 provider 的全部模型** | 回退顺序：effective_models → models → provider.models；只按前两者会漏套餐、下拉也缺项 |
| 4 | 实例页的接口是**根相对**的 `/me`（不是 `/mu-api/*`） | `/mu-api/*` 在实例页 403；数据一律走 `/me` |
| 5 | `/me` 系列后端只有 POST 没 GET → 页面静默空 | GET/POST 必须成对；`/me` 里的 `plans_error` 是排查入口 |
| 6 | 用户默认模型存 `users[..].default_override`，读却读 `default_plan` | 写读同源；"保持平台默认"要发 `plan-default-clear`（原来前端直接 return ✗） |
| 7 | 侧栏"实例状态"那一行是**我们自己的 client plugin** 插的，且自带 observer 会重建 | 不要在运行时删 ✗（删一次重建一次，还叠加 15s 轮询 → 请求风暴）；改**注入源**并从实例里清掉已装副本再 sync |
| 8 | 同一功能三套面板并存（旧 panel.js / 插件面板 / 新 mu-shell）→ 按 F2 弹三个 | 只保留一套；F2 用 capture + `stopImmediatePropagation()` 定归属 |
| 9 | 承载浮层的宿主是 `pointer-events:none` → 面板里所有按钮点不动 | 面板自身必须 `pointer-events:auto` |
| 10 | 改网关模板后页面不变 | `apk install` → **`dsh-muctl sync`** → **`rc-service dsh-mu-gateway restart`**（不是 openresty） |
| 11 | 内存/进程数值直接显示 | `/me` 里 `mem_used/mem_max` 是**字节**，要换算 MB |
| 12 | 扩展 iframe 里 DOM 突变刷屏、hover 抖动、按钮点不上（请求面板无异常） | 宿主的 `ready → init` 握手死循环：ready 只回一次 init（docs/21 §7.2.1）；三个探针可定位：iframe load 计数 / init 计数 / DOM 突变计数 |
| 13 | 想"看起来像设计稿"就自己写样式 ✗ | 先**逐行搬**设计稿的 CSS/结构，再只替换数据绑定；自造类名必然对不上 |

### 客户机升级的两条操作经验

1. 实例重启**必须后台跑 + 轮询**（`setsid … &` 再查状态）：同步等会被 SSH 30s 超时掐断，留下中间态。
2. `instance start` 在"状态记录 stopped 但服务仍在"时会报 already started；用 `instance restart` 更稳，
   判定存活看三件事：`instance status` 行 ✓ `rc-service status` ✓ 端口探测（401 = 活着，要求登录）✓。

## 附 A：实例 jail 的挂载表（照这张表记，别凭记忆）

除 `EXTRA_MOUNTS`（实例 conf 里的 `host:jail[:ro]`）与受保护插件的 ro 自 bind 之外，
`jail/dsh-mu-jail` 每次都建立这些挂载：

| jail 内路径 | 宿主来源 | 模式 | 说明 |
| --- | --- | --- | --- |
| `/`（自身） | 实例 rootfs | rw | 先自 bind 一次，bwrap 要求 `/` 是挂载点 |
| `/proc` | 新建 proc（失败退回宿主） | rw | |
| `/proc/sys` | 宿主 | ro | 防改内核参数 |
| `/sys` | 宿主 | ro | |
| `/dev` | 宿主 | ro | 防伪造设备 |
| `/tmp` | tmpfs 256 MB | rw | 每次启动清空 |
| `/run` | tmpfs 16 MB | rw | |
| `/etc/resolv.conf`·`/etc/hosts`·`/etc/localtime` | 宿主 | ro | |
| **`/usr/local/lib/node_modules`** | **runtime 的 `node_modules`** | **ro** | ★ 整棵树（见下） |
| `/home/dsh` | `instances/<n>/home` | rw | DSH_HOME：会话/凭据/profiles/用户插件 |
| `/home/dsh/workspace` | `instances/<n>/workspace` | rw | 用户真实工作区（覆盖 home 里的同名目录）|
| `/home/dsh/.pnpm-store` | `$DSHMU_PNPM_STORE` | rw | 默认**不挂** |
| 受保护插件（每包一条）| 它自己 | **ro 自 bind** | 内核 EROFS；已 drop CAP_SYS_ADMIN，卸不掉 |

**运行时是"一棵树"，不是一个包**：`@deepseek-ai/dsh` 从**同级** `node_modules` 解析依赖
（`@deepseek-ai/dsh-app-boot`、`dsh-tool-*` …）。所以挂整个 `node_modules`；
只挂 `.../@deepseek-ai/dsh` 会报 `Cannot find package '@deepseek-ai/dsh-app-boot'`（踩过）。

## 附 B：备份包含什么（`dsh-muctl backup`）

默认输出 `/var/backups/dsh-mu-<时间戳>.tgz`（0600，含 `manifest.json`）。按组过滤：
`--only` / `--except`，组名如下：

| 组 | 内容 |
| --- | --- |
| `state` | `/etc/dsh-mu/state.json`（用户/实例/端口/配额）|
| `config` | `dsh-mu.conf`、`.default` |
| `secrets` | `admin.token`、`secret.key`、`providers.json` |
| `gateway` | `gateway/{users,upstreams,config}.json` |
| `providers` | `/var/lib/dsh-mu/providers/`（平台 provider 包，如 `mu-*`）|
| `plans` | `/var/lib/dsh-mu/plans/` |
| `tls` | `/etc/dsh-mu/tls/` |
| **`usage`** | **`/var/lib/dsh-mu/state.db`**（用量 + token 库；用 SQLite 在线备份 API 做一致快照）|

`--with-instances` 额外备份每个实例的两个卷：
`instances/<n>/home`（DSH_HOME：会话、凭据、profiles、**用户自己装的插件**）与
`instances/<n>/workspace`（用户文件）。

**不在备份里**：runtime（apk 提供）、jail 模板（可重建）、pnpm store（默认不挂）、
平台插件 tarball 缓存（`/var/lib/dsh-mu/plugin-cache`，可重新 fetch）。

备份读的是**宿主路径**，不经过 jail —— 所以"jail 里只读/看不见"不影响备份，
受保护插件也照样被备份到。

## 附 C：运行时（runtime）由 admin 管理

### C.1 形态：一个含 `node_modules` 的目录

平台只认一件事：`dsh_runtime_dir` 指向一个**含 `node_modules/@deepseek-ai/dsh` 的目录**。
这个目录可以是：

| 形态 | 产生方式 | 磁盘 | 说明 |
| --- | --- | --- | --- |
| squashfs 镜像（推荐）| CI（`dsh-mu-runtime`）或 `runtime pack` | ~95–100 MB/版 | `mount -o loop,ro` 到 `runtimes/<id>/`；只读、可校验 |
| 解压目录 | 无 loop 的机器自动降级 `unsquashfs`；或手工放目录 | ~560 MB/版 | 对 jail 完全等价（`mode=extracted`）|
| 旧版 npm 树 | 老机器升级上来时已是这种 | 560 MB+ | **迁移输入**：`runtime pack` 一次转成镜像，不做长期维护 |

**不用软链**：配置里存绝对路径，切换 = 改一个配置值。
（软链在 jail 里追不到目标，踩过；`readlink -f` 只是历史兜底。）

### C.2 版本标识：`<dsh>-<variant>[-r<rev>]-<sha6>`

```
0.2.2-rc.1-local-50364b      本机（自己 build）   variant=local，指纹 50364b
0.2.2-rc.2-plain-523bba      CI 构建            variant=plain
0.2.2-rc.1-vscode-a1b2c3     变体 + 指纹
```
* `variant` 段**总是写出来**（`plain` 也写），这样"CI 构建 / 本机构建 / 变体"一眼可分。
* `sha6` 是**镜像内容指纹**（sha256 前 6 位）。id 自带指纹 ⇒ 同一 DSH 版本可以有任意多个
  构建，互不覆盖；"清单里的 id"与"装完的 id"完全一致。
* `rev`（`-rN`）保留给"同内容重新打包"的少数场景；一般靠指纹就够了。
* 解析顺序：先剥尾部 `-<sha6>`，再看 `-<variant>`，最后 `-<rev>`。旧格式（无 variant/无指纹）仍然可读。

`MANIFEST.json`（schema 1）字段：`id / dsh / variant / rev / arch / libc / source /
built_at / node_build / node_pty / pty_sha256 / provides[]`。

### C.3 两个清单文件（仓库侧，`dsh-mu-runtime/channels/`）

| 文件 | 内容 | 谁维护 |
| --- | --- | --- |
| `channels.json` | **指针**：`default` / `alpha` 各一条（哪个构建算"默认"）| CI（`channel=` 参数）|
| `runtimes.json` | **历史**：所有构建的 `id / dsh / variant / rev / sha256 / size / url / built_at` | CI 每次发布追加 |

* 二者职责不重叠：通道是指针（一对一），历史是清单（多对多累积，最多 100 条）。
* 「读取线上版本」**只读 `runtimes.json`**（浏览器直连）：列出全部历史 + 标出哪个是默认。
* `dsh-muctl runtime install`（不带版本）读 `channels.json` 的 `default`。
* 内网镜像：把这两个 JSON + `.squashfs` 放到客户可达的位置，改 `runtime_channel_base` 即可，
  **完全不碰 GitHub**。

### C.4 管理入口（三个，同一套实现）

```
命令行： dsh-muctl runtime status|list|install|use|pack|prune|channels|verify|ensure-mounted
admin ： 运行 → 运行时
   GET    /api/runtimes                  列表（默认/用量/实例分配/指纹）
   POST   /api/runtimes/download         后台下载任务（直连 + 镜像并发竞速）
   GET    /api/runtimes/downloads        任务列表（进行中 + 3 小时内历史）
   GET    /api/runtimes/download/<job>   单任务进度（received/total/state/error）
   POST   /api/runtimes/upload?id=<id>   裸流上传（页面用 XHR 显示进度/预计剩余/可取消）
   POST   /api/runtimes/default          设为「实例默认」，并把跟随者重启对齐（幂等）
   POST   /api/runtimes/assign           给单个实例指定运行时（见 C.6 规则）
   POST   /api/runtimes/verify           MANIFEST↔id、pty.node、目录可用性
   DELETE /api/runtimes/<id>             见 C.7 删除规则
```

### C.5 下载/上传的落地规则（内容寻址）

* 下载：候选源 = **直连 + 镜像前缀**（`runtime_mirrors` 可配，另可给 `runtime_proxy_list`），
  全部并发，**谁先吐满 1 MB 谁赢**，其余立刻放弃（镜像快慢随客户网络变化，不能写死顺序）。
* 上传/下载完成后：算 sha256 →
  * 同一 id 且内容相同 → **幂等**，什么都不做；
  * 同一 id 但内容不同 → 存成 `<dsh>-<variant>-<sha6>`（**绝不覆盖在用镜像**）。
* 落盘后**立刻只读挂载**（`mode=mounted`），所以列表里马上能看到大小/模式/node-pty。

### C.6 端口与运行时的一致性规则

```
实例在运行  → 改端口 / 改运行时后 **立即重启**（应用新值）
实例已停止  → 只保存配置，**不启动**（何时拉起由运维决定）
改「实例默认运行时」→ 跟随默认的运行中实例**全部重启对齐**；默认没变时是幂等 no-op ✓
```
实现位置（同一份逻辑，不是页面逻辑）：`apply_instance_runtime()` / `change_instance_port()` /
`set_default_runtime()`，CLI 与 admin 都走它们。

### C.7 删除规则

```
① 是「实例默认运行时」        → 拒绝
② 有运行中的实例在用它        → 拒绝（并列出实例名）
③ 只有已停止的实例在用它      → 允许删除，同时把这些实例回落到「默认」
```
`runtime prune`（按数量保留）走同一套判断。

### C.8 已知前提与坑

* squashfs 挂载需要 **loop 设备**；无 loop 自动降级解包（`mode=extracted`）。
* 开机后 loop 挂载会消失 → `/etc/init.d/dsh-mu-runtimes` 重新挂载；`sync` 也会幂等补挂。
* `runtime pack` 会校验源树（`node_modules/@deepseek-ai/dsh/package.json` + `node-pty/.../pty.node`）；
  **它会在网关 rootfs 的 `tmp/` 下留 500–800 MB 临时树**，用完请清（或在有 loop 的机器上别用它）。
* `runtime migrate-ids` 会改镜像名/挂载/实例 conf：**默认只预演**，必须 `--yes` 才执行
  （曾把 8 个实例弄挂 15 分钟）。
* 同一份页面里不要出现两份同名函数：JS 后定义会覆盖先定义，表现为"新功能不生效"。
  `tests/ui-lint.mjs` 现在会拦（重复定义 / 跨 script 块误用 `apiJ` / `</html>` 之后有内容）。
