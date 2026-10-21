/*
 * Minimal reproducer: DSH's own subprocess runtime fails to spawn bash inside a
 * mount-namespace jail, while the same call succeeds outside it.
 *
 *   node dsh-subprocess-repro.mjs
 *
 * Expected on a healthy host:  OUTCOME: {"exitCode":0,"signal":null}
 * Observed inside the jail:    SPAWN FAILED: Error | spawn bash ENOENT
 *
 * The runtime instance is built with a Proxy context so no cordis container is
 * needed; LocalSubprocessRuntime only reaches for logging helpers through ctx.
 */
import { createRequire } from 'node:module';

const require = createRequire('/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json');
const { LocalSubprocessRuntime } = require('@deepseek-ai/dsh-subprocess-local');

const noop = () => undefined;
const ctx = new Proxy({}, {
  get(target, prop) {
    if (prop in target) return target[prop];
    return new Proxy(noop, { get: () => noop, apply: () => undefined });
  },
  has: () => true,
});

const rt = new LocalSubprocessRuntime(ctx);
try {
  const handle = rt.spawn({
    argv: ['bash', '-c', 'echo hi'],
    cwd: '/tmp',
    stdio: { stdin: 'ignore', stdout: { maxBytes: 8000 }, stderr: { maxBytes: 8000 } },
    graceMs: 3000,
    env: { NO_COLOR: '1', TERM: 'dumb' },
  });
  const outcome = await handle.done;
  console.log('OUTCOME:', JSON.stringify(outcome));
} catch (err) {
  console.log('SPAWN FAILED:', err.name, '|', err.message);
  console.log(String(err.stack).split('\n').slice(0, 6).join('\n'));
}
