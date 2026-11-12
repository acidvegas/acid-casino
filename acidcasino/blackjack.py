#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/blackjack.py

import time

import cards
import chat
import hub
from storage import DB_LOCK, NotEnough, credit, db_connect, house_move, money, move, wager


SEATS       = 5
DECKS       = 6
PENETRATION = 0.75   # reshuffle once this much of the shoe is dealt
MAX_HANDS   = 4      # splitting up to four hands per seat
EXPOSURE    = 2 * MAX_HANDS # the most one bet can win: split to four hands, all doubled, all winning
BET_TIME    = 12     # seconds to bet once the first bet is down
INSURE_TIME = 10     # seconds to answer insurance
ACT_TIME    = 20     # seconds to play a hand
DEALER_STEP = 0.8    # seconds between dealer cards
SHOW_TIME   = 4.5    # seconds the results stay up
STAKES      = [('low', 'Low', 100, 50_000), ('mid', 'Mid', 1_000, 250_000), ('high', 'High', 10_000, 1_000_000), ('vip', 'VIP', 50_000, 5_000_000)]
RULES       = ['6 decks shuffled together, a card burned after every shuffle, and a fresh shuffle once the cut card comes out at 75%', 'Blackjack pays 3 to 2', 'Dealer stands on all 17s', 'Dealer peeks for blackjack with an ace or ten showing', 'Insurance pays 2 to 1 when the dealer shows an ace', 'Double on any first two cards, also after splitting', 'Split pairs (any two tens) up to 4 hands; split aces get one card each and can not be split again', 'Late surrender on your first two cards gives back half your bet', 'A split hand that makes 21 is not a blackjack']


class Table:
	'''A blackjack table where everyone plays their own hands against one dealer.'''

	game = 'blackjack'

	def __init__(self, tid: str, name: str, stakes: str, house: bool = False, private: bool = False, owner: int = None, solo: bool = False):
		_, label, low, high = next(s for s in STAKES if s[0] == stakes)
		self.id          = tid
		self.name        = name
		self.stakes      = stakes
		self.label       = label
		self.min_bet     = low
		self.table_max   = high
		self.house       = house
		self.private     = private
		self.owner       = owner
		self.solo        = solo
		self.seats       = [None] * (1 if solo else SEATS)
		self.phase       = 'betting'
		self.deadline    = None
		self.next_at     = 0
		self.shoe        = new_shoe()
		self.shuffled    = True
		self.dealer      = []
		self.dealer_shown = False
		self.active      = None   # (seat, hand) whose turn it is
		self.round       = 0
		self.empty_since = None

	@property
	def max_bet(self) -> int:
		'''Return the table limit, lowered while the house is too small to cover it.'''

		return min(self.table_max, hub.cover() // EXPOSURE)

	def busy(self) -> bool:
		'''Return True while chips are in play.'''

		return self.phase != 'betting' or any(s and s['bet'] for s in self.seats)

	def humans(self) -> int:
		'''Return how many people are seated.'''

		return sum(1 for s in self.seats if s)

	def tick(self) -> bool:
		'''Run the table clock.'''

		return tick(self)

	def view(self, user_id: int) -> dict:
		'''Return the table as a player sees it.'''

		return view(self, user_id)


def act(t: Table, seat: int, action: str) -> str:
	'''Play an action on the current hand: hit, stand, double, split or surrender. Returns an error or None.'''

	if t.phase != 'play' or not t.active or t.active[0] != seat:
		return 'it is not your turn'
	s    = t.seats[seat]
	hand = s['hands'][t.active[1]]
	if action not in allowed(t, seat):
		return 'you can not do that now'
	try:
		if action == 'hit':
			hand['cards'].append(draw(t))
			if total(hand['cards'])[0] >= 21:
				hand['done'] = True
		elif action == 'stand':
			hand['done'] = True
		elif action == 'double':
			stake(t, s, hand['bet'], 'double')
			hand['bet']    *= 2
			hand['doubled'] = True
			hand['cards'].append(draw(t))
			hand['done'] = True
		elif action == 'split':
			stake(t, s, hand['bet'], 'split')
			aces  = hand['cards'][0][0] == 'A'
			other = new_hand([hand['cards'].pop()], hand['bet'])
			hand.update(split=True, aces=aces)
			other.update(split=True, aces=aces)
			s['hands'].insert(t.active[1] + 1, other)
			for h in (hand, other):
				h['cards'].append(draw(t))
				if aces or total(h['cards'])[0] == 21:
					h['done'] = True
		elif action == 'surrender':
			hand['surrendered'] = True
			hand['done'] = True
	except NotEnough:
		return 'not enough chips'
	s['timeouts'] = 0
	advance(t)


def advance(t: Table):
	'''Hand the turn to the next unfinished hand, or to the dealer.'''

	order = [(i, k) for i, s in enumerate(t.seats) if s and s['hands'] for k in range(len(s['hands']))]
	start = order.index(t.active) if t.active in order else 0
	for i, k in order[start:]:
		if not t.seats[i]['hands'][k]['done']:
			t.active   = (i, k)
			t.deadline = time.time() + ACT_TIME
			return
	t.active   = None
	t.phase    = 'dealer'
	t.deadline = None
	t.next_at  = time.time() + DEALER_STEP


def allowed(t: Table, seat: int) -> list:
	'''Return what the player in a seat can do with the hand whose turn it is.'''

	if t.phase != 'play' or not t.active or t.active[0] != seat:
		return []
	s     = t.seats[seat]
	hand  = s['hands'][t.active[1]]
	first = len(hand['cards']) == 2
	out   = ['hit', 'stand']
	if first and not hand['aces']:
		out.append('double')
	if first and len(s['hands']) < MAX_HANDS and value(hand['cards'][0]) == value(hand['cards'][1]) and not hand['aces']:
		out.append('split')
	if first and len(s['hands']) == 1:
		out.append('surrender')
	return out


def bet(t: Table, seat: int, amount: int, ready: bool) -> str:
	'''Set a seat's bet for the next round, and say whether they are ready to deal. Returns an error or None.'''

	if t.phase != 'betting':
		return 'wait for the next round'
	if amount and t.max_bet < t.min_bet:
		return 'the house is too low on chips to cover this table right now'
	if not isinstance(amount, int) or (amount and not t.min_bet <= amount <= t.max_bet):
		return f'bets here are {money(t.min_bet)} to {money(t.max_bet)}'
	s = t.seats[seat]
	try:
		with DB_LOCK, db_connect() as c:
			if amount != s['bet']:
				move(c, s['user_id'], s['bet'] - amount, 'blackjack', f'bet {money(amount)} at {t.name}' if amount else f'took back {money(s["bet"])} at {t.name}')
			save_stake(c, t, s['user_id'], amount)
	except NotEnough:
		return 'not enough chips'
	s['bet']   = amount
	s['ready'] = bool(ready and amount)
	if amount and not t.deadline:
		t.deadline = time.time() + BET_TIME
	if not any(x and x['bet'] for x in t.seats):
		t.deadline = None
	if all(x['ready'] for x in t.seats if x):
		deal(t) # everyone seated is in and ready, no need to wait out the clock


def deal(t: Table):
	'''Close the betting and deal everyone in.'''

	players = [i for i, s in enumerate(t.seats) if s and s['bet']]
	if not players:
		t.deadline = None
		return
	if len(t.shoe) < DECKS * 52 * (1 - PENETRATION):
		t.shoe     = new_shoe()
		t.shuffled = True
	else:
		t.shuffled = False
	t.round += 1
	t.dealer = []
	t.dealer_shown = False
	for i in players:
		s = t.seats[i]
		s.update(hands=[new_hand([], s['bet'])], stake=s['bet'], insurance=None, net=None, ready=False)
	for _ in range(2):
		for i in players:
			t.seats[i]['hands'][0]['cards'].append(draw(t))
		t.dealer.append(draw(t))
	t.active = None
	if t.dealer[0][0] == 'A':
		t.phase    = 'insurance'
		t.deadline = time.time() + INSURE_TIME
	else:
		peek(t)


def draw(t: Table) -> str:
	'''Deal the next card from the shoe.'''

	return t.shoe.pop()


def insure(t: Table, seat: int, yes: bool) -> str:
	'''Take or turn down insurance. Returns an error or None.'''

	s = t.seats[seat]
	if t.phase != 'insurance' or not s or not s['hands'] or s['insurance'] is not None:
		return 'no insurance to answer'
	s['insurance'] = 0
	if yes:
		cost = s['hands'][0]['bet'] // 2
		try:
			stake(t, s, cost, 'insurance')
		except NotEnough:
			return 'not enough chips'
		s['insurance'] = cost
	if all(x['insurance'] is not None for x in t.seats if x and x['hands']):
		peek(t)


def is_blackjack(hand: list) -> bool:
	'''Return True for an ace and a ten-value card as the first two cards.'''

	return len(hand) == 2 and total(hand)[0] == 21


def leave(t: Table, seat: int):
	'''Stand up. A bet in play is played out by standing; a bet not yet dealt is given back.'''

	s = t.seats[seat]
	if s['hands'] and t.phase != 'betting':
		s['left'] = True
		for h in s['hands']:
			h['done'] = True
		if s['insurance'] is None:
			s['insurance'] = 0
		if t.phase == 'play' and t.active and t.active[0] == seat:
			advance(t)
		elif t.phase == 'insurance' and all(x['insurance'] is not None for x in t.seats if x and x['hands']):
			peek(t)
		return
	if s['bet']:
		bet(t, seat, 0, False)
	t.seats[seat] = None


def new_hand(hand: list, amount: int) -> dict:
	'''Return a new hand.'''

	return {'cards': hand, 'bet': amount, 'done': False, 'doubled': False, 'surrendered': False, 'split': False, 'aces': False, 'result': None, 'paid': 0}


def new_shoe() -> list:
	'''Shuffle the decks together and burn the first card, like a dealer does after every shuffle.'''

	shoe = cards.deck(DECKS)
	shoe.pop()
	return shoe


def peek(t: Table):
	'''End the round at once on a dealer blackjack, otherwise start play. Player blackjacks need no play.'''

	for s in t.seats:
		if s and s['hands']:
			s['insurance'] = s['insurance'] or 0
			if is_blackjack(s['hands'][0]['cards']):
				s['hands'][0]['done'] = True
	if is_blackjack(t.dealer):
		return settle(t)
	t.phase  = 'play'
	t.active = None
	advance(t)


def save_stake(c, t: Table, user_id: int, amount: int):
	'''Remember chips in play so a restart can give them back.'''

	if amount:
		c.execute('INSERT OR REPLACE INTO bj_bets(user_id, table_id, stake) VALUES(?, ?, ?)', (user_id, t.id, amount))
	else:
		c.execute('DELETE FROM bj_bets WHERE user_id=? AND table_id=?', (user_id, t.id))


def settle(t: Table):
	'''Pay every hand and show the results.'''

	dealer = total(t.dealer)[0]
	dbj    = is_blackjack(t.dealer)
	nets   = []
	with DB_LOCK, db_connect() as c:
		for s in t.seats:
			if not s or not s['hands']:
				continue
			payout = 3 * s['insurance'] if dbj and s['insurance'] else 0
			for h in s['hands']:
				pt  = total(h['cards'])[0]
				pbj = is_blackjack(h['cards']) and not h['split']
				if h['surrendered']:
					h['result'], h['paid'] = 'surrender', h['bet'] // 2
				elif pbj and not dbj:
					h['result'], h['paid'] = 'blackjack', h['bet'] + h['bet'] * 3 // 2
				elif pt > 21:
					h['result'], h['paid'] = 'bust', 0
				elif dbj and pbj:
					h['result'], h['paid'] = 'push', h['bet']
				elif dbj or (dealer <= 21 and dealer > pt):
					h['result'], h['paid'] = 'lose', 0
				elif dealer > 21 or pt > dealer:
					h['result'], h['paid'] = 'win', h['bet'] * 2
				else:
					h['result'], h['paid'] = 'push', h['bet']
				payout += h['paid']
			if payout:
				credit(c, s['user_id'], payout, s['stake'], 'blackjack', f'paid {money(payout)} at {t.name}')
			house_move(c, s['stake'] - payout)
			wager(c, s['user_id'], s['stake'])
			save_stake(c, t, s['user_id'], 0)
			s['net'] = payout - s['stake']
			nets.append((s['name'], s['net'], s['user_id']))
	for name, net, user_id in nets:
		chat.announce(name, net, 'blackjack', user_id)
	t.phase    = 'done'
	t.active   = None
	t.deadline = None
	t.next_at  = time.time() + SHOW_TIME


def sit(t: Table, user_id: int, name: str, seat: int = None) -> str:
	'''Take a seat. Returns an error or None.'''

	if any(s and s['user_id'] == user_id for s in t.seats):
		return 'you are already at this table'
	free = [i for i, s in enumerate(t.seats) if s is None]
	if not free:
		return 'the table is full'
	if seat not in free:
		seat = free[0]
	t.seats[seat] = {'user_id': user_id, 'name': name, 'bet': 0, 'ready': False, 'hands': [], 'stake': 0, 'insurance': None, 'net': None, 'left': False, 'timeouts': 0}
	hub.SEEN[user_id] = time.time()


def stake(t: Table, s: dict, amount: int, what: str):
	'''Take more chips for a double, split or insurance. Raises NotEnough.'''

	with DB_LOCK, db_connect() as c:
		move(c, s['user_id'], -amount, 'blackjack', f'{what} {money(amount)} at {t.name}')
		s['stake'] += amount
		save_stake(c, t, s['user_id'], s['stake'])


def tick(t: Table) -> bool:
	'''Run the table clock. Returns True if anything changed.'''

	now = time.time()
	if t.phase == 'betting':
		changed = False
		for i, s in enumerate(t.seats):
			if s and not s['bet'] and not hub.is_online(s['user_id']):
				t.seats[i] = None
				changed = True
		if t.deadline and now >= t.deadline:
			deal(t)
			return True
		return changed
	if t.phase == 'insurance' and now >= t.deadline:
		for s in t.seats:
			if s and s['hands'] and s['insurance'] is None:
				s['insurance'] = 0
		peek(t)
		return True
	if t.phase == 'play' and now >= t.deadline:
		seat, k = t.active
		t.seats[seat]['hands'][k]['done'] = True
		t.seats[seat]['timeouts'] += 1
		advance(t)
		return True
	if t.phase == 'dealer' and now >= t.next_at:
		live = [h for s in t.seats if s for h in s['hands'] if not h['surrendered'] and total(h['cards'])[0] <= 21 and not (is_blackjack(h['cards']) and not h['split'])]
		if not t.dealer_shown:
			t.dealer_shown = True
		elif live and total(t.dealer)[0] < 17:
			t.dealer.append(draw(t))
		else:
			settle(t)
			return True
		t.next_at = now + DEALER_STEP
		return True
	if t.phase == 'done' and now >= t.next_at:
		for i, s in enumerate(t.seats):
			if not s:
				continue
			if s['left'] or s['timeouts'] >= 3:
				t.seats[i] = None
				continue
			s.update(hands=[], stake=0, insurance=None, net=None, bet=0, ready=False)
		t.phase        = 'betting'
		t.dealer       = []
		t.dealer_shown = False
		t.deadline     = None
		return True
	return False


def total(hand: list) -> tuple:
	'''Return (best total, soft) for a blackjack hand.'''

	t    = sum(value(c) for c in hand)
	aces = sum(1 for c in hand if c[0] == 'A')
	if aces and t + 10 <= 21:
		return t + 10, True
	return t, False


def value(card: str) -> int:
	'''Blackjack value of a card, aces counted as 1.'''

	return 1 if card[0] == 'A' else 10 if card[0] in 'TJQK' else int(card[0])


def view(t: Table, user_id: int) -> dict:
	'''Return the table for the client, hiding the hole card until the dealer turns it over.'''

	shown  = t.phase == 'done' or (t.phase == 'dealer' and t.dealer_shown)
	dealer = t.dealer if shown else t.dealer[:1] + [None] * (len(t.dealer) > 1)
	mine   = next((i for i, s in enumerate(t.seats) if s and s['user_id'] == user_id), None)
	seats  = []
	for i, s in enumerate(t.seats):
		if not s:
			seats.append(None)
			continue
		seats.append({
			'name'     : s['name'],
			'user_id'  : s['user_id'],
			'bet'      : s['bet'],
			'ready'    : s['ready'],
			'net'      : s['net'],
			'insurance': s['insurance'],
			'hands'    : [{'cards': h['cards'], 'bet': h['bet'], 'total': total(h['cards'])[0], 'soft': total(h['cards'])[1], 'doubled': h['doubled'], 'result': h['result'], 'done': h['done']} for h in s['hands']],
		})
	return {
		'game'      : 'blackjack',
		'id'        : t.id,
		'name'      : t.name,
		'label'     : t.label,
		'min_bet'   : t.min_bet,
		'max_bet'   : t.max_bet,
		'private'   : t.private,
		'solo'      : t.solo,
		'house'     : t.house,
		'phase'     : t.phase,
		'round'     : t.round,
		'deadline'  : t.deadline,
		'time_total': {'betting': BET_TIME, 'insurance': INSURE_TIME, 'play': ACT_TIME}.get(t.phase),
		'dealer'    : dealer,
		'dealer_total': total([c for c in dealer if c])[0] if dealer else 0,
		'active'    : list(t.active) if t.active else None,
		'seats'     : seats,
		'me'        : mine,
		'actions'   : allowed(t, mine) if mine is not None else [],
		'insure'    : mine is not None and t.phase == 'insurance' and bool(t.seats[mine]['hands']) and t.seats[mine]['insurance'] is None,
		'shoe_left' : len(t.shoe),
		'shuffled'  : t.shuffled,
		'rules'     : RULES,
	}
