#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# tests/test_transfers.py

import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault('CASINO_DATA_DIR', tempfile.mkdtemp(prefix='acidcasino-test-'))
os.environ['CASINO_KEY'] = 'test-key'
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'acidcasino'))

import hub

hub.start = lambda every: None # no table clock thread

import app
import storage


INTERNAL = {'environ_base': {'casino.internal': True}, 'headers': {'X-Casino-Key': 'test-key'}}


def make_user(name: str, balance: int = 500_000, owed: int = 0) -> int:
	'''Create an account with a balance and debt and return its id.'''

	with storage.DB_LOCK, storage.db_connect() as c:
		return c.execute('INSERT INTO users(username, password_hash, created_at, balance, owed) VALUES(?, ?, ?, ?, ?)', (name, '', int(time.time()), balance, owed)).lastrowid


class TestTransfers(unittest.TestCase):
	def setUp(self):
		app.CASINO_KEY = 'test-key'
		self.client = app.app.test_client()

	def debit(self, **body):
		return self.client.post('/internal/debit', json=body, **INTERNAL)

	def test_debit(self):
		u   = make_user('sender')
		r   = self.debit(user_id=u, amount=200_000, game='wallstreet', ref='ws-1')
		self.assertEqual((r.status_code, r.get_json()), (200, {'balance': 300_000}))
		r   = self.debit(user_id=u, amount=200_000, game='wallstreet', ref='ws-1') # a retry of the same transfer takes nothing more
		self.assertEqual(r.get_json(), {'balance': 300_000})
		with storage.db_connect() as c:
			self.assertEqual(c.execute('SELECT balance FROM users WHERE id=?', (u,)).fetchone()[0], 300_000)
			row = c.execute('SELECT amount, game, note FROM ledger WHERE user_id=? ORDER BY id DESC', (u,)).fetchone()
		self.assertEqual(tuple(row), (-200_000, 'wallstreet', 'sent to wallstreet'))
		self.assertEqual(self.debit(user_id=u, amount=1, game='wallstreet', ref='ws-1').status_code, 400) # same ref, different transfer

	def test_refusals(self):
		u = make_user('broke', balance=5_000)
		d = make_user('debtor', owed=1_000)
		for body, message in (
			({'user_id': u, 'amount': 10_000, 'game': 'wallstreet', 'ref': 'a'}, 'not enough casino chips'),
			({'user_id': d, 'amount': 1_000, 'game': 'wallstreet', 'ref': 'b'}, 'loan'),
			({'user_id': 999_999, 'amount': 1_000, 'game': 'wallstreet', 'ref': 'c'}, 'no casino account'),
			({'user_id': u, 'amount': storage.SEND_MAX + 1, 'game': 'wallstreet', 'ref': 'd'}, 'bad transfer'),
			({'user_id': u, 'amount': 1.5, 'game': 'wallstreet', 'ref': 'e'}, 'bad transfer'),
			({'user_id': u, 'amount': -5, 'game': 'wallstreet', 'ref': 'f'}, 'bad transfer'),
		):
			r = self.debit(**body)
			self.assertEqual(r.status_code, 400, body)
			self.assertIn(message, r.get_json()['error'])

	def test_in_play(self):
		u = make_user('gambler')
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute("INSERT INTO bj_bets(user_id, table_id, stake) VALUES(?, 'bj-x', 100)", (u,))
		r = self.debit(user_id=u, amount=1_000, game='wallstreet', ref='g')
		self.assertIn('finish your casino hands', r.get_json()['error'])
		self.assertTrue(self.client.post('/internal/chips', json={'user_id': u}, **INTERNAL).get_json()['in_play'])

	def test_chips(self):
		u = make_user('viewer', balance=1_234, owed=50)
		self.assertEqual(self.client.post('/internal/chips', json={'user_id': u}, **INTERNAL).get_json(), {'balance': 1_234, 'owed': 50, 'in_play': False})
		self.assertEqual(self.client.post('/internal/chips', json={'user_id': 999_998}, **INTERNAL).status_code, 404)

	def test_guarded(self):
		u = make_user('guarded')
		self.assertEqual(self.client.post('/internal/debit', json={'user_id': u, 'amount': 1, 'game': 'x', 'ref': 'h'}, headers={'X-Casino-Key': 'test-key'}).status_code, 404) # public port
		self.assertEqual(self.client.post('/internal/debit', json={'user_id': u, 'amount': 1, 'game': 'x', 'ref': 'i'}, environ_base={'casino.internal': True}, headers={'X-Casino-Key': 'wrong'}).status_code, 403)
		app.CASINO_KEY = ''
		self.assertEqual(self.debit(user_id=u, amount=1, game='x', ref='j').status_code, 404) # no key set, no internal routes



if __name__ == '__main__':
	unittest.main()
