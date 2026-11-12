#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/chat.py

import collections
import threading
import time

import arcade
import hub
from storage import DB_LOCK, db_connect, feed_add, feed_load, money


KEEP       = 100  # messages kept per room
TEXT_MAX   = 300
RATE_SPAN  = 5.0  # seconds
RATE_MAX   = 4    # messages per span
FEED_KEEP  = 60   # results kept in the live feed
FEED       = collections.deque(maxlen=FEED_KEEP) # every finished bet from everyone, newest last
ROOMS      = {}   # room ('lobby' or a table id) -> recent messages
TIMES      = {}   # user id -> recent post times
NEXT       = [0]  # last message id handed out
EMOJIS     = ('👍', '👏', '😂', '😮', '😭', '😡', '🔥', '💰', '🍀', '🙏') # reactions a seated player can send
REACTS     = {}   # table id -> recent reactions and dealer tips, only ever sent live
REACT_NEXT = [0]  # last reaction id handed out
REACT_TIME = {}   # user id -> recent reaction and tip times
BIG_WIN    = 10_000_000 # cents, a single result paying this much net goes to the acid-arcade-db activity feed


def add(room: str, user_id: int, name: str, text: str):
	'''Store a message in a room, keeping lobby messages in the database.'''

	with hub.LOCK:
		NEXT[0] += 1
		msg = {'id': NEXT[0], 'room': room, 'user_id': user_id, 'name': name, 'text': text, 'at': int(time.time()), 'system': user_id is None}
		if room == 'lobby':
			with DB_LOCK, db_connect() as c:
				msg['id'] = NEXT[0] = max(NEXT[0], c.execute('INSERT INTO chat(user_id, name, text, at) VALUES(?, ?, ?, ?)', (user_id, name, text, msg['at'])).lastrowid)
		ROOMS.setdefault(room, collections.deque(maxlen=KEEP)).append(msg)


def announce(name: str, profit: int, game: str, user_id: int = None):
	'''Put a finished bet in the live feed, which has its own tab so it never fills the chat, and a big win in the arcade activity feed.

	:param profit: what the player won (or with a negative amount, lost) on the bet
	'''

	if profit >= BIG_WIN:
		event(user_id, 'bigwin', f'won {money(profit)} at {game}')

	at = int(time.time())
	with hub.LOCK:
		with DB_LOCK, db_connect() as c:
			fid = feed_add(c, name, game, profit, at)
		FEED.append({'id': fid, 'name': name, 'game': game, 'net': profit, 'at': at})
	hub.notify()


def drop(room: str):
	'''Forget a closed table's chat.'''

	ROOMS.pop(room, None)


def event(user_id: int, kind: str, text: str):
	'''Send a line to the acid-arcade-db activity feed in the background, printing any error so play never notices. Guests and standalone mode send nothing.'''

	def send():
		try:
			arcade.event(user_id, kind, text)
		except Exception as e:
			print(f'arcade event for {user_id} failed: {e!r}')

	if arcade.enabled() and user_id:
		threading.Thread(target=send, daemon=True).start()


def feed_since(last: int) -> list:
	'''Return the feed results newer than an id.'''

	return [r for r in FEED if r['id'] > last]


def load():
	'''Load the recent lobby chat, under the players' current names, and the live feed from the database.'''

	with db_connect() as c:
		rows = c.execute('SELECT c.id, c.user_id, COALESCE(u.username, c.name) AS name, c.text, c.at FROM chat c LEFT JOIN users u ON u.id = c.user_id ORDER BY c.id DESC LIMIT ?', (KEEP,)).fetchall()
	room = ROOMS.setdefault('lobby', collections.deque(maxlen=KEEP))
	for r in reversed(rows):
		room.append({'id': r['id'], 'room': 'lobby', 'user_id': r['user_id'], 'name': r['name'], 'text': r['text'], 'at': r['at'], 'system': r['user_id'] is None})
	NEXT[0] = rows[0]['id'] if rows else 0
	FEED.extend(feed_load(FEED_KEEP))


def post(room: str, user_id: int, name: str, text: str) -> str:
	'''Post a message. Returns an error message or None.'''

	text = ' '.join((text or '').split())[:TEXT_MAX]
	if not text:
		return 'say something first'
	now   = time.time()
	times = [t for t in TIMES.get(user_id, []) if now - t < RATE_SPAN]
	if len(times) >= RATE_MAX:
		return 'slow down a little'
	TIMES[user_id] = times + [now]
	with hub.LOCK:
		if room != 'lobby' and room not in hub.TABLES:
			return 'that table is closed'
		add(room, user_id, name, text)
	hub.notify()


def react(room: str, name: str, emoji: str = None, tip: int = 0):
	'''Show an emoji or a dealer tip over a player's seat for everyone watching the table.'''

	with hub.LOCK:
		REACT_NEXT[0] += 1
		REACTS.setdefault(room, collections.deque(maxlen=20)).append({'id': REACT_NEXT[0], 'name': name, 'emoji': emoji, 'tip': tip})
	hub.notify()


def reacts_since(room: str, last: int) -> list:
	'''Return a table's reactions newer than an id.'''

	return [r for r in REACTS.get(room, ()) if r['id'] > last]


def react_throttled(user_id: int) -> bool:
	'''Return True if a player is sending reactions or tips too fast, counting this one when they are not.'''

	now   = time.time()
	times = [t for t in REACT_TIME.get(user_id, []) if now - t < RATE_SPAN]
	if len(times) >= RATE_MAX:
		return True
	REACT_TIME[user_id] = times + [now]
	return False


def since(room: str, last: int) -> list:
	'''Return a room's messages newer than an id.'''

	return [m for m in ROOMS.get(room, ()) if m['id'] > last]
