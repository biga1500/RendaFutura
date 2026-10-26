"""Offline integration and fail-closed tests using preserved original bytes."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

from .cash_repair_proofs import ProofError
from .inventory_candidates import _Cache
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page
from . import sanb4_cash_repair as repair


MARKET = Path(__file__).parent / 'data/market'


class ExactSanb4CashRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = _Cache(MARKET, load_inventory(MARKET))
        body, observed = cls.cache.read('raw/expanded/SANB4-payments.html', 'test_original_calendar')
        cls.parsed = inspect_payment_page(body, 'SANB4', date(2026, 9, 30), observed)
        cls.proof = repair.load_proof(repair.IDENTITY, date(2026, 9, 30), cls.cache)

    def apply(self, parsed=None, identity=None, cutoff=date(2026, 9, 30)):
        return repair.apply_exact_repairs(self.parsed if parsed is None else parsed,
            identity or repair.IDENTITY, cutoff, self.cache)

    def test_literal_PN_components_not_ON_or_UNIT_or_net(self):
        output = self.apply()
        events = [r for r in output['events'] if r.get('exact_cash_repair_id') == repair.REPAIR_ID]
        self.assertEqual(len(events), 2)
        amounts = {r['label']: r['gross_per_share_decimal'] for r in events}
        self.assertEqual(amounts, {'DIVIDENDO': '0.09839755610', 'JCP': '0.14056793729'})
        self.assertEqual(sum((Decimal(v) for v in amounts.values()), Decimal(0)), Decimal('0.23896549339'))
        self.assertEqual(next(r for r in events if r['label'] == 'JCP')['net_per_share_decimal'], '0.11948274669')
        self.assertTrue(all(r['share_class'] == 'PN' and r['isin'] == 'BRSANBACNPR5' for r in events))

    def test_explicit_roles_schedule_start_is_not_settlement(self):
        events = self.proof['facts']
        for event in events:
            self.assertEqual((event['approval_date'], event['record_date'], event['ex_date'],
                event['payment_date'], event['payment_start_date']),
                ('2022-04-14', '2022-04-20', '2022-04-22', '2022-05-16', '2022-05-16'))
        output = self.apply()
        actual = output['exact_cash_repairs'][repair.REPAIR_ID]['new_events']
        self.assertTrue(all(not r['settlement_verified'] and not r['is_settlement_confirmation']
                            and r['status'] == 'past_scheduled' and not r['model_admitted'] for r in actual))

    def test_preserves_every_original_field_and_unrelated_rows_without_mutation(self):
        before = deepcopy(self.parsed)
        output = self.apply()
        self.assertEqual(self.parsed, before)
        audit = output['exact_cash_repairs'][repair.REPAIR_ID]
        group = audit['groups'][0]
        self.assertEqual(group['original_sum_decimal'], '0.239504')
        self.assertEqual(group['reported_original_fields'],
            [r for r in before['events'] if r['record_date'] == '2022-04-20'])
        self.assertEqual([r for r in output['events'] if r['record_date'] != '2022-04-20'],
            [r for r in before['events'] if r['record_date'] != '2022-04-20'])
        self.assertEqual(output['report'], before['report'])
        self.assertFalse(audit['independent_comparator_changed'])

    def test_idempotence_and_modified_view_rejected(self):
        output = self.apply()
        self.assertEqual(self.apply(output), output)
        event = next(r for r in output['events'] if r.get('exact_cash_repair_id'))
        event['gross_per_share'] = 99
        with self.assertRaisesRegex(ProofError, 'modified_view'):
            self.apply(output)

    def test_exact_identity_and_cutoff_required(self):
        for key, value in [('isin', 'BRSANBACNPR6'), ('share_class', 'ON'), ('cvm', 20533)]:
            with self.subTest(key=key), self.assertRaises(ProofError):
                self.apply(identity={**repair.IDENTITY, key: value})
        with self.assertRaises(ProofError):
            self.apply(cutoff=date(2026, 8, 31))

    def test_original_hash_and_exact_rows_required(self):
        output = deepcopy(self.parsed)
        output['report']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ProofError, 'original_payment_page'):
            self.apply(output)
        output = deepcopy(self.parsed)
        next(r for r in output['events'] if r['record_date'] == '2022-04-20')['gross_per_share'] += 0.001
        with self.assertRaisesRegex(ProofError, 'original_group_changed'):
            self.apply(output)

    def test_final_revision_and_real_retrieval_proved(self):
        self.assertTrue(self.proof['contract']['native_final_revision_verified'])
        source = self.proof['sources'][-1]
        self.assertEqual(source['sha256'], repair.BODY_SHA)
        self.assertIsNotNone(source['retrieved_at'])
        self.assertEqual(source['published_at'], '2022-04-14')
        self.assertFalse(source['settlement_verified'])

    def test_native_original_bytes_hash_is_checked_before_correction(self):
        original_read = self.cache.read

        def changed_read(relative, role):
            body, observed = original_read(relative, role)
            return (body + b'changed', observed) if relative == repair.BODY_FILE else (body, observed)

        with patch.object(self.cache, 'read', changed_read):
            with self.assertRaisesRegex(ProofError, 'original_hash_mismatch'):
                self.apply()

    def test_other_ticker_is_unchanged(self):
        self.assertIs(self.apply(identity={'ticker': 'SANB3'}), self.parsed)


if __name__ == '__main__':
    unittest.main()
