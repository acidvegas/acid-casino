#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/poker.py

import random
import time

import cards
import chat
import hub
from cards import RNG
from storage import DB_LOCK, LOAN_AGE, LOAN_WAGERED, NotEnough, credit, db_connect, house_move, money, move, wager


SEATS       = 6
ACT_TIME    = 25     # seconds a player gets to act
BETWEEN     = 5      # seconds the result of a hand stays up before the next deal
BOT_TARGET  = 4      # bots fill a table with a human at it up to this many players
BOT_STACK   = 100    # big blinds a bot sits down with
BOT_NAMES   = ['Ace_Bot', 'Bluffy', 'ChipBot', 'DonkBot', 'FishBot', 'GrinderBot', 'NitBot', 'RiverRat', 'SharkBot', 'TiltBot']
SIM_ROUNDS  = 200    # Monte Carlo deals a bot runs to judge its hand
SIM_RNG     = random.Random() # the practice deals only need speed, real cards come from cards.RNG
SIT_OUT_MAX = 600    # seconds before a sitting out player is stood up
STAKES      = [
	('micro', 'Micro', 25, 50),
	('low',   'Low',   100, 200),
	('mid',   'Mid',   500, 1_000),
	('high',  'High',  2_500, 5_000),
	('big',   'Big',   5_000, 10_000),
	('vip',   'VIP',   10_000, 20_000),
]
BUY_MIN     = 40     # big blinds
BUY_MAX     = 250    # big blinds


class Table:
	'''A six-seat no-limit Texas Hold'em cash table.'''

	game = 'poker'

	def __init__(self, tid: str, name: str, stakes: str, house: bool = False, private: bool = False, owner: int = None, bots: bool = True):
		_, label, sb, bb = next(s for s in STAKES if s[0] == stakes)
		self.id          = tid
		self.name        = name
		self.stakes      = stakes
		self.label       = label
		self.sb          = sb
		self.bb          = bb
		self.min_buy     = bb * BUY_MIN
		self.max_buy     = bb * BUY_MAX
		self.house       = house
		self.private     = private
		self.owner       = owner
		self.bots        = bots
		self.seats       = [None] * SEATS
		self.hand        = None
		self.button      = -1
		self.hand_no     = 0
		self.next_at     = 0
		self.log         = []
		self.empty_since = None

	def busy(self) -> bool:
		'''Return True while a hand is being played.'''

		return bool(self.hand and self.hand['street'] != 'done')

	def humans(self) -> int:
		'''Return how many people are seated.'''

		return sum(1 for st in self.seats if st and st['user_id'])

	def tick(self) -> bool:
		'''Run the table clock.'''

		return tick(self)

	def view(self, user_id: int) -> dict:
		'''Return the table as a player sees it.'''

		return view(self, user_id)


def act(table: Table, seat: int, action: str, to: int = 0) -> str:
	'''Apply a betting action for the player in a seat. Returns an error message or None.

	:param to: for bet and raise, the total this player's bet on this street becomes
	'''

	h = table.hand
	if not h or h['to_act'] != seat:
		return 'it is not your turn'
	p   = h['players'][seat]
	st  = table.seats[seat]
	opt = options(table, seat)
	if action == 'fold':
		p['folded'] = True
		say(table, seat, 'folds')
	elif action == 'check':
		if not opt['check']:
			return 'you can not check, there is a bet'
		say(table, seat, 'checks')
	elif action == 'call':
		if not opt['call']:
			return 'nothing to call'
		pay(table, seat, opt['call'])
		say(table, seat, f'calls {money(opt["call"])}' + (' and is all in' if p['allin'] else ''))
	elif action in ('bet', 'raise', 'allin'):
		if not opt['raise']:
			return 'you can not raise now'
		to = opt['max_to'] if action == 'allin' else int(to)
		if to > opt['max_to'] or (to < opt['min_to'] and to != opt['max_to']):
			return f'raise to between {money(opt["min_to"])} and {money(opt["max_to"])}'
		size = to - h['bet']
		full = size >= h['min_raise']
		for s, q in h['players'].items():
			if s != seat and not q['folded'] and not q['allin']:
				if full:
					q['can_raise'] = True
				elif q['acted']:
					q['can_raise'] = False
				q['acted'] = False
		if full:
			h['min_raise'] = size
		word     = 'bets' if h['bet'] == 0 else 'raises to'
		h['bet'] = to
		pay(table, seat, to - p['bet'])
		say(table, seat, f'{word} {money(to)}' + (' and is all in' if p['allin'] else ''))
	else:
		return 'unknown action'
	p['acted']     = True
	st['timeouts'] = 0
	advance(table)


def advance(table: Table):
	'''Pass the action on, or close the betting round and deal on, or end the hand.'''

	h      = table.hand
	living = [s for s, p in h['players'].items() if not p['folded']]
	if len(living) == 1:
		return finish(table)
	nxt = next_to_act(table, h['to_act'])
	if nxt is not None:
		return turn(table, nxt)
	# Betting round over
	for p in h['players'].values():
		p['bet'], p['acted'], p['can_raise'], p['said'] = 0, False, True, ''
	h['bet'], h['min_raise'] = 0, table.bb
	actors = [s for s in living if not h['players'][s]['allin']]
	while len(h['board']) < 5:
		h['deck'].pop() # burn a card before each street
		h['board'] += [h['deck'].pop() for _ in range(3 if not h['board'] else 1)]
		h['street'] = {3: 'flop', 4: 'turn', 5: 'river'}[len(h['board'])]
		table.log.append(f'{h["street"].capitalize()}: {" ".join(h["board"])}')
		if len(actors) > 1:
			return turn(table, next_to_act(table, table.button))
	finish(table)


def bot_decide(table: Table, seat: int) -> tuple:
	'''Pick an action for a bot from its chance of winning against the hands still in.'''

	h    = table.hand
	p    = h['players'][seat]
	opt  = options(table, seat)
	opps = sum(1 for s, q in h['players'].items() if s != seat and not q['folded'])
	eq   = equity(p['cards'], h['board'], opps)
	pot  = pot_total(h)
	call = opt['call']
	odds = call / (pot + call) if call else 0
	roll = RNG.random()
	size = lambda frac: max(opt['min_to'], min(opt['max_to'], h['bet'] + int((pot + call) * frac) // table.sb * table.sb))
	strong = 1.4 / (opps + 1) + 0.25 # what counts as a strong hand shrinks as more players stay in
	if opt['raise'] and (eq > strong or (roll < 0.06 and eq > 0.2)):
		return ('raise', size(RNG.choice([0.5, 0.66, 0.75, 1.0])))
	if call == 0:
		if opt['raise'] and eq > strong - 0.15 and roll < 0.5:
			return ('raise', size(0.5))
		return ('check', 0)
	if eq >= odds - 0.03 or (roll < 0.05 and call <= table.bb * 3):
		return ('call', 0)
	return ('fold', 0)


def cash_out(table: Table, seat: int):
	'''Stand a player up and return their stack to their balance.'''

	st = table.seats[seat]
	table.seats[seat] = None
	if st['user_id']:
		with DB_LOCK, db_connect() as c:
			c.execute('DELETE FROM poker_seats WHERE user_id=?', (st['user_id'],))
			if st['stack']:
				credit(c, st['user_id'], st['stack'], st['buyin'], 'poker', f'left {table.name} with {money(st["stack"])}')
		table.log.append(f'{st["name"]} leaves the table')


def cash_out_all():
	'''Return the chips of everyone seated before the last restart. The hand they were in is void.'''

	with DB_LOCK, db_connect() as c:
		for row in c.execute('SELECT user_id, table_id, stack, buyin FROM poker_seats').fetchall():
			if row['stack']:
				credit(c, row['user_id'], row['stack'], row['buyin'], 'poker', f'cashed out {money(row["stack"])} after a restart')
		c.execute('DELETE FROM poker_seats')


def equity(hole: list, board: list, opps: int) -> float:
	'''Estimate the chance of winning against a number of random hands by dealing out the rest many times.'''

	seen = set(hole + board)
	rest = [c for c in cards.deck() if c not in seen]
	won  = 0.0
	for _ in range(SIM_ROUNDS):
		SIM_RNG.shuffle(rest)
		full = board + rest[:5 - len(board)]
		at   = 5 - len(board)
		mine = cards.evaluate(hole + full)
		best = max((cards.evaluate(rest[at + 2 * i:at + 2 * i + 2] + full) for i in range(opps)), default=(-1,))
		won += 1 if mine > best else 0.5 if mine == best else 0
	return won / SIM_ROUNDS


def finish(table: Table):
	'''Split the pots, show the hands, save the stacks and get ready for the next hand.'''

	h      = table.hand
	living = [s for s, p in h['players'].items() if not p['folded']]
	order  = [(table.button + i) % SEATS for i in range(1, SEATS + 1)] # odd chips go to the first winner left of the button
	h['results'] = []
	if len(living) == 1:
		won = pot_total(h)
		table.seats[living[0]]['stack'] += won
		h['results'].append({'seats': living, 'amount': won, 'hand': None})
		table.log.append(f'{table.seats[living[0]]["name"]} wins {money(won)}')
	else:
		h['shown'] = living
		scores     = {s: cards.evaluate(h['players'][s]['cards'] + h['board']) for s in living}
		for amount, eligible in pots(h):
			best    = max(scores[s] for s in eligible)
			winners = [s for s in order if s in eligible and scores[s] == best]
			share, odd = divmod(amount, len(winners))
			for i, s in enumerate(winners):
				table.seats[s]['stack'] += share + (1 if i < odd else 0)
			if len(eligible) == 1:
				table.log.append(f'{money(amount)} uncalled, back to {table.seats[winners[0]]["name"]}')
				continue
			h['results'].append({'seats': winners, 'amount': amount, 'hand': cards.describe(best)})
			table.log.append(f'{" and ".join(table.seats[s]["name"] for s in winners)} {"split" if len(winners) > 1 else "wins"} {money(amount)} with {cards.describe(best)}')
		for s in living:
			h['players'][s]['hand_name'] = cards.describe(scores[s])
	bots = 0 # bots play with the house's money, so what they win or lose moves the house bankroll
	with DB_LOCK, db_connect() as c:
		for s, p in h['players'].items():
			st = table.seats[s]
			if st and not st['user_id']:
				bots += st['stack'] - p['start']
			elif st:
				wager(c, st['user_id'], p['total'])
		house_move(c, bots)
	for s, p in h['players'].items():
		st = table.seats[s]
		if st and st['user_id'] and st['stack'] != p['start']:
			chat.announce(st['name'], st['stack'] - p['start'], 'poker', st['user_id'])
	h['street']   = 'done'
	h['to_act']   = None
	table.next_at = time.time() + BETWEEN
	save_stacks(table)


def join(table: Table, user_id: int, name: str, buyin: int, seat: int = None) -> str:
	'''Sit a player down with chips from their balance. Returns an error message or None.'''

	if any(t.game == 'poker' and st and st['user_id'] == user_id for t in hub.TABLES.values() for st in t.seats):
		return 'you are already sitting at a poker table'
	if not isinstance(buyin, int) or buyin < table.min_buy or buyin > table.max_buy:
		return f'buy in for {money(table.min_buy)} to {money(table.max_buy)}'
	others = [st['user_id'] for st in table.seats if st and st['user_id']]
	with db_connect() as c: # chips borrowed from the house can not be handed to another account across a table
		if others and c.execute('SELECT owed FROM users WHERE id=?', (user_id,)).fetchone()['owed']:
			return 'pay off your debt before playing against other people (bot and solo tables are fine)'
		if others and c.execute(f'SELECT 1 FROM users WHERE owed > 0 AND id IN ({",".join("?" * len(others))})', others).fetchone():
			return 'someone at this table is in debt, so it is closed to other players until they pay it off'
		fresh = {r[0] for r in c.execute(f'SELECT id FROM users WHERE (created_at > ? OR wagered < ?) AND id IN ({",".join("?" * (len(others) + 1))})', (int(time.time()) - LOAN_AGE, LOAN_WAGERED, *others, user_id))} if others else set() # new accounts can not hand their welcome chips to another account across a table
		if user_id in fresh:
			return f'new accounts can play other people once they are a day old and have bet {money(LOAN_WAGERED)} (bot and solo tables are fine)'
		if fresh:
			return 'a new account is at this table, so it is closed to other players for now'
	busy = table.hand['players'] if table.hand and table.hand['street'] != 'done' else {} # a seat someone left mid-hand stays empty until the hand ends
	free = [i for i, st in enumerate(table.seats) if st is None and i not in busy]
	if not free:
		return 'the table is full'
	if seat not in free:
		seat = free[0]
	with DB_LOCK, db_connect() as c:
		try:
			move(c, user_id, -buyin, 'poker', f'sat at {table.name} with {money(buyin)}')
		except NotEnough:
			return 'not enough chips'
		c.execute('INSERT INTO poker_seats(user_id, table_id, seat, stack, buyin) VALUES(?, ?, ?, ?, ?)', (user_id, table.id, seat, buyin, buyin))
	running = sum(1 for st in table.seats if st and st['stack'] > 0 and not st['out_since']) >= 2
	table.seats[seat] = seat_new(user_id, name, buyin)
	table.seats[seat]['new'] = running # joining a game already going means posting a big blind to be dealt in
	hub.SEEN[user_id] = time.time()
	table.log.append(f'{name} sits down with {money(buyin)}')
	if not table.hand or table.hand['street'] == 'done':
		table.next_at = min(table.next_at or time.time() + 2, time.time() + 2)


def leave(table: Table, seat: int):
	'''Stand up now, folding first if in a hand. Chips already bet stay in the pot.'''

	h = table.hand
	if h and h['street'] != 'done' and seat in h['players'] and not h['players'][seat]['folded']:
		h['players'][seat]['folded'] = True
		say(table, seat, 'folds')
		if h['to_act'] == seat:
			advance(table)
		elif len([p for p in h['players'].values() if not p['folded']]) == 1:
			finish(table)
	cash_out(table, seat)


def next_to_act(table: Table, after: int):
	'''Return the next seat after a seat that still has to act this betting round, or None.'''

	h = table.hand
	for i in range(1, SEATS + 1):
		s = (after + i) % SEATS
		p = h['players'].get(s)
		if p and not p['folded'] and not p['allin'] and (not p['acted'] or p['bet'] < h['bet']):
			return s
	return None


def options(table: Table, seat: int) -> dict:
	'''Return what a player can do when it is their turn.'''

	h  = table.hand
	p  = h['players'][seat]
	st = table.seats[seat]
	call   = min(h['bet'] - p['bet'], st['stack'])
	max_to = p['bet'] + st['stack']
	min_to = min(max_to, (h['bet'] + h['min_raise']) if h['bet'] else table.bb)
	return {'check': call == 0, 'call': call, 'raise': p['can_raise'] and st['stack'] > call, 'min_to': min_to, 'max_to': max_to, 'bet': h['bet']}


def pay(table: Table, seat: int, amount: int):
	'''Move chips from a stack into the pot.'''

	st = table.seats[seat]
	p  = table.hand['players'][seat]
	amount       = min(amount, st['stack'])
	st['stack'] -= amount
	p['bet']    += amount
	p['total']  += amount
	if st['stack'] == 0:
		p['allin'] = True


def pot_total(h: dict) -> int:
	'''Return every chip put in this hand.'''

	return sum(p['total'] for p in h['players'].values())


def pots(h: dict) -> list:
	'''Split what everyone put in into the main pot and side pots, as (amount, seats that can win it).'''

	left = {s: p['total'] for s, p in h['players'].items() if p['total']}
	out  = []
	while left:
		live = [s for s in left if not h['players'][s]['folded']]
		if not live:
			out[-1] = (out[-1][0] + sum(left.values()), out[-1][1])
			break
		level  = min(left[s] for s in live)
		amount = sum(min(v, level) for v in left.values())
		left   = {s: v - level for s, v in left.items() if v > level}
		if out and out[-1][1] == live:
			out[-1] = (out[-1][0] + amount, live)
		else:
			out.append((amount, live))
	return out


def save_stacks(table: Table):
	'''Write the human stacks to the database at the end of a hand, so a restart returns them.'''

	with DB_LOCK, db_connect() as c:
		for st in table.seats:
			if st and st['user_id']:
				c.execute('UPDATE poker_seats SET stack=? WHERE user_id=?', (st['stack'], st['user_id']))


def say(table: Table, seat: int, text: str):
	'''Log an action at the table.'''

	table.log.append(f'{table.seats[seat]["name"]} {text}')
	table.hand['players'][seat]['said'] = text
	del table.log[:-60]


def seat_bots(table: Table):
	'''Between hands, fill empty seats with bots for a lone human, top up broke bots and send extra ones home.'''

	humans = sum(1 for st in table.seats if st and st['user_id'])
	want   = 0 if not humans or not table.bots or table.bb * BOT_STACK > hub.cover() else max(0, min(BOT_TARGET - humans, SEATS - 1 - humans)) # bots stay home while the house can not cover their stacks
	bots   = [i for i, st in enumerate(table.seats) if st and not st['user_id']]
	for i in bots[want:]:
		table.log.append(f'{table.seats[i]["name"]} leaves the table')
		table.seats[i] = None
	for i in bots[:want]:
		if table.seats[i]['stack'] < table.bb * BOT_STACK * 3 // 10:
			table.seats[i]['stack'] = table.bb * BOT_STACK
	used = {st['name'] for st in table.seats if st}
	for _ in range(want - len(bots)):
		free = [i for i, st in enumerate(table.seats) if st is None]
		name = RNG.choice([n for n in BOT_NAMES if n not in used])
		used.add(name)
		seat = RNG.choice(free)
		table.seats[seat] = seat_new(None, name, table.bb * BOT_STACK)
		table.log.append(f'{name} sits down')


def seat_new(user_id: int, name: str, stack: int) -> dict:
	'''Return a new seat.'''

	return {'user_id': user_id, 'name': name, 'stack': stack, 'buyin': stack, 'out_since': None, 'timeouts': 0}


def start_hand(table: Table) -> bool:
	'''Post the blinds and deal a new hand if enough players are in.'''

	ready = [i for i, st in enumerate(table.seats) if st and st['stack'] > 0 and not st['out_since']]
	if len(ready) < 2 or not any(table.seats[i]['user_id'] for i in ready):
		table.hand = None
		return False
	table.hand_no += 1
	table.button = next(s for s in ((table.button + i) % SEATS for i in range(1, SEATS + 1)) if s in ready)
	deck = cards.deck()
	h    = table.hand = {'no': table.hand_no, 'deck': deck, 'board': [], 'street': 'preflop', 'bet': 0, 'min_raise': table.bb, 'to_act': None, 'deadline': 0, 'shown': [], 'results': [], 'players': {}}
	for s in ready:
		h['players'][s] = {'start': table.seats[s]['stack'], 'cards': [], 'bet': 0, 'total': 0, 'folded': False, 'allin': False, 'acted': False, 'can_raise': True, 'said': '', 'hand_name': ''}
	after = lambda s: next(x for x in ((s + i) % SEATS for i in range(1, SEATS + 1)) if x in ready)
	sb    = table.button if len(ready) == 2 else after(table.button) # heads up, the button posts the small blind
	bb    = after(sb)
	for _ in range(2):
		for s in [x for x in ((table.button + i) % SEATS for i in range(1, SEATS + 1)) if x in ready]:
			h['players'][s]['cards'].append(deck.pop())
	h['sb_seat'], h['bb_seat'] = sb, bb
	table.log.append(f'Hand #{table.hand_no}')
	pay(table, sb, table.sb)
	say(table, sb, f'posts the small blind {money(h["players"][sb]["bet"])}')
	pay(table, bb, table.bb)
	say(table, bb, f'posts the big blind {money(h["players"][bb]["bet"])}')
	for s in ready:
		if table.seats[s].get('new') and s not in (sb, bb) and len(ready) > 2:
			pay(table, s, table.bb)
			say(table, s, f'posts {money(h["players"][s]["bet"])} to come in')
		table.seats[s]['new'] = False
	h['bet']    = table.bb
	h['to_act'] = bb
	advance(table)
	return True


def tick(table: Table) -> bool:
	'''Run one step of a table's clock. Returns True if anything changed.'''

	now = time.time()
	h   = table.hand
	if h and h['street'] != 'done':
		seat = h['to_act']
		st   = table.seats[seat]
		if not st['user_id'] and now >= h['bot_at']:
			action, to = bot_decide(table, seat)
			act(table, seat, action, to)
			return True
		if st['user_id'] and now >= h['deadline']:
			act(table, seat, 'check' if options(table, seat)['check'] else 'fold')
			st['timeouts'] += 1
			if st['timeouts'] >= 2:
				st['out_since'] = now
				table.log.append(f'{st["name"]} is sitting out')
			return True
		return False
	if now < table.next_at:
		return False
	changed = False
	for i, st in enumerate(table.seats):
		if st and st['user_id'] and (st['stack'] == 0 or (st['out_since'] and now - st['out_since'] > SIT_OUT_MAX) or not hub.is_online(st['user_id'])):
			cash_out(table, i)
			changed = True
	before = [st and st['name'] for st in table.seats]
	seat_bots(table)
	changed |= before != [st and st['name'] for st in table.seats]
	had = table.hand is not None
	table.next_at = now + 2
	return start_hand(table) or changed or had


def turn(table: Table, seat: int):
	'''Give the action to a seat and start its clock.'''

	h = table.hand
	h['to_act']   = seat
	h['deadline'] = time.time() + ACT_TIME
	h['bot_at']   = time.time() + RNG.uniform(0.8, 2.2)


def view(table: Table, user_id: int) -> dict:
	'''Return the table as one player (or a watcher) sees it, hiding cards they should not see.'''

	h     = table.hand
	mine  = next((i for i, st in enumerate(table.seats) if st and user_id and st['user_id'] == user_id), None)
	seats = []
	for i, st in enumerate(table.seats):
		if not st:
			seats.append(None)
			continue
		p   = h['players'].get(i) if h else None
		out = {'name': st['name'], 'user_id': st['user_id'], 'stack': st['stack'], 'bot': not st['user_id'], 'away': bool(st['out_since']), 'in_hand': bool(p and not p['folded'])}
		if p:
			show = i == mine or i in h['shown']
			out.update(bet=p['bet'], folded=p['folded'], allin=p['allin'], said=p['said'], cards=p['cards'] if show else [None] * len(p['cards']) if not p['folded'] else [], hand_name=p['hand_name'] if show and h['street'] == 'done' else '')
		seats.append(out)
	out = {
		'game'    : 'poker',
		'id'      : table.id,
		'name'    : table.name,
		'label'   : table.label,
		'private' : table.private,
		'house'   : table.house,
		'bots'    : table.bots,
		'act_time': ACT_TIME,
		'sb'      : table.sb,
		'bb'      : table.bb,
		'min_buy' : table.min_buy,
		'max_buy' : table.max_buy,
		'seats'   : seats,
		'me'      : mine,
		'button'  : table.button,
		'log'     : table.log[-30:],
		'hand'    : None,
	}
	if h:
		out['hand'] = {'no': h['no'], 'board': h['board'], 'street': h['street'], 'pot': pot_total(h), 'to_act': h['to_act'], 'deadline': h['deadline'] if h['to_act'] is not None else None, 'results': h['results'], 'sb_seat': h['sb_seat'], 'bb_seat': h['bb_seat']}
		if mine is not None and h['to_act'] == mine:
			out['hand']['options'] = options(table, mine)
	if mine is not None:
		out['my_hand'] = cards.describe(cards.evaluate(h['players'][mine]['cards'] + h['board'])) if h and mine in h['players'] and len(h['board']) >= 3 and not h['players'][mine]['folded'] else ''
	return out
