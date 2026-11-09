"""Family Budget Tracker: Python 3.10+ standard library, local classroom demo."""
import argparse
import os
import hashlib
import hmac
import secrets
import threading
import time
from http.cookies import SimpleCookie
import json
import re
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from recovery import Recovery, TelegramBot, RecoveryError, load_env

ROOT = Path(__file__).resolve().parent
CATEGORIES = {
    'income': ['Жалақы', 'Қосымша табыс', 'Басқа кіріс'],
    'expense': ['Азық-түлік', 'Көлік', 'Коммуналдық', 'Демалыс', 'Басқа шығыс'],
}


class ValidationError(ValueError):
    pass


class UsernameTaken(ValueError):
    pass


def hash_password(password, salt, iterations=600_000):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), iterations).hex()


def validate(payload):
    if not isinstance(payload, dict):
        raise ValidationError('The record must be a JSON object.')
    kind = payload.get('kind')
    if not isinstance(kind, str) or kind not in CATEGORIES:
        raise ValidationError('Choose income or expense.')
    category = payload.get('category')
    if category not in CATEGORIES[kind]:
        raise ValidationError('Choose a category that matches the transaction type.')
    amount = payload.get('amount')
    if not isinstance(amount, str) or not re.fullmatch(r'[0-9]{1,10}([.,][0-9]{1,2})?', amount.strip()):
        raise ValidationError('Enter a positive amount with up to two decimal places (for example, 1500.50).')
    parts = amount.strip().replace(',', '.').split('.')
    minor = int(parts[0]) * 100 + int((parts[1] if len(parts) == 2 else '').ljust(2, '0'))
    if not 1 <= minor <= 100_000_000_000:
        raise ValidationError('Amount must be between 0.01 and 1,000,000,000 KZT.')
    day = payload.get('date')
    try:
        if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError()
        date.fromisoformat(day)
    except ValueError:
        raise ValidationError('Enter a valid date in YYYY-MM-DD format.') from None
    note = payload.get('note', '')
    if not isinstance(note, str) or len(note.strip()) > 120:
        raise ValidationError('Note must contain no more than 120 characters.')
    return kind, category, minor, day, note.strip()



class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def normalize_phone(value):
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r'[+0-9 ()-]+', value):
        raise ValidationError('Введите номер в международном формате, например +7 777 123 45 67.')
    value = re.sub(r'[ ()-]', '', value)
    if not re.fullmatch(r'\+[1-9][0-9]{7,14}', value):
        raise ValidationError('Нужен код страны и знак +, например +7 777 123 45 67.')
    return value


class BudgetStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    def add(self, payload):
        values = validate(payload)
        with self.connect() as db:
            cur = db.execute('INSERT INTO transactions (kind, category, amount_minor, date, note) VALUES (?, ?, ?, ?, ?)', values)
            return dict(db.execute('SELECT * FROM transactions WHERE id = ?', (cur.lastrowid,)).fetchone())

    @staticmethod
    def transaction_id(value):
        if type(value) is not int or value <= 0:
            raise ValidationError('Invalid transaction ID.')
        return value

    def update(self, transaction_id, payload):
        transaction_id = self.transaction_id(transaction_id)
        values = validate(payload)
        with self.connect() as db:
            result = db.execute('UPDATE transactions SET kind=?, category=?, amount_minor=?, date=?, note=? WHERE id=?', (*values, transaction_id))
            if not result.rowcount:
                raise LookupError('Transaction not found.')
            return dict(db.execute('SELECT * FROM transactions WHERE id=?', (transaction_id,)).fetchone())

    def snapshot(self, filters=None):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM transactions ORDER BY date DESC, id DESC')]
        filters = filters or {}
        allowed = {'month', 'from', 'to', 'kind', 'category', 'min', 'max', 'q', 'sort'}
        if set(filters) - allowed or any(not isinstance(v, str) for v in filters.values()):
            raise ValidationError('Invalid report filters.')
        month = filters.get('month', '')
        if month:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}', month):
                raise ValidationError('Use YYYY-MM for month.')
            try:
                date.fromisoformat(month + '-01')
            except ValueError:
                raise ValidationError('Invalid month.') from None
        for key in ('from', 'to'):
            if filters.get(key):
                validate({'kind': 'income', 'category': CATEGORIES['income'][0], 'amount': '1', 'date': filters[key]})
        if filters.get('from') and filters.get('to') and filters['from'] > filters['to']:
            raise ValidationError('Start date must not follow end date.')
        if filters.get('kind') and filters['kind'] not in CATEGORIES:
            raise ValidationError('Invalid transaction type.')
        if filters.get('category') and filters['category'] not in sum(CATEGORIES.values(), []):
            raise ValidationError('Invalid category.')
        bounds = {}
        for key in ('min', 'max'):
            value = filters.get(key, '')
            if value:
                if not re.fullmatch(r'[0-9]{1,10}([.,][0-9]{1,2})?', value):
                    raise ValidationError('Invalid amount filter.')
                whole, _, fraction = value.replace(',', '.').partition('.')
                bounds[key] = int(whole) * 100 + int(fraction.ljust(2, '0'))
                if bounds[key] > 100_000_000_000:
                    raise ValidationError('Amount filter is too large.')
        if bounds.get('min', 0) > bounds.get('max', 100_000_000_000):
            raise ValidationError('Minimum amount must not exceed maximum.')
        if len(filters.get('q', '')) > 120:
            raise ValidationError('Search must contain no more than 120 characters.')
        sort = filters.get('sort') or 'newest'
        if sort not in ('newest', 'oldest', 'amount_desc', 'amount_asc'):
            raise ValidationError('Invalid sort order.')
        rows = [r for r in rows if
                (not month or r['date'].startswith(month + '-')) and
                (not filters.get('from') or r['date'] >= filters['from']) and
                (not filters.get('to') or r['date'] <= filters['to']) and
                (not filters.get('kind') or r['kind'] == filters['kind']) and
                (not filters.get('category') or r['category'] == filters['category']) and
                ('min' not in bounds or r['amount_minor'] >= bounds['min']) and
                ('max' not in bounds or r['amount_minor'] <= bounds['max']) and
                (filters.get('q', '').casefold() in r['note'].casefold())]
        if sort == 'oldest':
            rows.sort(key=lambda r: (r['date'], r['id']))
        elif sort.startswith('amount_'):
            rows.sort(key=lambda r: (r['amount_minor'], r['date'], r['id']), reverse=sort == 'amount_desc')
        income = sum(row['amount_minor'] for row in rows if row['kind'] == 'income')
        expense = sum(row['amount_minor'] for row in rows if row['kind'] == 'expense')
        grouped = {}
        for row in rows:
            if row['kind'] == 'expense':
                grouped[row['category']] = grouped.get(row['category'], 0) + row['amount_minor']
        return {'transactions': rows, 'summary': {'income_minor': income, 'expense_minor': expense,
                'balance_minor': income - expense, 'count': len(rows)},
                'expenses_by_category': dict(sorted(grouped.items(), key=lambda pair: -pair[1]))}

    def seed(self):
        entries = json.loads((ROOT / 'demo-data.json').read_text(encoding='utf-8'))
        values = [validate(entry) for entry in entries]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]:
                return False
            db.executemany('INSERT INTO transactions (kind, category, amount_minor, date, note) VALUES (?, ?, ?, ?, ?)', values)
        return True

    def delete(self, transaction_id):
        transaction_id = self.transaction_id(transaction_id)
        with self.connect() as db:
            if not db.execute('DELETE FROM transactions WHERE id = ?', (transaction_id,)).rowcount:
                raise LookupError('Transaction not found.')


class UserStore:
    """Separate SQLite file (data/users.sqlite3) with accounts created through registration."""
    USERNAME_RE = re.compile(r'[\w.\-]{3,40}')
    RESERVED = {'family'}  # the built-in household account

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )""")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
            if 'phone' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN phone TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_phone_unique ON users(phone) WHERE phone IS NOT NULL')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    def register(self, username, password, repeat, phone=None):
        if not isinstance(username, str) or not isinstance(password, str) or not isinstance(repeat, str):
            raise ValidationError('Enter a username and a password twice.')
        username = username.strip()
        if not self.USERNAME_RE.fullmatch(username):
            raise ValidationError('Username must be 3 to 40 characters: letters, digits, dot, dash or underscore.')
        if not 8 <= len(password) <= 128:
            raise ValidationError('Password must contain 8 to 128 characters.')
        if not hmac.compare_digest(password.encode(), repeat.encode()):
            raise ValidationError('Passwords do not match. An error occurred when repeating the password, please enter it again.')
        if username.lower() in self.RESERVED:
            raise UsernameTaken('This username is already in use. Please choose another one.')
        phone = normalize_phone(phone)
        salt = secrets.token_hex(16)
        try:
            with self.connect() as db:
                db.execute('INSERT INTO users (username, salt, password_hash, phone) VALUES (?, ?, ?, ?)',
                           (username, salt, hash_password(password, salt), phone))
        except sqlite3.IntegrityError:
            raise UsernameTaken('Username or phone is already in use. Логин или номер уже занят.') from None
        return username

    def find(self, username):
        with self.connect() as db:
            return db.execute('SELECT * FROM users WHERE username = ?', (username.strip(),)).fetchone()


    def find_identity(self, value):
        value = value.strip()
        if value.startswith('+'):
            try:
                phone = normalize_phone(value)
            except ValidationError:
                return None
            with self.connect() as db:
                return db.execute('SELECT * FROM users WHERE phone=?', (phone,)).fetchone()
        return self.find(value)


class Auth:
    """One household account. Salted password hash on disk; expiring sessions in memory."""
    ITERATIONS = 600_000
    SESSION_SECONDS = 8 * 60 * 60

    def __init__(self, store, users=None):
        self.store = store
        self.users = users
        self.lock = threading.Lock()
        self.sessions = {}
        self.session_users = {}
        self.failures = []
        self.registrations = []
        with store.connect() as db:
            if not db.execute('SELECT 1 FROM account WHERE id=1').fetchone():
                salt = secrets.token_hex(16)
                db.execute('INSERT INTO account VALUES (1, ?, ?, ?)',
                           ('family', salt, self.hash_password('Budget2026!', salt)))

    @classmethod
    def hash_password(cls, password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), cls.ITERATIONS).hex()

    def change_password(self, password):
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ValidationError('Password must contain 10 to 128 characters.')
        salt = secrets.token_hex(16)
        with self.lock, self.store.connect() as db:
            db.execute('UPDATE account SET salt=?, password_hash=? WHERE id=1',
                       (salt, self.hash_password(password, salt)))
            self.sessions.clear()

    def login(self, payload):
        if not isinstance(payload, dict):
            raise ValidationError('Enter your username and password.')
        username, password = payload.get('username'), payload.get('password')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 128:
            raise ValidationError('Enter your username and password.')
        username = username.strip()
        with self.lock:
            now = time.time()
            self.failures = [stamp for stamp in self.failures if now - stamp < 60]
            if len(self.failures) >= 5:
                return 429, None
            with self.store.connect() as db:
                account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
            registered = None if username.casefold() == account['username'].casefold() or not self.users else self.users.find_identity(username)
            record = registered or account
            valid_password = hmac.compare_digest(self.hash_password(password, record['salt']), record['password_hash'])
            if not valid_password or (not registered and username.casefold() != account['username'].casefold()):
                self.failures.append(now)
                return 401, None
            self.failures.clear()
            return 200, self._new_session(now, record['username'])

    def _new_session(self, now, username):
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        self.session_users = {key: name for key, name in self.session_users.items() if key in self.sessions}
        token = secrets.token_urlsafe(32)
        self.session_users[hashlib.sha256(token.encode()).hexdigest()] = username
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = now + self.SESSION_SECONDS
        return token

    def register(self, payload):
        """Returns (status, token_or_None, username_or_None). Signs the new user in on success."""
        if not isinstance(payload, dict):
            raise ValidationError('Enter a username and a password twice.')
        if self.users is None:
            raise ValidationError('Registration is not available.')
        with self.lock:
            now = time.time()
            self.registrations = [stamp for stamp in self.registrations if now - stamp < 60]
            if len(self.registrations) >= 10:
                return 429, None, None
            self.registrations.append(now)
            name = self.users.register(payload.get('username'), payload.get('password'), payload.get('password_repeat'), payload.get('phone'))
            return 201, self._new_session(now, name), name

    def authenticated(self, token):
        if not token:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            expiry = self.sessions.get(key, 0)
            if expiry <= time.time():
                self.sessions.pop(key, None)
                return False
            return True

    def username(self, token):
        if not self.authenticated(token):
            return None
        with self.lock:
            return self.session_users.get(hashlib.sha256(token.encode()).hexdigest())

    def logout(self, token):
        with self.lock:
            key = hashlib.sha256(token.encode()).hexdigest()
            self.sessions.pop(key, None)
            self.session_users.pop(key, None)


def make_server(db_path, port=8000, users_path=None, bot=None):
    store = BudgetStore(db_path)
    users = UserStore(users_path or Path(db_path).with_name('users.sqlite3'))
    auth = Auth(store, users)
    bot = bot or TelegramBot(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_BOT_USERNAME", ""))
    recovery = Recovery(auth, bot)

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, mime='application/json; charset=utf-8', headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def trusted_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def token(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                return cookie['budget_session'].value if 'budget_session' in cookie else ''
            except Exception:
                return ''

        def do_GET(self):
            if not self.trusted_host():
                return self.send(403, {'error': 'Use the local server address.'})
            path = urlsplit(self.path).path
            signed_in = auth.authenticated(self.token())
            if path == '/':
                if not signed_in:
                    return self.send(303, b'', headers={'Location': '/login'})
            elif path.startswith('/api/') and not signed_in:
                return self.send(401, {'error': 'Please sign in to continue.'})
            if path == '/api/budget':
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if any(len(v) != 1 for v in query.values()):
                        raise ValidationError('Repeated report filters are not allowed.')
                    return self.send(200, store.snapshot({k: v[0] for k, v in query.items()}))
                except ValidationError as error:
                    return self.send(400, {'error': str(error)})
                except sqlite3.Error:
                    return self.send(503, {'error': 'Database unavailable. Please try again.'})
            if path == '/api/categories':
                return self.send(200, CATEGORIES)
            if path == '/api/telegram':
                return self.send(200, recovery.status(auth.username(self.token())))
            if path == '/api/session':
                return self.send(200, {'username': auth.username(self.token())})
            if path in ('/login', '/register') and signed_in:
                return self.send(303, b'', headers={'Location': '/'})
            static = {'/forgot-password': ('forgot-password.html', 'text/html; charset=utf-8'),
                      '/forgot-password.js': ('forgot-password.js', 'text/javascript; charset=utf-8'),
                      '/': ('index.html', 'text/html; charset=utf-8'),
                      '/login': ('login.html', 'text/html; charset=utf-8'),
                      '/login.js': ('login.js', 'text/javascript; charset=utf-8'),
                      '/register': ('register.html', 'text/html; charset=utf-8'),
                      '/register.js': ('register.js', 'text/javascript; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path not in static:
                return self.send(404, {'error': 'Page not found.'})
            name, mime = static[path]
            try:
                return self.send(200, (ROOT / 'static' / name).read_bytes(), mime)
            except OSError:
                return self.send(503, {'error': 'Missing site files. Keep the static folder beside app.py.'})

        def do_POST(self):
            if not self.trusted_host():
                return self.send(403, {'error': 'Use the local server address.'})
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.send(403, {'error': 'Requests from other sites are not allowed.'})
            path = urlsplit(self.path).path
            public_routes = ('/api/login', '/api/register', '/api/password/request', '/api/password/reset')
            if path not in public_routes + ('/api/logout', '/api/telegram/link', '/api/transactions', '/api/transactions/delete', '/api/transactions/update'):
                return self.send(404, {'error': 'Route not found.'})
            if path not in public_routes and not auth.authenticated(self.token()):
                return self.send(401, {'error': 'Please sign in to continue.'})
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return self.send(415, {'error': 'JSON content type is required.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self.send(400, {'error': 'Invalid request size.'})
            if not 0 < length <= 8192:
                return self.send(413, {'error': 'Request is empty or too large.'})
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValidationError('The request must be a JSON object.')
                if path == '/api/telegram/link':
                    return self.send(200, recovery.link(auth.username(self.token()), payload.get('password')))
                if path == '/api/password/request':
                    return self.send(200, recovery.request(payload, self.client_address[0]))
                if path == '/api/password/reset':
                    recovery.reset(payload, self.client_address[0])
                    return self.send(200, {'ok': True})
                if path == '/api/login':
                    status, token = auth.login(payload)
                    if status != 200:
                        return self.send(status, {'error': 'Too many attempts. Try again in 60 seconds.' if status == 429 else 'Incorrect username or password.'})
                    return self.send(200, {'username': auth.username(token)}, headers={'Set-Cookie': f'budget_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={auth.SESSION_SECONDS}'})
                if path == '/api/register':
                    status, token, name = auth.register(payload)
                    if status != 201:
                        return self.send(status, {'error': 'Too many attempts. Try again in 60 seconds.'})
                    return self.send(201, {'username': name}, headers={'Set-Cookie': f'budget_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={auth.SESSION_SECONDS}'})
                if path == '/api/logout':
                    auth.logout(self.token())
                    return self.send(200, {'ok': True}, headers={'Set-Cookie': 'budget_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
                if path == '/api/transactions/update':
                    return self.send(200, store.update(payload.get('id'), payload))
                if path == '/api/transactions/delete':
                    tx_id = payload.get('id')
                    if type(tx_id) is not int:
                        return self.send(400, {'error': 'Invalid transaction ID.'})
                    store.delete(tx_id)
                    return self.send(200, {'ok': True})
                return self.send(201, store.add(payload))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self.send(400, {'error': 'Invalid JSON.'})
            except RecoveryError as error:
                return self.send(error.status, {'error': str(error)})
            except LookupError as error:
                return self.send(404, {'error': str(error)})
            except UsernameTaken as error:
                return self.send(409, {'error': str(error)})
            except ValidationError as error:
                return self.send(400, {'error': str(error)})
            except sqlite3.Error:
                return self.send(503, {'error': 'Database operation failed. Please try again.'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.auth = auth
    server.recovery = recovery
    server.bot = bot
    return server


def main():
    load_env(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'budget.sqlite3')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--set-password', action='store_true', help='Set the family account password interactively and exit; stop the server first')
    parser.add_argument('--seed', action='store_true', help='Load demo records only when the database is empty')
    args = parser.parse_args()
    store = BudgetStore(args.db)
    if args.set_password:
        from getpass import getpass
        password = getpass('New password (10–128 characters): ')
        if password != getpass('Repeat password: '):
            parser.error('Passwords do not match.')
        try:
            Auth(store).change_password(password)
        except ValidationError as error:
            parser.error(str(error))
        print('Password updated. Start the server again.')
        return
    if args.seed:
        print('Demo records loaded.' if store.seed() else 'Records already exist; demo data was not added again.', flush=True)
    server = make_server(args.db, args.port)
    print(f'Family Budget Tracker → http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Database: {args.db.resolve()} | Stop: Ctrl+C', flush=True)
    if server.bot.enabled:
        threading.Thread(target=server.bot.run, args=(server.recovery,), daemon=True).start()
        print('Telegram recovery bot enabled.', flush=True)
    else:
        print('Telegram recovery disabled: set TELEGRAM_BOT_TOKEN in .env.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.bot.stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
