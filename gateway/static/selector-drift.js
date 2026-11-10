/* selector-drift.js — DSH 升级后的第一道安全网。
 *
 * 我们的六块注入全靠 DSH 自己的 DOM 结构与 CSS 令牌（真机快照 dsh-native.html 里抓的）。
 * DSH 一升级，这些就可能改名/换结构；这个脚本在**实例页面**里一条命令跑完，
 * 直接告诉你"哪一块会坏"。
 *
 * 用法（浏览器控制台，在实例页里）：
 *   const s = await (await fetch('/mu-static/selector-drift.js?v=' + Date.now())).text();
 *   eval(s); await window.dshMuSelectorDrift();
 *
 * 输出：控制台一张表 + window.__selectorDrift（可 JSON.stringify 贴回来）。
 */
(function () {
  'use strict';

  const CHECKS = [
    // ---- 侧栏外壳（实例卡挂载点 + 折叠信号）
    { group: '侧栏', name: '.pI_x6G_frame', sel: '.pI_x6G_frame', need: true,
      why: '侧栏 grid 外壳；折叠状态靠它的 grid-template-columns（280px ↔ 56px）' },
    { group: '侧栏', name: '.hHd-Xa_root', sel: '.hHd-Xa_root', need: true,
      why: '侧栏根节点；实例卡要挂进它里面' },
    { group: '侧栏', name: '.hHd-Xa_panelList', sel: '.hHd-Xa_panelList', need: true,
      why: '折叠态把图标塞进这个列表（和 Plugins/Automation 同 gap）' },
    { group: '侧栏', name: '.hHd-Xa_newSession', sel: '.hHd-Xa_newSession', need: false,
      why: '展开态把卡片插在「新会话」之后（找不到会退化成追加到 root）' },

    // ---- 设置弹窗（我们只填右栏）
    { group: '设置', name: '设置面板壳 .VOzbGW_panel', sel: '.VOzbGW_panel', need: false,
      why: '宿主设置弹窗的壳；我们注册 settings.section，壳改名会影响布局' },
    { group: '设置', name: '设置导航项 .VOzbGW_navCell', sel: '.VOzbGW_navCell', need: false,
      why: '设置左侧导航；我们的页签「模型」挂在这里' },

    // ---- 账号菜单（我们在里面注入三项）
    { group: '更多', name: '[role="menu"]', sel: '[role="menu"]', need: false,
      why: '账号菜单容器；三项注入靠它（需要先把菜单点开再跑本脚本）' },

    // ---- 我们自己的注入
    { group: '注入', name: '#mu-shell-host', sel: '#mu-shell-host', need: true,
      why: '侧栏实例卡的 shadow 宿主（由 /mu-static/mu-shell.js 注入）' },
    { group: '注入', name: '卡片 .card', sel: '#mu-shell-host', shadow: '#card', need: true,
      why: '展开态卡片（row1/row2/bars）' },
    { group: '注入', name: 'F2 面板 #f2', sel: 'body', shadow: null, need: false,
      why: 'F2 控制中心（在 overlay 的 shadow root 里，脚本会单独检查）' },
  ];

  // 我们依赖的宿主令牌：变了颜色会"跑偏"，但不会崩，所以算 WARN
  const TOKENS = [
    '--dsw-alias-bg-base', '--dsw-alias-bg-layer-1', '--dsw-alias-label-primary',
    '--dsw-alias-label-caption', '--dsw-alias-border-l1', '--dsw-alias-border-l2',
    '--dsw-alias-interactive-bg-hover', '--dsw-alias-label-dimmed',
    '--dsw-font-family', '--dsw-font-family-code', '--dsw-radius-lg',
  ];

  const rows = [];
  const add = (group, name, state, detail) => rows.push({ group, name, state, detail });

  function shadowQuery(host, sel) {
    if (!host) return null;
    if (host.shadowRoot) return host.shadowRoot.querySelector(sel);
    return null;
  }

  function run() {
    rows.length = 0;

    // ① 选择器
    for (const c of CHECKS) {
      const host = document.querySelector(c.sel);
      if (!host) {
        add(c.group, c.name, c.need ? 'FAIL' : 'WARN', `找不到 → ${c.why}`);
        continue;
      }
      if (c.shadow) {
        const inner = shadowQuery(host, c.shadow);
        add(c.group, c.name, inner ? 'PASS' : 'FAIL',
          inner ? 'ok' : `宿主在但 shadow 里找不到 ${c.shadow} → ${c.why}`);
      } else {
        add(c.group, c.name, 'PASS', 'ok');
      }
    }

    // ② 折叠信号：grid-template-columns 的形状
    const frame = document.querySelector('.pI_x6G_frame');
    if (frame) {
      const cols = (frame.style.gridTemplateColumns || '').trim();
      const computed = getComputedStyle(frame).gridTemplateColumns || '';
      const ok = /^\s*\d+px/.test(cols) || /^\s*\d+px/.test(computed);
      add('侧栏', 'grid-template-columns 形状', ok ? 'PASS' : 'WARN',
        `inline="${cols || '(空)'}" computed="${computed}"`
        + (ok ? '' : ' → 折叠检测可能退化成量宽度'));
      const width = frame.getBoundingClientRect().width;
      add('侧栏', '折叠判定（宽 < 120 视为折叠）', 'INFO', `当前宽 ${Math.round(width)}px`);
    }

    // ③ 令牌
    const cs = getComputedStyle(document.documentElement);
    const missing = TOKENS.filter((k) => !cs.getPropertyValue(k).trim());
    add('令牌', `${TOKENS.length} 个 --dsw-* 令牌`, missing.length ? 'WARN' : 'PASS',
      missing.length ? '缺失：' + missing.join(', ') : '全部存在');

    // ④ 我们自己的注入是否完整（卡片三行结构）
    const cardHost = document.querySelector('#mu-shell-host');
    const card = shadowQuery(cardHost, '#card') || shadowQuery(cardHost, '#rail');
    if (card) {
      const kind = card.id === 'rail' ? 'rail(折叠态)' : 'card(展开态)';
      const parts = card.id === 'rail'
        ? ['svg.ico', '.badge']
        : ['.row1', '.row2', '.bars'];
      const lost = parts.filter((s) => !card.querySelector(s));
      add('注入', `卡片结构（${kind}）`, lost.length ? 'FAIL' : 'PASS',
        lost.length ? '缺失：' + lost.join(', ') : parts.join(' / ') + ' 都在');
    }

    // ⑤ F2 面板（在 overlay 的 shadow root 里，只能按属性找）
    const overlays = [...document.querySelectorAll('div')]
      .filter((el) => el.shadowRoot && el.shadowRoot.querySelector('#f2'));
    const f2 = overlays.length ? overlays[0].shadowRoot : null;
    add('注入', 'F2 面板（含日志/重启/退出）', f2 ? 'PASS' : 'WARN',
      f2 ? 'ok' : '没找到（可能是页面刚加载还没注入，稍后重跑）');
    if (f2) {
      const need = ['#f2name', '#f2state', '#f2cpu', '#f2mem', '#f2pids',
        '#logbox', '#f2restart', '#f2logout', '#f2close'];
      const lost = need.filter((s) => !f2.querySelector(s));
      add('注入', 'F2 内部元素', lost.length ? 'FAIL' : 'PASS',
        lost.length ? '缺失：' + lost.join(', ') : '9 个元素都在');
    }

    // ⑥ 握手幂等（我们是"ready 只回一次 init"）
    add('协议', '宿主 mu-shell 版本', 'INFO',
      (window.__muShellLoaded ? '已注入' : '未注入 / 非实例页'));

    // ---- 打印
    const bad = rows.filter((r) => r.state === 'FAIL').length;
    const warn = rows.filter((r) => r.state === 'WARN').length;
    if (console.table) {
      console.table(rows.map((r) => ({ 组: r.group, 项: r.name, 结果: r.state, 说明: r.detail })));
    } else {
      rows.forEach((r) => console.log(r.state, r.group, r.name, '—', r.detail));
    }
    const summary = { fail: bad, warn, rows };
    window.__selectorDrift = summary;
    console.log(`%cselector-drift: ${bad} FAIL / ${warn} WARN / ${rows.length - bad - warn} PASS`,
      `color:${bad ? '#c0392b' : warn ? '#8a5a00' : '#16a34a'};font-weight:600`);
    console.log('把 window.__selectorDrift 复制回贴给我即可（JSON）。');
    return summary;
  }

  window.dshMuSelectorDrift = function () {
    // 有菜单需要先点开：给一句提示，然后照常跑
    if (!document.querySelector('[role="menu"]')) {
      console.log('%c提示：先把「更多」菜单点开再跑，否则「更多」那几项会报 WARN。',
        'color:#8a5a00');
    }
    return run();
  };
  console.log('%cselector-drift 已就绪：await window.dshMuSelectorDrift()', 'color:#4176e6');
})();
