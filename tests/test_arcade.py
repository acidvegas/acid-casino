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
import blackjack
import chat
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


def cookies(res) -> list:
	'''Return the Set-Cookie headers of a response.'''

	return res.headers.getlist('Set-Cookie')


class TestArcade(unittest.TestCase):
	def setUp(self):
		self.client  = app.app.test_client()
		self.enabled = mock.patch.object(arcade, 'enabled', return_value=True)
		self.enabled.start()
		self.addCleanup(self.enabled.stop)

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



class TestStandalone(unittest.TestCase):
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
