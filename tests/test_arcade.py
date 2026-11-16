#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# tests/test_arcade.py

import json
import os
import re
import sys
import tempfile
import time
import types
import unittest

from unittest import mock

from werkzeug.security import generate_password_hash

os.environ['CASINO_PROXIES'] = '1' # behind nginx, so X-Forwarded-For is the player
os.environ.setdefault('CASINO_DATA_DIR', tempfile.mkdtemp(prefix='acidcasino-test-'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'acidcasino'))

import hub

hub.start = lambda every: None # no table clock thread, so tests can drive everything by hand

import app
import arcade
import auth
import blackjack
import chat
import poker
import roulette
import slots
import storage


class SyncThread:
	'''Stand-in for threading.Thread that runs the target right away, so background sends can be checked.'''

	def __init__(self, target, args=(), daemon=True):
		self.run = lambda: target(*args)

	def start(self):
		self.run()


SYNC = types.SimpleNamespace(Thread=SyncThread)


class StopLoop(Exception):
	'''Raised by the fake sleep to end a background loop after one pass.'''


def one_pass(news: dict):
	'''Run one pass of the acid-arcade-db sync loop against a canned changes reply.'''

	def stop(_):
		raise StopLoop()

	with mock.patch.object(arcade, 'changes', return_value=news), mock.patch.object(app, 'time', types.SimpleNamespace(time=time.time, sleep=stop)):
		try:
			app.arcade_sync()
		except StopLoop:
			pass


def rewards_pass(pending: list, ack_error: Exception = None) -> mock.Mock:
	'''Run one pass of the arcade rewards loop against a canned pending reply. Returns the rewards_ack mock.'''

	def stop(_):
		raise StopLoop()

	with mock.patch.object(arcade, 'rewards_pending', return_value=pending), mock.patch.object(arcade, 'rewards_ack', side_effect=ack_error) as ack, mock.patch.object(app, 'time', types.SimpleNamespace(time=time.time, sleep=stop)):
		try:
			app.rewards_loop()
		except StopLoop:
			pass
	return ack


def cookies(res) -> list:
	'''Return the Set-Cookie headers of a response.'''

	return res.headers.getlist('Set-Cookie')


class TestArcade(unittest.TestCase):
	def setUp(self):
		self.client  = app.app.test_client()
		self.enabled = mock.patch.object(arcade, 'enabled', return_value=True)
		self.enabled.start()
		self.addCleanup(self.enabled.stop)
		self.ips = mock.patch.object(auth, 'signup_allowed', return_value=True) # every test client shares one IP; test_welcome_once_per_ip checks the real rule
		self.ips.start()
		self.addCleanup(self.ips.stop)

	def session_as(self, user_id: int, username: str):
		'''Make every token resolve to this account.'''

		patch = mock.patch.object(arcade, 'session', return_value={'user_id': user_id, 'username': username, 'admin': False})
		patch.start()
		self.addCleanup(patch.stop)

	def test_bigwin_event(self):
		with mock.patch.object(arcade, 'event') as event, mock.patch.object(chat, 'threading', SYNC):
			chat.announce('lucky', chat.BIG_WIN, 'blackjack', 42)
			chat.announce('lucky', chat.BIG_WIN - 1, 'poker', 42)
			chat.announce('lucky', -chat.BIG_WIN, 'poker', 42)
			chat.announce('bot', chat.BIG_WIN * 2, 'poker', None)
		event.assert_called_once_with(42, 'bigwin', 'won $100,000 at blackjack')

	def test_bigwin_from_slots(self):
		with storage.DB_LOCK, storage.db_connect() as c:
			user_id = c.execute('INSERT INTO users(username, password_hash, created_at, balance) VALUES(?, ?, 0, ?)', ('spinner', '', 100_000)).lastrowid
		with mock.patch.object(arcade, 'event') as event, mock.patch.object(chat, 'threading', SYNC), mock.patch.object(slots, 'line_pay', return_value=(1_000, 5)), mock.patch.object(hub, 'cover', return_value=10 ** 15):
			result = slots.spin(user_id, 'spinner', 100_000)
		net = result['payout'] - 100_000
		event.assert_called_once_with(user_id, 'bigwin', f'won {storage.money(net)} at slots')
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, user_id)

	def test_captcha_proxied(self):
		with mock.patch.object(arcade, 'captcha', return_value={'captcha_id': 'c1', 'svg': '<svg/>'}):
			self.assertEqual(self.client.get('/auth/captcha').get_json(), {'captcha_id': 'c1', 'svg': '<svg/>'})

	def test_delete_removes_rows_and_seat(self):
		self.session_as(9002, 'doomed')
		self.client.get('/wallet', headers={'X-Auth-Token': 't'})
		chat.add('lobby', 9002, 'doomed', 'bye')
		t = blackjack.Table('tdel', 'Test', 'low', solo=True)
		hub.TABLES['tdel'] = t
		blackjack.sit(t, 9002, 'doomed')
		self.assertIsNone(blackjack.bet(t, 0, 1_000, False))
		one_pass({'now': 1, 'changed': [], 'deleted': [9002]})
		self.assertIsNone(t.seats[0])
		with storage.db_connect() as c:
			for table, col in (('users', 'id'), ('ledger', 'user_id'), ('chat', 'user_id'), ('bj_bets', 'user_id')):
				self.assertIsNone(c.execute(f'SELECT 1 FROM {table} WHERE {col}=9002').fetchone(), table)
		hub.TABLES.pop('tdel', None)

	def test_down_is_503(self):
		with mock.patch.object(arcade, 'session', side_effect=arcade.ArcadeDown('refused')):
			res = self.client.get('/wallet', headers={'X-Auth-Token': 't'})
		self.assertEqual(res.status_code, 503)
		self.assertEqual(res.get_json(), {'error': 'account server is down'})

	def test_error_passed_on(self):
		with mock.patch.object(arcade, 'login', side_effect=arcade.ArcadeError('wrong username or password', 401)):
			res = self.client.post('/auth/login', json={'username': 'x', 'password': 'y'})
		self.assertEqual(res.status_code, 401)
		self.assertEqual(res.get_json()['error'], 'wrong username or password')

	def test_login_mirrors_with_starting_chips(self):
		with mock.patch.object(arcade, 'login', return_value={'token': 'tok', 'user_id': 9001, 'username': 'mirrored'}) as login:
			res = self.client.post('/auth/login', json={'username': 'mirrored', 'password': 'pw'}, headers={'X-Forwarded-For': '203.0.113.7'})
		login.assert_called_once_with('mirrored', 'pw', '203.0.113.7')
		self.assertEqual(res.get_json(), {'token': 'tok', 'username': 'mirrored'})
		with storage.db_connect() as c:
			row = c.execute('SELECT username, password_hash, balance, score FROM users WHERE id=9001').fetchone()
			self.assertEqual(tuple(row), ('mirrored', '', storage.START_BALANCE, storage.SCORE_START))
			self.assertEqual(c.execute("SELECT COUNT(*) FROM ledger WHERE user_id=9001 AND note='welcome chips'").fetchone()[0], 1)
		self.session_as(9001, 'mirrored')
		self.assertEqual(self.client.get('/wallet', headers={'X-Auth-Token': 'tok'}).get_json()['balance'], storage.START_BALANCE)
		with storage.db_connect() as c:
			self.assertEqual(c.execute("SELECT COUNT(*) FROM ledger WHERE user_id=9001 AND note='welcome chips'").fetchone()[0], 1)

	def test_login_sets_cookie(self):
		with mock.patch.object(arcade, 'login', return_value={'token': 'tok', 'user_id': 9007, 'username': 'cookied'}):
			res = self.client.post('/auth/login', json={'username': 'cookied', 'password': 'pw'})
		self.assertTrue(any(c.startswith(f'{arcade.COOKIE}=tok;') for c in cookies(res)))

	def test_logout_proxied(self):
		with mock.patch.object(arcade, 'logout') as logout:
			res = self.client.post('/auth/logout', headers={'X-Auth-Token': 'tok'}, json={})
		logout.assert_called_once_with('tok')
		self.assertTrue(any(c.startswith(f'{arcade.COOKIE}=;') and 'Max-Age=0' in c for c in cookies(res)))

	def test_me_sets_cookie_when_missing(self):
		self.session_as(9008, 'sharer')
		res = self.client.get('/auth/me', headers={'X-Auth-Token': 'tok'})
		self.assertTrue(any(c.startswith(f'{arcade.COOKIE}=tok;') for c in cookies(res)))
		self.client.set_cookie(arcade.COOKIE, 'tok')
		self.assertFalse(cookies(self.client.get('/auth/me', headers={'X-Auth-Token': 'tok'})))
		self.client.delete_cookie(arcade.COOKIE)
		with mock.patch.object(arcade, 'session', return_value=None):
			self.assertFalse(cookies(self.client.get('/auth/me'))) # guests get no cookie

	def test_me_has_public_url(self):
		with mock.patch.object(arcade, 'ARCADE_PUBLIC', 'https://arcade.{domain}'):
			self.assertEqual(self.client.get('/auth/me', base_url='https://casino.supernets.org').get_json()['arcade_public_url'], 'https://arcade.supernets.org')
			self.assertEqual(self.client.get('/auth/me', base_url='https://casino.acid.vegas').get_json()['arcade_public_url'], 'https://arcade.acid.vegas')

	def test_presence(self):
		def stop(_):
			raise StopLoop()

		now = time.time()
		with mock.patch.dict(hub.SUBS, {'q1': 5, 'q2': None, 'q3': 5}, clear=True), mock.patch.dict(hub.SEEN, {7: now - 10, 8: now - 300}, clear=True), mock.patch.object(arcade, 'presence') as presence, mock.patch.object(app, 'time', types.SimpleNamespace(time=time.time, sleep=stop)):
			with self.assertRaises(StopLoop):
				app.presence_loop()
			presence.assert_called_once_with([5, 7])
			hub.SUBS.clear()
			hub.SEEN.clear()
			presence.reset_mock()
			presence.side_effect = arcade.ArcadeDown('refused') # printed, the loop carries on to its sleep
			with self.assertRaises(StopLoop):
				app.presence_loop()
			presence.assert_called_once_with([])

	def test_rate_limit_passed_on(self):
		for path, fn, body in (('/auth/login', 'login', {'username': 'x', 'password': 'y'}), ('/auth/signup', 'signup', {'username': 'x', 'password': 'password1'})):
			with mock.patch.object(arcade, fn, side_effect=arcade.ArcadeError('too many tries, wait a few minutes', 429)):
				res = self.client.post(path, json=body, headers={'X-Forwarded-For': '203.0.113.9'})
			self.assertEqual((res.status_code, res.get_json()['error']), (429, 'too many tries, wait a few minutes'))

	def test_rename_shows_everywhere(self):
		self.session_as(9003, 'oldname')
		self.client.get('/wallet', headers={'X-Auth-Token': 't'})
		chat.add('lobby', 9003, 'oldname', 'hello')
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('INSERT INTO topplers(user_id, username, worth, house, at) VALUES(9003, ?, 1, 0, 0)', ('oldname',))
		one_pass({'now': 1, 'changed': [{'user_id': 9003, 'username': 'newname'}], 'deleted': []})
		ranks = self.client.get('/leaderboard').get_json()
		with storage.db_connect() as c:
			self.assertIn('newname', [r['username'] for r in storage.leaderboard(c, 10_000)])
		self.assertIn({'user_id': 9003, 'username': 'newname'}, [{'user_id': r['user_id'], 'username': r['username']} for r in ranks['fame']])
		self.assertEqual([m['name'] for m in chat.ROOMS['lobby'] if m['user_id'] == 9003], ['newname'])
		chat.ROOMS.pop('lobby')
		chat.load()
		self.assertEqual([m['name'] for m in chat.ROOMS['lobby'] if m['user_id'] == 9003], ['newname'])
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, 9003)

	def test_signup_proxied(self):
		with mock.patch.object(arcade, 'signup', return_value={'token': 'tok', 'user_id': 9004, 'username': 'newbie'}) as signup:
			res = self.client.post('/auth/signup', json={'username': 'newbie', 'password': 'password1', 'captcha_id': 'c1', 'captcha': 'ABCDE'}, headers={'X-Forwarded-For': '203.0.113.8'})
		signup.assert_called_once_with('newbie', 'password1', 'c1', 'ABCDE', '203.0.113.8')
		self.assertEqual(res.get_json(), {'token': 'tok', 'username': 'newbie'})
		with storage.db_connect() as c:
			self.assertEqual(c.execute('SELECT balance FROM users WHERE id=9004').fetchone()['balance'], storage.START_BALANCE)
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, 9004)

	def test_stream_limit_per_ip(self):
		'''An IP address gets 20 event streams at once, and a closed stream frees its slot.'''

		self.session_as(9201, 'streamer')
		ip      = {'REMOTE_ADDR': '10.8.0.1'}
		streams = [self.client.get('/events?t=t', environ_base=ip, buffered=False) for _ in range(app.IP_STREAMS)]
		self.addCleanup(lambda: [s.close() for s in streams])
		self.assertTrue(all(s.status_code == 200 for s in streams))
		res = self.client.get('/events?t=t', environ_base=ip, buffered=False)
		self.assertEqual(res.status_code, 429)
		self.assertIn('error', res.get_json())
		other = self.client.get('/events?t=t', environ_base={'REMOTE_ADDR': '10.8.0.2'}, buffered=False)
		self.assertEqual(other.status_code, 200)
		other.close()
		streams.pop().close()
		again = self.client.get('/events?t=t', environ_base=ip, buffered=False)
		self.assertEqual(again.status_code, 200)
		streams.append(again)

	def test_welcome_once_per_ip(self):
		'''Only the first arcade account from an IP address in a day gets the welcome chips here.'''

		self.ips.stop()
		self.addCleanup(self.ips.start)
		for user_id, ip, chips in ((9101, '10.7.0.1', storage.START_BALANCE), (9102, '10.7.0.1', 0), (9103, '10.7.0.2', storage.START_BALANCE)):
			with mock.patch.object(arcade, 'session', return_value={'user_id': user_id, 'username': f'alt{user_id}', 'admin': False}):
				self.client.get('/wallet', headers={'X-Auth-Token': 't'}, environ_base={'REMOTE_ADDR': ip})
			with storage.db_connect() as c:
				self.assertEqual(c.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()['balance'], chips)

	def test_sso(self):
		with mock.patch.object(arcade, 'sso', return_value={'token': 'new', 'user_id': 9009, 'username': 'roamer'}) as sso:
			self.client.set_cookie(arcade.COOKIE, 'shared')
			res = self.client.get('/auth/sso')
			self.client.delete_cookie(arcade.COOKIE)
		sso.assert_called_once_with('shared')
		self.assertEqual(res.get_json(), {'token': 'new', 'username': 'roamer'})
		with storage.db_connect() as c:
			self.assertEqual(c.execute('SELECT balance FROM users WHERE id=9009').fetchone()['balance'], storage.START_BALANCE)
		with mock.patch.object(arcade, 'sso', return_value=None) as sso:
			res = self.client.get('/auth/sso')
		sso.assert_called_once_with(None)
		self.assertEqual(res.status_code, 401)
		self.assertTrue(any(c.startswith(f'{arcade.COOKIE}=;') for c in cookies(res)))
		with mock.patch.object(arcade, 'sso', side_effect=arcade.ArcadeDown('refused')):
			self.assertEqual(self.client.get('/auth/sso').status_code, 503)

	def test_stats_on_topple(self):
		self.session_as(9006, 'toppler')
		self.client.get('/wallet', headers={'X-Auth-Token': 't'})
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET balance=? WHERE id=9006', (10_000_000_000_000,))
		with mock.patch.object(arcade, 'push_stats') as push, mock.patch.object(arcade, 'event') as event, mock.patch.object(app, 'threading', SYNC), mock.patch.object(chat, 'threading', SYNC):
			app.house_tick()
			app.house_tick() # already in the hall of fame, so no second event
		user_id, main, stats = push.call_args.args
		self.assertEqual((user_id, main, stats['Toppled the house']), (9006, 10_000_000_000_000, 1))
		event.assert_called_once_with(9006, 'record', 'toppled the house')
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, 9006)

	def test_stats_push(self):
		self.session_as(9005, 'statsy')
		self.client.get('/wallet', headers={'X-Auth-Token': 't'})
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET owed=?, score=640 WHERE id=9005', (2_500,))
			row = storage.summaries(c, 9005)[0]
		with mock.patch.object(arcade, 'push_stats') as push:
			app.stats_push(row)
			app.stats_push(row) # unchanged, so not sent again
		push.assert_called_once_with(9005, storage.START_BALANCE - 2_500, {'Net worth': 975.0, 'Chips': 1000.0, 'Loans': 25.0, 'Credit score': 640, 'Toppled the house': 0})
		with mock.patch.object(arcade, 'push_stats', side_effect=arcade.ArcadeDown('refused')):
			app.STATS_SENT.clear()
			app.stats_push(row) # a failed push is printed, never raised
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, 9005)

	def test_spin_rate_and_table_chat(self):
		'''Slot spins are limited per second, and only players at a table (or its owner) can chat there.'''

		self.session_as(9401, 'spinner')
		auth = {'X-Auth-Token': 't'}
		codes = [self.client.post('/slots/spin', headers=auth, json={'bet': slots.BETS[0]}).status_code for _ in range(app.SPIN_MAX + 1)]
		self.assertEqual(codes, [200] * app.SPIN_MAX + [429])
		res = self.client.post('/tables/create', headers=auth, json={'game': 'blackjack', 'stakes': blackjack.STAKES[0][0]})
		tid = res.get_json()['id']
		self.addCleanup(hub.TABLES.pop, tid, None)
		self.assertEqual(self.client.post('/chat', headers=auth, json={'room': tid, 'text': 'owner here'}).status_code, 200)
		with mock.patch.object(arcade, 'session', return_value={'user_id': 9402, 'username': 'lurker', 'admin': False}):
			res = self.client.post('/chat', headers=auth, json={'room': tid, 'text': 'hi'})
		self.assertEqual(res.get_json()['error'], 'sit at the table to chat here')

	def test_closed_table_drops_chat(self):
		'''A player made table closed for sitting empty takes its chat and reactions with it.'''

		calls = []

		def sleep(_):
			calls.append(1)
			if len(calls) > 1:
				raise StopLoop()

		t = blackjack.Table('tgone', 'Gone', 'low')
		t.empty_since = time.time() - hub.CLOSE_AFTER - 1
		with mock.patch.dict(hub.TABLES, {'tgone': t}, clear=True), mock.patch.object(hub, 'time', types.SimpleNamespace(time=time.time, sleep=sleep)):
			chat.add('tgone', 1, 'someone', 'hi')
			chat.react('tgone', 'someone', chat.EMOJIS[0])
			try:
				hub.loop(lambda: False)
			except StopLoop:
				pass
			self.assertNotIn('tgone', hub.TABLES)
		self.assertNotIn('tgone', chat.ROOMS)
		self.assertNotIn('tgone', chat.REACTS)

	def test_events_skip_unchanged_wallet(self):
		'''An event stream only works out the wallet again when something it is made from changed.'''

		self.session_as(9501, 'watcher')
		with mock.patch.object(app, 'wallet', wraps=storage.wallet) as wallet:
			res = self.client.get('/events?t=t', environ_base={'REMOTE_ADDR': '10.8.1.1'}, buffered=False)
			self.addCleanup(res.close)
			chunks = res.iter_encoded()
			next(chunks) # padding
			self.assertIn(b'event: me', next(chunks))
			self.assertEqual(wallet.call_count, 1)
			chat.add('lobby', None, 'casino', 'nothing changed for the watcher')
			hub.notify()
			chunk = next(chunks)
			self.assertIn(b'nothing changed for the watcher', chunk)
			self.assertNotIn(b'event: me', chunk)
			self.assertEqual(wallet.call_count, 1)
			with storage.DB_LOCK, storage.db_connect() as c:
				storage.move(c, 9501, 100, 'bank', 'test')
			hub.notify()
			self.assertIn(b'event: me', next(chunks))
			self.assertEqual(wallet.call_count, 2)
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, 9501)

	def test_table_limit_per_ip(self):
		'''An IP address can have 5 player made tables open across accounts, other IP addresses are not held back.'''

		ip   = {'REMOTE_ADDR': '10.9.0.1'}
		made = []
		self.addCleanup(lambda: [hub.TABLES.pop(t, None) for t in made])
		for n in range(app.IP_TABLES):
			with mock.patch.object(arcade, 'session', return_value={'user_id': 9301 + n // app.OWN_MAX, 'username': f'alt{9301 + n // app.OWN_MAX}', 'admin': False}):
				res = self.client.post('/tables/create', headers={'X-Auth-Token': 't'}, json={'game': 'blackjack', 'stakes': blackjack.STAKES[0][0], 'solo': n % 2 == 0}, environ_base=ip)
			self.assertEqual(res.status_code, 200)
			made.append(res.get_json()['id'])
		with mock.patch.object(arcade, 'session', return_value={'user_id': 9303, 'username': 'alt9303', 'admin': False}):
			res = self.client.post('/tables/create', headers={'X-Auth-Token': 't'}, json={'game': 'poker', 'stakes': 'micro'}, environ_base=ip)
			self.assertEqual(res.status_code, 429)
			self.assertIn('5 open tables', res.get_json()['error'])
			res = self.client.post('/tables/create', headers={'X-Auth-Token': 't'}, json={'game': 'blackjack', 'stakes': blackjack.STAKES[0][0]}, environ_base={'REMOTE_ADDR': '10.9.0.2'})
		self.assertEqual(res.status_code, 200)
		made.append(res.get_json()['id'])
		del hub.TABLES[made.pop(0)]
		with mock.patch.object(arcade, 'session', return_value={'user_id': 9303, 'username': 'alt9303', 'admin': False}):
			res = self.client.post('/tables/create', headers={'X-Auth-Token': 't'}, json={'game': 'blackjack', 'stakes': blackjack.STAKES[0][0]}, environ_base=ip)
		self.assertEqual(res.status_code, 200)
		made.append(res.get_json()['id'])

	def test_roulette_tables(self):
		'''Both wheels have house tables in the lobby, and a player can open a solo table, sit and bet through the API.'''

		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('INSERT OR REPLACE INTO users(id, username, password_hash, created_at, balance) VALUES(9601, ?, ?, 0, 100000)', ('rlapi', 'x'))
		self.addCleanup(lambda: [hub.TABLES.pop(t, None) for t in [t for t, v in hub.TABLES.items() if v.owner == 9601]])
		house = {t['id'] for t in app.lobby_view(None)['tables'] if t['game'] == 'roulette'}
		self.assertEqual(house, {f'rl-{w}-{k}' for w in ('eu', 'am') for k, *_ in roulette.STAKES})
		with mock.patch.object(arcade, 'session', return_value={'user_id': 9601, 'username': 'rlapi', 'admin': False}):
			self.assertEqual(self.client.post('/tables/create', headers={'X-Auth-Token': 't'}, json={'game': 'roulette', 'stakes': 'low', 'wheel': 'mars'}).status_code, 400)
			tid = self.client.post('/tables/create', headers={'X-Auth-Token': 't'}, json={'game': 'roulette', 'stakes': 'low', 'wheel': 'american', 'solo': True}).get_json()['id']
			self.assertEqual(self.client.post('/table/sit', headers={'X-Auth-Token': 't'}, json={'table': tid}).status_code, 200)
			res = self.client.post('/rl/bet', headers={'X-Auth-Token': 't'}, json={'table': tid, 'bets': [{'spot': 'basket', 'amount': 500}], 'ready': False})
			self.assertEqual(res.get_json()['seats'][0]['bets'], {'basket': 500})
			self.assertEqual(self.client.post('/rl/bet', headers={'X-Auth-Token': 't'}, json={'table': tid, 'bets': [{'spot': 'basket', 'amount': 5.5}]}).status_code, 400)
			self.assertEqual(self.client.post('/table/leave', headers={'X-Auth-Token': 't'}, json={'table': tid}).status_code, 200)
		with storage.db_connect() as c:
			self.assertEqual(storage.balance(c, 9601), 100_000)



class TestRewards(unittest.TestCase):
	def setUp(self):
		self.enabled = mock.patch.object(arcade, 'enabled', return_value=True)
		self.enabled.start()
		self.addCleanup(self.enabled.stop)
		self.sent = mock.patch.object(arcade, 'activity')
		self.activity = self.sent.start()
		self.addCleanup(self.sent.stop)
		self.sync = mock.patch.object(chat, 'threading', SYNC)
		self.sync.start()
		self.addCleanup(self.sync.stop)

	def balance(self, user_id: int) -> int:
		'''Return a test account's balance.'''

		with storage.db_connect() as c:
			return storage.balance(c, user_id)

	def house(self) -> int:
		'''Return the house bankroll.'''

		with storage.db_connect() as c:
			return storage.house_balance(c)

	def make_user(self, user_id: int, ip: str = '203.0.113.50') -> int:
		'''Create a test account with $1,000 that last played from ip, removed after the test.'''

		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('INSERT INTO users(id, username, password_hash, created_at, balance, wagered) VALUES(?, ?, ?, 0, 100000, ?)', (user_id, f'rw{user_id}', 'x', storage.LOAN_WAGERED))
		hub.IPS[user_id] = ip

		def cleanup():
			hub.IPS.pop(user_id, None)
			with storage.DB_LOCK, storage.db_connect() as c:
				storage.delete_user(c, user_id)
				c.execute('DELETE FROM poker_seats WHERE user_id=?', (user_id,))

		self.addCleanup(cleanup)
		return user_id

	def sent_kinds(self) -> list:
		'''Return (user id, kind, data) for every activity sent, checking each carries the address and a unique short ref.'''

		refs = [c.args[2] for c in self.activity.call_args_list]
		self.assertEqual(len(refs), len(set(refs)))
		self.assertTrue(all(len(r) <= 64 for r in refs))
		self.assertTrue(all(c.args[3] == hub.IPS.get(c.args[0]) for c in self.activity.call_args_list))
		return [(c.args[0], c.args[1], c.args[4]) for c in self.activity.call_args_list]

	def test_activity_needs_arcade_and_address(self):
		u = self.make_user(9701)
		chat.activity(u, 'roulette_spin', 'x:1', {'won': True})
		chat.activity(None, 'poker_hand', 'x:2', {'won': True, 'showdown': False}) # a bot
		hub.IPS.pop(u)
		chat.activity(u, 'roulette_spin', 'x:3', {'won': True}) # no known address
		hub.IPS[u] = '203.0.113.50'
		with mock.patch.object(arcade, 'enabled', return_value=False):
			chat.activity(u, 'roulette_spin', 'x:4', {'won': True}) # standalone
		self.activity.assert_called_once_with(u, 'roulette_spin', 'x:1', '203.0.113.50', {'won': True})
		self.activity.side_effect = arcade.ArcadeDown('refused') # printed, play carries on
		chat.activity(u, 'roulette_spin', 'x:5', {'won': False})

	def test_address_from_requests(self):
		with mock.patch.object(arcade, 'session', return_value={'user_id': 9702, 'username': 'rwip', 'admin': False}):
			app.app.test_client().get('/wallet', headers={'X-Auth-Token': 't', 'X-Forwarded-For': '203.0.113.77'})
		self.assertEqual(hub.IPS.pop(9702), '203.0.113.77')
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, 9702)

	def test_blackjack_hands(self):
		u = self.make_user(9703)
		t = blackjack.Table('trwbj', 'Test', 'low', solo=True)
		blackjack.sit(t, u, 'rw9703')
		for player, dealer, extra, actions in ((['Ah', 'Kh'], ['Ts', '7s'], [], []), (['Th', '6h'], ['Ts', '7s'], ['9c'], ['hit']), (['Th', '9h'], ['Ts', '7s'], [], ['stand']), (['Th', '7h'], ['Ts', '7s'], [], ['stand'])):
			t.shoe = ['2c'] * 300 + list(reversed([player[0], dealer[0], player[1], dealer[1], *extra]))
			self.assertIsNone(blackjack.bet(t, 0, 1_000, True))
			for a in actions:
				self.assertIsNone(blackjack.act(t, 0, a))
			while t.phase == 'dealer':
				t.next_at = 0
				blackjack.tick(t)
			self.assertEqual(t.phase, 'done')
			t.next_at = 0
			blackjack.tick(t)
		self.assertEqual(self.sent_kinds(), [(u, 'bj_hand', {'result': r}) for r in ('blackjack', 'loss', 'win', 'push')])

	def test_client_calls(self):
		self.sent.stop()
		with mock.patch.object(arcade, 'ARCADE_GAME', 'casino'), mock.patch.object(arcade, 'call', side_effect=[{'granted': 0, 'reason': None, 'dup': False}, {'rewards': []}, {'acked': 2}]) as call:
			self.assertEqual(arcade.activity(5, 'bj_hand', 'r1', '203.0.113.1', {'result': 'win'}), {'granted': 0, 'reason': None, 'dup': False})
			self.assertEqual(arcade.rewards_pending(), [])
			self.assertEqual(arcade.rewards_ack([1, 2]), 2)
		self.sent.start()
		self.assertEqual([c.args for c in call.call_args_list], [
			('/internal/activity', {'game': 'casino', 'user_id': 5, 'kind': 'bj_hand', 'ref': 'r1', 'ip': '203.0.113.1', 'data': {'result': 'win'}}),
			('/internal/rewards/pending', {'game': 'casino'}),
			('/internal/rewards/ack', {'game': 'casino', 'ids': [1, 2]}),
		])

	def test_poker_humans_only(self):
		u = self.make_user(9704)
		hub.TABLES['trwpk'] = t = poker.Table('trwpk', 'Test', 'micro')
		self.addCleanup(hub.TABLES.pop, 'trwpk', None)
		self.assertIsNone(poker.join(t, u, 'rw9704', 4_000))
		poker.seat_bots(t)
		self.assertTrue(poker.start_hand(t))
		while t.hand['street'] != 'done':
			self.assertIsNone(poker.act(t, t.hand['to_act'], 'fold'))
		seat = next(i for i, st in enumerate(t.seats) if st and st['user_id'] == u)
		won  = t.seats[seat]['stack'] > 4_000
		self.assertEqual(self.sent_kinds(), [(u, 'poker_hand', {'won': won, 'showdown': False})])
		poker.leave(t, seat)

	def test_roulette_spins_not_slots(self):
		u = self.make_user(9705)
		t = roulette.Table('trwrl', 'Test', 'low', 'european', solo=True)
		roulette.sit(t, u, 'rw9705')
		for pocket in (17, 0):
			with mock.patch.object(roulette.RNG, 'choice', return_value=pocket):
				self.assertIsNone(roulette.place(t, 0, [{'spot': 'straight:17', 'amount': 100}], True))
			t.next_at = 0
			roulette.tick(t)
			t.next_at = 0
			roulette.tick(t)
		with mock.patch.object(hub, 'cover', return_value=10 ** 15):
			slots.spin(u, 'rw9705', 100)
		self.assertEqual(self.sent_kinds(), [(u, 'roulette_spin', {'won': True}), (u, 'roulette_spin', {'won': False})])

	def test_rewards_paid_once_from_the_house(self):
		u    = self.make_user(9706)
		bank = self.house()
		good = [{'id': 880001, 'user_id': u, 'amount': 30_000, 'note': 'Wordle solved in 3'}, {'id': 880002, 'user_id': u, 'amount': storage.REWARD_MAX, 'note': 'gold achievement'}]
		bad  = [{'id': 880003, 'user_id': u, 'amount': 0, 'note': 'x'}, {'id': 880004, 'user_id': u, 'amount': storage.REWARD_MAX + 1, 'note': 'x'}, {'id': 880005, 'user_id': u, 'amount': True, 'note': 'x'}, {'id': 880006, 'user_id': u, 'amount': 500.0, 'note': 'x'}, {'id': '880007', 'user_id': u, 'amount': 500, 'note': 'x'}, {'id': 880008, 'user_id': u, 'amount': -500, 'note': 'x'}]
		later = {'id': 880009, 'user_id': 9799, 'amount': 500, 'note': 'not in the casino yet'}
		ack  = rewards_pass(good + bad + [later])
		ack.assert_called_once_with([880001, 880002])
		total = 30_000 + storage.REWARD_MAX
		self.assertEqual((self.balance(u), self.house()), (100_000 + total, bank - total))
		with storage.db_connect() as c:
			rows = [tuple(r) for r in c.execute("SELECT amount, note FROM ledger WHERE user_id=? AND game='arcade' ORDER BY id", (u,))]
		self.assertEqual(rows, [(30_000, 'Wordle solved in 3'), (storage.REWARD_MAX, 'gold achievement')])
		self.assertEqual([r['note'] for r in storage.wallet(u)['arcade']], ['gold achievement', 'Wordle solved in 3'])
		ack = rewards_pass(good + [later], ack_error=arcade.ArcadeDown('refused')) # seen again, as if the last ack was lost: acked, never paid twice
		ack.assert_called_once_with([880001, 880002])
		ack = rewards_pass(good)
		ack.assert_called_once_with([880001, 880002])
		self.assertEqual((self.balance(u), self.house()), (100_000 + total, bank - total))
		self.make_user(9799) # the player opens the casino, so the waiting reward is paid on the next pass
		ack = rewards_pass([later])
		ack.assert_called_once_with([880009])
		self.assertEqual(self.balance(9799), 100_500)
		self.assertFalse(rewards_pass(bad).called) # nothing payable, nothing to ack
		with mock.patch.object(arcade, 'rewards_pending', side_effect=arcade.ArcadeDown('refused')), mock.patch.object(app, 'time', types.SimpleNamespace(time=time.time, sleep=mock.Mock(side_effect=StopLoop()))):
			with self.assertRaises(StopLoop): # printed, the loop carries on to its sleep
				app.rewards_loop()
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('DELETE FROM rewards_paid WHERE id BETWEEN 880001 AND 880009')

	def test_wallet_page_links_the_arcade(self):
		with open(os.path.join(os.path.dirname(app.__file__), 'static', 'utils.js')) as f:
			js = f.read()
		self.assertIn('Earn chips in the arcade', js)
		self.assertIn('id="earn-link"', app.app.test_client().get('/').get_data(as_text=True))


class TestStandalone(unittest.TestCase):
	def setUp(self):
		auth.ATTEMPTS.clear()

	def test_captcha_cap(self):
		auth.CAPTCHAS.clear()
		with mock.patch.object(auth, 'CAPTCHA_MAX', 3):
			first = [auth.captcha_create()['captcha_id'] for _ in range(5)]
		self.assertEqual(list(auth.CAPTCHAS), first[2:])
		auth.CAPTCHAS.clear()

	def test_captcha_no_text(self):
		svg = auth.captcha_render_svg(auth.CAPTCHA_CHARS)
		self.assertNotIn('<text', svg)
		self.assertIn('<path', svg)

	def test_captcha_rate_limit(self):
		client = app.app.test_client()
		ip     = {'X-Forwarded-For': '203.0.113.30'}
		for _ in range(30):
			self.assertEqual(client.get('/auth/captcha', headers=ip).status_code, 200)
		self.assertEqual(client.get('/auth/captcha', headers=ip).status_code, 429)
		self.assertEqual(client.get('/auth/captcha', headers={'X-Forwarded-For': '203.0.113.31'}).status_code, 200)

	def test_login_limits(self):
		client = app.app.test_client()
		with storage.DB_LOCK, storage.db_connect() as c:
			ids = [c.execute('INSERT INTO users(username, password_hash, created_at, balance) VALUES(?, ?, 0, ?)', (name, generate_password_hash('password1'), 100_000)).lastrowid for name in ('locked', 'other')]
		for i in range(10):
			res = client.post('/auth/login', json={'username': 'Locked', 'password': 'wrong'}, headers={'X-Forwarded-For': f'203.0.113.{40 + i}'})
			self.assertEqual(res.status_code, 401)
		res = client.post('/auth/login', json={'username': 'locked', 'password': 'password1'}, headers={'X-Forwarded-For': '203.0.113.60'})
		self.assertEqual(res.status_code, 429)
		self.assertIn('error', res.get_json())
		self.assertEqual(client.post('/auth/login', json={'username': 'other', 'password': 'password1'}, headers={'X-Forwarded-For': '203.0.113.60'}).status_code, 200)
		for i in range(30):
			client.post('/auth/login', json={'username': f'nobody{i}', 'password': 'wrong'}, headers={'X-Forwarded-For': '203.0.113.70'})
		self.assertEqual(client.post('/auth/login', json={'username': 'other', 'password': 'password1'}, headers={'X-Forwarded-For': '203.0.113.70'}).status_code, 429)
		with storage.DB_LOCK, storage.db_connect() as c:
			for user_id in ids:
				storage.delete_user(c, user_id)

	def test_no_events(self):
		with mock.patch.object(arcade, 'event') as event, mock.patch.object(chat, 'threading', SYNC):
			chat.announce('lucky', chat.BIG_WIN, 'slots', 42)
			chat.event(42, 'record', 'toppled the house')
		event.assert_not_called()

	def test_no_cookie(self):
		client = app.app.test_client()
		with storage.DB_LOCK, storage.db_connect() as c:
			user_id = c.execute('INSERT INTO users(username, password_hash, created_at, balance) VALUES(?, ?, 0, ?)', ('loner', generate_password_hash('password1'), 100_000)).lastrowid
		res = client.post('/auth/login', json={'username': 'loner', 'password': 'password1'})
		self.assertEqual(res.status_code, 200)
		token = res.get_json()['token']
		self.assertFalse(cookies(res))
		self.assertFalse(cookies(client.get('/auth/me', headers={'X-Auth-Token': token})))
		self.assertFalse(cookies(client.post('/auth/logout', headers={'X-Auth-Token': token}, json={})))
		self.assertEqual(client.get('/auth/sso').status_code, 404)
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.delete_user(c, user_id)



class TestSeo(unittest.TestCase):
	def page(self, base: str) -> tuple:
		'''Return (canonical, og:url, og:image, parsed JSON-LD) from the page as served to base.'''

		html = app.app.test_client().get('/', base_url=base).get_data(as_text=True)
		find = lambda pattern: re.search(pattern, html).group(1)
		return find(r'<link rel="canonical" href="([^"]+)">'), find(r'<meta property="og:url" content="([^"]+)">'), find(r'<meta property="og:image" content="([^"]+)">'), json.loads(find(r'<script type="application/ld\+json">(.+?)</script>'))

	def test_public_url(self):
		with mock.patch.object(app, 'PUBLIC_URL', 'https://casino.acid.vegas'):
			canonical, og_url, og_image, ld = self.page('https://casino.supernets.org')
			client = app.app.test_client()
			robots  = client.get('/robots.txt', base_url='https://casino.supernets.org').get_data(as_text=True)
			sitemap = client.get('/sitemap.xml', base_url='https://casino.supernets.org').get_data(as_text=True)
		self.assertEqual((canonical, og_url, ld['url']), ('https://casino.acid.vegas/',) * 3)
		self.assertEqual((og_image, ld['image']), ('https://casino.acid.vegas/static/og.png',) * 2)
		self.assertEqual(ld['@type'], ['VideoGame', 'WebApplication'])
		self.assertIn('Sitemap: https://casino.acid.vegas/sitemap.xml', robots)
		self.assertIn('<loc>https://casino.acid.vegas/</loc>', sitemap)

	def test_request_host(self):
		with mock.patch.object(app, 'PUBLIC_URL', ''):
			canonical, og_url, og_image, ld = self.page('https://casino.supernets.org')
		self.assertEqual((canonical, og_url, ld['url']), ('https://casino.supernets.org/',) * 3)
		self.assertEqual(og_image, 'https://casino.supernets.org/static/og.png')



if __name__ == '__main__':
	unittest.main()
