#!/usr/bin/env python3
# acid-casino - Developed by acidvegas in Python (https://github.com/acidvegas)
# acidcasino/storage.py

import os
import sqlite3
import threading
import time


DATA_DIR      = os.environ.get('CASINO_DATA_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data'))
DB_PATH       = os.path.join(DATA_DIR, 'acidcasino.db')
DB_LOCK       = threading.RLock()
START_BALANCE = 100_000             # cents, $1,000 for every new account
HOUSE_START   = 100_000_000_000     # cents, the house opens with $1,000,000,000
REFILL_AMOUNT = 100_000             # cents, the free $1,000 for players running dry
REFILL_UNDER  = 25_000              # cents, a balance under $250 can ask for a refill
REFILL_WAIT   = 24 * 60 * 60        # seconds between asking for a refill and getting it
LOAN_MIN      = 1_000               # cents, the smallest loan
LOAN_FREEZE   = HOUSE_START * 4 // 10 # cents, no new loans while the house is under $400M (down 60%)
LOAN_AGE      = 24 * 60 * 60        # seconds an account must exist before it can borrow
LOAN_WAGERED  = 100_000             # cents an account must have bet before it can borrow
DEBT_CAP      = 5_000_000           # cents, nobody ever owes more than $50,000, interest included
CREDIT_TIERS  = [(500, 100_000, 0.10), (600, 500_000, 0.07), (700, 1_000_000, 0.05), (780, 2_500_000, 0.03), (None, 5_000_000, 0.02)] # (score under, credit limit in cents, daily interest)
SCORE_START   = 600
SCORE_RANGE   = (300, 850)
PAID_FAST     = (3 * 86400, 25)     # paying a debt off within 3 days of it opening raises the score 25
PAID_OK       = (7 * 86400, 10)     # within 7 days, 10
PAID_EVERY    = 86400               # a payoff can raise the score at most once a day, so borrowing $10 and paying it straight back does not farm it
LATE_AFTER    = 3 * 86400           # a debt open longer than this costs the score...
LATE_COST     = 5                   # ...this much for every further day
MAXED_COST    = 15                  # borrowing right up to the credit limit costs the score this much
REPAY_SHARE   = 4                   # 1/4 of every profit goes to the debt
SIGNUP_EVERY  = 86400               # one new account per IP address in this many seconds
TIPS          = (100, 500, 2_500, 10_000) # cents a player can tip the dealer
JACKPOT_SEED  = 5_000_000           # cents, the slots jackpot starts (and restarts after every win) at $50,000, put up by the house
JACKPOT_USER  = 0                   # ledger user id for moves in and out of the jackpot, which belongs to nobody yet
FEED_KEEP     = 200                 # live feed results kept in the database
LEADERBOARD_N = 25
REWARD_MAX    = 1_000_000           # cents, an arcade reward over $10,000 is never paid
ARCADE_SHOWN  = 5                   # recent arcade rewards shown in the wallet

os.makedirs(DATA_DIR, exist_ok=True)


class NotEnough(Exception):
	'''Raised when a debit would take a balance below zero.'''


def accrue(c: sqlite3.Connection):
	'''Charge interest for each full hour since a debt was last charged, at the owner's credit rate, and take score for debts running late.'''

	now = int(time.time())
	for row in c.execute('SELECT id, owed, owed_at, score, debt_since, late_charged FROM users WHERE owed > 0').fetchall():
		if row['owed_at'] <= now - 3600:
			hours = (now - row['owed_at']) // 3600
			rate  = credit_terms(row['score'])[1]
			owed  = row['owed']
			for _ in range(hours):
				owed = min(DEBT_CAP, owed + round(owed * ((1 + rate) ** (1 / 24) - 1)))
			c.execute('UPDATE users SET owed=?, owed_at=? WHERE id=?', (owed, row['owed_at'] + hours * 3600, row['id']))
		late = max(0, (now - row['debt_since'] - LATE_AFTER) // 86400)
		if late > row['late_charged']:
			score(c, row['id'], -LATE_COST * (late - row['late_charged']))
			c.execute('UPDATE users SET late_charged=? WHERE id=?', (late, row['id']))


def balance(c: sqlite3.Connection, user_id: int) -> int:
	'''Return a user's balance in cents.'''

	return c.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()['balance']


def borrow(user_id: int, amount: int) -> str:
	'''Take an instant loan from the house, up to the credit limit. Returns an error message, or None when the chips are paid out.'''

	if not isinstance(amount, int) or amount < LOAN_MIN:
		return f'borrow at least {money(LOAN_MIN)}'
	with DB_LOCK, db_connect() as c:
		user   = c.execute('SELECT owed, score, created_at, wagered FROM users WHERE id=?', (user_id,)).fetchone()
		closed = loans_closed(c, user)
		if closed:
			return closed
		now         = int(time.time())
		limit, rate = credit_terms(user['score'])
		room        = max(0, limit - user['owed'])
		if amount > room:
			return f'your credit limit is {money(limit)}, so you can borrow {money(room)} more' if room else f'you are at your {money(limit)} credit limit'
		if not user['owed']:
			c.execute('UPDATE users SET owed_at=?, debt_since=?, late_charged=0 WHERE id=?', (now, now, user_id))
		c.execute('UPDATE users SET owed = owed + ? WHERE id=?', (amount, user_id))
		c.execute('INSERT INTO loans(user_id, amount, requested_at, granted_at, kind) VALUES(?, ?, ?, ?, ?)', (user_id, amount, now, now, 'loan'))
		house_move(c, -amount)
		move(c, user_id, amount, 'loan', f'borrowed {money(amount)} at {rate:.0%} a day')
		if user['owed'] + amount >= limit:
			score(c, user_id, -MAXED_COST)


def credit(c: sqlite3.Connection, user_id: int, payout: int, stake: int, game: str, note: str) -> int:
	'''Pay out a finished bet and send the debt share of any profit to the house. Returns the new balance.

	:param payout: everything returned to the player, stake included
	:param stake: what the player put in for this bet
	'''

	owed   = c.execute('SELECT owed FROM users WHERE id=?', (user_id,)).fetchone()['owed']
	profit = payout - stake
	repay  = min(owed, profit // REPAY_SHARE) if profit > 0 and owed > 0 else 0
	if repay:
		pay_debt(c, user_id, repay)
		note += f', {money(repay)} to the debt'
	return move(c, user_id, payout - repay, game, note) if payout - repay else balance(c, user_id)


def credit_terms(score: int) -> tuple:
	'''Return (credit limit in cents, daily interest) for a credit score.'''

	return next((limit, rate) for under, limit, rate in CREDIT_TIERS if under is None or score < under)


def db_connect() -> sqlite3.Connection:
	'''Open a connection to the database.'''

	conn = sqlite3.connect(DB_PATH, timeout=10)
	conn.row_factory = sqlite3.Row
	return conn


def delete_user(c: sqlite3.Connection, user_id: int):
	'''Delete every row belonging to an account deleted in acid-arcade-db, its mirrored users row included.'''

	for table in ('sessions', 'ledger', 'loans', 'bj_bets', 'rl_bets', 'chat', 'poker_seats', 'topplers'):
		c.execute(f'DELETE FROM {table} WHERE user_id=?', (user_id,))
	c.execute('DELETE FROM users WHERE id=?', (user_id,))


def fame() -> list:
	'''Return everyone who has toppled the house, first to last, under their current name.'''

	with db_connect() as c:
		return [dict(r) for r in c.execute('SELECT t.user_id, COALESCE(u.username, t.username) AS username, t.worth, t.house, t.at FROM topplers t LEFT JOIN users u ON u.id = t.user_id ORDER BY t.at').fetchall()]


def feed_add(c: sqlite3.Connection, name: str, game: str, net: int, at: int) -> int:
	'''Save a live feed result and drop old ones. Returns its id.'''

	fid = c.execute('INSERT INTO feed(name, game, net, at) VALUES(?, ?, ?, ?)', (name, game, net, at)).lastrowid
	if fid % 50 == 0:
		c.execute('DELETE FROM feed WHERE id <= ?', (fid - FEED_KEEP,))
	return fid


def feed_load(limit: int) -> list:
	'''Return the most recent live feed results, oldest first.'''

	with db_connect() as c:
		return [dict(r) for r in reversed(c.execute('SELECT id, name, game, net, at FROM feed ORDER BY id DESC LIMIT ?', (limit,)).fetchall())]


def house_balance(c: sqlite3.Connection) -> int:
	'''Return the house bankroll in cents.'''

	return c.execute('SELECT balance FROM house WHERE id=1').fetchone()['balance']


def house_move(c: sqlite3.Connection, amount: int):
	'''Add (or with a negative amount, take) chips from the house bankroll.'''

	if amount:
		c.execute('UPDATE house SET balance = balance + ? WHERE id=1', (amount,))


def in_play(c: sqlite3.Connection, user_id: int) -> bool:
	'''Return True if the user has chips on a poker table or a blackjack or roulette bet in play.'''

	return bool(c.execute('SELECT 1 FROM poker_seats WHERE user_id=? UNION SELECT 1 FROM bj_bets WHERE user_id=? UNION SELECT 1 FROM rl_bets WHERE user_id=?', (user_id, user_id, user_id)).fetchone())


def init_db():
	'''Create tables.'''

	with DB_LOCK, db_connect() as c:
		c.execute('PRAGMA journal_mode=WAL')
		c.execute('''CREATE TABLE IF NOT EXISTS users (
			id            INTEGER PRIMARY KEY AUTOINCREMENT,
			username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
			password_hash TEXT NOT NULL,
			created_at    INTEGER NOT NULL,
			balance       INTEGER NOT NULL,
			owed          INTEGER NOT NULL DEFAULT 0,
			owed_at       INTEGER NOT NULL DEFAULT 0
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS feed (
			id   INTEGER PRIMARY KEY AUTOINCREMENT,
			name TEXT NOT NULL,
			game TEXT NOT NULL,
			net  INTEGER NOT NULL,
			at   INTEGER NOT NULL
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS sessions (
			token      TEXT PRIMARY KEY,
			user_id    INTEGER NOT NULL,
			expires_at INTEGER NOT NULL
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS ledger (
			id      INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id INTEGER NOT NULL,
			amount  INTEGER NOT NULL,
			balance INTEGER NOT NULL,
			game    TEXT NOT NULL,
			note    TEXT NOT NULL,
			at      INTEGER NOT NULL
		)''')
		c.execute('CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id, id)')
		c.execute('CREATE INDEX IF NOT EXISTS ledger_game ON ledger(user_id, game, id)')
		c.execute('''CREATE TABLE IF NOT EXISTS loans (
			id           INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id      INTEGER NOT NULL,
			amount       INTEGER NOT NULL,
			requested_at INTEGER NOT NULL,
			granted_at   INTEGER,
			kind         TEXT NOT NULL DEFAULT 'refill'
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS bj_bets (
			user_id  INTEGER NOT NULL,
			table_id TEXT NOT NULL,
			stake    INTEGER NOT NULL,
			PRIMARY KEY (user_id, table_id)
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS rl_bets (
			user_id  INTEGER NOT NULL,
			table_id TEXT NOT NULL,
			stake    INTEGER NOT NULL,
			PRIMARY KEY (user_id, table_id)
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS chat (
			id      INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id INTEGER,
			name    TEXT NOT NULL,
			text    TEXT NOT NULL,
			at      INTEGER NOT NULL
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS poker_seats (
			user_id  INTEGER PRIMARY KEY,
			table_id TEXT NOT NULL,
			seat     INTEGER NOT NULL,
			stack    INTEGER NOT NULL,
			buyin    INTEGER NOT NULL
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS poker_pots (
			table_id TEXT PRIMARY KEY,
			amount   INTEGER NOT NULL
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS house (
			id      INTEGER PRIMARY KEY CHECK (id = 1),
			balance INTEGER NOT NULL
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS topplers (
			user_id  INTEGER PRIMARY KEY,
			username TEXT NOT NULL,
			worth    INTEGER NOT NULL,
			house    INTEGER NOT NULL,
			at       INTEGER NOT NULL
		)''')
		c.execute('''CREATE TABLE IF NOT EXISTS rewards_paid (
			id      INTEGER PRIMARY KEY,
			user_id INTEGER NOT NULL,
			amount  INTEGER NOT NULL,
			at      INTEGER NOT NULL
		)''')
		c.execute('INSERT OR IGNORE INTO house(id, balance) VALUES(1, ?)', (HOUSE_START,))
		cols = {r['name'] for r in c.execute('PRAGMA table_info(users)')}
		if 'owed_at' not in cols: # databases from before loans had interest
			c.execute('ALTER TABLE users ADD COLUMN owed_at INTEGER NOT NULL DEFAULT 0')
			c.execute('UPDATE users SET owed_at=? WHERE owed > 0', (int(time.time()),))
		for col, kind in (('score', f'INTEGER NOT NULL DEFAULT {SCORE_START}'), ('debt_since', 'INTEGER NOT NULL DEFAULT 0'), ('late_charged', 'INTEGER NOT NULL DEFAULT 0'), ('paid_bonus_at', 'INTEGER NOT NULL DEFAULT 0'), ('wagered', 'INTEGER NOT NULL DEFAULT 0'), ('signup_ip', 'TEXT')):
			if col not in cols: # added with the credit score
				c.execute(f'ALTER TABLE users ADD COLUMN {col} {kind}')
				if col == 'debt_since':
					c.execute('UPDATE users SET debt_since=? WHERE owed > 0', (int(time.time()),))
				if col == 'wagered': # count what was already bet at slots and blackjack before this was tracked
					c.execute("UPDATE users SET wagered = (SELECT COALESCE(-SUM(amount), 0) FROM ledger WHERE ledger.user_id = users.id AND amount < 0 AND game IN ('slots', 'blackjack'))")
		if 'kind' not in {r['name'] for r in c.execute('PRAGMA table_info(loans)')}:
			c.execute('ALTER TABLE loans ADD COLUMN kind TEXT NOT NULL DEFAULT \'refill\'')
		if 'jackpot' not in {r['name'] for r in c.execute('PRAGMA table_info(house)')}: # the slots jackpot, seeded from the house the first time
			c.execute('ALTER TABLE house ADD COLUMN jackpot INTEGER NOT NULL DEFAULT 0')
			house_move(c, -JACKPOT_SEED)
			jackpot_move(c, JACKPOT_SEED, f'seeded with {money(JACKPOT_SEED)} from the house')


def jackpot(c: sqlite3.Connection) -> int:
	'''Return the slots jackpot in cents.'''

	return c.execute('SELECT jackpot FROM house WHERE id=1').fetchone()['jackpot']


def jackpot_move(c: sqlite3.Connection, amount: int, note: str) -> int:
	'''Add (or with a negative amount, take) chips from the slots jackpot and record it in the ledger. The caller moves the other side. Returns the new jackpot.'''

	c.execute('UPDATE house SET jackpot = jackpot + ? WHERE id=1', (amount,))
	pot = jackpot(c)
	c.execute('INSERT INTO ledger(user_id, amount, balance, game, note, at) VALUES(?, ?, ?, ?, ?, ?)', (JACKPOT_USER, amount, pot, 'jackpot', note, int(time.time())))
	return pot


def leaderboard(c: sqlite3.Connection, limit: int = LEADERBOARD_N) -> list:
	'''Return the richest players, counting chips on poker tables and minus what they owe.'''

	rows = c.execute('SELECT u.id, u.username, u.balance + COALESCE(p.stack, 0) - u.owed AS worth FROM users u LEFT JOIN poker_seats p ON p.user_id = u.id ORDER BY worth DESC, u.id LIMIT ?', (limit,)).fetchall()
	return [dict(r) for r in rows]


def loans_closed(c: sqlite3.Connection, user: sqlite3.Row) -> str:
	'''Return why a player can not borrow right now, or None when they can.'''

	if house_balance(c) < LOAN_FREEZE:
		return f'the house is under {money(LOAN_FREEZE)}, so it has stopped lending until it earns its way back'
	if time.time() - user['created_at'] < LOAN_AGE:
		return 'accounts can borrow once they are a day old'
	if user['wagered'] < LOAN_WAGERED:
		return f'bet {money(LOAN_WAGERED - user["wagered"])} more before you can borrow'
	if user['owed'] >= DEBT_CAP:
		return f'nobody can owe more than {money(DEBT_CAP)}'


def money(cents: int) -> str:
	'''Format cents as dollars, dropping .00.'''

	sign = '-' if cents < 0 else ''
	d, r = divmod(abs(cents), 100)
	return f'{sign}${d:,}' + (f'.{r:02d}' if r else '')


def move(c: sqlite3.Connection, user_id: int, amount: int, game: str, note: str) -> int:
	'''Add (or with a negative amount, take) chips and record it. Returns the new balance.'''

	cur = balance(c, user_id)
	if cur + amount < 0:
		raise NotEnough('not enough chips')
	c.execute('UPDATE users SET balance=? WHERE id=?', (cur + amount, user_id))
	c.execute('INSERT INTO ledger(user_id, amount, balance, game, note, at) VALUES(?, ?, ?, ?, ?, ?)', (user_id, amount, cur + amount, game, note, int(time.time())))
	return cur + amount


def pay_debt(c: sqlite3.Connection, user_id: int, amount: int):
	'''Pay part of a debt to the house, raising the score when it clears it in good time.'''

	c.execute('UPDATE users SET owed = owed - ? WHERE id=?', (amount, user_id))
	house_move(c, amount)
	row = c.execute('SELECT owed, debt_since, paid_bonus_at FROM users WHERE id=?', (user_id,)).fetchone()
	if row['owed']:
		return
	now  = int(time.time())
	took = now - row['debt_since']
	gain = next((pts for within, pts in (PAID_FAST, PAID_OK) if took <= within), 0)
	if gain and now - row['paid_bonus_at'] >= PAID_EVERY:
		score(c, user_id, gain)
		c.execute('UPDATE users SET paid_bonus_at=? WHERE id=?', (now, user_id))
	c.execute('UPDATE users SET debt_since=0, late_charged=0 WHERE id=?', (user_id,))


def prune_sessions():
	'''Delete expired sessions.'''

	with DB_LOCK, db_connect() as c:
		c.execute('DELETE FROM sessions WHERE expires_at < ?', (int(time.time()),))


def refill_request(user_id: int) -> str:
	'''Ask for the free refill. Returns an error message, or None when the request is queued.'''

	with DB_LOCK, db_connect() as c:
		if c.execute('SELECT 1 FROM loans WHERE user_id=? AND granted_at IS NULL', (user_id,)).fetchone():
			return 'you already have a refill on the way'
		if balance(c, user_id) >= REFILL_UNDER or in_play(c, user_id):
			return f'refills are only for players under {money(REFILL_UNDER)} with nothing in play'
		c.execute('INSERT INTO loans(user_id, amount, requested_at, kind) VALUES(?, ?, ?, ?)', (user_id, REFILL_AMOUNT, int(time.time()), 'refill'))


def refills_grant(user_id: int):
	'''Pay out any of the user's refills whose wait is over.'''

	now = int(time.time())
	with DB_LOCK, db_connect() as c:
		for row in c.execute('SELECT id, amount FROM loans WHERE user_id=? AND granted_at IS NULL AND requested_at <= ?', (user_id, now - REFILL_WAIT)).fetchall():
			c.execute('UPDATE loans SET granted_at=? WHERE id=?', (now, row['id']))
			move(c, user_id, row['amount'], 'bank', 'free refill')


def refund_bets():
	'''Give back blackjack and roulette bets from rounds a restart cut off.'''

	with DB_LOCK, db_connect() as c:
		for table, game in (('bj_bets', 'blackjack'), ('rl_bets', 'roulette')):
			for row in c.execute(f'SELECT user_id, stake FROM {table}').fetchall():
				if row['stake']:
					move(c, row['user_id'], row['stake'], game, f'refund of {money(row["stake"])} after a restart')
			c.execute(f'DELETE FROM {table}')


def rename_user(c: sqlite3.Connection, user_id: int, username: str):
	'''Give a mirrored acid-arcade-db account its current name, moving aside a stale row still holding that name.'''

	c.execute("UPDATE users SET username = username || '~' || id WHERE username=? AND id!=?", (username, user_id))
	c.execute('UPDATE users SET username=? WHERE id=?', (username, user_id))


def repay(user_id: int, amount: int) -> str:
	'''Pay debt back to the house from the balance. Returns an error message or None.'''

	if not isinstance(amount, int) or amount <= 0:
		return 'pick an amount to pay back'
	with DB_LOCK, db_connect() as c:
		owed   = c.execute('SELECT owed FROM users WHERE id=?', (user_id,)).fetchone()['owed']
		amount = min(amount, owed, balance(c, user_id))
		if not owed:
			return 'you do not owe anything'
		if not amount:
			return 'not enough chips'
		pay_debt(c, user_id, amount)
		move(c, user_id, -amount, 'loan', f'paid back {money(amount)}')


def rewards_pay(rewards: list) -> list:
	'''Pay chips earned in the rest of the arcade from the house, each reward once. Returns the ids to acknowledge: paid now or already paid before. Rewards for players without a casino account yet wait.

	:param rewards: the list from arcade.rewards_pending()
	'''

	done = []
	with DB_LOCK, db_connect() as c:
		for r in rewards:
			rid, user_id, amount, note = r.get('id'), r.get('user_id'), r.get('amount'), r.get('note')
			if type(rid) is not int or type(user_id) is not int or type(amount) is not int or not 0 < amount <= REWARD_MAX or not isinstance(note, str):
				print(f'skipped a bad arcade reward: {r!r}')
				continue
			if c.execute('SELECT 1 FROM rewards_paid WHERE id=?', (rid,)).fetchone():
				done.append(rid)
				continue
			if not c.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
				continue
			c.execute('INSERT INTO rewards_paid(id, user_id, amount, at) VALUES(?, ?, ?, ?)', (rid, user_id, amount, int(time.time())))
			move(c, user_id, amount, 'arcade', note[:200])
			house_move(c, -amount)
			done.append(rid)
	return done


def score(c: sqlite3.Connection, user_id: int, delta: int):
	'''Move a credit score, keeping it in range.'''

	c.execute('UPDATE users SET score = MAX(?, MIN(?, score + ?)) WHERE id=?', (*SCORE_RANGE, delta, user_id))


def signup_allowed(c: sqlite3.Connection, ip: str) -> bool:
	'''Return True if this IP address has not made an account recently.'''

	return not c.execute('SELECT 1 FROM users WHERE signup_ip=? AND created_at > ?', (ip, int(time.time()) - SIGNUP_EVERY)).fetchone()


def summaries(c: sqlite3.Connection, user_id: int = None) -> list:
	'''Return every player's (or one player's) worth, chips, debt, credit score and whether they toppled the house, worth counted like the leaderboard.'''

	return [dict(r) for r in c.execute('SELECT u.id, u.balance + COALESCE(p.stack, 0) - u.owed AS worth, u.balance + COALESCE(p.stack, 0) AS chips, u.owed, u.score, t.user_id IS NOT NULL AS toppled FROM users u LEFT JOIN poker_seats p ON p.user_id = u.id LEFT JOIN topplers t ON t.user_id = u.id WHERE ? IS NULL OR u.id = ?', (user_id, user_id)).fetchall()]


def tip(user_id: int, amount: int, game: str) -> str:
	'''Tip the dealer from the balance. Tips go to the house. Returns an error message or None.'''

	if not isinstance(amount, int) or amount not in TIPS:
		return 'pick a tip'
	with DB_LOCK, db_connect() as c:
		if balance(c, user_id) < amount:
			return 'not enough chips'
		move(c, user_id, -amount, game, f'tipped the dealer {money(amount)}')
		house_move(c, amount)


def wager(c: sqlite3.Connection, user_id: int, amount: int):
	'''Count chips a player put at risk, which loans need some of first.'''

	c.execute('UPDATE users SET wagered = wagered + ? WHERE id=?', (amount, user_id))


def wallet(user_id: int) -> dict:
	'''Return a user's balance, debt, credit and refill state, paying out any refill that is due first.'''

	refills_grant(user_id)
	with db_connect() as c:
		row     = c.execute('SELECT id, username, balance, owed, score, created_at, wagered, debt_since FROM users WHERE id=?', (user_id,)).fetchone()
		if not row:
			return None
		pending = c.execute('SELECT requested_at FROM loans WHERE user_id=? AND granted_at IS NULL', (user_id,)).fetchone()
		busy    = in_play(c, user_id)
		closed  = loans_closed(c, row)
		arcade  = [dict(r) for r in c.execute("SELECT id, amount, note, at FROM ledger WHERE user_id=? AND game='arcade' ORDER BY id DESC LIMIT ?", (user_id, ARCADE_SHOWN))]
	limit, rate = credit_terms(row['score'])
	out = {k: row[k] for k in ('id', 'username', 'balance', 'owed', 'score')}
	out['refill_ready_in'] = max(0, pending['requested_at'] + REFILL_WAIT - int(time.time())) if pending else None
	out['can_refill']      = not pending and not busy and row['balance'] < REFILL_UNDER
	out['limit']           = limit
	out['rate']            = rate
	out['loan_room']       = 0 if closed else max(0, min(limit, DEBT_CAP) - row['owed'])
	out['loans_closed']    = closed
	out['debt_days']       = (int(time.time()) - row['debt_since']) / 86400 if row['owed'] else None
	out['arcade']          = arcade
	return out


def wallet_stamp(user_id: int) -> tuple:
	'''Return everything wallet() is worked out from that can change without the clock moving, or None for a missing user, so a caller can skip wallet() while it stays the same.'''

	with db_connect() as c:
		row = c.execute('SELECT username, balance, owed, score, wagered, created_at > ? AS young, (SELECT MAX(id) FROM loans WHERE user_id=u.id) AS loan, EXISTS(SELECT 1 FROM poker_seats WHERE user_id=u.id) OR EXISTS(SELECT 1 FROM bj_bets WHERE user_id=u.id) OR EXISTS(SELECT 1 FROM rl_bets WHERE user_id=u.id) AS busy FROM users u WHERE id=?', (time.time() - LOAN_AGE, user_id)).fetchone()
	return tuple(row) if row else None
