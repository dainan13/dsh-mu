-- admin.lua - gate the loopback admin backend behind the platform session.
--
-- Runs in the access phase for everything under /admin/. Anonymous browsers are
-- sent to the login page; authenticated non-admins get a 403 page; API calls
-- (XHR from the admin page) get JSON instead of a redirect so the page can show
-- a proper message. On success it fills the two variables the proxy location
-- uses: the shared admin token and the acting username (for the audit trail).
local mu = require "dshmu"
local cjson = require "cjson.safe"

local function esc(s)
	return (tostring(s):gsub("[&<>\"']", function(c)
		return ({ ["&"] = "&amp;", ["<"] = "&lt;", [">"] = "&gt;", ['"'] = "&quot;", ["'"] = "&#39;" })[c]
	end))
end

--- Plain text for JSON bodies (the HTML pages carry markup; JSON must not).
local function plain(s)
	return (tostring(s):gsub("<[^>]*>", ""))
end

local function wants_json()
	local uri = ngx.var.uri or ""
	return uri:sub(1, 10) == "/admin/api" or uri == "/admin/api"
end

local function err_page(status, title, detail)
	if wants_json() then
		ngx.status = status
		ngx.header["Content-Type"] = "application/json; charset=utf-8"
		ngx.header["Cache-Control"] = "no-store"
		ngx.say(cjson.encode({ error = plain(title), detail = plain(detail) }))
		return ngx.exit(status)
	end
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
<p><a href="/">返回实例</a> · <a href="/logout">退出登录</a></p>
</div></body></html>
]])
	return ngx.exit(status)
end

if not mu.origin_ok() then
	return err_page(403, "请求来源不被信任", "跨站请求已被拒绝（Origin 与 Host 不一致）。")
end

local name, u, reason = mu.current_user()
if not name then
	if wants_json() then
		ngx.status = 401
		ngx.header["Content-Type"] = "application/json; charset=utf-8"
		ngx.header["Cache-Control"] = "no-store"
		ngx.say(cjson.encode({ error = "unauthorized", reason = tostring(reason or "missing") }))
		return ngx.exit(401)
	end
	return ngx.redirect("/login?next=" .. ngx.escape_uri(mu.safe_next(ngx.var.request_uri)), 302)
end

if (u.role or "user") ~= "admin" then
	return err_page(403, "需要管理员权限",
		"账号 <code>" .. esc(name) .. "</code> 的角色是 <code>" .. esc(u.role or "user") ..
		"</code>，不能访问管理页面。请联系管理员提权。")
end

ngx.var.admin_token = mu.admin_token()
ngx.var.admin_user = name
ngx.var.admin_must_change = (u.must_change and "1" or "0")
