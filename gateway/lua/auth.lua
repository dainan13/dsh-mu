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

-- 三张状态页共用同一套设计外壳（cockpit 的 auth.css）：header + ring + panel + footer。
local function gate_shell(opts)
	local cfg = mu.config()
	local host = esc(cfg.gateway_host or "dsh-mu")
	local refresh = opts.refresh and ('<meta http-equiv="refresh" content="' .. opts.refresh .. '">') or ""
	return ([[<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
]] .. refresh .. [[
<title>]] .. esc(opts.title or "dsh-mu") .. [[ · DeepSeek Harness</title>
<link rel="stylesheet" href="/mu-static/auth.css">
</head>
<body>
<div class="gate">
  <div class="corners"><i></i><i></i><i></i><i></i></div>
  <header class="top">
    <div>
      <div class="mark">DeepSeek <b>Harness</b><span>Synthesize intelligence · dsh-mu gateway</span></div>
      <div class="rev">]] .. host .. [[ · web profile · <em id="clock">--:--:--</em></div>
      <div class="ticks"><i class="on"></i><i class="on"></i><i class="on"></i><i></i><i></i><i></i></div>
    </div>
    <div class="top-right">
      <div>Gateway <b>]] .. host .. [[</b></div>
      <div>Service <b>]] .. esc(opts.svc or "dsh-mu · gateway") .. [[</b></div>
    </div>
  </header>
  <main class="stage">
    ]] .. (opts.ring or "") .. [[
    <h1>]] .. esc(opts.title or "") .. [[</h1>
    <div class="en">]] .. esc(opts.en or "") .. [[</div>
    ]] .. (opts.body or "") .. [[
  </main>
  <footer class="rails">
    <div><span class="bars"><i></i><i></i><i></i></span>]] .. (opts.step or "[ 02 / 03 ] Provisioning") .. [[</div>
    <div class="c">]] .. (opts.foot_c or "") .. [[ <span class="blink"></span></div>
    <div class="r">]] .. (opts.foot_r or "") .. [[</div>
  </footer>
</div>
<script>
  const clock = document.getElementById('clock');
  if (clock) { const tick = () => { clock.textContent = new Date().toTimeString().slice(0, 8); }; tick(); setInterval(tick, 1000); }
  ]] .. (opts.script or "") .. [[
</script>
</body>
</html>
]])
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
	ngx.status = 503
	ngx.header["Content-Type"] = "text/html; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.print(gate_shell({
		title = "未分配实例", en = "No instance bound",
		svc = "dsh-mu · unbound", step = "[ 01 / 03 ] Binding",
		ring = [[<div class="ring broken">
      <svg viewBox="0 0 132 132" fill="none" aria-hidden="true">
        <circle class="dash" cx="66" cy="66" r="56" stroke-width="1" stroke-dasharray="26 14"/>
        <circle cx="66" cy="66" r="44" stroke-width="1" stroke="rgba(255,90,77,.22)"/>
        <path d="M52 52l28 28M80 52l-28 28" stroke="rgba(255,90,77,.85)" stroke-width="1.5"/>
        <path d="M66 2v9M66 121v9M2 66h9M121 66h9" class="dash" stroke-width="1" opacity=".4"/>
      </svg>
    </div>]],
		body = [[<div class="panel wide">
      <dl class="kv">
        <dt>Account</dt><dd>]] .. esc(name) .. [[</dd>
        <dt>Instance</dt><dd>—</dd>
        <dt>Status</dt><dd>no binding · 503</dd>
      </dl>
      <p class="hint" style="margin:0 0 14px;letter-spacing:.06em;text-transform:none;font-size:12px;line-height:1.8">
        账号存在，但还没有绑定 harness 实例。请让管理员执行下面的命令，然后重新登录。
      </p>
      <div class="cmd"><span>$</span>dsh-muctl instance create --user ]] .. esc(name) .. [[</div>
      <div class="row-actions">
        <button class="btn" onclick="location.href='/logout'">重新登录 · SIGN IN AGAIN</button>
      </div>
    </div>]],
		foot_c = "等待管理员分配实例", foot_r = "账号与实例是两件事：删实例不会删账号",
	}))
	return ngx.exit(503)
end

--- "instance is booting" page: it retries itself until the harness answers.
local function wait_page(inst_name, port, reason)
	ngx.status = 200
	ngx.header["Content-Type"] = "text/html; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.print(gate_shell({
		title = "实例正在启动", en = "Boot sequence in progress",
		svc = "dsh-mu · wait", refresh = "3",
		ring = [[<div class="ring progress">
      <svg viewBox="0 0 132 132" fill="none" aria-hidden="true">
        <circle class="dash" cx="66" cy="66" r="56" stroke-width="1" stroke-dasharray="2 6" opacity=".55"/>
        <circle cx="66" cy="66" r="44" stroke-width="1" stroke="rgba(239,236,230,.14)"/>
        <path class="arc" d="M66 10a56 56 0 0 1 0 112" stroke-width="2"/>
        <path d="M66 2v9M66 121v9M2 66h9M121 66h9" class="dash" stroke-width="1"/>
      </svg>
    </div>]],
		body = [[<div class="panel wide">
      <dl class="kv">
        <dt>Instance</dt><dd>]] .. esc(inst_name) .. [[</dd>
        <dt>Port</dt><dd>]] .. tostring(port) .. [[</dd>
        <dt>Reason</dt><dd>]] .. esc(reason or "等待 harness 应答") .. [[</dd>
      </dl>
      <div class="count">
        <span class="n" id="sec">3</span><span class="u">sec</span>
        <span class="note">后自动重试 · auto retry</span>
      </div>
      <div class="row-actions">
        <button class="btn primary" onclick="location.reload()">立即重试 · RETRY</button>
        <button class="btn" onclick="location.href='/logout'">退出登录 · SIGN OUT</button>
      </div>
    </div>]],
		foot_c = "自动重试中…", foot_r = "Cold start 5–20s · rebuild 后首次 30s",
		script = [[const sec = document.getElementById('sec'); let left = 3;
  setInterval(() => { left -= 1; if (left <= 0) { left = 3; } if (sec) sec.textContent = left; }, 1000);]],
	}))
	return ngx.exit(200)
end

-- Distinct from wait_page(): retrying every three seconds cannot help here, so this page
-- says what went wrong and offers a manual retry plus logout.
local function fail_page(inst_name, port, detail)
	ngx.status = 200
	ngx.header["Content-Type"] = "text/html; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.print(gate_shell({
		title = "实例启动失败", en = "Launch failed · self-check did not pass",
		svc = "dsh-mu · failed", step = "[ 03 / 03 ] Recovery",
		ring = [[<div class="ring broken fail">
      <svg viewBox="0 0 132 132" fill="none" aria-hidden="true">
        <circle class="dash" cx="66" cy="66" r="56" stroke-width="1" stroke-dasharray="10 12"/>
        <circle cx="66" cy="66" r="44" stroke-width="1" stroke="rgba(255,90,77,.22)"/>
        <path class="mark" d="M66 44v26" stroke-width="3" stroke-linecap="round"/>
        <circle class="mark" cx="66" cy="82" r="3.2"/>
        <path d="M66 2v9M66 121v9M2 66h9M121 66h9" class="dash" stroke-width="1" opacity=".35"/>
      </svg>
    </div>]],
		body = [[<div class="panel wide">
      <dl class="kv">
        <dt>Instance</dt><dd>]] .. esc(inst_name) .. [[</dd>
        <dt>Port</dt><dd>]] .. tostring(port) .. [[</dd>
        <dt>Error</dt><dd>]] .. esc(detail or "启动失败") .. [[</dd>
      </dl>
      <p class="hint" style="margin:0 0 16px">自动重试已停止 —— 反复重试帮不上忙，需要管理员看实例日志。</p>
      <div class="row-actions">
        <button class="btn primary" onclick="location.reload()">重新尝试 · RETRY</button>
        <button class="btn" onclick="location.href='/logout'">退出登录 · SIGN OUT</button>
      </div>
    </div>]],
		foot_c = "已停止自动重试", foot_r = "看实例日志：dsh-muctl instance logs " .. esc(inst_name),
	}))
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
			-- The start request itself failed (bad config, missing rootfs, ...). Retrying on
			-- a timer will not fix that, so show the failure instead of an endless spinner.
			return fail_page(inst_name, port, tostring(err))
		end
		ngx.log(ngx.NOTICE, "dshmu: auto-start requested for ", inst_name,
			" (", tostring(result.detail or "started"), ")")
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
