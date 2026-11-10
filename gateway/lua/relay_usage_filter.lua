-- filter：用 dofile 加载（require 依赖 lua_package_path，网关里没有配 → 会静默失败）
local ok, usage = pcall(dofile, "/usr/local/lib/dsh-mu/lua/relay_usage.lua")
if ok and usage then usage.filter() end
