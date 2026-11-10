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
