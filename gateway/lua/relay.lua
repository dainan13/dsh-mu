-- relay.lua - injects the real provider key for model-plan traffic.
--
-- The instances only know a placeholder credential and a loopback URL:
--
--   http://127.0.0.1:<relay_listen>/mu-llm/<instance-token>/<route>/<provider path>
--
-- This handler resolves <token>/<route> through the credential service (RESP 8098,
-- the sole source of truth: the relay.json file was retired), sets the auth header the
-- provider wants and
-- rewrites the upstream URL. Anything it cannot resolve is refused, so the relay can
-- never be used as an open proxy.
local cjson = require "cjson.safe"

local function deny(status, message)
	ngx.status = status
	ngx.header["Content-Type"] = "application/json; charset=utf-8"
	ngx.say(cjson.encode({ error = message, relay = true }))
	return ngx.exit(status)
end

local uri = ngx.var.uri or ""
-- /mu-llm/<instance-token>/<route>/<provider path>
local token, route, path = uri:match("^/mu%-llm/([^/]+)/([^/]+)(/.*)$")
if not token then
	token, route = uri:match("^/mu%-llm/([^/]+)/([^/]+)/?$")
	path = "/"
end
if not token or not route then
	return deny(404, "relay path needs /mu-llm/<token>/<route>/<path>")
end
-- 用量采集用：把 route/endpoint 放进 nginx 变量（最外层 ✓ 不受内层作用域影响 ✓）
ngx.var.relay_route = tostring(route)
ngx.var.relay_endpoint = tostring(path)

-- 凭据来源：**只有库**（RESP 问 dsh-mud）。
-- 历史包袱 relay.json 已退役：文件与库双源是今天"报 API 密钥无效"的根源 ✗
-- 可用性：把最近一次成功结果留在 shared dict 里（不设过期），库不可达时继续用它，
--         这样后端重启的几秒不会打断正在进行的模型请求 ✓
local function db_lookup(token)
	local redis = require "resty.redis"
	local r = redis:new()
	r:set_timeouts(300, 300, 300)
	local ok = r:connect("127.0.0.1", 8098)
	if not ok then return nil, "connect failed" end
	local payload = r:get("tok:" .. token)
	r:set_keepalive(1000, 16)
	if not payload or type(payload) ~= "string" then return nil, "miss" end
	local decoded_ok, doc2 = pcall(cjson.decode, payload)
	if not decoded_ok or type(doc2) ~= "table" then return nil, "bad json" end
	return doc2
end

-- 后端返回的就是 relay_document 的 token 条目（routes 是 {route: {upstream, auth, key, ...}}）
local function entry_from_db(doc2)
	if type(doc2) ~= "table" then return nil end
	if type(doc2.routes) ~= "table" then return nil end
	doc2.source = "db"
	return doc2
end

local entry
do
	local cache = ngx.shared.relay_cache
	local cached = cache and cache:get(token .. ":fresh")
	if cached then
		local ok2, parsed = pcall(cjson.decode, cached)
		if ok2 and type(parsed) == "table" then entry = entry_from_db(parsed) end
	end
	if not entry then
		local d, why = db_lookup(token)
		if d then
			entry = entry_from_db(d)
			if cache then
				cache:set(token .. ":fresh", cjson.encode(d), 30)   -- 30s 内复用
				cache:set(token .. ":last", cjson.encode(d), 3600)  -- 库挂了时的兜底
			end
		else
			-- 库不可达：用最近一次成功结果（最多 1 小时），否则明确报错
			local last = cache and cache:get(token .. ":last")
			if last then
				local ok3, parsed3 = pcall(cjson.decode, last)
				if ok3 and type(parsed3) == "table" then
					entry = entry_from_db(parsed3)
					ngx.log(ngx.WARN, "dshmu relay: credential db unreachable (",
						tostring(why), "); serving last known entry for this token")
				end
			end
			if not entry then
				ngx.log(ngx.ERR, "dshmu relay: credential db unreachable (", tostring(why),
					") and no cached entry for this token")
				return deny(503, "凭据服务不可达（dsh-mud 的 RESP 8098），请检查平台后台")
			end
		end
	end
end
if type(entry) ~= "table" then
	return deny(403, "unknown relay token")
end

-- 上游（opencode.ai 系的中转站）要求请求头 x-opencode-session ✓ 否则直接 400：
--   MissingSessionID: Request is missing x-opencode-session and cannot be routed efficiently
-- 我们按"实例账号 + token"生成一个**稳定**的会话标识（不含 token 本体 ✓ 不是随机 ✗）
-- 对所有路由都设置一次；不认这个头的上游会忽略它 ✓ 无副作用 ✓
do
	local ident = tostring(entry.user or "") .. "|" .. tostring(route or "") .. "|" .. tostring(entry.plan or "")
	local sha = require "resty.sha256"
	local h = sha:new()
	h:update(ident)
	local sess = ngx.encode_base64(h:final()):gsub("[^%w]", ""):sub(1, 32)
	ngx.req.set_header("x-opencode-session", sess)
	-- 有些中转站还认这两个（同源语义 ✓ 一起给上 ✓ 不需要就忽略 ✓）
	ngx.req.set_header("x-session-id", sess)
end

-- ---------------------------------------------------------------- plugin market
-- The platform feeds DSH's Discover tab from here: a format-1 index, npm
-- packuments, and the tarballs themselves. No provider is involved and nothing is
-- proxied, so the relay never becomes an open proxy through this route.
if route == "__market" then
	local function market_doc()
		local fh = io.open("/etc/dsh-mu/gateway/market.json", "rb")
		if not fh then return nil end
		local body = fh:read("*a")
		fh:close()
		local ok, parsed = pcall(cjson.decode, body)
		if not ok or type(parsed) ~= "table" then return nil end
		return parsed
	end
	local function send_json(value)
		ngx.header["Content-Type"] = "application/json; charset=utf-8"
		ngx.header["Cache-Control"] = "no-store"
		ngx.print(cjson.encode(value))
		return ngx.exit(200)
	end
	local function urlencode(s)
		return (tostring(s):gsub("[^%w%-%._~/]", function(c)
			return string.format("%%%02X", string.byte(c))
		end))
	end
	local function send_bytes(name)
		local path = "/var/lib/dsh-mu/market/" .. name:gsub("%%(%x%x)", function(h)
			return string.char(tonumber(h, 16))
		end)
		if path:find("%.%.") then return deny(400, "bad file name") end
		local fh = io.open(path, "rb")
		if not fh then return deny(404, "no such tarball") end
		local body = fh:read("*a")
		fh:close()
		ngx.header["Content-Type"] = "application/octet-stream"
		ngx.print(body)
		return ngx.exit(200)
	end

	local docm = market_doc()
	if not docm then
		return deny(503, "market feed is not rendered yet")
	end
	local plugins = docm.plugins or {}
	-- Absolute URLs are mandatory here: the index validator only accepts http(s), and
	-- pnpm needs a real origin for the tarball it downloads.
	local origin = "http://" .. tostring(ngx.var.http_host or (ngx.var.host .. ":" .. ngx.var.server_port))
	local base = origin .. "/mu-llm/" .. token .. "/__market"
	local registry = base .. "/registry"
	local here = ngx.var.uri:gsub("/+$", "")

	if path == "/index.json" or path == "/index.json/" or path == "/" then
		local entries = {}
		local by_name = {}
		local max_installs = 0
		for _, row in ipairs(plugins) do
			local name = tostring(row.name or "")
			local version = tostring(row.version or "")
			local file = tostring(row.file or "")
			if name ~= "" and version ~= "" and file ~= "" then
				local installs = tonumber(row.installs) or 0
				if installs > max_installs then max_installs = installs end
				local desc = tostring(row.description or "")
				if desc:match("^%s*$") then desc = name end
				local labels = nil
				if row.protected or row.managed then labels = { "platform" } end
				local entry_doc = {
					name = name,
					title = name,
					description = { en = desc, zh = desc },
					["dist-tags"] = { latest = version },
					versions = { {
						version = version,
						publishTimestamp = tonumber(docm.generated_at) or 0,
						source = { type = "npm", registry = registry },
						engines = { dsh = "*" },
					} },
					metadata = { sortKeys = { installs = installs } },
				}
				if labels then entry_doc.metadata.labels = labels end
				entries[#entries + 1] = entry_doc
				by_name[name] = entry_doc
			end
		end
		return send_json({
			format = 1,
			title = { en = "dsh-mu plugins", zh = "dsh-mu 插件" },
			description = { en = "Plugins approved on this dsh-mu platform",
				zh = "由本平台管理员添加的插件" },
			metadata = {
				sortKeys = {
					installs = { title = { en = "Installations", zh = "安装数" }, order = "desc" },
				},
				labels = {
					platform = { title = { en = "Platform managed", zh = "平台管理" } },
				},
			},
			plugins = entries,
		})
	end

	local pkg = path:match("^/registry/(.+)$")
	if pkg then
		pkg = pkg:gsub("%%2[fF]", "/"):gsub("/+$", "")
		for _, row in ipairs(plugins) do
			if tostring(row.name) == pkg then
				local version = tostring(row.version)
				local file = tostring(row.file)
				return send_json({
					name = pkg,
					["dist-tags"] = { latest = version },
					versions = {
						[version] = {
							name = pkg,
							version = version,
							dist = { tarball = base .. "/t/" .. urlencode(file) },
						},
					},
				})
			end
		end
		-- Cached metadata for the dependency tree (filled in when the admin adds a
		-- plugin). Serving it locally is what keeps pnpm's parallel resolution off the
		-- network: a burst of proxied requests used to come back 502.
		local cache_file = "/var/lib/dsh-mu/market/cache/" .. pkg:gsub("/", "__") .. ".json"
		local cfh = io.open(cache_file, "rb")
		if cfh then
			local body = cfh:read("*a")
			cfh:close()
			local okc, doc = pcall(cjson.decode, body)
			if okc and type(doc) == "table" then
				local ifh = io.open("/var/lib/dsh-mu/market/cache-index.json", "rb")
				local index = {}
				if ifh then
					local ibody = ifh:read("*a")
					ifh:close()
					local oki, parsed = pcall(cjson.decode, ibody)
					if oki and type(parsed) == "table" then index = parsed end
				end
				local mine = index[pkg] or {}
				for version, vdoc in pairs(doc.versions or {}) do
					local dist = type(vdoc) == "table" and vdoc.dist
					if type(dist) == "table" and type(dist.tarball) == "string" then
						local local_file = mine[version]
						if local_file then
							dist.tarball = base .. "/t/" .. urlencode(local_file)
						else
							dist.tarball = base .. "/npm-t/" .. urlencode(dist.tarball)
						end
					end
				end
				return send_json(doc)
			end
		end
		-- Anything not cached falls back to the public registry, one request at a time.
		local res = ngx.location.capture("/__npm/" .. pkg)
		if res.status ~= 200 then
			return deny(res.status == 0 and 502 or res.status, "package not found")
		end
		local ok, doc = pcall(cjson.decode, res.body)
		if not ok or type(doc) ~= "table" then
			return deny(502, "upstream registry returned non-JSON")
		end
		local versions = doc.versions or {}
		for _, vdoc in pairs(versions) do
			local dist = type(vdoc) == "table" and vdoc.dist
			if type(dist) == "table" and type(dist.tarball) == "string" then
				dist.tarball = base .. "/npm-t/" .. urlencode(dist.tarball)
			end
		end
		return send_json(doc)
	end

	local npm_t = path:match("^/npm%-t/(.+)$")
	if npm_t then
		local target = ngx.unescape_uri(npm_t)
		local upath = target:match("^https?://[^/]+(/.*)$")
		if not upath then return deny(400, "bad tarball url") end
		local res = ngx.location.capture("/__npmfile" .. upath)
		if res.status ~= 200 then return deny(res.status == 0 and 502 or res.status, "tarball unavailable") end
		ngx.header["Content-Type"] = "application/octet-stream"
		ngx.print(res.body)
		return ngx.exit(200)
	end

	local file = path:match("^/t/(.+)$")
	if file then
		return send_bytes(file)
	end

	return deny(404, "unknown market path")
end
local mu_route, mu_path = route, path
local target = (entry.routes or {})[route]
if type(target) ~= "table" then
	return deny(403, "unknown relay route")
end
-- 上游地址里如果还留着 {占位符}（例如 cloudflare 的 account_id 没填），
-- 直接给一句中文提示，而不是让上游回一个看不懂的 404/401 ✗
local upstream_str = tostring(target.upstream or "")
if upstream_str:find("{", 1, true) then
	local missing = upstream_str:match("{([%w_]+)}") or "参数"
	return deny(503, "该套餐还缺 " .. missing .. "：请在【套餐 → 改 key → 高级】里补上。"
		.. "当前上游地址：" .. upstream_str)
end
local key = tostring(target.key or "")
-- A keyless route (auth == "") is legitimate: the Kilo free gateway needs no
-- credential at all, and sending a bogus one is a 401 there. Only a route that
-- *expects* auth but has no key is an error.
local auth_kind = tostring(target.auth or "bearer")
if key == "" and auth_kind ~= "" then
	return deny(503, "relay route has no credential")
end

-- A route may publish one upstream base per wire protocol (opencode-go serves three, on
-- different paths). The first path segment after the route names that protocol; without
-- it the route's own `upstream` applies, which keeps instances rendered before this
-- existed working unchanged.
local function pick_protocol(rt, p)
	local protocols = type(rt) == "table" and rt.protocols
	if type(protocols) ~= "table" then return nil, nil end
	local segment, rest = p:match("^/([^/]+)(/.*)$")
	if not segment then return nil, nil end
	local base = protocols[segment]
	if type(base) ~= "string" or base == "" then return nil, nil end
	return base, rest
end

local protocol_base, protocol_path = pick_protocol(target, path)
local upstream = tostring(protocol_base or target.upstream or ""):gsub("/+$", "")
local scheme, host, prefix = upstream:match("^(https?)://([^/]+)(/.*)$")
if not scheme then
	scheme, host = upstream:match("^(https?)://([^/]+)$")
	prefix = ""
end
if not scheme then
	return deny(502, "relay route has an invalid upstream")
end

-- Keep the provider's own path prefix, then the path the harness appended (minus the
-- protocol segment when one selected the upstream above).
local full_path = (prefix or "") .. (protocol_path or path)
local args = ngx.var.args
local query = (args and args ~= "") and ("?" .. args) or ""
ngx.var.relay_target = scheme .. "://" .. host .. full_path .. query
ngx.var.relay_host = host
ngx.var.relay_scheme = scheme

-- The harness speaks the newest OpenAI shape, which uses the `developer` role. Several
-- OpenAI-compatible upstreams (DeepSeek's, for one) reject it outright:
--   422 ... messages[0].role: unknown variant `developer`, expected one of
--   system, user, assistant, tool, latest_reminder
-- Rename it to `system` before forwarding: same meaning, universally accepted.
local function normalize_message_roles()
	local ctype = ngx.var.content_type or ""
	if not ctype:find("json", 1, true) then return end
	ngx.req.read_body()
	local data = ngx.req.get_body_data()
	if not data then
		-- A real harness request (system prompt + tool schemas) is large enough that
		-- nginx buffers it to a temp file, where get_body_data() returns nil. Reading
		-- that file is what makes this work for the UI, not just for small curl probes.
		local path = ngx.req.get_body_file()
		if not path then return end
		local fh = io.open(path, "rb")
		if not fh then return end
		data = fh:read("*a")
		fh:close()
	end
	if not data or #data == 0 or #data > 16 * 1024 * 1024 then return end
	if not data:find("developer", 1, true) then return end
	local ok, doc = pcall(cjson.decode, data)
	if not ok or type(doc) ~= "table" or type(doc.messages) ~= "table" then return end
	local changed = false
	for _, m in ipairs(doc.messages) do
		if type(m) == "table" and m.role == "developer" then
			m.role = "system"
			changed = true
		end
	end
	if not changed then return end
	local encoded = cjson.encode(doc)
	ngx.req.set_body_data(encoded)
	ngx.req.set_header("Content-Length", tostring(#encoded))
	ngx.log(ngx.ERR, "dshmu relay: rewrote developer role -> system (",
		tostring(#data), " bytes in)")
end
if not ngx.ctx.dshmu_roles_normalized then
	ngx.ctx.dshmu_roles_normalized = true
	normalize_message_roles()
end

-- Pick the header the provider expects and clear the other one, so the placeholder
-- the harness sent never reaches the provider.
local auth = auth_kind
if auth == "" or key == "" then
	-- Keyless route (e.g. Kilo's free gateway): an empty header value makes nginx
	-- drop the header, and that service rejects any bogus Authorization with 401.
	ngx.var.relay_auth = ""
	ngx.var.relay_xkey = ""
elseif auth == "x-api-key" then
	ngx.var.relay_auth = ""
	ngx.var.relay_xkey = key
else
	ngx.var.relay_auth = "Bearer " .. key
	ngx.var.relay_xkey = ""
end
ngx.var.relay_plan = tostring(target.plan or "")
ngx.var.relay_instance = tostring(entry.instance or "")

-- ------------------------------------------------------------------ usage（旁路）
-- 只在这里记"维度"；token 数在 body_filter 里扫，最终在 log 阶段异步推给 dsh-mud。
-- usage_source=off 时整段跳过（出问题一条配置关掉，不影响请求）。
local usage_ok, usage = pcall(dofile, "/usr/local/lib/dsh-mu/lua/relay_usage.lua")
if usage_ok and usage then
	do
		local sha = require "resty.sha256"
			and nil   -- 不用哈希库：token_id 由 dsh-mud 侧算（同一算法 ✓）
		local body_method = ngx.req.get_method()
		local has_tools, is_stream, body_model = false, false, nil
		if body_method == "POST" then
			ngx.req.read_body()
			local raw = ngx.req.get_body_data()
			if not raw then
				-- 大请求体被 nginx 落到临时文件里了（43KB 的 prompt 就会这样 ✗）
				local tmp = ngx.req.get_body_file()
				if tmp then
					local fh = io.open(tmp, "rb")
					if fh then raw = fh:read(8192); fh:close() end
				end
			end
			if raw then
				has_tools = raw:find('"tools"', 1, true) ~= nil
				is_stream = raw:find('"stream"%s*:%s*true', 1) ~= nil
				local m = raw:match('"model"%s*:%s*"([^"]+)"')
				if m and m ~= "" then body_model = m end
			end
		end
		usage.begin({
			request_id = ngx.var.request_id or (ngx.now() .. "-" .. tostring(ngx.var.connection)),
			user = "",                       -- 由 dsh-mud 按 token 反查（不信任客户端）
			token = token,                   -- 只给 dsh-mud 查表用 ✓ **任何记录/日志都不写** ✗
			instance = tostring(entry.instance or ""),
			plan = tostring(entry.plan or target.plan or ""),
			provider = tostring(target.pkg or target.provider or ""),
			model = tostring(target.model or body_model or ""),
			route = ngx.var.relay_route or "",
			endpoint = ngx.var.relay_endpoint or "",
			surface = (ngx.var.http_x_dsh_surface or ""),
			client = (ngx.var.http_user_agent or ""):sub(1, 40),
			client_version = (ngx.var.http_x_dsh_version or ""),
			stream = is_stream
				or (ngx.req.get_headers()["accept"] or ""):find("event-stream", 1, true) ~= nil,
			has_tools = has_tools,
			token_id = "",                   -- 留空：dsh-mud 用 token 反查（不落 token 本体）
			session_id = ngx.var.cookie_dshmu_session or "",
		})
		-- token 只用于反查，绝不写进任何记录：dsh-mud 侧按 tokens 表映射成 token_id
		ngx.ctx.mu_usage.token = token
	end
end
