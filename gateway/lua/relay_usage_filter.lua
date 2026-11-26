-- filter：用 dofile 加载（require 依赖 lua_package_path，网关里没有配 → 会静默失败）
local ok, usage = pcall(dofile, "/usr/local/lib/dsh-mu/lua/relay_usage.lua")
if ok and usage then usage.filter() end

-- ---------------------------------------------------------------------------
-- 上游错误"说人话"
--   上游（Cloudflare/OpenAI/…）的错误是 {"errors":[{"message":"AiError: Model …"}]}，
--   DSH 会把这坨 JSON 原样显示给用户，于是"模型免费版不可用"被看成"API 密钥无效"✗
--   这里把 4xx/5xx 的 JSON 错误体换成一句中文 + 原始上游消息，方便定位 ✓
--   注意：只拦"错误的 JSON 响应"，正常响应与流式(SSE)完全不碰 ✓
-- ---------------------------------------------------------------------------
local cjson = require "cjson.safe"

local function first_message(text)
	if type(text) ~= "string" or text == "" then return "" end
	local okj, obj = pcall(cjson.decode, text)
	if not okj or type(obj) ~= "table" then return "" end
	if type(obj.errors) == "table" and type(obj.errors[1]) == "table" then
		return tostring(obj.errors[1].message or "")
	end
	if type(obj.error) == "table" then
		return tostring(obj.error.message or obj.error.code or "")
	end
	if type(obj.error) == "string" then return obj.error end
	if type(obj.message) == "string" then return obj.message end
	return ""
end

local function humanize(status, msg)
	local tail = (msg ~= "" and ("　上游原话：" .. msg) or "")
	if status == 401 then
		return "上游拒绝访问（401）：这个 key 无效或已吊销。" .. tail
	elseif status == 403 then
		return "上游拒绝访问（403）：key 有效但没有这个模型的权限，"
			.. "或该模型不在当前套餐/计划里（云厂商常见：免费计划不支持该模型）。" .. tail
	elseif status == 404 then
		return "上游返回 404：模型 id 或接口路径不对，请核对模型名。" .. tail
	elseif status == 410 then
		return "该模型已被上游下架（410），请换一个模型。" .. tail
	elseif status == 429 then
		return "上游限流（429）：请求太密或额度用尽，稍等再试。" .. tail
	elseif status >= 500 then
		return ("上游服务异常（%d），可以稍后重试；持续失败请换模型。"):format(status) .. tail
	end
	return ("上游返回 HTTP %d。"):format(status) .. tail
end

local status = tonumber(ngx.status) or 200
local ct = string.lower(tostring(ngx.header["content-type"] or ""))
local looks_json = ct:find("json", 1, true) or ct == "" or ct == "-"
local is_stream = ct:find("event-stream", 1, true) ~= nil

if status >= 400 and looks_json and not is_stream then
	local chunk = ngx.arg[1]
	if chunk and #chunk > 0 then
		local buf = ngx.ctx.mu_err_buf or ""
		if #buf < 16384 then buf = buf .. chunk end
		ngx.ctx.mu_err_buf = buf
		ngx.arg[1] = nil                       -- 先不回给客户端
		return
	end
	if ngx.arg[2] then                          -- 结束：一次性给出改写后的正文
		local msg = first_message(ngx.ctx.mu_err_buf or "")
		local out = cjson.encode({ error = humanize(status, msg),
			upstream_message = msg, relay = true })
		ngx.arg[1] = out
		ngx.header["content-length"] = tostring(#out)
	end
	return
end
