"""Native final revision and exact-date guardrails for RADL3 repairs."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import io
from pathlib import Path
import unittest

from pypdf import PdfReader

from . import cash_repair_proofs as proof
from . import radl_cash_repair as repair
from .inventory_candidates import _Cache
from .inventory_candidates import enrich_calendar
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page


MARKET = Path(__file__).parent / 'data/market'


class ExactRadlCashRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = _Cache(MARKET, load_inventory(MARKET))
        body, observed = cls.cache.read('raw/expanded/RADL3-payments.html', 'test_original_calendar')
        cls.parsed = inspect_payment_page(body, 'RADL3', proof.CUTOFF, observed)
        cls.evidence = repair.load_proof(repair.IDENTITY, proof.CUTOFF, cls.cache)

    def apply(self, parsed=None, identity=None, cutoff=proof.CUTOFF):
        return repair.apply_exact_repairs(self.parsed if parsed is None else parsed,
            identity or repair.IDENTITY, cutoff, self.cache)

    def test_two_missing_native_JCP_rows_exact_ISIN_dates_and_decimal_precision(self):
        output = self.apply()
        rows = output['exact_cash_repairs'][repair.REPAIR_ID]['new_events']
        self.assertEqual([(r['record_date'], r['ex_date'], r['payment_date'], r['gross_per_share_decimal']) for r in rows],
            [('2023-04-04', '2023-04-05', '2023-12-01', '0.04853059700'),
             ('2023-07-05', '2023-07-06', '2023-12-01', '0.05949664580')])
        self.assertEqual(sum((Decimal(r['gross_per_share_decimal']) for r in rows), Decimal(0)), Decimal('0.10802724280'))
        self.assertTrue(all(r['label'] == 'JCP' and r['isin'] == 'BRRADLACNOR0' and r['share_class'] == 'ON' for r in rows))
        self.assertEqual(len(output['events']), len(self.parsed['events']) + 2)

    def test_deadline_without_explicit_historical_payment_cannot_be_used(self):
        body, _ = self.cache.read(repair.ROOT + 'body-' + repair.MINUTES_SHA + '.bin', 'test_AGM')
        pages = [proof.text(p.extract_text()) for p in PdfReader(io.BytesIO(body)).pages]
        self.assertIn('pago s aos acionistas em 01 de dezembro de 2023', repair.confirm_historical_payment(pages, repair.SPECS[0]))
        pages[1] = pages[1].replace('pago s aos acionistas em 01 de dezembro de 2023',
                                   'a ser pago ate 01 de dezembro de 2023')
        with self.assertRaisesRegex(proof.ProofError, 'not_deadline'):
            repair.confirm_historical_payment(pages, repair.SPECS[0])

    def test_later_confirmation_publication_is_not_backdated(self):
        for fact in self.evidence['facts']:
            self.assertEqual(fact['published_at'], '2024-04-17')
            self.assertEqual(fact['payment_date_first_confirmed_in_used_primary_evidence'], '2024-04-17')
            self.assertLess(fact['initial_declaration_published_at'], fact['published_at'])
            self.assertTrue(fact['deadline_was_not_promoted_to_payment_date'])
        result = self.apply()
        self.assertTrue(all(not row['settlement_verified'] and not row['paid_status_as_of_verified']
                            and not row['model_admitted'] for row in result['exact_cash_repairs'][repair.REPAIR_ID]['new_events']))

    def test_final_2021_HTML_is_not_replaced_with_older_v2(self):
        output = self.apply()
        unresolved = output['exact_cash_repairs'][repair.REPAIR_ID]['unresolved_conflicts'][0]
        self.assertEqual(unresolved['native_final_document']['sequence'], '450732')
        self.assertEqual(unresolved['native_final_document']['version'], 3)
        self.assertTrue(unresolved['earlier_revision_447470_v2_not_applied'])
        old = [r for r in self.parsed['events'] if r['record_date'] == '2021-12-08']
        self.assertEqual(old, [r for r in output['events'] if r['record_date'] == '2021-12-08'])
        self.assertEqual(old, unresolved['reported_original_fields'])

    def test_other_source_rows_and_issues_and_original_bytes_unchanged(self):
        before = deepcopy(self.parsed)
        output = self.apply()
        self.assertEqual(self.parsed, before)
        self.assertEqual([r for r in output['events'] if not r.get('exact_cash_repair_id')], before['events'])
        self.assertEqual(output['report'], before['report'])
        self.assertFalse(output['exact_cash_repairs'][repair.REPAIR_ID]['independent_comparator_changed'])

    def test_exact_identity_hash_cutoff_and_empty_groups_required(self):
        with self.assertRaises(proof.ProofError):
            self.apply(identity={**repair.IDENTITY, 'isin': 'BRRADLACNOR1'})
        with self.assertRaises(proof.ProofError):
            self.apply(cutoff=date(2026, 8, 31))
        changed = deepcopy(self.parsed)
        changed['report']['sha256'] = '0' * 64
        with self.assertRaisesRegex(proof.ProofError, 'original_payment_page'):
            self.apply(changed)
        changed = deepcopy(self.parsed)
        changed['events'].append({'label': 'JCP', 'record_date': '2023-04-04',
            'payment_date': '2023-12-01', 'gross_per_share': 0.048530597, 'source_row': 'unexpected'})
        with self.assertRaisesRegex(proof.ProofError, 'original_group_changed'):
            self.apply(changed)

    def test_idempotent_and_no_other_ticker_transformation(self):
        output = self.apply()
        self.assertEqual(self.apply(output), output)
        self.assertIs(self.apply(identity={'ticker': 'RADL4'}), self.parsed)

    def test_every_cited_native_body_and_index_has_hash_and_unknown_distinct_from_retrieval(self):
        self.assertTrue(all(source['sha256'] and source['file'] for source in self.evidence['sources']))
        sources = [r for r in self.evidence['sources'] if r['sha256'] == repair.MINUTES_SHA]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['published_at'], '2024-04-17')
        self.assertTrue(sources[0]['retrieved_at'].startswith('2026-10-25T'))
        indices = [r for r in self.evidence['sources'] if r['sha256'] == proof.INDEX_PINS[2023]]
        self.assertEqual(len(indices), 1)
        self.assertEqual(len(indices[0]['source_locators']), 4)
        unresolved = self.evidence['unresolved']['sources'][-1]
        self.assertEqual(unresolved['retrieval_basis'], 'actual_public_GET_receipt')
        self.assertIsNotNone(unresolved['retrieved_at'])

    def test_enrichment_cannot_backdate_already_known_later_cash_confirmation(self):
        output = self.apply()
        row = output['exact_cash_repairs'][repair.REPAIR_ID]['new_events'][0]
        declaration = {'kind': 'cvm_declaration', 'issues': [], 'blockers': [],
            'gross_per_unit_decimal': row['gross_per_share_decimal'], 'record_date': row['record_date'],
            'payment_date': row['payment_date'], 'id': 'native-form', 'evidence': [],
            'published_at': '2023-03-31', 'available_as_of': True}
        self.assertEqual(enrich_calendar(output, {'events': [declaration]}), 1)
        current = next(r for r in output['events'] if r['record_date'] == '2023-04-04')
        self.assertEqual(current['published_at'], '2024-04-17')
        self.assertEqual(current['payment_date_first_confirmed_in_used_primary_evidence'], '2024-04-17')
        self.assertTrue(output['exact_cash_repairs'][repair.REPAIR_ID]['sources'])


if __name__ == '__main__':
    unittest.main()
