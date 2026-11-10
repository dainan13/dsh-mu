# 23 · 用量观测与 key 入库（设计 v2，按"先看用量"重写）

状态：**设计 + schema 已落地**（`sql/001-init.sql`，v1 已在内存库跑通）
实现顺序：**Phase A/B 先做 → 和 DSH 0.2.2rc1 一起测**（用户明确要求）
计价：**不做**（§7 只留一个空表与一页说明，等真有需要再启用）

## 0. 目标（用户原话转成一句话）

> 作为给小团队用的系统，最想知道的是 **"人到底用没用、怎么用的、用得怎么样"**，
> 而不是"该收多少钱"。所以：**维度越多越好，记录只增不减**；聚合与分析是后面的事。

由此推出的原则：

1. **先存事实，后定义指标**：一次请求一行（`usage`），字段宁多勿少；聚合放在 `usage_daily` 与查询里。
2. **不猜**：拿不到的字段写空/0，不推算（例如 provider 不回 usage 就是 0 行，不估）。
3. **不改请求路径**：采集是旁路（relay 有界队列 → 批量 POST），dsh-mud 停/慢都不影响用户请求。
4. **计价整个移出本期**：`prices` 表留着不用；`usage` 不存 cost。

## 1. 要回答的问题 → 需要的维度（这是本期的验收标准）

| 想回答 | 需要的维度 | 落在哪张表 |
| --- | --- | --- |
| 谁在用？哪些账号完全没用？ | `user`、`ts`、`requests` | `usage`、`usage_daily` |
| 一个"人"是真的在用，还是只是登录过？ | `sessions`（会话数、首末活跃、请求数）、`events(kind=login)` | `sessions`、`events` |
| 大家用哪个模型/套餐？ | `provider`、`model`、`plan` | `usage` |
| 用量在涨还是掉？ | `ts` 按日聚合 | `usage_daily` |
| 一次对话大概多重？ | `prompt_tokens` / `completion_tokens` / 是否 `stream` | `usage` |
| 是不是老失败（体验差）？ | `status`、`http_status`、`latency_ms` | `usage` |
| 插件/扩展到底有人用吗？ | `events(kind=plugin.use / ext.action)`、`target` | `events` |
| 多 key 轮换（谁在什么时间用哪把 key） | `token_id`（**脱敏 id，不是 token 本体**） | `usage`、`tokens` |
| "一次会话里用了多少" | `session_id` | `usage`、`sessions` |
| 用了多久（而不是"发了几次"） | `active_minutes`（有请求的分钟数） | `sessions`、`usage_daily` |
| 客户端版本分布（谁升了、谁没升） | `client`、`client_version` | `usage`、`sessions` |
| agentic 强度（有没有在用工具） | `tool_calls`、`has_tools` | `usage` |
| 多模态强度 | `image_inputs` | `usage` |
| 体验如何（快不快） | `latency_ms`、`upstream_ms`、`ttft_ms` | `usage` |
| 是不是老失败/被限流 | `status`、`http_status`、`stop_reason`、`attempts` | `usage` |
| 走哪个接口 | `endpoint`（/v1/chat/completions、/v1/messages…） | `usage` |

**脱敏原则（硬约束）**：

- `tokens` 表里才有 token 本体；`usage` 只存 `token_id`（随机 id）+ 可选 `token_prefix`（前 6 位，人工核对用）。
- 任何日志、导出、页面展示都不得包含 token 本体；`usage` 表可以整表导出给客户看。
- `session_id` 只用**网关会话 id**（不是 cookie 值）；CLI / API 调用没有会话 id 就**留空** ✗（不按时间窗口编造 ✗）。
`request_id` 每次请求随机，用于排障。

**本期明确不加的维度**（先不做，理由写清）：

| 不加 | 理由 |
| --- | --- |
| IP / 地理位置 | 隐私，且小团队不需要；排障时用 request_id 反查日志即可 |
| 具体提示词/回复内容 | 属于用户内容，不落库（要审计另设计，需明确授权） |
| 费用 / 汇率 / 账单 | 本期定位是"用量观测"（另见 §7） |
| 用户满意度 / 反馈 | 没有采集入口，先不做 |

## 2. 两张核心表（完整 DDL 见 `sql/001-init.sql`）

```
users / tokens / credentials / routes / kv      ← 身份与凭据（Phase A）
usage（一次请求一行，**32 列**）                 ← 用量事实（Phase B）
usage_daily（按 日·user·instance·provider·model·token_id 汇总，含 active_minutes/tool_calls/images）
sessions（会话：**只有存在网关会话 id 时才建行**；started_at / last_seen / requests / tokens / active_minutes / client / client_version / first_model / token_id）
events（登录、插件安装/使用、扩展动作 —— "用没用"的非请求证据）
prices（**本期不用**）
```

设计取舍：

- `usage.plan` / `usage.model` / `usage.provider` 都是**快照**，不 join 实时表 → 套餐改名的历史不丢。
- `usage_daily` 由 dsh-mud 定期 upsert（每 5 分钟跑增量），查询走它 → 便宜；对账时才扫 `usage`。
- `events.detail` 只放短文本，明确禁止写 token/key/密码。

## 3. Phase A · 身份与凭据入库（不改请求路径）

```
A1 建库：/var/lib/dsh-mu/state.db（0600，WAL）+ 跑 001-init.sql
A2 导入：relay.json + state.json → users / tokens / routes / credentials
     推导不出 user 的 token → legacy=1（不参与"按用户"统计，但请求照旧）
A3 双写：之后 key/plan/token 变更同时写库与 relay.json
A4 查询：命令行子命令 `relay`（**待实现**：tokens / routes / credential <provider>，只读）
A5 验收：换 key 后 relay.json 与库一致；`relay_source=file`（默认）行为完全不变
```

**这一阶段对外零影响**（网关仍读 relay.json ✗ 不改读取路径 ✗），可以随时停。

## 4. Phase B · 用量采集（旁路，不影响请求）

```
B1 relay 侧：上游返回后取 usage + 计时 + http status → 有界队列（如 200 条）
             每 2s 或满 50 条 flush → POST /internal/usage（loopback + 管理 token）
             失败丢弃 + 计数（doctor 里出 "usage ingest" 一行）
B2 dsh-mud：校验 → INSERT usage（token 归到 user/instance/plan 快照）→ 定期 upsert usage_daily
B3 会话：只用网关会话 id；没有就留空（不按时间窗口推算）
B4 观测命令（先 CLI，再考虑页面）：
     usage（待实现） today            # 今日：请求数 / token / 活跃用户 / 失败率
     usage（待实现） users --days 7   # 按用户：请求、token、最后活跃、会话数
     usage（待实现） models --days 7  # 按 provider·model
     usage（待实现） series --days 30 # 按日
B5 验收：一次真实对话后能对上 user/instance/plan/model/tokens/status；聚合=逐条求和（抽样）
```

## 5. 页面（本期最后一小块，可选）

```
管理台新增「用量」页（照设计稿的表格/面板风格）：
  · KPI：今日请求 / 今日 tokens / 7 日活跃用户 / 失败率
  · 按用户表：请求、tokens、会话数、最后活跃（能一眼看出"谁没用"）
  · 按模型表、按日趋势
  · 空态就写"还没有用量数据"，不编造
（不做：计价、账单、导出 PDF ✗ 这些等你决定要不要做收费再说）
```

## 6. 与 DSH 0.2.2rc1 的关系（为什么先做 A/B）

```
· A/B 都在**平台侧**（relay + dsh-mud）✓ 不碰实例内部 ✗
· B1 要解析上游 usage ✗ 而 DSH 升级可能改变上游请求/流式形态 ✗
  → 先做 B，再升 DSH，可以一次性验证"新版本下用量还能正确采集"✓
  → 顺序反过来会拖着两件事一起 debug ✗
· 升级当天用 `selector-drift.js`（实例页 DOM）+ `usage today`（待实现）（用量）两张表对照验收 ✓
```

## 7. 计价（本期不做，只留接口位）

```
prices 表已在 schema 里（空表）；usage 里**没有** cost 字段。
将来要收费时的最小改动：usage 加 cost_micros + price_id 两列 → 回填历史用 prices.effective_from ✓
在那之前，"花费"一律回答：**没有这项数据**（不推算、不估算 ✗）。
```

## 8. 验收门槛（本期）

```
Phase A
  [ ] 建库成功；`relay tokens`（待实现）能列出 11 个实例对应的 token
  [ ] 推导不出 user 的 token 标 legacy，不阻塞
  [ ] 双写后改一次 key：relay.json 与库一致；`relay_source=file` 行为不变
Phase B
  [ ] 一次对话 → usage 一行，user/instance/plan/provider/model/tokens/status 都对
  [ ] dsh-mud 停掉 30 秒：用户请求**照常**（只是丢用量，doctor WARN）
  [ ] `usage today` 与 `usage series`（待实现）抽样一致
  [ ] 30 天数据下 `usage users`（待实现）响应 < 1s（走 usage_daily）
```

## 9. 与既有文档的关系

- `docs/20`：早期备忘（本文件取代它）。
- `docs/18 §3`：扩展 jail / socket，不涉及本期。
- `docs/22`：DSH 用户侧六块 + 踩坑；其中"升级前跑 selector-drift"与本期的 `usage today` 一起构成升级验收。

## 10. 实现状态（1.8.5x）与踩过的坑

已落地：

```
SQLite /var/lib/dsh-mu/state.db（WAL）+ sql/001-init.sql（含幂等迁移 _migrate_state_db）
凭据：relay_source=file|db；db 模式 relay 走 30s 缓存 + unix socket 问 dsh-mud；失败回退 relay.json
db 模式下**停止写 relay.json**（文件保留，切回 file 即在用）
tokens：id 非自增（tk_ + 域分离哈希）、token_hash（安装盐 ‖ 套餐 ‖ token）、token_mask、token_plain
        删除只清 token_plain + revoked_at；同 token 同套餐复用 id
用量：relay（access / body_filter / log 三阶段）→ unix socket → dsh-mud → usage（35 列）
      request_id 幂等 ✓ 只推 end（begin 在 lua 本地记录 ✓）✓ 失败只计数不抛错
归集：token → user / token_id（加盐哈希反查，token 不入库）；plan → provider
查看：dsh-muctl relay status|tokens|routes|credential|usage --view today|users|models|series|recent
      管理台「供给 → 用量」页（KPI + 按用户/按模型/按日）
```

踩过的坑（每条都花了时间，记录下来别再犯）：

| # | 坑 | 结论 |
| --- | --- | --- |
| 1 | 网关跑在 jail 里，**看不到宿主机 127.0.0.1** | 用量通道用 unix socket（`/run/dsh-mu/resp.sock`）并把它挂进 jail；TCP 8098 只给本机 CLI 用 |
| 2 | `ngx.timer.at(0, fn, a, b)` 的回调第一个参数是 **premature** | 签名必须是 `fn(premature, a, b)`；写错时 RPUSH 参数全错，而日志还会像"成功" |
| 3 | 只推 end，begin 在 lua 本地 | 所以 token→user 的反查必须放在 end 分支；放 begin 永远查不到 |
| 4 | dsh-mud 的 INSERT 字典漏字段，payload 有值也会被丢 | 加字段时**同时**改 INSERT 与 UPDATE 两条路 |
| 5 | `main()` 里后置的 `import threading` 让整个函数内 `threading` 变成局部变量 | 在函数开头用它 → UnboundLocalError（且被 except 吞掉）；daemon 线程启动失败**必须打日志** |
| 6 | `require "relay_usage"` 依赖 `lua_package_path`（网关没配） | 用 `dofile("/usr/local/lib/dsh-mu/lua/...")` 加载 |
| 7 | 大请求体（>缓冲）`ngx.req.get_body_data()` 返回 nil | 退回 `ngx.req.get_body_file()` 读前 8KB |
| 8 | provider 在 relay 侧拿不到 | 由 dsh-mud 用 `plan → provider` 反查；payload 不依赖 relay |
| 9 | rel="relay_source" 没进网关 config.json | 它在生成器的**白名单**里，加了 key 必须同时加白名单 |

数据质量提醒：

- `source=relay_partial` 表示"只拿到请求维度、没拿到 usage"（流式默认如此）→ token 数为 0 是**预期**，不是 0 token。
- `model` 为空只出现在 `/v1/models` 这类没有请求体的探测上。
- 早期没有 user 归集的记录（user 为空）已清理；如需保留历史，用 `--view recent` 逐条看。

### 10.1 追加三条坑（流式 token 数这一段踩的）

| # | 坑 | 结论 |
| --- | --- | --- |
| 10 | 上游把 SSE **gzip 压缩**了，而网关里的 lua **没有 zlib** | 症状极隐蔽：`body_len` 很大、`content-type` 是 event-stream，但 body 头是 `\x1f\x8b`。修法：relay 转发时 `proxy_set_header Accept-Encoding ""`（和 npm 代理同一个坑） |
| 11 | Anthropic 协议（`/v1/messages`）的用量字段名不同 | `input_tokens` / `output_tokens`，且 `message_start` 帧里嵌在 `message.usage` 下。解析必须同时认 OpenAI 的 `prompt_tokens` / `completion_tokens` |
| 12 | `stream` 不能在前段判定 | `ngx.header["content-type"]` 在 body_filter 之后才最终确定；要在 `finish()` **末尾**按 SSE 覆盖 |

**计费口径补充**（虽然本期不做计价，口径要写清）：

- `cached_tokens` 是**独立计量**（Anthropic 的 `cache_read_input_tokens` / OpenAI 的 `prompt_tokens_details.cached_tokens`），
  **不计入** `prompt_tokens`，所以它比 prompt 大是正常的（命中缓存的上下文单独算）。
- `reasoning_tokens` 计入 `completion_tokens`（各家一致），单列只是为了观测。

**#13（已知限制，不再投入）**：`usage.stream` 目前恒为 0。

- body_filter 里 `ngx.header["content-type"]` 也拿不到最终值（同一请求里上游头在更早阶段已固定），
  而请求体里 DSH 的 `/v1/messages` 又不带 `"stream": true`。
- 这个字段**不参与任何聚合**（今天/按用户/按模型/按日都用不到它），所以按"已知限制"收口。
- 真要判断是否流式：看 `endpoint = /v1/messages` 且 `source = relay`（能读到 SSE 里的 usage 就说明是流式）。
