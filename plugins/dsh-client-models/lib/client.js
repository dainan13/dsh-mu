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
      // A plan created from the user's own key carries its real list in `effective_models`
      // (the models its key was verified to serve) while `models` stays empty until they
      // pick a subset. Filtering on `models` alone hid exactly those plans, so the
      // dropdown showed nothing but "(keep the platform default)".
      // `/me` returns `plans` as an ARRAY; reading `.plans.plans` always yielded
      // undefined, so the dropdown was empty for everyone -- team plans included.
      const rawPlans = Array.isArray(me.plans) ? me.plans : ((me.plans || {}).plans || []);
      // 套餐没声明子集（models/effective_models 都空）= 该 provider 的**全部模型**，
      // 所以回退到 /me 里 providers[] 的模型列表，否则这些套餐在下拉里会消失。
      const provs = Array.isArray(me.providers) ? me.providers : [];
      const provModels = {};
      provs.forEach((pv) => { provModels[pv.id] = pv.models || []; });
      const mid = (m) => (typeof m === 'string' ? m
        : (m && (m.id || m.name)) ? String(m.id || m.name) : '');
      const modelsOf = (p) => {
        const raw = (p.effective_models && p.effective_models.length) ? p.effective_models
          : (p.models && p.models.length) ? p.models
          : (provModels[p.provider] || []);
        return raw.map(mid).filter(Boolean);
      };
      const plans = rawPlans
        .map((p) => ({ ...p, models: modelsOf(p) }))
        .filter((p) => (p.models || []).length);
      // `/me` names the user's own pick `my_default` (`default` never existed).
      const current = me.my_default || me.default || {};
      const value = current.plan && current.model ? current.plan + '|' + current.model : '';
      const save = async (raw) => {
        // 选"（保持平台默认）"= 清掉我自己的覆盖，必须真的发请求（原来直接 return ✗ 保存不上 ✗）
        const clear = !raw;
        const parts = clear ? ['', ''] : raw.split('|');
        try {
          const res = await fetch('/me', { method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(clear
              ? { action: 'plan-default-clear' }
              : { action: 'plan-default', id: parts[0], plan: parts[0], model: parts[1] }) });
          const doc = await res.json().catch(() => ({}));
          const why = (doc && (doc.detail || doc.error)) || res.status;
          setNote(res.ok ? (clear ? '已恢复平台默认' : '已设为默认模型') : '失败：' + why);
          if (res.ok) {
            // 保存后重新读一次，让下拉反映真实值（否则重开面板才更新）
            fetch('/me', { headers: { accept: 'application/json' } })
              .then((r2) => r2.json()).then(setMe).catch(() => {});
          }
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
      const [showAdd, setShowAdd] = useState(false);

      const load = useCallback(async () => {
        try {
          const d = await api();
          setState({
            loading: false, error: '',
            user: d.user || '',
            providers: d.providers || [],
            plans: d.plans || [],
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

      const hint = { margin: '0 0 6px', fontSize: 12.5, color: 'var(--dsw-alias-label-caption, #8b939d)' };
      const h3 = { fontSize: 13, margin: '18px 0 8px' };
      const plRow = { display: 'flex', alignItems: 'baseline', gap: 10, padding: '7px 10px',
                      border: '1px solid var(--dsw-alias-border-l2, #e6e8eb)',
                      borderRadius: 8, marginBottom: 6, fontSize: 12.5 };
      const keyRow = { display: 'flex', alignItems: 'center', gap: 10, padding: '7px 10px',
                       border: '1px solid var(--dsw-alias-border-l2, #e6e8eb)',
                       borderRadius: 8, marginBottom: 6, fontSize: 12.5 };
      const addBtn = { display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
                       width: '100%', height: 36, border: '1px dashed var(--dsw-alias-border-l2, #e6e8eb)',
                       borderRadius: 8, background: 'transparent', color: 'inherit',
                       cursor: 'pointer', font: '12.5px var(--dsw-font-family, system-ui)' };
      const cap = { color: 'var(--dsw-alias-label-caption, #8b939d)' };
      const provModels = {};
      (state.providers || []).forEach((pv) => { provModels[pv.id] = pv.models || []; });
      const mid = (m) => (typeof m === 'string' ? m
        : (m && (m.id || m.name)) ? String(m.id || m.name) : '');
      const modelsOf = (p) => {
        const raw = (p.effective_models && p.effective_models.length) ? p.effective_models
          : (p.models && p.models.length) ? p.models
          : (provModels[p.provider] || []);
        return raw.map(mid).filter(Boolean);
      };

      return jsx.jsxs('div', { style: { padding: '8px 4px', fontSize: 13, lineHeight: 1.6 }, children: [
        jsx.jsx('h2', { style: { margin: '0 0 4px' }, children: '模型（dsh-mu）' }),
        jsx.jsx('p', { style: { ...hint, marginBottom: 14 },
          children: '平台托管的 provider 只能选、不能改；你自己的 key 保存在平台，加密后不回显。'
            + (state.user ? `（当前账号 ${state.user}）` : '') }),

        jsx.jsx('h3', { style: { ...h3, marginTop: 0 }, children: '我的默认模型' }),
        jsx.jsx('p', { style: hint, children: '选中即生效；平台套餐和我添加的 key 都在一个列表里' }),
        jsx.jsx(MuDefaultModel, {}),

        jsx.jsx('h3', { style: h3, children: '平台提供的套餐' }),
        (() => {
          const shared = (state.plans || []).filter((p) => !p.own);
          if (!shared.length) return jsx.jsx('div', { style: { ...cap, fontSize: 12.5 },
            children: '平台还没有给这个账号分配套餐。' });
          return shared.map((p) => jsx.jsxs('div', { style: plRow, key: p.id, children: [
            jsx.jsx('span', { style: { fontWeight: 500 }, children: p.name || p.id }),
            jsx.jsx('span', { style: { ...cap, fontSize: 12 },
              children: `${p.provider || ''} · ${modelsOf(p).join(' / ') || '未声明模型'}` }),
          ] }));
        })(),

        jsx.jsx('h3', { style: h3, children: '我添加的 key' }),
        (state.plans || []).filter((p) => p.own).length
          ? (state.plans || []).filter((p) => p.own).map((p) => jsx.jsxs('div', { style: keyRow, key: p.id, children: [
              jsx.jsx('span', { style: { fontWeight: 500 }, children: p.name }),
              jsx.jsx('span', { style: { ...cap, fontSize: 12 },
                children: `${p.provider} · ${p.has_key ? '加密保存，不回显' : 'key 缺失'}`
                  + ` · ${(p.effective_models || []).length} 个模型` }),
              jsx.jsxs('span', { style: { marginLeft: 'auto', display: 'flex', gap: 6 }, children: [
                jsx.jsx('button', { type: 'button', disabled: busy, onClick: () => act('probe', p.id),
                                    style: { fontSize: 12 }, children: '检测' }),
                jsx.jsx('button', { type: 'button', disabled: busy, onClick: () => act('delete', p.id),
                                    style: { fontSize: 12 }, children: '删除' }),
              ] }),
            ] }))
          : jsx.jsx('div', { style: { ...cap, fontSize: 12.5 }, children: '还没有。' }),

        msg ? jsx.jsx('p', { style: { margin: '10px 0 0', fontSize: 12.5, opacity: 0.85 }, children: msg }) : null,

        jsx.jsx('div', { style: { marginTop: 14 }, children: showAdd ? null : jsx.jsx('button', {
          type: 'button', style: addBtn, onClick: () => setShowAdd(true),
          children: '＋ 添加模型提供商',
        }) }),

        showAdd ? jsx.jsxs('div', { style: { marginTop: 8, padding: 10,
          border: '1px solid var(--dsw-alias-border-l2, #e6e8eb)', borderRadius: 8 }, children: [
          row('套餐名', input({ value: form.name, placeholder: '我的 groq',
                                onChange: (e) => setForm((f) => ({ ...f, name: e.target.value })) })),
          row('provider', jsx.jsx('select', {
            value: form.provider,
            onChange: (e) => setForm((f) => ({ ...f, provider: e.target.value })),
            style: { flex: 1, padding: '6px 8px', borderRadius: 6, background: 'transparent',
                     color: 'inherit', fontSize: 13 },
            children: state.providers.length
              ? state.providers.map((pv) => jsx.jsx('option', { value: pv.id,
                  children: `${pv.id} · ${pv.label || ''}（${(pv.models || []).length} 模型）` }, pv.id))
              : jsx.jsx('option', { value: '', children: '（管理员还没有导入可托管的 provider）' }),
          })),
          row('API key', input({ type: 'password', value: form.key,
                                  placeholder: '只发送到平台，加密保存，不回显',
                                  autoComplete: 'new-password',
                                  onChange: (e) => setForm((f) => ({ ...f, key: e.target.value })) })),
          jsx.jsxs('div', { style: { marginLeft: 84, display: 'flex', gap: 8 }, children: [
            jsx.jsx('button', { type: 'button', onClick: () => setShowAdd(false),
                                style: { padding: '6px 12px', borderRadius: 8, cursor: 'pointer', fontSize: 12.5 },
                                children: '取消' }),
            jsx.jsx('button', { type: 'button', disabled: busy, onClick: submit,
                                style: { padding: '6px 12px', borderRadius: 8, cursor: 'pointer', fontSize: 12.5,
                                         background: '#3b82f6', borderColor: '#3b82f6', color: '#fff' },
                                children: busy ? '保存中…' : '保存' }),
          ] }),
        ] }) : null,

        jsx.jsx('h3', { style: h3, children: 'Copilot / Codex' }),
        jsx.jsx('div', { style: { ...cap, fontSize: 12.5 },
          children: '这两个没有静态 API key，所以不在这里配置：Copilot 由平台代为换取短时令牌；'
            + 'Codex 目前未提供入口。' }),
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
        const plans = (me && me.plans && me.plans.plans) || [];
        const myPlan = (me && me.my_default && me.my_default.plan) || '';
        const planName = (plans.find((x) => x.id === myPlan) || {}).name || myPlan || '—';
        const stateText = inst.live ? '运行中' : (inst.state || '未运行');
        const instText = inst.name
          ? `${inst.name}${inst.port ? ' · 端口 ' + inst.port : ''} · ${stateText}`
          : '—';
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
        // 结构照 dsh-native.html 的「我的账号」：头部（dot+账号+账号·dsh-mu）
        // + 四项 kv（账号/实例/套餐/上次登录）+ 三个动作（修改密码/关闭/退出登录）
        const kv = (k, v) => jsx.jsxs('div', {
          style: { display: 'grid', gridTemplateColumns: '76px 1fr', gap: '6px 12px',
                   fontSize: 12.5, marginBottom: 6 },
          children: [
            jsx.jsx('span', { style: { color: 'var(--dsw-alias-label-caption, #8b939d)' }, children: k }),
            jsx.jsx('span', { children: v }),
          ],
        });
        return jsx.jsxs('div', {
          style: {
            width: 'min(420px, 92vw)', padding: '18px 18px 14px', borderRadius: 14,
            background: 'var(--dsw-alias-bg-base, #fff)',
            border: '1px solid var(--dsw-alias-border-l2, #e6e8eb)',
            color: 'var(--dsw-alias-label-primary, #1b1b1c)',
            boxShadow: '0 24px 60px rgba(0,0,0,.18)', fontSize: 13, lineHeight: 1.55,
          },
          children: [
          jsx.jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 },
            children: [
              jsx.jsx('span', { style: { width: 8, height: 8, borderRadius: '50%',
                                         background: '#22c55e', flex: '0 0 auto' } }),
              jsx.jsx('span', { style: { fontWeight: 600 }, children: (me && me.user) || '—' }),
              jsx.jsx('span', { style: { fontSize: 11.5,
                color: 'var(--dsw-alias-label-caption, #8b939d)' }, children: '账号 · dsh-mu' }),
            ] }),
          kv('账号', (me && me.user) || '—'),
          kv('实例', instText),
          kv('套餐', planName),
          kv('上次登录', '—'),
          note ? jsx.jsx('div', { style: { fontSize: 12, marginBottom: 6, opacity: 0.85 },
                                  children: note }) : null,
          jsx.jsxs('div', { style: { display: 'flex', gap: 8, marginTop: 12,
                                     borderTop: '1px solid var(--dsw-alias-border-l2, #e6e8eb)',
                                     paddingTop: 12 }, children: [
            muBtn('修改密码', change, true),
            muBtn('关闭', () => props.close()),
            muBtn('退出登录', () => { window.location.href = '/logout'; }),
          ] }),
        ] });
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
          // 认"账号菜单"的条件放宽：它一定有原生的「设置」行，且不止一项。
          // 0.95.x 用「意见反馈」/「Ctrl + Alt + ,」能认出来，换版本这些文案会变 ✗
          const hasSettings = texts.some((x) => x.startsWith('设置'));
          const isAccountMenu = texts.includes('意见反馈')
            || menu.textContent.indexOf('Ctrl + Alt + ,') >= 0
            || (hasSettings && texts.length >= 2);
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
      // 侧栏「实例状态」那一行已由 /mu-static/mu-shell.js 的实例卡取代（设计稿：
      // 会话外壳里只保留我们那张卡）。这里不再注入，避免出现两行状态。

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
        label: () => '模型',
      }, MuModelsSection)), 'dsh-mu: models section');
    }

    return { inject: ['slots'], apply };
  },
});
