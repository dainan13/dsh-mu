-- login.lua - platform login page (GET renders, POST verifies).
local mu = require "dshmu"

local LOGIN_LIMIT_USER = 10      -- failed attempts per username
local LOGIN_LIMIT_IP = 40        -- failed attempts per client address
local LOGIN_WINDOW = 600         -- seconds

local function esc(s)
	return (tostring(s):gsub("[&<>\"']", function(c)
		return ({ ["&"] = "&amp;", ["<"] = "&lt;", [">"] = "&gt;", ['"'] = "&quot;", ["'"] = "&#39;" })[c]
	end))
end

local function page(next_, error, username)
	local cfg = mu.config()
	local title = esc(cfg.gateway_host or "dsh-mu")
	local err_html = error and ('<p class="err">' .. esc(error) .. '</p>') or ""
	local csrf = ngx.var["cookie_" .. mu.CSRF_COOKIE]
	if not csrf or csrf == "" then
		csrf = mu.random_hex(16)
		ngx.header["Set-Cookie"] = mu.cookie(mu.CSRF_COOKIE, csrf, 3600)
	end
	return ([[<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>]] .. title .. [[ · 登录</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e8eb;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans SC",sans-serif}
.card{width:min(92vw,380px);background:#171a21;border:1px solid #262b36;border-radius:14px;padding:28px 26px;
box-shadow:0 18px 50px rgba(0,0,0,.45)}
h1{margin:0 0 4px;font-size:20px;letter-spacing:.2px}
.sub{margin:0 0 20px;color:#8b93a1;font-size:13px}
label{display:block;margin:14px 0 6px;font-size:13px;color:#aeb6c2}
input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #303643;background:#0f1218;color:#e6e8eb;font-size:14px}
input:focus{outline:none;border-color:#4d7cfe;box-shadow:0 0 0 3px rgba(77,124,254,.18)}
button{width:100%;margin-top:20px;padding:11px;border:0;border-radius:9px;background:#4d7cfe;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#3f6ce8}
.err{margin:14px 0 0;padding:9px 11px;border-radius:8px;background:rgba(255,86,86,.12);border:1px solid rgba(255,86,86,.35);color:#ff9c9c;font-size:13px}
.foot{margin-top:18px;color:#6b7280;font-size:12px;text-align:center}
</style>
</head>
<body>
<form class="card" method="post" action="/login">
  <h1>DeepSeek Harness</h1>
  <p class="sub">]] .. title .. [[ · 多实例网关</p>
  ]] .. err_html .. [[
  <input type="hidden" name="next" value="]] .. esc(next_) .. [[">
  <input type="hidden" name="csrf" value="]] .. esc(csrf) .. [[">
  <label for="u">用户名</label>
  <input id="u" name="username" autocomplete="username" autocapitalize="none" autofocus required value="]] .. esc(username or "") .. [[">
  <label for="p">密码</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">登录</button>
  <div class="foot">登录后进入你自己的隔离实例</div>
</form>
</body>
</html>
]])
end

local function reply(body, status)
	ngx.status = status
	ngx.header["Content-Type"] = "text/html; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.header["X-Frame-Options"] = "DENY"
	ngx.print(body)
	return ngx.exit(status)
end

local method = ngx.req.get_method()

if method == "GET" then
	local next_ = mu.safe_next(ngx.var.arg_next)
	-- already signed in? go straight through
	local name = mu.current_user()
	if name then return ngx.redirect(next_, 302) end
	return reply(page(next_), 200)
end

if method ~= "POST" then
	return reply(page("/", "不支持的请求方法"), 405)
end

ngx.req.read_body()
local args = ngx.req.get_post_args(20)
if type(args) ~= "table" then
	return reply(page("/", "请求格式错误"), 400)
end

local next_ = mu.safe_next(args.next)
local username = tostring(args.username or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")
local password = tostring(args.password or "")
local csrf = tostring(args.csrf or "")

if username == "" or password == "" then
	return reply(page(next_, "请输入用户名和密码", username), 400)
end

local csrf_cookie = ngx.var["cookie_" .. mu.CSRF_COOKIE] or ""
if csrf == "" or csrf ~= csrf_cookie then
	return reply(page(next_, "表单已过期，请重新提交", username), 400)
end

local ip = ngx.var.remote_addr or "?"
local ok_user = mu.allow_attempt("login:u:" .. username, LOGIN_LIMIT_USER, LOGIN_WINDOW)
local ok_ip = mu.allow_attempt("login:i:" .. ip, LOGIN_LIMIT_IP, LOGIN_WINDOW)
if not ok_user or not ok_ip then
	ngx.log(ngx.WARN, "dshmu: login rate limited user=", username, " ip=", ip)
	return reply(page(next_, "尝试次数过多，请稍后再试", username), 429)
end

-- Administrators are the host's own accounts: the password is verified against
-- /etc/shadow by the root-owned backend (the nginx worker cannot read it).
local u
if mu.is_system_admin(username) then
	local result, err, status = mu.internal_post("/auth/verify", username,
		{ user = username, password = password })
	if not result and status == 401 then
		ngx.log(ngx.WARN, "dshmu: failed system login user=", username, " ip=", ip)
		return reply(page(next_, "用户名或密码错误", username), 401)
	end
	if not result then
		ngx.log(ngx.ERR, "dshmu: system password check unavailable: ", tostring(err))
		return reply(page(next_, "认证服务暂时不可用（" .. esc(tostring(err)) .. "），请稍后再试",
			username), 503)
	end
	if not result.ok then
		ngx.log(ngx.WARN, "dshmu: failed system login user=", username, " ip=", ip,
			" reason=", tostring(result.error))
		return reply(page(next_, "用户名或密码错误", username), 401)
	end
	u = mu.user(username)
	if not u or not u.enabled then
		-- The host account is configured as an administrator but has no platform
		-- record yet: the session could not be validated afterwards, so ask the
		-- operator to run the one command that creates it.
		ngx.log(ngx.ERR, "dshmu: admin ", username, " has no platform record")
		return reply(page("/", "管理员记录缺失，请在服务器上执行：dsh-muctl bootstrap", username), 503)
	end
	next_ = (next_ == "/") and "/admin/" or next_
else
	u = mu.user(username)
	if not u or not u.enabled or not mu.verify_password(u.pw, password) then
		ngx.log(ngx.WARN, "dshmu: failed login user=", username, " ip=", ip)
		return reply(page(next_, "用户名或密码错误", username), 401)
	end
	-- Platform accounts are never administrators: admin is locked to the
	-- configured system accounts (ADMIN_SYSTEM_USERS, default root).
	u = { pw = u.pw, role = "user", enabled = u.enabled, epoch = u.epoch,
	      instance = u.instance, must_change = u.must_change }
end

local value, max_age = mu.issue_session(username, u, tonumber(mu.config().session_days) or 7)
mu.reset_attempts("login:u:" .. username)
ngx.header["Set-Cookie"] = {
	mu.cookie(mu.SESSION_COOKIE, value, max_age),
	mu.clear_cookie(mu.CSRF_COOKIE),
}
-- A bootstrap admin must replace the one-time password: land them on the admin
-- page, which shows the change-password banner.
if u.must_change and (u.role or "user") == "admin" and next_ == "/" then
	next_ = "/admin/"
end
ngx.log(ngx.NOTICE, "dshmu: login ok user=", username, " ip=", ip, " next=", next_)
return ngx.redirect(next_, 302)
