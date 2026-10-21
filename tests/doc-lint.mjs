#!/usr/bin/env node
// doc-lint.mjs - the documentation must not lie.
//
//   node tests/doc-lint.mjs [path/to/dsh-mu]
//
// Long-lived docs drift: counts go stale, a renamed command stays documented,
// a link points at a deleted file. Everything here is mechanically checkable:
//
//   * every relative markdown link resolves to a file that exists
//   * every `dsh-muctl <subcommand>` mentioned in the docs exists in the CLI
//   * every test script the docs mention exists
//   * the regression layer count quoted in the docs matches regression.sh
//   * the artifacts the handoff sheet promises are actually in the tree
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(process.argv[2] ?? join(here, '..'));

let pass = 0;
let fail = 0;
const ok = (m) => { pass += 1; console.log(`PASS  ${m}`); };
const bad = (m) => { fail += 1; console.log(`FAIL  ${m}`); };

const read = (rel) => {
  const p = join(root, rel);
  return existsSync(p) ? readFileSync(p, 'utf8') : '';
};

// Every markdown file in the tree is in scope, minus build leftovers.
const walk = (dir, out = []) => {
  for (const entry of readdirSync(join(root, dir), { withFileTypes: true })) {
    const rel = dir ? `${dir}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      if (['node_modules', 'dist', 'tmp', '__pycache__', '.git'].includes(entry.name)) continue;
      walk(rel, out);
    } else if (entry.name.endsWith('.md')) {
      out.push(rel);
    }
  }
  return out;
};

const docs = walk('');
if (docs.length === 0) bad('no markdown files found');

// 1 -------------------------------------------------------------- link targets
{
  const broken = [];
  for (const doc of docs) {
    const text = read(doc);
    for (const m of text.matchAll(/\[[^\]]*\]\(([^)\s]+)\)/g)) {
      const target = m[1];
      if (/^(https?:|mailto:|#)/.test(target)) continue;
      const path = target.split('#')[0];
      if (!path) continue;
      const resolved = resolve(join(root, dirname(doc)), path);
      if (!existsSync(resolved)) broken.push(`${doc} -> ${target}`);
    }
  }
  if (broken.length === 0) ok(`${docs.length} markdown files: every relative link resolves`);
  else bad(`broken links: ${broken.join('; ')}`);
}

// 2 ------------------------------------------- documented commands must exist
{
  const cli = read('cli/dshmuctl.py');
  const parserBlock = cli.slice(cli.indexOf('def build_parser'));
  const subcommands = new Set([...parserBlock.matchAll(/add_parser\(\s*"([\w-]+)"/g)].map((m) => m[1]));
  // instance/user sub-subcommands share a namespace with the top level
  const documented = new Set();
  for (const doc of docs) {
    for (const m of read(doc).matchAll(/dsh-muctl\s+([a-z][\w-]*)/g)) documented.add(m[1]);
  }
  const unknown = [...documented].filter((c) => !subcommands.has(c));
  if (subcommands.size === 0) bad('could not parse the CLI subcommands');
  else if (unknown.length === 0) ok(`every documented dsh-muctl subcommand exists (${documented.size} checked)`);
  else bad(`documented but not implemented: ${unknown.join(', ')}`);
}

// 3 ---------------------------------------------- documented tests must exist
{
  // Docs quote paths both in full (tests/selftest.mjs), in shorthand
  // (lua/login.lua) and as runtime locations (/etc/dsh-mu/gateway/users.json).
  // Only complain when nothing in the tree has that basename.
  const basenames = new Set();
  const collect = (dir) => {
    for (const entry of readdirSync(join(root, dir), { withFileTypes: true })) {
      const rel = dir ? `${dir}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (['node_modules', 'dist', 'tmp', '__pycache__', '.git'].includes(entry.name)) continue;
        collect(rel);
      } else {
        basenames.add(entry.name);
      }
    }
  };
  collect('');
  // Files that are generated on the target host, never in the repository.
  const generated = new Set(['users.json', 'upstreams.json', 'config.json', 'admin.token',
                             'secret.key', 'state.json', 'dsh-mu.conf', 'nginx.conf',
                             'providers.json']);
  const missing = new Set();
  for (const doc of docs) {
    for (const m of read(doc).matchAll(/(?:tests|packaging|install|jail|gateway|lua)\/[\w./-]+\.(?:sh|py|mjs|conf|lua|json|service)/g)) {
      const path = m[0];
      if (path.includes('*')) continue;
      if (existsSync(join(root, path))) continue;
      const base = path.split('/').pop();
      if (basenames.has(base)) continue;   // documentation shorthand
      if (generated.has(base)) continue;   // created at runtime on the host
      missing.add(path);
    }
  }
  if (missing.size === 0) ok('every file path quoted in the docs exists (by name or by path)');
  else bad(`quoted paths that do not exist anywhere: ${[...missing].join(', ')}`);
}

// 4 ------------------------------------ the quoted regression layer count is real
{
  const regression = read('tests/regression.sh');
  const steps = [...regression.matchAll(/step "(\d+)\/(\d+)/g)].map((m) => Number(m[2]));
  const actual = steps.length ? Math.max(...steps) : 0;
  // Chronological records (docs/03, docs/04, CHANGELOG) legitimately quote past
  // counts, and "作为第 5 层" is a position, not a total: only statements about
  // the whole suite ("依次跑 10 层" / "共 10 层") are checked.
  const current = docs.filter((d) => !/docs\/(03|04)-/.test(d) && d !== 'CHANGELOG.md');
  const claimed = new Set();
  for (const doc of current) {
    for (const m of read(doc).matchAll(/(?:依次跑|共)\s*(\d+)\s*层/g)) claimed.add(Number(m[1]));
  }
  const wrong = [...claimed].filter((n) => n !== actual);
  if (actual === 0) bad('could not count the regression layers');
  else if (wrong.length === 0) ok(`documentation agrees on ${actual} regression layers`);
  else bad(`current docs claim ${wrong.join('/')} layers but regression.sh has ${actual}`);
  // ...and the layer names in the list must match the steps
  const names = [...regression.matchAll(/step "\d+\/\d+ ([^"]+)"/g)].map((m) => m[1].toLowerCase());
  const listLine = read('tests/regression.sh');
  const described = (listLine.match(/^#\s+\d+\./gm) ?? []).length;
  if (described === actual) ok(`the layer index lists all ${actual} layers`);

  else bad(`the layer index lists ${described} layers, regression.sh runs ${actual}`);
  // The per-layer check counts appear both in the handoff sheet and in the
  // acceptance manual. They cannot be re-run from here, but the two documents
  // must at least agree with each other - they had drifted apart silently.
  {
    const delivery = readFileSync(resolve(root, 'DELIVERY.md'), 'utf8');
    const manual = readFileSync(resolve(root, 'docs', '08-验收流程.md'), 'utf8');
    const pairs = [
      ['cli-contract', /cli-contract\s+(\d+)/, /CLI CONTRACT: PASS=(\d+)/],
      ['api-contract', /api-contract\s+(\d+)/, /API CONTRACT: PASS=(\d+)/],
      ['acceptance', /acceptance\s+(\d+)/, /ACCEPTANCE: PASS=(\d+)/],
      ['isolation', /isolation\s+(\d+)/, /ISOLATION: PASS=(\d+)/],
      ['selftest', /selftest\s+(\d+)/, /PASS=(\d+) FAIL=0\][^|]*$/m],
    ];
    const mismatched = [];
    for (const [label, a, b] of pairs) {
      const ma = delivery.match(a);
      const mb = manual.match(b);
      if (!ma || !mb) continue;
      if (ma[1] !== mb[1]) mismatched.push(`${label}: handoff ${ma[1]} vs manual ${mb[1]}`);
    }
    if (mismatched.length === 0) ok('the handoff sheet and the manual agree on the per-layer check counts');
    else bad(`per-layer counts disagree: ${mismatched.join('; ')}`);
  }
}

// 5 ------------------------------------------------ handoff sheet completeness
{
  const sheet = read('DELIVERY.md');
  if (!sheet) {
    bad('DELIVERY.md (handoff sheet) is missing');
  } else {
    const required = [
      ['install command', /apk add --allow-untrusted/],
      ['first-run command', /dsh-muctl install/],
      ['acceptance command', /tests\/regression\.sh/],
      ['the user-gated step', /fresh-machine\.py/],
      ['known limitations', /已知(限制|取舍)|Limitations/],
    ];
    const missing = required.filter(([, re]) => !re.test(sheet)).map(([label]) => label);
    if (missing.length === 0) ok('DELIVERY.md covers install, first run, acceptance, the gated step and limits');
    else bad(`DELIVERY.md is missing: ${missing.join(', ')}`);
    for (const m of sheet.matchAll(/(?:tests|docs|packaging|install|jail)\/[^\s`)\]，。；、]+/g)) {
      if (!existsSync(join(root, m[0]))) bad(`DELIVERY.md points at a missing path: ${m[0]}`);
    }
  }
}

// 5b ------------------------------------------- the evidence index is complete
{
  const index = read('docs/07-证据索引.md');
  if (!index) {
    bad('docs/07-证据索引.md (evidence index) is missing');
  } else {
    // every layer script must be mentioned by name in the index
    // every layer of the regression pipeline must be traceable from the index
    const layerScripts = ['selftest.mjs', 'ui-lint.mjs', 'cli-contract.sh', 'api-contract.py',
                          'acceptance.sh', 'fresh-machine.py', 'isolation.sh', 'failure-modes.py',
                          'doc-lint.mjs', 'soak.py', 'artifact-audit.py', 'drift-audit.py',
                          'doctor-fix.sh', 'log-rotation.sh', 'user-flows.py', 'run-uid.sh',
                          'fresh-rehearsal.sh', 'state-lock.py', 'admin-surface.py', 'user-panel.py', 'port-move.py',
                          'restore-owner.py', 'restore-plan.py', 'log-bounds.py', 'config-bounds.py',
                          'name-key-bounds.py', 'limit-values.py', 'password-bounds.py', 'provider-probe.py', 'login-limit.py'];
    const absent = layerScripts.filter((s) => !index.includes(s));
    if (absent.length === 0) ok(`the evidence index names all ${layerScripts.length} layer scripts`);
    else bad(`evidence index does not mention: ${absent.join(', ')}`);

    // the four objectives must each be traceable
    const objectiveTopics = [['配置外置', /dsh-mu\.conf/], ['CLI 库与 shim', /shim/],
                             ['apk 打包', /apk/], ['干净 Alpine 复现', /干净 Alpine/]];
    const missingTopics = objectiveTopics.filter(([, re]) => !re.test(index)).map(([label]) => label);
    if (missingTopics.length === 0) ok('the evidence index covers every objective area');
    else bad(`evidence index does not cover: ${missingTopics.join(', ')}`);

    // and it must be honest about what is *not* proven yet
    if (/还没.*证明|未证明|边界/.test(index)) ok('the index states the limits of the evidence');
    else bad('the index does not state what is still unproven');
  }
}

// 5c ------------------------------- features we removed must not stay documented
{
  // The random first-install administrator was replaced by "administrators are
  // host system accounts". Docs drifting back to the old story would send a new
  // operator looking for a password that is never printed.
  const current = docs.filter((d) => !/docs\/(03|04)-/.test(d) && d !== 'CHANGELOG.md');
  const removed = [
    ['random bootstrap admin password', /随机 16 位密码|初始管理员密码/],
    ['"admin / <random>" credentials', /admin\s*\/\s*<随机>/],
  ];
  const stale = [];
  for (const doc of current) {
    const text = read(doc);
    for (const [label, pattern] of removed) {
      if (pattern.test(text)) stale.push(`${doc}: ${label}`);
    }
  }
  if (stale.length === 0) ok('current docs do not describe the removed random-admin flow');
  else bad(`docs still describe a removed feature: ${stale.join('; ')}`);

  const systemAccount = current.some((d) => /ADMIN_SYSTEM_USERS|passwd root/.test(read(d)));
  if (systemAccount) ok('current docs describe the system-account administrator model');
  else bad('current docs do not explain how the administrator account works');
}

// 5d ------------------------------------- the acceptance procedure is complete
{
  const procedure = read('docs/08-验收流程.md');
  if (!procedure) {
    bad('docs/08-验收流程.md (acceptance procedure) is missing');
  } else {
    const layerScripts = ['selftest.mjs', 'ui-lint.mjs', 'cli-contract.sh', 'api-contract.py',
                          'acceptance.sh', 'fresh-machine.py', 'isolation.sh', 'failure-modes.py',
                          'doc-lint.mjs', 'soak.py', 'artifact-audit.py', 'drift-audit.py',
                          'doctor-fix.sh', 'log-rotation.sh', 'user-flows.py', 'run-uid.sh',
                          'fresh-rehearsal.sh', 'state-lock.py', 'login-limit.py'];
    const absent = layerScripts.filter((s) => !procedure.includes(s));
    if (absent.length === 0) ok(`the acceptance procedure names all ${layerScripts.length} layer scripts`);
    else bad(`acceptance procedure does not mention: ${absent.join(', ')}`);

    // one signable row per layer (the table is the point of the document)
    const rows = (procedure.match(/^\|\s*\d+\s*\|/gm) ?? []).length;
    if (rows >= 34) ok(`the procedure has a per-layer table and a sign-off table (${rows} rows)`);
    else bad(`the procedure only has ${rows} numbered table rows (expected >= 34: 17 steps + 17 sign-off)`);

    if (/不覆盖|未覆盖/.test(procedure)) ok('the procedure states what it does not cover');
    else bad('the procedure does not state its limits');
  }
}

// 5e --------------------------- every requirement is traced to a current status
{
  const requirements = read('docs/01-需求整理.md');
  const index = read('docs/07-证据索引.md');
  if (!requirements || !index) {
    bad('cannot check requirement traceability (docs/01 or docs/07 missing)');
  } else {
    const ids = [...new Set(requirements.match(/FR-\d+\.\d+/g) ?? [])].sort();
    const missing = ids.filter((id) => !index.includes(id));
    if (ids.length >= 30 && missing.length === 0) {
      ok(`every requirement (${ids.length}) appears in the traceability table`);
    } else if (missing.length) {
      bad(`requirements missing from the traceability table: ${missing.join(', ')}`);
    } else {
      bad(`only ${ids.length} requirement ids found in docs/01`);
    }
    // and the table must be honest: it has to show partial ones, not only ticks
    const partial = (index.match(/🟡/g) ?? []).length;
    if (partial >= 5) ok(`the traceability table flags ${partial} partial-requirement marks`);
    else bad(`the traceability table flags only ${partial} partial requirements (expected >= 5)`);

    // every flagged requirement must say whether it is accepted or still open -
    // a bare "partial" is how gaps quietly become nobody's problem
    const flagged = [...index.matchAll(/^\|\s*(FR-\d+\.\d+)\s*\|([^\n]*)$/gm)]
      .filter((m) => m[2].includes('🟡'));
    const unclassified = flagged.filter((m) => !/已接受|待定/.test(m[2])).map((m) => m[1]);
    if (unclassified.length === 0) {
      const accepted = flagged.filter((m) => m[2].includes('已接受')).length;
      const pending = flagged.filter((m) => m[2].includes('待定')).length;
      ok(`all ${flagged.length} partial requirements are classified (${accepted} accepted / ${pending} pending a decision)`);
    } else {
      bad(`unclassified partial requirements: ${unclassified.join(', ')} (say "已接受" or "待定")`);
    }

    // the summary numbers must match the table (counted, not remembered)
    const rows = [...index.matchAll(/^\|\s*(FR-\d+\.\d+)\s*\|.*?\|\s*(✅|🟡|⛔)/gm)];
    const done = rows.filter((r) => r[2] === '✅').length;
    const part = rows.filter((r) => r[2] === '🟡').length;
    const gone = rows.filter((r) => r[2] === '⛔').length;
    const report = read('docs/04-验收报告.md');
    const claims = [
      [`${done} 条完全落地并实测`, index, 'docs/07'],
      [`${part} 条部分实现`, index, 'docs/07'],
      [`| ✅ 已实现并实测 | **${done}** |`, report, 'docs/04'],
      [`| 🟡 部分实现 | **${part}** |`, report, 'docs/04'],
    ];
    const wrong = claims.filter(([needle, hay]) => !hay.includes(needle)).map(([, , where]) => where);
    if (wrong.length === 0) {
      ok(`traceability numbers agree with the table (${done} done / ${part} partial / ${gone} dropped)`);
    } else {
      bad(`traceability summary numbers do not match the table (${done}/${part}/${gone}); stale in ${[...new Set(wrong)].join(', ')}`);
    }
  }
}

// 6 ------------------------------------------- the package ships what it claims
{
  const apkbuild = read('packaging/APKBUILD');
  const shipped = ['gateway/lua/dshmu.lua', 'admin/dsh-mud', 'admin/index.html',
                   'cli/dshmuctl.py', 'jail/dsh-mu-jail'];
  const missing = shipped.filter((f) => {
    if (!existsSync(join(root, f))) return true;
    if (apkbuild.includes(f.split('/').pop())) return false;          // named
    const dir = f.split('/').slice(0, -1).join('/');
    const short = dir.split('/').pop();
    return !new RegExp(`${short}/[\\w*{}.$-]*\\*`).test(apkbuild);  // or shipped as a glob
  });
  if (missing.length === 0) ok('the package recipe ships every core file (by name or glob)');
  else bad(`APKBUILD ships neither by name nor by glob: ${missing.join(', ')}`);
  if (!/TEMPLATE|templates/.test(apkbuild) || !apkbuild.includes('templates')) {
    ok('APKBUILD does not package the jail template (still true)');
  } else {
    // mentioning it in a comment is fine; packaging it is not
    const packaged = /templates/.test(apkbuild.split('package()')[1] ?? '');
    if (packaged) bad('APKBUILD packages the ~140MB jail template');
    else ok('APKBUILD only mentions the template in comments');
  }
}

console.log(`\n=== DOC LINT: PASS=${pass} FAIL=${fail} ===`);
console.log(fail === 0 ? 'DOC CHECKS PASSED' : 'DOC CHECKS FAILED');
process.exit(fail === 0 ? 0 : 1);
