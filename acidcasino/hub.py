#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/hub.py

import queue
import secrets
import threading
import time

from storage import HOUSE_START


LOCK          = threading.RLock() # guards every table and the chat; always taken before the database lock
SUBS          = {}               # event stream queue -> user id (None for guests)
SEEN          = {}               # user id -> last time they had the site open
TABLES        = {}               # table id -> poker or blackjack table
HOUSE         = {}               # house bankroll and the richest player, refreshed every second
OFFLINE_AFTER = 120              # seconds without the site open before a seat is given up
CLOSE_AFTER   = 120              # seconds a player made table stays open with nobody at it
CODE_CHARS    = 'abcdefghjkmnpqrstuvwxyz23456789'
COVER_SHARE   = 1000             # no single bet may be able to win more than 1/1000 of the house bankroll


def cover() -> int:
	'''Return the most any one bet may be able to win, in cents, so the house can always pay and never goes under in one hit.'''

	return max(0, HOUSE.get('balance', HOUSE_START)) // COVER_SHARE


def is_online(user_id: int) -> bool:
	'''Return True if the user has the site open, or had it open a moment ago.'''

	return user_id in SUBS.values() or time.time() - SEEN.get(user_id, 0) < OFFLINE_AFTER


def loop(every):
	'''Background thread: run every table's clock, close abandoned tables and run the once a second job.

	:param every: called each second under the lock, returning True when it changed anything
	'''

	last = 0
	while True:
		time.sleep(0.25)
		with LOCK:
			changed = False
			now     = time.time()
			if now - last >= 1:
				last = now
				try:
					changed |= bool(every())
				except Exception as e:
					print(f'house error: {e!r}')
			for table in list(TABLES.values()):
				try:
					changed |= bool(table.tick())
				except Exception as e:
					print(f'table {table.id} error: {e!r}')
				if table.house:
					continue
				if table.humans():
					table.empty_since = None
				elif not table.busy():
					table.empty_since = table.empty_since or now
					if now - table.empty_since > CLOSE_AFTER:
						del TABLES[table.id]
						changed = True
			if changed:
				notify()


def new_code() -> str:
	'''Return an unused five letter table code.'''

	while True:
		code = ''.join(secrets.choice(CODE_CHARS) for _ in range(5))
		if code not in TABLES:
			return code


def notify():
	'''Wake every open event stream.'''

	for q in list(SUBS):
		try:
			q.put_nowait(1)
		except queue.Full:
			pass


def online() -> int:
	'''Return how many signed in players have the site open.'''

	return len({u for u in SUBS.values() if u})


def start(every):
	'''Start the table clock.

	:param every: job to run once a second
	'''

	threading.Thread(target=loop, args=(every,), daemon=True).start()
