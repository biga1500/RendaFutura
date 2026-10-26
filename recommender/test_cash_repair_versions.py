"""A later delivery year must not hide a revised historical declaration."""
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from . import cash_repair_proofs as proof
from .inventory_candidates import _Cache
from .market_inventory import load_inventory


MARKET = Path(__file__).parent / 'data/market'


class CrossYearRevisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = _Cache(MARKET, load_inventory(MARKET))

    def test_initial_2023_form_is_not_final_when_2024_revision_exists(self):
        with self.assertRaisesRegex(proof.ProofError, 'latest_native_revision_required_no_fallback'):
            proof.index_document(self.cache, 2023, '683945', 1, 5762,
                '61.092.037/0001-81', '2023-11-07', 'relatorio proventos')

    def test_actual_2024_revision_of_2023_reference_is_accepted_offline(self):
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            doc, source = proof.index_document(self.cache, 2024, '734624', 2, 5762,
                '61.092.037/0001-81', '2023-11-07', 'relatorio proventos')
        self.assertEqual((doc['reference_date'], doc['published_at'], doc['version']),
            ('2023-11-07', '2024-03-20', 2))
        audit = source['revision_search']
        self.assertTrue(audit['later_calendar_year_revisions_checked'])
        self.assertEqual(audit['through'], '2026-09-30')
        self.assertEqual(len(audit['index_sources']), 4)
        self.assertEqual({item['Versao'] for item in audit['same_family_or_reference_candidates']}, {'1', '2'})
        self.assertTrue(all(item['CNPJ_Companhia'] == '61.092.037/0001-81'
            for item in audit['same_family_or_reference_candidates']))
        self.assertTrue(all(item['Data_Entrega'] <= '2026-09-30'
            for item in audit['same_family_or_reference_candidates']))

    def test_actual_final_amendment_stays_distinct_from_original_form(self):
        doc, source = proof.index_document(self.cache, 2024, '734262', 2, 5762,
            '61.092.037/0001-81', '2024-03-19', 'aviso aos acionistas')
        self.assertEqual(doc['published_at'], '2024-03-19')
        self.assertTrue(all(item['Categoria'] == 'Aviso aos Acionistas'
            for item in source['revision_search']['same_family_or_reference_candidates']))

    def test_blank_index_families_are_linked_by_literal_native_distribution_and_parent_IPE(self):
        _, source = proof.index_document(self.cache, 2024, '734624', 2, 5762,
            '61.092.037/0001-81', '2023-11-07', 'relatorio proventos')
        rows = source['revision_search']['same_family_or_reference_candidates']
        self.assertTrue(all(not row['Protocolo_Entrega'] for row in rows))
        linked = next(row['native_revision_link'] for row in rows if 'native_revision_link' in row)
        for side in ('selected', 'candidate'):
            self.assertEqual(linked[side]['protocolo_provento'], '1158896')
            self.assertEqual(linked[side]['protocolo_ipe'], '005762IPE071120230104523712-09')
            self.assertEqual(linked[side]['isin_scope'], ['BRETERACNOR3'])
            self.assertTrue(all(s['file'] and s['sha256'] for s in linked[side]['sources']))

    def test_native_link_cannot_borrow_another_issuer(self):
        _, source = proof.index_document(self.cache, 2024, '734624', 2, 5762,
            '61.092.037/0001-81', '2023-11-07', 'relatorio proventos')
        row = source['source_locator']['raw_row']
        with self.assertRaisesRegex(proof.ProofError, 'native_identity_conflict'):
            proof._structured_revision_link(self.cache, row, 99999)

    def test_changed_original_cannot_prove_a_cross_year_native_link(self):
        original_read = self.cache.read

        def changed(relative, role):
            body, observed = original_read(relative, role)
            if relative.startswith('raw/primary-research/cash-calendars/cvm683945v1-') and relative.endswith('.pdf'):
                return body + b'changed', observed
            return body, observed

        with patch.object(self.cache, 'read', side_effect=changed):
            with self.assertRaisesRegex(proof.ProofError, 'original_hash_mismatch'):
                proof.index_document(self.cache, 2024, '734624', 2, 5762,
                    '61.092.037/0001-81', '2023-11-07', 'relatorio proventos')

    def test_identity_category_and_reference_are_not_borrowed_from_nearby_row(self):
        for change in ({'cvm_code': 1}, {'cnpj': '00.000.000/0001-00'},
                       {'reference': '2024-03-19'}, {'category': 'aviso aos acionistas'}):
            args = dict(year=2024, sequence='734624', version=2, cvm_code=5762,
                cnpj='61.092.037/0001-81', reference='2023-11-07', category='relatorio proventos')
            with self.subTest(change=change), self.assertRaises(proof.ProofError):
                proof.index_document(self.cache, **{**args, **change})


if __name__ == '__main__':
    unittest.main()
