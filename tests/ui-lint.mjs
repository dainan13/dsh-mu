#!/usr/bin/env node
// ui-lint.mjs - static consistency checks for the web surfaces.
//
//   node tests/ui-lint.mjs [path/to/dsh-mu]
//
// Catches the class of bug that only shows up when a human clicks the button:
//   * an onclick/onchange handler that no longer exists
//   * getElementById()/querySelector('#…') targets that are not in the markup
//   * tab buttons without a matching panel
//   * admin-page API calls whose route is not registered in the backend
//   * user-panel endpoints that the nginx fragment does not serve
//
// It reads the sources, not a browser, so it is fast enough to run in the
// regression suite.
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = process.argv[2] ?? join(here, '..');

let pass = 0;
let fail = 0;
const ok = (m) => { pass += 1; console.log(`PASS  ${m}`); };
const bad = (m) => { fail += 1; console.log(`FAIL  ${m}`); };

const read = (rel) => {
  const path = join(root, rel);
  if (!existsSync(path)) {
    bad(`${rel} is missing`);
    return '';
  }
  return readFileSync(path, 'utf8');
};

const html = read('admin/index.html');
const backend = read('admin/dsh-mud');
const panel = read('gateway/static/panel.js');
const fragment = read('gateway/dsh-mu-server.conf');

// ------------------------------------------------------------------ helpers
const inlineScript = (() => {
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  return m ? m[1] : '';
})();

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function defined(name, code) {
  const n = escapeRe(name);
  return new RegExp(`function\\s+${n}\\s*\\(`).test(code)
    || new RegExp(`(window\\.)?${n}\\s*=\\s*(async\\s*)?(\\(|function)`).test(code)
    || new RegExp(`(const|let|var)\\s+${n}\\s*=`).test(code);
}

// 1 ------------------------------------------------------- inline HTML handlers
{
  // Only bare calls count: `foo(` is a handler, `this.form.reset(` is a method.
  const handlers = [...html.matchAll(/on(?:click|change|input|submit|keydown|keyup)="([^"]+)"/g)]
    .flatMap(([, body]) => [...body.matchAll(/(^|[^.\w$])([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[2]))
    .filter((n) => !['if', 'return', 'this', 'event', 'JSON', 'String', 'Number', 'Math'].includes(n));
  const missing = [...new Set(handlers)].filter((n) => !defined(n, inlineScript) && !defined(n, panel));
  if (missing.length === 0) ok(`all ${new Set(handlers).size} inline handlers are defined`);
  else bad(`handlers referenced but not defined: ${missing.join(', ')}`);
}

// 2 ---------------------------------------------------------- DOM id lookups
{
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const lookups = new Set([
    ...[...inlineScript.matchAll(/\$\(\s*'#([\w-]+)'/g)].map((m) => m[1]),
    ...[...inlineScript.matchAll(/getElementById\(\s*'([\w-]+)'/g)].map((m) => m[1]),
    ...[...inlineScript.matchAll(/querySelector\(\s*'#([\w-]+)/g)].map((m) => m[1]),
  ]);
  const missing = [...lookups].filter((id) => !ids.has(id));
  if (missing.length === 0) ok(`all ${lookups.size} element lookups have markup`);
  else bad(`element ids looked up but not present: ${missing.join(', ')}`);
}

// 3 ------------------------------------------------------------- tab coverage
{
  const tabs = [...html.matchAll(/data-tab="([\w-]+)"/g)].map((m) => m[1]);
  const panels = [...html.matchAll(/id="tab-([\w-]+)"/g)].map((m) => m[1]);
  const missing = tabs.filter((t) => !panels.includes(t));
  if (tabs.length > 0 && missing.length === 0) ok(`all ${tabs.length} tabs have a panel`);
  else if (tabs.length === 0) bad('no data-tab buttons found in the page');
  else bad(`tabs without a panel: ${missing.join(', ')}`);
}

// 4 ------------------------------------------- page API calls vs backend routes
// The page calls api('<path>'), which resolves to /admin/api/<path>; the gateway
// strips /admin, so the backend sees /api/<path> (that is why its routes are
// anchored there). Template holes count as one wildcard segment. Matching is
// segment-wise against the backend's own patterns - never against a pattern
// derived from the call, which would pass trivially.
{
  const segments = (s) => s.split('/').filter(Boolean);
  const routes = [];
  for (const m of backend.matchAll(/"(GET|POST|PUT|PATCH|DELETE)",\s*re\.compile\(r"([^"]+)"\)/g)) {
    const pattern = segments(m[2].replace(/^\^/, '').replace(/\$$/, ''))
      .map((part) => (part.includes('(') ? part.replace(/\([^)]*\)/g, 'X') : part));
    routes.push(pattern);
  }
  const calls = [...inlineScript.matchAll(/api\(\s*[`'"]([^`'"]+)[`'"]/g)].map((m) => m[1]);
  const unmatched = [];
  for (const call of new Set(calls)) {
    const path = segments('/api/' + call
      .replace(/\?.*$/, '')
      .replace(/\$\{[^}]+\}/g, 'X'));
    const hit = routes.some((route) => route.length === path.length
      && route.every((part, i) => part === 'X' || path[i] === 'X' || part === path[i]));
    if (!hit) unmatched.push(call);
  }
  if (routes.length === 0) bad('no backend routes parsed (did the route table change shape?)');
  else if (unmatched.length === 0) ok(`all ${new Set(calls).size} page API calls match a backend route`);
  else bad(`page calls with no backend route: ${unmatched.join(', ')}`);
}

// 5 ------------------------------------------------- user panel vs nginx routes
{
  // panel.js routes its requests through its own api() helper, so collect both
  // fetch(...) and api(...) call sites (and explicit navigations).
  const endpoints = new Set([
    ...[...panel.matchAll(/(?:fetch|api)\(\s*[`'"]([^`'"$]+)/g)].map((m) => m[1]),
    ...[...panel.matchAll(/location\.href\s*=\s*[`'"]([^`'"$]+)/g)].map((m) => m[1]),
  ]);
  const locations = [...fragment.matchAll(/location\s+(?:=|\^~)\s+(\S+)/g)].map((m) => m[1]);
  // "/" only covers the root itself; any other location covers its subtree.
  const covered = (path) => locations.some((loc) => {
    const base = loc.replace(/\*$/, '');
    if (base === '/') return path === '/';
    return path === base || path.startsWith(base.endsWith('/') ? base : `${base}/`)
      || path === base.replace(/\/$/, '');
  });
  const missing = [...endpoints].filter((e) => !covered(e.split('?')[0]));
  if (endpoints.size === 0) bad('panel.js makes no requests (parsed nothing)');
  else if (missing.length === 0) ok(`all ${endpoints.size} panel endpoints are served by the gateway`);
  else bad(`panel endpoints not routed: ${missing.join(', ')}`);
}

// 6 --------------------------------------------------------- panel asset wiring
{
  if (html && fragment.includes('/mu-static/') && fragment.includes('panel.js')) {
    ok('gateway serves /mu-static/ and injects panel.js');
  } else {
    bad('gateway fragment does not serve the panel assets');
  }
  if (panel.includes('dshmu-panel-host')) ok('panel.js defines its shadow host');
  else bad('panel.js does not define its shadow host');

  // panel.js is served as a classic script, so a stray top-level `await` (or any
  // other syntax error) kills the whole drawer at parse time -- the user just sees
  // the panel disappear. Parse it exactly like a browser would.
  {
    const { execFileSync } = await import('node:child_process');
    const fs = await import('node:fs');
    const os = await import('node:os');
    const path = await import('node:path');
    const tmp = path.join(os.tmpdir(), `panel-parse-${process.pid}.js`);
    fs.writeFileSync(tmp, panel);
    let okParse = true;
    let detail = '';
    try {
      execFileSync(process.execPath, ['--check', tmp], { stdio: 'pipe' });
    } catch (err) {
      okParse = false;
      const out = `${err.stdout || ''}${err.stderr || ''}`.trim().split('\n').slice(0, 2).join(' ');
      detail = out.slice(0, 160);
    } finally {
      try { fs.unlinkSync(tmp); } catch {}
    }
    if (okParse) ok('panel.js parses as a classic script (no stray await)');
    else bad(`panel.js does not parse: ${detail}`);
  }
}

// n ------------------------------- `hidden` really hides (display rules outrank it)
{
  const html = read('admin/index.html');
  const usesHidden = /<form[^>]*\bhidden\b/.test(html) || /\bhidden>/.test(html);
  // A class/element rule like `form{display:flex}` overrides the UA rule for [hidden],
  // which shipped once as a form sitting next to its own create button ("双份").
  const wins = /\[hidden\]\s*\{\s*display\s*:\s*none\s*!important/.test(html);
  if (!usesHidden) ok('no element relies on the hidden attribute');
  else if (wins) ok('[hidden] is forced to display:none, so hidden elements stay hidden');
  else bad('the page uses hidden="" but no `[hidden]{display:none!important}` rule exists');
}

// n ------------------------------- layout classes used in markup are defined
{
  const html = read('admin/index.html');
  const css = html.slice(html.indexOf('<style'), html.indexOf('</style>'));
  const used = new Set();
  for (const m of html.matchAll(/class="([^"]+)"/g)) {
    for (const c of m[1].split(/\s+/)) if (c) used.add(c);
  }
  // every `.name` that appears in any selector counts as "styled"
  const styled = new Set();
  for (const m of css.matchAll(/(^|\})([^{}]+)\{/g)) {
    for (const c of m[2].matchAll(/\.([\w-]+)/g)) styled.add(c[1]);
  }
  const layout = ['row', 'inline', 'card', 'msg', 'muted', 'banner', 'spinner'];
  // must have its OWN rule (`.cls{`), not merely appear inside some selector: a class
  // referenced only as `label.cls > input` still leaves the markup unstyled.
  const own = new Set();
  for (const c of css.matchAll(/(?:^|[\n}])\s*\.([\w-]+)\s*\{/gm)) own.add(c[1]);
  const missing = layout.filter((c) => used.has(c) && !own.has(c));
  if (missing.length === 0) ok('layout classes used in the page are actually defined');
  else bad(`classes used but never styled: ${missing.join(', ')}`);
}

// n ------------------------------- global input sizing must exempt checkboxes
{
  const html = read('admin/index.html');
  const css = html.slice(html.indexOf('<style'), html.indexOf('</style>'));
  const globalSizing = /input\s*,\s*select\s*\{[^}]*min-width\s*:\s*\d+px/.test(css);
  const exempt = /input\[type=checkbox\][^{]*\{[^}]*min-width\s*:\s*0/.test(css);
  if (!globalSizing) ok('no global min-width on inputs (nothing to exempt)');
  else if (exempt) ok('checkboxes/radios are exempt from the global input min-width');
  else bad('a global `input,select{min-width:...}` rule also sizes checkboxes into wide boxes');
}

// n --------------------------- panel reads must be forwarded by me.lua
{
  // /me builds its payload as an explicit whitelist. Three separate bugs came from
  // adding a field to the backend and forgetting the lua projection (resources,
  // my_default, started_at), so assert the contract explicitly.
  const me = read('gateway/lua/me.lua');
  const panel = read('gateway/static/panel.js');
  const required = [
    ['instance started_at', /\n\s*started_at\s*=\s*started_at/, /i\.started_at/],
    ['instance resources', /resources\s*=/, /i\.resources/],
    ['my_default', /my_default\s*=/, /data\.my_default/],
  ];
  const missing = required
    .filter(([, inLua, inPanel]) => inPanel.test(panel) && !inLua.test(me))
    .map(([label]) => label);
  if (missing.length === 0) ok('me.lua forwards every field the panel reads');
  else bad(`panel reads fields me.lua does not forward: ${missing.join(', ')}`);
}

console.log(`\n=== UI LINT: PASS=${pass} FAIL=${fail} ===`);
console.log(fail === 0 ? 'UI CHECKS PASSED' : 'UI CHECKS FAILED');
process.exit(fail === 0 ? 0 : 1);
