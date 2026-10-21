-- logout.lua - clear the platform session and return to the login page.
local mu = require "dshmu"

ngx.header["Set-Cookie"] = {
	mu.clear_cookie(mu.SESSION_COOKIE),
	mu.clear_cookie(mu.CSRF_COOKIE),
}
ngx.header["Cache-Control"] = "no-store"
return ngx.redirect("/login", 302)
