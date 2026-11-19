// dsh-mu 崩溃探针：在 jail 里用 NODE_OPTIONS=--require 预加载。
//
// 为什么需要它：DSH 默认几乎不往 stderr 写东西 ✗ 进程"突然消失"时
// （OOM、uncaught、被信号杀、事件循环卡死）日志是空的 ✗ 只能上机器猜。
// 这个探针只做四件事，全部输出到 stderr（supervise-daemon 会落到
// /var/log/dsh-mu/<name>.err.log ✓）：
//   1) 载入时一行：pid / node / 关键环境变量
//   2) uncaughtException / unhandledRejection：完整堆栈（然后按 Node 默认语义退出）
//   3) 退出时一行：code / uptime / rss（SIGKILL 时不会有，正好用来区分）
//   4) 心跳（默认 30 秒）：rss / 句柄数 / 事件循环延迟 —— 用来抓"卡住但不退出"
//
// 环境变量：
//   DSH_MU_PROBE_HEARTBEAT_S   心跳秒数，0 = 关闭（默认 30）
// 同时设 global 标记，防止被重复 require 打成两份。

'use strict'

if (!global.__dshMuProbe) {
  global.__dshMuProbe = true

  const tag = '[probe]'
  const started = Date.now()
  const write = (line, kind = '') => {
    try {
      process.stderr.write(kind ? `[probe:${kind}] ${line}\n` : `${tag} ${line}\n`)
    } catch { /* 探针自己绝不能影响宿主 */ }
  }
  const mb = (bytes) => Math.round((bytes || 0) / 1048576)

  const heartbeatRaw = Number(process.env.DSH_MU_PROBE_HEARTBEAT_S ?? 30)
  const heartbeatS = Number.isFinite(heartbeatRaw) ? Math.max(0, heartbeatRaw) : 30

  // 1) 载入行：把"这台实例在什么环境里跑"记下来
  write(`loaded pid=${process.pid} node=${process.version} ` +
        `dsh_home=${process.env.DSH_HOME || '-'} ` +
        `profile=${process.env.DSH_PROFILE || '-'} ` +
        `port=${process.env.DSH_PORT || '-'} ` +
        `cwd=${process.cwd()}`)

  // 4) 事件循环延迟（抓"进程活着但不干活"那类卡死）
  let loopDelay = null
  try {
    const { monitorEventLoopDelay } = require('node:perf_hooks')
    loopDelay = monitorEventLoopDelay({ resolution: 20 })
    loopDelay.enable()
  } catch { loopDelay = null }

  const heartbeat = setInterval(() => {
    try {
      const mem = process.memoryUsage()
      const uptimeS = Math.round((Date.now() - started) / 1000)
      const handles = typeof process.getActiveResourcesInfo === 'function'
        ? process.getActiveResourcesInfo().length
        : -1
      const p99 = loopDelay ? Math.round(loopDelay.percentile(99) / 1e6) : -1
      write(`uptime=${uptimeS}s rss=${mb(mem.rss)}MB heap=${mb(mem.heapUsed)}MB ` +
            `handles=${handles} p99=${p99}ms`, 'heartbeat')
    } catch { /* 心跳失败不影响业务 */ }
  }, Math.max(0, heartbeatS) * 1000)
  if (heartbeat.unref) heartbeat.unref()

  // 2) 未捕获异常 / 未处理的 Promise
  //    先打印，然后按 Node 默认语义退出（默认会 crash，我们绝不能把错误吞掉 ✗）
  let alreadyDying = false
  const fatal = (kind, error) => {
    if (alreadyDying) return
    alreadyDying = true
    const stack = error && error.stack ? error.stack : String(error)
    write(`${kind} uptime=${Math.round((Date.now() - started) / 1000)}s`)
    write(stack)
    // 给 supervise-daemon / 日志一点时间落盘
    setTimeout(() => process.exit(1), 50).unref()
  }
  process.on('uncaughtException', (error) => fatal('uncaughtException', error))
  process.on('unhandledRejection', (reason) => fatal('unhandledRejection', reason))

  process.on('warning', (warning) => {
    write(`warning ${warning && warning.name}: ${warning && warning.message}`)
  })

  // 3) 正常退出 / process.exit() —— 被 SIGKILL 时不会有这一行 ✓
  process.on('exit', (code) => {
    const mem = process.memoryUsage()
    write(`exit code=${code} uptime=${Math.round((Date.now() - started) / 1000)}s rss=${mb(mem.rss)}MB`)
  })

  // 信号：SIGTERM（stop）/ SIGINT 是"有人让它停"，与 OOM/KILL 区分开
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP', 'SIGQUIT']) {
    process.on(sig, () => {
      write(`signal ${sig} uptime=${Math.round((Date.now() - started) / 1000)}s`)
      // 不改变默认语义：Node 在没有其它监听者时会退出，这里只是记录 ✓
    })
  }
}
