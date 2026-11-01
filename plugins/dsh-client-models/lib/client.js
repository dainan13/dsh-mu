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

        jsx.jsx('h3', { style: { fontSize: 13, margin: '20px 0 6px' }, children: 'Copilot / Codex' }),
        jsx.jsx('div', { style: { opacity: 0.7, fontSize: 13 },
          children: '这两个只能用厂商的 OAuth 会话，平台无法代持凭据：请在本页「模型」里直接登录，key 只留在你自己的实例里。' }),
      ] });
    }

    function apply(ctx) {
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
