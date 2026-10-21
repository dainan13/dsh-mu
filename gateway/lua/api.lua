-- api.lua - token-gated local ops API.
--
-- Two independent gates: the location only accepts loopback peers, and the
-- caller must present the shared admin token (?t= or X-DSH-MU-Admin). Loopback
-- alone is not enough: a browser or any local process is loopback too, and this
-- API exposes platform state. Local callers read /etc/dsh-mu/admin.token (0640
-- root:nginx); browsers cannot.
local mu = require "dshmu"
local cjson = require "cjson.safe"

local function json(status, payload)
	ngx.status = status
	ngx.header["Content-Type"] = "application/json; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.say(cjson.encode(payload))
	return ngx.exit(status)
end

local path = ngx.var.uri

-- Defensive: never serve the ops API to a non-loopback peer, regardless of config.
local addr = ngx.var.remote_addr or ""
if addr ~= "127.0.0.1" and addr ~= "::1" then
	return json(403, { error = "loopback only" })
end

-- ...and require the shared token, so a local browser cannot use this API.
local expected = mu.admin_token()
local supplied = ngx.var.arg_t
if not supplied or supplied == "" then
	supplied = ngx.var.http_x_dsh_mu_admin
end
if expected == "" or supplied ~= expected then
	ngx.log(ngx.WARN, "dshmu: /mu-api rejected without the ops token from ", addr)
	return json(403, { error = "missing or wrong ops token",
	                   hint = "pass ?t=$(cat /etc/dsh-mu/admin.token)" })
end

if path == "/mu-api/healthz" then
	return json(200, { ok = true, ts = ngx.time() })
end

if path == "/mu-api/whoami" then
	local name, u, reason = mu.current_user()
	if not name then return json(401, { error = "unauthorized", reason = reason }) end
	return json(200, { user = name, role = u.role, instance = u.instance, epoch = u.epoch })
end

if path == "/mu-api/status" then
	local users = mu.state("users") or { users = {} }
	local insts = mu.state("upstreams") or { instances = {} }
	local running = 0
	local list = {}
	for name, inst in pairs(insts.instances or {}) do
		if inst.state == "running" then running = running + 1 end
		list[#list + 1] = {
			name = name, user = inst.user, host = inst.host, state = inst.state,
			cookie = (inst.cookie and inst.cookie ~= "") and true or false,
		}
	end
	table.sort(list, function(a, b) return a.name < b.name end)
	local count = 0
	for _ in pairs(users.users or {}) do count = count + 1 end
	return json(200, {
		users = count,
		instances = list,
		running = running,
		gateway = { host = (mu.config().gateway_host or ""), ts = ngx.time() },
	})
end

return json(404, { error = "unknown endpoint", path = path })
