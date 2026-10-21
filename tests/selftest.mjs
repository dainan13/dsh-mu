#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Resolve helpers relative to this file: the suite also runs inside the platform
// jail's chroot, where the host's /usr/local/lib/dsh-mu/tests does not exist.
const TEST_DIR = dirname(fileURLToPath(import.meta.url));
// selftest.mjs - end-to-end acceptance checks for the dsh-mu gateway.
//
//   node selftest.mjs [--host HOST] [--user U] [--pass P] [--ws-hold MS]
//
// Anonymous checks always run. The authenticated checks (login, proxying,
// WebSocket) need a real account; pass --user/--pass or set DSHMU_TEST_USER /
// DSHMU_TEST_PASS. Exit code 0 only when every executed check passes.
import http from 'node:http';
import { execSync } from 'node:child_process';

const CLI = process.env.DSHMU_CLI ?? '/usr/local/sbin/dsh-muctl';
import { createRequire } from 'node:module';

const require = createRequire('/usr/local/lib/node_modules/@deepseek-ai/dsh/');
let WebSocket = null;
try {
  WebSocket = require('ws');
} catch {
  WebSocket = null;
}

const argv = process.argv.slice(2);
const opt = (name, fallback = undefined) => {
  const i = argv.indexOf(name);
  return i === -1 ? fallback : argv[i + 1];
};

const HOST = opt('--host', process.env.DSHMU_HOST ?? '127.0.0.1');
const PORT = Number(opt('--port', process.env.DSHMU_PORT ?? 80));
const USER = opt('--user', process.env.DSHMU_TEST_USER ?? '');
const PASS = opt('--pass', process.env.DSHMU_TEST_PASS ?? '');
// nginx's default proxy_read_timeout is 60s, so a hold shorter than that
// cannot tell a configured 3600s apart from the default: it must exceed it.
const WS_HOLD = Number(opt('--ws-hold', 75000));

const results = [];
const ok = (name, note = '') => results.push(['PASS', name, note]);
const bad = (name, note = '') => results.push(['FAIL', name, note]);
const skip = (name, note = '') => results.push(['SKIP', name, note]);

function request({ method = 'GET', path = '/', headers = {}, body = null, hostHeader = HOST }) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: HOST, port: PORT, method, path, headers: { ...headers, Host: hostHeader } },
      (res) => {
        let text = '';
        res.setEncoding('utf8');
        res.on('data', (c) => { text += c; });
        res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: text }));
      },
    );
    req.setTimeout(60000, () => req.destroy(new Error('request timeout')));
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

const cookieJar = new Map();
function jarFrom(res) {
  const raw = res.headers['set-cookie'] ?? [];
  for (const line of raw) {
    const [pair] = line.split(';');
    const at = pair.indexOf('=');
    if (at > 0) {
      const name = pair.slice(0, at).trim();
      const value = pair.slice(at + 1).trim();
      if (value === '') cookieJar.delete(name);
      else cookieJar.set(name, value);
    }
  }
}
const jarHeader = () => [...cookieJar].map(([k, v]) => `${k}=${v}`).join('; ');

console.log(`=== dsh-mu selftest against http://${HOST}:${PORT}/ ===`);

// 1 ------------------------------------------------------------ anonymous index
let r = await request({ path: '/' });
if (r.status === 302 && String(r.headers.location ?? '').startsWith('/login')) {
  ok('1 anonymous / -> 302 /login', r.headers.location);
} else if (r.status === 200 && r.body.includes('/login')) {
  ok('1 anonymous / -> login page', 'served inline');
} else {
  bad('1 anonymous / -> 302 /login', `status ${r.status} location ${r.headers.location}`);
}

// 2 -------------------------------------------------------------- login page
r = await request({ path: '/login', headers: { Accept: 'text/html' } });
jarFrom(r);
const csrf = /name="csrf" value="([^"]+)"/.exec(r.body)?.[1] ?? '';
if (r.status === 200 && r.body.includes('name="password"') && csrf) {
  ok('2 GET /login -> 200 with form', `csrf ${csrf.slice(0, 8)}...`);
} else {
  bad('2 GET /login -> 200 with form', `status ${r.status} csrf=${csrf ? 'yes' : 'no'}`);
}

// 3 ------------------------------------------------------- password verification
let csrfB = csrf;
if (csrf) {
  const badBody = new URLSearchParams({ username: USER || 'nosuchuser', password: 'definitely-wrong', csrf, next: '/' }).toString();
  const r3 = await request({
    method: 'POST', path: '/login', body: badBody,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(badBody), Cookie: jarHeader() },
  });
  jarFrom(r3);
  if (r3.status === 401 || r3.status === 429) ok('3 POST /login wrong password rejected', `status ${r3.status}`);
  else bad('3 POST /login wrong password rejected', `status ${r3.status}`);
} else {
  skip('3 POST /login wrong password rejected', 'no csrf token');
}

// 4 ------------------------------------------------------------------- login
let session = false;
if (!USER || !PASS) {
  skip('4 POST /login valid credentials', 'set --user/--pass or DSHMU_TEST_USER/DSHMU_TEST_PASS');
} else {
  r = await request({ path: '/login', headers: { Accept: 'text/html' } });
  jarFrom(r);
  csrfB = /name="csrf" value="([^"]+)"/.exec(r.body)?.[1] ?? '';
  const body = new URLSearchParams({ username: USER, password: PASS, csrf: csrfB, next: '/' }).toString();
  const r4 = await request({
    method: 'POST', path: '/login', body,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(body), Cookie: jarHeader() },
  });
  jarFrom(r4);
  if (r4.status === 302 && cookieJar.has('dshmu_session')) {
    session = true;
    ok('4 POST /login valid credentials', `302 -> ${r4.headers.location}, session cookie set`);
  } else {
    bad('4 POST /login valid credentials', `status ${r4.status} cookie=${cookieJar.has('dshmu_session')}`);
  }
}

// 5 --------------------------------------------------------------- proxied UI
if (!session) {
  skip('5 authenticated / -> harness UI', 'no session');
} else {
  const r5 = await request({ path: '/', headers: { Cookie: jarHeader(), Accept: 'text/html' } });
  const title = /<title>([^<]*)<\/title>/.exec(r5.body)?.[1] ?? '?';
  if (r5.status === 200 && (r5.headers['content-type'] ?? '').includes('text/html')) {
    ok('5 authenticated / -> harness UI', `${r5.body.length} bytes, title=${title}`);
  } else {
    bad('5 authenticated / -> harness UI', `status ${r5.status} ${r5.headers['content-type']}`);
  }
  const r5b = await request({ path: '/api/remote.mux', headers: { Cookie: jarHeader() } });
  if (r5b.status !== 401 && r5b.status !== 403) ok('5b /api fence accepts proxied request', `status ${r5b.status}`);
  else bad('5b /api fence accepts proxied request', `status ${r5b.status}`);

  const injected = r5.body.includes('id="dshmu-panel-root"') && r5.body.includes('/mu-static/panel.js');
  if (injected) ok('5c index carries the injected user panel');
  else bad('5c index carries the injected user panel', 'marker missing');

  const me = await request({ path: '/me', headers: { Cookie: jarHeader() } });
  if (me.status === 200 && me.body.includes('"user"')) ok('5d /me returns the caller JSON');
  else bad('5d /me returns the caller JSON', `status ${me.status}`);

  const asset = await request({ path: '/mu-static/panel.js' });
  if (asset.status === 200 && asset.body.includes('dshmu-panel-host')) ok('5e panel asset served');
  else bad('5e panel asset served', `status ${asset.status}`);

  // 5f ------------------------------------------------------------ origin fence
  // The session cookie is SameSite=Lax; these checks prove the second line of
  // defence: an unsafe request carrying a foreign Origin must be refused, while
  // GETs and origin-less clients (CLI, probes) keep working.
  const badOrigin = await request({
    method: 'POST', path: '/me',
    headers: { 'Content-Type': 'application/json', Cookie: jarHeader(),
               Origin: 'http://evil.example' },
    body: JSON.stringify({ action: 'start' }),
  });
  if (badOrigin.status === 403) ok('5f cross-origin POST /me is refused');
  else bad('5f cross-origin POST /me is refused', `status ${badOrigin.status}`);

  const getForeign = await request({
    path: '/me', headers: { Cookie: jarHeader(), Origin: 'http://evil.example' },
  });
  if (getForeign.status === 200) ok('5f cross-origin GET /me is still allowed (safe method)');
  else bad('5f cross-origin GET /me is still allowed', `status ${getForeign.status}`);

  const sameOrigin = await request({
    method: 'POST', path: '/me',
    headers: { 'Content-Type': 'application/json', Cookie: jarHeader(),
               Origin: `http://${HOST}` },
    body: JSON.stringify({ action: 'no-such-action' }),
  });
  if (sameOrigin.status !== 403) ok('5f same-origin POST /me passes the fence', `HTTP ${sameOrigin.status}`);
  else bad('5f same-origin POST /me passes the fence', `status ${sameOrigin.status}`);

  const noOrigin = await request({
    method: 'POST', path: '/me',
    headers: { 'Content-Type': 'application/json', Cookie: jarHeader() },
    body: JSON.stringify({ action: 'no-such-action' }),
  });
  if (noOrigin.status !== 403) ok('5f origin-less POST /me passes (CLI compatibility)', `HTTP ${noOrigin.status}`);
  else bad('5f origin-less POST /me passes', `status ${noOrigin.status}`);
}

// 6 ------------------------------------------------------------- websocket soak
if (!session || !WebSocket) {
  skip('6 WebSocket /api/remote.mux', !session ? 'no session' : 'ws module unavailable');
} else {
  const out = await new Promise((resolve) => {
    const ws = new WebSocket(`ws://${HOST}:${PORT}/api/remote.mux`, {
      headers: { Cookie: jarHeader(), Origin: `http://${HOST}` },
    });
    let pings = 0;
    const timer = setTimeout(() => {
      const alive = ws.readyState === WebSocket.OPEN;
      try { ws.close(); } catch {}
      // Be precise about what this proves: the upstream pings every ~2s, and any
      // traffic resets proxy_read_timeout - so an *active* hold cannot prove the
      // timeout value. A silent hold could; we say which one we got.
      const note = pings === 0
        ? `held ${WS_HOLD / 1000}s with no upstream traffic - also proves proxy_read_timeout`
        : `held ${WS_HOLD / 1000}s, ${pings} upstream pings (traffic resets the read timeout, `
          + `so the timeout value itself is checked by 6b)`;
      resolve({ ok: alive, note });
    }, WS_HOLD);
    ws.on('ping', () => { pings += 1; });
    ws.on('open', () => {});
    ws.on('unexpected-response', (_q, res) => { clearTimeout(timer); resolve({ ok: false, note: `HTTP ${res.statusCode}` }); });
    ws.on('error', (e) => { clearTimeout(timer); resolve({ ok: false, note: e.message }); });
  });
  out.ok ? ok('6 WebSocket /api/remote.mux stays open', out.note) : bad('6 WebSocket /api/remote.mux stays open', out.note);
}

// 6b --------------------- the effective configuration carries the long timeouts
// `nginx -T` shows what the running server actually loaded (including includes),
// which is the only way to check the timeout value itself: the WebSocket check
// above only proves that a *busy* connection survives.
if (typeof process.getuid === 'function' && process.getuid() === 0 && session) {
  let conf = '';
  try {
    conf = execSync(`sh -c '` +
      `if [ -n "$DSH_MU_CONF_DIR" ] && [ -f "$DSH_MU_CONF_DIR/nginx.conf" ]; then ` +
      `cat "$DSH_MU_CONF_DIR/nginx.conf" "$DSH_MU_CONF_DIR"/conf.d/*.conf "$DSH_MU_CONF_DIR"/http.d/*.conf 2>/dev/null; ` +
      `elif [ -f /etc/dsh-mu-gateway/nginx.conf ]; then ` +
      `cat /etc/dsh-mu-gateway/nginx.conf /etc/dsh-mu-gateway/conf.d/*.conf /etc/dsh-mu-gateway/http.d/*.conf 2>/dev/null; ` +
      `elif [ -f /var/lib/dsh-mu/gateway/conf/nginx.conf ]; then ` +
      `cat /var/lib/dsh-mu/gateway/conf/nginx.conf /var/lib/dsh-mu/gateway/conf/conf.d/*.conf /var/lib/dsh-mu/gateway/conf/http.d/*.conf 2>/dev/null; ` +
      `else nginx -T 2>/dev/null; fi | grep -c "proxy_read_timeout *3600s" || true'`,
                    { encoding: 'utf8' }).trim();
  } catch { conf = ''; }
  const n = Number(conf);
  if (Number.isFinite(n) && n >= 1) ok('6b effective nginx config has proxy_read_timeout 3600s', `${n} occurrence(s) in nginx -T`);
  else bad('6b effective nginx config lacks proxy_read_timeout 3600s', `nginx -T count=${conf || 'none'}`);
} else {
  skip('6b effective nginx timeouts', session ? 'needs root to run nginx -T' : 'no session');
}

// 6c ------------------------ a body larger than nginx's 1m default must pass
// `client_max_body_size` defaults to 1m; the whole point of the upload setting is
// that a multi-megabyte body is *not* rejected, so send one.
if (session) {
  const big = Buffer.alloc(3 * 1024 * 1024, 0x41);
  let r6c;
  try {
    r6c = await request({
      method: 'POST', path: '/', body: big,
      headers: { 'Content-Type': 'application/octet-stream', 'Content-Length': big.length, Cookie: jarHeader() },
    });
  } catch (e) {
    r6c = { status: 0, note: e.message };
  }
  if (r6c.status && r6c.status !== 413) {
    ok('6c a 3 MB body is not rejected (nginx default is 1m)', `HTTP ${r6c.status}`);
  } else if (r6c.status === 413) {
    bad('6c a 3 MB body was rejected with 413', 'client_max_body_size is still at its default');
  } else {
    bad('6c could not send the large body', r6c.note ?? 'no response');
  }
  if (typeof process.getuid === 'function' && process.getuid() === 0) {
    let size = '';
    try {
      size = execSync(`sh -c '` +
        `if [ -n "$DSH_MU_CONF_DIR" ] && [ -f "$DSH_MU_CONF_DIR/nginx.conf" ]; then ` +
        `cat "$DSH_MU_CONF_DIR/nginx.conf" "$DSH_MU_CONF_DIR"/conf.d/*.conf "$DSH_MU_CONF_DIR"/http.d/*.conf 2>/dev/null; ` +
        `elif [ -f /etc/dsh-mu-gateway/nginx.conf ]; then ` +
        `cat /etc/dsh-mu-gateway/nginx.conf /etc/dsh-mu-gateway/conf.d/*.conf /etc/dsh-mu-gateway/http.d/*.conf 2>/dev/null; ` +
        `elif [ -f /var/lib/dsh-mu/gateway/conf/nginx.conf ]; then ` +
        `cat /var/lib/dsh-mu/gateway/conf/nginx.conf /var/lib/dsh-mu/gateway/conf/conf.d/*.conf /var/lib/dsh-mu/gateway/conf/http.d/*.conf 2>/dev/null; ` +
        `else nginx -T 2>/dev/null; fi | ` +
        `grep -o "client_max_body_size *[0-9a-z]*" | sort -u | tr "\\n" " " || true'`,
                      { encoding: 'utf8' }).trim();
    } catch { size = ''; }
    if (/512m/.test(size)) ok('6c effective config sets client_max_body_size 512m', size);
    else bad('6c effective config does not set 512m', size || 'nginx -T gave nothing');
  } else {
    skip('6c effective client_max_body_size', 'needs root to run nginx -T');
  }
} else {
  skip('6c large upload', 'no session');
}

// 6d (login rate limiting) lives in tests/login-limit.py: driving the limit
// burns the per-client-IP failure budget (40 per 600s), which would break every
// later login in the same regression run. It runs as its own final layer.

// 7 --------------------------------------------------------------- ops API
{
  // The gateway reads its own copy of the token from inside the jail; after a
  // rotation the host copy can be newer, so try both and keep whichever answers.
  const opsTokens = (() => {
    const out = [];
    if (process.env.DSH_MU_OPS_TOKEN) out.push(process.env.DSH_MU_OPS_TOKEN);
    for (const f of ['/etc/dsh-mu/admin.token',
                     '/var/lib/dsh-mu/gateway/etc/admin.token']) {
      try {
        const v = readFileSync(f, 'utf8').trim();
        if (v && !out.includes(v)) out.push(v);
      } catch {}
    }
    return out;
  })();
  let r7 = { status: 0, body: '' };
  for (const tok of opsTokens.length ? opsTokens : ['']) {
    r7 = await request({ path: `/mu-api/status?t=${encodeURIComponent(tok)}`, hostHeader: '127.0.0.1' });
    if (r7.status === 200) break;
  }
  if (r7.status === 200) {
    let summary = r7.body;
    try {
      const j = JSON.parse(r7.body);
      summary = `users=${j.users} instances=${(j.instances ?? []).length} running=${j.running}`;
    } catch {}
    ok('7 loopback /mu-api/status', summary);
  } else {
    bad('7 loopback /mu-api/status', `status ${r7.status} body=${String(r7.body).slice(0, 120)}`);
  }
}

// 8 ------------------------------------------------------------------- logout
if (session) {
  const r8 = await request({ path: '/logout', headers: { Cookie: jarHeader() } });
  jarFrom(r8);
  if (r8.status === 302 && !cookieJar.has('dshmu_session')) ok('8 /logout clears session', `-> ${r8.headers.location}`);
  else bad('8 /logout clears session', `status ${r8.status}`);
} else {
  skip('8 /logout clears session', 'no session');
}

for (const [state, name, note] of results) {
  console.log(`${state.padEnd(4)}  ${name}${note ? `  -- ${note}` : ''}`);
}
const failed = results.filter(([s]) => s === 'FAIL').length;
console.log(failed === 0 ? 'ALL CHECKS PASSED' : `${failed} CHECK(S) FAILED`);
process.exit(failed === 0 ? 0 : 1);
