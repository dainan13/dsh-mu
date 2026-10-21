-- auth.lua - access-phase gate: platform session -> instance -> upstream vars.
--
-- Runs before every proxied request. It resolves the caller's harness instance
-- and fills $gw_upstream / $gw_cookie / $gw_user, which the location block uses
-- to proxy_pass to the jailed instance with an injected upstream session cookie.
local mu = require "dshmu"

local function esc(s)
	return (tostring(s):gsub("[&<>\"']", function(c)
		return ({ ["&"] = "&amp;", ["<"] = "&lt;", [">"] = "&gt;", ['"'] = "&quot;", ["'"] = "&#39;" })[c]
	end))
end

local function err_page(status, title, detail)
	ngx.status = status
	ngx.header["Content-Type"] = "text/html; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.print([[
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>]] .. esc(title) .. [[</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;color:#e6e8eb;
font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
.box{max-width:520px;padding:26px 28px;background:#171a21;border:1px solid #262b36;border-radius:14px}
h1{margin:0 0 10px;font-size:18px}
p{margin:0 0 8px;color:#aeb6c2;font-size:14px}
code{background:#0f1218;border:1px solid #262b36;border-radius:6px;padding:1px 5px;color:#9ecbff}
a{color:#7aa2ff}
</style></head><body><div class="box">
<h1>]] .. esc(title) .. [[</h1>
<p>]] .. detail .. [[</p>
<p><a href="/logout">重新登录</a></p>
</div></body></html>
]])
	return ngx.exit(status)
end

local name, u, reason = mu.current_user()
if not name then
	local uri = ngx.var.uri or "/"
	local accept = ngx.var.http_accept or ""
	if uri:sub(1, 5) == "/api/" or accept:find("application/json", 1, true) then
		ngx.status = 401
		ngx.header["Content-Type"] = "application/json; charset=utf-8"
		ngx.header["Cache-Control"] = "no-store"
		ngx.say('{"error":"unauthorized","reason":"' .. tostring(reason) .. '"}')
		return ngx.exit(401)
	end
	local next_ = mu.safe_next(ngx.var.request_uri)
	return ngx.redirect("/login?next=" .. ngx.escape_uri(next_), 302)
end

local inst_name = u.instance
if not inst_name or inst_name == "" then
	return err_page(503, "未分配实例",
		"账号 <code>" .. esc(name) .. "</code> 还没有绑定 harness 实例，请联系管理员执行 <code>dsh-muctl instance create</code>。")
end

--- "instance is booting" page: it retries itself until the harness answers.
local function wait_page(inst_name, port, reason)
	ngx.status = 200
	ngx.header["Content-Type"] = "text/html; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.print([[
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>实例正在启动</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;color:#e6e8eb;
font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
.box{max-width:520px;padding:28px 30px;background:#171a21;border:1px solid #262b36;border-radius:14px;text-align:center}
h1{margin:0 0 10px;font-size:19px}
p{margin:0 0 8px;color:#aeb6c2;font-size:14px}
code{background:#0f1218;border:1px solid #262b36;border-radius:6px;padding:1px 5px;color:#9ecbff}
.spin{width:34px;height:34px;margin:14px auto 18px;border:3px solid #2b3240;border-top-color:#4d7cfe;border-radius:50%;
animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
a{color:#7aa2ff}
</style></head><body><div class="box">
<div class="spin"></div>
<h1>实例正在启动…</h1>
<p>实例 <code>]] .. esc(inst_name) .. [[</code>（端口 <code>]] .. tostring(port) .. [[</code>）已在启动，本页每 3 秒自动重试。</p>
<p>]] .. esc(reason or "") .. [[</p>
<p><a href="/">立即重试</a> · <a href="/logout">退出登录</a></p>
</div></body></html>
]])
	return ngx.exit(200)
end

--- Ask the root-owned backend to start this instance (rate limited to once/10s).
local function try_start(inst_name, port, reason, actor)
	local d = ngx.shared.dshmu
	local key = "ensure:" .. inst_name
	if not d:get(key) then
		d:set(key, 1, 10)
		local result, err = mu.internal_post("/instances/" .. inst_name .. "/ensure", actor)
		if not result then
			ngx.log(ngx.WARN, "dshmu: auto-start of ", inst_name, " failed: ", tostring(err))
		else
			ngx.log(ngx.NOTICE, "dshmu: auto-start requested for ", inst_name,
				" (", tostring(result.detail or "started"), ")")
		end
	end
	return wait_page(inst_name, port, reason)
end

local inst = mu.instance(inst_name)
if not inst then
	return err_page(503, "实例不存在",
		"实例 <code>" .. esc(inst_name) .. "</code> 尚未在网关注册，请管理员执行 <code>dsh-muctl sync</code>。")
end

-- Verify the upstream really listens before proxying: the stored state can
-- drift (crash, out-of-band stop), and a live port wins over a stale "stopped".
local host = inst.host or ("127.0.0.1:" .. tostring(inst.port))
local port = tonumber(inst.port) or tonumber((host:match(":(%d+)$")))
local alive = port ~= nil and port > 0 and mu.upstream_alive("127.0.0.1", port, true)
if not alive then
	-- Logging in is enough to get your instance running: ask the root backend to
	-- start it and keep the browser on a self-refreshing page meanwhile.
	if port and port > 0 then
		local reason = (inst.state == "running")
			and "它声明为运行中但端口拒绝连接（可能刚崩溃），正在重新拉起。"
			or ("它当前状态为 " .. tostring(inst.state) .. "。")
		return try_start(inst_name, port, reason, name)
	end
	return err_page(503, "实例未就绪",
		"实例 <code>" .. esc(inst_name) .. "</code> 没有可用端口。<br>"
		.. "请查看 <code>dsh-muctl instance logs " .. esc(inst_name) .. "</code>。")
end
if inst.state ~= "running" then
	-- Port answers while the stored state says otherwise: route anyway and let
	-- the operator reconcile with `dsh-muctl sync-state`.
	ngx.log(ngx.WARN, "dshmu: instance ", inst_name, " is serving on port ", tostring(port),
		" but its stored state is '", tostring(inst.state), "' (run `dsh-muctl sync-state`)")
end
if not inst.cookie or inst.cookie == "" then
	return err_page(503, "实例凭据未就绪",
		"实例 <code>" .. esc(inst_name) .. "</code> 的上游会话 cookie 尚未生成。<br>"
		.. "请管理员执行 <code>dsh-muctl instance refresh-cookie " .. esc(inst_name) .. "</code>。")
end

ngx.var.gw_upstream = host
ngx.var.gw_cookie = inst.cookie
ngx.var.gw_user = name
