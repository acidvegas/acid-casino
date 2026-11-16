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

from fractions import Fraction
from unittest import mock

os.environ['CASINO_DATA_DIR'] = tempfile.mkdtemp(prefix='acidcasino-test-')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'acidcasino'))

import blackjack
import cards
import chat
import hub
import poker
import roulette
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


def jackpot() -> int:
	'''Return the slots jackpot.'''

	with storage.db_connect() as c:
		return storage.jackpot(c)


def make_user(name: str, balance: int = 100_000) -> int:
	'''Create a test account and return its id.'''

	with storage.DB_LOCK, storage.db_connect() as c:
		return c.execute('INSERT INTO users(username, password_hash, created_at, balance) VALUES(?, ?, 0, ?)', (name, 'x', balance)).lastrowid


def bal(user_id: int) -> int:
	'''Return a test account's balance.'''

	with storage.db_connect() as c:
		return storage.balance(c, user_id)


def rigged_rng(jackpot_draw: int):
	'''Patch the shared RNG so every reel stops at 0 and the jackpot draw comes out as given.'''

	return mock.patch.object(slots.RNG, 'randrange', side_effect=lambda n: jackpot_draw if n == slots.JACKPOT_ODDS else 0)


def rig(t, player: list, dealer: list, extra: list = ()):
	'''Stack a solo table's shoe: player cards, dealer cards, then the cards drawn after.'''

	order  = [player[0], dealer[0], player[1], dealer[1], *extra]
	t.shoe = ['2c'] * 300 + list(reversed(order))


def rl_round(t, result: int):
	'''Spin a roulette table that has its bets down, land the ball on a pocket and pay out.'''

	with mock.patch.object(roulette.RNG, 'choice', return_value=result):
		roulette.spin(t)
	t.next_at = 0
	roulette.tick(t)


def rl_table(name: str, wheel: str = 'european', stakes: str = 'low', balance: int = 100_000) -> tuple:
	'''Return (user id, solo roulette table with them seated).'''

	u = make_user(name, balance)
	t = roulette.Table('r' + name, 'Test', stakes, wheel, solo=True)
	roulette.sit(t, u, name)
	return u, t


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

	def test_insurance_short_of_chips_stays_open(self):
		'''Insurance that can not be paid for leaves the question open instead of counting as turned down.'''

		u, t = solo('bj8', 1_000)
		rig(t, ['Th', '9h'], ['As', '7s'])
		blackjack.bet(t, 0, 1_000, True)
		self.assertEqual(t.phase, 'insurance')
		self.assertEqual(blackjack.insure(t, 0, True), 'not enough chips')
		self.assertIsNone(t.seats[0]['insurance'])
		self.assertTrue(t.view(u)['insure'])
		self.assertIsNone(blackjack.insure(t, 0, False))
		self.assertEqual(t.seats[0]['insurance'], 0)
		self.assertIsNone(blackjack.act(t, 0, 'stand'))
		while t.phase == 'dealer':
			t.next_at = 0
			blackjack.tick(t)
		self.assertEqual(bal(u), 2_000) # 19 beats 18, paying even money

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


class TestJackpot(unittest.TestCase):
	def tearDown(self):
		hub.HOUSE.clear()

	def test_chance_scales_with_the_bet(self):
		'''A spin hits when the draw lands under the bet in cents, so a $1,000 spin is 1,000 times likelier than a $1 one and hits once in 50,000.'''

		self.assertEqual(slots.JACKPOT_ODDS // slots.BETS[-1], 50_000)
		self.assertEqual(Fraction(slots.BETS[-1], slots.JACKPOT_ODDS) / Fraction(slots.BETS[0], slots.JACKPOT_ODDS), 1_000)
		u = make_user('jp1', 1_000_000)
		with rigged_rng(99):
			self.assertTrue(slots.spin(u, 'jp1', 100)['jackpot']) # 99 < 100
		with rigged_rng(100):
			self.assertFalse(slots.spin(u, 'jp1', 100)['jackpot'])
		with rigged_rng(99_999):
			self.assertTrue(slots.spin(u, 'jp1', 100_000)['jackpot'])

	def test_added_rtp(self):
		'''Over the long run the jackpot pays back every 1% put in plus the $50,000 seed of every win: 1% + 0.1% = 1.1% on top of the reels.'''

		self.assertAlmostEqual(slots.jackpot_rtp(), 0.011)
		self.assertAlmostEqual(slots.rtp() + slots.jackpot_rtp(), 0.965 + 0.011, delta=0.005)

	def test_funded_by_the_house(self):
		u = make_user('jp2', 1_000_000)
		pot, bank = jackpot(), house()
		with rigged_rng(slots.JACKPOT_ODDS - 1):
			res = slots.spin(u, 'jp2', 10_000)
		self.assertEqual(res['jackpot'], 0)
		self.assertEqual(jackpot(), pot + 100) # 1% of the bet
		self.assertEqual(res['pot'], pot + 100)
		self.assertEqual(house(), bank + 10_000 - res['payout'] - 100)
		self.assertEqual(bal(u), 1_000_000 - 10_000 + res['payout']) # the player's side is untouched
		with storage.db_connect() as c:
			row = c.execute('SELECT user_id, amount, balance, game FROM ledger ORDER BY id DESC LIMIT 1').fetchone()
		self.assertEqual(tuple(row), (storage.JACKPOT_USER, 100, pot + 100, 'jackpot'))

	def test_win_pays_the_pot_and_reseeds(self):
		u = make_user('jp3', 1_000_000)
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.house_move(c, -1_000_000)
			storage.jackpot_move(c, 1_000_000, 'test top up')
		pot, bank = jackpot(), house()
		with rigged_rng(0):
			res = slots.spin(u, 'jp3', 100)
		self.assertEqual(res['jackpot'], pot + 1)
		self.assertEqual(bal(u), 1_000_000 - 100 + res['payout'] + pot + 1)
		self.assertEqual(jackpot(), storage.JACKPOT_SEED)
		self.assertEqual(res['pot'], storage.JACKPOT_SEED)
		self.assertEqual(house(), bank + 100 - res['payout'] - 1 - storage.JACKPOT_SEED)
		with storage.db_connect() as c:
			notes = [r['note'] for r in c.execute('SELECT note FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 2', (storage.JACKPOT_USER,))]
			paid  = c.execute('SELECT amount, note FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 1', (u,)).fetchone()
		self.assertEqual(notes, [f'seeded with {storage.money(storage.JACKPOT_SEED)} from the house', 'won by jp3'])
		self.assertEqual(paid['amount'], pot + 1)
		self.assertIn('jackpot', paid['note'])
		self.assertTrue(any('SLOTS JACKPOT' in m['text'] for m in chat.ROOMS['lobby']))
		self.assertEqual(chat.FEED[-1]['net'], res['payout'] + pot + 1 - 100)

	def test_persists_across_restarts(self):
		with storage.DB_LOCK, storage.db_connect() as c:
			storage.house_move(c, -12_345)
			storage.jackpot_move(c, 12_345, 'test top up')
		pot, bank = jackpot(), house()
		storage.init_db() # a restart opens the same database
		self.assertEqual((jackpot(), house()), (pot, bank))

	def test_cover_keeps_the_seed_back(self):
		hub.HOUSE['balance'] = storage.JACKPOT_SEED + 5_000_000
		self.assertEqual(hub.cover(), 5_000)
		hub.HOUSE['balance'] = storage.JACKPOT_SEED
		self.assertEqual(hub.cover(), 0)


class TestRoulette(unittest.TestCase):
	def tearDown(self):
		hub.HOUSE.clear()

	def test_wheels(self):
		for wheel, size in (('european', 37), ('american', 38)):
			pockets = roulette.WHEELS[wheel]
			self.assertEqual(sorted(pockets), list(range(size)))
		self.assertEqual(roulette.WHEELS['european'][:5], [0, 32, 15, 19, 4])
		self.assertEqual(roulette.WHEELS['american'][18:21], [1, roulette.DOUBLE_0, 27])
		self.assertEqual(len(roulette.RED), 18)
		self.assertEqual({n for n in range(1, 37) if n not in roulette.RED} & {1, 3, 5, 7, 9}, set())

	def test_every_bet_returns_the_true_odds_less_the_zero(self):
		'''Every bet returns 36/37 on the European wheel and 36/38 on the American one, except the 5-number top line at 35/38.'''

		counts = {'european': 157, 'american': 161}
		for wheel, pockets in roulette.WHEELS.items():
			spots = roulette.SPOTS[wheel]
			self.assertEqual(len(spots), counts[wheel])
			for key, spot in spots.items():
				back = sum(roulette.payout(spots, {key: 100}, p) for p in pockets)
				want = Fraction(35, 38) if key == 'basket' else Fraction(36, len(pockets))
				self.assertEqual(Fraction(back, 100 * len(pockets)), want, key)

	def test_payout_of_every_bet_kind(self):
		pays = {'straight:17': 35, 'split:17-20': 17, 'street:16-17-18': 11, 'corner:16-17-19-20': 8, 'line:13-14-15-16-17-18': 5, 'dozen:2': 2, 'column:2': 2, 'black': 1, 'odd': 1, 'low': 1}
		for wheel in roulette.WHEELS:
			spots = roulette.SPOTS[wheel]
			for key, mult in pays.items():
				self.assertEqual(roulette.payout(spots, {key: 100}, 17), 100 * (mult + 1), (wheel, key))
			for key in ('red', 'even', 'high', 'dozen:1', 'column:1', 'straight:0'):
				self.assertEqual(roulette.payout(spots, {key: 100}, 17), 0, (wheel, key))
			for key in ('red', 'black', 'odd', 'even', 'low', 'high', 'dozen:1', 'column:3'):
				self.assertEqual(roulette.payout(spots, {key: 100}, 0), 0, (wheel, key)) # zero loses every outside bet
		eu, us = roulette.SPOTS['european'], roulette.SPOTS['american']
		self.assertEqual(roulette.payout(eu, {'corner:0-1-2-3': 100}, 0), 900)
		self.assertEqual(roulette.payout(eu, {'street:0-2-3': 100}, 3), 1_200)
		self.assertEqual(roulette.payout(us, {'basket': 100}, roulette.DOUBLE_0), 700)
		self.assertEqual(roulette.payout(us, {'split:0-00': 100}, roulette.DOUBLE_0), 1_800)
		self.assertEqual(roulette.payout(us, {'straight:00': 100}, roulette.DOUBLE_0), 3_600)
		self.assertEqual(roulette.payout(us, {'street:0-00-2': 100}, 2), 1_200)

	def test_round_pays_from_the_house(self):
		u, t = rl_table('rl1')
		before = house()
		self.assertIsNone(roulette.place(t, 0, [{'spot': 'straight:17', 'amount': 1_000}, {'spot': 'red', 'amount': 2_000}, {'spot': 'dozen:2', 'amount': 500}], False))
		self.assertEqual(bal(u), 100_000 - 3_500)
		self.assertIsNotNone(t.deadline)
		rl_round(t, 17) # black, middle dozen
		self.assertEqual(t.phase, 'done')
		self.assertEqual(t.seats[0]['net'], 35_000 - 2_000 + 1_000)
		self.assertEqual(bal(u), 100_000 + 34_000)
		self.assertEqual(house(), before - 34_000)
		self.assertEqual(list(t.history), [17])
		self.assertEqual(chat.FEED[-1]['net'], 34_000)
		with storage.db_connect() as c:
			self.assertIsNone(c.execute('SELECT 1 FROM rl_bets').fetchone())
		t.next_at = 0
		roulette.tick(t)
		self.assertEqual((t.phase, t.seats[0]['bets']), ('betting', {}))
		self.assertEqual(t.seats[0]['last'], {'straight:17': 1_000, 'red': 2_000, 'dozen:2': 500}) # for repeat last bet
		with mock.patch.object(roulette.RNG, 'choice', return_value=0):
			self.assertIsNone(roulette.place(t, 0, [{'spot': 'even', 'amount': 1_000}], True)) # ready on a solo table spins at once
		self.assertEqual(t.phase, 'spinning')
		t.next_at = 0
		roulette.tick(t)
		self.assertEqual(bal(u), 100_000 + 34_000 - 1_000) # even money loses on zero

	def test_shared_table(self):
		a, b = make_user('rlA'), make_user('rlB')
		t = roulette.Table('rshared', 'Shared', 'low', 'american')
		roulette.sit(t, a, 'rlA')
		roulette.sit(t, b, 'rlB')
		self.assertEqual(roulette.sit(t, a, 'rlA'), 'you are already at this table')
		roulette.place(t, 0, [{'spot': 'straight:00', 'amount': 100}], True)
		self.assertEqual(t.phase, 'betting') # waits for the other player
		with mock.patch.object(roulette.RNG, 'choice', return_value=roulette.DOUBLE_0):
			roulette.place(t, 1, [{'spot': 'basket', 'amount': 1_000}], True)
		self.assertEqual(t.phase, 'spinning')
		self.assertEqual(roulette.place(t, 1, [], False), 'no more bets, wait for the next spin')
		roulette.leave(t, 1) # leaving mid spin rides the bet out
		t.next_at = 0
		roulette.tick(t)
		self.assertEqual((bal(a), bal(b)), (100_000 + 3_500, 100_000 + 6_000))
		t.next_at = 0
		roulette.tick(t)
		self.assertIsNone(t.seats[1])

	def test_bet_validation(self):
		u, t = rl_table('rl2')
		_, us = rl_table('rl3', 'american')
		bad = [
			([{'spot': 'straight:37', 'amount': 100}], 'that is not a bet on this table'),
			([{'spot': 'straight:00', 'amount': 100}], 'that is not a bet on this table'), # no double zero in Europe
			([{'spot': 'basket', 'amount': 100}], 'that is not a bet on this table'),
			([{'spot': 'split:1-5', 'amount': 100}], 'that is not a bet on this table'),
			([{'spot': 'split:3-4', 'amount': 100}], 'that is not a bet on this table'), # 3 and 4 are not side by side on the layout
			([{'spot': 'corner:1-2-3-4', 'amount': 100}], 'that is not a bet on this table'),
			([{'spot': 'corner:3-4-6-7', 'amount': 100}], 'that is not a bet on this table'),
			([{'spot': 'street:2-3-4', 'amount': 100}], 'that is not a bet on this table'),
			([{'spot': 'straight:-1', 'amount': 100}], 'that is not a bet on this table'),
			([{'spot': 17, 'amount': 100}], 'that is not a bet on this table'),
			(['red'], 'that is not a bet on this table'),
			([{'spot': 'red', 'amount': 100.0}], 'bets must be whole cents'),
			([{'spot': 'red', 'amount': True}], 'bets must be whole cents'),
			([{'spot': 'red', 'amount': '100'}], 'bets must be whole cents'),
			([{'spot': 'red', 'amount': 99}], 'bets here are $1 to $100 a spot'),
			([{'spot': 'red', 'amount': 10_001}], 'bets here are $1 to $100 a spot'),
			([{'spot': 'red', 'amount': 100}, {'spot': 'red', 'amount': 100}], 'each spot can only be bet once'),
			([{'spot': f'straight:{n}', 'amount': 10_000} for n in range(11)], 'the most on the table in one spin here is $1,000'),
			({'spot': 'red', 'amount': 100}, 'bad bets'),
		]
		for bets, error in bad:
			self.assertEqual(roulette.place(t, 0, bets, False), error, bets)
		self.assertEqual(bal(u), 100_000)
		self.assertIsNone(t.deadline)
		self.assertIsNone(roulette.place(t, 0, [{'spot': 'split:3-6', 'amount': 100}, {'spot': 'corner:1-2-4-5', 'amount': 100}, {'spot': 'split:0-3', 'amount': 100}], False))
		self.assertIsNone(roulette.place(us, 0, [{'spot': 'straight:00', 'amount': 100}, {'spot': 'split:00-3', 'amount': 100}, {'spot': 'basket', 'amount': 100}], False))
		self.assertEqual(roulette.place(us, 0, [{'spot': 'corner:0-1-2-3', 'amount': 100}], False), 'that is not a bet on this table') # the first four is European only
		poor, pt = rl_table('rl4', balance=500)
		self.assertEqual(roulette.place(pt, 0, [{'spot': 'red', 'amount': 1_000}], False), 'not enough chips')
		self.assertEqual(bal(poor), 500)

	def test_house_cover(self):
		u, t = rl_table('rl5', stakes='vip', balance=10_000_000)
		hub.HOUSE['balance'] = storage.JACKPOT_SEED + 1_000_000 * 35 * hub.COVER_SHARE # a $10,000 straight up win is just coverable
		self.assertEqual(t.max_bet, 1_000_000)
		self.assertIsNone(roulette.place(t, 0, [{'spot': 'straight:5', 'amount': 1_000_000}], False))
		self.assertEqual(roulette.place(t, 0, [{'spot': 'straight:5', 'amount': 1_000_000}, {'spot': 'split:5-8', 'amount': 10_000}], False), 'the house can not cover that much riding on one number right now')
		self.assertIsNone(roulette.place(t, 0, [{'spot': 'straight:5', 'amount': 1_000_000}, {'spot': 'split:7-8', 'amount': 10_000}], False)) # different numbers never win together
		hub.HOUSE['balance'] = storage.JACKPOT_SEED + 9_999 * 35 * hub.COVER_SHARE
		self.assertEqual(t.max_bet, 9_999) # under the $100 table minimum
		self.assertEqual(roulette.place(t, 0, [{'spot': 'red', 'amount': 1_000_000}], False), 'the house is too low on chips to cover this table right now')
		roulette.place(t, 0, [], False)
		self.assertEqual(bal(u), 10_000_000)

	def test_refund_on_restart(self):
		u, t = rl_table('rl6')
		roulette.place(t, 0, [{'spot': 'odd', 'amount': 5_000}, {'spot': 'straight:9', 'amount': 2_500}], True) # solo and ready, so it is spinning when the restart hits
		self.assertEqual(t.phase, 'spinning')
		self.assertEqual(bal(u), 100_000 - 7_500)
		with storage.db_connect() as c:
			self.assertTrue(storage.in_play(c, u))
		storage.refund_bets()
		self.assertEqual(bal(u), 100_000)
		with storage.db_connect() as c:
			self.assertIsNone(c.execute('SELECT 1 FROM rl_bets').fetchone())

	def test_leave_before_the_spin_refunds(self):
		u, t = rl_table('rl7')
		roulette.place(t, 0, [{'spot': 'high', 'amount': 1_000}], False)
		roulette.leave(t, 0)
		self.assertEqual(bal(u), 100_000)
		self.assertIsNone(t.seats[0])
		self.assertFalse(t.busy())


class TestSlots(unittest.TestCase):
	def test_lines(self):
		self.assertEqual(slots.line_pay(['7', '7', '7', 'C', 'L']), (60, 3))
		self.assertEqual(slots.line_pay(['W', 'W', '7', '7', 'C']), (300, 4))
		self.assertEqual(slots.line_pay(['W', 'W', 'W', 'C', 'C']), (100, 3)) # three wilds beat five cherries made with them
		self.assertEqual(slots.line_pay(['S', 'S', 'S', 'C', 'C']), (0, 0))
		self.assertEqual(slots.line_pay(['C', 'C', 'L', 'C', 'C']), (0, 0))

	def test_float_bet_refused(self):
		'''A JSON float equal to a bet size is turned away, so balances stay whole cents.'''

		u = make_user('sl1')
		self.assertEqual(slots.spin(u, 'sl1', 100.0), {'error': 'pick one of the bet sizes'})
		self.assertEqual(slots.spin(u, 'sl1', True), {'error': 'pick one of the bet sizes'})
		self.assertEqual(bal(u), 100_000)
		self.assertIs(type(bal(u)), int)

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
	def test_restart_mid_hand_after_leaving(self):
		'''Chips a player left in the pot when standing up go to the house if a restart voids the hand, so nothing is lost.'''

		def everything() -> int:
			with storage.db_connect() as c:
				return c.execute('SELECT (SELECT SUM(balance) FROM users) + (SELECT COALESCE(SUM(stack), 0) FROM poker_seats) + (SELECT balance FROM house WHERE id=1)').fetchone()[0]

		hub.TABLES.clear()
		hub.TABLES['r'] = t = poker.Table('r', 'Restart', 'micro', bots=False)
		users = [make_user(f'pr{i}') for i in range(3)]
		for u in users:
			played(u)
		before = everything()
		for u in users:
			self.assertIsNone(poker.join(t, u, f'pr{u}', 4_000))
		self.assertTrue(poker.start_hand(t))
		h = t.hand
		self.assertIsNone(poker.act(t, h['to_act'], 'raise', 1_000))
		sb = h['sb_seat']
		self.assertEqual(h['to_act'], sb)
		self.assertIsNone(poker.act(t, sb, 'call'))
		leaver = t.seats[sb]['user_id']
		poker.leave(t, sb)
		self.assertNotEqual(h['street'], 'done') # two players still in, so the hand goes on
		self.assertEqual(bal(leaver), 100_000 - 1_000)
		with storage.db_connect() as c:
			self.assertEqual(c.execute('SELECT amount FROM poker_pots WHERE table_id=?', ('r',)).fetchone()[0], 1_000)
		bank = house()
		hub.TABLES.clear() # the restart: the hand in memory is gone
		poker.cash_out_all()
		self.assertEqual(house(), bank + 1_000)
		self.assertEqual(everything(), before)
		with storage.db_connect() as c:
			self.assertIsNone(c.execute('SELECT 1 FROM poker_pots').fetchone())

	def test_side_pots(self):
		h = {'players': {0: {'total': 100, 'folded': False}, 1: {'total': 300, 'folded': False}, 2: {'total': 300, 'folded': False}, 3: {'total': 50, 'folded': True}}}
		self.assertEqual(poker.pots(h), [(350, [0, 1, 2]), (400, [1, 2])])
		h = {'players': {0: {'total': 500, 'folded': False}, 1: {'total': 200, 'folded': False}}}
		self.assertEqual(poker.pots(h), [(400, [0, 1]), (300, [0])])

	def test_new_accounts_kept_apart(self):
		'''A new account can play bots but never sit with another person, in either order.'''

		hub.TABLES.clear()
		hub.TABLES['n'] = t = poker.Table('n', 'New', 'micro')
		old, new = make_user('pkold'), make_user('pknew')
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET wagered=? WHERE id=?', (storage.LOAN_WAGERED, old))
		self.assertIsNone(poker.join(t, new, 'pknew', 4_000))
		self.assertEqual(poker.join(t, old, 'pkold', 4_000), 'a new account is at this table, so it is closed to other players for now')
		hub.TABLES['o'] = t2 = poker.Table('o', 'Old', 'micro')
		self.assertIsNone(poker.join(t2, old, 'pkold', 4_000))
		hub.TABLES.pop('n')
		self.assertTrue(poker.join(t2, new, 'pknew', 4_000).startswith('new accounts can play other people'))

	def test_chips_are_conserved(self):
		hub.TABLES.clear()
		hub.TABLES['t'] = t = poker.Table('t', 'Test', 'micro')
		users = [make_user(f'pk{i}') for i in range(2)]
		with storage.DB_LOCK, storage.db_connect() as c:
			c.execute('UPDATE users SET wagered=? WHERE username IN (\'pk0\', \'pk1\')', (storage.LOAN_WAGERED,))
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
