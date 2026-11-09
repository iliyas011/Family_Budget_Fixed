import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

from app import Auth, BudgetStore, UserStore, make_server
from recovery import Recovery, RecoveryError, TelegramBot


class FakeBot:
    enabled = True
    username = 'test_budget_bot'
    def __init__(self):
        self.messages = []
    def send(self, chat_id, text):
        self.messages.append((chat_id, text))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.auth = Auth(BudgetStore(self.path / 'budget.sqlite3'), UserStore(self.path / 'users.sqlite3'))
        _, self.token, _ = self.auth.register({'username': 'Alice', 'phone': '+7 777 123 45 67', 'password': 'old-pass123', 'password_repeat': 'old-pass123'})
        self.bot = FakeBot()
        self.recovery = Recovery(self.auth, self.bot)

    def link(self, username='Alice', password='old-pass123'):
        url = self.recovery.link(username, password)['url']
        token = parse_qs(urlsplit(url).query)['start'][0][5:]
        return token

    def request(self, username='Alice'):
        result = self.recovery.request({'username': username}, 'local')
        code = re.search(r': ([0-9]{4})', self.bot.messages[-1][1]).group(1)
        return {'challenge': result['challenge'], 'code': code,
                'password': 'new-pass123', 'password_repeat': 'new-pass123'}

    def test_registration_login_normalization_and_session_identity(self):
        status, token = self.auth.login({'username': ' ALICE ', 'password': 'old-pass123'})
        self.assertEqual(status, 200)
        self.assertEqual(self.auth.username(token), 'Alice')
        self.assertEqual(self.auth.login({'username': ' FAMILY ', 'password': 'Budget2026!'})[0], 200)

    def test_bind_requires_password_is_one_time_and_persists(self):
        with self.assertRaises(RecoveryError):
            self.recovery.link('Alice', 'wrong')
        token = self.link()
        self.assertIn('привязан', self.recovery.bind(token, '123'))
        self.assertTrue(self.recovery.status('alice')['linked'])
        self.assertIn('недействительна', self.recovery.bind(token, '456'))
        reopened = Recovery(self.auth, self.bot)
        self.assertTrue(reopened.status('ALICE')['linked'])
        self.assertIn('недействительна', reopened.bind(self.link(), '456'))

    def test_full_reset_changes_password_revokes_only_target_sessions_and_persists(self):
        other_status, other = self.auth.login({'username': 'family', 'password': 'Budget2026!'})
        self.assertEqual(other_status, 200)
        self.recovery.bind(self.link(), '123')
        payload = self.request('alice')
        self.assertNotIn('code', next(iter(self.recovery.codes.values())))
        self.recovery.reset(payload, 'local')
        self.assertFalse(self.auth.authenticated(self.token))
        self.assertTrue(self.auth.authenticated(other))
        self.assertEqual(self.auth.login({'username': 'Alice', 'password': 'old-pass123'})[0], 401)
        reopened = Auth(BudgetStore(self.path / 'budget.sqlite3'), UserStore(self.path / 'users.sqlite3'))
        self.assertEqual(reopened.login({'username': 'Alice', 'password': 'new-pass123'})[0], 200)
        with self.assertRaises(RecoveryError):
            self.recovery.reset(payload, 'local')

    def test_wrong_codes_lock_after_five_attempts(self):
        self.recovery.bind(self.link(), '123')
        payload = self.request()
        wrong = '0000' if payload['code'] != '0000' else '0001'
        for _ in range(5):
            with self.assertRaises(RecoveryError):
                self.recovery.reset(dict(payload, code=wrong), 'local')
        with self.assertRaises(RecoveryError):
            self.recovery.reset(payload, 'local')

    def test_expired_codes_and_links(self):
        token = self.link()
        for value in self.recovery.links.values():
            value['expires'] = 0
        self.assertIn('недействительна', self.recovery.bind(token, '123'))
        self.recovery.bind(self.link(), '123')
        payload = self.request()
        for value in self.recovery.codes.values():
            value['expires'] = 0
        with self.assertRaises(RecoveryError):
            self.recovery.reset(payload, 'local')

    def test_resend_cancels_old_code_and_cooldown(self):
        self.recovery.bind(self.link(), '123')
        first = self.request()
        with self.assertRaises(RecoveryError) as caught:
            self.recovery.request({'username': 'ALICE'}, 'other-ip')
        self.assertEqual(caught.exception.status, 429)
        self.recovery.limits.clear()
        second = self.request()
        with self.assertRaises(RecoveryError):
            self.recovery.reset(first, 'local')
        self.recovery.reset(second, 'local')

    def test_unknown_and_unbound_have_same_public_response_and_no_messages(self):
        a = self.recovery.request({'username': 'Alice'}, 'local')
        b = self.recovery.request({'username': 'Nobody'}, 'local')
        self.assertEqual(a['message'], b['message'])
        self.assertEqual(len(a['challenge']), len(b['challenge']))
        self.assertEqual(self.bot.messages, [])
        self.assertEqual(self.recovery.codes, {})

    def test_family_password_reset(self):
        self.recovery.bind(self.link('family', 'Budget2026!'), '456')
        self.recovery.reset(self.request('family'), 'local')
        self.assertEqual(self.auth.login({'username': 'family', 'password': 'new-pass123'})[0], 200)
        self.assertEqual(self.auth.login({'username': 'family', 'password': 'Budget2026!'})[0], 401)

    def test_bot_disabled_and_delivery_failure(self):
        self.bot.enabled = False
        with self.assertRaises(RecoveryError) as caught:
            self.recovery.request({'username': 'Alice'}, 'local')
        self.assertEqual(caught.exception.status, 503)
        self.bot.enabled = True
        self.recovery.bind(self.link(), '123')
        with patch.object(self.bot, 'send', side_effect=RuntimeError('private')):
            with self.assertRaises(RecoveryError) as caught:
                self.recovery.request({'username': 'Alice'}, 'local')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.recovery.codes, {})

    def test_bot_accepts_only_private_binding(self):
        token = self.link()
        bot = TelegramBot()
        with patch.object(bot, 'send') as send:
            bot.handle({'message': {'chat': {'id': 123, 'type': 'group'}, 'text': '/start link_' + token}}, self.recovery)
            self.assertFalse(self.recovery.status('Alice')['linked'])
            self.assertFalse(send.called)
            bot.handle({'message': {'chat': {'id': 123, 'type': 'private'}, 'text': '/start link_' + token}}, self.recovery)
            self.assertTrue(self.recovery.status('Alice')['linked'])
            send.assert_called_once()

    def test_invalid_password_and_leading_zero_code(self):
        self.recovery.bind(self.link(), '123')
        with patch('recovery.secrets.randbelow', return_value=7):
            payload = self.request()
        self.assertEqual(payload['code'], '0007')
        with self.assertRaises(RecoveryError):
            self.recovery.reset(dict(payload, password_repeat='different'), 'local')
        self.recovery.reset(payload, 'local')

    def test_ip_limit(self):
        for index in range(10):
            self.recovery.request({'username': 'unknown' + str(index)}, 'local')
        with self.assertRaises(RecoveryError) as caught:
            self.recovery.request({'username': 'another'}, 'local')
        self.assertEqual(caught.exception.status, 429)


class HandlerTests(unittest.TestCase):
    """Exercise real HTTP handlers through in-memory streams; no listening socket."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        def factory(address, handler):
            return SimpleNamespace(handler=handler, server_port=8000)
        with patch('app.ThreadingHTTPServer', side_effect=factory):
            self.server = make_server(Path(self.tmp.name) / 'budget.sqlite3', bot=FakeBot())

    def call(self, path, payload=None, cookie='', method=None):
        handler = object.__new__(self.server.handler)
        handler.server = self.server
        handler.path = path
        handler.client_address = ('local', 1)
        handler.headers = {'Host': '127.0.0.1:8000', 'Cookie': cookie,
                           'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:8000'}
        body = json.dumps(payload).encode() if payload is not None else b''
        handler.headers['Content-Length'] = str(len(body))
        handler.rfile = io.BytesIO(body)
        result = {}
        def send(status, data, mime=None, headers=None):
            result.update(status=status, data=data, headers=headers or {})
        handler.send = send
        (handler.do_POST if method == 'POST' or payload is not None else handler.do_GET)()
        return result

    def test_register_login_username_session_and_static_routes(self):
        reg = self.call('/api/register', {'username': 'Alice', 'phone': '+7 777 123 45 67', 'password': 'old-pass123', 'password_repeat': 'old-pass123'})
        self.assertEqual(reg['status'], 201)
        cookie = reg['headers']['Set-Cookie'].split(';')[0]
        self.assertEqual(self.call('/api/session', cookie=cookie)['data']['username'], 'Alice')
        login = self.call('/api/login', {'username': ' ALICE ', 'password': 'old-pass123'})
        self.assertEqual(login['status'], 200)
        self.assertEqual(login['data']['username'], 'Alice')
        for route in ['/login', '/register', '/forgot-password', '/forgot-password.js', '/register.js']:
            self.assertEqual(self.call(route)['status'], 200)
        self.assertEqual(self.call('/api/telegram')['status'], 401)
        self.assertEqual(self.call('/api/password/request', [], method='POST')['status'], 400)

    def test_reset_through_routes(self):
        reg = self.call('/api/register', {'username': 'Alice', 'phone': '+7 777 123 45 67', 'password': 'old-pass123', 'password_repeat': 'old-pass123'})
        cookie = reg['headers']['Set-Cookie'].split(';')[0]
        self.assertEqual(self.call('/api/telegram/link', {'password': 'wrong'}, cookie)['status'], 401)
        link = self.call('/api/telegram/link', {'password': 'old-pass123'}, cookie)
        token = parse_qs(urlsplit(link['data']['url']).query)['start'][0][5:]
        self.server.recovery.bind(token, '123')
        requested = self.call('/api/password/request', {'username': 'alice'})
        self.assertEqual(requested['status'], 200)
        code = re.search(r': ([0-9]{4})', self.server.bot.messages[-1][1]).group(1)
        result = self.call('/api/password/reset', {'challenge': requested['data']['challenge'], 'code': code,
                            'password': 'new-pass123', 'password_repeat': 'new-pass123'})
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.call('/api/session', cookie=cookie)['status'], 401)
        self.assertEqual(self.call('/api/login', {'username': 'alice', 'password': 'new-pass123'})['status'], 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)

