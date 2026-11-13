const $ = (s, el = document) => el.querySelector(s);
const money = c => '$' + (c / 100).toFixed(2);
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
const store = {
  get: (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set: (k, v) => localStorage.setItem(k, JSON.stringify(v)),
};

const S = { config: null, menu: null, cart: store.get('cart', []), token: store.get('token', null), me: null, cat: 'drink' };
let pollTimer = null;

async function api(path, body) {
  const opts = { headers: {} };
  if (S.token) opts.headers.Authorization = 'Bearer ' + S.token;
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch('/api' + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || 'Something went wrong.'); e.status = res.status; throw e; }
  return data;
}

const saveCart = () => { store.set('cart', S.cart); renderNav(); };
const cartCount = () => S.cart.reduce((n, l) => n + l.qty, 0);

function lineUnit(line) {
  const item = S.menu.items.find(i => i.id === line.id);
  let u = item.price;
  for (const gid of item.groups) {
    const g = S.menu.modifier_groups[gid];
    const sel = line.mods[gid];
    const ids = g.multi ? (sel || []) : [sel || g.default].filter(Boolean);
    for (const id of ids) u += g.options.find(o => o.id === id)?.price || 0;
  }
  return u;
}
function lineDesc(line) {
  const item = S.menu.items.find(i => i.id === line.id);
  const out = [];
  for (const gid of item.groups) {
    const g = S.menu.modifier_groups[gid];
    const sel = line.mods[gid];
    const ids = g.multi ? (sel || []) : [sel || g.default].filter(Boolean);
    for (const id of ids) out.push(g.options.find(o => o.id === id)?.name);
  }
  return out.filter(Boolean);
}

// ---------- layout ----------
function renderNav() {
  const h = location.hash || '#/';
  const on = p => (h === p || (p !== '#/' && h.startsWith(p))) ? 'on' : '';
  $('#nav').innerHTML = `
    <a href="#/" class="${h === '#/' ? 'on' : ''}">Menu</a>
    <a href="#/rewards" class="${on('#/rewards')}">Rewards</a>
    <a href="#/cart" class="${on('#/cart')}${on('#/checkout')}">Cart${cartCount() ? `<span class="badge">${cartCount()}</span>` : ''}</a>`;
}
const view = html => { $('#app').innerHTML = html; window.scrollTo(0, 0); };
function closeModal() { $('#overlay').innerHTML = ''; }
function toast(msg) { alert(msg); }

// ---------- menu ----------
async function viewMenu() {
  const o = S.config.ordering;
  let banner = '';
  if (!o.open) banner = `<div class="notice">${esc(o.reason)}</div>`;
  const last = store.get('lastOrder', null);
  let active = '';
  if (last) {
    try {
      const ord = await api('/orders/' + last);
      if (['received', 'preparing', 'ready'].includes(ord.status))
        active = `<div class="hero"><b>Your order #${esc(ord.id.slice(-6))} is ${statusText(ord.status)}.</b><br><a href="#/order/${esc(ord.id)}">Track it →</a></div>`;
    } catch { }
  }
  const me = S.me;
  const promo = me
    ? `<p>Hi ${esc(me.name)}. You have <b>${me.points} points</b>.</p>`
    : `<p>Join Steve’s Rewards and get <b>${S.config.welcome_bonus} bonus points</b>.</p>`;
  const welcome = `<section class="menu-hero">
    <div class="menu-hero-copy">
      <h1>Order ahead.</h1>
      ${promo}
      <a class="btn" href="#/rewards">${me ? 'See rewards' : 'Join rewards'}</a>
    </div>
  </section>`;
  const cats = [['drink', 'Drinks'], ['beans', 'Beans']];
  view(`${active}${banner}${welcome}
    <div class="menu-heading"><h2>Pickup menu</h2>
    <div class="tabs" aria-label="Menu categories">${cats.map(([k, n]) => `<button class="tab ${S.cat === k ? 'on' : ''}" data-cat="${k}" aria-pressed="${S.cat === k}">${n}</button>`).join('')}</div></div>
    <div class="grid">${S.menu.items.filter(i => i.category === S.cat).map(i => `
      <button class="item" data-id="${i.id}">
        <span><b>${esc(i.name)}</b><small>${esc(i.description)}</small></span>
        <span class="item-action"><span class="price">${money(i.price)}</span><span aria-hidden="true">↗</span></span>
      </button>`).join('')}</div>
    ${cartCount() ? `<div class="bar"><a class="btn block" href="#/cart"><span>View cart (${cartCount()})</span><span>${money(S.cart.reduce((n, l) => n + l.unit * l.qty, 0))}</span></a></div>` : ''}`);
  document.querySelectorAll('.tab').forEach(b => b.onclick = () => { S.cat = b.dataset.cat; viewMenu(); });
  document.querySelectorAll('.item').forEach(b => b.onclick = () => openItem(b.dataset.id));
}

function openItem(id) {
  const item = S.menu.items.find(i => i.id === id);
  const sel = {};
  for (const gid of item.groups) {
    const g = S.menu.modifier_groups[gid];
    sel[gid] = g.multi ? [] : (g.default || null);
  }
  let qty = 1;
  const draw = () => {
    const line = { id, mods: sel };
    const unit = lineUnit(line);
    $('#overlay').innerHTML = `<div class="modal-bg"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="item-title">
      <div class="row"><h1 id="item-title" style="margin:0">${esc(item.name)}</h1><button class="tab" id="x" aria-label="Close item">✕</button></div>
      <p class="muted">${esc(item.description)}</p>
      ${item.groups.map(gid => {
      const g = S.menu.modifier_groups[gid];
      return `<h2>${g.label}${g.required ? '' : ' <span class="muted small">(optional)</span>'}</h2>` + g.options.map(o => {
        const checked = g.multi ? sel[gid].includes(o.id) : sel[gid] === o.id;
        return `<label class="opt"><input type="${g.multi ? 'checkbox' : 'radio'}" name="${gid}" value="${o.id}" ${checked ? 'checked' : ''}>
            <span>${esc(o.name)}</span><span>${o.price ? '+' + money(o.price) : ''}</span></label>`;
      }).join('');
    }).join('')}
      <div class="row" style="margin-top:18px"><div class="qty"><button id="m">−</button><b>${qty}</b><button id="p">+</button></div>
      <button class="btn" id="add">Add · ${money(unit * qty)}</button></div>
    </div></div>`;
    $('#x').onclick = closeModal;
    $('.modal-bg').onclick = e => { if (e.target.classList.contains('modal-bg')) closeModal(); };
    $('#m').onclick = () => { qty = Math.max(1, qty - 1); draw(); };
    $('#p').onclick = () => { qty = Math.min(20, qty + 1); draw(); };
    document.querySelectorAll('.modal input').forEach(inp => inp.onchange = () => {
      const g = S.menu.modifier_groups[inp.name];
      if (g.multi) sel[inp.name] = inp.checked ? [...sel[inp.name], inp.value] : sel[inp.name].filter(v => v !== inp.value);
      else sel[inp.name] = inp.value;
      draw();
    });
    $('#add').onclick = () => {
      const mods = JSON.parse(JSON.stringify(sel));
      const key = JSON.stringify([id, mods]);
      const ex = S.cart.find(l => JSON.stringify([l.id, l.mods]) === key);
      if (ex) ex.qty = Math.min(20, ex.qty + qty);
      else S.cart.push({ id, name: item.name, qty, mods, unit });
      saveCart(); closeModal(); viewMenu();
    };
  };
  draw();
}

// ---------- cart ----------
function viewCart() {
  if (!S.cart.length) return view(`<h1>Your cart</h1><div class="card muted">Nothing here yet. <a href="#/">Browse the menu</a></div>`);
  const sub = S.cart.reduce((n, l) => n + l.unit * l.qty, 0);
  view(`<h1>Your cart</h1>
    ${S.cart.map((l, i) => `<div class="card"><div class="row"><div><b>${esc(l.name)}</b>
      <div class="muted small">${esc(lineDesc(l).join(' · '))}</div></div><span class="price">${money(l.unit * l.qty)}</span></div>
      <div class="row" style="margin-top:10px"><div class="qty"><button data-d="-1" data-i="${i}">−</button><b>${l.qty}</b><button data-d="1" data-i="${i}">+</button></div>
      <button class="btn ghost sm" data-rm="${i}">Remove</button></div></div>`).join('')}
    <div class="row"><b>Subtotal</b><b>${money(sub)}</b></div><p class="muted small">Tax is added at pickup.</p>
    <a class="btn block" href="#/checkout">Checkout</a>`);
  document.querySelectorAll('[data-d]').forEach(b => b.onclick = () => {
    const l = S.cart[b.dataset.i]; l.qty += +b.dataset.d;
    if (l.qty < 1) S.cart.splice(b.dataset.i, 1); else l.qty = Math.min(20, l.qty);
    saveCart(); viewCart();
  });
  document.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => { S.cart.splice(b.dataset.rm, 1); saveCart(); viewCart(); });
}

// ---------- checkout ----------
async function viewCheckout() {
  if (!S.cart.length) { location.hash = '#/cart'; return; }
  const o = S.config.ordering;
  const slots = await api('/slots');
  const free = slots.slots.filter(s => s.available);
  const me = S.me;
  let reward = '', pickupType = 'walkin';
  const lines = () => S.cart.map(l => ({ id: l.id, qty: l.qty, mods: l.mods }));

  const draw = async () => {
    let quote = null, qerr = '';
    try { quote = await api('/quote', { lines: lines(), reward_id: reward || null }); }
    catch (e) { qerr = e.message; if (reward) { reward = ''; try { quote = await api('/quote', { lines: lines() }); } catch { } } }
    const name = $('#name')?.value ?? '', veh = $('#veh')?.value ?? '', when = $('#when')?.value ?? 'asap';
    view(`<h1>Checkout</h1>
      ${!slots.open ? `<div class="notice">${esc(slots.reason)}</div>` : ''}
      <div class="card"><h2 style="margin-top:0">Pickup</h2>
        ${me ? `<div class="muted">Ordering as <b>${esc(me.name)}</b></div>` : `<label>Name for the order</label><input id="name" value="${esc(name)}" placeholder="Your name" maxlength="40">
        <p class="small muted">Want points? <a href="#/rewards">Sign in or join rewards</a> first.</p>`}
        <label>Pickup time</label>
        <select id="when"><option value="asap">ASAP${free[0] ? ' (' + free[0].label + ')' : ''}</option>
          ${free.map(s => `<option value="${s.time}" ${when === s.time ? 'selected' : ''}>${s.label}</option>`).join('')}</select>
        ${S.config.curbside_enabled ? `<label>How are you picking up?</label>
          <select id="ptype"><option value="walkin">Walk in</option><option value="curbside" ${pickupType === 'curbside' ? 'selected' : ''}>Curbside</option></select>
          ${pickupType === 'curbside' ? `<label>Car (color, make)</label><input id="veh" value="${esc(veh)}" placeholder="Blue Honda Civic" maxlength="60">` : ''}` : ''}
      </div>
      ${me ? `<div class="card"><h2 style="margin-top:0">Rewards <span class="muted small">· ${me.points} pts</span></h2>
        <label class="opt"><input type="radio" name="rw" value="" ${!reward ? 'checked' : ''}><span>No reward</span></label>
        ${S.config.rewards.map(r => `<label class="opt" style="${me.points < r.cost ? 'opacity:.5' : ''}"><input type="radio" name="rw" value="${r.id}" ${reward === r.id ? 'checked' : ''} ${me.points < r.cost ? 'disabled' : ''}>
          <span>${esc(r.name)}</span><span>${r.cost} pts</span></label>`).join('')}</div>` : ''}
      ${qerr ? `<div class="err">${esc(qerr)}</div>` : ''}
      ${quote ? `<div class="card">
        <div class="row"><span>Subtotal</span><span>${money(quote.subtotal_cents)}</span></div>
        ${quote.discount_cents ? `<div class="row good"><span>Reward</span><span>−${money(quote.discount_cents)}</span></div>` : ''}
        <div class="row"><b>Total (plus tax)</b><b>${money(quote.total_cents)}</b></div>
        ${me ? `<div class="small muted">You'll earn ${quote.points_to_earn} points when you pick up.</div>` : ''}
        <div class="small muted" style="margin-top:6px">Pay at the counter when you pick up.</div></div>` : ''}
      <div id="err"></div>
      <button class="btn block" id="place" ${!slots.open || !free.length ? 'disabled' : ''}>Place order</button>`);
    $('#ptype')?.addEventListener('change', e => { pickupType = e.target.value; draw(); });
    document.querySelectorAll('[name=rw]').forEach(r => r.onchange = () => { reward = r.value; draw(); });
    $('#place').onclick = async () => {
      $('#place').disabled = true;
      try {
        const ord = await api('/orders', {
          lines: lines(), reward_id: reward || null, pickup_time: $('#when').value,
          name: $('#name')?.value, pickup_type: $('#ptype')?.value || 'walkin', vehicle: $('#veh')?.value,
        });
        S.cart = []; saveCart(); store.set('lastOrder', ord.id);
        if (S.token) loadMe();
        location.hash = '#/order/' + ord.id;
      } catch (e) {
        $('#err').innerHTML = `<div class="err">${esc(e.message)}</div>`;
        $('#place').disabled = false;
        if (e.status === 409) setTimeout(draw, 1200);
      }
    };
  };
  draw();
}

// ---------- order tracking ----------
const statusText = s => ({ received: 'received', preparing: 'being made', ready: 'ready for pickup', picked_up: 'picked up', cancelled: 'cancelled' }[s]);
async function viewOrder(id) {
  if (!/^[A-Fa-f0-9]{6,32}$/.test(id)) return view('<div class="err">Order not found.</div>');
  const draw = async () => {
    let o;
    try { o = await api('/orders/' + id); } catch (e) { return view(`<div class="err">${esc(e.message)}</div>`); }
    if (location.hash !== '#/order/' + id) return;
    const order = ['received', 'preparing', 'ready', 'picked_up'];
    const idx = order.indexOf(o.status);
    view(`<div class="card"><div class="muted small">Order</div><div class="big-code">#${esc(o.id.slice(-6))}</div>
      <div>Pickup <b>${esc(o.pickup_label)}</b> · ${o.pickup_type === 'curbside' ? 'Curbside' : 'Walk-in'} · for ${esc(o.name)}</div></div>
      ${o.status === 'cancelled' ? `<div class="err">This order was cancelled.</div>` : `
      <div class="steps">${[['received', 'Received'], ['preparing', 'Making'], ['ready', 'Ready'], ['picked_up', 'Picked up']].map(([k, n], i) =>
      `<div class="step ${i <= idx ? 'done' : ''}"><i></i>${n}</div>`).join('')}</div>
      ${o.status === 'ready' ? `<div class="hero"><b>Your order is ready!</b><br>${o.pickup_type === 'curbside' ? 'Tap the button below and we\'ll bring it out.' : 'Come on in and grab it at the counter.'}</div>` : ''}
      ${o.pickup_type === 'curbside' && ['received', 'preparing', 'ready'].includes(o.status) ? (o.arrived ? '<div class="notice">We know you\'re here. Hang tight!</div>' : '<button class="btn block" id="here">I\'m here</button>') : ''}`}
      <h2>Items</h2><div class="card">${o.lines.map(l => `<div class="row"><span>${l.qty}× ${esc(l.name)}<div class="muted small">${esc(l.desc.join(' · '))}</div></span></div>`).join('<hr style="border:0;border-top:1px solid var(--line)">')}
      <hr style="border:0;border-top:1px solid var(--line)">
      ${o.discount_cents ? `<div class="row good"><span>Reward</span><span>−${money(o.discount_cents)}</span></div>` : ''}
      <div class="row"><b>Total (plus tax)</b><b>${money(o.total_cents)}</b></div></div>
      ${o.status === 'picked_up' && o.points_awarded ? `<div class="notice">You earned <b>${o.points_awarded} points</b> on this order.</div>` : ''}
      ${!o.has_account && o.status !== 'cancelled' ? `<p class="muted small">Join <a href="#/rewards">rewards</a> next time to earn points on every order.</p>` : ''}
      <a class="btn ghost block" href="#/">Back to menu</a>`);
    $('#here')?.addEventListener('click', async () => {
      try { await api(`/orders/${id}/arrived`, {}); draw(); }
      catch (e) { toast(e.message); }
    });
    if (['received', 'preparing', 'ready'].includes(o.status)) schedule();
  };
  const schedule = () => { clearTimeout(pollTimer); pollTimer = setTimeout(() => { if (location.hash === '#/order/' + id) draw(); }, 5000); };
  draw();
}

// ---------- rewards ----------
async function loadMe() {
  if (!S.token) { S.me = null; return null; }
  try { const d = await api('/me'); S.me = d.customer; return d; }
  catch (e) { if (e.status === 401) { S.token = null; S.me = null; store.set('token', null); } return null; }
}

async function viewRewards() {
  const d = await loadMe();
  if (!d) return viewAuth('signup');
  const me = d.customer, rw = S.config.rewards;
  const next = rw.find(r => r.cost > me.points);
  const pct = next ? Math.min(100, (me.points / next.cost) * 100) : 100;
  view(`<h1>Hi ${esc(me.name)}</h1>
    <div class="card balance-card"><div class="points">${me.points}</div><div class="muted">points</div>
      <div class="meter"><div style="width:${pct}%"></div></div>
      <div class="small muted">${next ? `${next.cost - me.points} more points to unlock a <b>${esc(next.name.toLowerCase())}</b>` : 'You can redeem any reward!'}</div></div>
    <div class="card"><h2 style="margin-top:0">Rewards</h2>
      ${rw.map(r => `<div class="reward"><div><b>${esc(r.name)}</b><div class="small muted">${r.cost} points</div></div>
      ${me.points >= r.cost ? '<span class="pill ok">Ready to redeem</span>' : `<span class="pill">${r.cost - me.points} to go</span>`}</div>`).join('')}
      <p class="small muted" style="margin-bottom:0">Earn 1 point per $1. Choose a reward at checkout.</p></div>
    <h2>Recent activity</h2><div class="card">${d.ledger.length ? d.ledger.map(l => `<div class="row small" style="padding:4px 0"><span>${esc(l.reason)}</span><b class="${l.delta > 0 ? 'good' : ''}">${l.delta > 0 ? '+' : ''}${l.delta}</b></div>`).join('') : '<span class="muted">No activity yet.</span>'}</div>
    <h2>Past orders</h2>${d.orders.length ? d.orders.map(o => `<a class="item" style="text-decoration:none;margin-bottom:8px" href="#/order/${o.id}"><span><b>#${esc(o.id.slice(-6))}</b><small>${o.lines.map(l => l.qty + '× ' + esc(l.name)).join(', ')}</small></span><span class="price">${money(o.total_cents)}</span></a>`).join('') : '<div class="card muted">No orders yet.</div>'}
    <button class="btn ghost block" id="out" style="margin-top:14px">Sign out</button>`);
  $('#out').onclick = async () => {
    try { await api('/logout', {}); }
    catch (e) { return toast(e.message); }
    S.token = null; S.me = null; store.set('token', null);
    renderNav(); viewRewards();
  };
}

function viewAuth(mode) {
  const su = mode === 'signup';
  view(`<h1>${su ? 'Join Steve\'s Rewards' : 'Welcome back'}</h1>
    <div class="hero">Earn 1 point for every $1. Redeem 100 points for a free drink or 200 points for a free bag of beans. <b>Join today and get ${S.config.welcome_bonus} bonus points.</b></div>
    <div class="card">
      ${su ? '<label>Name</label><input id="n" maxlength="40" autocomplete="given-name">' : ''}
      <label>Mobile number</label><input id="ph" type="tel" inputmode="tel" placeholder="(555) 123-4567" autocomplete="tel">
      <label>4-digit PIN</label><input id="pin" type="password" inputmode="numeric" maxlength="4" placeholder="••••" autocomplete="${su ? 'new-password' : 'current-password'}">
      <div id="err"></div>
      <button class="btn block" id="go" style="margin-top:14px">${su ? 'Join rewards' : 'Sign in'}</button>
      <p class="small muted" style="text-align:center"><a href="#" id="sw">${su ? 'Already a member? Sign in' : 'New here? Join rewards'}</a></p>
    </div>`);
  $('#sw').onclick = e => { e.preventDefault(); viewAuth(su ? 'login' : 'signup'); };
  $('#go').onclick = async () => {
    try {
      const d = await api(su ? '/signup' : '/login', { name: $('#n')?.value, phone: $('#ph').value, pin: $('#pin').value });
      S.token = d.token; S.me = d.customer; store.set('token', d.token); renderNav(); viewRewards();
    } catch (e) { $('#err').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };
}

// ---------- router ----------
async function route() {
  clearTimeout(pollTimer); closeModal(); renderNav();
  const h = location.hash || '#/';
  try {
    if (S.token && !S.me) await loadMe();
    S.config = await api('/config');
    if (h === '#/') await viewMenu();
    else if (h === '#/cart') viewCart();
    else if (h === '#/checkout') await viewCheckout();
    else if (h === '#/rewards') await viewRewards();
    else if (h.startsWith('#/order/')) await viewOrder(h.slice(8));
    else await viewMenu();
  } catch (e) { view(`<div class="err">${esc(e.message)}</div>`); }
}

(async () => {
  try {
    S.menu = await api('/menu');
    const availableIds = new Set(S.menu.items.map(item => item.id));
    const availableCart = S.cart.filter(line => availableIds.has(line.id));
    if (availableCart.length !== S.cart.length) {
      S.cart = availableCart;
      saveCart();
    }
    window.addEventListener('hashchange', route);
    await route();
  } catch (e) {
    view(`<div class="err">${esc(e.message)}</div><button class="btn" id="retry">Reload app</button>`);
    $('#retry').onclick = () => location.reload();
  }
})();
