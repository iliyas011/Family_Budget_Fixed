"""Regression tests for editing, deletion and filtered money reports."""
import json
import unittest
from urllib.parse import urlencode
from test_app import StoreTests, APITests, entry
from app import BudgetStore, ValidationError


# Reuse fixtures without rediscovering the original tests in this module.
class ReportStoreTests(unittest.TestCase):
    setUp = StoreTests.setUp

    def populate(self):
        self.store.add(entry(amount='10', date='2026-10-31', note='Bus', category='Көлік'))
        self.store.add(entry(amount='20', date='2026-11-01', note='Market'))
        self.store.add(entry(amount='30', date='2026-11-30', note='MARKET food'))
        self.store.add(entry(amount='100', date='2026-12-01', kind='income', category='Жалақы'))

    def test_edit_changes_all_fields_and_totals(self):
        row = self.store.add(entry())
        updated = self.store.update(row['id'], entry(kind='income', category='Жалақы', amount='12.34', date='2026-10-01', note='Updated'))
        self.assertEqual(updated['id'], row['id'])
        self.assertEqual((updated['note'], updated['date'], updated['amount_minor']), ('Updated', '2026-10-01', 1234))
        self.assertEqual(self.store.snapshot()['summary'], dict(income_minor=1234, expense_minor=0, balance_minor=1234, count=1))
        self.assertEqual(self.store.snapshot()['expenses_by_category'], {})

    def test_invalid_edit_preserves_record(self):
        row = self.store.add(entry())
        with self.assertRaises(ValidationError):
            self.store.update(row['id'], entry(amount='-1'))
        self.assertEqual(self.store.snapshot()['transactions'], [row])

    def test_edit_missing_record(self):
        with self.assertRaises(LookupError):
            self.store.update(999, entry())

    def test_delete_updates_summary_and_chart(self):
        self.populate()
        row = self.store.snapshot()['transactions'][1]
        self.store.delete(row['id'])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['summary'], dict(income_minor=10000, expense_minor=3000, balance_minor=7000, count=3))
        self.assertEqual(snapshot['expenses_by_category'], {'Азық-түлік': 2000, 'Көлік': 1000})

    def test_delete_missing_and_duplicate(self):
        row = self.store.add(entry())
        self.store.delete(row['id'])
        with self.assertRaises(LookupError):
            self.store.delete(row['id'])
        self.assertEqual(self.store.snapshot()['summary']['count'], 0)

    def test_mutations_reject_invalid_ids(self):
        for value in (True, False, 0, -1, '1', None, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError): self.store.delete(value)
                with self.assertRaises(ValidationError): self.store.update(value, entry())

    def test_edit_and_delete_persist_after_reopen(self):
        a = self.store.add(entry()); b = self.store.add(entry())
        self.store.update(a['id'], entry(amount='8.25'))
        self.store.delete(b['id'])
        data = BudgetStore(self.path).snapshot()
        self.assertEqual(data['summary']['count'], 1)
        self.assertEqual(data['summary']['expense_minor'], 825)

    def test_month_includes_first_and_last_day_only(self):
        self.populate()
        data = self.store.snapshot({'month': '2026-11'})
        self.assertEqual([r['date'] for r in data['transactions']], ['2026-11-30', '2026-11-01'])
        self.assertEqual(data['summary'], dict(income_minor=0, expense_minor=5000, balance_minor=-5000, count=2))
        self.assertEqual(data['expenses_by_category'], {'Азық-түлік': 5000})
        self.assertEqual(self.store.snapshot()['summary']['count'], 4)

    def test_month_distinguishes_year_and_leap_day(self):
        self.store.add(entry(date='2024-02-29'))
        self.store.add(entry(date='2025-02-28'))
        self.assertEqual(self.store.snapshot({'month': '2024-02'})['summary']['count'], 1)

    def test_empty_month_has_zero_totals(self):
        self.populate()
        data = self.store.snapshot({'month': '2020-01'})
        self.assertEqual(data['transactions'], [])
        self.assertEqual(data['expenses_by_category'], {})
        self.assertEqual(data['summary'], dict(income_minor=0, expense_minor=0, balance_minor=0, count=0))

    def test_date_range_is_inclusive(self):
        self.populate()
        self.assertEqual(self.store.snapshot({'from': '2026-10-31', 'to': '2026-11-01'})['summary']['expense_minor'], 3000)

    def test_type_and_category_filters(self):
        self.populate()
        self.assertEqual(self.store.snapshot({'kind': 'income'})['summary']['income_minor'], 10000)
        self.assertEqual(self.store.snapshot({'category': 'Көлік'})['summary']['expense_minor'], 1000)

    def test_amount_bounds_use_exact_minor_units(self):
        for amount in ('0.10', '0.11', '0.12'): self.store.add(entry(amount=amount))
        data = self.store.snapshot({'min': '0,11', 'max': '0.11'})
        self.assertEqual(data['summary']['expense_minor'], 11)
        self.assertEqual(data['summary']['count'], 1)

    def test_note_search_case_insensitive_and_literal(self):
        self.populate()
        self.assertEqual(self.store.snapshot({'q': 'market'})['summary']['count'], 2)
        self.assertEqual(self.store.snapshot({'q': "' OR 1=1 --"})['summary']['count'], 0)

    def test_filters_combine_with_and(self):
        self.populate()
        filters = dict(month='2026-11', kind='expense', category='Азық-түлік', min='20', max='30', q='market', to='2026-11-01')
        self.assertEqual(self.store.snapshot(filters)['summary']['expense_minor'], 2000)

    def test_sort_orders(self):
        self.populate()
        for order, amounts in [('newest', [10000,3000,2000,1000]), ('oldest', [1000,2000,3000,10000]), ('amount_asc', [1000,2000,3000,10000]), ('amount_desc', [10000,3000,2000,1000])]:
            with self.subTest(order=order):
                self.assertEqual([r['amount_minor'] for r in self.store.snapshot({'sort':order})['transactions']], amounts)

    def test_invalid_filters_are_rejected(self):
        cases = [{'month':'2026-13'}, {'month':'0000-01'}, {'month':'2026-1'}, {'from':'2026-02-30'}, {'from':'2026-12-01','to':'2026-11-01'}, {'kind':'other'}, {'category':'unknown'}, {'min':'NaN'}, {'max':'-1'}, {'min':'20','max':'10'}, {'sort':'other'}, {'q':'x'*121}, {'unknown':'1'}, {'min':'1000000001'}]
        for filters in cases:
            with self.subTest(filters=filters), self.assertRaises(ValidationError): self.store.snapshot(filters)

    def test_edit_moves_record_between_months(self):
        row = self.store.add(entry())
        self.store.update(row['id'], entry(date='2026-12-01'))
        self.assertEqual(self.store.snapshot({'month':'2026-11'})['summary']['count'], 0)
        self.assertEqual(self.store.snapshot({'month':'2026-12'})['summary']['count'], 1)


class ReportAPITests(unittest.TestCase):
    setUp = APITests.setUp
    tearDown = APITests.tearDown
    call = APITests.call

    def post(self, path, payload, **headers):
        return self.call('POST', path, json.dumps(payload), {'Content-Type':'application/json', **headers})

    def test_http_edit_filter_delete_workflow(self):
        row = json.loads(self.post('/api/transactions', entry())[2])
        status, _, body = self.post('/api/transactions/update', dict(entry(date='2026-12-01', amount='7.25'), id=row['id']))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['amount_minor'], 725)
        self.assertEqual(json.loads(self.call(path='/api/budget?month=2026-11')[2])['summary']['count'], 0)
        self.assertEqual(json.loads(self.call(path='/api/budget?month=2026-12')[2])['summary']['expense_minor'], 725)
        self.assertEqual(self.post('/api/transactions/delete', {'id':row['id']})[0], 200)
        self.assertEqual(json.loads(self.call()[2])['summary']['count'], 0)

    def test_http_invalid_filters_and_duplicates(self):
        for query in ('month=2026-99','month=2026-11&month=2026-12','sort=bad','min=NaN','unknown=x'):
            self.assertEqual(self.call(path='/api/budget?'+query)[0], 400)

    def test_http_unicode_combined_filters(self):
        self.post('/api/transactions', entry(note='Нан'))
        query = urlencode(dict(month='2026-11', category='Азық-түлік', q='нан', min='2500', max='2500'))
        status, _, body = self.call(path='/api/budget?'+query)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['summary']['count'], 1)

    def test_http_mutations_require_session_and_same_origin(self):
        for route in ('/api/transactions/update','/api/transactions/delete'):
            self.assertEqual(self.post(route, {'id':1}, Cookie='')[0], 401)
            self.assertEqual(self.post(route, {'id':1}, Origin='https://example.com')[0], 403)
        self.assertEqual(self.call(path='/api/budget?month=2026-11', headers={'Cookie':''})[0], 401)

    def test_http_invalid_and_missing_ids(self):
        for route in ('/api/transactions/update','/api/transactions/delete'):
            for value in (True, 0, -1, '1', None):
                self.assertEqual(self.post(route, dict(entry(), id=value))[0], 400)
            self.assertEqual(self.post(route, dict(entry(), id=999))[0], 404)


# Imported fixture classes must not be collected twice by unittest discovery.
del StoreTests, APITests
if __name__ == '__main__': unittest.main(verbosity=2)
