-- dav.lua - 平台内置 WebDAV（openresty + Lua，不引入第三方模块；docs/27 §13.1）
--
-- 身份与只读由 site.lua 先完成：scope=ro 的写请求根本到不了这里。
-- 数据根：/var/lib/dsh-mu/instances/<tenant>/home（租户来自 X-DSH-MU-Tenant）
local cjson = require "cjson.safe"

local TENANT = ngx.var.site_tenant or ""
local ROOT_BASE = ngx.var.dav_root or "/var/lib/dsh-mu/instances"
local PREFIX = ngx.var.site_prefix or "/"
local ALLOW = "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, MOVE, COPY, LOCK, UNLOCK"

local function esc(s)
	return (tostring(s):gsub("[&<>\"']", function(c)
		return ({ ["&"] = "&amp;", ["<"] = "&lt;", [">"] = "&gt;", ['"'] = "&quot;", ["'"] = "&#39;" })[c]
	end))
end

local function q(s)
	return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function sh(cmd)
	local p = io.popen(cmd .. " 2>/dev/null")
	local out = ""
	if p then out = p:read("*a") or "" p:close() end
	return out
end

local function mtime_of(path)
	local out = sh("stat -c %Y " .. q(path))
	local ts = tonumber((out:gsub("%s", "")))
	return ts or os.time()
end

local function httpdate(ts)
	return os.date("!%a, %d %b %Y %H:%M:%S GMT", ts or os.time())
end

local function reply(status, body, ctype, extra)
	ngx.status = status
	ngx.header["Content-Type"] = ctype or "text/plain; charset=utf-8"
	for k, v in pairs(extra or {}) do ngx.header[k] = v end
	if body and #body > 0 then ngx.print(body) end
	return ngx.exit(status)
end

local function tenant_root()
	if not TENANT:match("^[A-Za-z0-9._-]+$") then return nil end
	return ROOT_BASE .. "/" .. TENANT
end

local function home_dir()
	local root = tenant_root()
	return root and (root .. "/home") or nil
end

local function work_dir()
	local root = tenant_root()
	return root and (root .. "/workspace") or nil
end

-- 虚拟根：/ → home；/workspace 与 /workspace/... → 实例的独立 workspace 目录
-- （真实布局是 home/ 与 workspace/ 平级；jail 里才把 workspace 挂成 home/workspace）
local function resolve(rel)
	local home = home_dir()
	local work = work_dir()
	if not home then return nil end
	if rel == "workspace" then return work end
	if rel:sub(1, 10) == "workspace/" then return work .. rel:sub(10) end
	if rel == "" then return home end
	return home .. "/" .. rel
end

local function rel_path()
	local uri = ngx.var.uri or "/"
	local p = uri
	if PREFIX ~= "/" then
		local pre = PREFIX:gsub("/$", "")
		if p:sub(1, #pre) == pre then p = p:sub(#pre + 1) end
	end
	p = p:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end)
	if p:find("%.%.") or p:find("%z") then return nil end
	p = p:gsub("^/+", ""):gsub("/+$", "")
	return p
end

local function full_path(rel)
	return resolve(rel)
end

local function is_dir(path)
	return sh("test -d " .. q(path) .. " && echo yes") == "yes\n"
end

local function is_file(path)
	return sh("test -f " .. q(path) .. " && echo yes") == "yes\n"
end

local function fsize(path)
	local out = sh("wc -c < " .. q(path))
	return tonumber((out:gsub("%s", ""))) or 0
end

local function listdir(path)
	local out = {}
	for name in sh("ls -1A " .. q(path)):gmatch("[^\n]+") do out[#out + 1] = name end
	return out
end

-- 虚拟根的顶层：home 内容 + 实例 workspace（隐藏 home 里那个空的 workspace 占位）
local function root_children()
	local out = {}
	for _, name in ipairs(listdir(home_dir())) do
		if name ~= "workspace" then out[#out + 1] = name end
	end
	if is_dir(work_dir()) then out[#out + 1] = "workspace" end
	return out
end

local function children_of(rel)
	if rel == "" then return root_children() end
	return listdir(full_path(rel))
end

local function href_of(rel, dir)
	local base = PREFIX
	if not base:match("/$") then base = base .. "/" end
	local quoted = rel:gsub("([^A-Za-z0-9%-%._~/])", function(c)
		return string.format("%%%02X", string.byte(c))
	end)
	local h = base .. quoted
	if dir and not h:match("/$") then h = h .. "/" end
	return h
end

local function prop_entry(rel)
	local path = full_path(rel)
	local dir = path and is_dir(path) or false
	local name = rel == "" and TENANT or (rel:match("([^/]+)$") or rel)
	local size = (dir or not path) and 0 or fsize(path)
	return "<D:response><D:href>" .. esc(href_of(rel, dir)) .. "</D:href>"
		.. "<D:propstat><D:prop>"
		.. "<D:displayname>" .. esc(name) .. "</D:displayname>"
		.. "<D:getcontentlength>" .. tostring(size) .. "</D:getcontentlength>"
		.. "<D:getlastmodified>" .. httpdate(path and mtime_of(path) or os.time())
		.. "</D:getlastmodified>"
		.. "<D:resourcetype>" .. (dir and "<D:collection/>" or "") .. "</D:resourcetype>"
		.. "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
end

local method = ngx.req.get_method()
local rel = rel_path()
if not rel then
	return reply(400, cjson.encode({ error = "bad path" }), "application/json")
end
local path = full_path(rel)
if not path then
	return reply(400, cjson.encode({ error = "bad tenant" }), "application/json")
end

if method == "OPTIONS" then
	return reply(200, "", "text/plain",
		{ ["DAV"] = "1, 2, 3", ["MS-Author-Via"] = "DAV", ["Allow"] = ALLOW })
end

if method == "PROPFIND" then
	if not is_dir(path) and not is_file(path) then
		return reply(404, "not found")
	end
	local depth = (ngx.var.http_depth or "1"):lower()
	if depth == "infinity" then return reply(403, "depth infinity not supported") end
	ngx.req.read_body()
	local req_body = ngx.req.get_body_data() or ""
	local propname = req_body:find("propname", 1, true) ~= nil
	local out = { '<?xml version="1.0" encoding="utf-8"?>', '<D:multistatus xmlns:D="DAV:">' }
	if propname then
		-- propname：只回属性名（这里给空 prop，够客户端发现"没有可调属性"）
		local function empty_entry(r)
			return "<D:response><D:href>" .. esc(href_of(r, is_dir(full_path(r)))) .. "</D:href>"
				.. "<D:propstat><D:prop/><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
		end
		out[#out + 1] = empty_entry(rel)
		if depth ~= "0" and is_dir(path) then
			for _, name in ipairs(children_of(rel)) do
				out[#out + 1] = empty_entry(rel == "" and name or (rel .. "/" .. name))
			end
		end
		out[#out + 1] = "</D:multistatus>"
		return reply(207, table.concat(out), 'application/xml; charset="utf-8"')
	end
	out[#out + 1] = prop_entry(rel)
	if depth ~= "0" and is_dir(path) then
		for _, name in ipairs(children_of(rel)) do
			out[#out + 1] = prop_entry(rel == "" and name or (rel .. "/" .. name))
		end
	end
	out[#out + 1] = "</D:multistatus>"
	return reply(207, table.concat(out), 'application/xml; charset="utf-8"')
end

if method == "PROPPATCH" then
	return reply(207, '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"/>',
		'application/xml; charset="utf-8"')
end

if method == "LOCK" then
	ngx.req.read_body()
	local token = "opaquelocktoken:" .. ngx.md5(TENANT .. "|" .. rel .. "|" .. tostring(ngx.now()))
	local d = ngx.shared.davlocks
	if d then d:set(token, path, 3600) end
	local body = '<?xml version="1.0" encoding="utf-8"?><D:prop xmlns:D="DAV:"><D:lockdiscovery>'
		.. '<D:activelock><D:locktoken><D:href>' .. token .. '</D:href></D:locktoken>'
		.. '<D:lockroot><D:href>' .. esc(href_of(rel, is_dir(path))) .. '</D:href></D:lockroot>'
		.. '<D:timeout>Second-3600</D:timeout></D:activelock></D:lockdiscovery></D:prop>'
	return reply(200, body, 'application/xml; charset="utf-8"', { ["Lock-Token"] = "<" .. token .. ">" })
end

if method == "UNLOCK" then
	local tok = (ngx.var.http_lock_token or ""):gsub("^<", ""):gsub(">$", "")
	local d = ngx.shared.davlocks
	if d and tok ~= "" then d:delete(tok) end
	return reply(204, "")
end

if method == "GET" or method == "HEAD" then
	if is_dir(path) then
		local rows = {}
		for _, name in ipairs(children_of(rel)) do
			local child_rel = rel == "" and name or (rel .. "/" .. name)
			rows[#rows + 1] = '<li><a href="' .. esc(href_of(child_rel, is_dir(full_path(child_rel))))
				.. '">' .. esc(name) .. "</a></li>"
		end
		local html = "<!doctype html><meta charset='utf-8'><title>文件</title><h3>租户 "
			.. esc(TENANT) .. " · " .. esc("/" .. rel) .. "</h3><ul>" .. table.concat(rows) .. "</ul>"
		return reply(200, html, "text/html; charset=utf-8")
	end
	if not is_file(path) then return reply(404, "not found") end
	local fh = io.open(path, "rb")
	if not fh then return reply(403, "cannot read") end
	local data = fh:read("*a") or ""
	fh:close()
	ngx.header["Content-Length"] = tostring(#data)
	if method == "HEAD" then return reply(200, "", "application/octet-stream") end
	return reply(200, data, "application/octet-stream")
end

if method == "PUT" then
	ngx.req.read_body()
	local body = ngx.req.get_body_data()
	if not body then
		local f = ngx.req.get_body_file()
		if f then
			local fh = io.open(f, "rb")
			if fh then body = fh:read("*a") fh:close() end
		end
	end
	body = body or ""
	local parent = path:match("^(.*)/[^/]+$")
	if parent then sh("mkdir -p " .. q(parent)) end
	local existed = is_file(path)
	local fh = io.open(path, "wb")
	if not fh then return reply(507, "cannot write") end
	fh:write(body)
	fh:close()
	return reply(existed and 204 or 201, "")
end

if method == "DELETE" then
	if is_dir(path) then sh("rm -rf " .. q(path)) return reply(204, "") end
	if not is_file(path) then return reply(404, "not found") end
	sh("rm -f " .. q(path))
	return reply(204, "")
end

if method == "MKCOL" then
	if is_dir(path) or is_file(path) then return reply(405, "already exists") end
	sh("mkdir -p " .. q(path))
	return reply(201, "")
end

if method == "MOVE" or method == "COPY" then
	local dest = ngx.var.http_destination or ""
	if dest == "" then return reply(400, "missing Destination") end
	local dpath = dest:gsub("^https?://[^/]+", "")
	if PREFIX ~= "/" then
		local pre = PREFIX:gsub("/$", "")
		if dpath:sub(1, #pre) == pre then dpath = dpath:sub(#pre + 1) end
	end
	dpath = dpath:gsub("^/+", ""):gsub("/+$", "")
	if dpath:find("%.%.") then return reply(400, "bad Destination") end
	local target = full_path(dpath)
	if not target then return reply(400, "bad Destination") end
	local parent = target:match("^(.*)/[^/]+$")
	if parent then sh("mkdir -p " .. q(parent)) end
	if method == "MOVE" then
		if os.rename(path, target) ~= true then
			sh("cp -a " .. q(path) .. " " .. q(target))
			sh("rm -rf " .. q(path))
		end
	else
		sh("cp -a " .. q(path) .. " " .. q(target))
	end
	return reply(201, "")
end

return reply(405, cjson.encode({ error = "method not allowed", method = method }), "application/json")
