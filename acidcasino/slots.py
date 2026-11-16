#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/slots.py

import itertools
import random

import chat
import hub
from cards import RNG
from storage import DB_LOCK, JACKPOT_SEED, NotEnough, credit, db_connect, house_move, jackpot_move, money, move, wager


BETS          = [100, 200, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000] # cents per spin, split evenly over the lines
JACKPOT_SHARE = 100           # 1/100 of every bet is put into the jackpot by the house, on top of the 96.5% the reels pay
JACKPOT_ODDS  = 5_000_000_000 # a spin hits the jackpot with a chance of bet in cents / this, so a $1,000 spin hits once in 50,000
ROWS          = 3
WILD          = 'W'
SCATTER       = 'S'
SYMBOLS       = {'C': 'Cherry', 'L': 'Lemon', 'O': 'Orange', 'P': 'Grapes', 'B': 'Bell', 'R': 'Bar', '7': 'Seven', WILD: 'Wild', SCATTER: 'Scatter'}

# How many of each symbol is on each of the five reels
REEL_COUNTS = [
	{'C': 7, 'L': 6, 'O': 5, 'P': 5, 'B': 3, 'R': 2, '7': 1, WILD: 1, SCATTER: 2},
	{'C': 7, 'L': 6, 'O': 5, 'P': 4, 'B': 3, 'R': 3, '7': 1, WILD: 2, SCATTER: 1},
	{'C': 7, 'L': 6, 'O': 5, 'P': 4, 'B': 3, 'R': 3, '7': 1, WILD: 2, SCATTER: 1},
	{'C': 7, 'L': 6, 'O': 5, 'P': 4, 'B': 3, 'R': 3, '7': 1, WILD: 2, SCATTER: 1},
	{'C': 7, 'L': 6, 'O': 5, 'P': 5, 'B': 3, 'R': 2, '7': 1, WILD: 1, SCATTER: 2},
]

# Line pays for 3, 4 and 5 in a row from the left, as multiples of the line bet
PAYS = {
	'C'  : (5, 15, 50),
	'L'  : (6, 20, 75),
	'O'  : (8, 25, 100),
	'P'  : (10, 30, 125),
	'B'  : (20, 60, 250),
	'R'  : (30, 125, 600),
	'7'  : (60, 300, 2_000),
	WILD : (100, 500, 5_000),
}

# Scatter pays anywhere on the screen, as multiples of the whole bet
SCATTER_PAYS = {3: 4, 4: 25, 5: 100}

LINES = [
	[1, 1, 1, 1, 1],
	[0, 0, 0, 0, 0],
	[2, 2, 2, 2, 2],
	[0, 1, 2, 1, 0],
	[2, 1, 0, 1, 2],
	[0, 0, 1, 2, 2],
	[2, 2, 1, 0, 0],
	[1, 0, 0, 0, 1],
	[1, 2, 2, 2, 1],
	[0, 1, 1, 1, 0],
]

# A ceiling on what one spin can win per unit bet, for the house cover rule: the top line prize plus the top scatter. The real most, found by trying every reel stop, is 528
WORST = PAYS[WILD][2] // len(LINES) + SCATTER_PAYS[5]


def build_strips() -> list:
	'''Lay each reel's symbols out in a fixed order, with scatters at least a screen apart so one reel never shows two.'''

	strips = []
	for i, counts in enumerate(REEL_COUNTS):
		order = random.Random(i)
		while True:
			strip = [s for s, n in counts.items() for _ in range(n)]
			order.shuffle(strip)
			at = [j for j, s in enumerate(strip) if s == SCATTER]
			if all((b - a) % len(strip) >= ROWS and (a - b) % len(strip) >= ROWS for a, b in itertools.combinations(at, 2)):
				break
		strips.append(strip)
	return strips


def info() -> dict:
	'''Return the pay table and lines for the client.'''

	return {'bets': BETS, 'jackpot_odds': JACKPOT_ODDS, 'jackpot_rtp': round(jackpot_rtp() * 100, 2), 'worst': WORST, 'lines': LINES, 'pays': PAYS, 'scatter_pays': SCATTER_PAYS, 'symbols': SYMBOLS, 'rtp': round(rtp() * 100, 2), 'strips': STRIPS}


def jackpot_rtp() -> float:
	'''Return what the jackpot adds to the return to player over the long run: every share put in is paid out in the end, plus the seed the house puts up for each win.'''

	return 1 / JACKPOT_SHARE + JACKPOT_SEED / JACKPOT_ODDS


def line_pay(syms: list) -> tuple:
	'''Pay for one line's five symbols, as (multiple of the line bet, how many symbols from the left made it).'''

	wilds = 0
	while wilds < len(syms) and syms[wilds] == WILD:
		wilds += 1
	best = (PAYS[WILD][wilds - 3], wilds) if wilds >= 3 else (0, 0)
	if wilds == len(syms) or syms[wilds] == SCATTER:
		return best
	sym = syms[wilds]
	run = wilds
	while run < len(syms) and syms[run] in (sym, WILD):
		run += 1
	return max(best, (PAYS[sym][run - 3], run) if run >= 3 else (0, 0))


def rtp() -> float:
	'''Work out the exact return to player from the reel strips and pay tables.'''

	probs = [{s: n / sum(c.values()) for s, n in c.items()} for c in REEL_COUNTS]
	lines = 0.0
	for combo in itertools.product(*[list(p) for p in probs]):
		pay = line_pay(list(combo))[0]
		if pay:
			chance = 1.0
			for reel, s in zip(probs, combo):
				chance *= reel[s]
			lines += chance * pay
	# Each reel shows at most one scatter, so the count is a sum of independent coin flips
	dist = [1.0]
	for c in REEL_COUNTS:
		p    = ROWS * c[SCATTER] / sum(c.values())
		dist = [(dist[k] if k < len(dist) else 0) * (1 - p) + (dist[k - 1] * p if k else 0) for k in range(len(dist) + 1)]
	scatter = sum(dist[k] * pay for k, pay in SCATTER_PAYS.items())
	return lines + scatter # every line costs one line bet, so the line pays are already per unit staked


def spin(user_id: int, name: str, bet: int) -> dict:
	'''Take a bet, spin the reels, pay any wins and maybe the jackpot.'''

	if type(bet) is not int or bet not in BETS: # 100.0 == 100, so a JSON float would pass the list check alone
		return {'error': 'pick one of the bet sizes'}
	if bet * WORST > hub.cover():
		return {'error': 'the house is too low on chips to cover a spin that big right now'}
	stops  = [RNG.randrange(len(s)) for s in STRIPS]
	grid   = [[STRIPS[r][(stops[r] + row) % len(STRIPS[r])] for row in range(ROWS)] for r in range(len(STRIPS))] # grid[reel][row]
	unit   = bet // len(LINES)
	hit    = RNG.randrange(JACKPOT_ODDS) < bet # the chance grows in step with the bet
	wins   = []
	for i, line in enumerate(LINES):
		mult, count = line_pay([grid[r][row] for r, row in enumerate(line)])
		if mult:
			wins.append({'line': i, 'count': count, 'pay': mult * unit})
	scatters = sum(col.count(SCATTER) for col in grid)
	if scatters in SCATTER_PAYS:
		wins.append({'line': None, 'count': scatters, 'pay': SCATTER_PAYS[scatters] * bet})
	payout = sum(w['pay'] for w in wins)
	won    = 0
	with DB_LOCK, db_connect() as c:
		try:
			bal = move(c, user_id, -bet, 'slots', f'spin {money(bet)}')
		except NotEnough:
			return {'error': 'not enough chips'}
		if payout:
			bal = credit(c, user_id, payout, bet, 'slots', f'won {money(payout)}')
		house_move(c, bet - payout - bet // JACKPOT_SHARE)
		pot = jackpot_move(c, bet // JACKPOT_SHARE, f'{money(bet // JACKPOT_SHARE)} from the house on a {money(bet)} spin')
		if hit:
			won = pot
			jackpot_move(c, -won, f'won by {name}')
			bal = credit(c, user_id, won, 0, 'slots', f'won the {money(won)} jackpot')
			house_move(c, -JACKPOT_SEED)
			pot = jackpot_move(c, JACKPOT_SEED, f'seeded with {money(JACKPOT_SEED)} from the house')
		wager(c, user_id, bet)
	if won:
		chat.add('lobby', None, 'casino', f'{name} hit the {money(won)} SLOTS JACKPOT')
		if payout + won - bet < chat.BIG_WIN:
			chat.event(user_id, 'bigwin', f'hit the {money(won)} jackpot at slots')
	chat.announce(name, payout + won - bet, 'slots', user_id)
	return {'stops': stops, 'grid': grid, 'wins': wins, 'payout': payout, 'jackpot': won, 'pot': pot, 'balance': bal}


STRIPS = build_strips()
