import sqlite3
from unittest.mock import patch

from app import UserStore, ValidationError, UsernameTaken, normalize_phone
import test_recovery
import unittest


class PhoneTests(unittest.TestCase):
    setUp = test_recovery.RecoveryTests.setUp
    link = test_recovery.RecoveryTests.link
    request = test_recovery.RecoveryTests.request
    def test_phone_login_and_duplicate_normalization(self):
        self.assertEqual(self.auth.users.find_identity('+7 (777) 123-45-67')['username'], 'Alice')
        status, token = self.auth.login({'username': '+77771234567', 'password': 'old-pass123'})
        self.assertEqual(status, 200)
        self.assertEqual(self.auth.username(token), 'Alice')
        with self.assertRaises(UsernameTaken):
            self.auth.users.register('Bob', 'password123', 'password123', '+7 777 123 45 67')
        self.assertEqual(self.auth.login({'username': '+77771234568', 'password': 'old-pass123'})[0], 401)

    def test_phone_required_and_strict_international_format(self):
        for value in [None, '', '87771234567', '+7abc7771234567', '+00000000', '+7', '++77771234567', 77771234567, '+1' + '2' * 15]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.auth.users.register('Bob', 'password123', 'password123', value)
        self.assertEqual(normalize_phone('+7 (777) 123-45-67'), '+77771234567')

    def test_recovery_phone_and_username_share_cooldown(self):
        self.recovery.bind(self.link(), '123')
        payload = self.request('+7 (777) 123-45-67')
        with self.assertRaises(Exception) as caught:
            self.recovery.request({'username': 'Alice'}, 'different-ip')
        self.assertEqual(caught.exception.status, 429)
        self.recovery.reset(payload, 'local')
        self.assertEqual(self.auth.login({'username': '+77771234567', 'password': 'new-pass123'})[0], 200)

    def test_old_database_migration_preserves_accounts(self):
        path = self.path / 'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, salt TEXT, password_hash TEXT)')
            db.execute("INSERT INTO users VALUES (1, 'Legacy', 'salt', 'hash')")
        users = UserStore(path)
        self.assertIsNone(users.find('Legacy')['phone'])
        self.assertEqual(users.find('Legacy')['password_hash'], 'hash')
        users.register('NewUser', 'password123', 'password123', '+447700900123')
        self.assertEqual(UserStore(path).find_identity('+44 7700 900123')['username'], 'NewUser')

