// acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
// utils.js

const $        = (id) => document.getElementById(id);
const AUTH_KEY = 'acidcasino_auth';
const SUITS    = { s: '♠', h: '♥', d: '♦', c: '♣' };
const CHIPS    = [[100, '#cfe9d8'], [500, '#ff3b5c'], [2500, '#1fd862'], [10000, '#0b0f0c'], [50000, '#9b5cff'], [100000, '#d4ff3a'], [500000, '#ff9f1c'], [1000000, '#38c8ff'], [2500000, '#ff5ed1']];
const SYM_HTML = { C: '🍒', L: '🍋', O: '🍊', P: '🍇', B: '🔔', R: '<span class="txt bar">BAR</span>', 7: '<span class="txt seven">7</span>', W: '<span class="txt wild">WILD</span>', S: '⭐' };
const BJ_SPOTS = { wide: [[11, 72], [30, 88], [50, 95], [70, 88], [89, 72]], tall: [[12, 74], [31, 85], [50, 94], [69, 85], [88, 74]], solo: [[50, 95]] };
const PK_SPOTS = { wide: [[50, 100], [7, 78], [7, 22], [50, 0], [93, 22], [93, 78]], tall: [[50, 100], [0, 72], [0, 28], [50, 0], [100, 28], [100, 72]] };
const LINE_COLORS = ['#39ff7a', '#38c8ff', '#ff5ed1', '#d4ff3a', '#ff9f1c', '#9b5cff', '#ff3b5c', '#2ef2d0', '#ffd23a', '#ff7a7a'];
const RESULT   = { win: 'Win', lose: 'Lose', bust: 'Bust', push: 'Push', blackjack: 'Blackjack', surrender: 'Surrender' };

let token     = localStorage.getItem(AUTH_KEY) || '';
let me        = null;     // wallet of the signed in player
let lobby     = { tables: [], online: 0 };
let route     = { view: 'lobby', table: null };
let table     = null;     // the table on screen
let prevTable = null;
let offset    = 0;        // server clock minus ours, in ms
let source    = null;
let sourceKey = '';
let filter    = 'all';
let options   = null;
let bjBet     = 0;
let bjLast    = 0;
let bjTimer   = null;
let pkSeat    = null;
let pkBuzz    = '';
let slotInfo  = null;
let slotBet   = 0;
let spinning  = false;
let autoLeft  = 0;      // spins left in an auto run
let winCycle  = null;   // timer that steps through the wins of the last spin
let authMode  = 'login';
let captchaId = '';
let bank      = null;   // loan and refill rules from the server
let feedSeen  = 0;      // newest live feed id shown
let sideTab   = 'ranks';
let houseLast = null;
let arcadeUrl = '';     // acid-arcade-db public site when accounts live there, for pictures and account settings
let ssoTried  = false;  // the shared sign-in is only tried once per page
const bubbles = new Map(); // player name -> their chat bubble on the table
const chat    = { room: 'lobby', lobby: [], table: [], seen: { lobby: new Set(), table: new Set() }, unread: { lobby: 0, table: 0 } };


// ---------- helpers ----------

function money(c) {
	const neg = c < 0;
	c = Math.abs(Math.round(c));
	const d = Math.floor(c / 100), r = c % 100;
	return (neg ? '-' : '') + '$' + d.toLocaleString('en-US') + (r ? '.' + String(r).padStart(2, '0') : '');
}

function avatar(id) {
	// A player's picture from acid-arcade-db, nothing for guests, bots or standalone accounts
	return arcadeUrl && id ? `<img class="avatar" src="${esc(arcadeUrl)}/avatar/${id}" alt="">` : '';
}

function who(name, id) {
	// A player's name, with their picture and a crown for anyone who has toppled the house
	const crowned = lobby.house && lobby.house.crowns && lobby.house.crowns.includes(name);
	return avatar(id) + esc(name) + (crowned ? '<span class="crown-tag" title="Toppled the house">♛</span>' : '');
}

function esc(s) {
	return String(s).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
}

function now() {
	return (Date.now() + offset) / 1000;
}

async function api(path, body) {
	const opts = { method: body ? 'POST' : 'GET', headers: { 'X-Auth-Token': token } };
	if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
	let res, data;
	try {
		res  = await fetch(path, opts);
		data = await res.json();
	} catch (e) {
		toast('Connection problem, try again');
		throw e;
	}
	if (res.status === 401 && path !== '/auth/login') { if (arcadeUrl && !ssoTried) loadMe().catch(() => {}); else signedOut(); }
	if (data && data.error) { toast(data.error); throw new Error(data.error); }
	return data;
}

let toastTimer = null;
function toast(text) {
	const t = $('toast');
	t.textContent = text;
	t.classList.remove('hidden');
	t.style.animation = 'none'; void t.offsetWidth; t.style.animation = '';
	clearTimeout(toastTimer);
	toastTimer = setTimeout(() => t.classList.add('hidden'), 2800);
}

function openModal(html) {
	$('modal').innerHTML = html;
	$('modal-bg').classList.remove('hidden');
}

function closeModal() {
	$('modal-bg').classList.add('hidden');
	$('modal').innerHTML = '';
}

function duration(sec) {
	const h = Math.floor(sec / 3600), m = Math.ceil((sec % 3600) / 60);
	return h ? `${h}h ${m}m` : `${m}m`;
}

function cardEl(code) {
	const el = document.createElement('div');
	el.dataset.code = code || '';
	if (!code) { el.className = 'card back'; return el; }
	const rank = code[0] === 'T' ? '10' : code[0], suit = code[1];
	el.className = 'card' + (suit === 'h' || suit === 'd' ? ' red' : '');
	el.innerHTML = `<div class="r">${rank}<small>${SUITS[suit]}</small></div><div class="pip">${SUITS[suit]}</div>`;
	return el;
}

function paintCard(el, code) {
	// Draw a card face onto an element that is already in the page
	const face = cardEl(code);
	el.classList.toggle('red', face.classList.contains('red'));
	el.innerHTML = face.innerHTML;
}

// Keep the cards already on screen and only animate the new ones, flying in from a source element
let dealQueue = 0;
function syncCards(box, list, from) {
	const old = [...box.children];
	list.forEach((c, i) => {
		const code = c || '';
		if (old[i] && old[i].dataset.code === code) return;
		const el = cardEl(c);
		if (old[i] && !old[i].dataset.code && code) el.classList.add('flip');
		else if (from) {
			const a = from.getBoundingClientRect(), b = box.getBoundingClientRect();
			el.style.setProperty('--dx', `${a.left + a.width / 2 - b.left - b.width / 2}px`);
			el.style.setProperty('--dy', `${a.top + a.height / 2 - b.top - b.height / 2}px`);
			el.style.animationDelay = `${dealQueue++ * 0.11}s`;
			el.classList.add('deal');
		}
		if (old[i]) box.replaceChild(el, old[i]); else box.appendChild(el);
	});
	while (box.children.length > list.length) box.lastChild.remove();
}

function chipStack(amount) {
	// Break an amount into chips, biggest first, at most 6 shown
	let html = '', left = amount, n = 0;
	for (const [v, color] of [...CHIPS].reverse()) {
		while (left >= v && n < 6) { html += `<div class="mini-chip" style="--c:${color};top:${-n * 3}px"></div>`; left -= v; n++; }
	}
	return html;
}

function burst(el, count = 18) {
	if (!el) return;
	const r = el.getBoundingClientRect(), fx = $('fx');
	for (let i = 0; i < count; i++) {
		const s = document.createElement('span');
		s.className = 'spark';
		s.textContent = ['$', '♣', '♠', '♦', '♥', '✦'][i % 6];
		const a = Math.random() * Math.PI * 2, d = 60 + Math.random() * 120;
		s.style.left = `${r.left + r.width / 2}px`;
		s.style.top  = `${r.top + r.height / 2}px`;
		s.style.fontSize = `${12 + Math.random() * 14}px`;
		s.style.setProperty('--x', `${Math.cos(a) * d}px`);
		s.style.setProperty('--y', `${Math.sin(a) * d - 40}px`);
		s.style.setProperty('--r', `${Math.random() * 360 - 180}deg`);
		fx.appendChild(s);
		setTimeout(() => s.remove(), 1200);
	}
}

function floatNum(el, cents) {
	if (!el || !cents) return;
	const r = el.getBoundingClientRect(), s = document.createElement('div');
	s.className = 'float-num' + (cents < 0 ? ' neg' : '');
	s.textContent = (cents > 0 ? '+' : '') + money(cents);
	s.style.left = `${r.left + r.width / 2}px`;
	s.style.top  = `${r.top}px`;
	$('fx').appendChild(s);
	setTimeout(() => s.remove(), 1500);
}

// Count the balance up or down instead of jumping
let shownBal = null, balAnim = null;
function setBalance(cents) {
	const el = $('bal'), btn = $('btn-wallet');
	if (shownBal === null) { shownBal = cents; el.textContent = money(cents); return; }
	if (cents === shownBal) return;
	btn.classList.remove('up', 'down'); void btn.offsetWidth;
	btn.classList.add(cents > shownBal ? 'up' : 'down');
	cancelAnimationFrame(balAnim);
	const from = shownBal, start = performance.now();
	shownBal = cents;
	const step = (t) => {
		const k = Math.min(1, (t - start) / 600);
		el.textContent = money(from + (cents - from) * (1 - Math.pow(1 - k, 3)));
		if (k < 1) balAnim = requestAnimationFrame(step);
	};
	balAnim = requestAnimationFrame(step);
}

// Turn timers and countdowns follow the server's clock
function timers() {
	const t = now();
	document.querySelectorAll('[data-deadline]').forEach((el) => {
		const left = Math.max(0, Number(el.dataset.deadline) - t), total = Number(el.dataset.total) || 1;
		if (el.classList.contains('timer')) { el.style.setProperty('--p', Math.min(1, left / total)); el.classList.toggle('low', left / total < 0.3); }
		else el.textContent = Math.ceil(left);
	});
	requestAnimationFrame(timers);
}


// ---------- accounts ----------

async function loadMe() {
	const data = await api('/auth/me');
	$('start-chips').textContent = money(data.start);
	bank = data;
	arcadeUrl = data.arcade_public_url || '';
	if (!data.user) return (await trySso()) ? loadMe() : signedOut();
	me = data.user;
	shownBal = null;
	setBalance(me.balance);
	$('top-user').classList.remove('hidden');
	$('nav').classList.remove('hidden');
	$('chat').classList.remove('hidden'); // on phones it stays tucked away until opened
	go();
}

async function trySso() {
	// Signed in on another acid-arcade site of this domain? Swap the shared cookie for a session here, once
	if (!arcadeUrl || ssoTried) return false;
	ssoTried = true;
	try {
		const res = await fetch('/auth/sso');
		if (!res.ok) return false;
		token = (await res.json()).token;
		localStorage.setItem(AUTH_KEY, token);
		return true;
	} catch (e) {
		return false;
	}
}

function signedOut() {
	token = '';
	me    = null;
	localStorage.removeItem(AUTH_KEY);
	if (source) source.close();
	source = null; sourceKey = '';
	$('top-user').classList.add('hidden');
	$('nav').classList.add('hidden');
	$('chat').classList.add('hidden');
	document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
	$('view-auth').classList.remove('hidden');
}

async function loadCaptcha() {
	const data = await api('/auth/captcha');
	captchaId = data.captcha_id;
	$('captcha-img').innerHTML = data.svg;
	$('captcha').value = '';
}

function setAuthMode(mode) {
	authMode = mode;
	document.querySelectorAll('#auth-seg button').forEach((b) => b.classList.toggle('active', b.dataset.mode === mode));
	$('captcha-box').classList.toggle('hidden', mode !== 'signup');
	$('auth-submit').textContent = mode === 'signup' ? 'Create account' : 'Sign in';
	$('auth-pass').autocomplete = mode === 'signup' ? 'new-password' : 'current-password';
	$('auth-error').textContent = '';
	if (mode === 'signup') loadCaptcha();
}

async function submitAuth(e) {
	e.preventDefault();
	const body = { username: $('auth-user').value.trim(), password: $('auth-pass').value };
	if (authMode === 'signup') Object.assign(body, { captcha_id: captchaId, captcha: $('captcha').value });
	$('auth-error').textContent = '';
	try {
		const data = await api(authMode === 'signup' ? '/auth/signup' : '/auth/login', body);
		token = data.token;
		localStorage.setItem(AUTH_KEY, token);
		$('auth-pass').value = '';
		await loadMe();
	} catch (err) {
		$('auth-error').textContent = err.message;
		if (authMode === 'signup') loadCaptcha();
	}
}

function openAccount() {
	openModal(`<h3>${who(me.username, me.id)}</h3>
		<p class="note">Your chips, seats and loans are saved to your account.</p>
		${arcadeUrl ? `<div class="btns"><a class="btn wide" href="${esc(arcadeUrl)}/account" target="_blank" rel="noopener">Account settings</a></div>` : ''}
		<div class="btns"><button class="btn danger wide" id="btn-logout">Sign out</button></div>
		<p class="credit" style="margin-top:14px">Developed by <a href="https://github.com/acidvegas" target="_blank" rel="noopener">acidvegas</a></p>`);
	$('btn-logout').onclick = async () => { await api('/auth/logout', {}); closeModal(); signedOut(); };
}

function scoreLabel(score) {
	return score < 500 ? 'Poor' : score < 600 ? 'Fair' : score < 700 ? 'Good' : score < 780 ? 'Very good' : 'Excellent';
}

async function openWallet() {
	const w = await api('/wallet');
	let refill = '';
	if (w.refill_ready_in !== null) refill = `<p class="note">Your free ${money(bank.refill)} lands in <b>${duration(w.refill_ready_in)}</b>.</p>`;
	else if (w.can_refill) refill = `<p class="note">You're under ${money(bank.refill_under)}. Ask for a free ${money(bank.refill)}: no debt, but it takes 24 hours to arrive.</p><div class="btns"><button class="btn wide" id="btn-refill">Request a free ${money(bank.refill)}</button></div>`;
	else refill = `<p class="note">Under ${money(bank.refill_under)} with nothing on a table? You can ask for a free ${money(bank.refill)} that lands 24 hours later.</p>`;
	const pct = (x) => `${Math.round(x * 100)}%`, [lo, hi] = bank.score_range;
	const debt = w.owed ? `<div class="debt">
			<p class="note">You owe the house <b class="neg">${money(w.owed)}</b> at <b>${pct(w.rate)} a day</b>, charged hourly${w.debt_days !== null ? `, open for <b>${w.debt_days < 1 ? 'under a day' : Math.floor(w.debt_days) + (Math.floor(w.debt_days) === 1 ? ' day' : ' days')}</b>` : ''}. ${100 / bank.repay_share}% of every win goes toward it.</p>
			<div class="money-row"><span class="dollar">$</span><input id="repay-amt" inputmode="numeric" value="${Math.min(Math.floor(w.owed / 100), Math.floor(w.balance / 100)) || ''}"><button class="btn" id="btn-repay">Pay back</button><button class="btn primary" id="btn-repay-all">Pay it all</button></div>
		</div>` : '';
	const tiers = bank.tiers.map(([under, limit, rate], i) => {
		const from = i ? bank.tiers[i - 1][0] : lo, to = under === null ? hi : under - 1, on = w.score >= from && w.score <= to;
		return `<tr class="${on ? 'on' : ''}"><td>${from}-${to}</td><td>${money(limit)}</td><td>${pct(rate)}</td></tr>`;
	}).join('');
	const borrow = w.loans_closed ? `<p class="note closed">${esc(w.loans_closed[0].toUpperCase() + w.loans_closed.slice(1))}.</p>`
		: w.loan_room < bank.loan_min ? `<p class="note closed">You're at your ${money(w.limit)} credit limit. Pay some back to borrow again.</p>`
		: `<div class="quick">${[10000, 50000, 100000, 500000, 1000000, 2500000].filter((c) => c <= w.loan_room).map((c) => `<button class="btn small" data-borrow="${c}">${money(c)}</button>`).join('')}</div>
		<div class="money-row"><span class="dollar">$</span><input id="loan-amt" inputmode="numeric" value="${Math.min(1000, Math.floor(w.loan_room / 100))}"><button class="btn primary" id="btn-borrow">Borrow</button></div>
		<p class="note" id="loan-preview"></p>`;
	openModal(`<h3>Your chips</h3>
		<div class="big-num">${money(w.balance)}</div>
		${debt}
		<h4>Credit score</h4>
		<div class="score"><b>${w.score}</b><span>${scoreLabel(w.score)}</span><div class="score-bar"><i style="left:${(w.score - lo) / (hi - lo) * 100}%"></i></div></div>
		<p class="note">Limit <b>${money(w.limit)}</b> · interest <b>${pct(w.rate)} a day</b>${w.owed ? ` · <b>${money(Math.max(0, w.limit - w.owed))}</b> left to borrow` : ''}</p>
		<table class="tiers"><tr><th>Score</th><th>Limit</th><th>Interest</th></tr>${tiers}</table>
		<p class="note">Pay a debt off within ${bank.paid_fast[0] / 86400} days: <b>+${bank.paid_fast[1]}</b>, within ${bank.paid_ok[0] / 86400} days: <b>+${bank.paid_ok[1]}</b> <i>(once a day)</i>. Each day a debt stays open past ${bank.late_after / 86400} days: <b>-${bank.late_cost}</b>. Borrowing up to your limit: <b>-${bank.maxed_cost}</b>. Nobody can owe more than ${money(bank.debt_cap)}.</p>
		<h4>Borrow from the house</h4>
		${borrow}
		<h4>Free refill</h4>
		${refill}`);
	const cents = (id) => Math.round(parseFloat($(id).value.replace(/[$,]/g, '')) * 100) || 0;
	if ($('loan-amt')) {
		const preview = () => {
			const amt = cents('loan-amt');
			if (amt < bank.loan_min) { $('loan-preview').textContent = `Borrow at least ${money(bank.loan_min)}.`; return; }
			if (amt > w.loan_room) { $('loan-preview').textContent = `You can borrow up to ${money(w.loan_room)} right now.`; return; }
			const total = w.owed + amt, r = w.rate, grow = (d) => Math.min(bank.debt_cap, total * Math.pow(1 + r, d));
			$('loan-preview').innerHTML = `At <b>${pct(r)} a day</b> your debt of ${money(total)} becomes <b>${money(grow(1))}</b> after a day and <b>${money(grow(7))}</b> after a week if nothing is paid back.${total >= w.limit ? ` Borrowing up to your limit costs <b>${bank.maxed_cost}</b> points.` : ''}`;
		};
		$('loan-amt').oninput = preview;
		preview();
		document.querySelectorAll('[data-borrow]').forEach((b) => { b.onclick = () => { $('loan-amt').value = b.dataset.borrow / 100; preview(); }; });
		$('btn-borrow').onclick = async () => { const amt = cents('loan-amt'); await api('/loan', { amount: amt }); toast(`Borrowed ${money(amt)}`); openWallet(); };
	}
	if ($('btn-repay')) $('btn-repay').onclick = async () => { await api('/repay', { amount: cents('repay-amt') }); toast('Paid back'); openWallet(); };
	if ($('btn-repay-all')) $('btn-repay-all').onclick = async () => { await api('/repay', { amount: w.owed }); toast('Paid back'); openWallet(); };
	if ($('btn-refill')) $('btn-refill').onclick = async () => { await api('/refill', {}); openWallet(); };
}


// ---------- routing and the live stream ----------

function parseHash() {
	const h = location.hash.replace(/^#\/?/, '');
	if (h.startsWith('t/')) return { view: 'table', table: h.slice(2) };
	if (h === 'slots') return { view: 'slots', table: null };
	return { view: 'lobby', table: null };
}

function go() {
	if (!me) return;
	route = parseHash();
	if (route.table !== (table && table.id)) { prevTable = null; table = null; chat.table = []; chat.seen.table = new Set(); chat.unread.table = 0; pkSeat = null; bubbles.forEach((b) => { clearTimeout(b.timer); b.el.remove(); }); bubbles.clear(); closeReact(); }
	document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.nav === route.view));
	$('chat-tab-table').classList.toggle('hidden', route.view !== 'table');
	if (route.view !== 'table' && chat.room === 'table') setRoom('lobby');
	if (route.view === 'table' && !table && chat.room === 'lobby') setRoom('table'); // at a table the chat box talks to the table, so messages show as bubbles over the seats
	const viewId = route.view === 'table' ? null : 'view-' + route.view;
	document.querySelectorAll('.view').forEach((v) => {
		const want = viewId ? v.id === viewId : false;
		if (want === v.classList.contains('hidden')) v.classList.toggle('hidden', !want);
	});
	if (route.view === 'lobby') { renderLobby(); loadRanks(); }
	if (route.view === 'slots') loadSlots();
	connect();
}

function connect() {
	const key = `${token}|${route.table || ''}`;
	if (key === sourceKey && source && source.readyState !== 2) return;
	if (source) source.close();
	sourceKey = key;
	source = new EventSource(`/events?t=${encodeURIComponent(token)}${route.table ? '&table=' + encodeURIComponent(route.table) : ''}`);
	const parse = (e) => { const d = JSON.parse(e.data); offset = d.now * 1000 - Date.now(); return d.data; };
	source.addEventListener('lobby', (e) => { lobby = parse(e); $('online').textContent = `${lobby.online} online`; renderHouse(); if (route.view === 'lobby') renderLobby(); if (route.view === 'slots' && slotInfo && !spinning && !autoLeft) renderSlotBet(); });
	source.addEventListener('feed', (e) => addFeed(JSON.parse(e.data)));
	source.addEventListener('me', (e) => { const w = parse(e); if (!w) return; const wasDue = me && me.refill_ready_in !== null; me = Object.assign(me || {}, w); setBalance(w.balance); if (wasDue && w.refill_ready_in === null) toast(`Your free ${money(bank.refill)} arrived`); if (table) { prevTable = table; renderTable(); } }); // same table state, so nothing on it counts as new
	source.addEventListener('table', (e) => {
		const t = parse(e);
		if (!t) { if (route.view === 'table') { toast('That table is closed'); location.hash = '#/lobby'; } return; }
		prevTable = table;
		table = t;
		renderTable();
	});
	source.addEventListener('chat', (e) => { const d = JSON.parse(e.data); addChat(d.room, d.messages); });
	source.addEventListener('react', (e) => JSON.parse(e.data).forEach(showReact));
	source.onerror = () => { if (source.readyState === 2) { sourceKey = ''; setTimeout(connect, 3000); } };
}


// ---------- lobby ----------

function renderLobby() {
	const list = lobby.tables.filter((t) => filter === 'all' || t.game === filter);
	const box  = $('table-list');
	if (!list.length) { box.innerHTML = '<div class="empty-note">No tables here yet. Open one with <b>New table</b>.</div>'; return; }
	const sig = JSON.stringify(list);
	if (box.dataset.sig === sig) return;
	const first = !box.dataset.sig;
	box.dataset.sig = sig;
	box.innerHTML = list.map((t, i) => {
		const tags = [t.mine ? 'SEATED' : '', t.solo ? 'SOLO' : t.private ? 'PRIVATE' : '', !t.house && !t.solo ? 'PLAYER' : ''].filter(Boolean).map((x) => `<span class="tag">${x}</span>`).join('');
		const pips = Array.from({ length: t.seats }, (_, k) => `<span class="spip ${k < t.people.length ? 'on' : k < t.people.length + t.bots ? 'bot' : ''}"></span>`).join('');
		const who  = t.people.length ? t.people.map((n, k) => avatar(t.ids[k]) + esc(n)).join(', ') + (t.bots ? ` + ${t.bots} bot${t.bots > 1 ? 's' : ''}` : '') : (t.bots ? `${t.bots} bots` : 'empty');
		return `<a class="trow ${t.mine ? 'mine' : ''}" href="#/t/${t.id}" style="animation-delay:${first ? i * 0.03 : 0}s">
			<span class="glyph ${t.game === 'poker' ? 'pk' : ''}">${t.game === 'poker' ? '♠' : '21'}</span>
			<span class="info"><div class="tname">${esc(t.name)}${tags}</div><div class="tsub"><b>${t.stakes}</b> · ${who}</div></span>
			<span class="pips">${pips}</span>
			<span class="btn small ${t.mine ? 'primary' : ''}">${t.mine ? 'Back' : t.taken >= t.seats ? 'Watch' : 'Join'}</span>
		</a>`;
	}).join('');
}

async function loadRanks() {
	const data = await api('/leaderboard');
	$('ranks').innerHTML = data.rows.map((r) => `<li class="${me && r.username === me.username ? 'me' : ''}"><span class="n">${who(r.username, r.id)}</span><span class="w ${r.worth < 0 ? 'neg' : ''}">${money(r.worth)}</span></li>`).join('') || '<li class="dim">Nobody yet</li>';
	$('fame').innerHTML = data.fame.map((r) => `<li><span class="crown">♛</span><span class="n">${avatar(r.user_id)}${esc(r.username)}<small>${new Date(r.at * 1000).toLocaleDateString()} · beat ${money(r.house)}</small></span><span class="w">${money(r.worth)}</span></li>`).join('') || '<li class="dim">Nobody has toppled the house yet. Be the first to get richer than it.</li>';
}

function addFeed(list) {
	const box = $('feed');
	if (!feedSeen) box.innerHTML = '';
	const label = { slots: 'SLOTS', blackjack: '21', poker: 'POKER' };
	for (const r of list) {
		if (r.id <= feedSeen) continue;
		feedSeen = r.id;
		const li = document.createElement('li');
		li.className = r.net > 0 ? 'win' : r.net < 0 ? 'lose' : 'even';
		if (me && r.name === me.username) li.classList.add('me');
		li.innerHTML = `<span class="g">${label[r.game] || r.game}</span><span class="n">${who(r.name)}</span><span class="w">${r.net > 0 ? '+' : ''}${r.net ? money(r.net) : 'even'}</span>`;
		box.prepend(li);
	}
	while (box.children.length > 60) box.lastChild.remove();
}

let crownSig = '';
function renderHouse() {
	const h = lobby.house;
	if (!h || !h.balance) return;
	const sig = (h.crowns || []).join('|');
	if (sig !== crownSig) { // someone new wears a crown, so redraw the names already on screen
		crownSig = sig;
		if (route.view === 'lobby') loadRanks().catch(() => {});
		$('chat-log').dataset.room = '';
		renderChat();
	}
	const bal = $('house-bal');
	bal.textContent = money(h.balance);
	if (houseLast !== null && h.balance !== houseLast) {
		bal.classList.remove('up', 'down'); void bal.offsetWidth;
		bal.classList.add(h.balance < houseLast ? 'down' : 'up');
	}
	houseLast = h.balance;
	const top = h.top, worth = top ? Math.max(top.worth, 100) : 100;
	const pct = Math.min(100, Math.log10(worth / 100) / Math.log10(h.balance / 100) * 100); // log scale, a straight line would sit at zero for a long time
	$('house-fill').style.width = `${Math.max(pct, 1)}%`;
	$('house').classList.toggle('toppled', !!top && top.worth > h.balance);
	$('house-note').innerHTML = !top ? 'Get richer than the house to topple it'
		: top.worth > h.balance ? `<b>${esc(top.name)}</b> has toppled the house with ${money(top.worth)}`
		: `<b>${esc(top.name)}</b> leads with ${money(top.worth)} and needs <b>${money(h.balance - top.worth)}</b> more to topple the house`;
	if (!h.lending) $('house-note').innerHTML += ' · <b style="color:var(--red)">loans paused</b>';
}

async function openCreate(preset = {}) {
	if (!options) options = await api('/options');
	const st = Object.assign({ game: 'blackjack', stakes: 'low', mode: 'public', bots: true }, preset);
	const draw = () => {
		const stakes = options[st.game];
		if (!stakes.find((s) => s.id === st.stakes)) st.stakes = stakes[0].id;
		const modes = st.game === 'blackjack' ? [['solo', 'Solo', 'Just you and the dealer'], ['public', 'Public', 'Anyone can join'], ['private', 'Private', 'Invite with a link']] : [['public', 'Public', 'Anyone can join'], ['private', 'Private', 'Invite with a link']];
		if (!modes.find((m) => m[0] === st.mode)) st.mode = 'public';
		openModal(`<h3>New table</h3>
			<div class="seg" id="c-game" style="width:100%"><button data-v="blackjack" class="${st.game === 'blackjack' ? 'active' : ''}" style="flex:1">Blackjack</button><button data-v="poker" class="${st.game === 'poker' ? 'active' : ''}" style="flex:1">Hold'em</button></div>
			<h4>Stakes</h4>
			<div class="opt-grid" id="c-stakes">${stakes.map((s) => `<button class="opt ${s.id === st.stakes ? 'active' : ''}" data-v="${s.id}"><b>${s.label}</b><span>${s.text}</span>${s.buyin ? `<small>buy in ${s.buyin}</small>` : '<small>bet range</small>'}</button>`).join('')}</div>
			<h4>Who can play</h4>
			<div class="opt-grid" id="c-mode">${modes.map((m) => `<button class="opt ${m[0] === st.mode ? 'active' : ''}" data-v="${m[0]}"><b>${m[1]}</b><small>${m[2]}</small></button>`).join('')}</div>
			${st.game === 'poker' ? `<h4>Computer players</h4><div class="opt-grid" id="c-bots"><button class="opt ${st.bots ? 'active' : ''}" data-v="1"><b>Bots on</b><small>Fill empty seats</small></button><button class="opt ${!st.bots ? 'active' : ''}" data-v="0"><b>People only</b><small>Needs 2+ players</small></button></div>` : ''}
			<div class="btns"><button class="btn primary wide" id="c-go">Open table</button></div>`);
		document.querySelectorAll('#c-game button').forEach((b) => { b.onclick = () => { st.game = b.dataset.v; draw(); }; });
		document.querySelectorAll('#c-stakes button').forEach((b) => { b.onclick = () => { st.stakes = b.dataset.v; draw(); }; });
		document.querySelectorAll('#c-mode button').forEach((b) => { b.onclick = () => { st.mode = b.dataset.v; draw(); }; });
		document.querySelectorAll('#c-bots button').forEach((b) => { b.onclick = () => { st.bots = b.dataset.v === '1'; draw(); }; });
		$('c-go').onclick = async () => {
			const res = await api('/tables/create', { game: st.game, stakes: st.stakes, private: st.mode !== 'public', solo: st.mode === 'solo', bots: st.bots });
			closeModal();
			if (st.game === 'blackjack') await api('/table/sit', { table: res.id }).catch(() => {});
			location.hash = `#/t/${res.id}`;
		};
	};
	draw();
}


// ---------- chat ----------

function addChat(room, msgs) {
	let added = 0;
	for (const m of msgs) {
		if (chat.seen[room].has(m.id)) continue;
		chat.seen[room].add(m.id);
		chat[room].push(m);
		added++;
		if (room === 'table' && !m.system && now() - m.at < 8) chatBubble(m.name, m.text); // only fresh messages, not the history sent on joining
	}
	if (chat[room].length > 150) chat[room] = chat[room].slice(-150);
	if (!added) return;
	const visible = chat.room === room && chatOpen();
	if (!visible && msgs.some((m) => !me || m.name !== me.username)) chat.unread[room] += added;
	if (chat.room === room) renderChat(); else badges();
	badges();
}

function chatOpen() {
	return window.innerWidth >= 1100 ? !$('chat').classList.contains('hidden') : $('chat').classList.contains('open');
}

function renderChat() {
	if (chat.room === 'feed') return badges();
	const log = $('chat-log'), list = chat[chat.room];
	const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
	if (!list.length) { log.innerHTML = `<div class="chat-empty">${chat.room === 'lobby' ? 'Say hi to the whole casino.' : 'Talk to this table.'}</div>`; return; }
	const shown = Number(log.dataset.count || 0), roomKey = chat.room + (chat.room === 'table' ? route.table : '');
	if (log.dataset.room !== roomKey || shown > list.length) { log.innerHTML = ''; log.dataset.count = 0; log.dataset.room = roomKey; }
	for (const m of list.slice(Number(log.dataset.count))) {
		const d = document.createElement('div');
		const at = new Date(m.at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
		d.className = 'msg' + (m.system ? ' sys' : '');
		d.innerHTML = m.system ? esc(m.text) : `<span class="who ${me && m.name === me.username ? 'me' : ''}">${who(m.name, m.user_id)}</span>${esc(m.text)}<span class="at">${at}</span>`;
		log.appendChild(d);
	}
	log.dataset.count = list.length;
	if (atBottom || list[list.length - 1].name === (me && me.username)) log.scrollTop = log.scrollHeight;
	if (chatOpen()) chat.unread[chat.room] = 0;
	badges();
}

function badges() {
	const total = chat.unread.lobby + chat.unread.table;
	const b = $('chat-badge');
	b.textContent = total > 99 ? '99+' : total;
	b.classList.toggle('hidden', !total || chatOpen());
	$('table-badge').textContent = chat.unread.table;
	$('table-badge').classList.toggle('hidden', !chat.unread.table || chat.room === 'table');
}

function setRoom(room) {
	chat.room = room;
	document.querySelectorAll('#chat-tabs button').forEach((b) => b.classList.toggle('active', b.dataset.room === room));
	const feed = room === 'feed'; // the feed is its own tab so the results never crowd the chat
	$('feed').classList.toggle('hidden', !feed);
	$('chat-log').classList.toggle('hidden', feed);
	$('chat-form').classList.toggle('hidden', feed);
	if (feed) return badges();
	$('chat-log').dataset.room = '';
	renderChat();
}

async function sendChat(e) {
	e.preventDefault();
	const input = $('chat-input'), text = input.value.trim();
	if (!text) return;
	input.value = '';
	try { await api('/chat', { room: chat.room === 'table' ? route.table : 'lobby', text }); } catch (err) { input.value = text; }
}


// ---------- tables ----------

function renderTable() {
	if (!table || route.view !== 'table') return;
	const id = table.game === 'poker' ? 'view-pk' : 'view-bj';
	document.querySelectorAll('.view').forEach((v) => { const want = v.id === id; if (want === v.classList.contains('hidden')) v.classList.toggle('hidden', !want); });
	if (table.game === 'poker') renderPoker(); else renderBlackjack();
	placeBubbles();
}

function tableStage() {
	return $(table.game === 'poker' ? 'pk-stage' : 'bj-stage');
}

function seatPlate(name) {
	// The name plate of whoever sits at the table under this name
	const i = table ? table.seats.findIndex((s) => s && s.name === name) : -1;
	if (i < 0) return null;
	const seat = table.game === 'poker' ? $('pk-felt').querySelector(`.pk-seat[data-i="${i}"]`) : $('bj-seats').children[i];
	return seat ? seat.querySelector('.plate') : null;
}

function chatBubble(name, text) {
	// Show a table chat message next to the speaker's seat for a few seconds, longer for longer messages
	if (!table) return;
	let b = bubbles.get(name);
	if (b) clearTimeout(b.timer);
	else { b = { el: document.createElement('div') }; bubbles.set(name, b); }
	b.el.className = 'bubble' + (me && name === me.username ? ' me' : '');
	b.el.textContent = text;
	tableStage().appendChild(b.el);
	void b.el.offsetWidth;
	b.el.classList.add('pop');
	placeBubbles();
	b.timer = setTimeout(() => {
		b.el.classList.add('out');
		b.timer = setTimeout(() => { b.el.remove(); bubbles.delete(name); }, 300);
	}, Math.min(9000, 3500 + text.length * 60));
}

function placeBubbles() {
	// Put each bubble below, beside or above its seat, wherever it covers the least of the cards, chips, plates and other bubbles without leaving the stage
	if (!bubbles.size || !table) return;
	const stage = tableStage(), sr = stage.getBoundingClientRect();
	const busy = [...stage.querySelectorAll('.card, .plate, .bet-spot.has, .pk-bet, .total-pill, .bj-status, .pk-pot, .pk-result, .shoe, .hname, .dealer-btn')].map((e) => ({ r: e.getBoundingClientRect(), k: e.classList.contains('card') ? 3 : 1 })).filter((q) => q.r.width && q.r.height); // covering cards counts the most
	const area = (a, q) => Math.max(0, Math.min(a.right, q.right) - Math.max(a.left, q.left)) * Math.max(0, Math.min(a.bottom, q.bottom) - Math.max(a.top, q.top));
	for (const [name, b] of bubbles) {
		const plate = seatPlate(name);
		b.el.classList.toggle('hidden', !plate); // they stood up
		if (!plate) continue;
		const p = plate.getBoundingClientRect(), w = b.el.offsetWidth, h = b.el.offsetHeight, gap = 4, cx = p.left + p.width / 2, cy = p.top + p.height / 2;
		const below = p.bottom + gap, above = p.top - gap - h;
		let best = null;
		for (let [x, y] of [[cx - w / 2, below], [p.right + gap, cy - h / 2], [p.left - gap - w, cy - h / 2], [p.left, below], [p.right - w, below], [cx - w / 2, above], [p.left, above], [p.right - w, above]]) {
			x = Math.min(Math.max(x, sr.left + 2), sr.right - w - 2); // slid back inside the stage rather than cut off
			y = Math.min(Math.max(y, sr.top + 2), sr.bottom - h - 2);
			const r = { left: x, top: y, right: x + w, bottom: y + h };
			const cost = busy.reduce((a, q) => a + area(r, q.r) * q.k, 0);
			if (!best || cost < best.cost) best = { cost, r };
			if (!cost) break;
		}
		busy.push({ r: best.r, k: 2 });
		b.el.style.left = `${best.r.left - sr.left}px`;
		b.el.style.top  = `${best.r.top - sr.top}px`;
	}
}

function showReact(r) {
	// An emoji floats up from the player's seat; a tip flies a chip from their seat to the dealer
	const plate = seatPlate(r.name);
	if (!plate) return;
	const stage = tableStage(), sr = stage.getBoundingClientRect(), p = plate.getBoundingClientRect(), x = p.left + p.width / 2 - sr.left, y = p.top - sr.top;
	const el = document.createElement('div');
	el.style.left = `${x}px`;
	el.style.top  = `${y}px`;
	if (r.tip) {
		const to = (table.game === 'poker' ? $('pk-pot') : $('bj-dealer').parentElement).getBoundingClientRect();
		el.className = 'tip-chip';
		el.style.setProperty('--x', `${to.left + to.width / 2 - sr.left - x}px`);
		el.style.setProperty('--y', `${to.top + to.height / 2 - sr.top - y}px`);
		setTimeout(() => {
			const f = document.createElement('div');
			f.className = 'float-num';
			f.textContent = `TIP ${money(r.tip)}`;
			f.style.left = `${to.left + to.width / 2}px`;
			f.style.top  = `${to.top + to.height / 2}px`;
			$('fx').appendChild(f);
			setTimeout(() => f.remove(), 1500);
		}, 650);
	} else {
		el.className = 'react-fly';
		el.textContent = r.emoji;
	}
	stage.appendChild(el);
	setTimeout(() => el.remove(), 1700);
}

function openReact(btn) {
	const pop = $('react-pop');
	if (!pop.classList.contains('hidden')) return closeReact();
	$('react-emojis').innerHTML = bank.emojis.map((e) => `<button data-emoji="${e}">${e}</button>`).join('');
	$('react-tips').innerHTML = bank.tips.map((c) => `<button class="btn tiny" data-tip="${c}" ${me.balance < c ? 'disabled' : ''}>${money(c)}</button>`).join('');
	const r = btn.getBoundingClientRect();
	pop.style.top   = `${r.bottom + 6}px`;
	pop.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
	pop.classList.remove('hidden');
}

function closeReact() {
	$('react-pop').classList.add('hidden');
}

async function sendReact(e) {
	const b = e.target.closest('button');
	if (!b || !table) return;
	closeReact();
	try {
		if (b.dataset.emoji) await api('/table/react', { table: table.id, emoji: b.dataset.emoji });
		else { const w = await api('/table/tip', { table: table.id, amount: Number(b.dataset.tip) }); setBalance(w.balance); toast(`You tipped the dealer ${money(Number(b.dataset.tip))}`); }
	} catch (err) { /* toast shown */ }
}

function shareLink() {
	const url = `${location.origin}/#/t/${table.id}`;
	if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => toast('Invite link copied')).catch(() => toast(url));
	else toast(url);
}

async function leaveTable() {
	try {
		await api('/table/leave', { table: table.id });
		toast(table.game === 'poker' ? 'Cashed out' : 'You stood up');
	} catch (e) { /* toast shown */ }
}


// ---------- blackjack ----------

function bjLayout() {
	if (table.solo) return BJ_SPOTS.solo;
	const stage = $('bj-stage');
	return stage.clientWidth < stage.clientHeight ? BJ_SPOTS.tall : BJ_SPOTS.wide;
}

function renderBlackjack() {
	const t = table, felt = $('bj-felt'), mine = t.me !== null ? t.seats[t.me] : null;
	const prev = prevTable && prevTable.id === t.id && prevTable.game === 'blackjack' ? prevTable : null;
	dealQueue = 0;
	felt.classList.toggle('solo', t.solo);
	$('bj-name').textContent = t.name;
	$('bj-stakes').textContent = t.max_bet >= t.min_bet ? `${money(t.min_bet)}-${money(t.max_bet)}` : 'closed: the house can not cover this table right now';
	$('bj-share').classList.toggle('hidden', t.house || t.solo);
	$('bj-leave').classList.toggle('hidden', !mine);
	$('bj-react').classList.toggle('hidden', !mine);
	$('bj-shoe-left').textContent = t.shoe_left;

	// Dealer
	syncCards($('bj-dealer'), t.dealer, $('bj-shoe'));
	const dt = $('bj-dealer-total');
	dt.classList.toggle('hidden', !t.dealer.length);
	dt.textContent = t.dealer_total;
	dt.classList.toggle('bust', t.dealer_total > 21);

	// Seats
	const box = $('bj-seats'), spots = bjLayout();
	if (box.children.length !== t.seats.length || box.dataset.layout !== String(spots.length) + spots[0]) {
		box.innerHTML = '';
		box.dataset.layout = String(spots.length) + spots[0];
		t.seats.forEach((_, i) => {
			const el = document.createElement('div');
			el.className = 'bj-seat';
			el.style.left = spots[i][0] + '%';
			el.style.top  = spots[i][1] + '%';
			el.innerHTML = '<div class="bj-hands"></div><div class="bet-spot"><div class="stack"></div><span class="amt"></span></div><div class="plate"><div class="nm"></div><div class="sub"></div></div>';
			el.querySelector('.plate').onclick = () => { if (table.me === null && !table.seats[i]) api('/table/sit', { table: table.id, seat: i }).catch(() => {}); };
			box.appendChild(el);
		});
	}
	t.seats.forEach((s, i) => {
		const el = box.children[i];
		el.classList.toggle('empty', !s);
		el.classList.toggle('can', !s && t.me === null);
		el.classList.toggle('me', i === t.me);
		el.classList.toggle('turn', !!(t.active && t.active[0] === i));
		const plate = el.querySelector('.plate'), hands = el.querySelector('.bj-hands'), spot = el.querySelector('.bet-spot');
		if (!s) {
			plate.querySelector('.nm').textContent = t.me === null ? 'Sit here' : 'Open';
			plate.querySelector('.sub').textContent = '';
			hands.innerHTML = '';
			spot.classList.remove('has');
			spot.querySelector('.stack').innerHTML = '';
			spot.querySelector('.amt').textContent = '';
			plate.querySelectorAll('.timer, .ready-dot').forEach((x) => x.remove());
			return;
		}
		plate.querySelector('.nm').innerHTML = who(s.name, s.user_id);
		const sub = plate.querySelector('.sub');
		sub.className = 'sub' + (s.net > 0 ? ' pos' : s.net < 0 ? ' neg' : '');
		sub.textContent = s.net !== null ? (s.net ? (s.net > 0 ? '+' : '') + money(s.net) : 'even') : s.insurance ? `insured ${money(s.insurance)}` : t.phase === 'betting' ? (s.ready ? 'ready' : s.bet ? 'betting' : '') : '';
		const stake = s.hands.length ? s.hands.reduce((a, h) => a + h.bet, 0) : s.bet;
		spot.classList.toggle('has', !!stake);
		const stackKey = String(stake);
		if (spot.dataset.k !== stackKey) { spot.dataset.k = stackKey; spot.querySelector('.stack').innerHTML = chipStack(stake); }
		spot.querySelector('.amt').textContent = stake ? money(stake) : '';
		// One element per hand, keeping cards that are already down
		while (hands.children.length > s.hands.length) hands.lastChild.remove();
		s.hands.forEach((h, k) => {
			let he = hands.children[k];
			if (!he) { he = document.createElement('div'); he.className = 'bj-hand'; he.innerHTML = '<div class="cards"></div><span class="hand-tag"></span>'; hands.appendChild(he); }
			he.classList.toggle('active', !!(t.active && t.active[0] === i && t.active[1] === k && s.hands.length > 1));
			syncCards(he.querySelector('.cards'), h.cards, $('bj-shoe'));
			const tag = he.querySelector('.hand-tag');
			tag.className = 'hand-tag ' + (h.result || '');
			tag.textContent = h.result ? RESULT[h.result] : (h.soft && h.total < 21 ? `${h.total - 10}/${h.total}` : h.total) + (h.doubled ? ' ×2' : '');
		});
		plate.querySelectorAll('.timer, .ready-dot').forEach((x) => x.remove());
		if (t.active && t.active[0] === i && t.deadline) plate.insertAdjacentHTML('beforeend', `<div class="timer" data-deadline="${t.deadline}" data-total="${t.time_total}"></div>`);
		if (t.phase === 'betting' && s.ready) plate.insertAdjacentHTML('beforeend', '<span class="ready-dot"></span>');
	});

	// Message on the felt
	const st = $('bj-status');
	let msg = '';
	if (t.phase === 'betting') msg = t.deadline && !t.solo ? `Bets close in <span data-deadline="${t.deadline}"></span>s` : (t.seats.some((s) => s) ? 'Place your bets' : '');
	else if (t.phase === 'insurance') msg = 'Insurance?';
	else if (t.phase === 'play' && t.active) msg = t.active[0] === t.me ? 'Your turn' : `${esc(t.seats[t.active[0]].name)}'s turn`;
	else if (t.phase === 'dealer') msg = 'Dealer plays';
	else if (t.phase === 'done') msg = t.dealer_total > 21 ? 'Dealer busts' : t.dealer.length === 2 && t.dealer_total === 21 ? 'Dealer blackjack' : `Dealer has ${t.dealer_total}`;
	const key = `${t.round}-${t.phase}-${t.active}`;
	if (st.dataset.key !== key) { st.dataset.key = key; st.classList.remove('pop'); void st.offsetWidth; st.classList.add('pop'); }
	st.innerHTML = msg;

	// Effects when a round ends
	if (prev && prev.phase !== 'done' && t.phase === 'done' && mine && mine.net !== null) {
		const el = box.children[t.me];
		floatNum(el, mine.net);
		if (mine.net > 0) burst(el.querySelector('.bet-spot'), mine.hands.some((h) => h.result === 'blackjack') ? 30 : 16);
	}
	if (prev && prev.phase === 'done' && t.phase === 'betting' && mine) bjBet = Math.min(bjLast, me ? me.balance : 0, t.max_bet) >= t.min_bet ? Math.min(bjLast, t.max_bet) : 0;
	renderBjDock(prev);
}

function renderBjDock(prev) {
	const t = table, mine = t.me !== null ? t.seats[t.me] : null;
	const rows = { sit: false, bet: false, ins: false, act: false, wait: false };
	let wait = '';
	if (!mine) {
		if (t.seats.some((s) => !s)) rows.sit = true;
		else { rows.wait = true; wait = 'The table is full. Watching.'; }
	} else if (t.phase === 'betting') {
		rows.bet = true;
		if (!bjTimer && mine.bet) bjBet = mine.bet;
		bjBet = Math.min(bjBet, t.max_bet);
		renderChips();
	} else if (t.phase === 'insurance') {
		if (t.insure) { rows.ins = true; $('bj-ins-yes').textContent = `Insure ${money(Math.floor(mine.hands[0].bet / 2))}`; }
		else { rows.wait = true; wait = mine.hands.length ? 'Waiting on insurance' : 'Sitting this round out'; }
	} else if (t.phase === 'play' && t.active && t.active[0] === t.me) {
		rows.act = true;
		document.querySelectorAll('[data-bj]').forEach((b) => b.classList.toggle('hidden', !t.actions.includes(b.dataset.bj)));
	} else {
		rows.wait = true;
		if (!mine.hands.length) wait = 'Sitting this round out. Bet when the next one starts.';
		else if (t.phase === 'done' && mine.net !== null) wait = mine.net > 0 ? `You won <b>${money(mine.net)}</b>` : mine.net < 0 ? `You lost ${money(-mine.net)}` : 'Push, your bet comes back';
		else if (t.phase === 'play' && t.active) wait = `Waiting for ${esc(t.seats[t.active[0]].name)}`;
		else wait = 'Dealer is playing';
	}
	$('bj-sit-row').classList.toggle('hidden', !rows.sit);
	$('bj-bet-row').classList.toggle('hidden', !rows.bet);
	$('bj-ins-row').classList.toggle('hidden', !rows.ins);
	$('bj-act-row').classList.toggle('hidden', !rows.act);
	$('bj-wait-row').classList.toggle('hidden', !rows.wait);
	$('bj-wait').innerHTML = wait;
}

function renderChips() {
	const t = table, rack = $('bj-chips'), mine = t.seats[t.me];
	const chips = CHIPS.filter(([v]) => v >= t.min_bet && v <= t.max_bet).slice(0, 5);
	const sig = chips.map((c) => c[0]).join();
	if (rack.dataset.sig !== sig) {
		rack.dataset.sig = sig;
		rack.innerHTML = chips.map(([v, c]) => `<button class="chip" style="--c:${c}" data-v="${v}">${money(v).replace('$', '$').replace(',000', 'k')}</button>`).join('');
		rack.querySelectorAll('.chip').forEach((b) => { b.onclick = () => { bjBet = Math.min(bjBet + Number(b.dataset.v), t.max_bet); sendBet(false); }; });
	}
	const avail = (me ? me.balance : 0) + (mine ? mine.bet : 0);
	rack.querySelectorAll('.chip').forEach((b) => { b.disabled = bjBet + Number(b.dataset.v) > Math.min(avail, t.max_bet); });
	$('bj-bet').textContent = money(bjBet);
	const deal = $('bj-deal');
	deal.disabled = bjBet < t.min_bet || bjBet > avail || (mine && mine.ready && mine.bet === bjBet);
	deal.textContent = mine && mine.ready ? 'Ready ✓' : t.solo ? 'Deal' : 'Ready';
	$('bj-double-bet').disabled = !bjBet || bjBet * 2 > Math.min(avail, t.max_bet);
}

function sendBet(ready) {
	// Chip taps are batched so the table sees the bet without a request per tap
	clearTimeout(bjTimer);
	renderChips();
	const go = () => {
		bjTimer = null;
		if (ready) bjLast = bjBet;
		api('/bj/bet', { table: table.id, amount: bjBet, ready }).then((v) => { prevTable = table; table = v; renderTable(); }).catch(() => { const m = table.seats[table.me]; bjBet = m ? m.bet : 0; renderChips(); });
	};
	if (ready) go(); else bjTimer = setTimeout(go, 350);
}

async function bjAct(action) {
	try { const v = await api('/bj/act', { table: table.id, action }); prevTable = table; table = v; renderTable(); } catch (e) { /* toast shown */ }
}

async function bjInsure(yes) {
	try { const v = await api('/bj/insure', { table: table.id, yes }); prevTable = table; table = v; renderTable(); } catch (e) { /* toast shown */ }
}

function bjRules() {
	openModal(`<h3>Blackjack rules</h3><ul>${(table ? table.rules : []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
		<p class="note">Everyone at the table plays their own hands against the dealer, in seat order from the left. Once the first bet is down there are 12 seconds to get in. Each hand has 20 seconds before it stands on its own.</p>`);
}


// ---------- poker ----------

function renderPoker() {
	const t = table, h = t.hand, felt = $('pk-felt'), stage = $('pk-stage');
	const prev = prevTable && prevTable.id === t.id && prevTable.game === 'poker' ? prevTable : null;
	dealQueue = 0;
	$('pk-name').textContent = t.name;
	$('pk-stakes').textContent = `${money(t.sb)}/${money(t.bb)}`;
	$('pk-share').classList.toggle('hidden', t.house);
	$('pk-leave').classList.toggle('hidden', t.me === null);
	$('pk-react').classList.toggle('hidden', t.me === null);
	const spots = stage.clientWidth < stage.clientHeight ? PK_SPOTS.tall : PK_SPOTS.wide;
	const base  = t.me !== null ? t.me : 0;
	const center = felt.querySelector('.pk-center');
	const newHand = !prev || !prev.hand || !h || prev.hand.no !== h.no;
	if (newHand) felt.querySelectorAll('.pk-seat, .pk-bet').forEach((e) => e.remove());
	const bets = new Set();
	const winners = new Set(h && h.street === 'done' ? h.results.flatMap((r) => r.seats) : []);
	t.seats.forEach((s, i) => {
		const [x, y] = spots[(i - base + 6) % 6];
		let el = felt.querySelector(`.pk-seat[data-i="${i}"]`);
		if (!el) { el = document.createElement('div'); el.className = 'pk-seat'; el.dataset.i = i; el.innerHTML = '<div class="hole"></div><div class="plate"></div>'; felt.appendChild(el); }
		el.style.left = x + '%';
		el.style.top  = y + '%';
		const plate = el.querySelector('.plate'), hole = el.querySelector('.hole');
		el.className = 'pk-seat';
		if (!s) {
			const can = t.me === null;
			el.classList.add('empty');
			if (can) el.classList.add('can');
			if (pkSeat === i) el.classList.add('picked');
			plate.innerHTML = can ? (pkSeat === i ? 'Your seat' : 'Sit here') : 'Empty';
			plate.onclick = can ? () => { pkSeat = i; prevTable = table; renderPoker(); } : null;
			hole.innerHTML = '';
			return;
		}
		plate.onclick = null;
		if (i === t.me) el.classList.add('me');
		if (h && h.to_act === i) el.classList.add('turn');
		if (s.folded || (h && h.street !== 'done' && !('cards' in s))) el.classList.add('out');
		if (s.away) el.classList.add('away');
		if (winners.has(i)) el.classList.add('winner');
		syncCards(hole, s.cards || [], center);
		hole.querySelectorAll('.card').forEach((c) => c.classList.toggle('win', winners.has(i) && !!s.hand_name));
		plate.innerHTML = `<div class="nm">${who(s.name, s.user_id)}</div><div class="st">${s.allin && h && h.street !== 'done' ? 'ALL IN' : money(s.stack)}</div><div class="said">${esc(s.said || '')}</div>${h && h.to_act === i && h.deadline ? `<div class="timer" data-deadline="${h.deadline}" data-total="${t.act_time}"></div>` : ''}`;
		el.querySelectorAll('.dealer-btn, .hname').forEach((x) => x.remove());
		if (t.button === i && h) el.insertAdjacentHTML('beforeend', '<div class="dealer-btn">D</div>');
		if (s.hand_name) el.insertAdjacentHTML('beforeend', `<div class="hname">${esc(s.hand_name)}</div>`);
		if (s.bet) { // keep the chip that is already there so it only animates in once
			let b = felt.querySelector(`.pk-bet[data-i="${i}"]`);
			if (!b) { b = document.createElement('div'); b.className = 'pk-bet'; b.dataset.i = i; felt.appendChild(b); }
			b.style.left = (x + (50 - x) * 0.4) + '%';
			b.style.top  = (y + (50 - y) * 0.42) + '%';
			if (b.textContent !== money(s.bet)) b.textContent = money(s.bet);
			bets.add(String(i));
		}
	});
	felt.querySelectorAll('.pk-bet').forEach((b) => { if (!bets.has(b.dataset.i)) b.remove(); });
	const board = $('pk-board');
	if (newHand) board.innerHTML = '';
	syncCards(board, h ? h.board : [], center);
	$('pk-pot').textContent = h && h.pot ? `POT ${money(h.pot)}` : '';
	const res = $('pk-result');
	const text = h && h.street === 'done' ? h.results.map((r) => `${r.seats.map((s) => t.seats[s] ? t.seats[s].name : '?').join(' & ')} ${r.seats.length > 1 ? 'split' : 'wins'} ${money(r.amount)}${r.hand ? ' · ' + r.hand : ''}`).join('  |  ') : !h ? pkWaitText() : '';
	if (res.textContent !== text) { res.textContent = text; res.classList.remove('pop'); void res.offsetWidth; if (text && h) res.classList.add('pop'); }
	if (prev && prev.hand && prev.hand.street !== 'done' && h && h.street === 'done' && t.me !== null && winners.has(t.me)) {
		const el = felt.querySelector(`.pk-seat[data-i="${t.me}"]`);
		burst(el, 22);
		const won = h.results.filter((r) => r.seats.includes(t.me)).reduce((a, r) => a + Math.floor(r.amount / r.seats.length), 0);
		floatNum(el, won);
	}
	renderPkDock();
}

function pkWaitText() {
	const t = table, people = t.seats.filter((s) => s && !s.bot).length;
	if (t.me === null) return people ? 'Pick a seat and buy in' : 'Sit down to start a game';
	if (!t.bots && people < 2) return 'Waiting for another player';
	return 'Next hand starting';
}

function renderPkDock() {
	const t = table, h = t.hand, mine = t.me !== null ? t.seats[t.me] : null;
	$('pk-sit-row').classList.toggle('hidden', !!mine);
	$('pk-away-row').classList.toggle('hidden', !mine || !mine.away);
	$('pk-play').classList.toggle('hidden', !mine || mine.away);
	if (!mine) {
		const r = $('pk-buyin'), max = Math.min(t.max_buy, me ? me.balance : 0);
		r.min = t.min_buy; r.max = Math.max(t.min_buy, max); r.step = t.bb;
		if (r.dataset.table !== t.id) { r.value = Math.max(t.min_buy, Math.min(max, t.bb * 100)); r.dataset.table = t.id; }
		$('pk-buyin-val').textContent = money(Number(r.value));
		const broke = !me || me.balance < t.min_buy;
		$('pk-sit').disabled = broke || !t.seats.some((s) => !s);
		$('pk-sit').textContent = broke ? `Need ${money(t.min_buy)}` : t.seats.some((s) => !s) ? 'Sit down' : 'Table full';
		return;
	}
	const mc = $('pk-mycards');
	syncCards(mc, mine.cards && mine.cards.length ? mine.cards : [], $('pk-felt').querySelector('.pk-center'));
	mc.classList.toggle('folded', !!mine.folded);
	$('pk-myhand').textContent = t.my_hand || (mine.folded ? 'Folded' : '');
	const opt = h && h.options;
	$('pk-actions').classList.toggle('hidden', !opt);
	$('pk-wait').classList.toggle('hidden', !!opt);
	if (!opt) {
		$('pk-wait').innerHTML = !h || h.street === 'done' ? pkWaitText() : h.to_act !== null && t.seats[h.to_act] ? `${esc(t.seats[h.to_act].name)} to act` : '';
		return;
	}
	const r = $('pk-raise');
	$('pk-call').textContent = opt.check ? 'Check' : `Call ${money(opt.call)}`;
	$('pk-fold').classList.toggle('hidden', opt.check);
	$('pk-raise-btn').disabled = !opt.raise;
	r.disabled = !opt.raise;
	document.querySelectorAll('[data-preset]').forEach((b) => { b.disabled = !opt.raise; });
	const key = `${h.no}-${h.street}-${opt.bet}`;
	if (r.dataset.key !== key) {
		r.dataset.key = key;
		r.min = opt.min_to; r.max = opt.max_to; r.step = t.sb;
		setRaise(opt.min_to);
		if (pkBuzz !== key) { pkBuzz = key; if (navigator.vibrate) navigator.vibrate(50); }
	}
}

function setRaise(to) {
	const opt = table.hand.options;
	to = Math.max(opt.min_to, Math.min(opt.max_to, Math.round(to)));
	$('pk-raise').value = to;
	$('pk-raise-btn').textContent = to === opt.max_to ? `All in ${money(to)}` : `${opt.bet ? 'Raise' : 'Bet'} ${money(to)}`;
	$('pk-raise-btn').dataset.to = to;
}

async function pkAct(action, to) {
	try { const v = await api('/poker/act', { table: table.id, action, to }); prevTable = table; table = v; renderTable(); } catch (e) { /* toast shown */ }
}

async function pkSit() {
	try { const v = await api('/table/sit', { table: table.id, buyin: Number($('pk-buyin').value), seat: pkSeat }); prevTable = table; table = v; renderTable(); } catch (e) { /* toast shown */ }
}

function pkHelp() {
	const hands = [['Royal flush', 'As Ks Qs Js Ts'], ['Straight flush', '9h 8h 7h 6h 5h'], ['Four of a kind', 'Qc Qd Qh Qs 2d'], ['Full house', 'Kd Kh Ks 4c 4h'], ['Flush', 'Ad Jd 8d 6d 2d'], ['Straight', 'Tc 9d 8h 7s 6c'], ['Three of a kind', '7c 7d 7h Kc 2s'], ['Two pair', 'Jh Js 5c 5d Ac'], ['Pair', 'Th Tc 8s 4d 2c'], ['High card', 'Ac Qd 9h 6s 3c']];
	openModal(`<h3>Hand rankings</h3><div class="ranking">${hands.map(([n, c]) => `<div class="cards">${c.split(' ').map((x) => cardEl(x).outerHTML).join('')}</div><span>${n}</span>`).join('')}</div>
		<p class="note">No-limit Texas Hold'em. Blinds move around the table each hand; the minimum raise is the size of the last raise. You have ${table ? table.act_time : 25} seconds to act. Time out twice and you sit out. Leaving the table cashes your stack back to your balance.</p>`);
}


// ---------- slots ----------

async function loadSlots() {
	if (slotInfo) return renderSlotBet();
	slotInfo = await api('/slots/info');
	slotBet  = Math.max(0, slotInfo.bets.indexOf(Number(localStorage.getItem('acidcasino_slotbet'))));
	const reels = $('reels');
	for (let r = 0; r < 5; r++) {
		const reel = document.createElement('div');
		reel.className = 'reel';
		reel.innerHTML = `<div class="strip">${[0, 1, 2].map((row) => `<div class="sym">${SYM_HTML[slotInfo.strips[r][row]]}</div>`).join('')}</div>`;
		reels.appendChild(reel);
	}
	renderSlotBet();
}

function slotMax() {
	// The biggest bet the house can cover right now: what a spin could win must fit in its cover
	const cover = lobby.house && lobby.house.cover !== undefined ? lobby.house.cover : Infinity;
	let i = slotInfo.bets.length - 1;
	while (i > 0 && slotInfo.bets[i] * slotInfo.worst > cover) i--;
	return i;
}

function renderSlotBet() {
	const top = slotMax();
	if (slotBet > top) slotBet = top;
	$('slot-bet-note').textContent = top < slotInfo.bets.length - 1 ? `house limit ${money(slotInfo.bets[top])}` : '';
	const bet = slotInfo.bets[slotBet];
	$('slot-bet').textContent = money(bet);
	const btn = $('slot-spin');
	btn.disabled = !autoLeft && (spinning || !me || me.balance < bet);
	btn.classList.toggle('auto-on', autoLeft > 0);
	$('slot-spin-label').textContent = autoLeft ? 'STOP' : 'SPIN';
	$('slot-spin-sub').textContent = autoLeft ? `${autoLeft} left` : '';
	document.querySelectorAll('[data-auto]').forEach((b) => { b.disabled = spinning || autoLeft > 0 || !me || me.balance < bet; });
	$('slot-less').disabled = slotBet === 0 || autoLeft > 0;
	$('slot-more').disabled = slotBet >= top || autoLeft > 0;
}

async function autoSpin(n) {
	// Pull the handle n times in a row, stopping early if the chips run out or STOP is pressed
	if (spinning || autoLeft) return;
	autoLeft = n;
	while (autoLeft > 0 && me && me.balance >= slotInfo.bets[slotBet]) {
		renderSlotBet();
		await spin(true);
		if (autoLeft > 0) autoLeft--;
		if (autoLeft > 0) await new Promise((ok) => setTimeout(ok, 700));
	}
	autoLeft = 0;
	renderSlotBet();
}

async function spin(auto) {
	if (!auto && autoLeft) { autoLeft = 0; renderSlotBet(); return; } // the button is STOP during an auto run
	if (spinning || !slotInfo) return;
	const bet = slotInfo.bets[slotBet];
	spinning = true;
	renderSlotBet();
	clearInterval(winCycle);
	$('lines-svg').innerHTML = '';
	$('line-tags').innerHTML = '';
	$('reels').classList.remove('dimmed');
	$('reels').querySelectorAll('.sym.hit').forEach((e) => e.classList.remove('hit'));
	$('slot-msg').textContent = '';
	$('slot-win').textContent = '$0';
	$('machine').querySelector('.win-meter').classList.remove('big');
	let res;
	try { res = await api('/slots/spin', { bet }); } catch (e) { spinning = false; autoLeft = 0; renderSlotBet(); return; }
	setBalance(res.balance - res.payout);
	const reels = [...$('reels').children];
	await Promise.all(reels.map((reel, r) => new Promise((done) => {
		// Build a strip: the new symbols on top, a run of random ones, the old symbols at the bottom, then slide it down
		const strip = reel.querySelector('.strip'), oldSyms = [...strip.children].slice(0, 3).map((e) => e.innerHTML);
		const run = 14 + r * 5, s = slotInfo.strips[r];
		const syms = res.grid[r].map((x) => SYM_HTML[x]).concat(Array.from({ length: run }, () => SYM_HTML[s[Math.floor(Math.random() * s.length)]]), oldSyms);
		strip.innerHTML = syms.map((x) => `<div class="sym">${x}</div>`).join('');
		const cell = reel.clientHeight / 3;
		strip.style.transition = 'none';
		strip.style.transform = `translateY(${-(syms.length - 3) * cell}px)`;
		void strip.offsetWidth;
		strip.style.transition = `transform ${0.85 + r * 0.22}s cubic-bezier(0.25, 0.1, 0.25, 1.08)`;
		strip.style.transform = 'translateY(0)';
		strip.style.filter = 'blur(1.2px)';
		setTimeout(() => { strip.style.filter = ''; }, (0.65 + r * 0.22) * 1000);
		strip.addEventListener('transitionend', () => { strip.style.transition = 'none'; [...strip.children].slice(3).forEach((e) => e.remove()); done(); }, { once: true });
	})));
	if (res.wins.length) {
		showWins(res);
		countUp($('slot-win'), res.payout);
		if (res.payout >= bet * 10) { $('machine').querySelector('.win-meter').classList.add('big'); burst($('reels'), 40); }
		else if (res.payout > bet) burst($('slot-win'), 14);
	} else {
		$('slot-msg').textContent = 'No win';
	}
	setBalance(res.balance);
	me.balance = res.balance;
	spinning = false;
	renderSlotBet();
}

function winCells(res, w) {
	// The reel/row cells that made a win: the run from the left on a line, or every scatter on screen
	if (w.line === null) return res.grid.flatMap((col, r) => col.map((x, row) => x === 'S' ? [r, row] : null)).filter(Boolean);
	return slotInfo.lines[w.line].slice(0, w.count).map((row, r) => [r, row]);
}

function winText(res, w, only) {
	// only: this is the spin's single win, so the meter already shows the amount
	const pay = only ? '' : ` · <b>${money(w.pay)}</b>`;
	if (w.line === null) return `${w.count} × ⭐ Scatter anywhere${pay}`;
	const cells = winCells(res, w), sym = cells.map(([r, row]) => res.grid[r][row]).find((x) => x !== 'W') || 'W';
	return `<i style="color:${LINE_COLORS[w.line]}">Line ${w.line + 1}</i> · ${w.count} × ${slotInfo.symbols[sym]}${sym !== 'W' && cells.some(([r, row]) => res.grid[r][row] === 'W') ? ' (with wild)' : ''}${pay}`;
}

function showWins(res) {
	// Show every win together, then step through them one at a time so each line and its pay can be read
	const svg = $('lines-svg'), reels = [...$('reels').children];
	const show = (list, faint) => {
		const hit = new Set(list.flatMap((w) => winCells(res, w).map(([r, row]) => `${r}-${row}`)));
		reels.forEach((reel, r) => [...reel.querySelector('.strip').children].forEach((el, row) => el.classList.toggle('hit', hit.has(`${r}-${row}`))));
		svg.innerHTML = list.filter((w) => w.line !== null).map((w, i) => {
			const line = slotInfo.lines[w.line], pt = (row, r) => `${50 + r * 100},${50 + row * 100}`, c = LINE_COLORS[w.line];
			return `<polyline class="ghost" style="stroke:${c}" points="${line.map(pt).join(' ')}"/><polyline style="stroke:${c};color:${c};animation-delay:${i * 0.12}s" points="${line.slice(0, w.count).map(pt).join(' ')}"/>`;
		}).join('');
		const rows = {}; // the number of each line goes in the gutter beside where it starts, stacked when lines share a row
		$('line-tags').innerHTML = list.filter((w) => w.line !== null).map((w) => {
			const row = slotInfo.lines[w.line][0], k = rows[row] = (rows[row] || 0) + 1;
			return `<span style="top:calc(${(row + 0.5) * 33.334}% + ${(k - 1) * 1.6}em);background:${LINE_COLORS[w.line]}">${w.line + 1}</span>`;
		}).join('');
		$('reels').classList.add('dimmed');
	};
	const wins = res.wins, msg = $('slot-msg');
	show(wins);
	if (wins.length === 1) { msg.innerHTML = winText(res, wins[0], true); return; }
	msg.innerHTML = `${wins.length} wins: ${wins.map((w) => w.line === null ? 'scatter' : `line ${w.line + 1}`).join(', ')}`;
	let i = 0;
	winCycle = setInterval(() => {
		const w = wins[i++ % wins.length];
		show([w]);
		msg.innerHTML = winText(res, w);
	}, 1800);
}

function countUp(el, cents) {
	const start = performance.now(), dur = Math.min(1500, 400 + cents / 50);
	const step = (t) => { const k = Math.min(1, (t - start) / dur); el.textContent = money(cents * k); if (k < 1) requestAnimationFrame(step); };
	requestAnimationFrame(step);
}

function slotPays() {
	const p = slotInfo.pays, sp = slotInfo.scatter_pays, bet = slotInfo.bets[slotBet], unit = bet / slotInfo.lines.length;
	const rows = ['W', '7', 'R', 'B', 'P', 'O', 'L', 'C'].map((k) => `<tr><td><span class="sym">${SYM_HTML[k]}</span></td>${p[k].map((x) => `<td>${money(x * unit)}</td>`).join('')}</tr>`).join('');
	const maps = slotInfo.lines.map((line, i) => `<div class="line-map" style="--c:${LINE_COLORS[i]}"><b>${i + 1}</b><div>${[0, 1, 2].map((row) => [0, 1, 2, 3, 4].map((r) => `<span class="${line[r] === row ? 'on' : ''}"></span>`).join('')).join('')}</div></div>`).join('');
	openModal(`<h3>Pays on a ${money(bet)} spin</h3>
		<table class="pays"><tr><th></th><th>3 in a row</th><th>4 in a row</th><th>5 in a row</th></tr>${rows}<tr><td><span class="sym">⭐</span></td>${[3, 4, 5].map((n) => `<td>${money(sp[n] * bet)}</td>`).join('')}</tr></table>
		<p class="note">Your bet is split over ${slotInfo.lines.length} lines <b>(${money(unit)} each)</b>. A line pays when the same symbol runs from the <b>leftmost reel</b> along it, 3 or more in a row. Every line that hits pays, so one spin can win on several. <b>WILD</b> stands in for any symbol except ⭐, and 3 or more wilds on their own pay the wild prize. ⭐ <b>Scatters</b> pay anywhere on the screen, on top of any line wins. Return to player: <b>${slotInfo.rtp}%</b>.</p>
		<h4>The ${slotInfo.lines.length} lines</h4>
		<div class="line-maps">${maps}</div>`);
}


// ---------- wiring ----------

document.querySelectorAll('#auth-seg button').forEach((b) => { b.onclick = () => setAuthMode(b.dataset.mode); });
$('auth-form').onsubmit = submitAuth;
$('captcha-img').onclick = loadCaptcha;
$('btn-wallet').onclick = openWallet;
$('btn-account').onclick = openAccount;
$('modal-close').onclick = closeModal;
$('modal-bg').onclick = (e) => { if (e.target === $('modal-bg')) closeModal(); };
$('btn-chat').onclick = () => { $('chat').classList.add('open'); chat.unread[chat.room] = 0; renderChat(); setTimeout(() => $('chat-input').focus({ preventScroll: true }), 300); };
$('chat-close').onclick = () => { $('chat').classList.remove('open'); badges(); };
document.querySelectorAll('#chat-tabs button').forEach((b) => { b.onclick = () => { chat.unread[b.dataset.room] = 0; setRoom(b.dataset.room); }; });
$('chat-form').onsubmit = sendChat;
document.addEventListener('keydown', (e) => {
	if (e.key === 'Escape') { closeModal(); closeReact(); $('chat').classList.remove('open'); }
	if (route.view === 'slots' && e.code === 'Space' && $('modal-bg').classList.contains('hidden') && document.activeElement.tagName !== 'INPUT') { e.preventDefault(); spin(false); }
});

document.querySelectorAll('#table-filter button').forEach((b) => { b.onclick = () => { filter = b.dataset.filter; document.querySelectorAll('#table-filter button').forEach((x) => x.classList.toggle('active', x === b)); $('table-list').dataset.sig = ''; renderLobby(); }; });
document.querySelectorAll('#side-tabs button').forEach((b) => {
	b.onclick = () => {
		sideTab = b.dataset.side;
		document.querySelectorAll('#side-tabs button').forEach((x) => x.classList.toggle('active', x === b));
		['ranks', 'fame'].forEach((id) => $(id).classList.toggle('hidden', id !== sideTab));
		loadRanks().catch(() => {});
	};
});
document.querySelectorAll('[data-filter-go]').forEach((b) => { b.onclick = () => document.querySelector(`#table-filter [data-filter="${b.dataset.filterGo}"]`).click(); });
document.querySelectorAll('[data-quick]').forEach((b) => { b.onclick = () => openCreate(b.dataset.quick === 'bj-solo' ? { game: 'blackjack', mode: 'solo' } : { game: 'poker', mode: 'private', bots: true }); });
$('btn-create').onclick = () => openCreate();
$('code-form').onsubmit = (e) => { e.preventDefault(); const code = $('code-input').value.trim().toLowerCase(); if (code) location.hash = `#/t/${code}`; $('code-input').value = ''; };
document.querySelectorAll('.tile-art [data-c]').forEach((el) => paintCard(el, el.dataset.c));
[['c1', 'As'], ['c2', 'Kd'], ['c3', 'Js']].forEach(([cls, c]) => paintCard(document.querySelector('.card.hero.' + cls), c));

$('bj-sit').onclick = () => api('/table/sit', { table: table.id }).catch(() => {});
$('bj-leave').onclick = leaveTable;
$('bj-share').onclick = shareLink;
$('bj-help').onclick = bjRules;
$('bj-clear').onclick = () => { bjBet = 0; sendBet(false); };
$('bj-double-bet').onclick = () => { bjBet = Math.min(bjBet * 2, table.max_bet); sendBet(false); };
$('bj-deal').onclick = () => sendBet(true);
$('bj-ins-yes').onclick = () => bjInsure(true);
$('bj-ins-no').onclick = () => bjInsure(false);
document.querySelectorAll('[data-bj]').forEach((b) => { b.onclick = () => bjAct(b.dataset.bj); });

$('bj-react').onclick = () => openReact($('bj-react'));
$('pk-react').onclick = () => openReact($('pk-react'));
$('react-pop').onclick = sendReact;
document.addEventListener('pointerdown', (e) => { if (!e.target.closest('#react-pop, #bj-react, #pk-react')) closeReact(); });

$('pk-sit').onclick = pkSit;
$('pk-leave').onclick = leaveTable;
$('pk-share').onclick = shareLink;
$('pk-help').onclick = pkHelp;
$('pk-buyin').oninput = () => { $('pk-buyin-val').textContent = money(Number($('pk-buyin').value)); };
$('pk-back-in').onclick = () => api('/poker/back', { table: table.id }).catch(() => {});
$('pk-fold').onclick = () => pkAct('fold');
$('pk-call').onclick = () => pkAct(table.hand.options.check ? 'check' : 'call');
$('pk-raise-btn').onclick = () => { const to = Number($('pk-raise-btn').dataset.to); pkAct(to === table.hand.options.max_to ? 'allin' : 'raise', to); };
$('pk-raise').oninput = () => setRaise(Number($('pk-raise').value));
document.querySelectorAll('[data-preset]').forEach((b) => {
	b.onclick = () => {
		const h = table.hand, opt = h.options;
		setRaise(b.dataset.preset === 'all' ? opt.max_to : opt.bet + (h.pot + opt.call) * Number(b.dataset.preset));
	};
});

$('slot-less').onclick = () => { slotBet = Math.max(0, slotBet - 1); localStorage.setItem('acidcasino_slotbet', slotInfo.bets[slotBet]); renderSlotBet(); };
$('slot-more').onclick = () => { slotBet = Math.min(slotMax(), slotBet + 1); localStorage.setItem('acidcasino_slotbet', slotInfo.bets[slotBet]); renderSlotBet(); };
$('slot-spin').onclick = () => spin(false);
document.querySelectorAll('[data-auto]').forEach((b) => { b.onclick = () => autoSpin(Number(b.dataset.auto)); });
$('slot-pays').onclick = slotPays;

window.addEventListener('hashchange', go);
let resizeTimer = null;
window.addEventListener('resize', () => {
	clearTimeout(resizeTimer);
	resizeTimer = setTimeout(() => {
		if (table) { $('bj-seats').dataset.layout = ''; prevTable = table; renderTable(); }
		closeReact();
		badges();
	}, 120);
});
setInterval(() => { if (me && route.view === 'lobby') loadRanks().catch(() => {}); }, 30000);
requestAnimationFrame(timers);
loadMe().catch(() => {});
