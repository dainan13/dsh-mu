-- site.lua - 站点入口鉴权（docs/27 §6）
--
-- 两条身份通道，最后都变成同一组变量（nginx 再注入成 X-DSH-MU-*）：
--   ① Authorization: Basic/Bearer  → 平台令牌（给 OS 客户端/脚本）
--   ② 会话 cookie                  → 已登录用户（给浏览器）
-- 令牌只由平台签发/校验（/mu-internal/site/verify），站点永远看不到令牌本体。
local mu = require "dshmu"
local cjson = require "cjson.safe"

-- 客户端同名头一律由 nginx 覆盖写（见渲染片段）；这里只负责"谁被允许进来"
if not mu.origin_ok() then
	return ngx.exit(403)
end

local site = ngx.var.site_name or ""

local function deny(status, why, realm)
	ngx.status = status
	if realm then
		ngx.header["WWW-Authenticate"] = realm
	end
	ngx.header["Content-Type"] = "application/json; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.say(cjson.encode({ error = why }))
	return ngx.exit(status)
end

local function parse_token(header)
	if not header or header == "" then return nil end
	local scheme, rest = header:match("^(%S+)%s+(.+)$")
	if not scheme then return nil end
	scheme = scheme:lower()
	if scheme == "bearer" then
		return rest
	end
	if scheme == "basic" then
		local decoded = ngx.decode_base64(rest) or ""
		local user, pass = decoded:match("^([^:]*):(.*)$")
		-- 三种常见填法都接受：
		--   用户名=任意, 密码=令牌（Windows 映射网络驱动器的默认行为）
		--   用户名=令牌, 密码=任意/空
		--   用户名=令牌（某些客户端）
		local function find_token(s)
			if not s then return nil end
			local i = s:find("dsk_", 1, true)
			return i and s:sub(i) or nil
		end
		local tp = find_token(pass)
		if tp then return tp end
		local tu = find_token(user)
		if tu then return tu end
		if pass and pass ~= "" then return pass end
		return user
	end
	return nil
end

-- 只读令牌要拦写方法（安全边界在网关，不靠站点实现）
local WRITE_METHODS = {
	PUT = true, DELETE = true, MKCOL = true, MOVE = true, COPY = true,
	PROPPATCH = true, LOCK = true, UNLOCK = true,
}

local auth = ngx.var.http_authorization
local token = parse_token(auth)
if token then
	local res, err, status = mu.internal_post("/site/verify", "",
		{ site = site, token = token, ip = ngx.var.remote_addr })
	if not res then
		return deny(401, "invalid token (" .. tostring(err or status or "") .. ")",
			'Basic realm="dsh-mu site ' .. site .. '"')
	end
	ngx.var.site_tenant = res.tenant or ""
	ngx.var.site_scope = res.scope or "rw"
	ngx.var.site_subject = res.subject or ""
	ngx.var.admin_user = res.subject or ""
	ngx.var.admin_token = ""
	ngx.var.admin_must_change = "0"
	if (res.scope or "rw") == "ro" and WRITE_METHODS[ngx.req.get_method()] then
		return deny(403, "read-only token")
	end
	return
end

-- 会话通道（浏览器）
local name, u, reason = mu.current_user()
if not name then
	local accept = ngx.var.http_accept or ""
	if accept:find("text/html", 1, true) then
		return ngx.redirect("/login?next=" .. ngx.escape_uri(mu.safe_next(ngx.var.request_uri)), 302)
	end
	ngx.status = 401
	ngx.header["Content-Type"] = "application/json; charset=utf-8"
	ngx.header["Cache-Control"] = "no-store"
	ngx.header["WWW-Authenticate"] = 'Basic realm="dsh-mu site ' .. site .. '"'
	ngx.say('{"error":"unauthorized","reason":"' .. tostring(reason or "missing") .. '"}')
	return ngx.exit(401)
end

ngx.var.admin_token = ""
ngx.var.admin_user = name
ngx.var.site_subject = name
ngx.var.site_tenant = name
ngx.var.site_scope = "rw"
ngx.var.admin_must_change = (u.must_change and "1" or "0")
