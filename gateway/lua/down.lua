-- down.lua - friendly page shown when the upstream instance cannot be reached.
--
-- Reached through `error_page 502 503 504 = @gw_down` after auth.lua has already
-- resolved $gw_user, so we can name the instance that failed.
local mu = require "dshmu"

local function esc(s)
	return (tostring(s):gsub("[&<>\"']", function(c)
		return ({ ["&"] = "&amp;", ["<"] = "&lt;", [">"] = "&gt;", ['"'] = "&quot;", ["'"] = "&#39;" })[c]
	end))
end

local user = ngx.var.gw_user or ""
local inst_name = ""
if user ~= "" then
	local u = mu.user(user)
	if u then inst_name = u.instance or "" end
end

local status = ngx.status
if status ~= 502 and status ~= 503 and status ~= 504 then status = 502 end

ngx.status = 503
ngx.header["Content-Type"] = "text/html; charset=utf-8"
ngx.header["Cache-Control"] = "no-store"
ngx.print([[
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>实例暂不可用</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;color:#e6e8eb;
font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
.box{max-width:560px;padding:26px 28px;background:#171a21;border:1px solid #262b36;border-radius:14px}
h1{margin:0 0 10px;font-size:18px}
p{margin:0 0 8px;color:#aeb6c2;font-size:14px}
code{background:#0f1218;border:1px solid #262b36;border-radius:6px;padding:1px 5px;color:#9ecbff}
a{color:#7aa2ff}
</style></head><body><div class="box">
<h1>实例暂不可用</h1>
<p>上游 harness 实例没有响应（网关状态 <code>]] .. tostring(status) .. [[</code>）。</p>
]])
if inst_name ~= "" then
	ngx.print('<p>实例：<code>' .. esc(inst_name) .. '</code></p>')
end
ngx.print([[
<p>它可能正在启动、刚崩溃重启，或已停止。请稍后刷新；若持续出现，请联系管理员：</p>
<p><code>dsh-muctl instance status ]] .. esc(inst_name) .. [[</code><br>
<code>dsh-muctl instance logs ]] .. esc(inst_name) .. [[</code><br>
<code>dsh-muctl instance restart ]] .. esc(inst_name) .. [[</code></p>
<p><a href="">重试</a> · <a href="/logout">退出登录</a></p>
</div></body></html>
]])
return ngx.exit(503)
