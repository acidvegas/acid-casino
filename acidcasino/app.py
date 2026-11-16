#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/app.py

import functools
import json
import os
import queue
import threading
import time

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import arcade
import blackjack
import chat
import hub
import poker
import slots
from auth import USERNAME_RE, captcha_create, captcha_verify, create_session, delete_session, mirror_user, request_token, request_user, session_user
from storage import CREDIT_TIERS, DB_LOCK, DEBT_CAP, HOUSE_START, LATE_AFTER, LATE_COST, LOAN_AGE, LOAN_FREEZE, LOAN_MIN, LOAN_WAGERED, MAXED_COST, PAID_FAST, PAID_OK, REFILL_AMOUNT, REFILL_UNDER, REFILL_WAIT, REPAY_SHARE, SCORE_RANGE, START_BALANCE, TIPS, accrue, borrow, db_connect, delete_user, fame, house_balance, init_db, leaderboard, money, prune_sessions, refill_request, refund_bets, rename_user, repay, signup_allowed, summaries, tip, wallet


app = Flask(__name__)

CACHE_VERSION = int(time.time())
PROXIES       = int(os.environ.get('CASINO_PROXIES', '0')) # reverse proxies in front of the app, so the real client IP is used
PUBLIC_URL    = os.environ.get('CASINO_PUBLIC_URL', '').rstrip('/') # main public address for canonical and share links, e.g. https://casino.acid.vegas
OWN_MAX       = 3    # tables one player can have open at once
IP_TABLES     = 5    # player made tables one IP address can have open at once, so alternate accounts can not flood the lobby
IP_STREAMS    = 20   # event streams one IP address can have open at once
STREAMS       = {}   # IP address -> open event streams
SPINS         = {}   # user id -> times of their spins in the last second
SPIN_MAX      = 4    # slot spins one player can make a second, faster than the reels animate
PING_EVERY    = 15   # seconds between keep-alive comments on an idle event stream
STATS_EVERY   = 30   # seconds between pushes of a player's summary to acid-arcade-db
CHANGES_EVERY = 60   # seconds between polls of acid-arcade-db for renamed and deleted accounts
STATS_SENT    = {}   # user id -> (main, stats) last pushed to acid-arcade-db
PRESENT_EVERY = 30   # seconds between reports of who has the casino open to acid-arcade-db

if PROXIES:
	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXIES, x_proto=PROXIES, x_host=PROXIES)


def api_error(message: str, status: int = 400) -> tuple:
	'''Return a JSON error response.'''

	return jsonify({'error': message}), status


def arcade_sync():
	'''Background thread: pick up renamed and deleted accounts from acid-arcade-db. Deleted players are stood up first and removed once their seats are empty.'''

	since = 0
	gone  = set() # deleted accounts still waiting on a seat
	while True:
		try:
			news  = arcade.changes(since)
			since = news['now']
			names = {u['user_id']: u['username'] for u in news['changed']}
			with DB_LOCK, db_connect() as c:
				for user_id, username in names.items():
					rename_user(c, user_id, username)
			with hub.LOCK:
				for room in chat.ROOMS.values():
					for m in room:
						if m.get('user_id') in names:
							m['name'] = names[m['user_id']]
			gone.update(news['deleted'])
		except Exception as e:
			print(f'acid-arcade-db sync error: {e!r}')
		if gone:
			with hub.LOCK:
				for t in list(hub.TABLES.values()):
					for i, s in enumerate(t.seats):
						if s and s.get('user_id') in gone and not s.get('left'):
							(poker if t.game == 'poker' else blackjack).leave(t, i)
				seated = {s.get('user_id') for t in hub.TABLES.values() for s in t.seats if s}
				for user_id in gone - seated:
					with DB_LOCK, db_connect() as c:
						delete_user(c, user_id)
					gone.discard(user_id)
					STATS_SENT.pop(user_id, None)
			hub.notify()
		time.sleep(CHANGES_EVERY)


def house_tick() -> bool:
	'''Charge interest, refresh the house and the richest player, and record anyone who just toppled the house. Returns True on a change.'''

	with DB_LOCK, db_connect() as c:
		accrue(c)
		bank = house_balance(c)
		top  = leaderboard(c, 1)
		top  = top[0] if top else None
		won  = top and top['worth'] > bank and not c.execute('SELECT 1 FROM topplers WHERE user_id=?', (top['id'],)).fetchone()
		if won:
			c.execute('INSERT INTO topplers(user_id, username, worth, house, at) VALUES(?, ?, ?, ?, ?)', (top['id'], top['username'], top['worth'], bank, int(time.time())))
			if arcade.enabled():
				threading.Thread(target=stats_push, args=(summaries(c, top['id'])[0],), daemon=True).start()
			chat.event(top['id'], 'record', 'toppled the house')
		crowns = [r['username'] for r in c.execute('SELECT COALESCE(u.username, t.username) AS username FROM topplers t LEFT JOIN users u ON u.id = t.user_id ORDER BY t.at')]
	if won:
		chat.add('lobby', None, 'casino', f'{top["username"]} TOPPLED THE HOUSE with {money(top["worth"])} against the house\'s {money(bank)}')
	new = {'balance': bank, 'start': HOUSE_START, 'top': top and {'name': top['username'], 'worth': top['worth']}, 'crowns': crowns, 'cover': max(0, bank) // hub.COVER_SHARE, 'lending': bank >= LOAN_FREEZE}
	if new == hub.HOUSE:
		return False
	hub.HOUSE.update(new)
	return True


def lobby_view(user_id: int) -> dict:
	'''Return the open tables a player can see and how many people are online.'''

	tables = []
	for t in hub.TABLES.values():
		mine = bool(user_id) and any(s and s['user_id'] == user_id for s in t.seats)
		if t.private and not mine and t.owner != user_id:
			continue
		tables.append({
			'id'     : t.id,
			'game'   : t.game,
			'name'   : t.name,
			'label'  : t.label,
			'stakes' : f'{money(t.sb)}/{money(t.bb)}' if t.game == 'poker' else f'{money(t.min_bet)}-{money(t.max_bet)}' if t.max_bet >= t.min_bet else 'house can not cover',
			'seats'  : len(t.seats),
			'taken'  : sum(1 for s in t.seats if s),
			'people' : [s['name'] for s in t.seats if s and s.get('user_id')],
			'ids'    : [s['user_id'] for s in t.seats if s and s.get('user_id')],
			'bots'   : sum(1 for s in t.seats if s and not s.get('user_id')),
			'house'  : t.house,
			'private': t.private,
			'solo'   : getattr(t, 'solo', False),
			'mine'   : mine,
		})
	order = {'blackjack': 0, 'poker': 1}
	tables.sort(key=lambda x: (not x['mine'], not x['house'], order[x['game']], x['id']))
	return {'tables': tables, 'online': hub.online(), 'house': hub.HOUSE}


def login_required(fn):
	'''Only let signed in players through, passing their user id and name.'''

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		user_id, username = request_user()
		if not user_id:
			return api_error('sign in to play', 401)
		return fn(user_id, username, *args, **kwargs)
	return wrapper


def my_seat(game: str, user_id: int) -> tuple:
	'''Return (table, seat) for the table named in the request, when the player sits there.'''

	table = hub.TABLES.get(payload().get('table') or '')
	if not table or table.game != game:
		return None, None
	return table, next((i for i, s in enumerate(table.seats) if s and s.get('user_id') == user_id), None)


def payload() -> dict:
	'''Return the JSON body of the request.'''

	return request.get_json(silent=True) or {}


def presence_loop():
	'''Background thread: tell acid-arcade-db which signed in players have the casino open, even when nobody does.'''

	while True:
		try:
			now = time.time()
			with hub.LOCK:
				ids = {u for u in hub.SUBS.values() if u} | {u for u, at in hub.SEEN.items() if now - at < 60}
			arcade.presence(sorted(ids))
		except Exception as e:
			print(f'presence error: {e!r}')
		time.sleep(PRESENT_EVERY)


def public_root() -> str:
	'''Return the public address with a trailing slash: CASINO_PUBLIC_URL when set, otherwise the request host.'''

	return PUBLIC_URL + '/' if PUBLIC_URL else request.url_root


def reply(table, user_id: int, error: str = None) -> tuple:
	'''Answer a table action with the new table view, or the error, and wake the watchers.'''

	if error:
		return api_error(error)
	hub.notify()
	return jsonify(table.view(user_id))


def setup():
	'''Open the database, give back chips from before a restart and open the house tables.'''

	init_db()
	prune_sessions()
	poker.cash_out_all()
	refund_bets()
	chat.load()
	for key, label, *_ in blackjack.STAKES:
		hub.TABLES[f'bj-{key}'] = blackjack.Table(f'bj-{key}', f'{label} Blackjack', key, house=True)
	for key, label, *_ in poker.STAKES:
		hub.TABLES[f'pk-{key}'] = poker.Table(f'pk-{key}', f'{label} Hold\'em', key, house=True)
	house_tick()
	hub.start(house_tick)
	if arcade.enabled():
		threading.Thread(target=arcade_sync, daemon=True).start()
		threading.Thread(target=stats_loop, daemon=True).start()
		threading.Thread(target=presence_loop, daemon=True).start()


def stats_loop():
	'''Background thread: push the summary of every player whose chips, debt or score moved, at most once every STATS_EVERY seconds each.'''

	while True:
		try:
			with db_connect() as c:
				rows = summaries(c)
			for row in rows:
				stats_push(row)
		except Exception as e:
			print(f'stats loop error: {e!r}')
		time.sleep(STATS_EVERY)


def stats_push(row: dict):
	'''Send a player's summary to acid-arcade-db unless it is unchanged, printing any error so play never notices.

	:param row: one row from storage.summaries()
	'''

	main  = row['worth']
	stats = {'Net worth': row['worth'] / 100, 'Chips': row['chips'] / 100, 'Loans': row['owed'] / 100, 'Credit score': row['score'], 'Toppled the house': int(row['toppled'])}
	if STATS_SENT.get(row['id']) == (main, stats):
		return
	try:
		arcade.push_stats(row['id'], main, stats)
		STATS_SENT[row['id']] = (main, stats)
	except Exception as e:
		print(f'stats push for {row["id"]} failed: {e!r}')


@app.route('/')
def index():
	'''Serve the page.'''

	url = public_root()
	ld  = {
		'@context'           : 'https://schema.org',
		'@type'              : ['VideoGame', 'WebApplication'],
		'name'               : 'acid casino',
		'url'                : url,
		'description'        : 'Multiplayer blackjack, no-limit Texas Hold\'em and slots with play chips. Every account starts with $1,000. Real casino rules, no real money.',
		'image'              : url + 'static/og.png',
		'applicationCategory': 'GameApplication',
		'operatingSystem'    : 'Any',
		'genre'              : ['Casino', 'Card game', 'Blackjack', 'Poker', 'Slots'],
		'gamePlatform'       : 'Web browser',
		'playMode'           : ['SinglePlayer', 'MultiPlayer'],
		'isAccessibleForFree': True,
		'offers'             : {'@type': 'Offer', 'price': '0', 'priceCurrency': 'USD'},
		'author'             : {'@type': 'Person', 'name': 'acidvegas', 'url': 'https://github.com/acidvegas'},
	}
	return render_template('index.html', cache_v=CACHE_VERSION, url=url, ld=json.dumps(ld).replace('</', '<\\/'))


@app.errorhandler(arcade.ArcadeDown)
def arcade_down(_: Exception) -> tuple:
	'''Fail hard when acid-arcade-db can not be reached.'''

	return api_error('account server is down', 503)


@app.errorhandler(arcade.ArcadeError)
def arcade_error(e: arcade.ArcadeError) -> tuple:
	'''Pass on an error from acid-arcade-db with its status.'''

	return api_error(str(e), e.status)


@app.route('/auth/captcha')
def auth_captcha():
	'''Return a new captcha.'''

	return jsonify(arcade.captcha() if arcade.enabled() else captcha_create())


@app.route('/auth/login', methods=['POST'])
def auth_login():
	'''Log in with username and password.'''

	data = payload()
	if arcade.enabled():
		user = arcade.login(data.get('username') or '', data.get('password') or '', request.remote_addr)
		mirror_user(user['user_id'], user['username'])
		resp = jsonify({'token': user['token'], 'username': user['username']})
		arcade.set_cookie(resp, request.host, user['token'])
		return resp
	with db_connect() as c:
		row = c.execute('SELECT id, username, password_hash FROM users WHERE username=?', (data.get('username') or '',)).fetchone()
	if not row or not check_password_hash(row['password_hash'], data.get('password') or ''):
		return api_error('wrong username or password', 401)
	return jsonify({'token': create_session(row['id']), 'username': row['username']})


@app.route('/auth/logout', methods=['POST'])
def auth_logout():
	'''Log out the current session.'''

	resp = jsonify({'ok': True})
	if arcade.enabled():
		arcade.logout(request_token())
		arcade.clear_cookie(resp, request.host)
	else:
		delete_session(request_token())
	return resp


@app.route('/auth/me')
def auth_me():
	'''Return the signed in player's wallet, if any.'''

	user_id, _ = request_user()
	resp       = jsonify({'user': wallet(user_id) if user_id else None, 'arcade_public_url': arcade.public_url(request.host) if arcade.enabled() else None, 'start': START_BALANCE, 'refill': REFILL_AMOUNT, 'refill_under': REFILL_UNDER, 'refill_wait': REFILL_WAIT, 'loan_min': LOAN_MIN, 'loan_age': LOAN_AGE, 'loan_wagered': LOAN_WAGERED, 'loan_freeze': LOAN_FREEZE, 'debt_cap': DEBT_CAP, 'tiers': CREDIT_TIERS, 'score_range': SCORE_RANGE, 'paid_fast': PAID_FAST, 'paid_ok': PAID_OK, 'late_after': LATE_AFTER, 'late_cost': LATE_COST, 'maxed_cost': MAXED_COST, 'repay_share': REPAY_SHARE, 'emojis': chat.EMOJIS, 'tips': TIPS})
	if arcade.enabled() and user_id and not request.cookies.get(arcade.COOKIE): # signed in before the sign-in was shared, so start sharing it
		arcade.set_cookie(resp, request.host, request_token())
	return resp


@app.route('/auth/signup', methods=['POST'])
def auth_signup():
	'''Create an account with the starting chips.'''

	data     = payload()
	username = (data.get('username') or '').strip()
	password = data.get('password') or ''
	if arcade.enabled():
		with db_connect() as c:
			allowed = signup_allowed(c, request.remote_addr)
		if not allowed:
			return api_error('only one new account per connection a day')
		user = arcade.signup(username, password, data.get('captcha_id') or '', data.get('captcha') or '', request.remote_addr)
		mirror_user(user['user_id'], user['username'])
		resp = jsonify({'token': user['token'], 'username': user['username']})
		arcade.set_cookie(resp, request.host, user['token'])
		return resp
	if not captcha_verify(data.get('captcha_id'), data.get('captcha')):
		return api_error('wrong captcha')
	if not USERNAME_RE.match(username):
		return api_error('username must be 3-16 letters, numbers or _')
	if len(password) < 8 or len(password) > 128:
		return api_error('password must be 8-128 characters')
	with DB_LOCK, db_connect() as c:
		if c.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
			return api_error('username taken')
		if not signup_allowed(c, request.remote_addr):
			return api_error('only one new account per connection a day')
		user_id = c.execute('INSERT INTO users(username, password_hash, created_at, balance, signup_ip) VALUES(?, ?, ?, ?, ?)', (username, generate_password_hash(password), int(time.time()), START_BALANCE, request.remote_addr)).lastrowid
		c.execute('INSERT INTO ledger(user_id, amount, balance, game, note, at) VALUES(?, ?, ?, ?, ?, ?)', (user_id, START_BALANCE, START_BALANCE, 'bank', 'welcome chips', int(time.time())))
	return jsonify({'token': create_session(user_id), 'username': username})


@app.route('/auth/sso')
def auth_sso():
	'''Swap the shared sign-in cookie set by any acid-arcade site on this domain for a session here.'''

	if not arcade.enabled():
		abort(404)
	user = arcade.sso(request.cookies.get(arcade.COOKIE))
	if not user:
		resp = jsonify({'error': 'not signed in'})
		arcade.clear_cookie(resp, request.host)
		return resp, 401
	mirror_user(user['user_id'], user['username'])
	return jsonify({'token': user['token'], 'username': user['username']})


@app.route('/bj/act', methods=['POST'])
@login_required
def bj_act(user_id: int, _: str):
	'''Hit, stand, double, split or surrender.'''

	with hub.LOCK:
		table, seat = my_seat('blackjack', user_id)
		if seat is None:
			return api_error('you are not at this table')
		return reply(table, user_id, blackjack.act(table, seat, payload().get('action')))


@app.route('/bj/bet', methods=['POST'])
@login_required
def bj_bet(user_id: int, _: str):
	'''Set a bet for the next round and say whether to deal.'''

	data = payload()
	with hub.LOCK:
		table, seat = my_seat('blackjack', user_id)
		if seat is None:
			return api_error('you are not at this table')
		return reply(table, user_id, blackjack.bet(table, seat, data.get('amount'), bool(data.get('ready'))))


@app.route('/bj/insure', methods=['POST'])
@login_required
def bj_insure(user_id: int, _: str):
	'''Take or turn down insurance.'''

	with hub.LOCK:
		table, seat = my_seat('blackjack', user_id)
		if seat is None:
			return api_error('you are not at this table')
		return reply(table, user_id, blackjack.insure(table, seat, bool(payload().get('yes'))))


@app.route('/chat', methods=['POST'])
@login_required
def chat_post(user_id: int, username: str):
	'''Post to the lobby or a table chat.'''

	data  = payload()
	error = chat.post(str(data.get('room') or 'lobby'), user_id, username, data.get('text'))
	return api_error(error) if error else jsonify({'ok': True})


@app.route('/events')
def events():
	'''Server-Sent Events: the lobby, the chat, the player's wallet and the table they are looking at.'''

	user_id = session_user(request.args.get('t') or '')[0]
	tid     = request.args.get('table') or ''
	ip      = request.remote_addr
	q       = queue.Queue(maxsize=8)
	with hub.LOCK:
		if STREAMS.get(ip, 0) >= IP_STREAMS:
			return api_error(f'you already have {IP_STREAMS} connections open, close some tabs first', 429)
		STREAMS[ip] = STREAMS.get(ip, 0) + 1
		hub.SUBS[q] = user_id
		if user_id:
			hub.SEEN[user_id] = time.time()
	hub.notify()

	def stream():
		last  = {}
		seen  = {'lobby': 0, 'table': 0, 'feed': 0, 'react': chat.REACT_NEXT[0]} # reactions from before the page opened are never replayed
		wrote = time.time()
		try:
			yield ': ' + (' ' * 2048) + '\n\nretry: 3000\n\n'
			while True:
				out = []
				with hub.LOCK:
					lobby = json.dumps(lobby_view(user_id))
					table = hub.TABLES.get(tid) if tid else None
					tview = json.dumps(table.view(user_id)) if table else 'null'
					lmsgs = chat.since('lobby', seen['lobby'])
					tmsgs = chat.since(tid, seen['table']) if table else []
					react = chat.reacts_since(tid, seen['react']) if table else []
					feed  = chat.feed_since(seen['feed'])
				mine = json.dumps(wallet(user_id)) if user_id else 'null'
				for name, data in (('lobby', lobby), ('me', mine)) + ((('table', tview),) if tid else ()):
					if last.get(name) != data:
						last[name] = data
						out.append(f'event: {name}\ndata: {{"now": {time.time()}, "data": {data}}}\n\n')
				for room, msgs in (('lobby', lmsgs), ('table', tmsgs)):
					if msgs:
						seen[room] = msgs[-1]['id']
						out.append(f'event: chat\ndata: {json.dumps({"room": room, "messages": msgs})}\n\n')
				if feed:
					seen['feed'] = feed[-1]['id']
					out.append(f'event: feed\ndata: {json.dumps(feed)}\n\n')
				if react:
					seen['react'] = react[-1]['id']
					out.append(f'event: react\ndata: {json.dumps(react)}\n\n')
				if out:
					wrote = time.time()
					yield ''.join(out)
				elif time.time() - wrote > PING_EVERY:
					wrote = time.time()
					yield ': ping\n\n'
				try:
					q.get(timeout=2)
				except queue.Empty:
					pass
		finally:
			with hub.LOCK:
				hub.SUBS.pop(q, None)
				if user_id:
					hub.SEEN[user_id] = time.time()
			hub.notify()

	def release():
		with hub.LOCK:
			STREAMS[ip] -= 1
			if not STREAMS[ip]:
				del STREAMS[ip]

	response = Response(stream(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache, no-transform, no-store', 'X-Accel-Buffering': 'no'})
	response.call_on_close(release) # runs once however the stream ends, even if it never started or errored out
	return response


@app.route('/favicon.ico')
def favicon():
	'''Serve the PNG icon for clients that only look for /favicon.ico.'''

	return send_from_directory(app.static_folder, 'icon-192.png', mimetype='image/png')


@app.route('/leaderboard')
def leaders():
	'''Return the richest players and everyone who has toppled the house.'''

	with db_connect() as c:
		rows = [{'id': r['id'], 'username': r['username'], 'worth': r['worth']} for r in leaderboard(c)]
	return jsonify({'rows': rows, 'fame': fame()})


@app.route('/loan', methods=['POST'])
@login_required
def loan(user_id: int, _: str):
	'''Borrow chips from the house right away.'''

	with hub.LOCK:
		shared = any(t.game == 'poker' and any(st and st['user_id'] == user_id for st in t.seats) and sum(1 for st in t.seats if st and st['user_id']) > 1 for t in hub.TABLES.values())
	if shared:
		return api_error('stand up from the poker table with other people before borrowing')
	error = borrow(user_id, payload().get('amount'))
	hub.notify()
	return api_error(error) if error else jsonify(wallet(user_id))


@app.after_request
def no_cache(response: Response) -> Response:
	'''Never cache API answers.'''

	if request.path != '/' and not request.path.startswith('/static/'):
		response.headers['Cache-Control'] = 'no-store'
	return response


@app.route('/options')
def options():
	'''Return the stakes on offer for creating tables.'''

	return jsonify({
		'blackjack': [{'id': k, 'label': n, 'text': f'{money(lo)}-{money(hi)}'} for k, n, lo, hi in blackjack.STAKES],
		'poker'    : [{'id': k, 'label': n, 'text': f'{money(sb)}/{money(bb)}', 'buyin': f'{money(bb * poker.BUY_MIN)}-{money(bb * poker.BUY_MAX)}'} for k, n, sb, bb in poker.STAKES],
	})


@app.route('/poker/act', methods=['POST'])
@login_required
def poker_act(user_id: int, _: str):
	'''Fold, check, call, bet, raise or go all in.'''

	data = payload()
	with hub.LOCK:
		table, seat = my_seat('poker', user_id)
		if seat is None:
			return api_error('you are not at this table')
		try:
			to = int(data.get('to') or 0)
		except (TypeError, ValueError):
			return api_error('bad amount')
		return reply(table, user_id, poker.act(table, seat, data.get('action'), to))


@app.route('/poker/back', methods=['POST'])
@login_required
def poker_back(user_id: int, _: str):
	'''Come back after sitting out.'''

	with hub.LOCK:
		table, seat = my_seat('poker', user_id)
		if seat is None:
			return api_error('you are not at this table')
		table.seats[seat]['out_since'] = None
		table.seats[seat]['timeouts']  = 0
		return reply(table, user_id)


@app.route('/refill', methods=['POST'])
@login_required
def refill(user_id: int, _: str):
	'''Ask for the free refill that lands a day later.'''

	error = refill_request(user_id)
	hub.notify()
	return api_error(error) if error else jsonify(wallet(user_id))


@app.route('/repay', methods=['POST'])
@login_required
def repay_debt(user_id: int, _: str):
	'''Pay debt back to the house.'''

	error = repay(user_id, payload().get('amount'))
	hub.notify()
	return api_error(error) if error else jsonify(wallet(user_id))


@app.route('/robots.txt')
def robots():
	'''Let crawlers in and point them at the sitemap.'''

	return Response(f'User-agent: *\nAllow: /\nSitemap: {public_root()}sitemap.xml\n', mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap():
	'''List the home page, the only page there is.'''

	return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{public_root()}</loc></url></urlset>\n', mimetype='application/xml')


@app.route('/slots/info')
def slots_info():
	'''Return the pay table, lines and reel strips.'''

	return jsonify(slots.info())


@app.route('/slots/spin', methods=['POST'])
@login_required
def slots_spin(user_id: int, username: str):
	'''Spin the reels.'''

	now   = time.time()
	spins = [t for t in SPINS.get(user_id, []) if t > now - 1]
	if len(spins) >= SPIN_MAX:
		return api_error('slow down a little', 429)
	SPINS[user_id] = spins + [now]
	result = slots.spin(user_id, username, payload().get('bet'))
	return api_error(result['error']) if 'error' in result else jsonify(result)


@app.route('/table/leave', methods=['POST'])
@login_required
def table_leave(user_id: int, _: str):
	'''Stand up from a table, taking poker chips back.'''

	with hub.LOCK:
		table = hub.TABLES.get(payload().get('table') or '')
		seat  = next((i for i, s in enumerate(table.seats) if s and s.get('user_id') == user_id), None) if table else None
		if seat is None:
			return api_error('you are not at this table')
		(poker if table.game == 'poker' else blackjack).leave(table, seat)
		return reply(table, user_id)


@app.route('/table/react', methods=['POST'])
@login_required
def table_react(user_id: int, username: str):
	'''Send an emoji reaction from your seat.'''

	data  = payload()
	emoji = data.get('emoji')
	with hub.LOCK:
		table = hub.TABLES.get(data.get('table') or '')
		if not table or not any(s and s.get('user_id') == user_id for s in table.seats):
			return api_error('sit at the table to react')
	if emoji not in chat.EMOJIS:
		return api_error('pick a reaction')
	if chat.react_throttled(user_id):
		return api_error('slow down a little')
	chat.react(table.id, username, emoji=emoji)
	return jsonify({'ok': True})


@app.route('/table/sit', methods=['POST'])
@login_required
def table_sit(user_id: int, username: str):
	'''Sit at a table, buying in at poker.'''

	data = payload()
	with hub.LOCK:
		table = hub.TABLES.get(data.get('table') or '')
		if not table:
			return api_error('that table is closed')
		if table.game == 'poker':
			error = poker.join(table, user_id, username, data.get('buyin'), data.get('seat'))
		else:
			error = blackjack.sit(table, user_id, username, data.get('seat'))
		return reply(table, user_id, error)


@app.route('/table/tip', methods=['POST'])
@login_required
def table_tip(user_id: int, username: str):
	'''Tip the dealer from your balance. The tip goes to the house.'''

	data   = payload()
	amount = data.get('amount')
	with hub.LOCK:
		table = hub.TABLES.get(data.get('table') or '')
		if not table or not any(s and s.get('user_id') == user_id for s in table.seats):
			return api_error('sit at the table to tip the dealer')
		if chat.react_throttled(user_id):
			return api_error('slow down a little')
		error = tip(user_id, amount, table.game)
		if error:
			return api_error(error)
		chat.add(table.id, None, 'casino', f'{username} tipped the dealer {money(amount)}')
	chat.react(table.id, username, tip=amount)
	return jsonify(wallet(user_id))


@app.route('/tables/create', methods=['POST'])
@login_required
def tables_create(user_id: int, username: str):
	'''Open a new table: blackjack or poker, any stakes, public or private, with or without bots.'''

	data    = payload()
	game    = data.get('game')
	stakes  = data.get('stakes')
	private = bool(data.get('private'))
	with hub.LOCK:
		if sum(1 for t in hub.TABLES.values() if t.owner == user_id) >= OWN_MAX:
			return api_error(f'you can have {OWN_MAX} tables open at once')
		if sum(1 for t in hub.TABLES.values() if getattr(t, 'creator_ip', None) == request.remote_addr) >= IP_TABLES:
			return api_error(f'you already have {IP_TABLES} open tables, finish or leave one first', 429)
		tid = hub.new_code()
		if game == 'blackjack' and stakes in {s[0] for s in blackjack.STAKES}:
			solo  = bool(data.get('solo'))
			table = blackjack.Table(tid, 'Solo blackjack' if solo else f'{username}\'s table', stakes, private=private or solo, owner=user_id, solo=solo)
		elif game == 'poker' and stakes in {s[0] for s in poker.STAKES}:
			table = poker.Table(tid, f'{username}\'s table', stakes, private=private, owner=user_id, bots=bool(data.get('bots', True)))
		else:
			return api_error('pick a game and stakes')
		table.empty_since = time.time()
		table.creator_ip  = request.remote_addr
		hub.TABLES[tid]   = table
	hub.notify()
	return jsonify({'id': tid})


@app.route('/wallet')
@login_required
def wallet_view(user_id: int, _: str):
	'''Return the balance, debt, loan and refill state.'''

	return jsonify(wallet(user_id))


setup()



if __name__ == '__main__':
	app.run(host='127.0.0.1', port=20005, threaded=True)
