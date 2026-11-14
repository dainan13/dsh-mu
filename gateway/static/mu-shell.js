/* mu-shell.js — DSH 用户侧的「运行状态」侧栏卡 + F2 控制中心。
 *
 * 设计依据：uidesign/cockpit/dsh-native.html（真机快照里注入的那两块）。
 * 原则（与 panel.js 一致）：
 *   · 只**注入**，不改宿主 DOM；我们的东西全部在 shadow root 里
 *   · 颜色/圆角/描边只用宿主的 --dsw-alias-* 变量，禁止写死颜色
 *   · 容器只建一次；数据刷新只改文本，绝不重建容器（重建会打断 iframe/焦点）
 *   · 侧栏折叠/展开由宿主重新渲染子树触发，用 MutationObserver 跟着走，不轮询
 */
(function () {
  'use strict';
  if (window.__muShellLoaded) return;
  window.__muShellLoaded = true;

  const api = (path) => fetch(path, { credentials: 'same-origin' })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText))));

  const state = { user: '', instance: '', port: '', running: false, loaded: false,
                  cpu_pct: null, mem_pct: null, up: '' };

  /* ── 样式（只用宿主令牌） ─────────────────────────────────────────── */
  /* 设计稿原文（uidesign/cockpit/dsh-native.html 里的 CSS），只保留卡片/折叠态/F2 需要的部分。
     颜色全部取宿主 DSH 的变量，不写死颜色。 */
  const CSS = `
  :host { all: initial; }
  * { box-sizing: border-box; }
  .card {
    font: 13px/1.5 var(--dsw-font-family, system-ui, -apple-system, "Segoe UI", "PingFang SC", sans-serif);
    color: var(--dsw-alias-label-primary, #1b1b1c);
    background: var(--dsw-alias-bg-layer-1, #fff);
    border: 1px solid var(--dsw-alias-border-l2, #e6e8eb);
    border-radius: var(--dsw-radius-lg, 10px);
    padding: 9px 10px 8px; display: grid; gap: 6px; cursor: pointer; margin: 4px 8px 8px;
  }
  .card:hover { background: var(--dsw-alias-interactive-bg-hover, #f5f6f7); }
  .row1 { display: flex; align-items: center; gap: 7px; min-width: 0; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; flex: none; }
  .dot.off { background: #f59e0b; }
  .name { font-weight: 500; }
  .st { font-size: 11.5px; color: #16a34a; }
  .st.bad { color: var(--dsw-alias-label-caption, #8b939d); }
  .port { margin-left: auto; font: 11px/1 var(--dsw-font-family-code, ui-monospace, monospace); color: var(--dsw-alias-label-caption, #8b939d); }
  .row2 { display: flex; gap: 10px; font-size: 10.5px; color: var(--dsw-alias-label-caption, #8b939d); }
  .row2 b { font-weight: 500; color: var(--dsw-alias-label-primary, #1b1b1c); font-variant-numeric: tabular-nums; }
  .bars { display: flex; gap: 8px; }
  .bar { flex: 1; height: 3px; border-radius: 2px; background: var(--dsw-alias-border-l2, #e6e8eb); overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--dsw-alias-label-dimmed, #9aa3ad); }
  .rail {
    width: 36px; height: 36px; margin: 0 auto; border-radius: 12px; cursor: pointer;
    display: grid; place-items: center; border: 0; background: transparent;
    position: relative; color: var(--dsw-alias-label-primary, #1b1b1c);
    transition: background .12s;
  }
  .rail:hover { background: var(--dsw-alias-interactive-bg-hover, #efefef); }
  .rail .ico { width: 16px; height: 16px; display: block; transform: translate(-1px, -1px); }
  .rail .badge {
    position: absolute; right: 4px; bottom: 4px; width: 6px; height: 6px;
    border-radius: 50%; background: #22c55e;
    box-shadow: 0 0 0 1.5px var(--dsw-alias-bg-base, #fff);
  }
  .rail.off .badge { background: #f59e0b; }
  .f2 {
    position: fixed; inset: 0; z-index: 2147483000; display: none; place-items: center;
    background: rgba(6,8,12,.45);
    font: 13px/1.55 var(--dsw-font-family, system-ui, "PingFang SC", sans-serif);
    color: var(--dsw-alias-label-primary, #1b1b1c);
  }
  .f2.on { display: grid; pointer-events: auto; }
  .win {
    width: min(560px, 92vw); background: var(--dsw-alias-bg-base, #fff);
    border: 1px solid var(--dsw-alias-border-l2, #e6e8eb); border-radius: 14px; padding: 18px 18px 14px;
    box-shadow: 0 24px 60px rgba(0,0,0,.18);
  }
  .hd { display: flex; align-items: center; gap: 10px; }
  .hd .name { font-size: 15px; font-weight: 600; }
  .sub { margin: 4px 0 12px; font-size: 12px; color: var(--dsw-alias-label-caption, #8b939d); }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 12px; }
  .stat { border: 1px solid var(--dsw-alias-border-l2, #e6e8eb); border-radius: 10px; padding: 8px 10px; }
  .stat .k { font-size: 10.5px; color: var(--dsw-alias-label-caption, #8b939d); }
  .stat .v { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .sec { border: 1px solid var(--dsw-alias-border-l2, #e6e8eb); border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
  .sec .h { padding: 8px 10px; font-size: 12px; color: var(--dsw-alias-label-dimmed, #6b7079);
            border-bottom: 1px solid var(--dsw-alias-border-l1, #eff1f3); display: flex; align-items: center; gap: 8px; }
  .logbox { padding: 6px 10px 8px; font: 11px/1.7 var(--dsw-font-family-code, ui-monospace, monospace);
            max-height: 168px; overflow: auto; }
  .logbox .lg { display: flex; gap: 8px; white-space: nowrap; }
  .logbox .lg + .lg { margin-top: 2px; }
  .logbox time { color: var(--dsw-alias-label-caption, #8b939d); flex: none; }
  .logbox .lvl { flex: none; font-size: 10px; font-weight: 500; }
  .logbox .lvl.info { color: var(--dsw-alias-label-dimmed, #6b7079); }
  .logbox .lvl.warn { color: #8a5a00; }
  .logbox .lvl.err { color: #c0392b; }
  .logbox .msg { color: var(--dsw-alias-label-primary, #1b1b1c); overflow: hidden; text-overflow: ellipsis; }
  .boot { width: 100%; padding: 10px 12px 12px; border: 1px solid var(--dsw-alias-border-l2, #e6e8eb);
    border-radius: 10px; background: var(--dsw-alias-bg-layer-1, #fff); position: relative; overflow: hidden;
    font: 11px/1.75 var(--dsw-font-family-code, ui-monospace, monospace);
    color: var(--dsw-alias-label-primary, #1b1b1c); opacity: .82; }
  .boot::after { content: ""; position: absolute; inset: 0; pointer-events: none;
    background: repeating-linear-gradient(0deg, rgba(127,127,127,.04) 0 1px, transparent 1px 5px); }
  .boot .bl { white-space: pre; opacity: 1; }
  .boot .bc { display: inline-block; width: 6px; height: 11px; margin-left: 2px; vertical-align: -1px;
    background: var(--dsw-alias-label-primary, #1b1b1c); animation: bcur 1s steps(1) infinite; }
  .boot .bt { height: 3px; margin-top: 8px; border-radius: 2px; background: var(--dsw-alias-border-l2, #e6e8eb); overflow: hidden; }
  .boot .bt i { display: block; height: 100%; width: 0; background: var(--dsw-alias-label-primary, #1b1b1c); transition: width 2.8s linear; }
  @keyframes bcur { 50% { opacity: 0; } }
  .acts { display: flex; flex-direction: column; align-items: center; gap: 8px; margin-top: 14px;
    padding-top: 12px; border-top: 1px solid var(--dsw-alias-border-l1, #eff1f3); }
  .acts .act { width: 220px; justify-content: center; }
  .ft { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
  .btn { font: 13px var(--dsw-font-family, system-ui); padding: 6px 12px; border-radius: 8px;
         border: 1px solid var(--dsw-alias-border-l2, #e6e8eb);
         background: var(--dsw-alias-bg-layer-1, #fff); color: inherit; cursor: pointer; }
  .btn.primary { background: var(--dsw-alias-button-primary-fill, #4176e6); border-color: transparent; color: #fff; }
  `;

  /* ── 宿主挂点 ───────────────────────────────────────────────────── */
  const host = document.createElement('div');
  host.id = 'mu-shell-host';
  host.style.cssText = 'display:block;pointer-events:auto';
  const sh = host.attachShadow({ mode: 'open' });
  sh.innerHTML = `<style>${CSS}</style><div id="slot"></div>`;

  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483000';
  const osh = overlay.attachShadow({ mode: 'open' });
  osh.innerHTML = `<style>${CSS}</style>
    <div class="f2" id="f2">
      <div class="win" role="dialog" aria-label="我的实例">
        <div class="hd"><span class="dot" id="f2dot" style="width:8px;height:8px;border-radius:50%;background:#22c55e"></span>
          <span class="name" id="f2name">实例</span><span class="st" id="f2state" style="color:#16a34a;font-size:12px"></span>
        </div>
        <div class="sub" id="f2sub">实例 —</div>
        <div class="grid">
          <div class="stat"><div class="k">CPU</div><div class="v" id="f2cpu">—</div></div>
          <div class="stat"><div class="k">内存</div><div class="v" id="f2mem">—</div></div>
          <div class="stat"><div class="k">进程</div><div class="v" id="f2pids">—</div></div>
        </div>
        <div class="sec"><div class="h">实例日志<span style="margin-left:auto;display:flex;gap:6px">
            <button class="btn" id="logRefresh" style="padding:2px 8px;font-size:11.5px">刷新</button>
            <button class="btn" id="logCopy" style="padding:2px 8px;font-size:11.5px">复制</button></span></div>
          <div class="logbox" id="logbox"></div>
          <div class="b" style="border-top:1px solid var(--dsw-alias-border-l1,#eff1f3)"><span style="font-size:11px;color:#8b939d">只保留最近 50 行；完整日志在「管理台 → 实例日志」。</span></div>
        </div>
        <div class="acts">
          <button class="btn primary act" id="f2restart">重启实例</button>
          <button class="btn act" id="f2logout" style="color:#c0392b;border-color:rgba(192,57,43,.45)">退出登录</button>
        </div>
        <div class="ft"><button class="btn" id="f2close">关闭</button></div>
      </div>
    </div>`;

  const q = (sel) => osh.querySelector(sel);
  function openF2(force) {
    const el = q('#f2');
    el.classList.toggle('on', force !== undefined ? !!force : !el.classList.contains('on'));
  }

  osh.addEventListener('click', async (e) => {
    if (e.target.id === 'logRefresh') { e.preventDefault(); await refresh(); }
    if (e.target.id === 'logCopy') {
      e.preventDefault();
      const text = (Array.isArray(state.logs) ? state.logs : []).join('\n');
      if (!text) return;
      try { await navigator.clipboard.writeText(text); }
      catch (err) {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch (e2) {}
        ta.remove();
      }
    }
  });
  osh.addEventListener('click', (e) => {
    if (e.target.id === 'f2close' || e.target.id === 'f2') openF2(false);
    if (e.target.id === 'f2logout') window.location.href = '/logout';
    if (e.target.closest('#f2restart') || e.target.id === 'f2restart') {
      e.preventDefault();
      doRestart();
    }
  });
  /* ── 重启：照 dsh-native.html 的 boot 面板（逐行 + 进度条 + 光标） ───────── */
  const bootHTML = (pct) => {
    const lines = ['> restart instance ' + (state.instance || ''),
      '  stopping supervisor ......... ok',
      '  releasing :' + (state.port || '—') + ' ............. ok',
      '  starting container',
      '  checking health <span class="bc"></span>'];
    return '<div class="boot">' + lines.map((x) => '<div class="bl">' + x + '</div>').join('')
      + '<div class="bt"><i style="width:' + pct + '%"></i></div></div>';
  };

  function doRestart() {
    const actsEl = q('.acts');
    if (!actsEl || actsEl.dataset.busy === '1') return;
    actsEl.dataset.busy = '1';
    const actsHTML = actsEl.innerHTML;
    const st = q('#f2state');
    if (st) { st.textContent = '重启中'; st.style.color = '#8a5a00'; }
    actsEl.innerHTML = bootHTML(0);
    const fill = actsEl.querySelector('.bt i');
    setTimeout(() => { if (fill) fill.style.width = '100%'; }, 60);
    fetch('/me', { method: 'POST', headers: { 'content-type': 'application/json' },
                   body: JSON.stringify({ action: 'restart' }) }).catch(() => {});
    // 轮询 /me 直到实例重新 running（最多 40 秒），然后收动画
    const started = Date.now();
    const tick = () => {
      refresh().then(() => {
        const up = state.running && Date.now() - started > 3000;
        if (up || Date.now() - started > 40000) {
          actsEl.dataset.busy = '0';
          actsEl.innerHTML = actsHTML;
          paint();
        } else {
          setTimeout(tick, 1500);
        }
      }).catch(() => setTimeout(tick, 1500));
    };
    setTimeout(tick, 2000);
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'F2') { e.preventDefault(); e.stopImmediatePropagation(); openF2(true); }
    if (e.key === 'Escape') { openF2(false); }
  }, true);

  /* ── 渲染 ───────────────────────────────────────────────────────── */
  function cardHTML() {
    const ok = state.running;
    const cpu = state.cpu_pct == null ? '—' : state.cpu_pct + '%';
    const mem = state.mem_pct == null ? '—' : state.mem_pct + '%';
    const w = (v) => Math.max(0, Math.min(100, Number(v) || 0));
    const title = `${state.instance} · ${ok ? '运行中' : '未运行'}${state.port ? ' · :' + state.port : ''}`;
    // 结构与 dsh-native.html 里的 cardHTML 一致（row1 / row2 / bars）
    return `<div class="card" id="card" title="${title}">
      <div class="row1"><span class="dot ${ok ? '' : 'off'}"></span><span class="name">${state.instance}</span><span class="st ${ok ? '' : 'bad'}">${ok ? '运行中' : (state.loaded ? '未运行' : '读取中')}</span><span class="port">${state.port ? ':' + state.port : ''}</span></div>
      <div class="row2"><span>CPU <b>${cpu}</b></span><span>内存 <b>${mem}</b></span><span>启动 <b>${state.up || '—'}</b></span></div>
      <div class="bars"><span class="bar"><i style="width:${w(state.cpu_pct)}%"></i></span><span class="bar"><i style="width:${w(state.mem_pct)}%"></i></span></div>
    </div>`;
  }

  function railHTML() {
    const ok = state.running;
    const title = `${state.instance} · ${ok ? '运行中' : '未运行'}${state.port ? ' · :' + state.port : ''}`;
    // 与设计稿的 railHTML 一致（36×36 / 12px / 16 网格图标 / 右下 6px 状态点）
    return `<div class="rail ${ok ? '' : 'off'}" id="rail" title="${title}">
      <svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true">
        <rect x="1.9" y="2.6" width="12.2" height="10.8" rx="2.2"></rect>
        <path d="M4.3 10.9 6.4 7.9l1.9 1.7 3.4-3.8"></path>
      </svg>
      <span class="badge"></span>
    </div>`;
  }

  let collapsed = false;
  function paint() {
    const slot = sh.getElementById('slot');
    slot.innerHTML = collapsed ? railHTML() : cardHTML();
    const el = slot.querySelector(collapsed ? '#rail' : '#card');
    if (el) el.addEventListener('click', () => openF2(true));
    host.style.cssText = collapsed
      ? 'display:block;width:36px;margin:0 auto;pointer-events:auto'
      : 'display:block;pointer-events:auto';
    renderLogs();
    // F2 里的字段跟着数据走
    const cap = 'font-size:11px;font-weight:400;color:#8b939d';
    q('#f2dot').style.background = state.running ? '#22c55e' : '#f59e0b';
    q('#f2name').textContent = state.instance || '我的实例';
    q('#f2state').textContent = state.running ? '运行中' : (state.loaded ? '未运行' : '读取中');
    q('#f2state').style.color = state.running ? '#16a34a' : '#8b939d';
    q('#f2sub').textContent = '实例 ' + (state.instance || '—')
      + (state.port ? ' · 端口 ' + state.port : '')
      + (state.startedText ? ' · 启动于 ' + state.startedText : '');
    q('#f2cpu').textContent = state.cpu_pct == null ? '—' : state.cpu_pct + '%';
    const mb = (v) => (v == null ? null : Math.round(v / (1024 * 1024)));
    q('#f2mem').innerHTML = (state.mem_used == null ? '—' : mb(state.mem_used))
      + (state.mem_max ? `<span style="${cap}"> / ${mb(state.mem_max)} MB</span>` : '');
    q('#f2pids').innerHTML = (state.pids == null ? '—' : state.pids)
      + (state.pids_max ? `<span style="${cap}"> / ${state.pids_max}</span>` : '');
  }

  /* ── 会话过期 → 回登录页 ✓
     DSH 页面是 SPA：平台会话失效后，页面本身还能显示 ✗
     它自己的 XHR 只会报错（用户看到一堆接口错误 ✗），不会跳登录 ✗
     这里统一拦 fetch/XHR 的 401（平台约定：{"error":"unauthorized"} 或 401 ✓） */
  function watchUnauthorized() {
    const toLogin = () => {
      if (/\/login(\?|$)/.test(location.pathname + location.search)) return;
      location.replace('/login?next=' + encodeURIComponent(location.pathname + location.search));
    };
    const origFetch = window.fetch;
    window.fetch = function () {
      return origFetch.apply(this, arguments).then((res) => {
        if (res && res.status === 401) toLogin();
        return res;
      });
    };
    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) {
      this.addEventListener('load', () => { if (this.status === 401) toLogin(); });
      return origOpen.apply(this, arguments);
    };
  }
  watchUnauthorized();

  /* ── 挂进宿主的侧栏（展开 after 新会话；折叠 insert 到 panelList 顶部） ── */
  function place() {
    const frame = document.querySelector('.pI_x6G_frame');
    if (!frame) return false;
    // 宿主真实的折叠动作就是把 frame 的 grid 模板列从 280px 改成 56px
    // （dsh-native.html 里的 btnCollapse 也是这么模拟的：COLS(56|280) + 重挂侧栏）。
    // 所以先认 grid 列，再退回量宽度。
    const cols = (frame.style.gridTemplateColumns || '').trim();
    const width = frame.getBoundingClientRect().width;
    const nowCollapsed = cols ? /^\s*56px/.test(cols) : width < 120;
    if (nowCollapsed !== collapsed) { collapsed = nowCollapsed; paint(); }
    // 折叠/展开时宿主会重渲染整棵侧栏子树，我们那份 host 会被一起丢掉 → 重新挂回去
    if (!host.isConnected) { /* fallthrough: 下面的 place 逻辑会重新插入 */ }

    const root = document.querySelector('.hHd-Xa_root');
    if (!root) return false;
    if (collapsed) {
      const list = root.querySelector('.hHd-Xa_panelList');
      if (list) { if (host.parentElement !== list) list.insertBefore(host, list.firstChild); return true; }
      if (host.parentElement !== root) root.appendChild(host);
      return true;
    }
    const anchor = [...root.querySelectorAll('button')]
      .find((b) => /New Session|新建|新会话/i.test(b.textContent || ''));
    if (anchor && anchor.parentElement) {
      // DSH 0.2.2-rc.3 起，「新会话」按钮**外面多包了一层**（.hHd-Xa_newSessionRow）✗
      // 直接 anchor.after(host) 会把我们这块塞进那一行里面 ✗（行内变成了小元素）
      // → 先认已知容器，再退化成"向上爬到只包着它的最外层"✓
      let target = anchor;
      const row = anchor.closest('.hHd-Xa_newSessionRow');
      if (row && row !== root) {
        target = row;
      } else {
        let p = anchor.parentElement;
        while (p && p !== root && p.children && p.children.length === 1) {
          target = p;
          p = p.parentElement;
        }
      }
      if (host.previousElementSibling !== target) target.after(host);
      return true;
    }
    if (host.parentElement !== root) root.appendChild(host);
    return true;
  }

  let scheduled = false;
  function schedulePlace() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; place(); dropLegacyRow(); });
  }

  /* ── 数据 ───────────────────────────────────────────────────────── */
  async function refresh() {
    try {
      // 实例页里的接口是**根相对**的（由实例代理转发），不是 /mu-api/*：
      // /me 返回 { user, role, instance:{name,state,live,port,started_at} }
      const me = await api('/me');
      state.user = me.user || '';
      const inst = me.instance || {};
      state.instance = inst.name || '';
      if (!state.instance) {
        host.remove();               // 管理员 / 未绑定：这块没有意义
        return;
      }
      state.running = inst.state === 'running' || inst.live === true;
      state.port = inst.port != null ? String(inst.port) : '';
      const res = inst.resources || {};
      state.logs = Array.isArray(me.logs) ? me.logs : [];
      const r2 = inst.resources || res;
      state.mem_used = r2.mem_used != null ? Math.round(r2.mem_used) : null;
      state.mem_max = r2.mem_max != null ? Math.round(r2.mem_max) : null;
      state.pids = r2.pids != null ? r2.pids : null;
      state.pids_max = r2.pids_max != null ? r2.pids_max : null;
      state.startedText = inst.started_at
        ? new Date(Number(inst.started_at) * 1000).toLocaleString() : '';
      state.cpu_pct = res.cpu_pct != null ? Math.round(res.cpu_pct) : null;
      state.mem_pct = res.mem_pct != null ? Math.round(res.mem_pct) : null;
      const started = Number(inst.started_at || 0);
      state.up = started ? (() => {
        const h = (Date.now() / 1000 - started) / 3600;
        return h < 1 ? Math.round(h * 60) + 'm' : h.toFixed(1) + 'h';
      })() : '—';
      state.loaded = true;
    } catch (e) {
      state.loaded = true;
    }
    paint();
    place();
  }

  // 宿主/旧注入里还有一行「● <实例> · 运行中」：设计稿要求这张卡取代它。
  // 它出现在侧栏面板列表（.hHd-Xa_panelList）里，按"含实例名 + 运行中"精确匹配删除，
  // 不动宿主别的条目（Plugins / Automation tasks 之类）。
  function dropLegacyRow() {
    // 不要在这里删除「实例状态」那一行：插它的 client plugin 自带 observer，
    // 删一次它就重建一次（并且每重建一次多一个 15s 轮询），会变成请求风暴 ✗。
    // 那一行已经在 plugin 源码里删掉了（dsh-client-models 1.8.2 起），这里只保留
    // 对"宿主自己渲染的重复行"的兜底清理。
    if (!state.instance) return;
    const name = state.instance;
    document.querySelectorAll('.hHd-Xa_panelList > *').forEach((el) => {
      const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (txt.indexOf(name) >= 0 && /运行中|running/i.test(txt) && !el.querySelector('#mu-shell-host')) {
        el.remove();
      }
    });
  }

  /* ── F2 的实例日志（最近 50 行，只读） ───────────────────────────── */
  const LVL = /\b(ERROR|ERR|FATAL)\b/i;
  const WARN = /\b(WARN|WARNING)\b/i;
  function logLineHTML(line) {
    const m = /^(\d{2}:\d{2}:\d{2}[.,]?\d*)\s+(.*)$/.exec(line);
    const time = m ? m[1] : '';
    const rest = m ? m[2] : line;
    const lvl = LVL.test(rest) ? 'err' : WARN.test(rest) ? 'warn' : 'info';
    const label = lvl === 'err' ? 'ERROR' : lvl === 'warn' ? 'WARN' : 'INFO';
    const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    return `<div class="lg">${time ? `<time>${esc(time)}</time>` : ''}`
      + `<span class="lvl ${lvl}">${label}</span>`
      + `<span class="msg">${esc(rest.replace(/^\[\w+\]\s*/, ''))}</span></div>`;
  }
  function renderLogs() {
    const box = q('#logbox');
    if (!box) return;
    const lines = Array.isArray(state.logs) ? state.logs : [];
    if (!lines.length) {
      box.innerHTML = '<div class="lg empty">' + (state.loaded ? '还没有日志。' : '读取中…') + '</div>';
      return;
    }
    box.innerHTML = lines.slice(-50).map(logLineHTML).join('');
    box.scrollTop = box.scrollHeight;
  }

  function boot() {
    if (!document.querySelector('.pI_x6G_frame')) { setTimeout(boot, 400); return; }
    document.body.appendChild(overlay);
    dropLegacyRow();
    refresh();
    new MutationObserver(schedulePlace).observe(document.documentElement, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'],
    });
    setInterval(() => { if (!document.hidden) refresh(); }, 20000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
