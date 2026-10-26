/* dsh-mu user panel - injected into every proxied harness page.
 *
 * A chibi character peeks out of the left edge; clicking it slides open a drawer
 * with the account, the instance, a password form and the logout button. It is
 * rendered inside a shadow root so the harness stylesheet cannot touch it and it
 * cannot leak styles into the harness.
 */
(function () {
  "use strict";
  if (document.getElementById("dshmu-panel-host")) return;

  const HOST_ID = "dshmu-panel-host";
  const host = document.createElement("div");
  host.id = HOST_ID;
  // Only the layout-critical properties live inline; `:host { all: initial }` in
  // the shadow stylesheet already blocks inherited harness styles (and keeps this
  // attribute short instead of expanding `all` into hundreds of longhands).
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483000";
  document.body.appendChild(host);

  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
<style>
  :host { all: initial; }
  * { box-sizing: border-box; }
  .wrap { font: 13px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif; color: #e6e8eb; }
  /* ---- peeking character on the left edge ---- */
  .peek {
    position: fixed; left: 0; top: 50%; transform: translate(-24px, -50%);
    width: 35px; height: 76px; padding: 0; border: 0; background: transparent;
    cursor: pointer; pointer-events: auto; transition: transform .22s ease;
  }
  .peek:hover, .peek:focus-visible { transform: translate(0, -50%); outline: none; }
  .peek img { display: block; width: 35px; height: 76px; image-rendering: auto; }
  .peek .tip {
    position: absolute; left: 100%; top: 50%; transform: translate(-6px, -50%);
    white-space: nowrap; padding: 4px 9px; border-radius: 8px; font-size: 12px;
    background: rgba(23,26,33,.94); border: 1px solid #2b3240; color: #c9d1d9;
    opacity: 0; transition: opacity .15s; pointer-events: none;
  }
  .peek:hover .tip { opacity: 1; }
  /* ---- drawer ---- */
  .scrim {
    position: fixed; inset: 0; z-index: 40;
    background: rgba(6,8,12,.52); backdrop-filter: blur(1.5px);
    opacity: 0; pointer-events: none; transition: opacity .18s;
  }
  .scrim.open { opacity: 1; pointer-events: auto; }
  .drawer {
    position: fixed; left: 0; top: 0; bottom: 0; z-index: 50; width: 328px; max-width: 92vw;
    background: #14171d; border-right: 1px solid #262b36; padding: 18px 18px 16px;
    transform: translateX(-102%); transition: transform .22s ease; overflow-y: auto;
    pointer-events: auto;
  }
  .drawer.open { transform: translateX(0); }
  .plan { display: flex; align-items: center; gap: 8px; padding: 6px 0;
          border-bottom: 1px solid #1d222c; }
  .plan:last-child { border-bottom: 0; }
  .plan .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mu-greenfield { display: block; margin: 8px 0; font-size: 12px; color: #aeb6c2; }
  .mu-greenfield select { display: block; width: 100%; margin-top: 4px; }
  .plan .tag { font-size: 11px; color: #8b93a1; border: 1px solid #2b3240; border-radius: 999px;
               padding: 1px 6px; flex: 0 0 auto; }
  .plan button { padding: 2px 6px; font-size: 11px; }
  h2 { margin: 0 0 2px; font-size: 16px; font-weight: 600; }
  .sub { color: #8b93a1; font-size: 12px; margin: 0 0 14px; }
  .row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .avatar { width: 44px; height: 44px; border-radius: 12px; background: #1b1f27; overflow: hidden; flex: 0 0 auto;
            border: 1px solid #262b36; display: flex; align-items: center; justify-content: center; }
  .avatar img { width: 150%; height: auto; object-fit: cover; object-position: 50% 22%; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
           border: 1px solid #2b3240; color: #aeb6c2; }
  .badge.admin { color: #ffd77a; border-color: #6b5312; background: rgba(210,153,34,.12); }
  .card { background: #171a21; border: 1px solid #262b36; border-radius: 12px; padding: 12px 13px; margin-bottom: 12px; }
  .card h3 { margin: 0 0 9px; font-size: 12px; letter-spacing: .04em; text-transform: uppercase; color: #8b93a1; font-weight: 600; }
  .kv { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0; font-size: 12.5px; }
  .kv .k { color: #8b93a1; }
  .kv .v { text-align: right; word-break: break-all; }
  .ok { color: #7ee787; } .bad { color: #ff9c9c; } .warn { color: #ffd77a; }
  label { display: block; font-size: 12px; color: #aeb6c2; margin: 4px 0 5px; }
  input[type=password] {
    width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid #303643;
    background: #0f1218; color: #e6e8eb; font-size: 13px;
  }
  input[type=password]:focus { outline: none; border-color: #4d7cfe; }
  button.btn {
    display: block; width: 100%; margin-top: 8px; padding: 9px; border: 0; border-radius: 9px;
    background: #20262f; color: #e6e8eb; font-size: 13px; cursor: pointer; border: 1px solid #2b3240;
  }
  button.btn:hover { border-color: #4d7cfe; }
  button.primary { background: #4d7cfe; border-color: #4d7cfe; color: #fff; font-weight: 600; }
  button.primary:hover { background: #3f6ce8; }
  button.danger { color: #ff9c9c; }
  button.danger:hover { border-color: #6e2b28; }
  .msg { margin-top: 8px; font-size: 12px; min-height: 16px; }
  .msg.err { color: #ff9c9c; } .msg.good { color: #7ee787; }
  .foot { margin-top: 14px; color: #6b7280; font-size: 11px; text-align: center; }
  .note { margin: 10px 0 0; color: #8b93a3; font-size: 11.5px; line-height: 1.6; }
  .close { position: absolute; right: 12px; top: 12px; width: 26px; height: 26px; border-radius: 8px;
           border: 1px solid #2b3240; background: #1b1f27; color: #aeb6c2; cursor: pointer; font-size: 14px; }

  .who { padding: 2px 0 12px; margin-bottom: 14px; border-bottom: 1px solid #232936; }
  .who-acts { display: flex; gap: 8px; margin-top: 10px; }
  .who-name { font-size: 15px; font-weight: 600; color: #e6e9ef;
              overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .who-meta { margin-top: 4px; }

  .card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .card-head h3 { margin: 0; }

  .btn.sm { padding: 5px 10px; font-size: 12px; border-radius: 8px; }
  .btn.ghost { background: transparent; border-color: #303747; color: #9fb0c7; }
  .btn.ghost:hover { border-color: #4b5568; color: #cfd8e6; }
  .btn.danger-ghost { background: transparent; border-color: #4a2b31; color: #e08b98; }
  .btn.danger { border-color: #7a3b45; background: #4a222a; color: #ffdde2; }
  .acts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }

  .plan { display: flex; align-items: flex-start; gap: 8px; padding: 9px 0;
          border-top: 1px solid #232936; }
  .plan:first-child { border-top: 0; }
  .plan-main { flex: 1 1 auto; min-width: 0; }
  .plan-name { font-size: 13px; color: #dfe5ee; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .plan-sub { margin-top: 2px; font-size: 11.5px; color: #8593a7; overflow: hidden;
              text-overflow: ellipsis; white-space: nowrap; }
  .plan-side { flex: 0 0 auto; display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; }
  .plan-side .tag, .plan-side .link { flex: 0 0 auto; }
  .tag.mine { color: #8fb8ff; border-color: #2f3f5c; }
  .tag.def { color: #0f1218; background: #8fb8ff; border-color: #8fb8ff; font-weight: 600; }
  /* The restart hint keeps its slot whether or not it is showing: a banner that
     appears and pushes the buttons down is what makes the card feel jumpy. */
  .pending { display: flex; align-items: center; justify-content: space-between; gap: 8px;
             margin-top: 10px; padding: 8px 10px; border-radius: 8px;
             background: #2a2416; border: 1px solid #4a3d22; color: #e3b341; font-size: 12px;
             visibility: hidden; }
  .pending.show { visibility: visible; }
  .tag.warn { color: #e3b341; border-color: #4a3d22; }
  .link { background: none; border: 0; padding: 0 2px; color: #8fb8ff; cursor: pointer;
          font-size: 12px; }
  .link:hover { text-decoration: underline; }
  .link.danger { color: #e08b98; }

  .mu-modal { position: fixed; inset: 0; z-index: 2147483000; display: flex;
              align-items: center; justify-content: center; padding: 16px;
              background: rgba(8, 10, 14, .62); }
  .mu-modal[hidden] { display: none; }
  .mu-sheet { width: min(380px, 100%); max-height: 88vh; overflow: auto;
              background: #141821; border: 1px solid #2b3240; border-radius: 14px;
              padding: 16px; box-shadow: 0 24px 60px rgba(0, 0, 0, .5); }
  .mu-sheet h3 { margin: 0 0 10px; font-size: 14px; }
  .mu-body label { display: block; margin: 10px 0 4px; font-size: 12px; color: #9aa7b8; }
  .mu-body input, .mu-body select { width: 100%; box-sizing: border-box; padding: 8px 10px;
              border-radius: 8px; border: 1px solid #2b3240; background: #0f131a;
              color: #e6e9ef; font-size: 13px; }
  .mu-hint { margin: 0 0 6px; font-size: 12px; color: #8593a7; line-height: 1.5; }
  .mu-acts { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
</style>
<div class="wrap">
  <div class="scrim" id="scrim"></div>
  <button class="peek" id="peek" title="账户 / 实例" aria-label="账户与实例面板">
    <img src="/mu-static/icon.png" alt="">
    <span class="tip">账户 / 实例</span>
  </button>
  <aside class="drawer" id="drawer" role="dialog" aria-label="账户与实例">
    <button class="close" id="close" aria-label="关闭">×</button>

    <header class="who">
      <div class="who-name" id="uname">…</div>
      <div class="who-meta" id="urole">加载中…</div>
      <div class="who-acts">
        <button class="btn sm ghost" id="pwOpen">修改密码</button>
        <button class="btn sm danger-ghost" id="logout">退出登录</button>
      </div>
    </header>

    <div class="card">
      <h3>我的实例</h3>
      <div id="inst"></div>
      <div class="pending" id="pending">
        <span>有改动要重启才生效</span>
        <button class="btn sm" id="pendingRestart">立即重启</button>
      </div>
      <div class="acts">
        <button class="btn sm" id="startInst" style="display:none">启动</button>
        <button class="btn sm" id="restartInst">重启</button>
      </div>
    </div>

    <div class="card" id="planCard">
      <div class="card-head">
        <h3>我的套餐</h3>
        <button class="btn sm ghost" id="planAddOpen" title="添加一个自己的套餐">＋ 添加</button>
      </div>
      <div id="plans">加载中…</div>
      <div class="msg" id="plMsg"></div>
    </div>

    <div class="card" id="myDefaultCard">
      <div class="card-head">
        <h3>我的默认模型</h3>
        <button class="btn sm" id="myDefEdit">设置</button>
      </div>
      <div id="myDefState" class="plan-sub">加载中…</div>
      <div class="mu-hint" style="margin:6px 0 0">
        新会话从这个模型开始，优先于管理员设置的默认；只能设一条，改完要重启实例才生效。
      </div>
      <div class="msg" id="myDefMsg"></div>
    </div>

    <div class="foot">dsh-mu 多实例网关</div>
  </aside>

</div>`;

  const $ = (id) => root.getElementById(id);
  const dq = (id) => document.getElementById(id);

  // A dialog that lives in document.body with inline styles: the drawer's scrim
  // (backdrop-filter) and the harness shell cannot swallow its clicks, and shadow
  // scoping cannot drop its rules.
  const MODAL_CSS = `
    #dsh-mu-modal .mu-sheet { width: min(380px, 100%); max-height: 88vh; overflow: auto;
      background: #141821; border: 1px solid #2b3240; border-radius: 14px; padding: 16px;
      box-shadow: 0 24px 60px rgba(0,0,0,.55); color: #e6e9ef;
      font: 13px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif; }
    #dsh-mu-modal h3 { margin: 0 0 10px; font-size: 14px; }
    #dsh-mu-modal label { display: block; margin: 10px 0 4px; font-size: 12px; color: #9aa7b8; }
    #dsh-mu-modal input, #dsh-mu-modal select { width: 100%; box-sizing: border-box;
      padding: 8px 10px; border-radius: 8px; border: 1px solid #2b3240; background: #0f131a;
      color: #e6e9ef; font-size: 13px; }
    #dsh-mu-modal .mu-hint { margin: 0 0 6px; font-size: 12px; color: #8593a7; line-height: 1.5; }
    #dsh-mu-modal .mu-acts { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
    #dsh-mu-modal .mu-btn { padding: 6px 12px; border-radius: 8px; cursor: pointer;
      border: 1px solid #2b3240; background: #1b1f27; color: #cfd8e6; font-size: 13px; }
    #dsh-mu-modal .mu-btn.primary { border-color: #2f6fed; background: #2f6fed; color: #fff; }
    #dsh-mu-modal .mu-btn.danger { border-color: #7a3b45; background: #4a222a; color: #ffdde2; }
    #dsh-mu-modal .mu-btn[disabled] { opacity: .6; cursor: default; }
    #dsh-mu-modal .msg { margin-top: 8px; font-size: 12px; color: #9aa7b8; }
    #dsh-mu-modal .msg.err { color: #ff8b9a; }
    #dsh-mu-modal .msg.good { color: #6fce8f; }`;

  function ensureModal() {
    let el = dq("dsh-mu-modal");
    if (el) return el;
    const style = document.createElement("style");
    style.id = "dsh-mu-modal-style";
    style.textContent = MODAL_CSS;
    document.head.appendChild(style);
    el = document.createElement("div");
    el.id = "dsh-mu-modal";
    Object.assign(el.style, {
      position: "fixed", inset: "0", zIndex: "2147483647", display: "none",
      alignItems: "center", justifyContent: "center", padding: "16px",
      background: "rgba(8,10,14,.62)",
    });
    el.innerHTML = `<div class="mu-sheet" role="dialog" aria-modal="true" aria-labelledby="mTitle">
        <h3 id="mTitle"></h3>
        <div class="mu-body" id="mBody"></div>
        <div class="mu-acts">
          <button class="mu-btn" id="mCancel" type="button">取消</button>
          <button class="mu-btn primary" id="mOk" type="button">确定</button>
        </div>
      </div>`;
    el.addEventListener("click", (ev) => { if (ev.target === el) closeModal(); });
    document.body.appendChild(el);
    return el;
  }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let state = { open: false, data: null, pendingRestart: false };

  function markPending(on) {
    state.pendingRestart = !!on;
    const box = $("pending");
    if (box) box.classList.toggle("show", !!on);
  }

  let usageTimer = null;

  function setOpen(open) {
    state.open = open;
    $("drawer").classList.toggle("open", open);
    $("scrim").classList.toggle("open", open);
    // Poll usage only while the drawer is actually open: an idle tab must not cost
    // the backend 12 requests a minute.
    if (usageTimer) { clearInterval(usageTimer); usageTimer = null; }
    if (open) {
      refresh();
      usageTimer = setInterval(() => { if (state.open) refresh(); }, 5000);
    }
  }

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ credentials: "same-origin" }, options || {}));
    let body = {};
    try { body = await res.json(); } catch (e) { body = {}; }
    if (!res.ok) throw Object.assign(new Error(body.error || res.statusText), { body: body, status: res.status });
    return body;
  }

  const listOf = (v) => (Array.isArray(v) ? v : []);

  const wireMyDefault = () => {
    const edit = $("myDefEdit");
    if (edit && !edit.dataset.wired) {
      edit.dataset.wired = "1";
      edit.addEventListener("click", () => openMyDefault());
    }
  };

  function planRow(p) {
    const acts = p.own
      ? `<button class="link" data-act="probe" data-id="${esc(p.id)}">检测</button>` +
        `<button class="link" data-act="models" data-id="${esc(p.id)}">模型</button>` +
        `<button class="link" data-act="rename" data-id="${esc(p.id)}">改名</button>` +
        `<button class="link danger" data-act="delete" data-id="${esc(p.id)}">删除</button>`
      : "";
    const badges = (p.is_default ? '<span class="tag def" title="默认套餐">默认</span>' : "") +
      (p.team ? '<span class="tag">团队</span>' : '<span class="tag mine">我的</span>') +
      (p.has_key ? "" : '<span class="tag warn" title="这个套餐还没有可用的 key">缺 key</span>');
    const models = listOf(p.models);
    const avail = listOf(p.available_models);
    const eff = listOf(p.effective_models).length ? listOf(p.effective_models) : models;
    const probed = Array.isArray(p.available_models);
    const unusable = probed ? models.filter((m) => !avail.includes(m)) : [];
    let modelText = eff.length ? eff.length + " 个模型" : "全部模型";
    if (probed && unusable.length) modelText += "（" + unusable.length + " 个该 key 不可用）";
    const sub = [p.provider, modelText].filter(Boolean).join(" · ");
    return `<div class="plan">
      <div class="plan-main">
        <div class="plan-name" title="${esc(p.name)}">${esc(p.name)}</div>
        <div class="plan-sub" title="${esc(sub)}">${esc(sub)}</div>
      </div>
      <div class="plan-side">${badges}${acts}</div>
    </div>`;
  }

  // The user's own default: one entry, outranks whatever the operator configured.
  // The card only *shows* the current state; editing happens in a dialog, like every
  // other create/modify action in this panel.
  function renderMyDefault(data) {
    const box = $("myDefState");
    const btn = $("myDefEdit");
    if (!box || !btn) return;
    const mine = data.my_default || {};
    const plans = listOf(data.plans);
    const plan = plans.find((p) => p.id === mine.plan);
    if (mine.plan) {
      box.innerHTML = `已设置：<b>${esc(plan ? plan.name : mine.plan)}</b>`
        + (mine.model ? ` · <code>${esc(mine.model)}</code>` : " · 套餐默认模型")
        + ' <button class="link danger" id="myDefClear">清除</button>';
      btn.textContent = "修改";
      const clear = box.querySelector("#myDefClear");
      if (clear) clear.addEventListener("click", () => clearMyDefault());
    } else {
      const fallback = plans.find((p) => p.is_default) || plans[0];
      box.innerHTML = `未设置${fallback ? `（新会话按平台默认：${esc(fallback.name)}）` : ""}`;
      btn.textContent = "设置";
    }
    state.myDefault = mine;
    state.myDefaultPlans = plans.filter((p) => p.enabled !== false);
  }

  function openMyDefault() {
    const plans = state.myDefaultPlans || [];
    const mine = state.myDefault || {};
    if (!plans.length) {
      formMsg("myDefMsg", "没有可用的套餐", "err");
      return;
    }
    const planOf = (pid) => plans.find((p) => p.id === pid) || plans[0];
    const modelsOf = (pid) => {
      const plan = planOf(pid);
      const eff = listOf(plan.effective_models).length ? listOf(plan.effective_models) : listOf(plan.models);
      return eff;
    };
    const first = mine.plan || (plans.find((p) => p.is_default) || plans[0]).id;
    const opts = (list, selected) => list
      .map((v) => `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc(v)}</option>`)
      .join("");
    const modelOptions = (pid, selected) =>
      ['<option value="">（套餐默认）</option>']
        .concat(modelsOf(pid).map((m) =>
          `<option value="${esc(m)}" ${m === selected ? "selected" : ""}>${esc(m)}</option>`)).join("");

    openModal({
      title: mine.plan ? "修改我的默认模型" : "设置我的默认模型",
      ok: "保存",
      cancel: "取消",
      body: `<p class="mu-hint">新会话从这个模型开始，优先于管理员设置的默认；只能设一条。改完要重启实例才生效。</p>
        <label class="mu-greenfield">套餐
          <select id="mdPlan">${plans.map((p) =>
            `<option value="${esc(p.id)}" ${p.id === first ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
        <label class="mu-greenfield">模型
          <select id="mdModel">${modelOptions(first, mine.model || "")}</select></label>
        <div class="msg" id="mdMsg"></div>`,
      onOk: async () => {
        const planSel = dq("mdPlan");
        const modelSel = dq("mdModel");
        try {
          await api("/me", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "plan-default", id: planSel.value,
                                   model: modelSel.value }),
          });
        } catch (e) {
          formMsg("myDefMsg", "保存失败：" + ((e.body && e.body.detail) || e.message), "err");
          return false;
        }
        formMsg("myDefMsg", "已保存 ✓ 点上面「我的实例」里的「重启」让它生效（约十几秒到一分钟）", "good");
        refresh();
        return false;
      },
    });
    const planSel = dq("mdPlan");
    if (planSel) {
      planSel.addEventListener("change", () => {
        dq("mdModel").innerHTML = modelOptions(planSel.value, "");
      });
    }
  }

  async function clearMyDefault() {
    try {
      await api("/me", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "plan-default-clear" }),
      });
      formMsg("myDefMsg", "已清除：新会话回到平台默认（重启实例后生效）", "good");
      refresh();
    } catch (e) {
      formMsg("myDefMsg", "清除失败：" + ((e.body && e.body.detail) || e.message), "err");
    }
  }

  function renderPlans(data) {
    const plans = data.plans || [];
    const box = $("plans");
    box.innerHTML = plans.length
      ? plans.map(planRow).join("")
      : '<div class="msg">还没有可见的套餐；下面可以自己添加一个。</div>';
    box.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => planAction(btn.dataset.act, btn.dataset.id));
    });
  }

  async function planAction(act, id) {
    const plan = ((state.data && state.data.plans) || []).find((p) => p.id === id) || { name: id };
    if (act === "delete") {
      openModal({
        title: "删除套餐",
        danger: true,
        ok: "删除",
        body: `<p class="mu-hint">「${esc(plan.name)}」会从模型选择器里消失（重启实例后生效）。</p>`,
        onOk: async () => {
          await postPlan("plan-delete", { id }, "已删除；重启实例后生效。");
          return false;
        },
      });
      return;
    }
    if (act === "models") {
      const provider = ((state.data && state.data.providers) || [])
        .find((x) => x.id === (plan.provider || "")) || { models: [] };
      const catalog = (provider.models || []).map((m) => (typeof m === "string" ? { id: m } : m));
      const avail = Array.isArray(plan.available_models) ? plan.available_models : null;
      const selModels = Array.isArray(plan.models) ? plan.models : [];
      const chosen = new Set(selModels.length ? selModels : (avail || catalog.map((m) => m.id)));
      const probe = plan.last_probe || {};
      const head = `目录 ${catalog.length} · 已选 ${chosen.size} · ` +
        (avail ? `可用 ${avail.length}` : "未检测") +
        ` · 上次检测 ${probe.at ? new Date(probe.at * 1000).toLocaleString() : "从未"}`;
      const rows = catalog.map((m) => {
        const usable = !avail || avail.includes(m.id);
        const checked = chosen.has(m.id);
        const state = usable ? (checked ? "已选" : "未选") : "该 key 不可用";
        const eff = m.reasoningEfforts;
        const effortText = eff === false ? "非推理"
          : (eff && typeof eff === "object") ? Object.keys(eff).join(" / ") : "-";
        return `<tr style="${usable ? "" : "opacity:.55"}">
          <td><input type="checkbox" value="${esc(m.id)}" ${checked ? "checked" : ""} ${usable ? "" : "disabled"}></td>
          <td><code>${esc(m.id)}</code></td>
          <td class="mu-hint">${esc(m.name || "-")}</td>
          <td class="mu-hint">${esc(effortText)}</td>
          <td>${usable ? "✅" : "⛔"} ${state}</td></tr>`;
      }).join("");
      openModal({
        title: `「${esc(plan.name || id)}」的模型`,
        ok: "保存",
        body: `<p class="mu-hint">${head}。不带勾的模型不会出现在模型选择器里${avail ? "；不可用的模型无法勾选。" : "。"}</p>
          <div id="plModels" style="max-height:46vh;overflow:auto">
            <table><thead><tr><th></th><th>模型 id</th><th>名称</th><th>推理等级</th><th>状态</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="5" class="mu-hint">这个 provider 目录里没有模型</td></tr>'}</tbody></table>
          </div>
          <div class="msg" id="plMsg"></div>`,
        onOk: async () => {
          const picked = [...document.querySelectorAll("#plModels input[type=checkbox]")]
            .filter((cb) => cb.checked).map((cb) => cb.value);
          await postPlan("plan-models", { id, models: picked }, "已保存；重启实例后生效。");
          return false;
        },
      });
      return;
    }
    if (act === "probe") {
      try {
        const r = await api("/me", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "plan-probe", id }) });
        const unknown = (r.unknown || []).length;
        let msg;
        if (r.first) {
          msg = `首次检测：可用 ${r.available.length} 个` + (unknown ? `；上游有 ${unknown} 个不在目录里` : "");
        } else {
          const parts = [];
          if ((r.added || []).length) parts.push(`新增 ${r.added.length}`);
          if ((r.lost || []).length) parts.push(`失去 ${r.lost.length}`);
          msg = `可用 ${r.available.length} 个（上次 ${r.before}）：` + (parts.join(" · ") || "没有变化");
        }
        openModal({
          title: "检测结果",
          ok: "知道了",
          body: `<p>${esc(msg)}</p><p class="mu-hint">可用性变化要重启实例后才生效。</p>`,
          onOk: () => { refresh(); return false; },
        });
      } catch (e) {
        openModal({
          title: "检测失败",
          danger: true,
          ok: "关闭",
          body: `<p>${esc((e.body && e.body.error) || e.message)}</p>`,
          onOk: () => false,
        });
      }
      return;
    }
    if (act === "rename") {
      openModal({
        title: "重命名套餐",
        ok: "保存",
        body: `<label for="plRename">新名字</label>
          <input id="plRename" maxlength="40" value="${esc(plan.name || "")}">
          <div class="msg" id="plMsg"></div>`,
        onOk: async () => {
          const name = dq("plRename").value.trim();
          if (!name) { formMsg("plMsg", "名字不能为空", "err"); return true; }
          await postPlan("plan-rename", { id, name }, "已改名；重启实例后生效。");
          return false;
        },
      });
    }
  }

  async function postPlan(action, body, okText) {
    try {
      await api("/me", { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify(Object.assign({ action }, body)) });
      formMsg("plMsg", okText, "good");
      markPending(true);
      refresh();
    } catch (e) {
      formMsg("plMsg", (e.body && e.body.detail) || e.message, "err");
      throw e;
    }
  }

  function pluginRow(p) {
    const badge = p.source === "curated"
      ? '<span class="tag">清单内</span>'
      : '<span class="tag mine">自装</span>';
    const sub = [p.version, p.by ? "由 " + p.by : ""].filter(Boolean).join(" · ");
    return `<div class="plan">
      <div class="plan-main">
        <div class="plan-name" title="${esc(p.pkg)}">${esc(p.pkg)}</div>
        <div class="plan-sub" title="${esc(sub)}">${esc(sub)}</div>
      </div>
      <div class="plan-side">${badge}
        <button class="link danger" data-plug="remove" data-pkg="${esc(p.pkg)}">卸载</button>
      </div>
    </div>`;
  }

  function render(data) {
    state.data = data;
    // Each card renders independently: when one throws (a field of an unexpected
    // shape), the operator still gets the rest of the drawer instead of an error page.
    const safe = (label, fn) => {
      try {
        fn();
      } catch (e) {
        flash(`${label} 渲染失败：${e.message}`, "err");
      }
    };
    safe("套餐", () => renderPlans(data));
    safe("我的默认模型", () => { renderMyDefault(data); wireMyDefault(); });
    $("uname").textContent = data.user;
    $("uname").title = data.user;
    $("urole").innerHTML = (data.admin
      ? '<span class="badge admin">系统管理员</span>'
      : '<span class="badge">普通用户</span>')
      + (data.system_account ? ' <span class="badge">系统账号</span>' : '');

    const i = data.instance;
    if (!i) {
      $("inst").innerHTML = '<div class="kv"><span class="k">实例</span><span class="v">未分配</span></div>';
      $("startInst").style.display = "none";
    } else {
      const live = i.live;
      const res = i.resources || {};
      const mb = (bytes) => (bytes == null ? "—" : (bytes / 1048576).toFixed(0) + " MB");
      const memText = res.mem_used == null
        ? "—"
        : `${mb(res.mem_used)} / ${res.mem_max ? mb(res.mem_max) : "不限"}`
          + (res.mem_pct != null ? `（${res.mem_pct}%）` : "");
      const cpuText = res.cpu_pct == null ? "—" : `${res.cpu_pct}%`;
      const pidsText = res.pids == null
        ? "—"
        : `${res.pids}${res.pids_max ? " / " + res.pids_max : ""}`;
      const startedAt = Number(i.started_at || 0);
      const fmtAgo = (ts) => {
        const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
        if (secs < 90) return `${secs} 秒前`;
        if (secs < 5400) return `${Math.round(secs / 60)} 分钟前`;
        if (secs < 172800) return `${(secs / 3600).toFixed(1)} 小时前`;
        return `${Math.round(secs / 86400)} 天前`;
      };
      const startedText = startedAt
        ? `${new Date(startedAt * 1000).toLocaleString()}（${fmtAgo(startedAt)}）`
        : "—";
      $("inst").innerHTML = [
        ["名称", esc(i.name)],
        ["端口", esc(i.port)],
        ["状态", `<span class="${live ? "ok" : "bad"}">${live ? "运行中" : esc(i.state)}</span>`],
        ["启动", startedText],
        ["模型", esc(i.provider) + (i.model ? " / " + esc(i.model) : "")],
        ["内存", memText],
        ["CPU", cpuText],
        ["进程", pidsText],
      ].map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("");
      $("startInst").style.display = live ? "none" : "block";
      $("restartInst").style.display = "block";
    }

    // system accounts change their password on the host, not here
    $("pwOpen").style.display = data.system_account ? "none" : "inline-block";
    if (data.system_account) {
      $("urole").innerHTML += '<span class="badge" title="口令用服务器上的 passwd 修改">系统账号口令</span>';
    }
  }

  async function refresh() {
    try {
      const data = await api("/me");
      render(data);
      $("urole").dataset.loaded = "1";
    } catch (e) {
      $("urole").textContent = e.status === 401 ? "会话已过期，请重新登录" : ("加载失败：" + e.message);
    }
  }

  $("peek").addEventListener("click", () => setOpen(!state.open));
  $("close").addEventListener("click", () => setOpen(false));
  $("scrim").addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const el = dq("dsh-mu-modal");
    if (el && el.style.display !== "none") { closeModal(); return; }
    if (state.open) setOpen(false);
  });

  // ---- one shared modal: every drawer action that needs input uses it --------
  let modalOnOk = null;

  function openModal(opts) {
    const el = ensureModal();
    dq("mTitle").textContent = opts.title || "";
    dq("mBody").innerHTML = opts.body || "";
    const okBtn = dq("mOk");
    okBtn.textContent = opts.ok || "确定";
    okBtn.className = "mu-btn primary" + (opts.danger ? " danger" : "");
    dq("mCancel").textContent = opts.cancel || "取消";
    modalOnOk = opts.onOk || null;
    el.style.display = "flex";
    wireModal();
    const first = dq("mBody").querySelector("input, select, textarea");
    if (first) setTimeout(() => first.focus(), 30);
  }

  function closeModal() {
    const el = dq("dsh-mu-modal");
    if (el) el.style.display = "none";
    modalOnOk = null;
  }

  async function runModalOk() {
    if (!modalOnOk) { return closeModal(); }
    const btn = dq("mOk");
    btn.disabled = true;
    try {
      const keepOpen = await modalOnOk();
      if (!keepOpen) closeModal();
    } finally {
      btn.disabled = false;
    }
  }

  function wireModal() {
    const el = ensureModal();
    if (el.dataset.wired) return;
    el.dataset.wired = "1";
    dq("mCancel").addEventListener("click", closeModal);
    dq("mOk").addEventListener("click", runModalOk);
    dq("mBody").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && ev.target.tagName === "INPUT") {
        ev.preventDefault();
        runModalOk();
      }
    });
  }

  function formMsg(id, text, kind) {
    // drawer cards live in the shadow root, modal bodies in the document
    const el = dq(id) || $(id);
    if (!el) return;
    el.className = "msg " + (kind || "");
    el.textContent = text || "";
  }

  // ---- instance actions ------------------------------------------------------
  function setBusy(btn, busyText) {
    btn.dataset.label = btn.dataset.label || btn.textContent;
    btn.disabled = !!busyText;
    btn.textContent = busyText || btn.dataset.label;
  }

  $("startInst").addEventListener("click", async () => {
    setBusy($("startInst"), "正在启动…");
    try {
      await api("/me", { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify({ action: "start-instance" }) });
      setBusy($("startInst"), "已提交");
      setTimeout(refresh, 5000);
    } catch (e) {
      setBusy($("startInst"), "");
      formMsg("plMsg", "启动失败：" + e.message, "err");
    }
  });

  function confirmRestart() {
    openModal({
      title: "重启实例",
      body: '<p class="mu-hint">重启会中断正在进行的对话，通常需要十几秒到一分钟。</p>',
      ok: "重启",
      onOk: async () => {
        markPending(false);
        restartWithProgress($("restartInst"));
        return false;
      },
    });
  }

  $("pendingRestart").addEventListener("click", confirmRestart);

  // Restart is slow (tens of seconds). Show progress instead of going quiet: poll /me
  // until the harness answers again, and report the elapsed time.
  async function restartWithProgress(button) {
    setBusy(button, "重启中… 0s");
    formMsg("plMsg", "正在重启，可以继续用这个抽屉；进度会显示在重启按钮上。", "good");
    const began = Date.now();
    try {
      await api("/me", { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify({ action: "restart" }) });
    } catch (e) {
      setBusy(button, "");
      formMsg("plMsg", "重启失败：" + ((e.body && e.body.detail) || e.message), "err");
      return;
    }
    let tick = 0;
    const timer = setInterval(() => {
      tick += 1;
      const secs = Math.round((Date.now() - began) / 1000);
      setBusy(button, `重启中… ${secs}s`);
    }, 1000);
    const deadline = began + 180000;
    const poll = async () => {
      let live = false;
      try {
        const data = await api("/me");
        live = Boolean(data.instance && data.instance.live);
        if (live) state.data = data;
      } catch (e) { /* the gateway may briefly refuse while it restarts */ }
      if (live) {
        clearInterval(timer);
        const secs = Math.round((Date.now() - began) / 1000);
        setBusy(button, "");
        formMsg("plMsg", `已重启完成（${secs} 秒）`, "good");
        refresh();
        return;
      }
      if (Date.now() > deadline) {
        clearInterval(timer);
        setBusy(button, "");
        formMsg("plMsg", "等了 3 分钟还没起来，看一下实例日志（dsh-muctl instance logs）", "err");
        refresh();
        return;
      }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 2000);
  }

  $("restartInst").addEventListener("click", () => {
    openModal({
      title: "重启实例",
      body: '<p class="mu-hint">重启会中断正在进行的对话，通常需要十几秒到一分钟。'
        + '点「重启」后按钮上会显示进度，实例恢复后会有提示。</p>',
      ok: "重启",
      onOk: async () => {
        restartWithProgress($("restartInst"));
        return false;
      },
    });
  });

  // ---- change password: a modal, never a form lying in the drawer ------------
  $("pwOpen").addEventListener("click", () => {
    openModal({
      title: "修改密码",
      ok: "保存并重新登录",
      body: `<p class="mu-hint">改完后所有会话都会失效，需要重新登录。</p>
        <label for="pw1">新密码（至少 8 位）</label>
        <input type="password" id="pw1" autocomplete="new-password">
        <label for="pw2">再输一次</label>
        <input type="password" id="pw2" autocomplete="new-password">
        <div class="msg" id="pwMsg"></div>`,
      onOk: async () => {
        const a = dq("pw1").value, b = dq("pw2").value;
        formMsg("pwMsg", "");
        if (a.length < 8) { formMsg("pwMsg", "密码至少 8 位", "err"); return true; }
        if (a !== b) { formMsg("pwMsg", "两次输入不一致", "err"); return true; }
        try {
          await api("/me", { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify({ action: "password", password: a }) });
          formMsg("pwMsg", "密码已修改，正在跳转登录…", "good");
          setTimeout(() => { location.href = "/logout"; }, 1200);
          return true;
        } catch (e) {
          formMsg("pwMsg", e.message + (e.body && e.body.detail ? "：" + e.body.detail : ""), "err");
          return true;
        }
      },
    });
  });

  // ---- add plan: the same modal, with the provider list filled on open -------
  $("planAddOpen").addEventListener("click", () => {
    const providers = (state.data && state.data.providers) || [];
    if (!providers.length) {
      formMsg("plMsg", "管理员还没有导入任何 provider，暂时无法添加套餐。", "err");
      return;
    }
    const opts = providers.map((p) =>
      `<option value="${esc(p.id)}">${esc(p.label || p.id)}</option>`).join("");
    openModal({
      title: "添加我的套餐",
      ok: "添加",
      body: `<p class="mu-hint">套餐会以你起的名字出现在模型选择器的分组里。</p>
        <label for="plName">名字</label>
        <input id="plName" placeholder="我的 opencode go" maxlength="40">
        <label for="plProv">provider</label>
        <select id="plProv">${opts}</select>
        <label for="plKey">key</label>
        <input id="plKey" type="password" autocomplete="off" placeholder="只保存到这个实例的凭据库">
        <div class="msg" id="plMsg"></div>`,
      onOk: async () => {
        const name = dq("plName").value.trim();
        const provider = dq("plProv").value;
        const key = dq("plKey").value.trim();
        formMsg("plMsg", "");
        if (!name || !provider || !key) { formMsg("plMsg", "名字、provider、key 都要填。", "err"); return true; }
        try {
          await api("/me", { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify({ action: "plan-add", name, provider, key }) });
          formMsg("plMsg", "已添加「" + name + "」；重启实例后出现在模型选择器里。", "good");
          markPending(true);
          refresh();
          return false;
        } catch (e) {
          formMsg("plMsg", (e.body && e.body.detail) || e.message, "err");
          return true;
        }
      },
    });
  });

  $("logout").addEventListener("click", () => { location.href = "/logout"; });

  // Prime the panel once on load so opening it is instant.
  refresh();
  setInterval(() => { if (!state.open) refresh(); }, 30000);
})();
