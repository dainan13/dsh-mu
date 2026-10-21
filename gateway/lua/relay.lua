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
