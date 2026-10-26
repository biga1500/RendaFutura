"""Exact native dates and amounts, complete-group repairs, no calendar inference."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from . import cash_repair_proofs as proof
from . import round3_eter_cash as repair
from .inventory_candidates import _Cache, _one
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page


MARKET = Path(__file__).parent / 'data/market'


class EternitNativeCashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = load_inventory(MARKET)
        cls.cache = _Cache(MARKET, cls.inventory)
        cls.identity, = [r for r in cls.inventory['stocks'] if r['ticker'] == 'ETER3']
        body, observed = cls.cache.read('raw/expanded/ETER3-payments.html', 'test_original_calendar')
        cls.parsed = inspect_payment_page(body, 'ETER3', proof.CUTOFF, observed)
        cls.evidence = repair.load_proof(cls.identity, proof.CUTOFF, cls.cache)

    def apply(self, parsed=None, identity=None, cutoff=proof.CUTOFF):
        return repair.apply_exact_repairs(self.parsed if parsed is None else parsed,
            identity or self.identity, cutoff, self.cache)

    def test_final_cross_year_form_and_explicit_amendment_not_original_schedule(self):
        fact = self.evidence['facts'][0]
        self.assertEqual((fact['approval_date'], fact['record_date'], fact['ex_date'], fact['payment_date']),
            ('2023-11-07', '2023-11-10', '2023-11-13', '2024-03-28'))
        self.assertEqual(fact['gross_per_share_decimal'], '0.07661590000')
        self.assertEqual(fact['published_at'], '2024-03-20')
        self.assertEqual(fact['payment_amendment_published_at'], '2024-03-19')
        self.assertEqual(fact['initial_declared_payment_date'], '2024-03-29')
        self.assertEqual(fact['source_locator']['form_sequence'], '734624')
        self.assertEqual(fact['source_locator']['form_version'], 2)
        self.assertFalse(self.evidence['contract']['payment_amendment']['date_inferred_from_holiday'])

    def test_exact_reported_2024_gross_not_secondary_rounding_or_tax_reconstruction(self):
        fact = self.evidence['facts'][1]
        self.assertEqual((fact['approval_date'], fact['record_date'], fact['ex_date'], fact['payment_date']),
            ('2024-11-05', '2024-11-08', '2024-11-11', '2025-03-31'))
        self.assertEqual(fact['gross_per_share_decimal'], '0.05202290000')
        self.assertEqual(fact['published_at'], '2024-11-07')
        self.assertFalse(fact['gross_reconstructed_from_net'])

    def test_only_two_complete_groups_change_without_modifying_originals(self):
        before = deepcopy(self.parsed)
        output = self.apply()
        self.assertEqual(self.parsed, before)
        self.assertEqual(output['report'], before['report'])
        self.assertEqual(len(output['events']), len(before['events']))
        self.assertFalse(any(r.get('record_date') == '2023-11-12' for r in output['events']))
        self.assertEqual([r for r in output['events'] if not r.get('exact_cash_repair_id')],
            [r for r in before['events'] if r.get('record_date') not in ('2023-11-12', '2024-11-08')])
        audit = output['exact_cash_repairs'][repair.REPAIR_ID]
        self.assertEqual(audit['groups'][0]['original_record_date'], '2023-11-12')
        self.assertEqual(audit['groups'][0]['reported_original_fields'][0]['payment_date'], '2024-03-28')
        self.assertFalse(audit['source_bytes_modified'])
        self.assertFalse(audit['independent_comparator_changed'])

    def test_repaired_view_is_idempotent_but_reintroduced_origin_is_not(self):
        result = self.apply()
        self.assertEqual(self.apply(result), result)
        result['events'].extend(deepcopy([r for r in self.parsed['events'] if r['record_date'] == '2023-11-12']))
        with self.assertRaisesRegex(proof.ProofError, 'modified_view'):
            self.apply(result)

    def test_untagged_duplicate_target_and_changed_source_group_fail_closed(self):
        result = self.apply()
        duplicate = deepcopy(next(r for r in result['events'] if r.get('record_date') == '2023-11-10'))
        duplicate.pop('exact_cash_repair_id')
        result['events'].append(duplicate)
        with self.assertRaisesRegex(proof.ProofError, 'modified_view'):
            self.apply(result)
        changed = deepcopy(self.parsed)
        next(r for r in changed['events'] if r['record_date'] == '2024-11-08')['gross_per_share'] = .052
        with self.assertRaisesRegex(proof.ProofError, 'original_group_changed'):
            self.apply(changed)

    def test_first_relocation_cannot_merge_an_existing_native_target(self):
        changed = deepcopy(self.parsed)
        row = deepcopy(next(r for r in changed['events'] if r['record_date'] == '2023-11-12'))
        row['record_date'] = '2023-11-10'
        changed['events'].append(row)
        with self.assertRaisesRegex(proof.ProofError, 'relocation_target_not_empty'):
            self.apply(changed)

    def test_exact_identity_cutoff_and_original_page_hash_are_required(self):
        for value in ({'cvm': 1}, {'share_class': 'PN'}, {'isin': 'BRETERACNPR1'}):
            with self.subTest(value=value), self.assertRaises(proof.ProofError):
                self.apply(identity={**self.identity, **value})
        with self.assertRaises(proof.ProofError):
            self.apply(cutoff=date(2026, 8, 31))
        changed = deepcopy(self.parsed)
        changed['report']['sha256'] = '0' * 64
        with self.assertRaisesRegex(proof.ProofError, 'original_payment_page'):
            self.apply(changed)
        self.assertIs(self.apply(identity={'ticker': 'ABCD3'}), self.parsed)

    def test_incomplete_or_conditional_amendment_is_not_a_dated_payment_fact(self):
        _, pages, _, _ = repair.native(self.cache, repair.SPECS['amendment'])
        for old, new in (('28/03/2024', '28/03/2025'), ('3T23 e 4T23', '3T24 e 4T24'),
                         ('serao antecipados para o dia', 'poderao ser antecipados para o dia')):
            changed = [proof.text(pages[0]).replace(old, new), pages[1]]
            with self.subTest(old=old), self.assertRaises(proof.ProofError):
                repair.verify_amendment(changed)

    def test_sources_preserve_publication_retrieval_and_unknown_as_distinct(self):
        self.assertTrue(all(r['sha256'] and r['file'] for r in self.evidence['sources']))
        final2024 = next(r for r in self.evidence['sources'] if r['sha256'] == repair.SPECS['final2024'][6])
        self.assertIsNone(final2024['retrieved_at'])
        self.assertEqual(final2024['published_at'], '2024-11-07')
        amendment = next(r for r in self.evidence['sources'] if r['sha256'] == repair.SPECS['amendment'][6])
        self.assertTrue(amendment['retrieved_at'])
        self.assertEqual(amendment['published_at'], '2024-03-19')
        self.assertFalse(self.evidence['contract']['settlement_verified'])
        self.assertTrue(self.evidence['contract']['initial_2023_form_v1_not_used_as_final'])

    def test_ordinary_production_candidate_passes_prices_and_all_cash_groups_offline(self):
        checks = {}
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            candidate = _one(self.identity, _Cache(MARKET, self.inventory), proof.CUTOFF, checks)
        self.assertGreaterEqual(len(candidate['prices']), 61)
        self.assertEqual(checks['independent_price_matches'], 13)
        self.assertEqual(checks['independent_cash_matches'], 13)
        self.assertEqual(len(candidate['ledger']), 14)
        self.assertTrue(all(row['financial_confirmation'] ==
            'reported_calendar_independently_amount_reconciled_not_settlement_confirmed'
            for row in candidate['ledger']))
        self.assertIn(repair.REPAIR_ID, checks['exact_cash_repairs'])


if __name__ == '__main__':
    unittest.main()
