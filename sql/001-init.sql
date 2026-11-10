-- dsh-mu 状态库（SQLite, WAL）。schema 版本见 kv.schema_version。
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── 身份：谁（最大维度优先）────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
  name        TEXT PRIMARY KEY,
  created_at  INTEGER NOT NULL DEFAULT 0,
  note        TEXT
);

-- ── relay token：网关按它认人（之前 relay.json 里只有 instance，没有 user）
-- 安全设计（用户要求，见 docs/23 §2）：
--   · id 不用自增 ✗（自增会暴露"换了多少次 token"✗）
--   · id = tk_ + sha256("id\x00" ‖ install_salt ‖ plan ‖ token)[:16]  ← 域分离，和 token_hash 不同源
--   · token_hash = sha256(install_salt ‖ plan ‖ token)  ← 同 token + 同套餐 永远命中同一行（id 可复用）
--   · token_mask = 前 4 + … + 后 4（反显，不可逆）
--   · 删除只清 token_plain + 写 revoked_at（id/hash/mask 保留 → 用量历史不断）
CREATE TABLE IF NOT EXISTS tokens (
  id           TEXT PRIMARY KEY,            -- tk_xxxxxxxxxxxxxxxx（非自增 ✓ 不含时间/顺序信息）
  token_hash   TEXT NOT NULL UNIQUE,        -- 加了安装盐的 hash（同 token+套餐 → 同一行）
  token_mask   TEXT NOT NULL DEFAULT '',    -- 反显字符串
  token_plain  TEXT,                        -- 真 token；revoked 后置 NULL（行保留）
  user         TEXT NOT NULL DEFAULT '',
  instance     TEXT NOT NULL DEFAULT '',
  plan         TEXT NOT NULL DEFAULT '',
  created_at   INTEGER NOT NULL DEFAULT 0,
  revoked_at   INTEGER,
  legacy       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tokens_user ON tokens(user);

-- ── provider 凭据（一 provider 一把 key，多 token 共用）
CREATE TABLE IF NOT EXISTS credentials (
  provider    TEXT PRIMARY KEY,
  key_cipher  TEXT NOT NULL,
  updated_at  INTEGER NOT NULL DEFAULT 0
);

-- ── token + 路由名 → provider（原 relay.json 的 routes）
CREATE TABLE IF NOT EXISTS routes (
  token     TEXT NOT NULL,
  route     TEXT NOT NULL,
  provider  TEXT NOT NULL,
  upstream  TEXT NOT NULL,
  PRIMARY KEY (token, route)
);

-- ── 单行状态：凭据版本号，网关缓存靠它失效（改 key 后 ≤1 个请求生效）
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
INSERT OR IGNORE INTO kv(k, v) VALUES ('credential_version', '1');
INSERT OR IGNORE INTO kv(k, v) VALUES ('schema_version', '1');

-- ── 用量事件：一次上游请求一行（维度不嫌多；先记事实，怎么聚合以后再说）
CREATE TABLE IF NOT EXISTS usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id        TEXT NOT NULL UNIQUE,          -- 网关生成的随机 id；两段式落库的幂等键
  ts_started        INTEGER NOT NULL DEFAULT 0,    -- access 阶段（请求到达）
  ts                INTEGER NOT NULL DEFAULT 0,    -- log 阶段（请求结束）；没 end 时 = ts_started
  source            TEXT NOT NULL DEFAULT 'relay', -- relay | relay_partial | gateway_log | client
  aborted           INTEGER NOT NULL DEFAULT 0,    -- 1 = 只有 begin 没有 end（中断/超时）
  token_id          TEXT NOT NULL DEFAULT '',      -- ↦ tokens.token_id（**不是 token 本体**）
  session_id        TEXT NOT NULL DEFAULT '',      -- ↦ sessions.id（"一次会话里用了多少"）
  user              TEXT NOT NULL DEFAULT '',
  instance          TEXT NOT NULL DEFAULT '',
  plan              TEXT NOT NULL DEFAULT '',      -- 当时用的套餐（快照，不 join 实时表）
  provider          TEXT NOT NULL DEFAULT '',
  model             TEXT NOT NULL DEFAULT '',
  route             TEXT NOT NULL DEFAULT '',      -- 网关侧路由名
  endpoint          TEXT NOT NULL DEFAULT '',      -- /v1/chat/completions · /v1/messages …
  surface           TEXT NOT NULL DEFAULT '',      -- web / cli / api / unknown（有就记）
  client            TEXT NOT NULL DEFAULT '',      -- 客户端粗分/名字（UA 或 x-dsh-client）
  client_version    TEXT NOT NULL DEFAULT '',      -- DSH / CLI 版本，看"谁升了、谁没升"
  stream            INTEGER NOT NULL DEFAULT 0,    -- 是否流式
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens     INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
  latency_ms        INTEGER NOT NULL DEFAULT 0,    -- 网关看的总耗时
  upstream_ms       INTEGER NOT NULL DEFAULT 0,    -- 上游耗时（不含网关排队）
  ttft_ms           INTEGER NOT NULL DEFAULT 0,    -- 首字延迟（流式才有）
  attempts          INTEGER NOT NULL DEFAULT 1,    -- 失败重试/换 key 的次数
  tool_calls        INTEGER NOT NULL DEFAULT 0,    -- 本次用了几次工具（agentic 强度）
  has_tools         INTEGER NOT NULL DEFAULT 0,    -- 请求里是否声明了工具
  image_inputs      INTEGER NOT NULL DEFAULT 0,    -- 带图数量（多模态强度）
  stop_reason       TEXT NOT NULL DEFAULT '',      -- stop / length / tool_use / error
  status            TEXT NOT NULL DEFAULT 'ok',    -- ok | auth_fail | upstream_error | timeout
  http_status       INTEGER NOT NULL DEFAULT 0,
  bytes_in          INTEGER NOT NULL DEFAULT 0,
  bytes_out         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_ts       ON usage(ts);
CREATE INDEX IF NOT EXISTS usage_user_ts  ON usage(user, ts);
CREATE INDEX IF NOT EXISTS usage_inst_ts  ON usage(instance, ts);
CREATE INDEX IF NOT EXISTS usage_model_ts ON usage(provider, model, ts);
CREATE INDEX IF NOT EXISTS usage_status   ON usage(status, ts);
CREATE INDEX IF NOT EXISTS usage_token_ts ON usage(token_id, ts);
CREATE INDEX IF NOT EXISTS usage_session  ON usage(session_id);
CREATE INDEX IF NOT EXISTS usage_plan_ts  ON usage(plan, ts);

-- ── 会话/活动（"人到底用没用"：有请求≠在用；会话数、活跃时长更接近真相）
CREATE TABLE IF NOT EXISTS sessions (
  id             TEXT PRIMARY KEY,     -- 网关会话 id（dshmu_session 的那个 id，不是 cookie 值）
  user           TEXT NOT NULL,
  instance       TEXT NOT NULL DEFAULT '',
  started_at     INTEGER NOT NULL DEFAULT 0,
  last_seen      INTEGER NOT NULL DEFAULT 0,
  requests       INTEGER NOT NULL DEFAULT 0,
  tokens         INTEGER NOT NULL DEFAULT 0,
  active_minutes INTEGER NOT NULL DEFAULT 0,  -- 有请求的"分钟"数：比请求数更接近"用了多久"
  client         TEXT NOT NULL DEFAULT '',    -- browser / cli / unknown
  client_version TEXT NOT NULL DEFAULT '',
  first_model    TEXT NOT NULL DEFAULT '',
  token_id       TEXT NOT NULL DEFAULT ''     -- 这次会话用的是哪把 key（脱敏 id）
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user, last_seen);
CREATE INDEX IF NOT EXISTS sessions_last ON sessions(last_seen);

-- ── 工具/能力使用（插件与扩展到底有没有人用）
CREATE TABLE IF NOT EXISTS events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  user     TEXT NOT NULL DEFAULT '',
  kind     TEXT NOT NULL,              -- plugin.install | plugin.use | ext.action | login | …
  target   TEXT NOT NULL DEFAULT '',   -- 插件名/扩展名/动作名
  detail   TEXT NOT NULL DEFAULT ''    -- 短 JSON 或自由文本（不放敏感值）
);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_user_ts ON events(user, ts);

-- ── 日汇总（便宜查询；由 dsh-mud 定期 upsert，不靠物化视图）
CREATE TABLE IF NOT EXISTS usage_daily (
  day         TEXT NOT NULL,           -- YYYY-MM-DD（本地时区）
  user        TEXT NOT NULL DEFAULT '',
  instance    TEXT NOT NULL DEFAULT '',
  provider    TEXT NOT NULL DEFAULT '',
  model       TEXT NOT NULL DEFAULT '',
  token_id    TEXT NOT NULL DEFAULT '',
  requests    INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  errors      INTEGER NOT NULL DEFAULT 0,
  tool_calls  INTEGER NOT NULL DEFAULT 0,
  images      INTEGER NOT NULL DEFAULT 0,
  active_minutes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, user, instance, provider, model, token_id)
);

-- ── 计价：**留着但不启用**（当前规划只算用量；等需要时再填，usage 里不再依赖它）
CREATE TABLE IF NOT EXISTS prices (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  provider     TEXT NOT NULL,
  model        TEXT NOT NULL,
  in_per_mtok  INTEGER NOT NULL DEFAULT 0,
  out_per_mtok INTEGER NOT NULL DEFAULT 0,
  currency     TEXT NOT NULL DEFAULT 'CNY',
  effective_from INTEGER NOT NULL DEFAULT 0,
  note         TEXT
);
