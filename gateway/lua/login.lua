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
<title>需要访问许可 · DeepSeek Harness</title>
<link rel="stylesheet" href="/mu-static/auth.css">
</head>
<body>
<div class="gate">
  <div class="corners"><i></i><i></i><i></i><i></i></div>

  <header class="top">
    <div>
      <div class="mark">DeepSeek <b>Harness</b><span>Synthesize intelligence · dsh-mu gateway</span></div>
      <div class="rev">]] .. title .. [[ · web profile · <em id="clock">--:--:--</em></div>
      <div class="ticks"><i class="on"></i><i class="on"></i><i></i><i></i><i></i><i></i></div>
    </div>
    <div class="top-right">
      <div>Gateway <b>]] .. title .. [[</b></div>
      <div>Login <b>dsh-mu · host</b></div>
    </div>
  </header>

  <main class="stage">
    <div class="welcome" id="welcome">
      <div class="w1">WELCOME TO</div>
      <div class="w2">DEEPSEEK HARNESS</div>
      <div class="w3">
        <svg viewBox="0 0 50 50" aria-hidden="true"><path fill="currentColor" d="M48.8354 10.0479C48.3232 9.79199 48.1025 10.2798 47.8032 10.5278C47.7007 10.6079 47.6143 10.7119 47.5273 10.8076C46.7793 11.624 45.9048 12.1597 44.7622 12.0957C43.0923 12 41.666 12.5356 40.4058 13.8398C40.1377 12.2319 39.2476 11.272 37.8926 10.6558C37.1836 10.3359 36.4668 10.0156 35.9702 9.31982C35.6235 8.82373 35.5293 8.27197 35.356 7.72754C35.2456 7.3999 35.1353 7.06396 34.7651 7.00781C34.3633 6.94385 34.2056 7.2876 34.0479 7.57568C33.418 8.75195 33.1733 10.0479 33.1973 11.3599C33.2524 14.312 34.4736 16.6641 36.8999 18.3359C37.1758 18.5278 37.2466 18.7197 37.1597 19C36.9946 19.5757 36.7974 20.1357 36.624 20.7119C36.5137 21.0801 36.3486 21.1597 35.9624 21C34.6309 20.4321 33.481 19.5918 32.4644 18.5757C30.7393 16.8721 29.1792 14.9917 27.2334 13.52C26.7764 13.1758 26.3193 12.856 25.8467 12.5518C23.8618 10.584 26.1069 8.96777 26.627 8.77588C27.1704 8.57568 26.8159 7.8877 25.0591 7.896C23.3022 7.90381 21.6953 8.50391 19.647 9.30371C19.3477 9.42383 19.0322 9.51172 18.7095 9.58398C16.8501 9.22363 14.9199 9.14355 12.9033 9.37598C9.10596 9.80762 6.07275 11.6396 3.84326 14.7681C1.16455 18.5278 0.53418 22.7998 1.30664 27.2559C2.11768 31.9521 4.46582 35.8398 8.07373 38.8799C11.8159 42.0322 16.1255 43.5762 21.041 43.2803C24.0269 43.104 27.3516 42.6963 31.1016 39.4561C32.0469 39.936 33.0396 40.1279 34.686 40.272C35.9546 40.3921 37.1758 40.208 38.1211 40.0078C39.6021 39.688 39.4995 38.2881 38.9639 38.0322C34.623 35.9678 35.5762 36.8081 34.71 36.1279C36.9155 33.4639 40.2402 30.6958 41.54 21.728C41.6426 21.0161 41.5557 20.5679 41.54 19.9917C41.5322 19.6396 41.6108 19.5039 42.0049 19.4639C43.0923 19.3359 44.1479 19.0317 45.1167 18.4878C47.9292 16.9199 49.064 14.3438 49.3315 11.2559C49.3711 10.7837 49.3237 10.2959 48.8354 10.0479Z"/></svg>
        <span>D E E P S E E K · H A R N E S S</span>
      </div>
      <div class="en">Access permission required</div>
    </div>

    <form class="panel" method="post" action="/login" onsubmit="return pending()">
      <div class="err]] .. (error and " on" or "") .. [[" id="err">]] .. esc(error or "用户名或密码不正确") .. [[</div>
      <input type="hidden" name="next" value="]] .. esc(next_) .. [[">
      <input type="hidden" name="csrf" value="]] .. esc(csrf) .. [[">
      <div class="field">
        <label for="u">User / 用户名</label>
        <input class="input" id="u" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" autofocus required value="]] .. esc(username or "") .. [[">
      </div>
      <div class="field">
        <label for="p">Passphrase / 密码</label>
        <input class="input" id="p" name="password" type="password" autocomplete="current-password" required placeholder="••••••••">
      </div>
      <button class="btn primary block" id="go" type="submit">进入实例 · ENTER</button>
      <p class="hint">登录后进入你自己的隔离实例 · isolated instance</p>
    </form>
  </main>

  <footer class="rails">
    <div><span class="bars"><i></i><i></i><i></i></span>[ 01 / 03 ] Authentication</div>
    <div class="c">等待用户授权… <span class="blink"></span></div>
    <div class="r">Powered by DeepSeek · <b>dsh-mu</b></div>
  </footer>
</div>
<script>
  const clock = document.getElementById('clock');
  if (clock) { const tick = () => { clock.textContent = new Date().toTimeString().slice(0, 8); }; tick(); setInterval(tick, 1000); }
  function pending() {
    const u = document.getElementById('u'), p = document.getElementById('p');
    const err = document.getElementById('err'), go = document.getElementById('go');
    const welcome = document.getElementById('welcome');
    if (!u.value.trim() || !p.value.trim()) { err.classList.add('on'); if (welcome) welcome.classList.remove('pending'); return false; }
    err.classList.remove('on');
    go.disabled = true; go.textContent = '正在验证…';
    if (welcome) welcome.classList.add('pending');
    return true;   // 继续原生 POST（校验在网关侧）
  }
</script>
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
	return reply(page(next_, "尝试次数过多，请约 " .. math.ceil(LOGIN_WINDOW / 60)
		.. " 分钟后重试（或让管理员执行 dsh-muctl gateway restart）", username), 429)
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
