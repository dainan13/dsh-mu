-- dshmu.lua - shared gateway helpers for the dsh-mu platform.
--
-- Loads the CLI-rendered state views (users.json / upstreams.json) with a short
-- shared-memory cache, and implements the platform session cookie and password
-- verification (PBKDF2-HMAC-SHA256, optionally scrypt).
--
-- No external Lua dependencies beyond what OpenResty ships.
local cjson = require "cjson.safe"
local hmac = require "resty.hmac"
local bit = require "bit"

local _M = {}

local ETC = "/etc/dsh-mu"
local GW = ETC .. "/gateway"
local SECRET_PATH = ETC .. "/secret.key"

local STATE_TTL = 2          -- seconds a rendered view may be cached
local COOKIE_VERSION = "v1"
local SESSION_COOKIE = "dshmu_session"
local CSRF_COOKIE = "dshmu_csrf"
local DEFAULT_SESSION_DAYS = 7

_M.SESSION_COOKIE = SESSION_COOKIE
_M.CSRF_COOKIE = CSRF_COOKIE

local function dict()
	return ngx.shared.dshmu
end

-- ------------------------------------------------------------------ primitives

function _M.read_file(path)
	local fh = io.open(path, "r")
	if not fh then return nil end
	local data = fh:read("*a")
	fh:close()
	return data
end

local function hex_to_bin(hex)
	local out = {}
	for i = 1, #hex - 1, 2 do
		out[#out + 1] = string.char(tonumber(hex:sub(i, i + 1), 16) or 0)
	end
	return table.concat(out)
end

function _M.secret()
	local cached = dict():get("secret")
	if cached then return cached end
	local hex = _M.read_file(SECRET_PATH)
	if not hex then return nil end
	local bin = hex_to_bin((hex:gsub("%s+", "")))
	dict():set("secret", bin)
	return bin
end

local function b64url_encode(s)
	return (ngx.encode_base64(s):gsub("%+", "-"):gsub("/", "_"):gsub("=+$", ""))
end

local function b64url_decode(s)
	s = s:gsub("-", "+"):gsub("_", "/")
	local pad = #s % 4
	if pad > 0 then s = s .. string.rep("=", 4 - pad) end
	return ngx.decode_base64(s)
end

_M.b64url_encode = b64url_encode
_M.b64url_decode = b64url_decode

local function new_hmac(key)
	-- lua-resty-string's hmac is constructed with colon syntax: hmac:new(key, algo).
	return hmac:new(key, hmac.ALGOS.SHA256)
end

local function hmac_sha256(key, msg)
	return new_hmac(key):final(msg)
end

local function consteq(a, b)
	if type(a) ~= "string" or type(b) ~= "string" or #a ~= #b then return false end
	local diff = 0
	for i = 1, #a do
		diff = bit.bor(diff, bit.bxor(a:byte(i), b:byte(i)))
	end
	return diff == 0
end

--- Cryptographically strong random hex; falls back to a seeded PRNG.
local function random_bytes(n)
	local ok, rand = pcall(require, "resty.random")
	if ok then
		local b = rand.bytes(n)
		if b and #b == n then return b end
	end
	local seeded = dict():get("seeded")
	if not seeded then
		local pid = (ngx.worker and ngx.worker.pid and ngx.worker.pid()) or 0
		math.randomseed(ngx.now() * 1000 + pid + os.time())
		dict():set("seeded", 1)
	end
	local out = {}
	for _ = 1, n do out[#out + 1] = string.char(math.random(0, 255)) end
	return table.concat(out)
end

function _M.random_hex(bytes)
	local out = {}
	for i = 1, bytes do
		out[i] = string.format("%02x", random_bytes(1):byte(1))
	end
	return table.concat(out)
end

-- ------------------------------------------------------------------ state views

function _M.state(kind)
	local d = dict()
	local now = ngx.now()
	if (d:get(kind .. ":exp") or 0) > now then
		local raw = d:get(kind)
		if raw then
			local ok, obj = pcall(cjson.decode, raw)
			if ok and obj then return obj end
		end
	end
	local raw = _M.read_file(GW .. "/" .. kind .. ".json")
	if not raw then return nil end
	local ok, obj = pcall(cjson.decode, raw)
	if not ok or type(obj) ~= "table" then return nil end
	d:set(kind, raw, 30)
	d:set(kind .. ":exp", now + STATE_TTL, 30)
	return obj
end

function _M.user(name)
	local st = _M.state("users")
	return st and st.users and st.users[name] or nil
end

function _M.instance(name)
	local st = _M.state("upstreams")
	return st and st.instances and st.instances[name] or nil
end

--- Probe whether an instance's loopback port accepts connections.
--
-- `state.json` is written by the CLI and can drift from reality (a crashed
-- harness, a service stopped out of band), so the gateway verifies the port
-- before proxying instead of trusting the stored state. Results are cached in
-- the shared dict: a live upstream for ALIVE_TTL, a dead one for the shorter
-- DEAD_TTL so a restarting instance is picked up quickly.
local ALIVE_TTL, DEAD_TTL, CONNECT_TIMEOUT_MS = 5, 2, 400

function _M.upstream_alive(host, port, log_failure)
	local d = dict()
	local key = "upstream:" .. host .. ":" .. tostring(port)
	local cached = d:get(key)
	if cached ~= nil then
		return cached == 1
	end
	local sock = ngx.socket.tcp()
	sock:settimeout(CONNECT_TIMEOUT_MS)
	local ok, err = sock:connect(host, port)
	if ok then
		sock:close()
		d:set(key, 1, ALIVE_TTL)
		return true
	end
	d:set(key, 0, DEAD_TTL)
	if log_failure then
		ngx.log(ngx.WARN, "dshmu: upstream ", host, ":", tostring(port), " unreachable: ", tostring(err))
	end
	return false
end

--- Forget a cached probe result (used when an instance is restarted).
function _M.forget_upstream(host, port)
	dict():delete("upstream:" .. host .. ":" .. tostring(port))
end

--- System accounts that may administer the platform (verified via /etc/shadow).
function _M.system_admins()
	local raw = tostring(_M.config().admin_system_users or "root")
	local names = {}
	for name in raw:gmatch("[^,%s]+") do
		names[#names + 1] = name
	end
	if #names == 0 then names = { "root" } end
	return names
end

function _M.is_system_admin(name)
	for _, candidate in ipairs(_M.system_admins()) do
		if candidate == name then return true end
	end
	return false
end

--- Call the loopback admin backend from Lua.
--
-- The internal location (/mu-internal/) is gated by the shared token, which the
-- browser never sees; the acting username travels in a header for the audit log.
function _M.internal_post(path, actor, body, timeout)
	local expected = _M.admin_token()
	if expected == "" then return nil, "no admin token" end
	-- The token travels as a query argument: nginx does not inherit headers set
	-- with ngx.req.set_header into ngx.location.capture subrequests. The internal
	-- location has access_log disabled so the token never reaches a log file.
	local uri = "/mu-internal" .. path .. "?t=" .. ngx.escape_uri(expected)
		.. "&a=" .. ngx.escape_uri(actor or "")
	local res = ngx.location.capture(uri, {
		method = ngx.HTTP_POST,
		body = body and cjson.encode(body) or nil,
		ctx = ngx.ctx,
	})
	if not res then return nil, "subrequest failed" end
	if res.status >= 200 and res.status < 300 then
		local ok, parsed = pcall(cjson.decode, res.body)
		return (ok and parsed) or {}, nil
	end
	-- Third value lets callers tell "wrong password" (401) from a dead channel.
	return nil, "http " .. tostring(res.status), res.status
end

--- GET variant of internal_post (the panel reads its plan list through it).
function _M.internal_get(path, actor, timeout)
	local expected = _M.admin_token()
	if expected == "" then return nil, "no admin token" end
	local uri = "/mu-internal" .. path .. "?t=" .. ngx.escape_uri(expected)
		.. "&a=" .. ngx.escape_uri(actor or "")
	local res = ngx.location.capture(uri, { method = ngx.HTTP_GET, ctx = ngx.ctx })
	if not res then return nil, "subrequest failed" end
	if res.status >= 200 and res.status < 300 then
		local ok, parsed = pcall(cjson.decode, res.body)
		return (ok and parsed) or {}, nil
	end
	return nil, "http " .. tostring(res.status), res.status
end

--- Shared secret the gateway presents to the loopback admin backend.
function _M.admin_token()
	local cached = dict():get("admin_token")
	if cached then return cached end
	local raw = _M.read_file(ETC .. "/admin.token")
	if not raw then return "" end
	local token = (raw:gsub("%s+", ""))
	dict():set("admin_token", token, 30)
	return token
end

function _M.config()
	return _M.state("config") or {}
end

-- ---------------------------------------------------------------- session cookie

function _M.encode_session(payload)
	local body = b64url_encode(cjson.encode(payload))
	local mac = b64url_encode(hmac_sha256(_M.secret(), body))
	return COOKIE_VERSION .. "." .. body .. "." .. mac
end

function _M.decode_session(value)
	if type(value) ~= "string" then return nil end
	local ver, body, mac = value:match("^([^.]+)%.([^.]+)%.([^.]+)$")
	if ver ~= COOKIE_VERSION or not body or not mac then return nil end
	local expected = b64url_encode(hmac_sha256(_M.secret(), body))
	if not consteq(expected, mac) then return nil end
	local raw = b64url_decode(body)
	if not raw then return nil end
	local ok, payload = pcall(cjson.decode, raw)
	if not ok or type(payload) ~= "table" then return nil end
	return payload
end

--- Verify the caller's session.
-- @return username, user record, error reason
function _M.current_user()
	local raw = ngx.var["cookie_" .. SESSION_COOKIE]
	if not raw then return nil, nil, "missing" end
	local payload = _M.decode_session(raw)
	if not payload then return nil, nil, "invalid" end
	local now = ngx.time()
	if not (tonumber(payload.exp) and payload.exp > now and payload.iat <= now) then
		return nil, nil, "expired"
	end
	local u = _M.user(payload.u)
	if not u then return nil, nil, "unknown" end
	if not u.enabled then return nil, nil, "disabled" end
	if tonumber(u.epoch or 0) ~= tonumber(payload.e) then return nil, nil, "stale" end
	-- Optional idle timeout (0 = off): a session unused for longer than the window
	-- is dropped even though the signed cookie has not expired yet.
	local idle_min = tonumber((_M.config() or {}).session_idle_minutes or 0) or 0
	if idle_min > 0 and payload.sid then
		local d = ngx.shared.dshmu
		local key = "seen:" .. tostring(payload.sid)
		local seen = tonumber(d:get(key) or 0)
		if seen > 0 and (now - seen) > idle_min * 60 then
			d:delete(key)
			return nil, nil, "idle"
		end
		d:set(key, now, math.max(idle_min * 60 * 2, 3600))
	end
	return payload.u, u, nil
end

function _M.issue_session(username, u, days)
	local now = ngx.time()
	days = days or DEFAULT_SESSION_DAYS
	return _M.encode_session({
		v = 1,
		u = username,
		e = tonumber(u.epoch or 0),
		sid = _M.random_hex(16),
		iat = now,
		exp = now + days * 86400,
	}), days * 86400
end

function _M.cookie(name, value, max_age)
	local parts = { name .. "=" .. value, "Path=/", "HttpOnly", "SameSite=Lax" }
	-- Mark the session cookie Secure only when plain HTTP is redirected: otherwise a
	-- browser on http:// would receive a cookie it refuses to send back (login loop).
	local cfg = _M.config() or {}
	if cfg.tls_enabled and cfg.tls_redirect then parts[#parts + 1] = "Secure" end
	if max_age then parts[#parts + 1] = "Max-Age=" .. tostring(max_age) end
	return table.concat(parts, "; ")
end

--- True when an unsafe (state-changing) request comes from our own origin.
--
-- Browsers always send Origin on cross-origin unsafe requests, so a mismatch is
-- rejected; SameSite=Lax on the session cookie is the first line of defence and
-- this is the second. Requests without Origin (curl, the CLI, health probes)
-- are allowed through - they carry no ambient cookie from a foreign page.
function _M.origin_ok()
	local method = ngx.req.get_method()
	if method == "GET" or method == "HEAD" or method == "OPTIONS" then
		return true
	end
	local origin = ngx.var.http_origin
	if not origin or origin == "" then
		return true
	end
	local host = ngx.var.host or ""
	local scheme = ngx.var.scheme or "http"
	if origin == scheme .. "://" .. host then
		return true
	end
	-- X-Forwarded-Proto style deployments may terminate TLS in front of us.
	local forwarded = ngx.var.http_x_forwarded_proto
	if forwarded and forwarded ~= "" and origin == forwarded .. "://" .. host then
		return true
	end
	ngx.log(ngx.WARN, "dshmu: rejected ", method, " from origin ", origin, " (host ", host, ")")
	return false
end

function _M.clear_cookie(name)
	return name .. "=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
end

-- -------------------------------------------------------------------- passwords

local function int32be(i)
	return string.char(
		math.floor(i / 16777216) % 256,
		math.floor(i / 65536) % 256,
		math.floor(i / 256) % 256,
		i % 256
	)
end

local function xor_strings(a, b)
	local out = {}
	for i = 1, #a do
		out[i] = string.char(bit.bxor(a:byte(i), b:byte(i)))
	end
	return table.concat(out)
end

--- PBKDF2-HMAC-SHA256 with a single output block (32 bytes).
local function pbkdf2_sha256(password, salt, iterations)
	local h = new_hmac(password)
	h:update(salt)
	h:update(int32be(1))
	local u = h:final()
	local t = u
	for _ = 2, iterations do
		h:reset()
		h:update(u)
		u = h:final()
		t = xor_strings(t, u)
	end
	return t
end

--- Verify a `algo$params$salt$hash` record produced by dsh-muctl.
function _M.verify_password(stored, password)
	if type(stored) ~= "string" or type(password) ~= "string" then return false end
	local algo, params, salt_b64, hash_b64 = stored:match("^([%w]+)%$([^%$]*)%$([^%$]+)%$([^%$]+)$")
	if not algo then return false end
	local salt = b64url_decode(salt_b64)
	local expected = b64url_decode(hash_b64)
	if not salt or not expected then return false end
	local got
	if algo == "pbkdf2sha256" then
		local iterations = tonumber(params:match("(%d+)")) or 20000
		if iterations < 1000 or iterations > 2000000 then return false end
		local ok, res = pcall(pbkdf2_sha256, password, salt, iterations)
		if not ok then
			ngx.log(ngx.ERR, "dshmu: pbkdf2 failed: ", tostring(res))
			return false
		end
		got = res
	elseif algo == "scrypt" then
		local ok, kdf = pcall(require, "resty.openssl.kdf")
		if not ok then
			ngx.log(ngx.ERR, "dshmu: scrypt requested but resty.openssl.kdf is unavailable")
			return false
		end
		local n = tonumber(params:match("n=(%d+)")) or 16384
		local r = tonumber(params:match("r=(%d+)")) or 8
		local p = tonumber(params:match("p=(%d+)")) or 1
		local ok2, dk = pcall(kdf.derive, {
			type = "scrypt", pass = password, salt = salt, n = n, r = r, p = p, keylen = #expected,
		})
		if not ok2 or type(dk) ~= "string" then
			ngx.log(ngx.ERR, "dshmu: scrypt failed: ", tostring(dk))
			return false
		end
		got = dk
	else
		ngx.log(ngx.ERR, "dshmu: unknown password algorithm: ", tostring(algo))
		return false
	end
	return consteq(got, expected)
end

-- ------------------------------------------------------------------ rate limiting

--- Count one attempt for `key`; returns true while under `limit` per `window`.
function _M.allow_attempt(key, limit, window)
	local d = dict()
	local count, err = d:incr(key, 1, 0, window)
	if not count then
		ngx.log(ngx.WARN, "dshmu: shared dict incr failed: ", tostring(err))
		return true, 0
	end
	return count <= limit, count
end

function _M.reset_attempts(key)
	dict():delete(key)
end

--- Accept only same-site absolute paths as post-login destinations.
function _M.safe_next(value)
	if type(value) ~= "string" or value == "" then return "/" end
	if value:sub(1, 1) ~= "/" then return "/" end
	if value:sub(1, 2) == "//" then return "/" end
	if value:find("[\r\n]") then return "/" end
	return value
end

return _M
