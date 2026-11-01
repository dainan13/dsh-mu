-- relay.lua - injects the real provider key for model-plan traffic.
--
-- The instances only know a placeholder credential and a loopback URL:
--
--   http://127.0.0.1:<relay_listen>/mu-llm/<instance-token>/<route>/<provider path>
--
-- This handler resolves <token>/<route> in /etc/dsh-mu/gateway/relay.json (which is
-- never mounted into an instance jail), sets the auth header the provider wants and
-- rewrites the upstream URL. Anything it cannot resolve is refused, so the relay can
-- never be used as an open proxy.
local cjson = require "cjson.safe"

local RELAY_PATH = "/etc/dsh-mu/gateway/relay.json"

local function load_document()
	local fh = io.open(RELAY_PATH, "rb")
	if not fh then
		return nil, "relay.json missing"
	end
	local body = fh:read("*a")
	fh:close()
	local ok, doc = pcall(cjson.decode, body)
	if not ok or type(doc) ~= "table" then
		return nil, "relay.json invalid"
	end
	return doc
end

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

local doc, err = load_document()
if not doc then
	return deny(503, err or "relay unavailable")
end
local entry = (doc.tokens or {})[token]
if type(entry) ~= "table" then
	return deny(403, "unknown relay token")
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
local target = (entry.routes or {})[route]
if type(target) ~= "table" then
	return deny(403, "unknown relay route")
end
local key = tostring(target.key or "")
-- A keyless route (auth == "") is legitimate: the Kilo free gateway needs no
-- credential at all, and sending a bogus one is a 401 there. Only a route that
-- *expects* auth but has no key is an error.
local auth_kind = tostring(target.auth or "bearer")
if key == "" and auth_kind ~= "" then
	return deny(503, "relay route has no credential")
end

local upstream = tostring(target.upstream or ""):gsub("/+$", "")
local scheme, host, prefix = upstream:match("^(https?)://([^/]+)(/.*)$")
if not scheme then
	scheme, host = upstream:match("^(https?)://([^/]+)$")
	prefix = ""
end
if not scheme then
	return deny(502, "relay route has an invalid upstream")
end

-- Keep the provider's own path prefix, then the path the harness appended.
local full_path = (prefix or "") .. path
local args = ngx.var.args
local query = (args and args ~= "") and ("?" .. args) or ""
ngx.var.relay_target = scheme .. "://" .. host .. full_path .. query
ngx.var.relay_host = host
ngx.var.relay_scheme = scheme

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
