"""Independent, read-only review of actual frozen DEXP evidence and dispatch.

No acquisition/freeze/report producer is called. Normal production passes use
the real hooks and source bytes. Negative controls alter only in-memory inputs.
"""

from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
import zipfile

from lxml import html
from pypdf import PdfReader

from . import inventory_candidates as candidates
from . import inventory_financials as financials
from . import round2_dexp_eps as eps
from . import round2_dexp_prices as prices
from . import round2_share_actions as primary
from .collect_market_data import financial_features, month_end, shift_month, stock_observations
from .market_inventory import load_inventory
from .share_basis import br_number, factor_after, reconcile_prices


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected_files():
    root = eps.ROOT
    paths = set((root / eps.BASE).glob('*'))
    paths.update((root / 'raw/expanded').glob('DEXP*'))
    paths.update(root / p for p in (eps.ANNUAL, eps.INDEX, 'current.json',
                                   'inventory/current.json', 'stock-selection/current.json'))
    frozen = json.loads(primary.read(root, eps.review_file(), eps.REVIEW_SHA))
    paths.update(root / ref['file'] for ref in frozen['sources'])
    paths.update(Path(__file__).parent / p for p in (
        'round2_dexp_prices.py', 'round2_dexp_eps.py', 'inventory_candidates.py',
        'inventory_financials.py', 'share_basis.py', 'preferred_class_binding.py'))
    return {str(p): digest(p) for p in paths if p.is_file()}


class DexpIndependentReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network_attempts = []

        def forbidden(*args, **kwargs):
            cls.network_attempts.append('network_forbidden')
            raise AssertionError('network_forbidden')

        for target, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                             (socket, 'create_connection'), (socket, 'getaddrinfo')):
            blocker = patch.object(target, name, forbidden)
            blocker.start()
            cls.addClassCleanup(blocker.stop)
        cls.before = protected_files()
        cls.evidence = {ticker: prices.diagnose(eps.ROOT, identity)
                        for ticker, identity in eps.IDENTITIES.items()}
        cls.proof = eps.build_proof()
        cls.readings, cls.eps_sources = eps.get_dexp_eps_readings(
            eps.ROOT, eps.CUTOFF, list(eps.IDENTITIES.values()))
        cls.inventory = load_inventory(eps.ROOT)
        cls.rows = {r['ticker']: r for r in cls.inventory['stocks'] if r['ticker'] in eps.IDENTITIES}
        cls.context = {t: prices.original_context(eps.ROOT, eps.IDENTITIES[t]) for t in eps.IDENTITIES}
        cls.normal = {}
        cls.candidate = None
        for ticker, row in cls.rows.items():
            checks = {}
            cache = candidates._Cache(eps.ROOT, cls.inventory)
            try:
                candidate = candidates._one(row, cache, eps.CUTOFF, checks)
                if ticker == 'DEXP3':
                    cls.candidate = candidate
                cls.normal[ticker] = {'passed': True, 'candidate': candidate, 'checks': checks}
            except ValueError as error:
                cls.normal[ticker] = {'passed': False, 'reason': str(error), 'checks': checks}
        cls.statements, cls.fin_diagnostics, cls.fin_sources = financials.prepare_financials(
            eps.ROOT, list(cls.rows.values()), eps.CUTOFF)
        row = cls.rows['DEXP3']
        cls.current = financial_features(cls.statements, cls.candidate['payments'], eps.CUTOFF,
                                         (row['cvm'], row['share_class'], row['sector_code']))
        observations, cls.training_coverage = stock_observations(
            'DEXP3', cls.candidate['prices'], cls.candidate['payments'], cls.statements,
            eps.CUTOFF, spec=(row['cvm'], row['share_class'], row['sector_code']))
        cls.training = [r for r in observations
                        if date.fromisoformat(r['as_of']) >= cls.candidate['training_since']
                        and date.fromisoformat(r['fiscal_end']) >= date.fromisoformat(cls.candidate['financial_basis_start'])]

    def test_all_original_literal_quotes_retained_not_replaced(self):
        for ticker, evidence in self.evidence.items():
            original, _, _, _ = self.context[ticker]
            self.assertEqual({d: evidence['raw_quotes'][d] for d in original}, original)
            self.assertEqual(evidence['proof']['original_anchor_count_in_window'], 7)
            literal = {}
            for locator in evidence['proof']['native_exchange_quote_readings']:
                row = locator['native_row']
                day = datetime.strptime(row['dateClosingPricePriorExDate'], '%d/%m/%Y').date()
                value = br_number(row['closingPricePriorExDate'])
                self.assertEqual(original[day], value)
                self.assertEqual(locator['actual_quote_date_field'], 'dateClosingPricePriorExDate')
                literal[day] = value
            self.assertEqual(literal, original)

    def test_dexp4_quote_date_not_record_date_alias(self):
        original = self.context['DEXP4'][0]
        self.assertEqual(original[date(2024, 4, 25)], 11.08)
        self.assertNotIn(date(2024, 4, 29), original)
        locator, = [r for r in self.evidence['DEXP4']['proof']['native_exchange_quote_readings']
                    if r['native_row']['dateClosingPricePriorExDate'] == '25/04/2024']
        self.assertEqual(locator['native_row']['lastDatePriorEx'], '29/04/2024')

    def test_old_literal_quote_dates_outside_window_still_visible(self):
        proof = self.evidence['DEXP3']['proof']
        native = [r for r in proof['native_exchange_quote_readings']
                  if r['native_row']['dateClosingPricePriorExDate'] == '28/06/2005']
        self.assertEqual(len(native), 3)
        self.assertTrue(all(r['native_row']['lastDatePriorEx'] == '30/06/2005' for r in native))
        self.assertTrue(all(not r['used_in_required_window'] for r in native))
        self.assertIn({'date': '2005-06-28', 'raw_quote': 1.04},
                      proof['original_quotes_outside_required_window_retained'])

    def test_native_provider_table_all_134_rows_no_favorable_day_filter(self):
        for ticker, evidence in self.evidence.items():
            body, _, _ = prices.source(eps.ROOT, ticker)
            tree = html.fromstring(body.decode('utf8'))
            table, = [t for t in tree.xpath('//table')
                      if len(t.xpath('.//tr')) == 135]
            native = {}
            for row in table.xpath('.//tr')[1:]:
                cells = [' '.join(cell.text_content().split()) for cell in row.xpath('./td')]
                day = datetime.strptime(cells[0], '%d/%m/%Y').date()
                native[day] = Decimal(cells[3].replace('.', '').replace(',', '.'))
            observed = {date.fromisoformat(r['date']): Decimal(r['close_decimal'])
                        for r in evidence['proof']['provider_observation_rows']}
            self.assertEqual(native, observed)
            self.assertEqual(len(native), 134)
            window, future, total = {'DEXP3': (118, 16, 125), 'DEXP4': (119, 15, 126)}[ticker]
            self.assertEqual(evidence['proof']['in_window_provider_rows'], window)
            self.assertEqual(len(evidence['proof']['post_cutoff_rows_not_used']), future)
            self.assertEqual(len(evidence['proof']['independent_comparisons']), total)
            self.assertTrue(all(factor_after(d, evidence['primary_actions']['observed_actions'], eps.CUTOFF) == 1
                                for d in native if primary.START <= d <= eps.CUTOFF))

    def test_dexp3_gate_matches_all_dates_and_every_used_regime(self):
        proof = self.evidence['DEXP3']['proof']
        self.assertTrue(proof['original_price_gate']['passed'])
        self.assertEqual(proof['original_price_gate']['mode'], 'split_adjusted')
        self.assertEqual(len(proof['original_price_gate']['matches']), 125)
        self.assertEqual(proof['all_split_adjusted_conflicts'], [])
        self.assertEqual({r['factor_after'] for r in proof['used_regimes']}, {1, 1.125})
        self.assertTrue(all(r['all_independent_dates'] for r in proof['used_regimes']))
        self.assertTrue(any(not r['raw_agrees'] for r in proof['independent_comparisons']))

    def test_dexp4_nonmonthly_conflict_is_preserved_and_blocks_loader(self):
        proof = self.evidence['DEXP4']['proof']
        conflict, = proof['all_split_adjusted_conflicts']
        self.assertEqual(conflict['date'], '2026-09-04')
        self.assertEqual(conflict['raw_quote'], 7.50)
        self.assertAlmostEqual(conflict['comparison_close'], 7.40, places=5)
        self.assertEqual(conflict['documented_factor_after'], 1)
        self.assertFalse(conflict['is_model_monthly_date'])
        self.assertFalse(proof['original_price_gate']['passed'])
        self.assertFalse(conflict['raw_agrees'])
        self.assertTrue(all(abs(v - 7.4) < 1e-6 for k, v in conflict['original_comparison_OHLCV'].items() if k != 'volume'))
        with self.assertRaisesRegex(ValueError, 'historical_quote_or_share_scale_disagrees'):
            prices.load_evidence(eps.ROOT, eps.IDENTITIES['DEXP4'], eps.CUTOFF)

    def test_all_61_required_monthly_inputs_are_unchanged_yahoo(self):
        content = self.context['DEXP3'][3]
        payload = json.loads(content)['chart']['result'][0]
        monthly = {}
        for stamp, value in sorted(zip(payload['timestamp'], payload['indicators']['quote'][0]['close'])):
            day = datetime.fromtimestamp(stamp, timezone.utc).date()
            if value is not None and day <= eps.CUTOFF:
                monthly[month_end(day.year, day.month)] = day, value
        self.assertEqual(self.candidate['price_source_file'], self.context['DEXP3'][2]['comparison']['file'])
        for index in range(61):
            month = shift_month(eps.CUTOFF, -index)
            observation, value = monthly[month]
            self.assertEqual(self.candidate['prices'][month], (observation, value, value))
        self.assertEqual(len(self.evidence['DEXP3']['proof']['model_monthly_inputs']), 61)

    def test_independent_reconcile_call_same_common_gate(self):
        for ticker, evidence in self.evidence.items():
            args = self.context[ticker][3], ticker, evidence['primary_actions']['observed_actions'], evidence['raw_quotes'], eps.CUTOFF
            if ticker == 'DEXP4':
                with self.assertRaisesRegex(ValueError, 'historical_quote_or_share_scale_disagrees'):
                    reconcile_prices(*args, evidence_provider=prices.PROVIDER)
            else:
                result, dates, mode = reconcile_prices(*args, evidence_provider=prices.PROVIDER)
                self.assertEqual(result, self.evidence[ticker]['prices'])
                self.assertEqual(len(dates), 125)
                self.assertEqual(mode, 'split_adjusted')

    def test_caller_cannot_drop_original_quote_or_action(self):
        original, _, _, content = self.context['DEXP3']
        dropped = dict(original)
        dropped.pop(date(2022, 4, 28))
        with self.assertRaisesRegex(ValueError, 'original_quote_set_changed'):
            prices.load_evidence(eps.ROOT, eps.IDENTITIES['DEXP3'], eps.CUTOFF, independent_quotes=dropped)
        with self.assertRaisesRegex(ValueError, 'caller_action_list_changed'):
            prices.load_evidence(eps.ROOT, eps.IDENTITIES['DEXP3'], eps.CUTOFF, observed_actions=[])
        with self.assertRaisesRegex(ValueError, 'caller_original_feed_changed'):
            prices.load_evidence(eps.ROOT, eps.IDENTITIES['DEXP3'], eps.CUTOFF, comparison_content=content + b' ')

    def test_identity_and_cutoff_strict_for_price_and_eps_loaders(self):
        for field, value in (('cvm', 16631), ('cvm', '16632'), ('share_class', 'PN'), ('isin', 'BRDEXPACNPR8')):
            identity = {**eps.IDENTITIES['DEXP3'], field: value}
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    prices.load_evidence(eps.ROOT, identity, eps.CUTOFF)
                with self.assertRaises(ValueError):
                    eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF, [identity])
        with self.assertRaises(ValueError):
            prices.load_evidence(eps.ROOT, eps.IDENTITIES['DEXP3'], date(2026, 8, 31))
        with self.assertRaises(ValueError):
            eps.get_dexp_eps_readings(eps.ROOT, date(2026, 8, 31))

    def test_provider_parser_rejects_duplicate_dates_wrong_security_and_wrong_ohlc(self):
        body, _, _ = prices.source(eps.ROOT, 'DEXP3')
        for change in ('duplicate', 'identity', 'ohlc'):
            tree = html.fromstring(body.decode('utf8'))
            table, = [t for t in tree.xpath('//table') if len(t.xpath('.//tr')) == 135]
            first, second = table.xpath('.//tr')[1:3]
            if change == 'duplicate':
                second.xpath('./td')[0].text = first.xpath('./td')[0].text_content()
            elif change == 'identity':
                tree.xpath('//title')[0].text = 'Wrong security'
            else:
                first.xpath('./td')[3].text = '999,99'
            with self.subTest(change=change), self.assertRaises(ValueError):
                prices.parse_ibovx(html.tostring(tree, encoding='utf8'), 'DEXP3')

    def test_adjusted_heading_not_claimed_as_raw_semantics(self):
        proof = self.evidence['DEXP4']['proof']
        third = proof['third_source_audit']
        body, _, _ = prices.source(eps.ROOT, 'third')
        text = ' '.join(html.fromstring(body.decode('utf8')).text_content().split())
        self.assertIn('ajustados por desdobramentos e grupamentos', text)
        self.assertFalse(third['raw_storage_basis_independently_certified'])
        self.assertFalse(third['official_exchange_authority_claimed'])
        self.assertFalse(third['upstream_independence_verified'])
        self.assertFalse(proof['provider_adjustment_flag_used_as_proof'])
        self.assertFalse(proof['native61_issuer_history_recovered'])
        self.assertFalse(proof['synthetic_exchange_rows_created'])

    def test_current_quarter_literal_not_historical_comparative_or_average(self):
        release = self.evidence['DEXP3']['proof']['issuer_release']
        body, _, _ = prices.source(eps.ROOT, 'release')
        text = primary.normalized(PdfReader(io.BytesIO(body)).pages[10].extract_text())
        self.assertIn('30 de junho de 2026', text)
        self.assertIn('cotacao de r$ 7,11 por acao', text)
        self.assertIn('valores historicos ajustados', text)
        self.assertEqual(release['literal_close'], '7,11')
        self.assertTrue(release['historical_comparative_prices_rejected_as_raw'])
        self.assertFalse(release['used_as_extra_independent_gate_anchor'])
        self.assertEqual(self.evidence['DEXP3']['raw_quotes'][date(2026, 6, 30)], 7.11)

    def test_exact_current_filing_and_final_version_not_retrieval_date(self):
        master = self.proof['latest_master']
        self.assertEqual(tuple(master[k] for k in ('CD_CVM', 'CNPJ_CIA', 'DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB')),
                         ('016632', '02.193.750/0001-52', '2025-12-31', '1', '155652', '2026-03-24'))
        self.assertEqual(self.readings[0]['published_at'], '2026-03-24')
        self.assertNotEqual(self.proof['native_package']['retrieved_at'][:10], '2026-03-24')
        self.assertFalse(self.proof['point_in_time_archive_verified'])

    def test_native_zip_members_byte_identical_crc_and_xml_identity(self):
        body, _ = eps.transport(eps.ROOT, 'package')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIsNone(archive.testzip())
            for ref in self.proof['native_members'].values():
                actual = archive.read(ref['original_zip_member'])
                self.assertEqual(actual, primary.read(eps.ROOT, ref['file'], ref['sha256']))
                self.assertEqual(primary.sha(actual), ref['sha256'])
        xml = primary.read(eps.ROOT, self.proof['native_members']['xml']['file'],
                           self.proof['native_members']['xml']['sha256'])
        self.assertIn(b'<CodigoCvm>016632</CodigoCvm>', xml)
        self.assertIn(b'<CnpjEmpresa>02193750000152</CnpjEmpresa>', xml)

    def test_pdf_table_year_class_and_basic_diluted_columns(self):
        refs = [self.proof['native_members']['pdf'],
                next(r for r in self.proof['sources'] if r['sha256'] == eps.PINS['audit'][0])]
        for ref, number in zip(refs, (64, 61)):
            text = primary.normalized(PdfReader(io.BytesIO(primary.read(eps.ROOT, ref['file'], ref['sha256'])))
                                      .pages[number - 1].extract_text())
            self.assertIn('31/12/2025', text)
            self.assertIn('31/12/2024', text)
            self.assertIn('ordinarias 1,3299', text)
            self.assertIn('preferenciais 1,3299', text)
            self.assertIn('basico e diluido', text)
        audit = refs[1]
        text = primary.normalized(PdfReader(io.BytesIO(primary.read(eps.ROOT, audit['file'], audit['sha256'])))
                                  .pages[60].extract_text())
        self.assertIn('individuais e consolidadas', text)
        self.assertIn('em milhares de reais', text)

    def test_reported_class_numerators_shares_and_full_consolidated_dre(self):
        reading, = self.readings
        _, dre, _ = eps.current_tables()
        self.assertEqual(financials._dre_fingerprint(dre, full=True), eps.DRE_SHA)
        self.assertEqual(reading['accounts_scope'], 'consolidated')
        self.assertEqual(reading['parent_profit_brl'], '143653000')
        leaves = {r['share_class']: r for r in reading['basic_eps']}
        self.assertEqual((leaves['ON']['numerator_brl'], leaves['ON']['weighted_shares']), ('136449000', '102602353'))
        self.assertEqual((leaves['PN']['numerator_brl'], leaves['PN']['weighted_shares']), ('7204000', '5416933'))
        self.assertEqual(sum(Decimal(r['numerator_brl']) for r in leaves.values()), Decimal('143653000'))
        self.assertEqual(sum(Decimal(r['weighted_shares']) for r in leaves.values()), Decimal('108019286'))
        self.assertTrue(all(r['value'] == '1.3299' for r in leaves.values()))
        financials._checked_note_eps(dre, reading)
        bad = deepcopy(reading)
        bad['basic_eps'][0]['value'] = '1.4111'
        with self.assertRaises(ValueError):
            financials._checked_note_eps(dre, bad)

    def test_one_current_issuer_reading_for_two_requested_classes(self):
        reading, = self.readings
        self.assertEqual(set(reading['tickers']), {'DEXP3', 'DEXP4'})
        self.assertEqual(reading['manifest_sha256'], eps.REVIEW_SHA)
        saved = json.loads(primary.read(eps.ROOT, eps.review_file(), eps.REVIEW_SHA))
        self.assertEqual(saved, json.loads(primary.encoded(self.proof)))
        self.assertEqual(self.proof['preferred_class_binding']['registered_class'], 'PNA')
        self.assertEqual(self.proof['preferred_class_binding']['financial_source_class'], 'PN')
        self.assertEqual(len(self.proof['preferred_class_binding']['historical_registry']), 6)

    def test_exact_identity_rows_in_all_six_original_fca_archives(self):
        for ticker, history in self.proof['exact_security_primary_history'].items():
            self.assertEqual(len(history), 6)
            for entry in history:
                source = entry['source']
                with zipfile.ZipFile(io.BytesIO(primary.read(eps.ROOT, source['file'], source['sha256']))) as archive:
                    rows = list(csv.DictReader(io.StringIO(archive.read(entry['member']).decode('latin1')), delimiter=';'))
                for locator in entry['rows']:
                    row = rows[locator['csv_line'] - 2]
                    self.assertEqual(row, locator['native_row'])
                    self.assertEqual(row['Codigo_Negociacao'], ticker)
                    self.assertEqual(row['CNPJ_Companhia'], '02.193.750/0001-52')
                    self.assertEqual(row['ID_Documento'], entry['filing_id'])
                    preferred = [r for r in rows if r['CNPJ_Companhia'] == row['CNPJ_Companhia']
                                 and r['ID_Documento'] == row['ID_Documento']
                                 and primary.normalized(r['Valor_Mobiliario']) == 'acoes preferenciais']
                    self.assertEqual({r['Sigla_Classe_Acao_Preferencial'] for r in preferred}, {'PNA'})

    def test_real_production_dispatch_and_current_financial_not_fake_eps(self):
        self.assertTrue(self.normal['DEXP3']['passed'])
        self.assertFalse(self.normal['DEXP4']['passed'])
        self.assertIn('historical_quote_or_share_scale_disagrees', self.normal['DEXP4']['reason'])
        self.assertEqual(len(self.candidate['independent_price_matches']), 125)
        self.assertIsNotNone(self.current)
        self.assertEqual(self.current[1]['fiscal_end'], '2025-12-31')
        self.assertEqual(self.training, [])
        statements = [r for r in self.statements if r['cvm'] == 16632 and r['fiscal_end'] == '2025-12-31']
        self.assertTrue(statements)
        self.assertTrue(any(r['profit_per_share'] == 1.3299 for r in statements))
        preferred, = [r for r in statements if r['share_class'] == 'PNA']
        self.assertEqual(preferred['profit_per_share'], 1.3299)
        self.assertEqual(preferred['financial_method']['source_share_class'], 'PN')

    def test_without_exact_eps_reader_no_older_current_filing_fallback(self):
        with patch.object(eps, 'get_dexp_eps_readings', return_value=([], [])):
            statements, _, _ = financials.prepare_financials(eps.ROOT, list(self.rows.values()), eps.CUTOFF)
        row = self.rows['DEXP3']
        current = financial_features(statements, self.candidate['payments'], eps.CUTOFF,
                                     (row['cvm'], row['share_class'], row['sector_code']))
        self.assertIsNone(current)

    def test_complete_actual_evidence_sources_protected_not_written(self):
        after = protected_files()
        # A parent may publish another candidate concurrently; do not attribute
        # such a pointer update to a read-only loader or revert another writer.
        frozen = {p: value for p, value in self.before.items() if not p.endswith('current.json')
                  and not p.endswith('.py')}
        self.assertTrue(all(after.get(p) == value for p, value in frozen.items()))
        self.assertEqual(self.network_attempts, [])


if __name__ == '__main__':
    unittest.main()
