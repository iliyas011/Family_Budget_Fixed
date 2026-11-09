"""Telegram account binding and one-time password recovery; standard library only."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from collections import defaultdict


class RecoveryError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def load_env(path):
    import os
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                if key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_BOT_USERNAME'):
                    os.environ.setdefault(key, value.strip().strip('"').strip("'"))


class TelegramBot:
    def __init__(self, token='', username=''):
        self.token = token
        self.username = username.lstrip('@')
        self.enabled = bool(token)
        self.stop = threading.Event()

    def api(self, method, **payload):
        request = urllib.request.Request(
            'https://api.telegram.org/bot' + self.token + '/' + method,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
        if not data.get('ok'):
            raise RuntimeError('Telegram request failed')
        return data['result']

    def send(self, chat_id, text):
        self.api('sendMessage', chat_id=chat_id, text=text)

    def handle(self, update, recovery):
        message = update.get('message', {})
        chat = message.get('chat', {})
        if chat.get('type') != 'private':
            return
        text = message.get('text', '')
        if not isinstance(text, str):
            return
        parts = text.split()
        if len(parts) == 2 and parts[0] == '/start' and parts[1].startswith('link_'):
            reply = recovery.bind(parts[1][5:], str(chat['id']))
        else:
            reply = ('Привяжите Telegram из своего аккаунта на сайте: введите текущий пароль, '
                     'откройте ссылку и нажмите Start. Для восстановления нажмите «Забыли пароль?» на сайте. '
                     'Поддержка: обратитесь к владельцу сайта.')
        self.send(chat['id'], reply)

    def run(self, recovery):
        offset = 0
        try:
            info = self.api('getMe')
            self.username = info['username']
        except Exception:
            print('Telegram bot unavailable: check token and internet connection.', flush=True)
            return
        while not self.stop.is_set():
            try:
                updates = self.api('getUpdates', offset=offset, timeout=20,
                                   allowed_updates=['message'])
                for update in updates:
                    self.handle(update, recovery)
                    offset = update['update_id'] + 1
            except Exception:
                # Never print exception objects: their URL can contain the bot token.
                print('Telegram connection unavailable; retrying.', flush=True)
                self.stop.wait(5)


class Recovery:
    TTL = 300
    MAX_ATTEMPTS = 5

    def __init__(self, auth, bot):
        self.auth = auth
        self.bot = bot
        self.lock = threading.RLock()
        self.key = secrets.token_bytes(32)
        self.links = {}
        self.codes = {}
        self.limits = defaultdict(list)
        with auth.users.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS telegram_accounts (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                chat_id TEXT NOT NULL UNIQUE)''')

    def digest(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def require_bot(self):
        if not self.bot.enabled:
            raise RecoveryError('Telegram-бот ещё не настроен владельцем сайта.', 503)
        if not self.bot.username:
            raise RecoveryError('Бот ещё подключается. Повторите через несколько секунд.', 503)

    def account(self, username):
        if not isinstance(username, str) or not 1 <= len(username.strip()) <= 80:
            raise RecoveryError('Введите логин.')
        username = username.strip()
        with self.auth.store.connect() as db:
            row = db.execute('SELECT * FROM account WHERE username = ? COLLATE NOCASE', (username,)).fetchone()
        return row or self.auth.users.find_identity(username)

    def throttle(self, key, count, seconds):
        now = time.time()
        # Remove idle entries so random usernames cannot grow memory indefinitely.
        self.limits = defaultdict(list, {k: [t for t in v if now-t < 900]
                                        for k, v in self.limits.items() if any(now-t < 900 for t in v)})
        stamps = [t for t in self.limits[key] if now-t < seconds]
        if len(stamps) >= count:
            raise RecoveryError('Слишком много попыток. Попробуйте позже.', 429)
        stamps.append(now)
        self.limits[key] = stamps

    def cleanup(self):
        now = time.time()
        self.links = {k: v for k, v in self.links.items() if v['expires'] > now}
        self.codes = {k: v for k, v in self.codes.items() if v['expires'] > now}

    def status(self, username):
        with self.auth.users.connect() as db:
            linked = db.execute('SELECT 1 FROM telegram_accounts WHERE username=?', (username,)).fetchone()
        return {'enabled': self.bot.enabled and bool(self.bot.username), 'linked': bool(linked)}

    def link(self, username, password):
        self.require_bot()
        with self.lock:
            self.cleanup()
            self.throttle('link:' + username.casefold(), 5, 300)
            row = self.account(username)
            if not isinstance(password, str) or len(password) > 128 or not row:
                raise RecoveryError('Введите текущий пароль.', 401)
            hashed = self.auth.hash_password(password, row['salt'])
            if not hmac.compare_digest(hashed, row['password_hash']):
                raise RecoveryError('Неверный текущий пароль.', 401)
            self.links = {k: v for k, v in self.links.items() if v['username'] != row['username']}
            token = secrets.token_urlsafe(24)
            self.links[self.digest(token)] = {'username': row['username'], 'expires': time.time() + self.TTL}
            return {'url': 'https://t.me/' + self.bot.username + '?start=link_' + token}

    def bind(self, token, chat_id):
        with self.lock:
            self.cleanup()
            link = self.links.pop(self.digest(token), None)
            if not link:
                return 'Ссылка недействительна или истекла. Создайте новую на сайте.'
            try:
                with self.auth.users.connect() as db:
                    db.execute('''INSERT INTO telegram_accounts(username, chat_id) VALUES (?, ?)
                        ON CONFLICT(username) DO UPDATE SET chat_id=excluded.chat_id''',
                               (link['username'], chat_id))
            except sqlite3.IntegrityError:
                return 'Этот Telegram уже привязан к другому аккаунту.'
            self.codes = {k: v for k, v in self.codes.items() if v['username'] != link['username']}
            return 'Telegram привязан. Теперь здесь можно получать коды восстановления пароля.'

    def request(self, payload, client):
        self.require_bot()
        with self.lock:
            self.cleanup()
            self.throttle('request-ip:' + client, 10, 900)
            username = payload.get('username')
            row = self.account(username)
            canonical = row['username'] if row else username.strip()
            self.throttle('request-user:' + canonical.casefold(), 1, 60)
            challenge = secrets.token_urlsafe(32)
            code = str(secrets.randbelow(10000)).zfill(4)
            with self.auth.users.connect() as db:
                chat = db.execute('SELECT chat_id FROM telegram_accounts WHERE username=?', (canonical,)).fetchone()
            # Invalidate prior codes on resend, even when the send fails.
            self.codes = {k: v for k, v in self.codes.items()
                          if v['username'].casefold() != canonical.casefold()}
            if row and chat:
                try:
                    self.bot.send(chat['chat_id'], 'Код восстановления Family Budget: ' + code +
                                  '\nДействует 5 минут. Никому не сообщайте код.')
                except Exception:
                    raise RecoveryError('Не удалось отправить код. Повторите через минуту.', 503) from None
                self.codes[self.digest(challenge)] = {
                    'username': canonical, 'hash': self.digest(challenge + ':' + code),
                    'expires': time.time() + self.TTL, 'attempts': 0}
            return {'challenge': challenge, 'message':
                    'Если аккаунт существует и Telegram привязан, код отправлен в бот. Код действует 5 минут.'}

    def reset(self, payload, client):
        with self.lock:
            self.cleanup()
            self.throttle('reset-ip:' + client, 30, 900)
            challenge, code = payload.get('challenge'), payload.get('code')
            if not isinstance(challenge, str) or len(challenge) > 128 or not isinstance(code, str) or not re.fullmatch(r'[0-9]{4}', code):
                raise RecoveryError('Введите код из четырёх цифр.')
            key = self.digest(challenge)
            item = self.codes.get(key)
            if not item:
                raise RecoveryError('Код неверный или истёк.')
            item['attempts'] += 1
            if not hmac.compare_digest(self.digest(challenge + ':' + code), item['hash']):
                if item['attempts'] >= self.MAX_ATTEMPTS:
                    self.codes.pop(key, None)
                raise RecoveryError('Код неверный или истёк.')
            password, repeat = payload.get('password'), payload.get('password_repeat')
            if not isinstance(password, str) or not 8 <= len(password) <= 128 or not isinstance(repeat, str):
                raise RecoveryError('Пароль должен содержать от 8 до 128 символов.')
            if not hmac.compare_digest(password.encode(), repeat.encode()):
                raise RecoveryError('Пароли не совпадают.')
            # The verified code cannot be used for unlimited password validation attempts.
            if item['attempts'] > self.MAX_ATTEMPTS:
                self.codes.pop(key, None)
                raise RecoveryError('Код неверный или истёк.')
            salt = secrets.token_hex(16)
            hashed = self.auth.hash_password(password, salt)
            username = item['username']
            with self.auth.lock:
                with self.auth.store.connect() as db:
                    family = db.execute('SELECT username FROM account WHERE id=1').fetchone()['username']
                    if username.casefold() == family.casefold():
                        db.execute('UPDATE account SET salt=?, password_hash=? WHERE id=1', (salt, hashed))
                    else:
                        with self.auth.users.connect() as users:
                            users.execute('UPDATE users SET salt=?, password_hash=? WHERE username=?', (salt, hashed, username))
                for session_key, name in list(self.auth.session_users.items()):
                    if name.casefold() == username.casefold():
                        self.auth.sessions.pop(session_key, None)
                        self.auth.session_users.pop(session_key, None)
            self.codes = {k: v for k, v in self.codes.items() if v['username'] != username}
            self.links = {k: v for k, v in self.links.items() if v['username'] != username}

