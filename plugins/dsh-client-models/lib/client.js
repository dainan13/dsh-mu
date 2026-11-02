// dsh-mu: the platform-managed side of the Models settings.
//
// Data comes from the gateway's session-scoped /me endpoint (same origin as the
// instance page), so no new auth path is needed: the login cookie that opened this
// page is what authorises the read/write. Keys are sent to the platform and stored
// there; the instance only ever carries a placeholder env name.
window.__ModuleLoader__.load({
  id: '@dsh-mu/dsh-client-models',
  factory: (require) => {
    const react = require('react');
    // Overlays must live outside the sidebar (it clips its overflow and collapses to
    // icons), so they are mounted on <body> with whichever React renderer this host
    // ships. A host with neither simply gets no overlay instead of a broken plugin.
    // Same icon library DSH's own menu uses (its client-ui-primitives package), so the
    // two appended rows look like they were always there. If the host does not expose it,
    // the rows simply render without an icon.
    const muIcons = (() => {
      try { return require('@deepseek-ai/dsh-client-ui-primitives'); } catch (e) { return null; }
    })();
    const muIcon = (name) => {
      const C = muIcons && muIcons[name];
      return C ? jsx.jsx(C, { size: 16 }) : null;
    };

    const muRenderer = (() => {
      try {
        const c = require('react-dom/client');
        if (c && typeof c.createRoot === 'function') {
          return (node) => {
            const root = c.createRoot(node);
            return { show: (el) => root.render(el), hide: () => root.unmount() };
          };
        }
      } catch (e) { /* fall through */ }
      try {
        const d = require('react-dom');
        if (d && typeof d.render === 'function') {
          return (node) => ({ show: (el) => d.render(el, node),
                              hide: () => d.unmountComponentAtNode(node) });
        }
      } catch (e) { /* give up */ }
      return null;
    })();
    const jsx = require('react/jsx-runtime');

    // A plan change only lands in the instance after it restarts (the provider list is
    // read from the launch profile), so every successful write applies immediately
    // instead of leaving the chat showing the previous set of models.
    const applyNow = async () => {
      try {
        await fetch('/me', { method: 'POST', headers: { 'content-type': 'application/json' },
                             body: JSON.stringify({ action: 'restart' }) });
        return true;
      } catch (e) { return false; }
    };

    const api = async (init) => {
      const res = await fetch('/me', init);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      return body;
    };

    // ---------------------------------------------------------- instance status
    //
    // One chip in the sidebar footer replaces three drawer entries: the instance state,
    // the password dialog and the restart confirmation. It talks to the gateway's own
    // session API (`/me`), which the browser cookie already authenticates, so the plugin
    // needs no token of its own.
    function MuStatusChip(props) {
      const { useState, useEffect } = react;
      const [me, setMe] = useState(null);
      const [open, setOpen] = useState(false);
      const [busy, setBusy] = useState('');
      const [note, setNote] = useState('');

      const load = () => fetch('/me', { headers: { accept: 'application/json' } })
        .then((r) => r.json()).then(setMe).catch(() => {});
      useEffect(() => {
        load();
        const timer = setInterval(load, 15000);
        return () => clearInterval(timer);
      }, []);

      const inst = (me && me.instance) || {};
      const running = !!inst.live;
      const dot = running ? '#22c55e' : '#ef4444';
      const who = (me && me.user) || '...';
      const state = me ? (running ? '运行中' : '已停止') : '读取中';

      const post = async (body) => {
        const res = await fetch('/me', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        });
        let doc = {};
        try { doc = await res.json(); } catch (e) { /* no body */ }
        if (!res.ok) throw new Error((doc && (doc.error || doc.detail)) || ('HTTP ' + res.status));
        return doc;
      };

      const changePassword = async () => {
        const next = window.prompt('新的登录密码：');
        if (!next) return;
        setBusy('password'); setNote('');
        try { await post({ action: 'password', password: next }); setNote('密码已修改'); }
        catch (e) { setNote('失败：' + e.message); }
        setBusy('');
      };

      const restart = async () => {
        if (!window.confirm('重启这个实例？正在进行的会话会中断。')) return;
        setBusy('restart'); setNote('');
        try { await post({ action: 'restart' }); setNote('正在重启，几秒后刷新');
          setTimeout(load, 6000); }
        catch (e) { setNote('失败：' + e.message); }
        setBusy('');
      };

      const logout = () => { window.location.href = '/logout'; };

      const item = (label, onClick, disabled) => jsx.jsx('button', {
        type: 'button', disabled: !!disabled, onClick,
        style: { display: 'block', width: '100%', textAlign: 'left', padding: '6px 10px',
                 margin: 0, border: 0, background: 'transparent', color: 'inherit',
                 fontSize: 13, cursor: disabled ? 'default' : 'pointer', borderRadius: 6 },
        children: label,
      });

      return jsx.jsxs('div', { style: { position: 'relative' }, children: [
        jsx.jsxs('button', {
          type: 'button', onClick: () => { if (props && props.onOpen) props.onOpen(); else setOpen(!open); },
          title: who + ' · 实例 ' + (inst.port ? ':' + inst.port : ''),
          style: { display: 'flex', alignItems: 'center', gap: 6,
                   padding: '4px 8px', border: 0, background: 'transparent',
                   color: 'inherit', fontSize: 12, cursor: 'pointer', borderRadius: 6,
                   whiteSpace: 'nowrap' },
          children: [
            jsx.jsx('span', { style: { width: 7, height: 7, borderRadius: '50%',
                                       background: dot, flex: '0 0 auto' } }),
            jsx.jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis',
                                       whiteSpace: 'nowrap' },
                              children: who + ' · ' + state }),
          ],
        }),
        open ? jsx.jsxs('div', {
          style: { position: 'absolute', bottom: '100%', left: 0, marginBottom: 6,
                   minWidth: 168, padding: 6, borderRadius: 8, zIndex: 50,
                   background: 'var(--dsw-alias-bg-elevated, #fff)',
                   boxShadow: '0 6px 24px rgba(0,0,0,.18)',
                   border: '1px solid var(--dsw-alias-border-default, #e5e7eb)' },
          children: [
            jsx.jsx('div', { style: { fontSize: 11, opacity: 0.6, padding: '2px 10px 6px' },
              children: '实例 ' + (inst.port ? ':' + inst.port : '未启动') }),
            item(busy === 'password' ? '修改密码…' : '修改密码', changePassword, busy !== ''),
            item(busy === 'restart' ? '重启中…' : '重启实例', restart, busy !== ''),
            item('退出登录', logout, false),
            note ? jsx.jsx('div', { style: { fontSize: 11, padding: '6px 10px 0', opacity: 0.8 },
                                     children: note }) : null,
          ],
        }) : null,
      ] });
    }

    // ------------------------------------------------------------ default model
    function MuDefaultModel() {
      const { useState, useEffect } = react;
      const [me, setMe] = useState(null);
      const [note, setNote] = useState('');
      useEffect(() => {
        fetch('/me', { headers: { accept: 'application/json' } })
          .then((r) => r.json()).then(setMe).catch(() => {});
      }, []);
      if (!me) return jsx.jsx('div', { style: { fontSize: 12, opacity: 0.6 }, children: '读取中…' });
      const plans = ((((me.plans || {}).plans)) || []).filter((p) => (p.models || []).length);
      const current = me.default || {};
      const value = current.plan && current.model ? current.plan + '|' + current.model : '';
      const save = async (raw) => {
        if (!raw) return;
        const parts = raw.split('|');
        try {
          const res = await fetch('/me', { method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ action: 'plan-default', plan: parts[0], model: parts[1] }) });
          const doc = await res.json().catch(() => ({}));
          setNote(res.ok ? '已设为默认模型' : '失败：' + ((doc && doc.error) || res.status));
        } catch (e) { setNote('失败：' + e.message); }
      };
      const options = [jsx.jsx('option', { value: '', children: '（保持平台默认）' }, '__none')];
      plans.forEach((p) => {
        options.push(jsx.jsx('optgroup', { label: p.name || p.id, children:
          (p.models || []).map((mid) => jsx.jsx('option',
            { value: p.id + '|' + mid, children: mid }, p.id + '|' + mid)) }, p.id));
      });
      return jsx.jsxs('div', { children: [
        jsx.jsx('div', { style: { fontSize: 12, opacity: 0.7, marginBottom: 4 },
          children: '新会话默认用这个模型（只影响你自己的账号）' }),
        jsx.jsx('select', {
          defaultValue: value, onChange: (e) => save(e.target.value),
          style: { width: '100%', maxWidth: 420, padding: '6px 8px', borderRadius: 6,
                   fontSize: 13, background: 'transparent', color: 'inherit' },
          children: options,
        }),
        note ? jsx.jsx('div', { style: { fontSize: 12, marginTop: 4, opacity: 0.8 },
                               children: note }) : null,
      ] });
    }

    function MuModelsSection() {
      const { useState, useEffect, useCallback } = react;
      const [state, setState] = useState({ loading: true, error: '', providers: [], plans: [], user: '' });
      const [form, setForm] = useState({ name: '', provider: '', key: '' });
      const [msg, setMsg] = useState('');
      const [busy, setBusy] = useState(false);

      const load = useCallback(async () => {
        try {
          const d = await api();
          setState({
            loading: false, error: '',
            user: d.user || '',
            providers: d.providers || [],
            plans: (d.plans || []).filter((p) => p.own),
          });
          if (!form.provider && (d.providers || []).length) {
            setForm((f) => ({ ...f, provider: d.providers[0].id }));
          }
        } catch (e) {
          setState((s) => ({ ...s, loading: false, error: e.message }));
        }
      }, [form.provider]);

      useEffect(() => { load(); }, []);

      const submit = async () => {
        if (!form.name.trim() || !form.provider || !form.key) {
          setMsg('套餐名、provider、key 都要填。'); return;
        }
        setBusy(true); setMsg('');
        try {
          await api({ method: 'POST', headers: { 'content-type': 'application/json' },
                      body: JSON.stringify({ action: 'plan-add', name: form.name.trim(),
                                             provider: form.provider, key: form.key }) });
          setForm((f) => ({ ...f, key: '' }));
          setMsg('已保存，正在重启你的实例…');
          const ok = await applyNow();
          setMsg(ok ? '已保存并重启实例：几秒后刷新页面，就能在会话里选到它。'
                    : '已保存；自动重启失败，请手动重启实例后生效。');
          await load();
        } catch (e) { setMsg('保存失败：' + e.message); }
        setBusy(false);
      };

      const act = async (action, id) => {
        setBusy(true); setMsg('');
        try {
          await api({ method: 'POST', headers: { 'content-type': 'application/json' },
                      body: JSON.stringify({ action: `plan-${action}`, id }) });
          setMsg(action === 'delete' ? '已删除，正在重启实例…' : '已更新，正在重启实例…');
          const ok2 = await applyNow();
          setMsg(ok2 ? '已生效（实例已重启，几秒后刷新）。' : '已保存；自动重启失败，请手动重启实例。');
          await load();
        } catch (e) { setMsg('操作失败：' + e.message); }
        setBusy(false);
      };

      const row = (label, child) => jsx.jsxs('div', {
        style: { display: 'flex', gap: 10, alignItems: 'center', marginBottom: 8 },
        children: [
          jsx.jsx('span', { style: { width: 74, opacity: 0.75, fontSize: 13 }, children: label }),
          child,
        ],
      }, label);

      const input = (props) => jsx.jsx('input', {
        style: { flex: 1, padding: '6px 8px', borderRadius: 6,
                 border: '1px solid var(--dsw-alias-border-default, #d0d4da)',
                 background: 'transparent', color: 'inherit', fontSize: 13 },
        ...props,
      });

      if (state.loading) return jsx.jsx('div', { style: { padding: 16 }, children: '读取平台配置…' });
      if (state.error) {
        return jsx.jsxs('div', { style: { padding: 16, lineHeight: 1.8 }, children: [
          jsx.jsx('h2', { style: { margin: '0 0 8px' }, children: '模型（dsh-mu）' }),
          jsx.jsx('div', { style: { opacity: 0.8 }, children: '读不到平台配置：' + state.error }),
          jsx.jsx('div', { style: { opacity: 0.6, fontSize: 12, marginTop: 6 },
                           children: '如果你是通过实例端口直接打开的，而不是平台首页，请改用平台地址访问。' }),
        ] });
      }

      return jsx.jsxs('div', { style: { padding: '8px 4px', fontSize: 14, lineHeight: 1.8 }, children: [
        jsx.jsx('h2', { style: { margin: '0 0 4px' }, children: '模型（dsh-mu）' }),
        jsx.jsx('p', { style: { margin: '0 0 14px', opacity: 0.7, fontSize: 13 },
          children: `当前账号：${state.user || '（未知）'}。平台托管的 provider 只能选、不能改；key 保存在平台，实例里不落地。` }),

        jsx.jsx('h3', { style: { fontSize: 13, margin: '0 0 8px' }, children: '添加我自己的 key' }),
        row('套餐名', input({ value: form.name, placeholder: '我的 groq',
                              onChange: (e) => setForm((f) => ({ ...f, name: e.target.value })) })),
        row('provider', jsx.jsx('select', {
          value: form.provider,
          onChange: (e) => setForm((f) => ({ ...f, provider: e.target.value })),
          style: { flex: 1, padding: '6px 8px', borderRadius: 6, background: 'transparent',
                   color: 'inherit', fontSize: 13 },
          children: state.providers.length
            ? state.providers.map((p) => jsx.jsxs('option', {
                value: p.id,
                children: [`${p.id} · ${p.label || ''}（${(p.models || []).length} 模型`,
                           p.origin === 'dsh' ? ' · DSH' : '', '）'],
              }, p.id))
            : jsx.jsx('option', { value: '', children: '（管理员还没有导入可托管的 provider）' }),
        })),
        row('API key', input({ type: 'password', value: form.key, placeholder: '只发送到平台，0600 保存',
                                autoComplete: 'new-password',
                                onChange: (e) => setForm((f) => ({ ...f, key: e.target.value })) })),
        jsx.jsx('div', { style: { marginLeft: 84 }, children: jsx.jsx('button', {
          type: 'button', disabled: busy, onClick: submit,
          style: { padding: '6px 14px', borderRadius: 6, cursor: 'pointer', fontSize: 13 },
          children: busy ? '保存中…' : '保存',
        }) }),

        msg ? jsx.jsx('p', { style: { margin: '10px 0 0', fontSize: 13, opacity: 0.85 }, children: msg }) : null,

        jsx.jsx('h3', { style: { fontSize: 13, margin: '20px 0 8px' }, children: '我添加的' }),
        state.plans.length
          ? jsx.jsx('table', { style: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
            children: jsx.jsx('tbody', { children: state.plans.map((p) => jsx.jsxs('tr', {
              children: [
                jsx.jsx('td', { style: { padding: '6px 8px 6px 0' }, children: jsx.jsx('b', { children: p.name }) }),
                jsx.jsx('td', { style: { padding: '6px 8px', opacity: 0.75 }, children: p.provider }),
                jsx.jsx('td', { style: { padding: '6px 8px', opacity: 0.75 },
                                children: p.has_key ? 'key 已保存' : 'key 缺失' }),
                jsx.jsx('td', { style: { padding: '6px 0', opacity: 0.75 },
                                children: `${(p.effective_models || []).length} 个模型` }),
                jsx.jsx('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' }, children: [
                  jsx.jsx('button', { type: 'button', disabled: busy, onClick: () => act('probe', p.id),
                                      style: { marginRight: 6, fontSize: 12 }, children: '检测可用模型' }),
                  jsx.jsx('button', { type: 'button', disabled: busy, onClick: () => act('delete', p.id),
                                      style: { fontSize: 12 }, children: '删除' }),
                ] }),
              ],
            }, p.id)) }),
          })
          : jsx.jsx('div', { style: { opacity: 0.6, fontSize: 13 }, children: '还没有。' }),

        jsx.jsx('h3', { style: { fontSize: 13, margin: '20px 0 6px' }, children: '我的默认模型' }),
        jsx.jsx(MuDefaultModel, {}),
        jsx.jsx('h3', { style: { fontSize: 13, margin: '20px 0 6px' }, children: 'Copilot / Codex' }),
        jsx.jsx('div', { style: { opacity: 0.7, fontSize: 13 },
          children: '这两个没有静态 API key，所以不在这里配置：Copilot 由平台代为换取短时令牌'
            + '（需要账号有 Copilot 权益，正在验证）；Codex 目前未提供入口。' }),
      ] });
    }

    function apply(ctx) {
      // ---------------------------------------------------------------- surfaces
      //
      // Overlays are fixed-position and appended to <body>, never nested inside the
      // sidebar: the sidebar collapses to icons and clips its own overflow, so anything
      // anchored inside it cannot overlay or close properly.
      const muOverlay = (render) => {
        if (!muRenderer) { window.alert('当前界面不支持浮层'); return null; }
        const host = document.createElement('div');
        host.setAttribute('data-dsh-mu-overlay', '1');
        host.style.cssText = 'position:fixed;inset:0;z-index:2147483000;'
          + 'display:flex;align-items:center;justify-content:center;'
          + 'background:rgba(15,23,42,.35)';
        const handle = muRenderer(host);
        const close = () => { try { handle.hide(); } catch (e) {} host.remove(); };
        host.addEventListener('mousedown', (e) => { if (e.target === host) close(); });
        document.body.appendChild(host);
        handle.show(render(close));
        return close;
      };

      const muOpenSettings = () => {
        try { ctx.shortcuts.run('settings.open'); return true; } catch (e) { /* fall through */ }
        try {
          document.dispatchEvent(new KeyboardEvent('keydown',
            { key: ',', code: 'Comma', ctrlKey: true, altKey: true, bubbles: true }));
          return true;
        } catch (e) { return false; }
      };

      const muCard = (title, children, close) => jsx.jsxs('div', {
        style: { width: 460, maxWidth: '92vw', maxHeight: '80vh', overflow: 'auto',
                 borderRadius: 12, background: 'var(--dsw-alias-bg-elevated, #fff)',
                 color: 'inherit', boxShadow: '0 18px 48px rgba(0,0,0,.28)',
                 border: '1px solid var(--dsw-alias-border-default, #e5e7eb)' },
        onMouseDown: (e) => e.stopPropagation(),
        children: [
          jsx.jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8,
                                     padding: '12px 16px',
                                     borderBottom: '1px solid var(--dsw-alias-border-default, #eee)' },
            children: [
              jsx.jsx('div', { style: { fontWeight: 600, fontSize: 14 }, children: title }),
              jsx.jsx('span', { style: { flex: 1 } }),
              jsx.jsx('button', { type: 'button', onClick: close,
                style: { border: 0, background: 'transparent', color: 'inherit',
                         cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: 4 },
                children: '×' }),
            ] }),
          jsx.jsx('div', { style: { padding: 16, fontSize: 13 }, children }),
        ],
      });

      const muBtn = (label, onClick, primary) => jsx.jsx('button', {
        type: 'button', onClick,
        style: { padding: '6px 12px', borderRadius: 6, fontSize: 13, cursor: 'pointer',
                 border: '1px solid var(--dsw-alias-border-default, #d0d4da)',
                 background: primary ? 'var(--dsw-alias-bg-accent, #2563eb)' : 'transparent',
                 color: primary ? '#fff' : 'inherit' },
        children: label,
      });

      const muRow = (k, v) => jsx.jsxs('div', { style: { display: 'flex', gap: 12,
                                                          padding: '3px 0' }, children: [
        jsx.jsx('div', { style: { width: 96, opacity: 0.65 }, children: k }),
        jsx.jsx('div', { style: { flex: 1 }, children: v }),
      ] });

      function MuInstancePanel(props) {
        const [me, setMe] = react.useState(null);
        react.useEffect(() => { load(); }, []);
        const load = () => fetch('/me', { headers: { accept: 'application/json' } })
          .then((r) => r.json()).then(setMe).catch(() => {});
        const inst = (me && me.instance) || {};
        const res = inst.resources || {};
        const restart = async () => {
          if (!window.confirm('重启这个实例？正在进行的会话会中断。')) return;
          try {
            await fetch('/me', { method: 'POST', headers: { 'content-type': 'application/json' },
                                  body: JSON.stringify({ action: 'restart' }) });
            window.alert('正在重启，几秒后刷新');
          } catch (e) { window.alert('重启失败：' + (e && e.message || e)); }
        };
        return muCard('实例信息', [
          muRow('用户', inst.user || (me && me.user) || '—'),
          muRow('状态', inst.live ? '运行中' : '已停止'),
          muRow('端口', inst.port ? '127.0.0.1:' + inst.port : '未启动'),
          muRow('启动时间', inst.started_at ? new Date(inst.started_at * 1000).toLocaleString() : '—'),
          muRow('CPU', res.cpu ? res.cpu : '—'),
          muRow('内存', res.memory && res.memory.current
            ? Math.round(res.memory.current / 1048576) + ' MB' : '—'),
          jsx.jsxs('div', { style: { marginTop: 14, display: 'flex', gap: 8 }, children: [
            muBtn('刷新', load),
            muBtn('重启实例', restart, true),
          ] }),
        ], props.close);
      }

      // F2: the system surface. The settings drawer stays for now, but the panel a user
      // reaches with one key is this one -- instance health, the restart switch and a way
      // into the settings drawer, laid over everything else instead of docking at a side.
      function MuSystemPanel(props) {
        const [me, setMe] = react.useState(null);
        const [note, setNote] = react.useState('');
        const load = () => fetch('/me', { headers: { accept: 'application/json' } })
          .then((r) => r.json()).then(setMe).catch(() => {});
        react.useEffect(() => { load(); const timer = setInterval(load, 5000); return () => clearInterval(timer); }, []);
        const inst = (me && me.instance) || {};
        const res = inst.resources || {};
        const up = inst.started_at
          ? Math.max(0, Math.floor(Date.now() / 1000 - inst.started_at)) : 0;
        const human = (s) => {
          const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
          const m = Math.floor((s % 3600) / 60);
          return d ? d + '天 ' + h + '时' : h ? h + '时 ' + m + '分' : m + '分 ' + (s % 60) + '秒';
        };
        const mb = (v) => (v ? Math.round(v / 1048576) + ' MB' : '—');
        const restart = async () => {
          if (!window.confirm('重启这个实例？正在进行的会话会中断。')) return;
          try {
            await fetch('/me', { method: 'POST', headers: { 'content-type': 'application/json' },
                                  body: JSON.stringify({ action: 'restart' }) });
            setNote('正在重启，几秒后刷新');
          } catch (e) { setNote('失败：' + (e && e.message || e)); }
        };
        return muCard('系统面板 · F2', [
          muRow('用户', (me && me.user) || '—'),
          muRow('实例状态', inst.live ? '运行中' : '已停止'),
          muRow('端口', inst.port ? '127.0.0.1:' + inst.port : '未启动'),
          muRow('已运行', up ? human(up) : '—'),
          muRow('CPU', res.cpu ? res.cpu : '—'),
          muRow('内存', mb(res.memory && res.memory.current)),
          muRow('套餐', (((me && me.plans && me.plans.plans) || []).length) + ' 个'),
          jsx.jsxs('div', { style: { marginTop: 16, display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [
            muBtn('刷新', load),
            muBtn('重启实例', restart, true),
            muBtn('打开设置', () => { props.close(); muOpenSettings(); }),
          ] }),
          note ? jsx.jsx('div', { style: { marginTop: 10, fontSize: 12, opacity: 0.8 }, children: note }) : null,
        ], props.close);
      }

      // F2 toggles the system panel; Escape closes whatever overlay is on top.
      try {
        const muKey = (e) => {
          if (e.key === 'F2' && !e.ctrlKey && !e.altKey && !e.metaKey) {
            e.preventDefault();
            const open = document.querySelector('[data-dsh-mu-overlay] [data-dsh-mu-system]');
            if (open) { open.dispatchEvent(new CustomEvent('dsh-mu-close')); return; }
            muOverlay((close) => jsx.jsx(MuSystemPanel, { close }));
          }
        };
        document.addEventListener('keydown', muKey, true);
      } catch (e) { /* cosmetic only */ }

      function MuAccountPanel(props) {
        const [me, setMe] = react.useState(null);
        const [note, setNote] = react.useState('');
        react.useEffect(() => {
          fetch('/me', { headers: { accept: 'application/json' } })
            .then((r) => r.json()).then(setMe).catch(() => {});
        }, []);
        const inst = (me && me.instance) || {};
        const change = async () => {
          const next = window.prompt('新的登录密码：');
          if (!next) return;
          try {
            const res = await fetch('/me', { method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ action: 'password', password: next }) });
            const doc = await res.json().catch(() => ({}));
            setNote(res.ok ? '密码已修改' : '失败：' + ((doc && doc.error) || res.status));
          } catch (e) { setNote('失败：' + (e && e.message || e)); }
        };
        return muCard('我的账号', [
          muRow('用户名', (me && me.user) || '—'),
          muRow('角色', (me && me.role) || '—'),
          muRow('实例', inst.port ? ':' + inst.port : '未启动'),
          muRow('套餐', ((me && me.plans && me.plans.plans) || []).length + ' 个'),
          jsx.jsxs('div', { style: { marginTop: 14, display: 'flex', gap: 8,
                                     alignItems: 'center' }, children: [
            muBtn('修改密码', change, true),
            note ? jsx.jsx('span', { style: { fontSize: 12, opacity: 0.8 }, children: note }) : null,
          ] }),
        ], props.close);
      }

      function MuMoreMenu(props) {
        const items = [
          ['设置', () => { props.close(); muOpenSettings(); }],
          ['我的账号', () => muOverlay((close) => jsx.jsx(MuAccountPanel, { close }))],
          ['退出登录', () => { window.location.href = '/logout'; }],
        ];
        return jsx.jsx('div', {
          style: { minWidth: 148, padding: 6, borderRadius: 10,
                   background: 'var(--dsw-alias-bg-elevated, #fff)',
                   boxShadow: '0 10px 32px rgba(0,0,0,.22)',
                   border: '1px solid var(--dsw-alias-border-default, #e5e7eb)',
                   position: 'fixed', left: 12, bottom: 56, zIndex: 2147483001 },
          onMouseDown: (e) => e.stopPropagation(),
          children: items.map(([label, run]) => jsx.jsx('button', {
            type: 'button', onClick: run,
            style: { display: 'block', width: '100%', textAlign: 'left', padding: '7px 10px',
                     border: 0, background: 'transparent', color: 'inherit', fontSize: 13,
                     cursor: 'pointer', borderRadius: 6 },
            children: label,
          }, label)),
        });
      }

      // One compact entry in the sidebar foot: the status chip. Clicking it opens the
      // instance panel as an overlay (the instance panel has the restart button).
      const MuStatusEntry = () => jsx.jsx(MuStatusChip, { onOpen: () =>
        muOverlay((close) => jsx.jsx(MuInstancePanel, { close })) });
      const MuMoreEntry = () => jsx.jsx('button', {
        type: 'button', onClick: () => muOverlay((close) => jsx.jsx(MuMoreMenu, { close })),
        style: { display: 'flex', alignItems: 'center', gap: 6, padding: '4px 8px', border: 0,
                 background: 'transparent', color: 'inherit', fontSize: 12,
                 cursor: 'pointer', borderRadius: 6, whiteSpace: 'nowrap' },
        children: '··· 更多',
      });

      // This platform does not use two of DSH's account entries, and it does want two of
      // its own in the same place. Both are done on the DOM, because that menu is rendered
      // by a package that exposes no slot: a slot can add surfaces, never remove or extend
      // a list it owns.
      const muMenuSweep = () => {
        for (const menu of document.querySelectorAll('[role="menu"]')) {
          // Only the account menu: it is the one that carries the feedback entry and the
          // settings shortcut hint. Other menus (the panel list has its own "更多") must
          // be left completely alone.
          const rows = Array.from(menu.querySelectorAll('[role="menuitem"], button, li, a'));
          const texts = rows.map((el) => (el.textContent || '').trim());
          const isAccountMenu = texts.includes('意见反馈')
            || menu.textContent.indexOf('Ctrl + Alt + ,') >= 0;
          if (!isAccountMenu) continue;
          // a) hide the entries this platform replaces
          for (const el of rows) {
            const text = (el.textContent || '').trim();
            if (text === '意见反馈' || text === '登录') el.style.display = 'none';
          }
          // b) append ours once, styled like the native rows
          if (menu.querySelector('[data-dsh-mu-menu-items]')) continue;
          // Mirror the native row exactly: same wrapper, same item classes, same inner
          // spans. DSH's own CSS then supplies hover, spacing and focus for free, and if
          // it renames those classes we simply fall back to the plain style below.
          const native = rows.find((el) => (el.textContent || '').trim().startsWith('设置'));
          const wrapCls = native && native.parentElement ? native.parentElement.className : '';
          const itemCls = native ? native.className : '';
          const iconEl = native ? native.querySelector('[class*="itemIcon"]') : null;
          const labelEl = native ? native.querySelector('[class*="itemLabel"]') : null;
          const iconCls = iconEl ? iconEl.className : '';
          const labelCls = labelEl ? labelEl.className : '';

          const box = document.createElement('div');
          box.setAttribute('data-dsh-mu-menu-items', '1');
          const closeMenu = () => {
            try { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); } catch (err) {}
          };
          const addRow = (label, iconName, run) => {
            const wrap = document.createElement('div');
            if (wrapCls) wrap.className = wrapCls;
            const button = document.createElement('button');
            button.type = 'button';
            button.setAttribute('role', 'menuitem');
            if (itemCls) {
              button.className = itemCls;
            } else {
              button.style.cssText = 'display:flex;align-items:center;gap:10px;width:100%;'
                + 'padding:8px 12px;border:0;background:transparent;color:inherit;'
                + 'font:inherit;font-size:13px;cursor:pointer;border-radius:6px';
              button.addEventListener('mouseenter', () => { button.style.background = 'rgba(127,127,127,.12)'; });
              button.addEventListener('mouseleave', () => { button.style.background = 'transparent'; });
            }
            button.addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); run(); });

            const iconBox = document.createElement('span');
            if (iconCls) iconBox.className = iconCls;
            const C = muIcons && muIcons[iconName];
            if (C && muRenderer) {
              const h = muRenderer(iconBox);
              h.show(jsx.jsx(C, { size: 16 }));
            }
            const text = document.createElement('span');
            if (labelCls) { text.className = labelCls; } else { text.style.flex = '1'; }
            text.textContent = label;

            button.appendChild(iconBox);
            button.appendChild(text);
            wrap.appendChild(button);
            box.appendChild(wrap);
          };
          addRow('我的账号', 'IconUserOutlineMedium',
            () => muOverlay((close) => jsx.jsx(MuAccountPanel, { close })));
          addRow('退出登录', 'IconRightUpOutlineRegular',
            () => { window.location.href = '/logout'; });
          menu.appendChild(box);
        }
      };
      try {
        new MutationObserver(muMenuSweep).observe(document.body, { childList: true, subtree: true });
        muMenuSweep();
      } catch (e) { /* cosmetic only */ }
      // The instance status belongs in the sidebar's panel list, first: it is the row a
      // user looks for next to 插件 / 自动化任务. That list is rendered by DSH with no
      // slot, so the row is added to its <nav> and styled like the rows around it --
      // including their hover, and hiding the label when the sidebar is collapsed.
      const muPanelRow = () => {
        const nav = document.querySelector('nav[class*="panelList"], nav[class*="panel-list"]');
        if (!nav || nav.querySelector('[data-dsh-mu-instance-row]')) return !!nav;
        const row = document.createElement('button');
        row.type = 'button';
        row.setAttribute('data-dsh-mu-instance-row', '1');
        row.style.cssText = 'display:flex;align-items:center;gap:10px;width:100%;'
          + 'padding:8px 10px;margin:0 0 4px;border:0;border-radius:8px;background:transparent;'
          + 'color:inherit;font:inherit;font-size:13px;cursor:pointer;text-align:left;'
          + 'white-space:nowrap;overflow:hidden';
        row.addEventListener('mouseenter', () => { row.style.background = 'rgba(127,127,127,.12)'; });
        row.addEventListener('mouseleave', () => { row.style.background = 'transparent'; });
        row.addEventListener('click', () => muOverlay((close) => jsx.jsx(MuInstancePanel, { close })));

        const dot = document.createElement('span');
        dot.style.cssText = 'flex:0 0 auto;width:9px;height:9px;border-radius:50%;background:#94a3b8';
        const label = document.createElement('span');
        label.style.cssText = 'overflow:hidden;text-overflow:ellipsis';
        label.textContent = '实例状态';
        row.appendChild(dot);
        row.appendChild(label);

        // Collapsed sidebar: DSH narrows the <nav> to icons, so drop the label and centre
        // the dot instead of letting it overflow.
        const syncWidth = () => {
          const narrow = nav.clientWidth > 0 && nav.clientWidth < 90;
          label.style.display = narrow ? 'none' : '';
          row.style.justifyContent = narrow ? 'center' : 'flex-start';
          row.style.padding = narrow ? '8px 0' : '8px 10px';
        };
        let observer = null;
        try {
          observer = new ResizeObserver(syncWidth);
          observer.observe(nav);
        } catch (e) { /* older browser: check on demand instead */ }
        syncWidth();

        const refresh = () => fetch('/me', { headers: { accept: 'application/json' } })
          .then((r) => r.json()).then((me) => {
            const inst = (me && me.instance) || {};
            const who = (me && me.user) || '';
            const running = !!inst.live;
            dot.style.background = running ? '#22c55e' : '#ef4444';
            label.textContent = who + ' · ' + (running ? '运行中' : '已停止');
            row.title = who + ' · ' + (running ? '运行中' : '已停止')
              + (inst.port ? ' · :' + inst.port : '');
            syncWidth();
          }).catch(() => {});
        refresh();
        setInterval(refresh, 15000);
        nav.insertBefore(row, nav.firstChild);
        return true;
      };
      try {
        const panelTimer = setInterval(() => { if (muPanelRow()) clearInterval(panelTimer); }, 400);
        new MutationObserver(() => muPanelRow())
          .observe(document.body, { childList: true, subtree: true });
      } catch (e) { /* cosmetic only */ }

      // The settings drawer still renders the three account actions this platform has
      // moved into 「更多」. The drawer is being repurposed (a debug/emergency surface with
      // a different shape), so its copies are hidden -- scoped to the dialog and matched
      // by exact label, so nothing outside it is touched.
      const muDrawerSweep = () => {
        for (const dialog of document.querySelectorAll('[role="dialog"]')) {
          for (const el of dialog.querySelectorAll('button, [role="menuitem"], li, a, div')) {
            const text = (el.textContent || '').trim();
            if (text !== '修改密码' && text !== '退出登录' && text !== '重启') continue;
            if (el.querySelector('button, [role="menuitem"]')) continue; // container, keep
            if (el.closest('[data-dsh-mu-overlay]')) continue;            // our own panel
            el.style.display = 'none';
          }
        }
      };
      try {
        new MutationObserver(muDrawerSweep).observe(document.body, { childList: true, subtree: true });
        muDrawerSweep();
      } catch (e) { /* cosmetic only */ }

      ctx.effect(() => ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section',
        id: 'mu-models',
        order: 11,
        label: () => '模型（mu）',
      }, MuModelsSection)), 'dsh-mu: models section');
    }

    return { inject: ['slots'], apply };
  },
});
