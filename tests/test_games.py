#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# tests/test_games.py

import json
import os
import random
import sys
import tempfile
import time
import unittest

os.environ['CASINO_DATA_DIR'] = tempfile.mkdtemp(prefix='acidcasino-test-')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'acidcasino'))

import blackjack
import cards
import hub
import poker
import slots
import storage


def house() -> int:
	'''Return the house bankroll.'''

	with storage.db_connect() as c:
		return storage.house_balance(c)


def played(user_id: int):
	'''Mark a test account as having bet enough to borrow.'''

	with storage.DB_LOCK, storage.db_connect() as c:
		c.execute('UPDATE users SET wagered=? WHERE id=?', (storage.LOAN_WAGERED, user_id))


def make_user(name: str, balance: int = 100_000) -> int:
	'''Create a test account and return its id.'''

	with storage.DB_LOCK, storage.db_connect() as c:
		return c.execute('INSERT INTO users(username, password_hash, created_at, balance) VALUES(?, ?, 0, ?)', (name, 'x', balance)).lastrowid


def bal(user_id: int) -> int:
	'''Return a test account's balance.'''

	with storage.db_connect() as c:
		return storage.balance(c, user_id)


def rig(t, player: list, dealer: list, extra: list = ()):
	'''Stack a solo table's shoe: player cards, dealer cards, then the cards drawn after.'''

	order  = [player[0], dealer[0], player[1], dealer[1], *extra]
	t.shoe = ['2c'] * 300 + list(reversed(order))


def solo(name: str, balance: int = 100_000) -> tuple:
	'''Return (user id, solo blackjack table with them seated).'''

	u = make_user(name, balance)
	t = blackjack.Table('t' + name, 'Test', 'low', solo=True)
	blackjack.sit(t, u, name)
	return u, t


def play(t, bet: int, player: list, dealer: list, extra: list = (), actions: list = ()) -> dict:
	'''Deal a rigged round, play the actions, then let the dealer finish.'''

	rig(t, player, dealer, extra)
	assert blackjack.bet(t, 0, bet, True) is None
	for a in actions:
		if a in ('insurance', 'no_insurance'):
			assert blackjack.insure(t, 0, a == 'insurance') is None
		else:
			assert blackjack.act(t, 0, a) is None, a
	while t.phase == 'dealer':
		t.next_at = 0
		blackjack.tick(t)
	return t.view(None)


storage.init_db()


class TestCards(unittest.TestCase):
	def test_rankings(self):
		hands = [
			['As', 'Ks', 'Qs', 'Js', 'Ts', '2d', '3c'],
			['9h', '9d', '9s', '9c', 'Ah', '2d', '3c'],
			['Kh', 'Kd', 'Ks', '2c', '2h', '2d', '3c'],
			['Ah', '9h', '7h', '5h', '2h', 'Kd', 'Kc'],
			['5d', '4h', '3s', '2c', 'Ah', 'Kd', 'Kc'],
			['Qh', 'Qd', 'Qs', '7c', '2h', '3d', '4c'],
			['Jh', 'Jd', '4s', '4c', '9h', '2d', '3c'],
			['Th', 'Td', 'As', '7c', '6h', '2d', '3c'],
			['Ah', 'Jd', '9s', '7c', '5h', '3d', '2c'],
		]
		scores = [cards.evaluate(h) for h in hands]
		self.assertEqual([s[0] for s in scores], [8, 7, 6, 5, 4, 3, 2, 1, 0])
		self.assertEqual(cards.describe(scores[0]), 'Royal flush')
		self.assertEqual(cards.describe(scores[4]), 'Straight, Five high')
		self.assertEqual(cards.describe(scores[2]), 'Full house, Kings full of Twos')

	def test_kickers_and_ties(self):
		board = ['Kh', 'Kd', '8s', '7c', '2h']
		self.assertGreater(cards.evaluate(['Ac', '3d'] + board), cards.evaluate(['Qc', 'Jd'] + board))
		self.assertEqual(cards.evaluate(['3c', '4d'] + ['Ah', 'Ad', 'Ks', 'Kc', 'Qh']), cards.evaluate(['5c', '6d'] + ['Ah', 'Ad', 'Ks', 'Kc', 'Qh']))
		self.assertEqual(cards.evaluate(['As', 'Ad', 'Ac', 'Kd', 'Kc', 'Qh', 'Qd']), (6, 14, 13))
		self.assertEqual(cards.evaluate(['Jh', 'Jd', '4s', '4c', '9h', '9d', 'Ac']), (2, 11, 9, 14))


class TestBlackjack(unittest.TestCase):
	def test_win_lose_push(self):
		u, t = solo('bj1')
		v = play(t, 1_000, ['Th', '9h'], ['Ts', '7s'], actions=['stand'])
		self.assertEqual(v['seats'][0]['hands'][0]['result'], 'win')
		self.assertEqual(bal(u), 101_000)
		self.assertEqual(blackjack.bet(t, 0, 1_000, True), 'wait for the next round')
		t.next_at = 0
		blackjack.tick(t)
		v = play(t, 1_000, ['Th', '7h'], ['Ts', '7s'], actions=['stand'])
		self.assertEqual(v['seats'][0]['hands'][0]['result'], 'push')
		self.assertEqual(bal(u), 101_000)
		t.next_at = 0
		blackjack.tick(t)
		v = play(t, 1_000, ['Th', '6h'], ['Ts', '7s'], ['9c'], actions=['hit'])
		self.assertEqual(v['seats'][0]['hands'][0]['result'], 'bust')
		self.assertEqual(bal(u), 100_000)

	def test_blackjack_pays_3_to_2(self):
		u, t = solo('bj2')
		v = play(t, 1_000, ['Ah', 'Kh'], ['Ts', '7s'])
		self.assertEqual(v['seats'][0]['hands'][0]['result'], 'blackjack')
		self.assertEqual(bal(u), 101_500)

	def test_dealer_peek_and_insurance(self):
		u, t = solo('bj3')
		rig(t, ['Th', '9h'], ['As', 'Ks'])
		blackjack.bet(t, 0, 1_000, True)
		v = t.view(u)
		self.assertEqual(v['phase'], 'insurance')
		self.assertIsNone(v['dealer'][1])
		self.assertTrue(v['insure'])
		blackjack.insure(t, 0, True)
		self.assertEqual(t.phase, 'done')
		self.assertEqual(bal(u), 100_000) # lose 10, insurance 5 pays 10

	def test_double_split_surrender(self):
		u, t = solo('bj4')
		v = play(t, 1_000, ['6h', '5h'], ['Ts', '7s'], ['Tc'], actions=['double'])
		self.assertEqual(v['seats'][0]['hands'][0]['result'], 'win')
		self.assertEqual(bal(u), 102_000)
		t.next_at = 0
		blackjack.tick(t)
		v = play(t, 1_000, ['8h', '8d'], ['Ts', '7s'], ['3c', 'Tc', 'Th'], actions=['split', 'double', 'stand'])
		self.assertEqual([h['result'] for h in v['seats'][0]['hands']], ['win', 'win'])
		self.assertEqual(bal(u), 105_000) # staked 3,000 (bet, split, double), paid 6,000
		t.next_at = 0
		blackjack.tick(t)
		play(t, 1_000, ['Th', '6h'], ['Ts', '7s'], actions=['surrender'])
		self.assertEqual(bal(u), 104_500)

	def test_split_aces_one_card(self):
		u, t = solo('bj5')
		v = play(t, 1_000, ['Ah', 'Ad'], ['Ts', '7s'], ['Kc', '9c'], actions=['split'])
		self.assertEqual([h['result'] for h in v['seats'][0]['hands']], ['win', 'win']) # 21 (not blackjack) and 20 beat 17
		self.assertEqual(bal(u), 102_000)

	def test_shared_table(self):
		a, b = make_user('bjA'), make_user('bjB')
		t = blackjack.Table('shared', 'Shared', 'low')
		t.shoe = ['5c'] * 300 # no naturals, so the turn order is the same every run
		blackjack.sit(t, a, 'bjA')
		blackjack.sit(t, b, 'bjB')
		blackjack.bet(t, 0, 500, True)
		self.assertEqual(t.phase, 'betting') # waits for the other player
		blackjack.bet(t, 1, 1_000, True)
		self.assertEqual(t.phase, 'play')
		self.assertEqual(blackjack.act(t, 1, 'stand'), 'it is not your turn')
		self.assertIsNone(blackjack.act(t, 0, 'stand'))
		self.assertEqual(t.active[0], 1)
		blackjack.leave(t, 1) # leaving mid round stands the hand
		while t.phase == 'dealer':
			t.next_at = 0
			blackjack.tick(t)
		self.assertEqual(t.phase, 'done')
		self.assertIsNotNone(t.seats[1]['net'])
		t.next_at = 0
		blackjack.tick(t)
		self.assertIsNone(t.seats[1])
		with storage.db_connect() as c:
			self.assertIsNone(c.execute('SELECT 1 FROM bj_bets').fetchone())

	def test_house_takes_and_pays(self):
		u, t = solo('bj7')
		before = house()
		play(t, 1_000, ['Th', '9h'], ['Ts', '7s'], actions=['stand'])
		self.assertEqual(house(), before - 1_000)
		t.next_at = 0
		blackjack.tick(t)
		play(t, 1_000, ['Th', '6h'], ['Ts', '7s'], ['9c'], actions=['hit'])
		self.assertEqual(house(), before)


class TestBank(unittest.TestCase):
	def test_refill_is_free_and_waits(self):
		u = make_user('rf1', 25_000)
		self.assertIsNotNone(storage.refill_request(u)) # $250 is not under $250
		u = make_user('rf2', 24_999)
		before = house()
		self.assertIsNone(storage.refill_request(u))
		self.assertEqual(storage.wallet(u)['balance'], 24_999)
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE loans SET requested_at = requested_at - ? WHERE user_id=?', (storage.REFILL_WAIT, u))
		w = storage.wallet(u)
		self.assertEqual((w['balance'], w['owed']), (124_999, 0))
		self.assertEqual(house(), before)

	def test_loan_rules(self):
		u = make_user('ln0', 0)
		self.assertEqual(storage.borrow(u, 1_000), 'bet $1,000 more before you can borrow')
		played(u)
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET created_at=? WHERE id=?', (int(time.time()), u))
		self.assertEqual(storage.borrow(u, 1_000), 'accounts can borrow once they are a day old')
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET created_at=0 WHERE id=?', (u,))
			c.execute('UPDATE house SET balance=?', (storage.LOAN_FREEZE - 1,))
		self.assertIn('stopped lending', storage.borrow(u, 1_000))
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE house SET balance=?', (storage.LOAN_FREEZE,))
		self.assertIsNone(storage.borrow(u, 1_000)) # lending opens again once the house is back to $400M
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE house SET balance=?', (storage.HOUSE_START,))

	def test_credit_limit_rate_and_score(self):
		u, t = solo('ln1', 0)
		played(u)
		w = storage.wallet(u)
		self.assertEqual((w['score'], w['limit'], w['rate'], w['loan_room']), (600, 1_000_000, 0.05, 1_000_000))
		before = house()
		self.assertIsNone(storage.borrow(u, 600_000))
		self.assertEqual(storage.borrow(u, 500_000), 'your credit limit is $10,000, so you can borrow $4,000 more')
		self.assertIsNone(storage.borrow(u, 400_000)) # right up to the limit costs 15 points
		self.assertEqual(house(), before - 1_000_000)
		w = storage.wallet(u)
		self.assertEqual((w['balance'], w['owed'], w['score'], w['limit'], w['rate']), (1_000_000, 1_000_000, 585, 500_000, 0.07))
		self.assertEqual(storage.borrow(u, 1_000), 'you are at your $5,000 credit limit')
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET owed_at = owed_at - 86400, debt_since = debt_since - 5 * 86400 WHERE id=?', (u,))
			storage.accrue(c)
		w = storage.wallet(u)
		self.assertAlmostEqual(w['owed'], 1_070_000, delta=24) # a day at 7% for a 585 score, give or take a cent of rounding each hour
		self.assertEqual(w['score'], 575) # 2 days past the 3 day grace at -5 each
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.accrue(c)
		self.assertEqual(storage.wallet(u)['score'], 575) # late days are only charged once
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET owed=?, debt_since=? WHERE id=?', (100_000, int(time.time()), u))
		play(t, 1_000, ['Th', '9h'], ['Ts', '7s'], actions=['stand'])
		w = storage.wallet(u)
		self.assertEqual((w['balance'], w['owed']), (1_000_750, 99_750)) # a quarter of the profit goes to the debt
		h = house()
		self.assertIsNone(storage.repay(u, 10**9))
		w = storage.wallet(u)
		self.assertEqual((w['balance'], w['owed'], w['score']), (901_000, 0, 600)) # paid off within 3 days: +25
		self.assertEqual(house(), h + 99_750)
		self.assertEqual(storage.repay(u, 100), 'you do not owe anything')
		self.assertIsNone(storage.borrow(u, 1_000))
		self.assertIsNone(storage.repay(u, 1_000))
		self.assertEqual(storage.wallet(u)['score'], 600) # the payoff bonus only comes once a day

	def test_debt_cap(self):
		u = make_user('ln2', 0)
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET owed=?, owed_at=?, debt_since=? WHERE id=?', (4_990_000, int(time.time()) - 86400, int(time.time()), u))
			storage.accrue(c)
		self.assertEqual(storage.wallet(u)['owed'], storage.DEBT_CAP)

	def test_credit_tiers(self):
		self.assertEqual([storage.credit_terms(x) for x in (300, 499, 500, 650, 700, 779, 780, 850)], [(100_000, 0.10), (100_000, 0.10), (500_000, 0.07), (1_000_000, 0.05), (2_500_000, 0.03), (2_500_000, 0.03), (5_000_000, 0.02), (5_000_000, 0.02)])


class TestHouseCover(unittest.TestCase):
	def tearDown(self):
		hub.HOUSE.clear()

	def test_limits_shrink_with_the_house(self):
		u = make_user('cv1', 10_000_000)
		hub.HOUSE['balance'] = storage.HOUSE_START
		self.assertNotIn('error', slots.spin(u, 'cv1', 100_000))
		hub.HOUSE['balance'] = 100_000 * slots.WORST * hub.COVER_SHARE - 1
		self.assertIn('too low', slots.spin(u, 'cv1', 100_000)['error'])
		self.assertNotIn('error', slots.spin(u, 'cv1', 50_000))
		t = blackjack.Table('cv', 'Cover', 'vip')
		self.assertEqual(t.max_bet, min(5_000_000, hub.cover() // blackjack.EXPOSURE))
		hub.HOUSE['balance'] = 0
		blackjack.sit(t, u, 'cv1')
		self.assertIn('too low', blackjack.bet(t, 0, 50_000, True))

	def test_no_poker_in_debt(self):
		hub.TABLES.clear()
		hub.TABLES['d'] = t = poker.Table('d', 'Debt', 'micro')
		a, b = make_user('pd1'), make_user('pd2')
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET owed=500 WHERE id=?', (a,))
		self.assertIsNone(poker.join(t, a, 'pd1', 4_000)) # alone with bots is fine
		self.assertIn('someone at this table is in debt', poker.join(t, b, 'pd2', 4_000))
		poker.leave(t, 0 if t.seats[0] and t.seats[0]['user_id'] == a else next(i for i, s in enumerate(t.seats) if s and s['user_id'] == a))
		self.assertIsNone(poker.join(t, b, 'pd2', 4_000))
		self.assertIn('pay off your debt', poker.join(t, a, 'pd1', 4_000))


class TestSlots(unittest.TestCase):
	def test_lines(self):
		self.assertEqual(slots.line_pay(['7', '7', '7', 'C', 'L']), (60, 3))
		self.assertEqual(slots.line_pay(['W', 'W', '7', '7', 'C']), (300, 4))
		self.assertEqual(slots.line_pay(['W', 'W', 'W', 'C', 'C']), (100, 3)) # three wilds beat five cherries made with them
		self.assertEqual(slots.line_pay(['S', 'S', 'S', 'C', 'C']), (0, 0))
		self.assertEqual(slots.line_pay(['C', 'C', 'L', 'C', 'C']), (0, 0))

	def test_rtp_matches_simulation(self):
		random.seed(1)
		paid, n = 0, 200_000
		for _ in range(n):
			stops = [random.randrange(len(s)) for s in slots.STRIPS]
			grid  = [[slots.STRIPS[r][(stops[r] + row) % len(slots.STRIPS[r])] for row in range(slots.ROWS)] for r in range(5)]
			paid += sum(slots.line_pay([grid[r][row] for r, row in enumerate(line)])[0] for line in slots.LINES)
			paid += slots.SCATTER_PAYS.get(sum(col.count('S') for col in grid), 0) * len(slots.LINES)
		self.assertAlmostEqual(paid / (n * len(slots.LINES)), slots.rtp(), delta=0.03)
		self.assertTrue(0.95 < slots.rtp() < 0.97)


class TestPoker(unittest.TestCase):
	def test_side_pots(self):
		h = {'players': {0: {'total': 100, 'folded': False}, 1: {'total': 300, 'folded': False}, 2: {'total': 300, 'folded': False}, 3: {'total': 50, 'folded': True}}}
		self.assertEqual(poker.pots(h), [(350, [0, 1, 2]), (400, [1, 2])])
		h = {'players': {0: {'total': 500, 'folded': False}, 1: {'total': 200, 'folded': False}}}
		self.assertEqual(poker.pots(h), [(400, [0, 1]), (300, [0])])

	def test_chips_are_conserved(self):
		hub.TABLES.clear()
		hub.TABLES['t'] = t = poker.Table('t', 'Test', 'micro')
		users = [make_user(f'pk{i}') for i in range(2)]
		for u in users:
			self.assertIsNone(poker.join(t, u, f'pk{u}', 4_000))
		self.assertEqual(poker.join(t, users[0], 'again', 10_000), 'you are already sitting at a poker table')
		poker.seat_bots(t)
		self.assertEqual(sum(1 for s in t.seats if s), 4)
		total = sum(s['stack'] for s in t.seats if s)
		bank, bots_at = house(), sum(s['stack'] for s in t.seats if s and not s['user_id'])
		for _ in range(40):
			self.assertTrue(poker.start_hand(t))
			while t.hand['street'] != 'done':
				seat = t.hand['to_act']
				if t.seats[seat]['user_id']:
					opt = poker.options(t, seat)
					choice = random.choice(['call', 'check', 'fold', 'raise'])
					if choice == 'raise' and opt['raise']:
						self.assertIsNone(poker.act(t, seat, 'raise', random.randint(opt['min_to'], opt['max_to'])))
					else:
						self.assertIsNone(poker.act(t, seat, 'check' if opt['check'] else 'call' if choice != 'fold' else 'fold'))
				else:
					self.assertIsNone(poker.act(t, seat, *poker.bot_decide(t, seat)))
			self.assertEqual(sum(s['stack'] for s in t.seats if s), total)
			bots = sum(s['stack'] for s in t.seats if s and not s['user_id'])
			self.assertEqual(house() - bank, bots - bots_at) # what the bots win or lose is the house's
			for i, s in enumerate(t.seats):
				if s and s['stack'] == 0:
					if s['user_id']:
						s['stack'] = 4_000
					else:
						s['stack'] = t.bb * poker.BOT_STACK
					total = sum(x['stack'] for x in t.seats if x)
			bank, bots_at = house(), sum(x['stack'] for x in t.seats if x and not x['user_id'])
		before = bal(users[0]) + next(s['stack'] for s in t.seats if s and s['user_id'] == users[0])
		poker.leave(t, next(i for i, s in enumerate(t.seats) if s and s['user_id'] == users[0]))
		self.assertEqual(bal(users[0]), before)



if __name__ == '__main__':
	unittest.main()
