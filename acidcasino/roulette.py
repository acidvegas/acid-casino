#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/roulette.py

import collections
import time

import chat
import hub
from cards import RNG
from storage import DB_LOCK, NotEnough, credit, db_connect, house_move, money, move, wager


SEATS      = 6
BET_TIME   = 20     # seconds to bet once the first chip is down
SPIN_TIME  = 9      # seconds the ball runs before it drops into its pocket
SHOW_TIME  = 6      # seconds the result stays up
HISTORY    = 20     # winning numbers kept for the board
DOUBLE_0   = 37     # how 00 is written on the American wheel
RED        = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
PAYS       = {'straight': 35, 'split': 17, 'street': 11, 'corner': 8, 'basket': 6, 'line': 5, 'dozen': 2, 'column': 2, 'red': 1, 'black': 1, 'odd': 1, 'even': 1, 'low': 1, 'high': 1}
STAKES     = [('low', 'Low', 100, 10_000, 100_000), ('mid', 'Mid', 1_000, 100_000, 1_000_000), ('vip', 'VIP', 10_000, 1_000_000, 10_000_000)] # (key, label, smallest bet, biggest bet on one spot, most on the table in one spin) in cents
WHEELS     = {
	'european': [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26],
	'american': [0, 28, 9, 26, 30, 11, 7, 20, 32, 17, 5, 22, 34, 15, 3, 24, 36, 13, 1, DOUBLE_0, 27, 10, 25, 29, 12, 8, 19, 31, 18, 6, 21, 33, 16, 4, 23, 35, 14, 2],
}
WHEEL_NAME = {'european': 'European', 'american': 'American'}
RULES      = {
	'european': ['Single zero wheel, 37 pockets', 'Straight up 35 to 1, split 17 to 1, street 11 to 1, corner 8 to 1, six line 5 to 1', 'Zero can be split with 1, 2 or 3, played in the trios 0-1-2 and 0-2-3 and in the first four 0-1-2-3', 'Dozens and columns 2 to 1; red, black, odd, even, 1-18 and 19-36 even money', 'Outside bets lose on zero', 'House edge 2.7% on every bet'],
	'american': ['Double zero wheel, 38 pockets', 'Straight up 35 to 1, split 17 to 1, street 11 to 1, corner 8 to 1, six line 5 to 1', 'Top line 0-00-1-2-3 pays 6 to 1 (house edge 7.89%)', 'Zero and double zero can be split together and with their neighbours, and played in the trios 0-1-2, 0-00-2 and 00-2-3', 'Dozens and columns 2 to 1; red, black, odd, even, 1-18 and 19-36 even money', 'Outside bets lose on 0 and 00', 'House edge 5.26% on every other bet'],
}


class Table:
	'''A roulette table where everyone bets on the same spin of one wheel.'''

	game = 'roulette'

	def __init__(self, tid: str, name: str, stakes: str, wheel: str, house: bool = False, private: bool = False, owner: int = None, solo: bool = False):
		_, label, low, high, spin_max = next(s for s in STAKES if s[0] == stakes)
		self.id          = tid
		self.name        = name
		self.stakes      = stakes
		self.wheel       = wheel
		self.label       = f'{label} {WHEEL_NAME[wheel]}'
		self.min_bet     = low
		self.table_max   = high
		self.spin_max    = spin_max
		self.house       = house
		self.private     = private
		self.owner       = owner
		self.solo        = solo
		self.seats       = [None] * (1 if solo else SEATS)
		self.spots       = SPOTS[wheel]
		self.phase       = 'betting'
		self.deadline    = None
		self.next_at     = 0
		self.result      = None
		self.round       = 0
		self.history     = collections.deque(maxlen=HISTORY)
		self.empty_since = None

	@property
	def max_bet(self) -> int:
		'''Return the most on one spot, lowered while the house is too small to cover a straight up win.'''

		return min(self.table_max, hub.cover() // PAYS['straight'])

	def busy(self) -> bool:
		'''Return True while chips are in play.'''

		return self.phase != 'betting' or any(s and s['bets'] for s in self.seats)

	def humans(self) -> int:
		'''Return how many people are seated.'''

		return sum(1 for s in self.seats if s)

	def tick(self) -> bool:
		'''Run the table clock.'''

		return tick(self)

	def view(self, user_id: int) -> dict:
		'''Return the table as a player sees it.'''

		return view(self, user_id)


def build_spots(wheel: str) -> dict:
	'''Return every bet the layout of a wheel takes, keyed by name, as {'kind', 'numbers', 'pays'}.'''

	spots = {}

	def add(kind: str, numbers: list, key: str = None):
		numbers = sorted(numbers, key=lambda n: 0.5 if n == DOUBLE_0 else n) # 00 sits between 0 and 1, like on the layout
		spots[key or f'{kind}:' + '-'.join(label(n) for n in numbers)] = {'kind': kind, 'numbers': numbers, 'pays': PAYS[kind]}

	for n in WHEELS[wheel]:
		add('straight', [n])
	for n in range(1, 37):
		if n % 3:
			add('split', [n, n + 1])
		if n + 3 <= 36:
			add('split', [n, n + 3])
		if n % 3 and n + 4 <= 36:
			add('corner', [n, n + 1, n + 3, n + 4])
	for row in range(12):
		add('street', [row * 3 + 1, row * 3 + 2, row * 3 + 3])
		if row < 11:
			add('line', list(range(row * 3 + 1, row * 3 + 7)))
	if wheel == 'european':
		for nums in ([0, 1], [0, 2], [0, 3]):
			add('split', nums)
		for nums in ([0, 1, 2], [0, 2, 3]):
			add('street', nums)
		add('corner', [0, 1, 2, 3]) # the first four
	else:
		for nums in ([0, DOUBLE_0], [0, 1], [0, 2], [DOUBLE_0, 2], [DOUBLE_0, 3]):
			add('split', nums)
		for nums in ([0, 1, 2], [0, DOUBLE_0, 2], [DOUBLE_0, 2, 3]):
			add('street', nums)
		add('basket', [0, DOUBLE_0, 1, 2, 3], 'basket') # the top line
	for k in range(3):
		add('dozen', range(k * 12 + 1, k * 12 + 13), f'dozen:{k + 1}')
		add('column', range(k + 1, 37, 3), f'column:{k + 1}')
	add('red', RED, 'red')
	add('black', set(range(1, 37)) - RED, 'black')
	add('odd', range(1, 37, 2), 'odd')
	add('even', range(2, 37, 2), 'even')
	add('low', range(1, 19), 'low')
	add('high', range(19, 37), 'high')
	return spots


def label(n: int) -> str:
	'''Write a pocket the way it is printed on the wheel.'''

	return '00' if n == DOUBLE_0 else str(n)


def leave(t: Table, seat: int):
	'''Stand up. Chips already on a spin that has started ride it out; chips placed before the spin are given back.'''

	s = t.seats[seat]
	if t.phase != 'betting':
		s['left'] = True
		return
	if s['bets']:
		place(t, seat, [], False)
	t.seats[seat] = None


def payout(spots: dict, bets: dict, pocket: int) -> int:
	'''Return everything a set of bets pays back when the ball lands in a pocket, the stakes of the winning bets included.'''

	return sum(amount * (spots[key]['pays'] + 1) for key, amount in bets.items() if pocket in spots[key]['numbers'])


def place(t: Table, seat: int, bets: list, ready: bool) -> str:
	'''Set every chip a seat has on the layout for the next spin, and say whether they are ready to spin. Returns an error or None.

	:param bets: [{'spot': spot name, 'amount': cents}, ...], replacing what was there
	'''

	if t.phase != 'betting':
		return 'no more bets, wait for the next spin'
	if not isinstance(bets, list) or len(bets) > len(t.spots):
		return 'bad bets'
	chips = {}
	for b in bets:
		spot, amount = (b.get('spot'), b.get('amount')) if isinstance(b, dict) else (None, None)
		if not isinstance(spot, str) or spot not in t.spots:
			return 'that is not a bet on this table'
		if spot in chips:
			return 'each spot can only be bet once'
		if type(amount) is not int: # a JSON float or a bool would slip past the range check
			return 'bets must be whole cents'
		if not t.min_bet <= amount <= t.max_bet:
			return 'the house is too low on chips to cover this table right now' if t.max_bet < t.min_bet else f'bets here are {money(t.min_bet)} to {money(t.max_bet)} a spot'
		chips[spot] = amount
	total = sum(chips.values())
	if total > t.spin_max:
		return f'the most on the table in one spin here is {money(t.spin_max)}'
	if chips and max(payout(t.spots, chips, p) for p in WHEELS[t.wheel]) - total > hub.cover():
		return 'the house can not cover that much riding on one number right now'
	s = t.seats[seat]
	try:
		with DB_LOCK, db_connect() as c:
			if total != s['stake']:
				move(c, s['user_id'], s['stake'] - total, 'roulette', f'bet {money(total)} at {t.name}' if total else f'took back {money(s["stake"])} at {t.name}')
			save_stake(c, t, s['user_id'], total)
	except NotEnough:
		return 'not enough chips'
	s['bets']  = chips
	s['stake'] = total
	s['ready'] = bool(ready and total)
	if total and not t.deadline:
		t.deadline = time.time() + BET_TIME
	if not any(x and x['bets'] for x in t.seats):
		t.deadline = None
	if all(x['ready'] for x in t.seats if x):
		spin(t) # everyone seated is in and ready, no need to wait out the clock


def save_stake(c, t: Table, user_id: int, amount: int):
	'''Remember chips in play so a restart can give them back.'''

	if amount:
		c.execute('INSERT OR REPLACE INTO rl_bets(user_id, table_id, stake) VALUES(?, ?, ?)', (user_id, t.id, amount))
	else:
		c.execute('DELETE FROM rl_bets WHERE user_id=? AND table_id=?', (user_id, t.id))


def settle(t: Table):
	'''Pay every winning bet once the ball has landed and show the results.'''

	nets = []
	with DB_LOCK, db_connect() as c:
		for s in t.seats:
			if not s or not s['bets']:
				continue
			paid = payout(t.spots, s['bets'], t.result)
			if paid:
				credit(c, s['user_id'], paid, s['stake'], 'roulette', f'paid {money(paid)} on {label(t.result)} at {t.name}')
			house_move(c, s['stake'] - paid)
			wager(c, s['user_id'], s['stake'])
			save_stake(c, t, s['user_id'], 0)
			s['net'] = paid - s['stake']
			nets.append((s['name'], s['net'], s['user_id']))
	t.history.appendleft(t.result)
	for name, net, user_id in nets:
		chat.announce(name, net, 'roulette', user_id)
	t.phase   = 'done'
	t.next_at = time.time() + SHOW_TIME


def sit(t: Table, user_id: int, name: str, seat: int = None) -> str:
	'''Take a seat. Returns an error or None.'''

	if any(s and s['user_id'] == user_id for s in t.seats):
		return 'you are already at this table'
	free = [i for i, s in enumerate(t.seats) if s is None]
	if not free:
		return 'the table is full'
	if seat not in free:
		seat = free[0]
	t.seats[seat] = {'user_id': user_id, 'name': name, 'bets': {}, 'stake': 0, 'last': {}, 'ready': False, 'net': None, 'left': False}
	hub.SEEN[user_id] = time.time()


def spin(t: Table):
	'''Close the betting and send the ball round. The pocket is drawn now so every screen can land it in the same place.'''

	if not any(s and s['bets'] for s in t.seats):
		t.deadline = None
		return
	t.round   += 1
	t.result   = RNG.choice(WHEELS[t.wheel])
	t.phase    = 'spinning'
	t.deadline = None
	t.next_at  = time.time() + SPIN_TIME
	for s in t.seats:
		if s:
			s['ready'] = False
			if s['bets']:
				s['last'] = dict(s['bets'])


def tick(t: Table) -> bool:
	'''Run the table clock. Returns True if anything changed.'''

	now = time.time()
	if t.phase == 'betting':
		changed = False
		for i, s in enumerate(t.seats):
			if s and not s['bets'] and not hub.is_online(s['user_id']):
				t.seats[i] = None
				changed = True
		if t.deadline and now >= t.deadline:
			spin(t)
			return True
		return changed
	if t.phase == 'spinning' and now >= t.next_at:
		settle(t)
		return True
	if t.phase == 'done' and now >= t.next_at:
		for i, s in enumerate(t.seats):
			if not s:
				continue
			if s['left']:
				t.seats[i] = None
				continue
			s.update(bets={}, stake=0, net=None, ready=False)
		t.phase    = 'betting'
		t.result   = None
		t.deadline = None
		return True
	return False


def view(t: Table, user_id: int) -> dict:
	'''Return the table for the client.'''

	mine  = next((i for i, s in enumerate(t.seats) if s and s['user_id'] == user_id), None)
	seats = [None if not s else {'name': s['name'], 'user_id': s['user_id'], 'bets': s['bets'], 'stake': s['stake'], 'ready': s['ready'], 'net': s['net'], 'last': s['last'] if i == mine else {}} for i, s in enumerate(t.seats)]
	return {
		'game'      : 'roulette',
		'id'        : t.id,
		'name'      : t.name,
		'label'     : t.label,
		'wheel'     : t.wheel,
		'pockets'   : WHEELS[t.wheel],
		'min_bet'   : t.min_bet,
		'max_bet'   : t.max_bet,
		'spin_max'  : t.spin_max,
		'private'   : t.private,
		'solo'      : t.solo,
		'house'     : t.house,
		'phase'     : t.phase,
		'round'     : t.round,
		'deadline'  : t.deadline,
		'time_total': BET_TIME if t.phase == 'betting' else SPIN_TIME if t.phase == 'spinning' else None,
		'lands_at'  : t.next_at if t.phase == 'spinning' else None,
		'result'    : t.result if t.phase in ('spinning', 'done') else None,
		'history'   : list(t.history),
		'seats'     : seats,
		'me'        : mine,
		'rules'     : RULES[t.wheel],
	}


SPOTS = {wheel: build_spots(wheel) for wheel in WHEELS}
