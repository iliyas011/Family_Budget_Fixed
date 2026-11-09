import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from app import Auth, BudgetStore, ValidationError, make_server


def entry(**overrides):
    return dict({'kind': 'expense', 'category': 'Азық-түлік', 'amount': '2500',
                 'date': '2026-11-02', 'note': 'Нан және сүт'}, **overrides)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = BudgetStore(self.path)

    def test_empty_budget(self):
        self.assertEqual(self.store.snapshot()['summary'], {'income_minor': 0, 'expense_minor': 0, 'balance_minor': 0, 'count': 0})

    def test_seed_totals_and_no_duplicate_seed(self):
        self.assertTrue(self.store.seed())
        self.assertFalse(self.store.seed())
        self.assertEqual(self.store.snapshot()['summary'], {'income_minor': 35000000, 'expense_minor': 4500000, 'balance_minor': 30500000, 'count': 5})

    def test_lesson_demo_totals(self):
        self.store.seed()
        self.store.add(entry(kind='income', category='Қосымша табыс', amount='10000'))
        self.store.add(entry())
        self.assertEqual(self.store.snapshot()['summary'], {'income_minor': 36000000, 'expense_minor': 4750000, 'balance_minor': 31250000, 'count': 7})

    def test_decimal_arithmetic_and_comma(self):
        self.store.add(entry(kind='income', category='Жалақы', amount='0.30'))
        self.store.add(entry(amount='0,10'))
        self.store.add(entry(amount='0.20'))
        self.assertEqual(self.store.snapshot()['summary']['balance_minor'], 0)

    def test_persists_after_reopening(self):
        self.store.add(entry())
        reopened = BudgetStore(self.path)
        self.assertEqual(reopened.snapshot()['transactions'][0]['note'], 'Нан және сүт')
        self.assertEqual(reopened.snapshot()['summary']['balance_minor'], -250000)

    def test_invalid_amounts_do_not_insert(self):
        for amount in ['0', '-1', '1.001', 'abc', 'NaN', '1e3', '1000000001', '2 500', '', '∞', 25, None, True]:
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                self.store.add(entry(amount=amount))
        self.assertEqual(self.store.snapshot()['summary']['count'], 0)

    def test_invalid_fields_do_not_insert(self):
        for patch in [{'kind': 'other'}, {'kind': []}, {'category': 'Жалақы'}, {'category': {}}, {'date': '2026-02-30'}, {'date': '2026-2-1'}, {'date': None}, {'note': 'x' * 121}, {'note': []}]:
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                self.store.add(entry(**patch))
        self.assertEqual(self.store.snapshot()['summary']['count'], 0)

    def test_boundaries_and_optional_note(self):
        self.store.add(entry(amount='0.01', note=''))
        self.store.add(entry(amount='1000000000', note='x' * 120, date='2024-02-29'))
        self.assertEqual(self.store.snapshot()['summary']['count'], 2)

    def test_order_and_category_aggregation(self):
        self.store.add(entry(amount='100', date='2026-11-01'))
        newest = self.store.add(entry(amount='250'))
        self.assertEqual(self.store.snapshot()['transactions'][0]['id'], newest['id'])
        self.assertEqual(self.store.snapshot()['expenses_by_category'], {'Азық-түлік': 35000})

    def test_sql_injection_is_stored_as_text(self):
        note = "'); DROP TABLE transactions; --"
        self.store.add(entry(note=note))
        self.store.add(entry())
        self.assertEqual(self.store.snapshot()['summary']['count'], 2)
        self.assertEqual(self.store.snapshot()['transactions'][1]['note'], note)

    def test_database_constraints(self):
        with self.store.connect() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO transactions(kind,category,amount_minor,date) VALUES('expense','Көлік',-1,'2026-11-02')")


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.tmp.name) / 'api.sqlite3', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''
        status, headers, _ = self.call('POST', '/api/login', json.dumps({'username': 'family', 'password': 'Budget2026!'}), {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method='GET', path='/api/budget', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, path, body, {'Cookie': self.cookie, **(headers or {})})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_http_add_read_and_reload(self):
        status, _, body = self.call('POST', '/api/transactions', json.dumps(entry()), {'Content-Type': 'application/json'})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)['amount_minor'], 250000)
        for _ in range(2):
            status, _, body = self.call()
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['summary']['balance_minor'], -250000)

    def test_http_validation_and_malformed_json(self):
        for body in [json.dumps(entry(amount='-10')), '{bad', '[]', 'null']:
            with self.subTest(body=body):
                status, _, response = self.call('POST', '/api/transactions', body, {'Content-Type': 'application/json'})
                self.assertEqual(status, 400)
                self.assertIn('error', json.loads(response))
        self.assertEqual(json.loads(self.call()[2])['summary']['count'], 0)

    def test_http_media_type_origin_and_host_guards(self):
        for extra, expected in [({'Content-Type': 'text/plain'}, 415), ({'Origin': 'https://example.com'}, 403), ({'Host': 'evil.example'}, 403)]:
            headers = {'Content-Type': 'application/json', **extra}
            self.assertEqual(self.call('POST', '/api/transactions', json.dumps(entry()), headers)[0], expected)
        self.assertEqual(self.call(headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(json.loads(self.call()[2])['summary']['count'], 0)

    def test_http_size_guard(self):
        self.assertEqual(self.call('POST', '/api/transactions', 'x' * 8193, {'Content-Type': 'application/json'})[0], 413)

    def test_static_allowlist_and_security_headers(self):
        for path in ['/', '/app.js', '/style.css', '/api/categories']:
            status, headers, _ = self.call(path=path)
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ['/schema.sql', '/../app.py', '/data/budget.sqlite3', '/does-not-exist']:
            self.assertEqual(self.call(path=path)[0], 404)

    def test_unauthenticated_pages_and_api_are_protected(self):
        for path in ['/api/budget', '/api/categories', '/api/session']:
            self.assertEqual(self.call(path=path, headers={'Cookie': ''})[0], 401)
        status, headers, _ = self.call(path='/', headers={'Cookie': ''})
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/login')
        self.assertEqual(self.call('POST', '/api/transactions', json.dumps(entry()), {'Content-Type': 'application/json', 'Cookie': ''})[0], 401)
        status, _, body = self.call(path='/login', headers={'Cookie': ''})
        self.assertEqual(status, 200)
        self.assertIn(b'lang="en"', body)

    def test_login_wrong_credentials_and_cookie_flags(self):
        for patch in [{'username': 'wrong', 'password': 'Budget2026!'}, {'username': 'family', 'password': 'wrong'}]:
            status, headers, _ = self.call('POST', '/api/login', json.dumps(patch), {'Content-Type': 'application/json', 'Cookie': ''})
            self.assertEqual(status, 401)
            self.assertNotIn('Set-Cookie', headers)
        status, headers, _ = self.call('POST', '/api/login', json.dumps({'username': 'family', 'password': 'Budget2026!'}), {'Content-Type': 'application/json', 'Cookie': ''})
        self.assertEqual(status, 200)
        for flag in ['HttpOnly', 'SameSite=Strict', 'Max-Age=28800']:
            self.assertIn(flag, headers['Set-Cookie'])
        self.assertNotEqual(headers['Set-Cookie'].split(';')[0], self.cookie)

    def test_logout_revokes_token(self):
        self.assertEqual(self.call('POST', '/api/logout', '{}', {'Content-Type': 'application/json'})[0], 200)
        self.assertEqual(self.call()[0], 401)
        self.assertEqual(self.call('POST', '/api/transactions', json.dumps(entry()), {'Content-Type': 'application/json'})[0], 401)

    def test_expired_and_forged_sessions(self):
        self.assertEqual(self.call(headers={'Cookie': 'budget_session=forged'})[0], 401)
        for key in self.server.auth.sessions:
            self.server.auth.sessions[key] = 0
        self.assertEqual(self.call()[0], 401)

    def test_login_rate_limit_and_cross_origin(self):
        for _ in range(5):
            self.assertEqual(self.call('POST', '/api/login', json.dumps({'username': 'family', 'password': 'wrong'}), {'Content-Type': 'application/json'})[0], 401)
        self.assertEqual(self.call('POST', '/api/login', json.dumps({'username': 'family', 'password': 'Budget2026!'}), {'Content-Type': 'application/json'})[0], 429)
        self.assertEqual(self.call('POST', '/api/login', '{}', {'Content-Type': 'application/json', 'Origin': 'https://example.com'})[0], 403)
        self.server.auth.failures = [0] * 5
        self.assertEqual(self.call('POST', '/api/login', json.dumps({'username': 'family', 'password': 'Budget2026!'}), {'Content-Type': 'application/json'})[0], 200)

    def test_password_hash_persists_and_new_server_auth_has_no_sessions(self):
        auth = self.server.auth
        with auth.store.connect() as db:
            account = dict(db.execute('SELECT * FROM account').fetchone())
        self.assertNotIn('Budget2026!', str(account))
        self.assertEqual(len(account['salt']), 32)
        self.assertEqual(len(account['password_hash']), 64)
        restarted = Auth(BudgetStore(auth.store.path))
        self.assertFalse(restarted.authenticated(self.cookie.split('=', 1)[1]))
        self.assertEqual(restarted.login({'username':'family', 'password':'Budget2026!'})[0], 200)
        self.assertEqual(auth.store.snapshot()['summary']['count'], 0)

    def test_password_change_invalidates_old_password_and_sessions(self):
        auth = self.server.auth
        with self.assertRaises(ValidationError):
            auth.change_password('short')
        auth.change_password('DifferentPassword42!')
        self.assertEqual(self.call()[0], 401)
        self.assertEqual(auth.login({'username':'family', 'password':'Budget2026!'})[0], 401)
        self.assertEqual(auth.login({'username':'family', 'password':'DifferentPassword42!'})[0], 200)
        reopened = Auth(BudgetStore(auth.store.path))
        self.assertEqual(reopened.login({'username':'family', 'password':'DifferentPassword42!'})[0], 200)

    def test_registration_flow(self):
        hdr = {'Content-Type': 'application/json', 'Cookie': ''}
        reg = lambda **kw: self.call('POST', '/api/register', json.dumps(dict({'username': 'aigerim', 'password': 'secret-pass', 'password_repeat': 'secret-pass', 'phone': '+77771234567' if kw.get('username', 'aigerim') != 'bolat' else '+77771234568'}, **kw)), hdr)
        self.assertEqual(self.call(path='/register', headers={'Cookie': ''})[0], 200)
        status, headers, _ = reg(password_repeat='different')
        self.assertEqual(status, 400)
        self.assertNotIn('Set-Cookie', headers)
        status, headers, _ = reg()
        self.assertEqual(status, 201)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        for name in ['aigerim', 'AIGERIM', 'family']:
            status, _, body = reg(username=name)
            self.assertEqual(status, 409)
            self.assertIn('already in use', json.loads(body)['error'])
        self.assertEqual(reg(username='bolat')[0], 201)  # same password is allowed
        self.assertEqual(reg(username='ab')[0], 400)
        self.assertEqual(reg(username='dana', password='short', password_repeat='short')[0], 400)
        login = lambda u, p: self.call('POST', '/api/login', json.dumps({'username': u, 'password': p}), hdr)[0]
        self.assertEqual(login('aigerim', 'secret-pass'), 200)
        self.assertEqual(login('aigerim', 'wrong-pass'), 401)
        self.assertEqual(login('family', 'Budget2026!'), 200)
        self.assertTrue((Path(self.server.auth.users.path)).name == 'users.sqlite3')
        with sqlite3.connect(self.server.auth.users.path) as db:
            row = db.execute("SELECT password_hash FROM users WHERE username='aigerim'").fetchone()
        self.assertNotIn('secret-pass', row[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
