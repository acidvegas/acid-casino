#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/cards.py

import secrets

from collections import Counter


RANKS      = '23456789TJQKA'
SUITS      = 'cdhs'
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}
RANK_NAME  = {2: 'Two', 3: 'Three', 4: 'Four', 5: 'Five', 6: 'Six', 7: 'Seven', 8: 'Eight', 9: 'Nine', 10: 'Ten', 11: 'Jack', 12: 'Queen', 13: 'King', 14: 'Ace'}
RANK_PLURAL = {**{k: v + 's' for k, v in RANK_NAME.items()}, 6: 'Sixes'}
RNG        = secrets.SystemRandom()


def deck(count: int = 1) -> list:
	'''Return a freshly shuffled list of cards like 'As' or 'Td', from one or more decks.'''

	cards = [r + s for r in RANKS for s in SUITS] * count
	RNG.shuffle(cards)
	return cards


def describe(score: tuple) -> str:
	'''Name a poker hand from its evaluate() score.'''

	cat, *v = score
	n, p = RANK_NAME, RANK_PLURAL
	return [
		lambda: f'{n[v[0]]} high',
		lambda: f'Pair of {p[v[0]]}',
		lambda: f'Two pair, {p[v[0]]} and {p[v[1]]}',
		lambda: f'Three {p[v[0]]}',
		lambda: f'Straight, {n[v[0]]} high',
		lambda: f'Flush, {n[v[0]]} high',
		lambda: f'Full house, {p[v[0]]} full of {p[v[1]]}',
		lambda: f'Four {p[v[0]]}',
		lambda: 'Royal flush' if v[0] == 14 else f'Straight flush, {n[v[0]]} high',
	][cat]()


def evaluate(cards: list) -> tuple:
	'''Score the best five-card poker hand in 5 to 7 cards. Higher tuples are better hands.'''

	vals    = sorted((RANK_VALUE[c[0]] for c in cards), reverse=True)
	by_suit = {}
	for c in cards:
		by_suit.setdefault(c[1], []).append(RANK_VALUE[c[0]])
	flush = None
	for suited in by_suit.values():
		if len(suited) >= 5:
			top = straight_top(set(suited))
			if top:
				return (8, top)
			flush = tuple(sorted(suited, reverse=True)[:5])
	counts = Counter(vals)
	quads  = [r for r, k in counts.items() if k == 4]
	trips  = sorted((r for r, k in counts.items() if k == 3), reverse=True)
	pairs  = sorted((r for r, k in counts.items() if k == 2), reverse=True)
	if quads:
		return (7, quads[0], max(v for v in vals if v != quads[0]))
	if trips and (len(trips) > 1 or pairs):
		return (6, trips[0], max(trips[1:] + pairs))
	if flush:
		return (5,) + flush
	top = straight_top(set(vals))
	if top:
		return (4, top)
	if trips:
		return (3, trips[0]) + tuple(v for v in vals if v != trips[0])[:2]
	if len(pairs) >= 2:
		return (2, pairs[0], pairs[1], max(v for v in vals if v not in pairs[:2]))
	if pairs:
		return (1, pairs[0]) + tuple(v for v in vals if v != pairs[0])[:3]
	return (0,) + tuple(vals[:5])


def straight_top(ranks: set) -> int:
	'''Return the top rank of the best straight in a set of ranks, or 0. The wheel (A-5) counts as 5 high.'''

	for top in range(14, 5, -1):
		if all(top - i in ranks for i in range(5)):
			return top
	return 5 if {14, 2, 3, 4, 5} <= ranks else 0
