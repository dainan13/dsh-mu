-- me.lua - session-scoped JSON about the caller, for the in-page user panel.
--
--   GET  /me                 -> {user, role, admin, instance:{...}, gateway:{...}}
--   POST /me {action:...}    -> change the caller's own password, or start their instance
--
-- Only the caller's own data is ever returned; mutations are forwarded to the
-- loopback backend (which runs as root) over the token-gated internal channel.
local mu = require "dshmu"
local cjson = require "cjson.safe"

-- cjson encodes an empty Lua table as `{}`, so a plan with no model subset would
-- arrive in the browser as an object and `models.filter(...)` would throw. Anything
-- the panel treats as an array goes through here first.
local function as_array(t)
	if type(t) ~= "table" then
		return t          -- nil / cjson.null must stay null ("never probed")
	end
	if #t > 0 then
		return t
	end
	return setmetatable({}, cjson.empty_array_mt)
end

local function clean_plan(p)
	if type(p) ~= "table" then
		return p
	end
	p.models = as_array(p.models)
	p.effective_models = as_array(p.effective_models)
	-- available_models stays nil when the plan was never probed
	if type(p.available_models) == "table" then
		p.available_models = as_array(p.available_models)
	end
	return p
end

local function json(status, payload)
	ngx.status = status
	ngx.header["Content-Type"] = "application/json; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.say(cjson.encode(payload))
	return ngx.exit(status)
end

if not mu.origin_ok() then
	return json(403, { error = "cross-origin request rejected" })
end

local name, u, reason = mu.current_user()
if not name then
	return json(401, { error = "unauthorized", reason = reason or "missing" })
end

local function instance_payload()
	local inst_name = u.instance or name
	local inst = mu.instance(inst_name)
	if not inst then
		return cjson.null
	end
	local port = tonumber(inst.port) or 0
	local live = false
	if port > 0 then
		live = mu.upstream_alive("127.0.0.1", port, false)
	end
	local provider = inst.provider or {}
	-- upstreams.json is written by sync, so it cannot carry live cgroup numbers;
	-- ask the backend (the same view the admin page uses) instead.
	local resources = nil
	local started_at = tonumber(inst.started_at) or 0
	local full = mu.internal_get("/instances/" .. inst_name)
	if full and type(full.instance) == "table" then
		resources = full.instance.resources
		-- the state file is the source of truth, but the backend view is fresher
		-- (a restart updates it), so prefer a non-zero value from there.
		started_at = tonumber(full.instance.started_at) or started_at
	end
	return {
		name = inst_name,
		port = port,
		state = inst.state or "unknown",
		live = live,
		provider = provider.preset or "deepseek",
		model = provider.model or "",
		user = inst.user or name,
		resources = resources,
		started_at = started_at,
	}
end

local method = ngx.req.get_method()

if method == "GET" then
	local cfg = mu.config()
	local plans, perr = mu.internal_get("/me/plans", name)
	return json(200, {
		user = name,
		role = (mu.is_system_admin(name) and "admin" or "user"),
		admin = mu.is_system_admin(name),
		must_change = u.must_change and true or false,
		system_account = mu.is_system_admin(name),
		instance = instance_payload(),
		gateway = { host = cfg.gateway_host or "", version = "1.0.0" },
		plans = (function()
			local out = {}
			for _, p in ipairs((plans and plans.plans) or {}) do
				out[#out + 1] = clean_plan(p)
			end
			return as_array(out)
		end)(),
		defaultPlan = (plans and plans.default) or "",
		-- the user's own default (one entry, outranks the operator chain). Without this
		-- the drawer saved it and still rendered "未设置".
		my_default = (function()
			local d = plans and plans.my_default
			if type(d) ~= "table" then return { plan = "", model = "" } end
			return { plan = tostring(d.plan or ""), model = tostring(d.model or "") }
		end)(),
		providers = as_array((plans and plans.providers) or {}),
		plans_error = perr or nil,
		-- plugins are managed inside each instance's own DSH UI now; the platform
		-- only keeps a read-only view (admin) plus protected/mandatory policy.

	})
end

if method ~= "POST" then
	return json(405, { error = "method not allowed" })
end

ngx.req.read_body()
local body = ngx.req.get_body_data()
local payload = {}
if body and body ~= "" then
	local ok, parsed = pcall(cjson.decode, body)
	if not ok or type(parsed) ~= "table" then
		return json(400, { error = "invalid json" })
	end
	payload = parsed
end

local action = tostring(payload.action or "")

if action == "start-instance" then
	local inst_name = u.instance or name
	if not mu.instance(inst_name) then
		return json(404, { error = "no instance", instance = inst_name })
	end
	local result, err = mu.internal_post("/instances/" .. inst_name .. "/ensure", name)
	if not result then
		return json(502, { error = "start request failed", detail = err })
	end
	mu.forget_upstream("127.0.0.1", tonumber((mu.instance(inst_name) or {}).port) or 0)
	return json(202, { accepted = true, instance = inst_name,
	                   detail = result.detail or "starting" })
end

if action == "plugin-install" or action == "plugin-uninstall" then
	local result, err, status = mu.internal_post("/me/plugins", name, {
		action = action == "plugin-install" and "install" or "uninstall",
		pkg = payload.pkg, version = payload.version,
		admin_password = payload.admin_password,
	})
	if not result then
		local body = { error = err or "plugin request failed" }
		if status == 403 then
			body.needs_admin_password = true
			body.detail = (type(err) == "string" and err) or "需要管理员密码"
		end
		return json(status or 502, body)
	end
	return json(200, result)
end

if action == "restart" then
	-- only the caller's own instance: the backend resolves it from the actor
	local result, err, status = mu.internal_post("/me/instance/restart", name, {})
	if not result then
		return json(status or 502, { error = err or "restart failed",
			detail = (type(err) == "string" and err) or nil })
	end
	local inst_name = result.instance or name
	mu.forget_upstream("127.0.0.1", tonumber((mu.instance(inst_name) or {}).port) or 0)
	return json(202, { accepted = true, instance = inst_name,
	                   state = result.state, detail = "restarting" })
end

if action == "plan-add" or action == "plan-rename" or action == "plan-delete"
	or action == "plan-models" or action == "plan-probe"
	or action == "plan-default" or action == "plan-default-clear" then
	local result, err, status = mu.internal_post("/me/plans", name, {
		action = action:gsub("^plan%-", ""),
		id = payload.id, name = payload.name, provider = payload.provider,
		key = payload.key, models = payload.models, default_model = payload.default_model,
		model = payload.model, all_available = payload.all_available,
	})
	if not result then
		return json(status == 403 and 403 or 502, { error = err or "plan request failed",
			detail = (type(err) == "string" and err) or nil })
	end
	return json(200, result)
end

if action == "password" then
	if mu.is_system_admin(name) then
		return json(400, {
			error = "system account",
			detail = "This account uses the operating system password; change it with `passwd` on the server.",
		})
	end
	local password = tostring(payload.password or "")
	if #password < 8 then
		return json(400, { error = "password too short", detail = "at least 8 characters" })
	end
	local result, err = mu.internal_post("/me/password", name, { password = password })
	if not result then
		return json(502, { error = "password change failed", detail = err })
	end
	return json(200, { changed = true, relogin_required = true,
	                   detail = "every session of this account was invalidated; log in again" })
end

return json(400, { error = "unknown action", action = action })
