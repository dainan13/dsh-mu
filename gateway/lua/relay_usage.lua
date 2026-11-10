-- relay_usage.lua — 用量采集（旁路；绝不改响应）。
--
-- 设计：docs/23-用量观测与key入库设计.md
--   access   ：relay.lua 解析出 route/provider 后调用 M.begin(...)  → 只带维度
--   body_filter：M.filter() 增量扫上游响应里的 usage（只读 ✗ 不动 ngx.arg[1]）
--   log      ：M.finish() 组一条 end 记录 → ngx.timer 异步 RPUSH usage:end
--
-- 失败一律丢弃 + 计数（shared dict），绝不影响请求。
local cjson = require "cjson.safe"

local M = {}

local SHM = "relay_usage"
local REDIS_HOST, REDIS_PORT = "127.0.0.1", 8098
local MAX_BODY = 256 * 1024      -- 只缓存这么多用于找 usage；流式正文通常不带 usage

local function shm()
	return ngx.shared[SHM]
end

local function bump(key)
	local d = shm()
	if d then d:incr(key, 1, 0) end
end

local function now_ms()
	return math.floor(ngx.now() * 1000)
end

-- access 阶段：记录维度（不落库，先放 ngx.ctx）
function M.begin(ctx)
	ngx.ctx.mu_usage = {
		request_id     = ctx.request_id,
		ts_started     = ngx.time(),
		token_id       = ctx.token_id or "",
		session_id     = ctx.session_id or "",
		user           = ctx.user or "",
		instance       = ctx.instance or "",
		plan           = ctx.plan or "",
		provider       = ctx.provider or "",
		model          = ctx.model or "",
		route          = ctx.route or "",
		endpoint       = ctx.endpoint or "",
		surface        = ctx.surface or "",
		client         = ctx.client or "",
		client_version = ctx.client_version or "",
		stream         = ctx.stream and 1 or 0,
		has_tools      = ctx.has_tools and 1 or 0,
		started_ms     = now_ms(),
		body           = {},
		body_len       = 0,
	}
end

-- body_filter：只读扫描（流式也安全：只累积，不改数据）
function M.filter()
	local st = ngx.ctx.mu_usage
	if not st then return end
	if not st.ct then
		st.ct = string.lower(tostring(ngx.header["content-type"] or ""))
	end
	local chunk = ngx.arg[1]
	if chunk and #chunk > 0 and st.body_len < MAX_BODY then
		st.body[#st.body + 1] = chunk
		st.body_len = st.body_len + #chunk
	end
end

local function extract_usage(text)
	-- 上游可能返回 {"usage":{...}}；流式里是 data: {...}\n 逐行
	local obj = cjson.decode(text)
	if type(obj) == "table" and type(obj.usage) == "table" then return obj.usage end
	local found
	for line in text:gmatch("[^\r\n]+") do
		local payload = line:match("^data:%s*(.+)$")
		if payload and payload ~= "[DONE]" then
			local ok, row = pcall(cjson.decode, payload)
			if ok and type(row) == "table" and type(row.usage) == "table" then
				found = row.usage
			end
		end
	end
	return found
end

local SOCK = "/run/dsh-mu/resp.sock"

-- 注意：ngx.timer 的回调第一个参数是 premature，第二个才是我们的实参 ✗
-- 之前签名写成 push(queue, payload) → queue 拿到 false ✗ RPUSH 参数全错 ✗（而且在
-- "pushed false" 这样的日志里看起来像成功 ✗）
local function push(premature, queue, payload)
	if premature then return end
	local ok, err = pcall(function()
		local redis = require "resty.redis"
		local r = redis:new()
		r:set_timeouts(300, 300, 300)
		-- 首选 unix socket：网关在独立网络命名空间里，连不到宿主的 127.0.0.1
		local cok, cerr = r:connect("unix:" .. SOCK)
		if not cok then
			cok, cerr = r:connect(REDIS_HOST, REDIS_PORT)
		end
		if not cok then error(cerr or "connect") end
		local encoded = cjson.encode(payload)
		r:rpush(queue, encoded)
		r:set_keepalive(2000, 32)
	end)
	if not ok then
		bump("push_failed")
		ngx.log(ngx.ERR, "[mu-usage] push failed: ", tostring(err and err or "?"))
	else
		-- 正常情况下不写日志（每条请求一行太吵 ✗）；需要排障时把下面这行打开 ✓
		-- ngx.log(ngx.ERR, "[mu-usage] pushed ", tostring(queue))
	end
	return ok, err
end

-- log 阶段：组装 end 记录并异步推送（不带凭据；token 只出现 token_id）
function M.finish()
	local st = ngx.ctx.mu_usage
	if not st then
		ngx.log(ngx.ERR, "[mu-usage] finish without begin")
		return
	end
	local ended = ngx.time()
	local ct = tostring(st.ct or "")          -- 由 body_filter 抓取（log 阶段读不到响应头 ✗）
	local is_sse = ct:find("event-stream", 1, true) ~= nil
	local record = {
		request_id    = st.request_id,
		ts            = ended,
		ts_started    = st.ts_started,
		token_id      = st.token_id,
		session_id    = st.session_id,
		user          = st.user,
		plan          = st.plan ~= "" and st.plan or (ngx.var.relay_plan or ""),
		instance      = st.instance ~= "" and st.instance or (ngx.var.relay_instance or ""),
		provider      = st.provider,
		model         = st.model,
		route         = st.route ~= "" and st.route or (ngx.var.relay_route or ""),
		endpoint      = st.endpoint ~= "" and st.endpoint or (ngx.var.relay_endpoint or ""),
		http_status   = tonumber(ngx.status) or 0,
		latency_ms    = math.max(0, now_ms() - (st.started_ms or now_ms())),
		bytes_in      = tonumber(ngx.var.request_length) or 0,
		bytes_out     = tonumber(ngx.var.bytes_sent) or 0,
		status        = (tonumber(ngx.status) or 0) >= 400 and "upstream_error" or "ok",
		source        = "relay",
		-- 只给 dsh-mud 反查 user/token_id 用；它入库前会丢弃（绝不进 usage 表 ✗）
		token         = st.token,
	}
	-- 无论流式与否都尝试解析：不少上游（含 DeepSeek）在流式最后一帧里也带 usage ✓
	-- 只有真的解析到才把 source 记成 relay ✓ 否则保持 relay_partial ✓（不编造 ✗）
	if st.body_len > 0 then
		local text = table.concat(st.body)
		local usage = extract_usage(text)
		if usage then
			-- 两套命名都要认：OpenAI 的 prompt/completion_tokens，
			-- Anthropic 的 input_tokens/output_tokens（/v1/messages 走这套 ✓）
			record.prompt_tokens = tonumber(usage.prompt_tokens
				or usage.input_tokens
				or (usage.message and usage.message.usage and usage.message.usage.input_tokens)) or 0
			record.completion_tokens = tonumber(usage.completion_tokens
				or usage.output_tokens) or 0
			record.cached_tokens = tonumber(usage.prompt_tokens_details
				and usage.prompt_tokens_details.cached_tokens)
				or tonumber(usage.cache_read_input_tokens) or 0
			record.reasoning_tokens = tonumber(usage.completion_tokens_details
				and usage.completion_tokens_details.reasoning_tokens)
				or tonumber(usage.reasoning_tokens) or 0
			record.source = "relay"   -- 真的读到了 usage ✓（流式也一样 ✓）
		else
			record.source = "relay_partial"   -- 拿不到 usage（流式/无 usage 字段）
		end
	else
		record.source = "relay_partial"
	end
	-- stream 必须在最后判定：body_filter 之后 content-type 才最终确定 ✗
	record.stream = is_sse and 1 or (st.stream or 0)
	local d = shm()
	if d then d:incr("pushed", 1, 0) end
	local ok = ngx.timer.at(0, push, "usage:end", record)
	if not ok then bump("timer_failed") end
end

-- 供 doctor/自检读取
function M.stats()
	local d = shm()
	if not d then return {} end
	return { pushed = d:get("pushed") or 0, push_failed = d:get("push_failed") or 0,
	         timer_failed = d:get("timer_failed") or 0 }
end

return M
