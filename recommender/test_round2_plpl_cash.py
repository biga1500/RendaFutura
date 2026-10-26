"""Native date roles, final schedule, precision and relocation guardrails."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from . import cash_repair_proofs as proof
from . import round2_plpl_cash as repair
from .inventory_candidates import _Cache, _one
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page


MARKET = Path(__file__).parent / 'data/market'


class ExactPlplCashRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = load_inventory(MARKET)
        cls.identity, = [r for r in cls.inventory['stocks'] if r['ticker'] == 'PLPL3']
        cls.cache = _Cache(MARKET, cls.inventory)
        body, observed = cls.cache.read('raw/expanded/PLPL3-payments.html', 'test_original_calendar')
        cls.parsed = inspect_payment_page(body, 'PLPL3', proof.CUTOFF, observed)
        cls.evidence = repair.load_proof(cls.identity, proof.CUTOFF, cls.cache)
        cls.root, cls.packet, _ = repair.packet(MARKET)
        cls.cash_body, _ = repair.receipt_source(cls.root, cls.packet['cash_source'],
            cls.packet['cash_source']['sha256'])

    def apply(self, parsed=None, identity=None, cutoff=proof.CUTOFF):
        return repair.apply_exact_repairs(self.parsed if parsed is None else parsed,
            identity or self.identity, cutoff, self.cache)

    def test_primary_approval_entitlement_ex_and_payment_are_distinct_literal_roles(self):
        output = self.apply()
        fact, = output['exact_cash_repairs'][repair.REPAIR_ID]['new_events']
        self.assertEqual((fact['approval_date'], fact['record_date'], fact['ex_date'], fact['payment_date']),
            ('2024-01-15', '2024-01-18', '2024-01-19', '2024-02-08'))
        self.assertEqual(fact['gross_per_share_decimal'], '0.50439835364')
        self.assertEqual(fact['isin'], 'BRPLPLACNOR5')
        self.assertFalse(any(r['record_date'] == '2024-01-15' for r in output['events']))
        self.assertEqual(len(output['events']), len(self.parsed['events']))

    def test_relocation_keeps_original_received_rows_and_source_hash(self):
        before = deepcopy(self.parsed)
        output = self.apply()
        audit = output['exact_cash_repairs'][repair.REPAIR_ID]
        group, = audit['groups']
        self.assertEqual(group['original_record_date'], '2024-01-15')
        self.assertEqual(group['reported_original_fields'],
            [r for r in before['events'] if r['record_date'] == '2024-01-15'])
        self.assertEqual(before, self.parsed)
        self.assertEqual(output['report'], before['report'])
        self.assertEqual(audit['original_calendar_sha256'], repair.PAYMENT_SHA)
        self.assertFalse(audit['source_bytes_modified'])
        self.assertEqual([r for r in output['events'] if not r.get('exact_cash_repair_id')],
            [r for r in before['events'] if r['record_date'] != '2024-01-15'])

    def test_nonempty_relocation_target_requires_review_not_merging_or_overwrite(self):
        changed = deepcopy(self.parsed)
        changed['events'].append({**deepcopy(changed['events'][0]), 'record_date': '2024-01-18'})
        with self.assertRaisesRegex(proof.ProofError, 'relocation_target_not_empty'):
            self.apply(changed)

    def test_changed_original_group_or_page_hash_cannot_be_repaired(self):
        changed = deepcopy(self.parsed)
        next(r for r in changed['events'] if r['record_date'] == '2024-01-15')['gross_per_share'] = .6
        with self.assertRaisesRegex(proof.ProofError, 'original_group_changed'):
            self.apply(changed)
        changed = deepcopy(self.parsed)
        changed['report']['sha256'] = '0' * 64
        with self.assertRaisesRegex(proof.ProofError, 'original_payment_page'):
            self.apply(changed)

    def test_idempotent_and_modified_repaired_view_is_rejected(self):
        result = self.apply()
        self.assertEqual(self.apply(result), result)
        changed = deepcopy(result)
        next(r for r in changed['events'] if r.get('exact_cash_repair_id'))['record_date'] = '2024-01-15'
        with self.assertRaisesRegex(proof.ProofError, 'modified_view_cannot_be_reapplied'):
            self.apply(changed)

    def test_exact_identity_and_reference_required_without_capturing_another_stock(self):
        self.assertIs(self.apply(identity={'ticker': 'PETR4'}), self.parsed)
        for changed in ({'isin': 'BRPLPLACNOR6'}, {'cvm': 1}, {'share_class': 'PN'}):
            with self.subTest(changed=changed), self.assertRaises(proof.ProofError):
                self.apply(identity={**self.identity, **changed})
        with self.assertRaises(proof.ProofError):
            self.apply(cutoff=date(2026, 8, 31))

    def test_explicit_final_notice_controls_named_payment_not_obsolete_provider_date(self):
        calendar = repair.parse_calendar(self.cash_body)
        row, = [r for r in calendar if r['record_date'] == '2025-12-29']
        self.assertEqual(row['original_reported_payment_date'], '2026-07-01')
        self.assertEqual(row['payment_date'], '2026-07-02')
        self.assertEqual(row['payment_amendment_published_at'], '2026-06-25')
        contract = self.evidence['contract']
        self.assertTrue(contract['all_other_terms_explicitly_ratified_by_final_notice'])
        self.assertTrue(contract['no_old_form_used_to_override_final_notice'])
        self.assertEqual(contract['final_structured_form_v2_response'],
            'HTML_not_usable_native_PDF_retained_not_retried')

    def test_secondary_precision_and_rows_are_not_invented_from_primary_amount(self):
        calendar = repair.parse_calendar(self.cash_body)
        self.assertEqual(len(calendar), 6)
        row, = [r for r in calendar if r['record_date'] == '2024-01-18']
        self.assertEqual(row['reported_amount_decimal'], '0.5044')
        self.assertEqual(row['gross_per_share_decimal'], '0.5044')
        self.assertEqual(row['original_reported_fields'][1], '0,5044')
        self.assertTrue(all(r['label'] == 'DIVIDENDO' for r in calendar))

    def test_provider_changes_fail_closed_not_assume_the_new_schedule_is_same(self):
        for old, new in ((b'PLPL3', b'ABCD3'), (b'0,4928', b'0,3928'),
                         (b'01/07/2026', b'03/07/2026'), (b'DIVIDENDO', b'JCP')):
            with self.subTest(old=old), self.assertRaises((proof.ProofError, ValueError)):
                repair.parse_calendar(self.cash_body.replace(old, new))

    def test_all_sources_have_retained_hash_and_actual_retrieval_not_assumed_publication(self):
        self.assertTrue(all(r['sha256'] and r['file'] for r in self.evidence['sources']))
        notices = [r for r in self.evidence['sources'] if r.get('published_at')]
        self.assertEqual(len(notices), 6)
        self.assertTrue(all(r['retrieved_at'] for r in notices))
        self.assertFalse(self.evidence['contract']['settlement_verified'])
        fact, = self.evidence['facts']
        self.assertEqual(fact['published_at'], '2024-01-15')

    def test_all_sixty_one_model_quotes_and_primary_repaired_cash_pass_ordinary_path_offline(self):
        checks = {}
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            cache = _Cache(MARKET, self.inventory)
            candidate = _one(self.identity, cache, proof.CUTOFF, checks)
        self.assertGreaterEqual(len(candidate['prices']), 61)
        self.assertEqual(checks['independent_price_matches'], 61)
        self.assertEqual(len(candidate['payments']), 5)
        self.assertTrue(candidate['independent_cash_matches'])
        self.assertIn('exact_cash_repairs', checks)


if __name__ == '__main__':
    unittest.main()
