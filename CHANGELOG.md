# CHANGELOG

## 0.21.4（`nftables` 其实改错了地方：无签名包读的是 make-apk.sh）

- 0.21.3 我只改了 `packaging/APKBUILD` 的 `depends` ✗ —— 那份只在 `--signed`（abuild）路径生效 ✓；
  我们实际发布的**无签名 v3 包**由 `apk mkpkg` 打包 ✓，依赖写死在 `packaging/make-apk.sh`
  的 `-I "depends:..."` 里 ✗ → 装出来的包**依然没有 nftables** ✗（实测 `apk info -R` 为空 ✓）；
- 现在两处都加上了 ✓，`make-apk.sh` 里的列表提成一个 `DEPENDS` 变量并注明"只认最后一个
  `-I depends:`"，同时在注释里写明要和 `APKBUILD` 保持同步 ✓；
- 顺手把两处列表对齐 ✓（都去掉 `nodejs`/`npm`/`pnpm` —— DSH 运行时装在网关模板里 ✓，宿主不需要 ✓）。

## 0.21.3（`nftables` 写进 apk 依赖）

- 之前 `nftables` 是**手动步骤** ✗：不装 → 默认开启的出网策略**静默不生效** ✗，
  只有 `install`/`doctor` 的 hint 会提一句 ✓ —— 默认开着却默认不生效，这个默认值不合理 ✓；
- 现在 `depends="python3 nodejs npm pnpm util-linux logrotate nftables"` ✓，
  装包即装好 ✓，`apk info -R dsh-mu` 能看到 ✓；
- 文案同步 ✓：`install` / `doctor` / `egress show` 的提示改成"它是包的依赖，缺了就
  `apk add nftables` 或重装 dsh-mu"✓；docs/05、docs/06、README 里"需要手动装"的说法都改掉 ✓。

> **关于版本号**：`0.16.1` / `0.18.2` / `0.18.3` / `0.18.5` 是当天连续排障时的**中间构建** ✓，
> 它们的修复已合并进相邻条目（`0.16.0`、`0.18.1`、`0.18.4`）✓，没有单独记 ✓。
> 当前版本见 `VERSION` 文件 ✓。

## 0.21.1（`plan order` 的用法提示）

- `dsh-muctl plan order <id> 20` 会被 argparse 拒 ✗ —— 顺序是 `--order` 选项 ✓；
  现在提示直接写成 `plan order <plan-id> --order <number>` ✓，并说明"越小越靠前，也就是用户看到的顺序" ✓。
- 顺带修掉一个 AttributeError ✓：`plan order` 的兜底读了 `args.policy` ✓，而 `plan` 子命令没有这个属性 ✗
  （只有 `egress` 有 ✓）→ 用 `getattr(args, "policy", "")` ✓。
- 另外说明一个重要现象 ✓：**把平台搜索套餐删掉之后 `doctor` 会 WARN（web search plan）** ✓ ——
  这是故意的 ✓（没有可用搜索套餐时应当可见 ✓），加一个隐藏的 DeepSeek 套餐就会恢复 PASS ✓。

## 0.21.0（套餐加「隐藏」与「顺序」；web_search 用平台套餐）

按讨论落地 ✓：搜索变成**平台能力** ✓，不再依赖用户自己的官网套餐 ✓。

- **套餐新增两个字段** ✓：
  - `hidden`：用户**看不到、选不到**（不进抽屉、不进模型选择器、不进默认链 ✓），
    但平台内部**仍然能用它跑 web_search** ✓；
  - `order`：数字，越小越靠前 ✓ —— 同时决定 **① 抽屉展示顺序 ✓ ② web_search 用哪个套餐** ✓；
    **不影响默认模型** ✓（默认模型只由「默认模型（按优先级）」那条链决定 ✓）；
- **搜索选择规则**（`search_plan()`）✓：按 `order` 取**第一个能跑 DeepSeek 搜索**的套餐
  （`api=anthropic-messages` 且 `base_url` 指向 `api.deepseek.com` ✓，**含隐藏套餐** ✓）；
- **relay 新增固定路由 `__search`** ✓：每个实例的 `relay.json` 里多一条 `__search` ✓，
  指向上面那个套餐 ✓ —— 于是 patch 里可以写成
  `/mu-llm/<token>/__search/v1` ✓，**relay.lua 不用改** ✓，用户也拿不到套餐 route ✓；
  对应的凭据占位符是 `DSHMU_SEARCH` ✓（真实 key 永不进 jail ✓）；
- 没有平台搜索套餐时，行为回退到原来的"用户自己的官网 DeepSeek" ✓，再没有就"注册但不可用 + 隐藏 tool-web.search" ✓；
- 后台「套餐」表：新增「顺序」列（↑↓ 一键重排 ✓，自动写成 10/20/30… ✓）和「隐藏 / 取消隐藏」✓，
  隐藏行淡显 + 徽章 ✓；表格上方显示 **当前搜索将使用：<套餐>** ✓（把隐式关系变可见 ✓）；
- CLI：`dsh-muctl plan hide|show <id>` ✓、`plan order <id> <n>` ✓、`plan add --order N` ✓；
- `doctor` 新增 **web search plan** 检查 ✓（没有可用套餐时 WARN ✓）；
- 说明：隐藏套餐 = **平台买单的共享资源** ✓（本项目是给小型团队用的 ✓，不打算就别加隐藏套餐 ✓）。

## 0.20.7（出网状态列恢复，但只读）

- 0.20.5 我把「出网」列整个删掉了 ✗ —— 我的理解偏了 ✓：要的是**不能改**，不是**看不见** ✓；
- 现在这列回来了 ✓，但渲染成**只读徽章**（`禁内网` / `全限制` / `不限制`）✓，
  有本实例例外时后面跟 `+N` ✓，悬停显示例外清单 ✓；**没有下拉、不能改** ✓；
- 修改入口仍然只有实例行的「配置」✓（一处能改 ✓，一处能看 ✓）。

## 0.20.6（特殊规则也改成"先按钮后弹窗"）

- 「出网特殊规则」卡片原来把表单**平铺**在页面上 ✗（实例下拉 + 目标输入 + 添加按钮 ✓），
  违反我们自己的 UI 约定 ✓；现在是：卡片只有一张规则表 + 一个「＋ 添加规则」按钮 ✓，
  点开**弹窗**（实例 / 目标 / 语法提示 ✓）→ 点「添加」才提交 ✓，成功自动关闭 ✓；
- 卡片说明里的过时文案也改了 ✓：「每个实例的策略在那一行的**配置**里选」✓（原来写"表格的「出网」列"✗）；
- 顺带删掉卡片里多余的"备注"输入 ✗（它从来没被保存过 ✓）。

## 0.20.5（出网只能在「配置」里改；配置里不再增删特殊规则）

- 实例表格**去掉「出网」列** ✓ —— 策略只在「配置」弹窗里选 ✓（页面上只有一个入口 ✓）；
- 「配置」里的出网段**只留下策略下拉** ✓：去掉了"例外目标"输入框与「添加」按钮 ✓，
  也去掉了每条规则的「删除」✓ —— 现在只显示一行提示：
  `当前放行：…（在下面「出网特殊规则」里增减 ✓）` ✓；
- 特殊规则**统一在实例页下方的「出网特殊规则」卡片里维护** ✓（含「全部实例」✓），
  一处新增、一处删除 ✓，不再两处都能改 ✓；
- 顺带清掉因此不再使用的 `egressCell()` / `setInstanceEgress()` / `addConfigRule()` ✓。

## 0.20.4（实例操作列重做：图标 + 一个「配置」）

- **删掉 `provider`** ✓：它写了 `inst["provider"]` ✓，但 `sync_instance_plans()` 会用套餐**整份重写** patch ✓ →
  除了网关里一个只用来展示的字段 ✓，它**不影响实例实际用哪个模型** ✗（上一轮删掉那张表时漏了这个入口 ✓）。
  `POST /api/instances/<n>/provider` 端点与 `editProvider()` 一并移除 ✓（宿主机仍留 `dsh-muctl instance provider` 做排障 ✓）；
- **操作列改为图标 + 少量文字** ✓：

  | 按钮 | 含义 |
  |---|---|
  | `▶` | 启动（仅在未运行时出现） |
  | `🛑` | 停止（仅在运行时出现） |
  | `⟳` | 重启（按钮上显示进度秒数） |
  | `🔑` | 重建登录态（原 refresh-cookie，页面打不开时用） |
  | `日志` | 跳到「实例日志」页并载入该实例 |
  | `配置` | 弹窗：限额 + 出网策略 + 本实例例外 + 清个人默认 |
  | `删除` | 不变 |

  图标都带 `title` 与 `aria-label` ✓（悬停即见中文说明 ✓）；
- **「配置」把这些合成一个弹窗** ✓：CPU/内存/进程数/端口 ✓、出网策略三选一 ✓、
  本实例例外目标（可增删 ✓）、以及「清除该用户的个人默认」✓ —— 原来分散的「限额」「出网」「清默认」入口都收进来了 ✓。

## 0.20.3（重启后 refresh-cookie 取不到 token）

- `wait_for_banner()` 只读当前日志 ✓，而 logrotate 是 `copytruncate` + 留 4 份 ✓ ——
  实例重启后 banner 常常落在 `.log.1` 里 ✗ → 报 "no live launch token found" ✗；
- 现在依次读 `.log`、`.log.1..4`、`.log.gz` ✓（实测这条老毛病会让人以为实例挂了 ✓）。

## 0.20.2（"全部实例"规则没存住）

- 后端把共享规则写进了 `st["config"]` ✗ —— 那是内存里的快照 ✓，配置真正的落点是
  `/etc/dsh-mu/dsh-mu.conf` ✓ → 没调 `save_config()` ✗ → 返回 200 但读回来是空 ✗；
- 已补 `mu.save_config(st_e["config"])` ✓；`egress allow-*` 缺参数时的提示也改清楚 ✓。

## 0.20.1（特殊规则：支持"全部实例"与地址/端口区间）

- **全部实例** ✓：目标可以挂到一个共享清单上 ✓（存在 `INSTANCE_EGRESS_ALLOW` ✓），
  渲染时**并入每个实例**的链 ✓ —— 有自己规则的实例也照样继承它 ✓（不再互相覆盖 ✗）；
  UI 的实例下拉第一项就是「全部实例」✓，规则表里显示为「全部实例 / 所有实例」✓；
  CLI：`dsh-muctl egress allow 10.0.0.0-10.0.0.9:10000-11000` ✓（`allow`/`allow-add`/`allow-rm` ✓），
  `egress instance all --allow …` 也可 ✓；
- **区间** ✓：目标支持 `ip` / `ip/24` / `ip1-ip2` ✓，每种都能带 `:443` 或 `:10000-11000` ✓，
  例：`192.168.5.0-192.168.5.100:10000-11000` ✓（IPv4/IPv6 都行 ✓，IPv6 带端口写
  `[2001:db8::1]:443` ✓）；
- 解析改用标准库 `ipaddress` ✓（之前的正则会把 `abc` 当 IPv6 ✗、把 `2001:db8::1` 的
  末段当端口 ✗），现在会校验地址/网段/区间方向/端口范围 ✓，反向区间与 `0.0.0.0/0` 都会被拒 ✓。

## 0.20.0（出网策略改成"实例属性 + 特殊规则清单"）

按讨论重做交互 ✓ —— 之前是"一张大卡片里全局策略 + 每实例覆盖 + 白名单输入框" ✗，
把两种不同的东西混在一起了 ✓。

- **策略归到实例**：实例表格新增「出网」列 ✓，每行一个下拉 ✓：
  **全限制 / 禁内网 / 不限制** ✓（`allowlist` / `no-intranet` / `open` ✓），改完立即生效 ✓；
- **特殊规则单独成一张清单** ✓：一行 = 一个实例 + 一个放行目标 + 备注 ✓，可增可删 ✓；
  语义随策略变化 ✓：
  - 实例选**全限制** → 清单就是"**只允许这些**" ✓（外加自身端口 / relay / DNS ✓）；
  - 实例选**禁内网** → 公网自由 ✓、内网默认拒 ✓，清单里的目标**例外放行** ✓；
  - 实例选**不限制** → 清单不生效 ✓；
- 新实例的默认值仍然来自 `INSTANCE_EGRESS`（页面上标出"新实例默认：禁内网"）✓；
- API：`GET /api/egress` 多返回 `labels` ✓；`POST` 只改策略时**不会清空**该实例已有的特殊规则 ✓；
- CLI 不变（`egress set` 改默认 ✓、`egress instance <n> <policy> --allow …` 改单个 ✓）。

## 0.19.4（受保护：路径拼接再修 + 不再越权安装）

- `protected_paths()` 拼宿主路径时漏了 `node_modules` ✗ →
  `<home>/.dsh/profiles/web/<pkg>` ✗ → 永远判为"不存在" ✗ → `RO_PATHS` 恒为空 ✗ →
  只读挂载**根本没生效** ✓（`pnpm remove` 照样能删 ✓，实测 ✓）；
  现在用 `instance_home + (jail路径去掉 /home/dsh 前缀)` ✓；
- 0.19.3 让 `protected` 也"缺了就重装" ✗ —— 结果**给每个实例都装上了** ✗（bob 也中招 ✗）；
  已收回：只有 **`mandatory`** 会安装 ✓，`protected` 只保护已存在的 ✓，
  若发现"受保护但文件不在了"则打 WARNING 提示改用强制安装 ✓。

## 0.19.3（受保护插件若已被删，要能重装回来）

- 追查发现真正的原因不是路径 ✗：**DSH 启动时会按 `package.json` 裁剪 `node_modules`** ✓ ——
  用户"卸载"后清单里没有它 ✓，下次启动这些文件就被 pnpm 删掉了 ✗；
- 所以只"补清单"不够 ✓：现在 `protected` 和 `mandatory` 一样，**缺了就重装** ✓
  （`install_mandatory_plugins()` 同时覆盖两者 ✓），顺序仍是"先补齐 → 再对账清单" ✓；
- 完整闭环因此变成：用户卸载 → 只读挂载让 pnpm 删不掉 ✓ → `sync` 补清单 ✓ →
  重启后插件照常加载 ✓；万一文件真被删了（比如保护还没挂上时）→ `sync` 重装 ✓。

## 0.19.2（受保护插件：路径前缀用错，自愈与只读挂载其实都没生效）

- `installed_package_dirs()` 和 `protected_paths()` 都拼成
  `instances/<n>/rootfs/home/dsh/.dsh/profiles/…` ✗ —— 但实例的 profile 在
  `instances/<n>/home/.dsh/profiles/…` ✓（`rootfs` 只是模板 ✓，`home` 是运行时绑进去的 ✓）；
- 后果：自愈一次都没跑过 ✗（"看起来没有已装包"→ 直接跳过 ✓），只读挂载也只在
  `RO_PATHS` 恰好非空的那一版偶然生效过 ✓；
- 现在两者都用 `instance_home()` ✓。jail 侧不用改 ✓（那边 `$ROOTFS/home/dsh` 已经是绑定后的目录 ✓）。

## 0.19.1（插件策略三处修正）

- **自愈失效** ✗：`reconcile_protected_plugins` 用 `instance_plugins()`（= 读 package.json 依赖）
  判断"这个包装了没有"✓ —— 而卸载恰恰就是改这个文件 ✗ → 判定为"没装" → 直接跳过 ✗，
  自愈形同虚设 ✗。改为**看 `node_modules` 里的真实目录** ✓（`installed_package_dirs()` ✓），
  `mandatory` 的安装判断也一起换掉 ✓；
- `dsh-muctl plugin policy`（不带子命令）会报 "needs a package name" ✗ —— `policy` 没被当成"列出" ✓，已修 ✓；
- `RO_PATHS` 原来给**所有**实例都导出受保护包路径 ✓，没装它的实例会在 jail 日志里刷
  `protected path missing` ✗；现在只导出**实际存在**的目录 ✓。

## 0.19.0（jail 加固产品化：出网策略 + 去掉 sys_admin + 插件策略；抽屉/后台的插件管理下线）

### 1. 出网策略（nftables，按 cgroup 匹配）
- 新配置：`INSTANCE_EGRESS`（`open` / **`no-intranet`（默认）** / `allowlist`）+
  `INSTANCE_EGRESS_ALLOW`（`10.1.2.3:443, 192.168.5.0/24`，可带端口）；
- `dsh-muctl sync` 渲染 `/etc/dsh-mu/egress.nft` 并用**一条 nft 事务**装载（原子 ✓，无半张表 ✓）；
- 每条链固定包含 **`ct state established,related accept`** ✓ —— 少了它连实例自己的回包都会被挡 ✗
  （实测踩过：端口在听但连不上 ✗）；
- 放行：自身端口 + relay ✓、配置的 DNS ✓、白名单 ✓、公网（`no-intranet` 模式）✓；
  拒绝：其它 loopback 端口 ✓（别的实例 / 管理 API ✓）、RFC1918 + link-local + CGNAT ✓、
  IPv6 ULA/link-local ✓；`allowlist` 模式连公网也拒 ✓；
- 单实例覆盖：`state.instances[<n>].egress` / `.egress_allow`（后台卡片里可设 ✓，改完**立即生效** ✓
  不用重启实例 ✓）；
- CLI：`dsh-muctl egress show | set <policy> | allow <list> | allow-add | allow-rm |
  instance <name> <policy> [--allow …] [--reset]` ✓
- `doctor` 新增两项：`egress nft`（没装 nftables 就 WARN ✓）、`egress rules`（表在不在、每个实例有没有链 ✓）。

### 2. 实例不再保留 `CAP_SYS_ADMIN`
- `INSTANCE_KEEP_CAPS` 默认改为空 ✓；`install` 会把旧默认 `sys_admin,setuid,setgid` 自动迁移成空 ✓；
- 依据：实测去掉后 DSH 完全正常 ✓（cookie 刷新 ✓、模型链路 ✓、bash ✓、插件安装 ✓），
  而 `umount` 变成 `must be superuser to unmount` ✓、`mount -t tmpfs` 变成 `permission denied` ✓
  —— **只读挂载保护从此不可绕过** ✓。

### 3. 插件策略：受保护 / 强制安装（两个独立开关）
- 新增 `state.plugin_policy = {pkg: {protected, mandatory}}` ✓；
- **受保护**：jail 启动时把该包的 `node_modules/<pkg>` 做**同路径只读绑定** ✓（`RO_PATHS` ✓，
  内核 EROFS ✓）；`sync` 还会把 `package.json` 的依赖与 `dsh.profile.bundles` 补回 ✓、
  并清掉 `cordis.patch.yml` 里针对它的 `disabled: true` ✓（否则"文件还在但不加载" ✗）；
- **强制安装**：`sync` / 新实例创建时对每个 `mandatory` 包best-effort 安装 ✓，
  失败只 WARN 不阻塞 ✓；
- CLI：`dsh-muctl plugin policy | protect | unprotect | require | unrequire <pkg>` ✓
- API：`GET/POST /api/plugin-policy` ✓（GET 同时返回各实例已装清单 ✓）。

### 4. 插件管理从平台下线（按讨论结论）
- **删除抽屉里的插件卡** ✓（`me.lua` 不再下发插件数据 ✓，`/api/me/plugins` 端点删除 ✓）；
  用户直接用实例内 DSH 自带的插件管理器 ✓；
- **删除后台的"可装清单（catalog）"** ✓：`POST /api/plugins`、`plugin_catalog` handler、
  抽屉的 install/uninstall 密码门槛全部移除 ✓（实测那门槛本来就被原生 UI 绕过 ✗）；
- 「插件」标签页改为**只读清单 + 策略编辑** ✓：每个包一行（受保护 / 强制安装两个勾 ✓），
  下面列出每个实例装了什么、来源（平台/自装）✓。

## 0.18.7（「启动」显示横线：又是一个白名单漏字段）

- `instance_payload()`（抽屉的实例数据）也是**手写白名单** ✓，没带 `started_at` ✗ →
  抽屉拿到 `undefined` ✓ → 渲染成 `—` ✗。现在带上 ✓（先取 `state.json` 里的 ✓，
  后端视图有更新值时以后端为准 ✓），并从后端视图 `full.instance` 里取最新值 ✓；
- 这已经是同一类问题的第三次 ✗（`resources` 漏 ✓、`my_default` 漏 ✓、`started_at` 漏 ✓），
  所以加了一条**闸门**（ui-lint 现 12 项）✓：`panel.js` 里读的 `i.started_at` / `i.resources` /
  `data.my_default`，**必须在 `me.lua` 的返回表里出现** ✓ —— 已反向验证
  （删掉 `started_at = started_at` 那行立刻 FAIL ✓）。

## 0.18.6（修：设了默认却显示"未设置"；重启带进度；实例显示启动时间）

- **真正的原因** ✗：抽屉保存其实**成功了** ✓（`state.users[<name>].default_override` 有值 ✓），
  但 GET `/me` 的返回是 `me.lua` 里**手写白名单** ✓，没有 `my_default` 这一项 ✗ →
  抽屉读不到 ✓ → 永远渲染"未设置" ✗。已把 `my_default = {plan, model}` 加进返回 ✓；
- **重启有进度了** ✓：点「重启」后按钮上显示 `重启中… 12s` ✓，每 2 秒轮询 `/me` 直到实例重新应答 ✓，
  完成后提示 `已重启完成（38 秒）` ✓；超过 3 分钟未恢复会提示去看实例日志 ✓
  （抽屉里的「立即重启」横幅也走同一条路径 ✓，不再只静默等 6/20 秒 ✓）；
- **实例卡片新增「启动」** ✓：`启动于 2026/10/23 07:41:12（12 分钟前）` ✓
  （后端 `instance_view` 增加 `started_at` ✓，由 `start_instance()` 写入 ✓）；
- 「已保存」的提示改成**指明下一步** ✓：`已保存 ✓ 点上面「我的实例」里的「重启」让它生效（约十几秒到一分钟）` ✓。

## 0.18.4（抽屉「我的默认模型」改成先按钮后弹窗；修网关 stale Lua）

- **「我的默认模型」重做** ✓：卡片上只显示**当前状态**
  （`已设置：myog · glm-5.3` 或 `未设置（新会话按平台默认：Kilo Free）`✓），
  右边一个「设置 / 修改」按钮 ✓ → 点开**弹窗**选套餐 + 模型 ✓ → 保存 ✓。
  已设置时状态行里才出现「清除」✓ —— 之前表单平铺 ✓ 且**预填了 Kilo Free** ✗，
  用户根本看不出自己到底设没设 ✓；
- **修 `失败：unknown action`** ✗：`me.lua` 已经加了 `plan-default` 转发 ✓，但**网关没 reload** ✗ ——
  openresty 的 `lua_code_cache on` 会把旧 Lua 留在内存里 ✓，`apk` 升级只换文件 ✓，
  于是抽屉打到的还是旧逻辑 ✓。现在：
  - `dsh-muctl sync` 会检测"Lua 文件比网关进程新" ✓ 并**自动 reload**
    （jail 模式重启 gateway 服务 ✓，host 模式 `rc-service openresty reload` ✓）；
  - `doctor` 新增一项 **gateway lua** ✓：stale 时 WARN 并给出修复命令 ✓；
  - jail 模式的 pid 文件叫 `/run/dsh-mu-__gateway.pid` ✓（不是 `openresty.pid` ✗）——
    一开始只查了 openresty/nginx 的固定路径 ✓ → 永远拿不到启动时间 → **恒为 PASS** ✗（假阴性 ✗）；
    现在用 `glob('/run/*gateway*.pid')` + openresty/nginx 一起兜 ✓。
- 「UI 约定：先按钮、后表单」写进 `docs/06-运维手册.md` ✓
  （新增/修改类操作一律按钮 + 弹窗 + 当前状态 ✓）。

## 0.18.1（用户级默认模型：个人一条，优先于 admin 的链）

- **用户抽屉新增「我的默认模型」** ✓：套餐 + 模型两个下拉 ✓，**只能设一条** ✓
  （不是优先级链 ✓），「保存」/「清除」✓ —— 存在 `state.users[<name>].default_override` ✓；
- **优先级**：用户自己的选择 **高于** admin 的「默认模型（按优先级）」✓：
  `sync_instance_plans` 先按用户那条解析 ✓，不可用（套餐没了/模型不在可用集合）才顺延到 admin 链 ✓，
  再不行回落到"第一个可见套餐" ✓；
- **admin 实例表每行新增「清默认」** ✓（`POST /api/users/<name>/default-clear` ✓），
  只清那个用户的个人默认 ✓，不动 admin 链 ✓；
- **admin「默认模型」区新增覆盖列表 + 一键清除** ✓：
  列出"哪些用户设了自己的默认"✓（用户 / 套餐 / 模型 / 清除 ✓），
  并有 **「一键清除全部用户默认」** ✓（`POST /api/user-defaults/clear-all` ✓）；
- CLI：`dsh-muctl default user-set <user> <plan> [--model <m>]` ✓、
  `default user-clear <user>` ✓、`default user-clear --all` ✓，`default list` 会先列个人默认 ✓；
- 网关 `me.lua` 转发两个新动作 `plan-default` / `plan-default-clear` ✓（走 `/me`，与其它抽屉操作一致 ✓；
  直连后端时动作名是 `default` / `default-clear` ✓ —— 测试时容易搞混 ✓，两者都可用 ✓）；
- **模型校验补严** ✓：个人默认的模型原来只跟"套餐已选子集"比 ✓ ——
  而"全部模型"的套餐子集是空的 ✓ → **任意字符串都能存进去** ✗（`model: "nope"` 返回 200 ✗）。
  现在先按 **provider 目录**校验 ✓，再按套餐子集校验 ✓，两处都 400 ✓；
- **链会自动忽略失效条目** ✓：`default_chain()` 里跳过 `load_plan()` 取不到的 plan id ✓ ——
  0.18.0 把 `plan_kilo_free` 改名成 `plan_default` 后 ✓，旧链里那一档就变成"指向不存在的套餐"✗，
  以前会让"第一档"静默失效 ✓；现在直接跳过 ✓，把下一档顶上 ✓。

## 0.18.0（plan_default 占位默认套餐；删掉实例页的 provider/key；优先级用高/低）

- **内置默认套餐改名为 `plan_default`** ✓：id 固定 ✓，由 `install` **每次升级按新版本的内置定义覆盖内容** ✓
  （将来内置默认换成别的套餐 ✓，还是占同一个 id ✓，不会留下旧 id 的残留 ✓）；
  `enabled` 在升级时**保留** ✓ —— 你停用过它，升级不会悄悄打开 ✓；
- **不可删除** ✓：`plan delete plan_default` 与 `DELETE /api/plans/plan_default` 都返回 400 ✓
  （提示改用"停用" ✓），后台那一行把「删除」换成「内置，随版本更新」✓；
- **保底可用** ✓：某个用户**一个可用套餐都没有**时，`plan_default` 即使被停用也仍然可见可用 ✓
  （`plan_visible_to_user` 里特判 ✓，用 `other_plans_for_user()` 判断是否真的没有别的 ✓）；
- 旧的 `plan_kilo_free` 会在升级时**自动迁移**成 `plan_default` ✓（沿用它的 `enabled` ✓）；
- **删掉「实例的 provider / key」整块** ✓（`#provAssign` + 相关 JS ✓ 约 60 行 ✓）：
  有套餐时它本来就不参与 ✓，留着只会让人以为要在这里配 ✓；
  顺带把「资源限额」「查看 patch」「连通性测试」这些结果从已删除的容器搬进**共用弹窗** ✓；
- 「默认模型（按优先级）」的第一列不再写 1/2/3 ✓，改成 **高 / 中 / 低** ✓（两行就是 高/低 ✓）。

## 0.17.6（套餐重名：个人套餐天生不冲突，团队套餐加保护）

- 结论先说：**不同用户建同名个人套餐没问题** ✓ ——
  抽屉创建时 id 是 `hash(owner + "/" + 名字)` ✓（`plan_id_for(actor_user + "/" + name_)`），
  `alice/我的 deepseek` → `plan_62c079fe`、`bob/我的 deepseek` → `plan_d95e0fd2` ✓ 互不影响 ✓；
- **但团队套餐是 `hash(名字)`** ✓ —— 后台/CLI 用同名再建一次会**静默覆盖**前一个 ✗
  （同 id → 同一个 `plans/<id>.json` ✓，`save_plan` 直接写 ✓）。三处都加了保护 ✓：
  - 后台 API：返回 **409** `a plan with this name already exists` ✓，带 `replace: true` 才覆盖 ✓；
  - 后台页面：409 时弹**确认框** ✓「已经有同名套餐…用新配置覆盖它吗？」✓；
  - CLI：`plan add` 同名直接拒绝 ✓，提示换个名字或加 **`--replace`** ✓；
  - 抽屉：同一个用户重复建同名套餐也拒绝 ✓（`you already have a plan with this name` ✓）。

## 0.17.5（真正的根因：全局 min-width:110px 也套到了 checkbox 上）

- 前面几轮我一直在挪列宽 ✗，**真正的毛病在这里** ✓：

  ```css
  input,select{ …; min-width:110px }   /* 也匹配了 checkbox ✗ */
  ```

  → **每个复选框都被撑成 110px 宽** ✗：这正是"勾离说明文字老远"✗（实例日志的「实时刷新」✓、
  用户面板的「设为默认」✓）和"勾那一列特别宽"✗ 的共同原因 ✓；
- 修法：给复选框/单选框**豁免** ✓

  ```css
  input[type=checkbox],input[type=radio]{min-width:0;width:auto;padding:0;margin:0;
    border-radius:0;background:none;vertical-align:middle}
  input[type=checkbox]{accent-color:var(--acc)}
  ```
- 勾选框**恢复成独立一列**（36px ✓ 居中 ✓），不再和模型 id 挤一格 ✓（上一版把两列合并是治标 ✗）；
- `ui-lint` 新增闸门（现 11 项）：**存在全局 `input,select{min-width:…}` 时，必须有
  `input[type=checkbox]{min-width:0}` 豁免** ✓ —— 已反向验证（删掉豁免立刻 FAIL ✓）。

## 0.17.4（模型表勾选框修好；撤掉「改范围」）

- **勾选框压住模型 id** ✗：0.17.3 把勾选做成独立的 44px 列 ✓，太窄 ✓，id 文字跑到框下面 ✗。
  现在**合并成一列**（`label.pickrow`：勾选 + id 同行 ✓，flex 布局 ✓，id 过长自动省略 ✓），
  列数 6 → 5 ✓，id 列 42% ✓；
- **撤掉「改范围」** ✗ —— 我上一轮理解错了 ✓：这个操作实际是把套餐在**团队/个人**之间**换类型** ✗，
  而不是"团队套餐限定某几个人"✓；换类型会把别人的套餐变成私人套餐 ✓，即使是 admin 也不该这么做 ✓。
  按钮、`POST /api/plans/<id>/scope` 端点、前端函数**全部移除** ✓（不留死接口 ✓）；
- **范围列的显示改成一行** ✓：原来 `团队` / `*`、`个人:alice` / `alice` 两行 ✓，看起来像"实例范围 + 用户范围"✓。
  现在只有三种明确说法：
  - `团队 所有用户` ✓
  - `团队 限 alice、bob` ✓（创建时在「可见用户」里填人名即可 ✓ —— 这就是"团队套餐限定某些人" ✓）
  - `个人 alice` ✓
- 标题下的说明改写为：**范围由创建时决定、之后不能改** ✓，要换类型就删掉重建 ✓。

## 0.17.3（模型弹窗：删掉「默认」列，修勾选列被拉宽）

- 0.17.2 的列宽改动**没生效** ✗：表格还是 `table-layout:auto` ✗ —— 宽度只是"建议" ✓，
  浏览器按内容重新分配 ✓ → 勾选列被撑得很宽 ✗。现在 `table-layout:fixed` ✓ + 逐列定宽 ✓
  （勾选 44px / 模型 id 38% / 推理等级 110px / 可用 58px / 状态 88px）✓；
- **删掉「默认」单选列** ✗ —— 默认模型现在由「默认模型（按优先级）」那条链决定 ✓，
  这个单选只在保存时写一个 `plan.default_model` ✓，与链重复 ✓；保存时也不再提交它 ✓；
- 套餐表里那句 `新会话默认 xxx` 改成 **`套餐默认 xxx`** ✓ —— 链可能给某个用户钉了别的模型 ✓，
  叫他"新会话默认"会误导 ✓；
- 模型 id 用 `text-overflow:ellipsis` ✓ 不再折行 ✓，状态徽章 `nowrap` ✓。

## 0.17.2（套餐表：去掉没用的「设默认」，补上「改 key」「改范围」，模型表列宽重排）

- **删掉「设默认」** ✗ —— 默认模型现在由「默认模型（按优先级）」那条链决定 ✓，
  这个按钮只会写 `default_plan` ✓（链的第一档 ✓），留着只会让人以为有两套默认 ✗；
- **标题改名**：`团队套餐（provider + key + 名字）` → **`套餐（provider + key + 名字）`** ✓ ——
  表里本来就同时有团队套餐（`范围=团队`）和个人套餐（`范围=个人:alice`）✓；
  下方补一句「范围是团队或个人的说明」✓，创建表单的"可见用户"也加了提示 ✓
  （留空=团队；填用户名=个人 ✓）；
- **新增「改 key」** ✓：以前只有 CLI 能换（`dsh-muctl plan key <id>` ✓），后台没入口 ✗ ——
  现在每行一个按钮 ✓，`POST /api/plans/<id>/key` ✓，写回 0600 文件 ✓ 并立即 `sync_all` ✓；
- **新增「改范围」** ✓：团队套餐的 `visible_to` 以前建完改不了 ✗ ——
  现在 `POST /api/plans/<id>/scope` ✓，留空/`*`=团队 ✓，填用户名=只有这些人 ✓（会校验用户存在 ✓）；
- **模型选择表的列宽重排**：`模型 id` 34% 且**不换行** ✓、推理等级改成**小徽章**（原来
  `off / low / high` 挤成一行 ✓）、勾选/单选列收窄 ✓、弹窗在打表时加宽到 **940px** ✓，关闭后还原 ✓。

## 0.17.1（修：默认模型链保存后丢档）

- 0.17.0 的链设置**写得进去、读不回来** ✗：`default_model_chain` 没进配置字段白名单 ✗ →
  `save_config()` 只序列化声明过的字段 ✓ → 这一项被静默丢弃 ✗（`default list` 说"没设置"✓，API 返回 `[]` ✗）；
- 已把 `DEFAULT_MODEL_CHAIN` 登记为正式配置项 ✓（默认空 ✓，`dsh-muctl config` 里可见 ✓）。
- 实测（0.17.1，两档链 `[myog/glm-5.3] → [kilo-free/默认]`）：
  - `alice`（能看到 `plan_8f7c3b30`）→ `agent-default-model` = `plan-8f7c3b30-1` / `glm-5.3` ✓
  - `bob`（看不到它）→ 顺延到 `plan-kilo-free` / `kilo-auto/free` ✓
  - 链里填目录里不存在的模型 → 400 `plan … has no model …` ✓
  - 恢复单档后两个实例都回到 `kilo-auto/free` ✓

## 0.17.0（默认模型改成「按优先级链」，创建套餐表单去掉两个默认项）

- **去掉**创建套餐表单里的「默认模型」输入和「设为平台默认套餐」勾选 ✓ ——
  它们混在"创建"里确实说不清层级 ✓；
- 新增独立栏目 **「默认模型（按优先级）」** ✓：一行一档 ✓，可增/删/上移/下移 ✓，
  每档选**套餐 + 模型**（模型候选 = 该套餐实际生效的模型 ✓）；
- **为什么是链**：每个人能看到的套餐不同 ✓ —— 新会话取**第一条该用户可见**的档 ✓；
  一档用不上（套餐看不到 / 模型不在可用集合）就顺延 ✓；全都不行才回落到"第一个可见套餐" ✓；
- `default_plan` 仍保留 ✓（抽屉的默认徽章 ✓），它恒等于链的第一档 ✓；
- 新增 CLI：`dsh-muctl default list|add|set|rm` ✓；API：`GET/POST /api/default-chain` ✓；
- **新增 CLI 闸门**（`tests/cli-contract.sh`）：遍历**全部 26 个子命令**做 `--help` ✓，
  任何子命令的 parser 构建失败就 FAIL ✓ —— 专治"加了个重复参数导致整个子命令失效"✗
  （0.16.6 的 `--model` 冲突就是这么炸的 ✗）。

## 0.16.5（套餐的「默认模型」可以改了 + 把「设为默认」说清楚）

- **补缺口**：套餐原来只能在创建时填 `default_model` ✗，建完就改不了 ✗ ——
  而「默认套餐 + 默认模型」正是**新会话的起始模型** ✓（实例 patch 里的 `agent-default-model` ✓）。
  现在：「编辑模型」弹窗里每行多一个**默认单选框** ✓，保存时一起提交 ✓；
  命令行 `dsh-muctl plan default-model <plan-id> <model-id>` ✓；
  API `POST /api/plans/<id>/models` 接受 `default_model` ✓（用户改自己的套餐同样支持 ✓）。
- 套餐表的模型列会显示「…，新会话默认 `xxx`」✓，选错能一眼看出来。
- 创建套餐里的勾选框改名为 **「设为平台默认套餐」** ✓，并注明"决定新会话的起始模型，
  以及新用户首次进入时预选哪个套餐" ✓ —— 之前只写"设为默认"，看不出是哪个层级 ✓。
- **渲染兜底**：套餐的 `default_model` 若不在该套餐实际提供的模型里（例如后来缩小了子集），
  `agent-default-model` 会**回落到第一个可用模型** ✓ 而不是指向一个不在 patch 里的模型 ✓。
- 修一个我自己引入的**严重回归**：给 `plan` 加了 `--model`，而**它本来就有** ✗ →
  argparse 报 `conflicting option string: --model` ✗ → **整个 `plan` 命令全部失效** ✗。
  已删除重复参数 ✓（`plan default-model <id> --model <m>` 用原有的那个 ✓）。
- 文档补充三条语义（会话级记忆）：
  1. **DSH 按会话记住最后用的模型** ✓（会话投影 `modelSelection.lastUsed`，实测
     `{"provider":"plan-d523574a","model":"deepseek-flash"}` ✓）；
  2. **新会话**没有 `lastUsed` ✓ → 回到 `agent-default-model` = 默认套餐 + 它的 `default_model` ✓；
  3. 所以"记住最后选择"是**同一个会话内**的事 ✓，不是跨会话 ✓ —— 之前我表述含糊 ✓。

## 0.16.4（模型接入页：模板库改成按需弹窗）

- **「读取线上模板列表」不再常驻** ✗ → 和「＋ 导入 provider（JSON）」并排成一个按钮行 ✓；
  点按钮才弹窗、才发请求 ✓ —— 打开页面/切标签页**不再自动拉模板** ✓（之前切过来就请求 ✗）；
- 弹窗里有**加载动画**（转圈 + "正在从模板库读取…（首次可能要几秒）"）✓；
  导入某个模板时把列表换成"正在导入 X…"✓，失败则恢复列表并报错 ✓ —— 不再是一个空框干等 ✗；
- 「配置规范 / 给 AI 的提示词」那块提示**移到按钮行下面** ✓，模板说明也归到一行 ✓；
- **团队套餐**改成和其它页一致：**标题单独一行** ✓，下面才是「＋ 创建套餐」按钮行 ✓（之前标题和按钮挤一行 ✗）；
- `.spinner` 加入 ui-lint 的布局类检查（现 10 项）✓。

## 0.16.3（修：`.row` 从未定义 —— 所有"工具条"都在竖着排）

- 页面里 `class="row"` 用了 **8 处**，但样式表里**根本没有 `.row` 规则** ✗ ——
  所以那些本该"一行"的工具条（实例日志的 实例/行数/实时刷新/读取、各页的创建按钮行…）
  实际是**块级元素竖排** ✗，这就是截图上"表头一行一个控件"的原因 ✓；
- 现在补上 `.row{display:flex;flex-wrap:wrap;align-items:center;gap:10px}` ✓ +
  `.row > label{margin:0}` ✓ + `.inline{display:flex;flex-direction:row;align-items:center;gap:6px}` ✓
  （带复选框的标签按一行排 ✓）；
- 实例日志工具条重排：实例下拉 240px、行数 120px、复选框与「读取」同一行 ✓，
  右侧 `#logMeta` 用 `margin-left:auto` 顶到最右 ✓；
- `ui-lint` 新增一项闸门（现 10 项）：**布局类（row/inline/card/msg/muted/banner）必须各自有
  独立的 `.cls{` 规则** ✓ —— 已做反向验证（删掉 `.row{` 立刻 FAIL ✓）。

## 0.16.2（实例页重排：日志独立成页、挂载去卡片）

- **实例日志**从「实例」页底部的卡片**独立成一个标签页** ✓（导航：概览/用户/实例/**实例日志**/模型接入/…）；
- 实例列表里的 `logs` 按钮改成 **`gotoLogs(name)`** ✓：切到「实例日志」页 ✓、自动选中该实例 ✓、立即读取 ✓
  （之前是往页面底部的 `#instLogs` 里塞一段 `<h3>+<pre>` ✗，位置和样式都很怪 ✗）；
- 日志工具条重排成**一行**：实例 / 行数 / 实时刷新（2 秒） / 读取 / 说明 ✓；
  右侧 `#logMeta` 显示 `实例 · 文件 · 来源(live|rotated|empty) · 时间 · 行数` ✓；
  日志区 `max-height:60vh`、`word-break`、等宽行高 ✓，长 URL 不再把页面撑宽；
- **宿主目录挂载去掉外层卡片** ✓ —— 之前是「卡片套在页面里」✓，现在只是一个标题 + 按钮 + 表格 ✓；
- 顺带修回一处误删：`实例的 provider / key` 表格（`#provAssign`）在重构时被删掉 ✗，已恢复 ✓。

## 0.16.0（系统账号保护 + 角色语义澄清）

- **修危险缺陷**：后台用户表对 `root` 也显示了「停用 / 改密 / 删除」✗，而这三条**真的会执行** ✗：
  - 改密对它**无效**（root 认证的是宿主机 `/etc/shadow` 口令 ✓）；
  - 停用/删除会**把 root 锁在网页外** ✗（登录走"管理员记录缺失"的 503 ✗，只能回 shell 修 ✓）。
  现在：CLI（`user passwd/toggle/del`）与后台 API 都**拒绝系统账号** ✓，
  并给出替代做法（`passwd root` / 从 `ADMIN_SYSTEM_USERS` 移除 ✓）；页面上这三个按钮对系统账号**不再显示** ✓
  （只保留「踢下线」✓，并在名字旁标「系统账号」✓）。
- **澄清一个误导**：创建用户时的「角色」下拉原本可以选 `admin` ✗ ——
  但网关在登录时**强制把平台账号的 role 改成 `user`** ✓（`login.lua` 注释明说"admin is locked to the
  configured system accounts" ✓）。所以那个 `admin` 选项**从来不会带来管理员权限** ✗
  → 下拉现在只有「普通用户」✓，并注明"管理员只能是宿主机账号（root 或 ADMIN_SYSTEM_USERS）"✓。
- 结论（写进代码注释与文档）：**平台账号可以拥有实例** ✓（`--instance auto` 会建 ✓），
  但**永远不是管理员** ✓；管理员身份只来自宿主机账号 ✓，登录后默认落到 `/admin/` ✓。

## 0.15.11（让浏览器别再弹"生成/管理密码"）

- 弄清了一件事：Firefox/Chrome 那个气泡**不是判定"这是登录表单"** ✗ ——
  而是**只要字段是 `type="password"`** 就会提供「生成安全密码 / 管理密码」✓，网页侧关不掉 ✗；
- 所以创建用户表单里的密码字段**改成 `type="text"`** ✓（`autocomplete="off"` + `data-lpignore`），
  气泡彻底消失 ✓；旁边加了 **「随机生成」** 按钮 ✓（16 位、去掉易混字符）✓，
  并注明"明文输入，可以直接看到并转交用户" ✓ —— 对管理员建号这个场景反而更实用 ✓；
- 密码仍是"留空 = 服务端随机生成" ✓，行为不变 ✓；
- **团队套餐的 key 仍然保持 `type="password"`** ✓ —— 那是凭据不是密码 ✓，
  只补了 `autocomplete="new-password"` / `data-lpignore` 降低被误判的概率 ✓。

## 0.15.10（给浏览器的密码气泡留位置）

- 图1/图2 里那个「管理密码 / 使用生成的安全密码」是 **Firefox 自己的 UI** ✓（点开是 `about:logins`），
  网页既不能关闭它、也不能定义它的位置 ✗ —— 但可以**避免它盖住别的字段** ✓；
- 创建用户表单里把「密码」**挪到最后一行并占满整行**，下方留 84px 空白 ✓；
  于是气泡向下展开时落在**空白区**里 ✓，不再压住「实例」下拉 ✓；
- 该字段仍是 `autocomplete="new-password"` + `data-lpignore` ✓（降低被当成登录表单的概率）；
- 其余排版不动（0.15.9 的两列栅格）。

## 0.15.9（弹窗表单的排版与去重）

- **去重**：表单自带的「创建 X」提交按钮在弹窗里隐藏 ✓ —— 底部只留一个按钮 ✓，
  而且它会**沿用表单自己的动词**（"创建用户"/"创建套餐"/"导入"…）✓，不再是笼统的"确认创建"；
- **排版**：弹窗表单改成**两列栅格**（长字段如 JSON textarea 自动占满一行）✓，
  「设为默认」这类复选框改成**文字+方框同一行** ✓（之前它被挤到中间 ✗）；
- 弹窗宽度 620 → **760px** ✓，高度上限 86vh ✓，打开时自动回到顶部 ✓；
- **浏览器的"管理密码"气泡**：给创建表单加了 `autocomplete="off"` + `data-lpignore`/`data-1p-ignore`，
  用户名/密码字段各自标了 `autocomplete` ✓ —— 这个气泡是 Chrome 的登录表单启发式弹的，
  我们能做的是降低误判概率（无法 100% 关闭浏览器的 UI）；
- 弹窗内的表单去掉卡片外观，标签统一 12px 次要色、输入框 100% 宽 ✓。

## 0.15.8（修：创建按钮旁边还多出来一份表单）

- `form{display:flex}` 这条元素选择器**盖过了** `[hidden]` 的 UA 规则 ✗，
  于是六个"创建"表单虽然加了 `hidden` 却照旧显示 ✗ → 页面上看起来是"双份添加"；
- 加了一条 `[hidden]{display:none!important}` ✓；顺带把弹窗里的表单**去掉卡片外观**
  （`#admin-modal form`）✓ —— 否则弹窗里会套一层圆角边框 ✓；
- `ui-lint` 新增一项闸门（现 9 项）：只要页面用了 `hidden` 属性，
  就必须存在 `[hidden]{display:none!important}` 规则 ✓ —— 已做反向验证（改成 block 立刻 FAIL ✓）。

## 0.15.7（后台：创建类操作统一成「按钮 → 弹窗 → 确认」）

- 「用户 / 实例 / 宿主挂载 / provider 导入 / 团队套餐 / 插件清单」六处原本常驻的内联表单，
  全部改成**一个创建按钮**；点开后表单出现在弹窗里，点「确认创建」才提交；
- 实现方式是**移动表单节点**（不是复制），所以原有 submit 逻辑与校验一字未改；
- 提交成功后自动关闭弹窗 ✓；取消 / 点遮罩 / 右上角 × 都会把表单**放回原位**并清理弹窗 ✓；
- 顺手修掉实例表单里**重复的「端口」字段**（两个 `name="port"`，后一个会覆盖前一个）；
- 弹窗复用了模型选择器那一个（`#admin-modal`）：打开创建表单时隐藏「全选可用/全不选」，
  关闭时恢复它们与按钮文案 ✓。

## 0.15.6（web_search：没有可用 provider 时把工具藏起来）

- 0.15.5 只是把报错换成"registered but unavailable" ✗ —— 实测模型**照样会调**，
  于是用户看到 `DeepSeek search has no API key for "DSHMU_WEB_SEARCH_UNCONFIGURED"` ✗ 红字；
- 0.15.6 再补一刀：没有官方 DeepSeek 套餐时，额外渲染
  `tool-web: { search: false }` ✓（`dsh-tool-web` 的 schema 是 `search` + `fetch` 两个独立开关），
  于是 `web_search` **不出现在工具列表里** ✓，`web_fetch` 保留 ✓，用户不再看到任何搜索报错 ✓；
- 附带两条实测结论：
  - **`web_fetch` 的 `TypeError: fetch failed` 不是网络问题**：同一个 URL 用裸 node `fetch`
    返回 200（baidu / ithome / chinanews 都 200，只有 BBC 是 `UND_ERR_CONNECT_TIMEOUT`）；
    DSH 的 fetch provider 走 undici + 代理策略 + `redirect: manual`，失败点在插件侧，
    绕过办法是在实例里用 `curl`（模型自己也这么做了）；
  - 实例 jail 里**没有 `python3`**，只有 node —— 这是设计如此，不是缺依赖。

## 0.15.5（web_search：注册但不可用，而不是禁用）

- 0.15.4 把 `web-search-deepseek` **禁用**了 ✗ → 但 `dsh-web` 的 `searchProvider` 仍然指着它 ✓，
  于是工具报 `configured web provider "deepseek-official" is not registered` ✗（比原来更难懂）；
- 现在没有 DeepSeek 套餐时改成**注册但不可用**：`apiKeyEnv: DSHMU_WEB_SEARCH_UNCONFIGURED`
  （没人设这个变量）→ DSH 报 `… is registered but unavailable` ✓ —— 最准确、可执行；
- 有官方 `api.deepseek.com` 套餐时仍自动接 relay（真 key 由 relay 注入）；
  `web_search_plan` 显式指定的兼容套餐优先；
- 附带结论：`web_fetch` 在 jail 里是好的（node 直连 200 ✓），之前失败是个别站点；
  jail 里没有 `python3`（只有 node）——这是设计如此，模型该用 node 或 curl。

## 0.15.4（web_search：只自动认官方 DeepSeek，其它必须显式指定）

- 0.15.3 会退而求其次挑一个 `anthropic-messages` 的套餐 —— 实测又错了：
  opencode-go 的 anthropic 路由返回 **403 `error code: 1010`**（Cloudflare），
  和 Kilo 的 401 `You need to sign in to use this model` 一样属于误导性报错；
- 现在**自动只用 base_url 是 `api.deepseek.com` 的套餐**；其它供应商一律 `disabled: true`，
  除非运维用 `web_search_plan` **显式指定**（且协议必须是 `anthropic-messages`，否则仍禁用）；
- 四种组合已单测：只有 kilo+opencode-go → disabled；加官方 deepseek → 用它；
  显式指定 opencode 的 anthropic 路由 → 用它；显式指定 kilo → disabled。

## 0.15.3（修：web_search 选到了协议不兼容的套餐）

- 0.15.2 的兜底是"挑不到 deepseek 套餐就用第一个套餐"，于是只有 Kilo 套餐的机器会把
  `web-search-deepseek.baseURL` 指向 **Kilo**（OpenAI 协议）→ 必然失败；
- 现在**按协议挑**：只考虑 `anthropic-messages` 的套餐（优先 `api.deepseek.com`），
  一个都没有就 **`disabled: true`**（干净地"未配置"，不再产生 401/乱七八糟的错）；
- 显式 `web_search_plan` 指向不兼容套餐时同样**禁用**，而不是硬指；
- `docs/11` §9 改写选择规则，并给出"只有 Kilo 时如何补一个 DeepSeek 套餐"的命令。

## 0.15.2（修：web_search 拿占位符直连 DeepSeek 导致 401）

- **现象**：实例里调用 `web_search` 报 `WEB_PROVIDER_ERROR` /
  `DeepSeek API error (HTTP 401): your api key: ****elay is invalid`；
- **原因**：`web-search-deepseek` 插件有自己的 `baseURL` + `apiKeyEnv`（默认 `DEEPSEEK_API_KEY`），
  它不走 relay、直连 `api.deepseek.com`，而实例里的 `DEEPSEEK_API_KEY` 是占位符 `mu-relay`；
- **修法**：`plans_patch_yaml` 现在同时渲染 `web-search-deepseek`：
  relay 模式下 `baseURL = http://127.0.0.1:<relay>/mu-llm/<token>/<route>/v1` + 占位符凭据
  （真 key 由 relay 注入）；无 relay 时用真实端点 + 套餐凭据；没有可用套餐则显式 `disabled: true`；
- 新增配置项 `web_search_plan`（留空=自动挑 deepseek 系套餐）；
- 实测：直连 relay 的 `/v1/messages`（只带占位符）返回 200；
- 文档 `docs/11` 新增第 9 节说明原因与两种模式的渲染结果。

## 0.15.1（修：apk 不能从 URL 安装）

- **实测纠正**：`apk add --allow-untrusted <URL>` 在 Alpine 3.24 / apk-tools 3.0.6 上**不支持** ✗
  —— apk 只接受**本地文件**或**仓库包名**；
- README / `docs/05` / Release 说明全部改成 **`wget` 下载 + `apk add ./本地文件`** 两步，
  并写明"apk add <URL> 不行"以及可选校验（`SHA256SUMS`）；
- CI 里那段冒烟测试本来就是装**本地文件** ✓，不受影响；
- `doc-lint` 新增一项闸门（现 22 项）：**任何文档里出现 `apk add … http(s)://` 就 FAIL**，
  已做反向验证（故意写错 → 立刻 FAIL）。

## 0.15.0（打 tag 自动发 Release，支持一行安装）

- 新增 `.github/workflows/release.yml`：推送 `v*` tag（或手动指定版本）即自动打包并发布 Release；
- **latest 的关键**：每个 Release 同时上传**固定名字** `dsh-mu.apk`（+ 版本化名字 + `SHA256SUMS`），
  并显式 `make_latest: true` —— 于是
  `https://github.com/dainan13/dsh-mu/releases/latest/download/dsh-mu.apk` 成为**永久不变**的安装地址；
- 手动触发时可选 **prerelease**：标记为预览版后，GitHub 的 `latest` 会**跳过**它（这正是发预览版的正确姿势）；
- CI 门：tag 必须等于 `VERSION` → `doc-lint`/`ui-lint` → 在 **Alpine 3.24** 里构建
  （`apk mkpkg` 只在 apk-tools 3 里有）→ 在**干净** Alpine 容器里**真的 `apk add` 装一遍**并校验
  `dsh-muctl --version`、三个二进制、admin 页面与 tests 都在位 → 才发布；
- Release 说明自动取 `CHANGELOG.md` 里对应版本的段落，并附一行安装命令；
- README 安装章节改为"一行安装（推荐）"，并给出固定版本、离线机器两种替代写法；
- `docs/05` 新增第 0 节：latest 的三个必要条件、资产清单、CI 门、以及"包未签名"的已知限制与签名路径。

## 0.14.0（从线上模板库导入 provider）

- 新增 `dsh-muctl template list|import|source`：从项目仓库的 `provider-packages/` 拉取模板并导入；
- 后台「模型接入」页新增 **从线上模板库导入** 卡片：列表（已导入/未导入）、一键导入、覆盖需确认；
- 模板先经 `validate_provider_package()` 校验再落盘；只允许 `https://` / `file://`；
  不覆盖已有 provider（除非 `--force`）、不自动建套餐、模板名防路径穿越；
- 仓库新增 `provider-packages/index.json`（模板清单；没有 index 时回落到 GitHub contents API）；
- 模板库地址可配：`provider_template_base`（`dsh-muctl config` / `template source --url`），
  支持 `file://` 本地目录，离线机器也能用；
- 新增回归层 `tests/provider-templates.py`（**完全离线**，用临时 `file://` 目录）；
  回归层数 32 → 33；
- 审计记录 `provider.template.import`（含 `sha256`）。

## 0.13.2（日志视图：空日志不再只是「(空)」）

- 实例日志接口与页面现在**解释**为什么是空的，而不是只回一个 `(空)`：
  返回 `source`（live / rotated / empty / missing）、`file`、`size`、`mtime`、`note`；
- **回落**：实时日志为 0 字节（`logrotate` 用 copytruncate，服务持有描述符）时，
  自动读 `.1` / `.2` / `.gz` 里最近的内容，并在页面顶部注明"当前日志为空：已轮转，下面是 … 的最后 N 行"；
- 真的没有任何输出时，页面显示文件路径、字节数、最后修改时间和原因，而不是一个空框；
- 日志里的**启动 token 会被打码**（`token=***`）：文件仍是 0600 root，接口不再把一次性登录令牌递给看日志的人。

## 0.13.1（修：抽屉加载失败 — cjson 空数组变成 {}）

- **修 `加载失败: (p.models || []).filter is not a function`**：网关用 Lua 的 cjson 编码，
  **空的 Lua table 会被编码成 `{}` 而不是 `[]`**，于是「模型子集为空」的套餐到浏览器就变成对象，
  `.filter` 直接不存在，整个抽屉白屏；
- `me.lua` 现在对 `plans[].models / effective_models / available_models` 以及
  `plans / providers / installed / pluginCatalog` 统一走 `as_array()`（空表 → `[]`）；
  `available_models` 的 **null 语义保留**（= 从未检测过）；
- **`/me` 里的 `instance.resources` 修好**：之前从 `upstreams.json` 取（那是 sync 时写的，没有实时数字），
  现在每次请求向后台取一次，抽屉里的内存/CPU/进程数才是活的；
- 抽屉**逐卡片容错**：某一个卡片渲染失败只提示一条，不再连累整个面板；
  `planRow` / 模型选择器对列表字段做 `Array.isArray` 兜底；
- **新增回归闸门**（`tests/user-panel.py`）：断言 `/me` 的 `plans[]` 各列表字段必须是 JSON 数组
  （空的也算），且 `instance.resources.mem_used` 存在 —— 这个 bug 以后会被测试直接抓住。

## 0.13.0（收口轮：日志实时刷新 / 会话空闲 24h / 资源占用）

- **实例日志实时刷新**：后台「实例日志」新增「实时刷新」开关，2 秒轮询；标签页不可见时暂停；
  读取失败自动关闭轮询；滚动位置跟随（停在底部才自动滚，翻上去看历史时不会被打断）；
- **会话空闲过期默认 24h**：`SESSION_IDLE_MINUTES` 出厂默认改为 **1440**，设 0 可关闭；
  实测把窗口临时设为 1 分钟，登录后静默 75 秒访问 `/me` → **401**（会话已失效）；
- **用户面板资源占用**：抽屉「我的实例」显示 内存（已用/上限 + 百分比）· CPU% · 进程数；
  数据来自 cgroup v2（`memory.current/max`、`cpu.stat`、`pids.current/max`），CPU% 用两次采样差值；
  **只在抽屉打开时** 5 秒刷新一次，关掉即停；
- 文档：`docs/04` 与 `docs/07` 追溯表更新为 **27 ✅ / 7 🟡 / 0 ⛔**（FR-1.3 会话空闲过期转为已实测），
  `docs/07` 的"后续可选工作"标掉已完成项（HTTPS / 挂载 / 推理档位 / Kilo / 日志刷新 / 资源占用）；
- `docs/06-运维手册` 新增三个开关的用法与复现步骤。

## 0.12.0（宿主目录挂载管理）

- 后台「实例」页新增 **宿主目录挂载** 卡片：选实例、填宿主路径 / jail 路径 / 只读或读写，
  表格里能看到每条的状态（宿主路径是否还存在）并可移除；
- 新增 CLI：`dsh-muctl mount list|add|rm`（`--instance` / `--all` / `--rw`）；
- 新增 API：`GET/POST/DELETE /api/mounts`（管理员）；
- **安全校验**：平台内部路径（`/proc` `/sys` `/dev` `/etc` `/root` `/usr` `/var/lib/dsh-mu` …）、
  其他实例的 jail 目录、jail 内的 `/`、`/proc`…、`/.dsh*`、`/usr/local/lib*`、不存在的宿主路径，全部拒绝；
- 默认只读，必须显式 `--rw` / 「读写」才可写；
- doctor 为每条挂载输出一行（宿主路径消失 → WARN）；
- 实测：只读挂载写入报 `Read-only file system`，读写挂载双向生效，宿主侧能收到 jail 写入的文件；
- 新增文档 `docs/17-宿主目录挂载.md`。

## 0.11.4（修：控制台的 manifest 语法错误）

- 网关现在直接返回一个**合法的 PWA manifest**（`/manifest.webmanifest`，
  `application/manifest+json`），不再把它 302 到登录页；
- 之前 DSH 页面里根相对引用的 manifest 落到了鉴权跳转上，浏览器把 HTML 当 JSON 解析，
  控制台就会报 `Manifest: Line: 1, column: 1, Syntax error`；
- 该路径在鉴权之前放行（内容公开、极小），http 与 https 都验过 200 + 合法 JSON。

## 0.11.3（修：keyless 套餐 + 抽屉消失）

- **修 `No API key for provider: plan-kilo-free`**：DSH 选模型时要求 provider 有一个 key，
  所以 keyless 套餐改走"实例拿 relay 占位符、relay 决定不发 Authorization"的同一条路径；
  无 relay 时才真的不写 apiKeyEnv；
- **修抽屉消失**：`panelAction` 里用了 `await` 却没声明 `async`，导致整份 panel.js
  在浏览器里解析失败（SyntaxError），整个抽屉不渲染；
- **加回归闸门**：`tests/ui-lint.mjs` 现在会用 `node --check` 按"经典脚本"解析 panel.js，
  并已验证它能复现该错误（同一条 384 行）。

## 0.11.2（开箱即用的默认套餐：Kilo Free）

- 新增内置 provider `kilo`：`https://api.kilo.ai/api/gateway`，模型 `kilo-auto/free`；
- **无需 API Key**（pi-ai 的 keyless profile；平台三处配合保证不发出任何 Authorization）；
- `install` 自动创建团队套餐 **Kilo Free（默认）** 并设为 `default_plan` —— 装完即可对话；
- 该套餐带实测过的推理档位 `off/low/medium/high/max`（`off` 映射到线上写法 `none`）；
- 实测：tools ✓、SSE 流式 ✓、经 relay 200 ✓（首次冷启动较慢，之后约 6 秒）；
- 文档 `docs/11` 新增第 7 节，写明 keyless 的三处配合与"第三方公共服务"的风险提示。

## 0.10.7（DeepSeek 官方推理等级，随安装包内置）

- `deepseek` 内置 preset 现在带**实测过的推理档位**：`off/low/medium/high/max`；
- 模型补全为官方 `/models` 的两个 id：**deepseek-flash**、**deepseek-v4-pro**；
- 档位→线上写法的映射按 DeepSeek 的真实枚举：`off: none`（DeepSeek 没有 `off`，发 `off` 会 422），
  `low/medium/high/max` 原样透传为 `output_config.effort`；
- 实测依据（2026-10-20，官方 anthropic 端点）：
  `none` → 200 且响应只有 text（思考关闭）；`minimal/low/medium/high/max` → 200 且带 thinking 块。

## 0.10.3（推理等级）

- 模型项支持 **`reasoningEfforts`**（pi-ai 原生字段）：键=选择器里的等级，值=发给上游的写法；
- 模型行新增第 5 段：`id | 名称 | contextWindow | maxTokens | off,high`
  （支持 `high:ultra` 改写法、`false` 声明非推理、留空沿用目录能力）；
- 该字段从 provider 目录 → 套餐 → 实例 patch 全程透传（修掉了 `plan_routes` 丢弃它的问题）；
- 后台/抽屉的「编辑模型」表格新增 **推理等级** 列（`off / high`、`非推理`）；
- 内置 preset（deepseek / opencode*）的定义在代码里，想给它们加等级需先 `provider import` 成自定义包；


## 0.10.2（消息不再顶动界面）

- 后台的所有提示改成**固定在页面底部中央的浮层**（`position:fixed`，不占文档流）：
  出现和消失**不再让整页上下跳**，按钮位置保持不动；
- 浮层可**点击关闭**、最多叠 4 条、6 秒自动消失；
- 修掉一个连带 bug：旧实现用 `setTimeout(() => flash(''), 6000)`，
  较晚出现的消息会被**较早的计时器**一起清掉；现在每个提示只清理自己那个节点；
- 表单内提示（`.msg`）与 provider 测试结果（`#provResult`）**预留固定高度**，
  文字出现/消失不再挤动下方按钮；
- 抽屉的「有改动要重启」提示改成**占位但隐藏**（`visibility`），
  所以它出现时「启动 / 重启」按钮也不会位移。

## 0.10.1（模型编辑与检测的可感知性）

- 「编辑模型」改为**表格**：勾选 / 模型 id / 名称 / **该 key 是否可用** / 状态；不可用的行标灰且不可勾；
- 表格头部显示**目录数 · 已选数 · 可用数 · 上次检测时间**，并单列一行"上次检测的变化"；
- 「检测可用模型」给出**差异**而不是只有一个数字：新增可用 / 失去可用 / 目录缺口 / 无变化；
- 抽屉里用户自己的套餐也有「检测」按钮，结果用弹窗展示（不再只有一句 toast）；
- 后端把每次探测记进 `plan.last_probe`（时间、前后数量、added / lost / unknown），
  所以刷新页面之后仍然看得到"上次检测的结果"。

## 0.9.9（模型接入页重排：provider 是目录，套餐才是可用性）

- **provider 只维护全量目录**：移除了「刷新模型 / 查配额」按钮（能力保留在套餐侧）；
- **套餐负责"这个 key 能用什么"**：`plan probe` 用该 key 实测 → 存 `available_models`；
  `plan models --set/--all/--clear` 编辑子集；`plan usage` 查配额；
- 渲染进实例的模型 = **已选 ∩ 可用**，不可用的**丢弃**并在界面标灰提示（不再猜、不再 401）；
- 探测会把"上游有、目录没有"的模型列出来（提示先补进 provider 目录）；
- 抽屉里用户可编辑**自己**套餐的模型；管理界面可编辑**团队**套餐；
- 改动需重启实例生效 —— 抽屉会亮「有改动要重启」，管理界面提示受影响实例。

## 0.9.4（HTTPS）

- 新增 `dsh-muctl tls`：`status` / `self-sign` / `import` / `enable` / `disable` / `redirect` / `keep-http`；
- 开启后网关同时监听 443（`ssl_protocols TLSv1.2 TLSv1.3`），证书只以 **ro** 方式进入网关 jail；
- 80/443 关系可配：默认两者都服务（http 也能登录）；`tls redirect on` 后 80 全量 301，并给会话 cookie 加 `Secure`；
- 自签证书自动把主机名与本机 IPv4 写入 SAN；也支持导入正式 CA 的 fullchain + key；
- `doctor` 新增三项：证书可读性、到期天数（<14 天 WARN）、443 是否监听；
- 新文档 `docs/16-HTTPS.md`；打包依赖新增 `openssl`。

## 0.9.2（实例内命令与搜索可用）

### 修复：`spawn bash ENOENT` / `ripgrep provider failure`（长期未解的那个）

**根因**：jail 自建的 `/dev` tmpfs（只有手工 `mknod` 的 null/zero/random/…）缺少
DSH subprocess runner 需要的完整设备视图（`/dev/pts`、`/dev/fd` 等），runner 起不来；
DSH 把它统一包装成 `SubprocessExecutableNotFoundError`，界面显示 `spawn bash ENOENT`
——这个错误名一直在误导排障方向（bash 其实存在、可执行、能 spawn）。

**修法**：`jail/dsh-mu-jail` 把 `/dev` 从"私有 tmpfs + 手工 mknod"改为**只读 bind 宿主 `/dev`**。
只读 bind 保留了真正需要的安全性质（实例无法 unlink/替换宿主设备节点），同时所有节点可解析。

**验证**：真实 jail 内 `LocalSubprocessRuntime.spawn(['bash',…])` 返回 `{"exitCode":0}`；
定位用的最小复现脚本随包分发：`tests/dsh-subprocess-repro.mjs`（详见 docs/15）。

### 定位方法（记下来，避免下次再绕）

用 Proxy 伪造 ctx 直接实例化 DSH 的 `LocalSubprocessRuntime` 做最小复现，
拿到"网关 rootfs 成功 / 实例 jail 失败"的对照，再把 jail 的特性逐项加回二分：
rootfs、`unshare -m`、`--no-new-privs`、能力集、procfs、`/proc/sys`、运行时 ro/rw、
self-bind、cgroup —— 全部排除后，**私有 `/dev` tmpfs** 一项即复现。

## 0.8.1（首个正式版）

**全量回归 `PASS=32 FAIL=0`，`doctor: 38 PASS, 0 WARN, 0 FAIL`。**

### 实例隔离与运行时
- 运行时（DSH + node/npm）整体搬进网关模板，**宿主机不再需要 node**：实例只读 bind 挂载规范路径
  `/usr/local/lib/node_modules/@deepseek-ai/dsh`；
- jail 的 `/` 先自 bind 成挂载点（bwrap 需要），`/proc/sys` 只读 bind 进 jail，实例保留
  `sys_admin,setuid,setgid` 三个 capability —— 实测 `bwrap --dev-bind / / echo` 成功（`BW-OK`）；
- 隔离仍成立：宿主数据目录在 jail 内不可见、`/proc/<host-pid>/root` 不可达、mknod/写 sysctl 被拒；
- `cgroup2` 缺失时 `install` / `doctor --fix` 会自动挂载（新装 Alpine 的 VM 不再红 3 项）。

### 模型接入
- 模型接入改为**配置文件**（导入/导出 + 可被 AI 改写的 spec + 下载为文件）；
- 套餐 = provider + key + 显示名，选择器里分组名即套餐名；团队套餐强制可见、用户不可删；
- 每套餐模型子集、默认套餐、图片模态（pi-ai 的 `input` 字段）；
- **relay**：实例只持有占位符 `mu-relay`，真 key 在网关 jail 内的 `relay.json`（实例 `grep -c sk-` = 0）。

### 用户抽屉
- 账号操作集中到页头（修改密码 / 退出登录）；我的实例（启动 / 重启 / 改动待重启提示）；
- 我的套餐（两行省略 + 默认标记 + 清单内/自装）；我的插件（含管理员密码升级安装）；
- 统一一个模态框，长名称一律省略不破版。

### 管理与运维
- 后台 8 个 tab：概览 / 用户 / 实例 / 模型接入 / 插件 / 平台 / 自检 / 审计；
- 插件：目录 + 抽屉安装/卸载 + 管理员密码升级 + `instance plugin replay`；
- 实例日志查看器；`plan key` 轮换；`session_idle_minutes` 空闲超时；
- 备份/恢复、审计 JSONL、日志轮转、`doctor` 自检与 `--fix`。

### 测试
- 回归 32 层全部通过；其中 4 层依赖 node —— 宿主无 node 时改为
  "bind 源码/测试进网关模板 + chroot 内用模板的 node 运行"，路径按 jail 内 `/tmp/...` 传入。

### 已知限制
- 实例内「执行命令 / 查找文件」不可用：jail 侧 bwrap 已可用，但 DSH 的 subprocess 层启动时
  fail-closed，需上游支持。对话、看图、文件读写、生成交付物、模型路由均正常。

## 0.1.0

### 平台化改造
- 配置外置到 `/etc/dsh-mu/dsh-mu.conf`（24 键，脚本可 source），旧的 `state.json.config` 自动迁移；
- CLI 拆成可导入库 `/usr/local/lib/dsh-mu/python/dshmuctl.py` + `/usr/local/sbin/dsh-muctl` shim；
- `state.json` 全程 `flock`（可重入）；Alpine 版本、community 仓库、npm registry 自动探测；
- 网关改为 **nginx 片段化安装**（`conf.d` + `http.d` + 受管 include，幂等、备份、`nginx -t` 失败自动回滚），
  不再覆盖 `nginx.conf`；自动识别并还原历史的"整文件覆盖"安装。

### 可运维性
- `dsh-muctl doctor`：30+ 项前置条件自检（随实例数增长）（内核/容器能力、cgroup 委派、apk keys、镜像可达、nginx 集成、
  运行态一致性、管理员账号、日志轮转），支持 `--json` 与 `--fix`；
- 网关**不再盲信 state.json**：转发前实测上游端口，失败时返回自有 503 页并可自动拉起实例；
- `dsh-muctl sync-state` 按端口/服务实测纠偏；状态判定服务感知（重启窗口不误报）；
- 日志轮转：`logrotate` drop-in（daily + maxsize 16M + copytruncate）+ 无依赖兜底 `logs rotate`。

### 管理界面
- `dsh-mud` 回环后端（JSON API + OpenRC 服务 + 共享令牌 + 审计 JSONL），直接复用 CLI 的库与状态锁；
- 网关 `/admin/` 路由（会话 + 系统管理员校验），管理页面六个标签：概览 / 用户 / 实例 / 模型接入 / 平台 / 自检 / 审计；
- **管理员 = 宿主系统账号**（默认 root，口令对 `/etc/shadow` 校验）；平台账号一律普通用户；
- 用户面板：左边缘角色图标 → 抽屉（实例信息、启动实例、改密、退出），Shadow DOM 注入，`/me` 提供数据；
- **登录即自动启动实例**（等待页 3 秒自刷新）；
- 实例限额人话化（核数 / MB / 进程数 / **不限**），运行中立即生效；实例**端口可视化修改**；
- Provider 目录可编辑（内置预设 + `/etc/dsh-mu/providers.json`），支持自定义 header、连通性探测（真发最小请求）、
  凭据优先级（实例库 → 平台 key）、`instance-patch` 查看最终配置。

### 修：包里漏了所有 `.py` 测试（drift 审计抓出来的）
- 改打包脚本时顺手把 `make-apk.sh` 的测试拷贝改成 `tests/*`（原来只有 `tests/*.mjs`、
  `tests/*.sh`），因为 `tests/drift-audit.py` 报出
  `tests/fresh-machine.py: missing at …/usr/local/lib/dsh-mu/tests/…`（共 10 个 `.py` 缺失）；
- 后果本来会很直接：**干净机器上装完 dsh-mu 后根本没有 `fresh-machine.py`**，
  目标 (4) 的自验收跑不起来；
- 同时把 `artifact-audit.py` 的承诺清单扩展到 `acceptance.sh`/`fresh-machine.py`/`api-contract.py`/
  `artifact-audit.py`/`drift-audit.py`，以后少一个就失败；
- 修复后 `drift audit` 显示 `checkout vs package (tests): all 34 files identical`（此前 13 个），
  全量回归 `PASS=31 FAIL=0`。

### P1 模型代理：key 只存在于网关
- 新增 loopback 代理 `127.0.0.1:8099/mu-llm/<实例token>/<route>/…`（网关 Lua `relay.lua`）：
  实例里只有占位符 `mu-relay`，真 key 由网关注入，`relay.json` 不挂进实例 jail；
- 按实例 token 校验 + 未知 token/route 一律 403；三种协议按 `bearer` / `x-api-key` 自动选头；
- 流式：`proxy_buffering off` + `Connection ""` + `gzip off`（实测 55 事件、首个分块 0 s）；
- 逃生开关 `RELAY_ENABLED=false` 一键回直连；`doctor` 增加「model relay」检查；
- 实测：deepseek 非流式 200（usage 35/16）、实例内 `sk-` 计数 0。

### 宿主去 node：DSH 运行时装进网关模板
- 网关模板新增 `nodejs/npm/pnpm`；`install_dsh_runtime()` 在网关 rootfs 里 `chroot npm i -g @deepseek-ai/dsh`
  （判断"已安装"只看网关模板那份，避免被宿主旧副本骗过）；
- 实例只读挂载**网关模板里的**运行时（`DSH_RUNTIME_DIR` 指向 gateway rootfs），一份包共享给所有实例；
- 实例启动命令改为 `exec node <runtime>/lib/bin.js …`（宿主不再有 `dsh` 二进制，PATH 里也没有）；
- apk 依赖与 `doctor` 的必需命令去掉 node/npm/pnpm，新增「shared dsh runtime」检查；
- `install` 顺序调整为：宿主包 → 实例模板 → 网关 jail → 运行时 → 重启网关。

### 抽屉/后台三件小改
- 抽屉：套餐行显示「默认」徽标（与实例实际生效的默认套餐一致）；
- 抽屉：装/卸插件、改套餐后出现「有改动要重启才生效 + 立即重启」提示条；
- 后台「插件」页新增「各实例已装」表（实例/插件/版本/来源/操作人）。

### 插件：清单 + 抽屉安装/卸载 + 管理员密码升级
- 一条规则：清单内免密安装；装清单外的包、卸载清单内的插件 → 需要**管理员密码**；
- 抽屉新增「我的插件」（列表/安装/卸载，需要密码时原地展开密码框）；后台新增「插件」tab（只有清单表，无开关）；
- 执行方式：实例内 `dsh plugin --profile <p> add|remove`，落在实例自己的 `~/.dsh/profiles/<p>`；
- 记账与重放：`state.json` 里记清单与每家装机记录（来源 curated/self），`instance plugin replay` 可重建后重放；
- 不启用 DSH 自带的 plugin-manager（会绕过清单、无归属记录）。

### 抽屉页头：账户动作上移
- 「修改密码 / 退出登录」移到页头（用户名/角色下方），不再单独占一张「账户」卡片；
- 页头只用一条分隔线，不套边框；系统账号在页头显示「系统账号口令」徽标。

### 抽屉改版：统一弹层 + 重启实例 + 长名字排版
- 新增「重启实例」：`dsh-mud` 的 `POST /api/me/instance/restart`（只允许动自己的实例，202 接受、写审计），
  经 `me.lua` 的 `restart` 动作暴露给抽屉；
- 修改密码、添加套餐、重命名、删除确认、重启确认**全部改用同一个弹层**，抽屉内不再平铺输入框；
- 「账户」卡片上移（账号/角色/口令 + 修改密码 + 退出登录）；
- 「我的套餐」行改成两行排版（套餐名 / `provider · N 个模型`），都单行省略，长名字不再撑歪。

### 实例模板补 ripgrep / procps
- harness 的 `glob`/`grep` 工具依赖 `rg`，缺了会报 `ripgrep provider failure`；
  实例模板默认包加 `ripgrep`、`procps`；已有实例可用 `chroot <rootfs> apk add` 现场补。

### 图片能力用对了字段：pi-ai 是 `input`，不是 `inputModalities`
- 查 `dsh-llm-pi-ai` 的 `PiAiModelProfile` 定案：模型条目字段是 `input: PiAiModality[]`；
  `inputModalities` 只属于内置 DeepSeek 适配器，在 pi-ai 路由上被忽略（这也是"声明了仍不支持图片"的根因）；
- `plans_patch_yaml()` 改为输出 `input: [text, image]`。

### 修复 export/import 往返丢字段
- `provider export` 改为输出文档写法（`input_modalities`/`context_window`/`max_tokens`）；
- `validate_provider_package()` 同时接受 camelCase 与 snake_case，`load_provider_package()`
  不再在每次加载时丢掉 `contextWindow`/`maxTokens`（此前会静默丢失）。

### 修「当前模型不支持图片」：模型条目补 input_modalities
- 规范新增模型级 `input_modalities`（`["text"]` / `["text","image"]`），`provider validate`、
  `discover`（上游 `input_modalities` 字段）与渲染（patch 里的 `inputModalities`）全部打通；
- 内置包更新：`deepseek-flash` = text+image、`deepseek-v4-pro` = text；opencode-go 的
  `gpt-*`/`claude-*`/`gemini-*`/`glm-5*`/`kimi-k3`/`minimax-m3`/`qwen3.8-max` 标为 text+image，其余保守按 text；
- 之前导入过的包可以原地补字段后 `dsh-muctl plan sync`，再重启实例生效。

### 模板下载改用 python（带进度）+ 镜像优先
- 全新 Alpine 没有 curl，busybox wget 对 TUNA/USTC 会 403：下载改用 **python3/urllib**
  （硬依赖、行为等同 curl），并打印进度 `x MB / y MB (pct, MB/s)`；
- 候选顺序改为 **TUNA → USTC → 配置基址 → 官方**（官方在 CN 可能极慢）；
- `curl` 重新加入宿主依赖（python 不可用时的兜底），wget 仅作最后手段。

### 套餐接管模型 + 修掉"实例起不来"的 DUPLICATE_KEY
- 启用套餐后清空内置 DeepSeek 目录（`llm-deepseek: models: []`），并把 `agent-default-model` 指向默认套餐，
  同时用 `DEEPSEEK_API_KEY` 兼容升级前建的会话；
- **修复**：`.credentials.yaml` 按名字合并 refs（原来会写出重复的 `DEEPSEEK_API_KEY`，
  导致 `startup failed: DUPLICATE_KEY`、实例无法启动）；
- `instance start` 失败时打印 harness 自己的失败原因；冷启动等待 300s → 900s；
- `refresh-cookie` 在 mint 失败时会重读日志、重试 45 秒；
- 抽屉的 provider 下拉只列**已导入**的包（内置 preset 不再出现）；
- 镜像下载：去掉会误判的 Range 预探测，改为对每个候选**先 curl** 再 wget、带超时、逐个尝试并记录；
- 文档：`user add` 已自动建并启动实例，不需要再 `instance create`。

### 抽屉「我的套餐」+ 提示词重写 + 后台去掉 401 警示
- 抽屉新增「我的套餐」：列出可见套餐（自己的可改名/删除，团队套餐只读）、可自助添加（名字/provider/key）；
  接口走 `/me`，团队套餐的改动被平台侧 403 拒绝；
- 提示词重写为**自包含**：不再要求 AI 跑 `dsh-muctl`，只要"读文档 → 输出一个 JSON"，校验交给平台；
  并写清三种协议的 URL 拼接与 `base_url` 前缀规则；后台加了「下载为文件」；
- 后台「模型接入」移除了"启用会 401、先别开"的警示（该问题已修）；
- 修复 `/api/me/plans` 的 `plans_for_user` 迭代错误（500）；
- 实测：`GET /me` 返回自己的套餐 + 团队套餐 + providers；`plan-add` 200；团队套餐改名 403。

### 后台「模型接入」改成配置文件驱动 + 团队套餐
- provider 不再用表单填：粘贴配置文件 → 校验 / 导入 / 导出 / 删除；「查看/复制」给出规范与 AI 提示词；
- provider 行可「刷新模型」（上游拉取 + 逐模型协议探测）与「查配额」；
- 新增团队套餐管理与创建表单（套餐名 / provider / key / 可见用户 / 限定模型 / 默认模型 / 设为默认）；
- 新增 API：`/api/packages*`、`/api/plans*`、`/api/provider-spec`（走同一套鉴权与审计）；
- 验证：admin-surface 37/0、user-panel 13/0、doctor 35/0/0。

### provider 改成"配置文件"，可导入导出、列模型、查配额
- 新规范 `dsh-mu.provider/v1`，`dsh-muctl provider spec` 打印规范与"给 AI 的提示词"
  （用户把提示词 + 供应商文档丢给 AI，即可生成配置；生成后 `provider validate` 自检）；
- 新命令：`provider validate|import|export|models|usage`；
  `provider models --refresh --detect-api` 会去上游拉模型目录并**逐个模型探测协议**
  （openai-completions / openai-responses / anthropic-messages），因为提供商经常按模型分协议；
- 模型条目支持 `api`/`base_url` 覆盖：同一 base_url 下不同协议的模型仍落在同一个分组；
- 内建包 `provider-packages/{deepseek,opencode-go}.json`；
- 真机验证：deepseek 2 个模型（带 context/max_tokens）；opencode-go 37 个模型、31 个为上游新增、
  `gpt-5.6-luna → responses`、`claude-haiku-5-5 → messages`；`provider usage opencode-go`
  读到 rolling/weekly/monthly 百分比与重置时间。

### 抽屉探头（peek）视觉修正
- 图标按原始 **35×76** 像素 1:1 显示（原来被拉成 62px 宽）；
- 去掉绿色状态点（连同 CSS/HTML/JS 刷新逻辑）与 `drop-shadow`；
- 图标 PNG 里**自带**的柔和阴影一并清掉（清除了 157 个"中性灰+半透明"像素）；
- `/mu-static/panel.js` 的 URL 现在带 `?v=<size+mtime>` 缓存键，改样式后普通刷新即可生效；
- 注入宿主元素的内联样式精简为 4 个属性（`all: initial` 只保留在 shadow 的 `:host` 里）。

### 网关搬进平台 jail（默认启用，宿主不再需要 nginx）
- 平台 jail：`/var/lib/dsh-mu/gateway/{rootfs,conf,logs,etc}`，由 `dsh-muctl install` 在 jail 模式下
  自动构建（镜像 + `openresty` + `lua-resty-{core,hmac,lrucache,openssl,string}`），配置卷挂
  `/etc/dsh-mu-gateway`，`/etc/dsh-mu` 用 jail 自己的副本（只读），日志沿用 `/var/log/dsh-mu`；
- **只保留 `CAP_NET_BIND_SERVICE`**（绑 80/443），其余能力全清；jail 内 nginx 以 root 运行
  （jail 本身是边界，这样不必 chown/setuid 到别的 uid）；
- 实测：:80 由 jail 里的 nginx 唯一监听；`/login` 200、`POST /login` 302、`/me` 200、
  `/` 200（真的代理到实例页面）；宿主 openresty 已停止并移出 runlevel；
- **宿主不再依赖 openresty**：`apk add dsh-mu` 的 depends 去掉了 `openresty`/`lua-resty-*`
  （它们在平台 jail 里）；`install` 的宿主包列表也按模式裁剪；
- doctor 分模式：jail 模式下检查 `gateway jail rootfs / config / configuration / answering`，
  不再检查宿主 nginx（jail 在应答时跳过 `nginx -t`，因为它会抢端口）；
- 修掉的坑（都会影响正式安装）：① 交互提示在脚本里也会吃 stdin → 只在 stdin+stdout 都是 TTY 时提示；
  ② 宿主没有 `xz`，`tar -xJf` 失败 → 改用 Python `tarfile`；③ `openresty` 包的二进制叫
  `/usr/sbin/nginx`，`-g 'daemon off;'` 会被 OpenRC 拆引号 → `daemon off;` 写进配置；
  ④ `KEEP_CAPS` 没导出给内层脚本 → 能力全被清成 0（现在 capability 探针显示只有 `0x400`）；
- 仍可回退：`dsh-muctl config gateway_mode host` + `dsh-muctl install`。

### jail 模板改成 rjail 方式：Proxmox 系统镜像 → 解压 → chroot 内装包
- 按你的意见，**删掉**原来 `apk add --root --initdb` 自建 rootfs 的路径，改为：
  1. 从 Proxmox 索引列出 Alpine 系统镜像（默认 `http://download.proxmox.com/images/system`，
     镜像站一般不可列目录，所以索引固定用官方），按架构过滤，**列出来让用户选序号，默认最新**；
     `--yes`/非交互取最新，`--template-image` 可钉死文件名；
  2. 下载并缓存到 `/var/lib/dsh-mu/images/`（约 3 MB，一次性）；
  3. 解压到 `/var/lib/dsh-mu/templates/alpine`，把 `/etc/apk/repositories` 换成 `ALPINE_MIRROR/v<版本>`，
     并复制宿主 apk 公钥与 `resolv.conf`；
  4. `chroot <模板> apk add` 装 `ALPINE_TEMPLATE_PACKAGES` —— 真 chroot 里包脚本/trigger 正常执行，
     旧流程那条 "busybox applet symlinks 缺失 → busybox --install -s" 补丁随之删除；
- **按公网 IP 归属地选镜像**（`detect_cn_region()`：CN → 清华 alpine + 清华 proxmox；其他 → 官方），
  交互时提示确认；显式配置的 `ALPINE_MIRROR`/`ALPINE_IMAGE_BASE` 永远优先，且**不会**被写回配置文件；
- **不做哈希校验**（按你的要求）；镜像站滞后时下载自动回退官方地址；
- 新增配置项 `ALPINE_IMAGE_INDEX`、`ALPINE_IMAGE_BASE`、`ALPINE_TEMPLATE_IMAGE`；`install` 新增
  `--template-image` 与 `--yes`；
- 实测（全新 Alpine chroot 内）：`mirrors: … looks like CN; using the Tsinghua mirrors` →
  `using the newest image: alpine-3.24-default_20260714_amd64.tar.xz` → `downloaded … (3 MB)` →
  `extracting …` → `installing into the template inside a chroot …` →
  `template built: … (140 MB) from alpine-3.24-…`；模板内 repositories 为清华 `v3.24/main|community`；
  `doctor: PASS jail template (140 MB)`；排练 `PASS=53 FAIL=0`。
- 另新增 **`docs/09-构建环境.md`**：构建 apk 只需要 apk-tools 3 的 `apk mkpkg`（工作区这台
  Alpine 3.24.2 自带，**无需安装任何东西**），并写明重装机器后如何从零重建、如何静态验证、发布约定。

### 打包：改成一键出「未签名 v3 包」，不再依赖 abuild
- 起因：你决定 apk **不签名**（也不发布到 Alpine 仓库，只挂 GitHub），并且希望**工作区这台机也能构建**，
  因为 dsh-mu 会还原成初始状态。原来的 `make-apk.sh` 默认走 `abuild`（需要签名密钥），
  而工作区没有 `abuild`；老的 `--legacy-v2` 手写打包器又已经被 apk-tools 3 拒绝
  （实测 `ERROR: …: v2 package format error`）。
- 做法：默认改用 apk-tools 3 自带的 **`apk mkpkg`** 产出**无签名 v3 包**：不需要 abuild、不需要密钥、
  不联网；`--signed` 仍保留（走 abuild + APKBUILD）作为"本机 `apk add` 免 `--allow-untrusted`"的可选路径。
- 实测（工作区构建）：`dist/dsh-mu-0.1.0-r0.apk` 290 KB；`apk verify` → `UNTRUSTED signature`（符合设计）；
  `apk --allow-untrusted manifest` 列出 **42 个文件**、17 项承诺文件齐全、**0** 个模板/状态/密钥/字节码条目。
- 实机排练用**工作区产出的包**跑通完整生命周期：`apk add` → 升级（`post-upgrade pre-upgrade` 都执行）→
  卸载（记录与 `secret.key` 逐字节存活）→ 重装；`FRESH REHEARSAL: PASS=62 FAIL=0`。
  （这一条同时证明了 `apk mkpkg -s TYPE:FILE` 是把**脚本内容**嵌进包，删掉源文件后安装依然执行。）
- `tests/artifact-audit.py` 随之支持 v3：用 `apk manifest` 读文件清单、用 `apk extract` 取文件大小/权限，
  并把"未签名"视为**预期状态**而不是失败。

### provider 连接测试：200 不等于"通"
- 抽查页面「测试连接」（`POST /api/provider-test` → `provider_probe()`）。多数行为本来就稳
  （25s 超时、最多读 2000 字节、异常都变成结果而不会崩、`ThreadingHTTPServer` 让慢探针不阻塞接口），
  但判定有问题：**只要 HTTP 2xx 就报 `ok: true`** —— 返回 200 却带着垃圾正文的端点会显示"连接正常"，
  把管理员引向错误的方向。实测
  `FAIL  a 200 with a junk body was reported as working  -- ok=True detail=HTTP ok (empty body parsed)`；
- 修法：`ok` 现在同时要求 **2xx 且正文里有可识别的模型回复**（OpenAI 的 `choices` 或 Anthropic 的 `content`），
  否则 `ok: false` 并给出原因与正文片段（`status` 仍保留 200 供诊断）；
- 测试同时钉住"**密钥绝不回显**"（用带标记的假 key 检查整个响应体）、慢探针期间 `GET /api/overview`
  仍能在 1 秒级应答、`file://` base_url 干净失败且不泄露文件内容、未知 provider 404；
- 新增 `tests/provider-probe.py` 作为回归第 30 层（现共 31 层）。

### 密码最小长度：管理员重置那条路漏了校验
- 抽查密码这道边界：`me_password`（用户自助改密）与 `do_user_create`（建号）**都有** `≥8` 校验，
  但 **CLI 的 `user passwd` 完全没有** —— 而管理员页面重置密码走的正是
  `do_user_password` → `cmd_user_passwd` 这条**没有校验**的路。也就是说：
  - `dsh-muctl user passwd <名> --password x` → 接受；
  - `--password ''` 且 stdin 为空 → **真的把密码设成空**（实测 `exit=0`）；
  - 管理员 API `POST /api/users/<名>/password {"password":"1234567"}` → **200**（不是 400）。
  （好消息：Lua 侧 `verify_password` 对空/畸形记录一律 `return false`，属于 fail-closed，
  所以空密码不会变成"空密码可登录"，只是把账号锁死。）
- 修法：唯一收口点 `require_password()`（≥8 且不许纯空白），由 `cmd_user_passwd` 与 `cmd_user_add`
  调用 —— CLI 与页面两条路自动一致；`user add` 不传密码仍然**生成**随机密码（长）不受影响；
- 测试同时验证"拒绝的副作用为零"：被拒的修改后**哈希与 session epoch 都不变**（不会误踢用户下线），
  以及 8 字符边界**真的能登录**（新密码 302、旧密码失败）；
- 新增 `tests/password-bounds.py` 作为回归第 29 层（现共 30 层）。

### 限额取值：`0` 的两种含义与负值的静默反转
- 抽查 `instance limits` 的取值边界，发现**两个方向相反的问题**：
  1. **原始形式里的 `0` 会真的写进 cgroup**：`--mem 0` → `memory.max=0`（一个字节都不许），
     `--pids 0` → `pids.max=0`（一个进程都不许）——同一屏里的 `--mem-mb 0` 却是"不限"。
     现在原始形式拒绝 `0`，并在报错里指明用 `max`（或 `--mem-mb/--pids-num` 的 `0`）；
  2. **负值被静默当成"不限"**：`--mem-mb -1`、`--cpu-cores -1`、`--pids-num -1` 都会**移除**限制
     （`mb > 0 else "max"` / `cores <= 0`）。现在一律拒绝：`memory must not be negative
     (use 0 or 'max' for unlimited)`。`0` 仍然是文档承诺的"不限"（并且实测对 cgroup 生效）。
- 测试顺带钉住：`2G` → `memory.max=2147483648`、`1.5` 核 → `cpu.max=150000 100000`、
  非数字拒绝、原始 `max` 仍可用、还原原限额后**再验证**（第 54 轮的教训）；
- 新增 `tests/limit-values.py` 作为回归第 28 层（现共 29 层）。

### 账号名与 API key：同一规则要守在每个入口
- 抽查"名字与密钥"这两类**会变成路径或进入 jail 的输入**：
  - 账号名已有正则 `[a-z][a-z0-9_-]{0,31}`（`../escape`、`a/b`、`.`、`..`、大写、空格、33 字符全部被拒，
    且**不留用户记录、不建目录**）；管理员名也不能被占用（`user add root` → 拒绝，实例不会被创建成孤儿）；
  - **发现不一致**：1 个字符的 API key，**CLI 接受并写进实例凭据**，而**页面（API）返回 400
    `api_key looks too short`** —— 同一份数据两个入口两套规则；
- 修法：把长度校验收到唯一入口 `write_instance_api_key()`（≥8 字符），CLI 与页面自动一致，
  报错也统一为 `the API key looks too short (N characters; at least 8)`；`--remove` 不受影响；
- 新增 `tests/name-key-bounds.py` 作为回归第 27 层（现共 28 层）。测试第一版把 `.`/`..` 判成"创建了目录"
  （那两个路径本来就存在）——已改成对比 `instances/` 目录快照，并把 argparse 层面的用法错误也算作拒绝。

### 平台配置的边界（危险但合法的取值要有人管）
- 抽查页面的「平台」标签接口 `POST /api/config {key,value}`。三条**本来就对**的行为被测试钉住：
  未知键 → 400 并列出 25 个可接受键；非法值 → 400（点名键与 kind）且**配置文件逐字节未动**；
  **清空 `admin_system_users` 不会把所有人锁在门外**——`system_admin_names()` 有 `or ["root"]` 兜底，
  root 仍然是管理员；
- **发现一个真缺口**：把 `PORT_RANGE` 收缩到**不包含现有实例端口**时，配置被接受、而 `doctor` 一声不吭
  （平台记录与自身配置矛盾却报全绿）。已补检查：实例端口越界 → `WARN instance <名> port range`
  并给出修复提示（`instance limits <名> --port …` 或放宽 PORT_RANGE）；
- 先写测试证明（`6 PASS / 1 FAIL`，其中一条 FAIL 是**我自己清理时的还原失败**，见下）→ 修 → `6 PASS / 0 FAIL`；
- 新增 `tests/config-bounds.py` 作为回归第 26 层（现共 27 层）。

> 本轮有两处**我自己的失误**：① 检查最初被插进了 `render_gateway()`（那里没有 `add`），
> 会让渲染在遇到越界端口时报错——是测试里"doctor 什么都没说"暴露的；② 我的清理代码没能还原
> `PORT_RANGE`（用剥引号的办法解析 CLI 的 JSON 数组），把实机留在了缩小后的范围上，随后手工修正。
> 两处都写进记录：**改代码要确认改在了正确的函数里，测试的清理同样要验证。**

### 日志接口的边界（错别字不该是 500）
- 抽查页面的 logs 接口 `?lines=N`：**限幅本身是对的**（`max(1, min(lines, 2000))`），但
  **解析是裸的 `int(query[...])`** —— `?lines=abc` 会抛出 ValueError 走 500 兜底
  （`{"error": "ValueError", "detail": "invalid literal for int()..."}`），而该路由**也不检查实例是否存在**，
  未知实例同样返回 200 的 `{"error": "no such log"}`；
- 先写测试证明（`tests/log-bounds.py`：`9 PASS / 2 FAIL`），再修：非数字 → **400** `lines must be an integer`
  （并提示限幅范围）、未知实例 → **404**（与其它 per-instance 路由一致）。同一测试变为 **11 PASS / 0 FAIL**，
  并把限幅行为钉住（`0`/`-5` → 1 行，超大值 → 2000 行）；
- 新增 `tests/log-bounds.py` 作为回归第 25 层（现共 26 层）。

### 恢复前的体检报告（会覆盖什么、归档是否过旧）
- 抽查 `restore --dry-run`：它**什么都不改** ✓，但人读输出只有一行 `instance files included: True` ——
  **不点名会被覆盖的卷**，也不提醒"归档比现网旧"（恢复陈旧备份正是最常见的丢数据方式）；
- 先写测试证明（`tests/restore-plan.py`：备份 → 之后修改现网 → 跑 dry-run）：
  `FAIL 不点名卷 / 不警告覆盖 / 不警告陈旧 / JSON 缺少 archive_volumes|overwrites`（8 PASS / 4 FAIL）；
- 修法：`restore_plan()` 增加 `archive_volumes`（从 manifest 的 members 取）、`overwrites`（现网已存在的）、
  `archive_is_older`（归档创建时间 vs 现网 `updated_at`）；`cmd_restore` 逐行打印
  `volume: … (OVERWRITES live data)` 与 `warning: this archive is OLDER…`。同一测试变为 **12 PASS / 0 FAIL**；
- 新增 `tests/restore-plan.py` 作为回归第 24 层（现共 25 层）。

### 恢复后实例卷的属主（跟进改动）
- 跟进第 50 轮：`apply_restore()` 以 root 解包，**只**对 `/etc/dsh-mu` 下 5 个配置文件修权限，
  实例卷完全依赖 tar 记录的属主 —— 换过 uid 的归档（或来自别的机器）会把 `home`/`workspace`
  留成别人的属主，而 jail 内没有 capability，实例连自己的目录都写不了；
- 先写测试证明这一点：把实例卷故意 `chown -R root:root` 后再备份、恢复，实测
  `FAIL the restored home/workspace volume is owned by uid 0, not 60001`（12 PASS / 2 FAIL）；
- 修法：恢复后按**记录**对每个实例调用 `ensure_owner()`（记录才是权威，与在线实例一致）；
  同一测试变为 **14 PASS / 0 FAIL**，两卷回到 uid 60001、内容完整、实例可写；
- 新增 `tests/restore-owner.py` 作为回归第 23 层（现共 24 层）。

### 备份漏掉了用户真正的工作区（并改为流式下载）
- 抽查管理页的备份下载时发现**真问题**：jail 把 `instances/<名>/workspace` 单独 bind 到
  `/home/dsh/workspace`（**遮蔽**了 `home/workspace`），而 `backup_members(--with-instances)`
  只打包 `instances/<名>/home` —— 于是**用户写在工作区里的文件根本不在备份里**，
  灾难恢复会静默丢掉它们。已在实机证实：归档成员只有 9 个配置/记录文件 + 各实例 `home`，
  `instances/<名>/workspace/` 的出现次数为 **0**；
- 修法：`with_instances` 现在同时打包 **`home`（DSH_HOME）与 `workspace`（实际工作区）** 两个持久卷；
  实测把一个探针文件写进 `instances/alice/workspace/` 后，归档里能找到并解出内容；
- 同时修掉第 26 轮那场灾难恢复演练的**误导性检查**：它把标记写在被遮蔽的 `home/workspace` 里，
  所以"恢复成功"验证的是一个用户从不写入的目录。现在演练**同时在两个卷放标记**，并断言
  **归档里没有 rootfs 拷贝**（只含记录与持久卷）；
- 备份下载改为**流式发送**（`shutil.copyfileobj` 256KB 块）：工作区是用户数据、可能很大，
  原来的 `open(...).read()` 会把整个归档读进后端内存，让 RSS 随备份大小增长；
- 顺带把该处理器里最后一处 `str(exc)` 换成 `mu_exc_message(exc)`（第 49 轮漏网的一处）。

### 端口迁移：三个守卫与"网关无需 reload"（并修掉错误消息丢失）
- 新增 `tests/port-move.py`（回归第 22 层，11 项）：`PATCH /api/instances/<n> {port:N}` 迁移端口 →
  记录与实例配置被重写 → 重启后**只听新端口** → **`/me` 无需 reload 网关即报新端口且 `live=true`**（FR-3.2
  每请求解析）；三个守卫（越出 PORT_RANGE、占用中、已分配给别的实例）都必须拒绝；
- **修掉一个真 bug：`die()` 的错误原因在接口里丢失**。`die()` 把消息打到 stderr 后抛 `SystemExit(code)`，
  而接口 `except SystemExit as exc: str(exc)` 只能拿到退出码——于是客户端看到
  `{"error": "cannot move the port", "detail": "1"}`。现在 `die()` 抛 **`CliError`（继承 SystemExit）并携带
  `message`**，接口用 `mu_exc_message()` 取它。实测错误变成了
  `port 3100 is already assigned to instance alice` / `port 1 is outside the configured range 3100-3999`。

### 页面其余标签也走了一遍（限额/日志/审计/provider 目录）
- `tests/admin-surface.py` 扩到 **37 项**，把页面其余标签使用的接口都真跑一遍：
  **实例日志**（`?lines=50` 必须返回 `file` + `lines` 数组）、**限额编辑**（`PATCH` 后
  `limits_human.pids` 必须立刻反映新值，并确认**落到 cgroup**）、**审计**（表格视图的
  `scanned/matched/records` 字段 + **JSONL 导出**逐行合法）、**provider 目录**（新增自定义 →
  目录里标为 custom → 删除 → **内置 provider 不可删**，返回 404）；
- 顺带修一个真问题：**限额 PATCH 对未知字段静默返回 200**。调用方发 `pids_max`（记录里的原始字段名）
  时不会报错，却**什么都不改**——脚本调用方会以为改成功了。现在返回 **400**，并列出可接受字段
  （`cpu_cores|cpu`、`mem_mb|mem`、`pids_num|pids`）以及"记录里才是 cpu_max/mem_max/pids_max"的提示。
  页面本来就发对了字段（`pids_num`），是我第一版测试发错了——**但静默无操作确实是个坑**。

### 管理页面两个真 bug（由"以管理员身份走一遍"发现）
- **凭据状态列永远是"未设置"**：页面 provider 表读 `i.key_set` / `i.key_source`，而 `/api/instances`
  的视图**从不返回这两个字段**（只有 key 设置/删除的响应里有）。现在 `instance_view()` 会算出
  `key_set` 与 `key_source`（读实例凭据文件，不暴露秘密）；
- **删除用户会留下孤儿实例**：页面「删除」走 `DELETE /api/users/<名>`，而它只删账号、保留实例（CLI 的
  `--with-instance` 是显式选项）→ 留下孤儿实例并让 `doctor` 报警。现在该接口在账号仍拥有实例时返回
  **409** 并说明如何继续；页面改为**先确认"实例与数据一起删除"**再带 `?with_instance=1`；
- 同一条数据路径的另一半：API 的 `instance_key_set` / `instance_key_del` 现在也写**非秘密标记**并
  `save_state`（第 44 轮只修了 CLI 那条路），于是凭据变更在审计里有一致的记录；
- `tests/admin-surface.py` 扩到 **27 项**：新增 provider 指派、launcher patch 文件、`GET patch` 解析、
  写入 key、**实例列表的凭据状态**、失败的 provider 探测必须**结构化失败**（`ok=false`，不是 5xx）、
  以及"无标记删除被 409 拒绝且实例存活 / 带标记删除成功"。

### 凭据：审计盲点、脱敏漏洞，与灾难恢复里的凭据核对
- 抽查"备份/恢复是否带上模型凭据"时发现三件事：
  ① **灾难恢复演练从来没核对过凭据**（只核对了账号与实例 home），本轮补上：演练现在会写一个
  **短**（11 字符）测试密钥、自定义 provider，然后断言——凭据落在实例的 **0600 文件**里、
  `state.json` 里只有**布尔标记**（无秘密）、**归档包含凭据文件与标记**、恢复后凭据文件**逐字节一致**、
  目录里的自定义 provider 仍在；
  ② **改凭据完全没有审计记录**：`instance api-key` 写的是实例的凭据文件，从不写 `state.json`，于是
  审计层没有任何记录，而审计指纹里"是否有凭据"的字段也就**永远是假**（没人设置）。现在写入一个
  非秘密标记并 `save_state`，凭据变更会被记为 `instance.change`；
  ③ **脱敏靠长度启发式，短密钥会漏**：`instance api-key <名> <密钥>` 是**位置参数**，25 字符以上才会被
  `***` 替换。现在补一条**按位置**的规则（`api-key` 后的第二个词一律脱敏），并用**短密钥**在排练里
  实测"密钥值不出现在审计日志与 state.json 中"。

### state.json 并发锁的行为验证（并修一处变量名冲突）
- 新增 `tests/state-lock.py`（回归第 19 层，9 项）：8 个并发写者交替修改同一账号后，`state.json`
  **仍是合法 JSON 且无丢失更新**；更重要的是证明**锁真的会阻塞**——用一个助手进程持锁 3 秒，
  另一个写者的命令**等了 3.1 秒**才完成。顺序执行时它不影响其它层（只短暂持锁）。
- 过程中抓到一处**我自己造成的变量名冲突**：`tests/lib.sh` 早就把 `DSHMU_LIB_DIR` 定义为平台库根目录
  （`/usr/local/lib/dsh-mu`），而新脚本把它当成 Python 模块路径用，于是**只有经过 lib.sh 环境的回归里**
  助手进程 import 失败。更糟的是我把子进程 stderr 丢掉了，只看到一句没头没脑的 FAIL。现在：改用
  `DSHMU_PYTHON_DIR`，并**捕获子进程 stderr 以便直接看到原因**。

### cgroup 限额：补上"限流"与"在线调整"的执行证据
- 抽查 FR-4.5"限额可在线调整"。验收层原本已用**临时 cgroup** 行为化验证了 `memory.max`（`oom_kill`）与
  `pids.max`（派生数），但**`cpu.max` 从未被证明真的限流**，**"在线调整"也从未在运行中的实例上验证**；
- 新增三项：① 临时组设成 2% 单核跑满 CPU，断言内核计数器 **`nr_throttled` 增长**；
  ② **走受支持接口**（`instance limits --pids N`，它调用 `apply_limits_live`）把运行中实例的限额下调，
  断言 cgroup **立即**变成新值、且 jail 内 12 次派生只成功很少几次，随后经同一接口恢复原值（trap 兜底）；
  ③ 钉住一个实测发现的行为：**手工改 cgroup 文件不"粘"**——下一次 jail 启动会按**记录**重写限额
  （所以"记录才是权威"，也说明在线调整必须走 CLI）。验收层 30 → 34 项。`io.max` 属可选、本期未设，已在追溯表写明。

> 我第一版 B2 直接改 cgroup 文件，结果 12 个进程全部派生成功——因为 jail 每次启动都按记录重写限额，
> 我的手改值立刻被覆盖，"恢复成功"也是虚假通过。这正是"测试必须走受支持路径"的又一条教训。

### 上传与限速：两条"只有配置、没有行为"的主张补上实测
- 抽查发现 **FR-3.5（大文件上传 512m）此前没有任何测试**，而证据索引写着"已配置并实测"——属于过度声称；
  **FR-1.5（防爆破限速）也只验了"单次错误口令返回 401 或 429"**，从不验证限速真的触发；
- 新增两项检查（`tests/selftest.mjs`，现共 21 项）：**6c** 经网关 POST 3 MB 请求体必须**不是 413**
  （nginx 默认只有 1m）+ `nginx -T` 确认生效值为 `512m`；**6d** 用临时账号连续失败到第 **11** 次必须返回
  **429**（阈值 10），且**锁定期间连正确口令也被拒**（600s 窗口），跑完删号；
- 6d 第一版自己是错的（登录 POST 没带 CSRF cookie → 全是 400、根本没算失败登录），实机跑出来立刻暴露并修好——
  再一次印证：**新检查必须真跑，而且要能失败**。

### WebSocket 长连接检查：从"15 秒"改成"能真正区分超时"
- 抽查发现 `tests/selftest.mjs` 的 WebSocket 检查**默认只保持 15 秒**，而 nginx 默认 `proxy_read_timeout`
  是 **60 秒**——即使超时配置错了，这个检查也会通过，属于"不能失败的检查"；
- 实测 75 秒后看到 `pings=37`：上游每 ~2 秒发心跳，而**任何流量都会重置 `proxy_read_timeout`**，
  所以"保持 75 秒"**仍不能**证明超时值是 3600s。断言因此拆成两层：① WebSocket 保持 75s（默认值已改）
  且**明确写出它证明了什么**（有心跳则说明超时值由 6b 检查）；② 新增 **6b：`nginx -T` 有效配置中
  必须有 `proxy_read_timeout 3600s`**（这才是超时值本身的证据；无 root 时 SKIP）。

### 隔离层补上"端口不对外暴露"的实测（并修正一处过度声称）
- 抽查发现证据索引把 FR-3.7（实例端口只监听 127.0.0.1、外部不能绕过网关直连）标为"已实测（隔离边界层）"，
  但隔离层实际只验证了"jail 内跨租户访问被拒"，**从未验证从外部接口连不上** —— 属于过度声称；
- 实机实测该主张**成立**（`192.168.13.61:3100` → Connection refused；`127.0.0.1:3100` → HTTP 401），
  并把它变成永久检查写进 `tests/isolation.sh` 第 10 组：内核表（`/proc/net/tcp`）中该端口**不得有通配监听**、
  从宿主外部 IP 连接必须被拒、**同一端口在回环上必须确有响应**（否则前一条可能只是因为它根本没在服务）。
  隔离层 20 → 23 项。`ss` 不在 Alpine 基础系统里，所以直接读内核表。

### 部署漂移审计
- 新增 `tests/drift-audit.py`：把**仓库 ↔ 实机 ↔ 包（apk）**三者逐字节对照（CLI 库与 shim、jail 与 cgroup
  初始化、管理后端与页面、网关 Lua、静态资源、测试），回答"实机验证通过的到底是不是交付的代码"；
  作为回归第 13 层（现共 18 层）；
- 首次运行结果：**6 组对比全部一致**（核心 6 个文件、8 个网关 Lua、2 个静态资源、测试目录）——
  回溯证明此前所有"实机验证"跑的确实是交付的代码；
- 顺带把自己写脚本时的一个 `NameError`（推导式里引用未绑定变量）在实机上抓出并修正。

### 需求缺口收口：8 条全部完成分类
- 追溯表里的 8 条"部分实现"不再悬而未决：**7 条标为"已接受（偏差）"**（FR-1.2 散列算法、FR-2.2 PID 落库、
  FR-2.4 预置插件、FR-2.5 自助开关、FR-4.6 默认非 root、FR-4.8 崩溃计数、FR-5.4 批量升级），
  每条写明**理由**与**等价做法/影响**；**1 条标为"待定"**（FR-1.3 会话空闲过期，阈值是产品决定，需你拍板）；
- `tests/doc-lint.mjs` 增加 1 项（现共 20 项）：**每个 🟡 行必须写明"已接受"或"待定"**——
  "裸的部分实现"会 FAIL，防止缺口悄悄变成无人负责；
- 三项文档检查都用副本做了**破坏验证**（删分类、改数字、删 FR 行），确认都会失败。

### `dsh-muctl user kick`（补齐 FR-1.7）与审计指纹修正
- 新增 `dsh-muctl user kick <名>`：**推进会话 epoch 强制下线该账号全部会话，但不改口令**——
  需求 FR-1.7（"管理员可强制下线任意会话"）此前只能用改密达到同样效果；同一条能力也接到
  管理页面（用户表新增「踢下线」按钮）与回环 API（`POST /api/users/<名>/kick`）；
- **同时修一处审计盲点**：`_user_fingerprint` 原本不含 `epoch`，所以"只踢会话"这种变更**不会留下审计记录**；
  现已把 epoch 纳入指纹（口令变更本来就会记，不会造成重复记录）；
- 测试：`tests/user-flows.py` 增加 5 项（踢成功后会话失效、**口令仍然可用**、有审计记录、未知账号干净报错），
  `tests/cli-contract.sh` 增加 2 项（缺参数/未知账号的干净报错）。

### 需求追溯（发现 7 条"答应了但没写明"的缺口）
- 把 `docs/01-需求整理.md` 的 **34 条 FR 逐条对照现状**，写进 `docs/07-证据索引.md` §5：
  **25 条已实现并实测、9 条部分实现、0 条静默丢弃**；每条部分实现的都写明"差什么、怎么补"；
- 9 条缺口：口令散列默认用 PBKDF2 而非 scrypt（FR-1.2）、**无空闲过期**（FR-1.3）、
  **无独立踢会话命令**（FR-1.7，现只能靠改密）、PID 不落库（FR-2.2）、不预置插件（FR-2.4）、
  无自助开关（FR-2.5，本就没有自助入口）、**默认仍是 jail-root 而非非 root**（FR-4.6）、
  **崩溃计数未入库**（FR-4.8）、无批量升级命令（FR-5.4）；
- 全部修法进入 [证据索引 §4 的选项清单](docs/07-证据索引.md)，**没有一条阻塞交付**；
- `tests/doc-lint.mjs` 增加 3 项（现共 19 项）：**`docs/01` 里每个 FR 编号都必须出现在追溯表里**、
  追溯表必须如实标出 ≥5 条"部分实现"（防止把缺口涂成绿色）、且**汇总数字必须与表格逐行统计一致**
  （第一版就是凭印象写错的，现在由脚本核算）。

### 验收走查清单
- 新增 `docs/08-验收流程.md`：把 17 层验证写成**可独立复核**的步骤——每层的命令、期望结果、证据位置、
  失败时先看什么，附**签收表**（17 行）与"这份清单不覆盖什么"；文档随包分发到 `/usr/share/doc/dsh-mu/`；
- `tests/doc-lint.mjs` 增加 3 项检查（现共 16 项）：流程文档必须点到全部 16 个层脚本、
  必须有逐层步骤表**与**签收表（≥34 行）、必须声明未覆盖范围。

### 每实例独立 uid 验证
- 新增 `tests/run-uid.sh`：把 `instance_run_uid` 切到 `auto` → 建临时账号+实例 → 验证专用宿主 uid（实测 60001）、
  `INST_UID` 与 home/workspace 属主一致、**jail 内 `id -u` 即该 uid**、能力仍全清、procfs magic link 仍关闭、
  home 可写而 rootfs 不可写（该模式的代价：没有 jail 内 apk），最后删干净并恢复配置；作为回归第 16 层
  （现共 17 层）。威胁模型里推荐的加固选项从此有实测支撑。

### 账号生命周期验证（并修两处真实缺口）
- 新增 `tests/user-flows.py`：建号（`--must-change --instance none`）→ 登录落到实例根 → `/me` 报告标记 →
  面板确有改密入口 → `POST /me` 改密 → **标记清除、既有会话失效、旧口令再也登不进、新口令可用** → 删号；
  作为回归第 15 层（现共 16 层）；
- 验证过程中发现**两处真实缺口并修掉**：① `--must-change` **在 CLI 里根本没有定义**（代码只读
  `args.must_change`），所以这个字段**没有任何入口能设置** → 补回 `user add --must-change` 与
  `user passwd --must-change`；② 文档仍在描述**已被取代**的"首装随机管理员口令（`admin / <随机>`）"流程
  → README、`docs/05` 改写为当前的"管理员 = 宿主系统账号 + `passwd root`"，并明确
  `--must-change` 是**提示不是锁**、改密入口在用户面板；
- `tests/doc-lint.mjs` 增加"**被移除的功能不得继续被文档描述**"检查（并必须解释系统账号管理员模型），
  现共 13 项；
- 顺带把两个此前无人记录的行为写进文档：网关的**用户视图缓存 2 秒**（改密/禁用最多 2 秒后生效）
  与**无实例账号登录返回 503 提示页但会话已建立**。

### 日志轮转验证
- 新增 `tests/log-rotation.sh`：drop-in 必须用 `copytruncate`（nginx/`supervise-daemon` 持有文件描述符，
  重命名会让它们继续写旧文件）、`logrotate -d` 无报错、真跑 `logrotate -f` 后**出现轮转副本**且
  **活文件被原地截断**、随后**服务继续写日志且 nginx worker 未被重启**、审计日志轮转后仍在记录、
  兜底 `dsh-muctl logs rotate --force` 可用、保留份数有界；作为回归第 14 层（现共 15 层）。

### 修复能力验证（并补两处真实缺口）
- 新增 `tests/doctor-fix.sh`：故意破坏四件可修的东西（cgroup 委派服务的 **boot 注册**、nginx 临时目录、
  网关视图、把运行中的实例记录改成 `stopped`），断言 doctor 报出每一处、`doctor --fix`/`sync-state`
  **真的修好**，最后恢复 0 FAIL / 0 WARN；作为回归第 13 层（现共 14 层）；
- 写这个测试时发现**两处真实缺口并修掉**：① `doctor` 从不检查 `dsh-mu-cgroup` 是否注册在 boot runlevel
  （链接丢失后重启即失去委派，而 doctor 报一切正常）→ 新增检查项；② `doctor --fix` 只 `rc-service start`、
  不补注册 → 改为先 `ensure_cgroup_service()`（重写并 `rc-update add`）再启动；
- `nginx runtime` 检查从"目录是否存在"扩展为**同时校验属主与可写权限**（删除目录会被 nginx 立刻重建，
  属主/权限被改坏才不会自愈——这也正是会让上传失败的故障）。

### 证据索引
- 新增 `docs/07-证据索引.md`：把每一条能力主张映射到**能复现它的脚本**、最近一次实测结果、
  记录细节的章节，并明确列出**还没被证明的边界**（真实 OpenRC 起服务、真实 jail 实例 → 只能真机验收）
  与"后续可选工作"清单（含价值/工作量）；
- `tests/doc-lint.mjs` 增加 3 项检查：证据索引必须点到全部 12 个层脚本、覆盖四个目标领域、
  并如实声明未证明的部分（现共 11 项）。

### 交付物审计与文档随包分发
- 包与安装脚本现在会把 **`DELIVERY.md`（交接单）、`CHANGELOG.md` 与 `docs/*.md`** 装到
  `/usr/share/doc/dsh-mu/`：新机器上自带完整文档，不必回头找源码包（`APKBUILD`、`make-apk.sh` 的兜底路径、
  `install/install.sh` 三处同步）；
- 新增 `tests/artifact-audit.py`（只读、不安装）：用**当前源码树**重新打包后核对——签名验证、17 项承诺文件
  是否齐、**不该有的都没有**（jail 模板、实例数据、`state.json`/`secret.key`/`admin.token`/`providers.json`、
  `__pycache__`、`dist/`）、单文件体积、world-writable 权限；源码归档查同样的项，并核对 `DELIVERY.md`
  的承诺（安装/首跑/验收/恢复/限制）与实际一致。作为回归第 12 层（现共 13 层），实测 11 项全过。

### 灾难恢复演练
- `tests/fresh-rehearsal.sh` 新增第 9 步：在干净 Alpine 里建账号+实例 → 备份（含实例 home）→
  **`rm -rf /etc/dsh-mu`** → 重装（相当于新机器）→ `restore --force`；断言口令散列/端口/属主逐字节一致、
  会话密钥回来了（老会话继续有效）、网关视图重新渲染、实例 home 文件恢复、安全副本已生成、CLI 重新列出账号；
  紧随其后的运行时冒烟（第 10 步）跑在**恢复后的平台**上。实测该步骤 10 项全过、排练总计 `PASS=47 FAIL=0`。

### 测试脚本共享辅助
- 抽出 `tests/lib.sh`：CLI/jail/state 路径解析、计数与汇总、读实例端口与字段、等端口、进 jail 执行、
  依赖检查；`acceptance.sh`/`isolation.sh`/`cli-contract.sh`/`regression.sh`/`fresh-rehearsal.sh`
  改为引用它，重复逻辑不再各写一份；缺少 `lib.sh` 时脚本**明确报错退出 2**（实测）；
- 顺带修掉 soak 的一处**不稳断言**：nginx worker 数原来用 `pgrep -f 'nginx: worker'` 统计，
  会把上一轮排练遗留的 worker 也算进来（实测出现 3 -> 2 的假失败）；现在只统计宿主 nginx master
  的子进程，并在预热后判断稳定性（减少算"正常收敛"，增加才算失败）。

### 持续负载（soak）
- 新增 `tests/soak.py`：多个并发客户端持续打真实流量（登录页、实例转发、`/me`、管理 API），
  每 15 秒采样进程；断言后端**未被重启**、nginx worker 数不变、后端 RSS **预热后无上升趋势**（≤1 MB/min）
  且绝对增长受限、fd 不增长、**零 5xx / 零客户端错误**、实例进程 RSS 平稳；作为回归第 11 层
  （现共 12 层）；
- 实测（2 分钟 / 4 并发）：1735 请求、0 错误 0 5xx、p50 1ms / p95 3ms；8 分钟长跑用于排查泄漏。

### 管理 API 契约
- 新增 `tests/api-contract.py`：20+ 个错误请求（无/错令牌、未知路由与实例、畸形/非对象 JSON、
  越界端口、未知 provider、删内置 provider、错口令、非法限额）必须返回 JSON + 合理状态码 + `error` 字段，
  且后端日志无 traceback；再验证 9 个只读端点 200。作为回归第 5 层（现共 10 层）；
- 顺带修一处**误导性错误**：畸形 JSON 此前与"空 body"无法区分，页面会显示"name is required"；
  现在后端会记住解析失败原因并回答 `invalid JSON body (…)` / `JSON body must be an object`，
  契约测试钉住了这两句文案。

### 记录值语法检查
- `doctor` 新增每实例的「limits」检查：记录里的 `cpu_max`/`mem_max`/`mem_high`/`pids_max` 必须是
  `max` / `<n>` / `<quota> <period>`，否则 FAIL 并给出修复命令。**这条检查当场抓出了本轮自己造成的一处
  数据污染**（详见 docs/04 §17）。

### CLI 契约与命令补齐
- 新增 `tests/cli-contract.sh`：~25 个故意写错的调用必须**干净失败**（非零退出 + 人话错误 + 无 traceback），
  外加只读调用与 `--json` 可解析性；作为回归第 4 层（现共 9 层）；
- **它一次抓出 4 个真问题并已修**：`instance status <不存在>` 静默成功（现 exit 1）、
  `instance restart <不存在>` 抛 `KeyError` traceback（现干净报错）、`--cpu/--mem/--pids` 的原始 cgroup 值
  不做校验就落库（现校验 `max` / `<n>` / `<quota> <period>`）、以及**根本没有 `instance list`**（现补齐，
  支持 `--json`，`status` 也支持 `--json`）；
- 新增运维命令 `instance exec <名> -- <命令>`（jail 内执行、退出码透传，供排障）与
  `user del --with-instance`（连同实例一起清除，闭环此前只能靠 doctor 提示的悬空实例）。

### 界面自洽性检查
- 新增 `tests/ui-lint.mjs`（静态、秒级）：处理器是否有定义、DOM id 是否有标记、标签是否有面板、
  **页面 API 调用是否都在后端路由表里**、面板请求是否都被网关路由覆盖；
- 作为 `tests/regression.sh` 的第 3 层（现共 8 层）；
- 每一条检查都用"故意破坏源码"验证过**确实会失败**——过程中发现我自己的两条检查原本是**恒真**的
  （路由比对拿"从调用生成的模式"匹配自己；`location = /` 让 `/` 成为万能前缀），已修。

### 跨站防护
- 写操作新增同源栅栏：带 `Origin` 且与 `Host` 不一致的 `POST/PUT/PATCH/DELETE` 一律 403
  （`/me` 与 `/admin/` 全路径），安全方法与无 `Origin` 的 CLI/探针放行；
- `tests/selftest.mjs` 增加 5f 四条断言（跨站 POST 拒绝 / 跨站 GET 放行 / 同站通过 / 无 Origin 通过）。

### 审计留痕（CLI 补齐）
- 此前只有管理页/API 的变更留痕，**终端里改用户/实例/配置/provider 完全没有记录**。现在所有状态写入
  都经过 `save_state()`/`save_config()`/`save_custom_providers()` 的差异快照，自动写
  `state.change`/`config.change`/`provider.change`（记录实体名与变更类型，不记值）；
- 每条记录附一条**打码后的命令行**（口令/密钥/令牌/长随机串替换为 `***`），actor 为
  `cli:<系统用户>@<主机>`（`DSHMU_ACTOR` 可覆盖）；
- 后端与 CLI 复用同一个审计写入函数；后端保留请求级记录（管理员 + 客户端 IP + 状态码 + 脱敏请求体）；
- `acceptance.sh` 新增 G 段（8 条）断言：CLI 变更留痕、记录点名受影响账号、**口令不落盘**、
  actor 正确、配置变更留痕、API 动作留痕、记录带客户端地址。

### 故障与恢复
- `tests/failure-modes.py`：管理后端停止时的降级路径（匿名仍走登录页、实例转发与运维 API 不受影响、
  运维提示页在运行配置中确实生效）、实例停止时的"启动中"页、`kill -9` 后 supervisor 自动拉起、
  以及并发请求下的 `gateway install` 热重载零丢包；脚本无论成败都会恢复服务并做最终健康断言；
- 作为 `tests/regression.sh` 的第 6 层自动运行（现共 7 层）。

### 隔离验证
- `tests/isolation.sh`：在活着的实例内部逐条验证隔离边界——chroot、capability 清零、procfs magic link
  （`/proc/<宿主pid>/root|environ`）、写 `/proc/sys`/`mount`/`mknod`/`unshare -m` 被拒、共享 DSH runtime
  只读、私有 `/tmp`、cgroup 归属与限额一致、**跨租户直连必须 401 / 运维 API 必须 403**；
- 作为 `tests/regression.sh` 的第 5 层自动运行；`docs/02` 增补威胁模型与已知取舍（无 netns / 无 PID ns）。

### 可观测与一致性
- `doctor` 新增**记录一致性**检查：账号指向不存在的实例、实例的属主账号已不存在、实例 rootfs/home 目录缺失；
  健康平台保持 `0 WARN`（`--instance none` 的哨兵值不再误报，新写入统一存为空串）；
- `tests/regression.sh`：一条命令跑完 doctor → selftest → acceptance → fresh-machine → 干净 Alpine 排练，
  末尾一行汇总；
- 排练新增**包生命周期**步骤（真实版本升级、卸载逐字节保留记录、重装恢复）与失败自证输出；
- `docs/06-运维手册.md`：日常操作、故障对照表、安全清单。

### 备份与恢复
- `dsh-muctl backup` / `restore`：账号、实例记录、配置、密钥与网关视图打包（`0600`，含 `MANIFEST.json`），
  可选包含实例 home；恢复支持 `--dry-run` 计划与 `--force` 执行，执行前自动生成安全副本并还原文件权限；
- 管理页「平台」标签提供备份下载（`GET /admin/api/backup`）。

### 打包与交付
- `packaging/APKBUILD` + `make-apk.sh`：优先 abuild（自动生成签名密钥），产出单文件 `dsh-mu-<ver>-r0.apk`；
  不打包 ~140MB 模板与任何运行期数据；
- `.pre/post-install|upgrade|deinstall` 脚本：安装后打印三步引导、升级提示、卸载停服务并**保留数据**；
- 文档：`docs/01` 需求、`docs/02` 架构（含 M3 后的现状）、`docs/04` 验收报告（逐轮实测记录）、`docs/05` 打包与安装；
- 一键验收：`tests/fresh-machine.py`（doctor → install → 建号建实例 → 网关/管理面 → 回归 → 清理）。

### 已知限制
- 未签名分发的 apk 需要 `--allow-untrusted`（或把 `~/.abuild/*.rsa.pub` 分发到目标机）；
- `dsh-mud` 启动时缓存库，升级后需 `dsh-muctl admin restart`（`install` 会代做）；
- LXC 内无 userns/landlock ⇒ 实例以 `danger-full-access` 运行，jail 即边界；
- 尚未做 HTTPS 终结与多网关部署；测试账号（alice/bob）口令需正式使用前更换。
