"""Synthetic layout/security tests, NOT actual COTAHIST or financial admission."""

import calendar
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from . import cotahist_local as c


IDENTITY = c.InstrumentIdentity('TEST3', 'BRTESTACNOR9', '12345', 'ON', 'synthetic_test_identity_only')


def quote(day=date(2021, 9, 30), ticker='TEST3', isin=IDENTITY.isin, close=3059,
          factor=1, market='010', currency='R$', specification='ON', trades=10,
          quantity=100, distribution='100', issuer='SYNTHETIC'):
    # Independent concatenation of the documented widths, not parser slices.
    fields = ['01', day.strftime('%Y%m%d'), '02', ticker.ljust(12), market,
              issuer.ljust(12), specification.ljust(10), '   ', currency.ljust(4)]
    fields.extend(f'{close:013d}' for _ in range(7))
    fields.extend([f'{trades:05d}', f'{quantity:018d}', '000000000000000000',
                   '0000000000000', '0', '00000000', f'{factor:07d}',
                   '0000000000000', isin, distribution])
    result = ''.join(fields).encode('latin1')
    assert len(result) == 245
    return result


def document(rows, year=2021, ending=b'\r\n', generation=None, final_newline=True):
    generation = generation or date(year, 12, 31)
    identity = f'COTAHIST.{year}BOVESPA {generation:%Y%m%d}'.encode('ascii')
    header = b'00' + identity + b' ' * 214
    trailer = b'99' + identity + f'{len(rows) + 2:011d}'.encode('ascii') + b' ' * 203
    assert len(header) == len(trailer) == 245
    return ending.join([header, *rows, trailer]) + (ending if final_newline else b'')


def replace_columns(row, start, value):
    return row[:start] + value + row[start + len(value):]


class OfflineCotahistTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Any accidental downloader or socket use is an immediate test failure.
        self.no_network = patch.object(socket, 'socket', side_effect=AssertionError('network_forbidden'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def write(self, content, name='input.txt'):
        path = self.root / name
        path.write_bytes(content)
        return path

    def zip(self, content, name='COTAHIST_A2021.TXT', compression=zipfile.ZIP_DEFLATED,
            archive_name='input.zip', info=None):
        path = self.root / archive_name
        with zipfile.ZipFile(path, 'w', compression=compression) as archive:
            if info is None:
                info = zipfile.ZipInfo(name)
                # ZipInfo normally normalizes Windows separators on creation;
                # deliberately preserve hostile raw ZIP names for these tests.
                info.filename = info.orig_filename = name
                info.compress_type = compression
            archive.writestr(info, content)
        return path

    def read(self, paths, **kwargs):
        return c.read_local_files(paths if isinstance(paths, list) else [paths],
                                  selected_tickers=kwargs.pop('selected_tickers', ['TEST3']),
                                  identities=kwargs.pop('identities', [IDENTITY]), **kwargs)

    def rejects(self, content, reason, **kwargs):
        with self.assertRaisesRegex(c.CotahistError, reason):
            self.read(self.write(content), **kwargs)

    def test_layout_close_cents_and_literal_identity(self):
        data = document([quote(issuer='SINT\xc9TICO')])
        result = self.read(self.write(data))
        raw = result.histories[0].daily[0]
        self.assertEqual(raw.quoted_close, Decimal('30.59'))
        self.assertEqual(raw.unit_close, Fraction(3059, 100))
        self.assertEqual(raw.unit_close_decimal, '30.59')
        self.assertEqual(raw.isin_literal, IDENTITY.isin)
        self.assertEqual(raw.specification_literal, 'ON        ')
        self.assertEqual(raw.issuer_name_literal.rstrip(), 'SINT\xc9TICO')
        self.assertEqual(raw.line_number, 2)
        self.assertEqual(raw.record_sha256, hashlib.sha256(data.splitlines()[1]).hexdigest())

    def test_thousand_quote_factor_is_not_split_adjustment(self):
        result = self.read(self.write(document([quote(close=1234567, factor=1000)])))
        raw = result.histories[0].daily[0]
        self.assertEqual(raw.quoted_close, Decimal('12345.67'))
        self.assertEqual(raw.unit_close, Fraction(1234567, 100000))
        self.assertEqual(raw.unit_close_decimal, '12.34567')
        self.assertEqual(raw.quotation_factor, 1000)

    def test_quotation_factor_may_change_without_stitching_or_fitting(self):
        rows = [quote(day=date(2021, 9, 29), close=3059, factor=1),
                quote(day=date(2021, 9, 30), close=3059000, factor=1000)]
        history = self.read(self.write(document(rows))).histories[0]
        self.assertEqual([q.unit_close for q in history.daily], [Fraction(3059, 100)] * 2)
        self.assertEqual([q.close_integer for q in history.daily], [3059, 3059000])
        self.assertEqual(history.monthly[0].observation.quotation_factor, 1000)

    def test_max_width_precision_independent_of_decimal_context(self):
        with localcontext() as context:
            context.prec = 3
            raw = self.read(self.write(document([quote(close=9999999999999, factor=1000)]))).histories[0].daily[0]
            self.assertEqual(str(raw.quoted_close), '99999999999.99')
            self.assertEqual(raw.unit_close_decimal, '99999999.99999')
            self.assertEqual(raw.unit_close, Fraction(9999999999999, 100000))

    def test_documented_factors_only(self):
        for factor in (0, 10, 100, 9999999):
            with self.subTest(factor=factor):
                self.rejects(document([quote(factor=factor)]), 'quotation_factor')

    def test_lf_crlf_and_final_record_without_newline(self):
        for ending in (b'\n', b'\r\n'):
            for final in (True, False):
                with self.subTest(ending=ending, final=final):
                    data = document([quote()], ending=ending, final_newline=final)
                    self.assertEqual(self.read(self.write(data)).sources[0].text_sha256,
                                     hashlib.sha256(data).hexdigest())

    def test_245_bytes_not_unicode_codepoints(self):
        data = document([quote(issuer='SINT\xc9TICO')]).decode('latin1').encode('utf-8')
        self.rejects(data, '245')

    def test_invalid_record_lengths_controls_and_bom(self):
        for row in (quote()[:-1], quote() + b' ', replace_columns(quote(), 35, b'\x00'),
                    quote() + b'X' * 10000):
            with self.subTest(length=len(row)):
                self.rejects(document([row]), '245')
        self.rejects(b'\xef\xbb\xbf' + document([quote()]), '245')

    def test_header_required_and_annual_year_bounds(self):
        data = document([quote()])
        self.rejects(data[247:], 'header_required')
        for year in (2020, 2027):
            with self.subTest(year=year):
                self.rejects(document([quote()], year=year), 'header_required')

    def test_header_origin_reserved_and_generation(self):
        data = document([quote()])
        for start, value, reason in ((15, b'B3     X', 'origin'), (31, b'X', 'reserved'),
                                      (23, b'20210230', 'invalid_date'),
                                      (23, b'20201231', 'generation_precedes')):
            with self.subTest(start=start, value=value):
                self.rejects(replace_columns(data, start, value), reason)

    def test_generation_is_not_retrieval_or_local_mtime(self):
        result = self.read(self.write(document([quote()], generation=date(2022, 1, 3))))
        source = result.sources[0].to_dict()
        self.assertEqual(source['generation_date'], '2022-01-03')
        self.assertIsNone(source['retrieved_at'])
        self.assertFalse(source['generation_date_is_retrieval_date'])
        self.assertNotEqual(source['observed_at'][:10], source['generation_date'])
        self.assertFalse(source['authenticity_verified'])

    def test_trailer_count_includes_header_and_trailer(self):
        data = document([quote()])
        self.assertEqual(self.read(self.write(data)).sources[0].total_records_including_header_and_trailer, 3)
        self.rejects(replace_columns(data, 247 * 2 + 31, b'00000000001'), 'trailer_record_count')

    def test_missing_mismatched_or_followed_trailer(self):
        data = document([quote()])
        self.rejects(data[:247 * 2], 'trailer_required')
        self.rejects(replace_columns(data, 247 * 2 + 11, b'2022'), 'trailer_header_identity')
        self.rejects(data + quote() + b'\r\n', 'data_after_trailer')
        self.rejects(data + b'\r\n', '245')
        self.rejects(document([]), 'header_detail_and_trailer_required')

    def test_unknown_repeated_header_and_bad_trading_dates(self):
        self.rejects(document([replace_columns(quote(), 0, b'00')]), 'record_type')
        self.rejects(document([replace_columns(quote(), 0, b'02')]), 'record_type')
        self.rejects(document([replace_columns(quote(), 2, b'20210230')]), 'invalid_date')
        self.rejects(document([quote(day=date(2022, 1, 3))]), 'outside_header_year')
        self.rejects(document([quote()], generation=date(2021, 9, 29)), 'after_generation')

    def test_invalid_numeric_fields(self):
        for start in (10, 108, 147, 152, 210, 242):
            with self.subTest(start=start):
                self.rejects(document([replace_columns(quote(), start, b' ')]), 'non_decimal_field')

    def test_spot_only_not_fractional_options_or_term(self):
        rows = [quote(day=date(2021, 9, 29)), quote(market='020'), quote(market='070'), quote(market='030')]
        result = self.read(self.write(document(rows)))
        self.assertEqual(len(result.histories[0].daily), 1)
        self.assertEqual(dict(result.sources[0].filters)['selected_non_spot_market'], 3)

    def test_foreign_and_ambiguous_currencies_fail_not_convert(self):
        for currency in ('US$', 'BRL', '', ' R$'):
            with self.subTest(currency=currency):
                self.rejects(document([quote(currency=currency)]), 'currency_not_literal_BRL')

    def test_exact_isin_no_stitch_or_suffix_class_inference(self):
        self.rejects(document([quote(isin='BRTESTACNPR1')]), 'literal_ISIN_mismatch')
        external = replace(IDENTITY, share_class='PN')
        raw = self.read(self.write(document([quote(specification='PN')])), identities=[external]).histories[0].daily[0]
        self.assertEqual(raw.identity.share_class, 'PN')
        self.assertEqual(raw.specification_literal, 'PN        ')
        self.assertEqual(raw.identity.ticker, 'TEST3')
        self.assertIn('not_inferred', c.SOURCE_CONTRACT['class_and_cvm'])

    def test_literal_isin_mismatch_in_earlier_window_also_blocks(self):
        paths = [self.write(document([quote(isin='BRTESTACNPR1')]), 'old.txt'),
                 self.write(document([quote(day=date(2026, 9, 30))], year=2026), 'new.txt')]
        with self.assertRaisesRegex(c.CotahistError, 'literal_ISIN_mismatch'):
            self.read(paths)

    def test_no_alias_isin_combination_or_outside_identity(self):
        second = replace(IDENTITY, ticker='TEST4')
        path = self.write(document([quote()]))
        for identities, selected in (([IDENTITY, second], ['TEST3', 'TEST4']),
                                      ([second], ['TEST3']), ([IDENTITY, IDENTITY], ['TEST3'])):
            with self.subTest(identities=identities):
                with self.assertRaisesRegex(c.CotahistError, 'external_identity'):
                    self.read(path, identities=identities, selected_tickers=selected)

    def test_missing_identity_remains_unresolved_not_inferred(self):
        result = self.read(self.write(document([quote()])), identities=[])
        self.assertEqual(result.unresolved_identity_tickers, ('TEST3',))
        self.assertEqual(result.histories, ())
        self.assertEqual(dict(result.sources[0].filters)['selected_unresolved_external_identity'], 1)

    def test_unit_is_allowed_only_by_explicit_identity_not_numeric_suffix(self):
        unit = c.InstrumentIdentity('TEST11', 'BRTESTUNT000', '12345', 'UNIT', 'synthetic_unit_packet')
        result = self.read(self.write(document([quote(ticker='TEST11', isin=unit.isin, specification='UNT')]), 'unit.txt'),
                           selected_tickers=['TEST11'], identities=[unit])
        self.assertEqual(result.histories[0].identity, unit)
        self.assertEqual(result.histories[0].daily[0].specification_literal, 'UNT       ')

    def test_only_explicit_selected_symbols_retained(self):
        rows = [quote(), quote(ticker='ZZZZ4', isin='BRTESTACNPR1')]
        result = self.read(self.write(document(rows)))
        self.assertEqual([h.identity.ticker for h in result.histories], ['TEST3'])
        self.assertEqual(dict(result.sources[0].filters)['outside_selected_scope'], 1)
        self.assertNotIn('ZZZZ4', json.dumps(result.to_dict(True)))

    def test_selection_required_unique_and_at_most_262(self):
        path = self.write(document([quote()]))
        for selected in ([], ['TEST3', 'TEST3'], [f'A{i}' for i in range(263)]):
            with self.subTest(count=len(selected)):
                with self.assertRaisesRegex(c.CotahistError, 'explicit_unique_selection'):
                    self.read(path, identities=[], selected_tickers=selected)

    def test_duplicates_same_price_or_different_distribution_fail(self):
        for other in (quote(), quote(close=3060), quote(distribution='101')):
            with self.subTest(other=other[-3:]):
                self.rejects(document([quote(), other]), 'duplicate_selected_spot_date')

    def test_never_select_duplicate_year_or_revision(self):
        paths = [self.write(document([quote()]), 'a.txt'),
                 self.write(document([quote(day=date(2021, 9, 29))]), 'b.txt')]
        with self.assertRaisesRegex(c.CotahistError, 'duplicate_annual_source'):
            self.read(paths)
        with self.assertRaisesRegex(c.CotahistError, 'duplicate_local_input_path'):
            self.read([paths[0], paths[0]])

    def test_window_filters_old_and_after_cutoff_without_identity_stitch(self):
        p1 = self.write(document([quote(day=date(2021, 8, 31), isin='BRTESTACNPR1'), quote()]), 'a.txt')
        p2 = self.write(document([quote(day=date(2026, 10, 1), isin='BRTESTACNPR1')], year=2026), 'b.txt')
        result = self.read([p1, p2])
        self.assertEqual(len(result.histories[0].daily), 1)
        self.assertEqual(sum(dict(s.filters).get('selected_outside_window', 0) for s in result.sources), 2)

    def test_last_actual_observation_not_31_day_placeholder(self):
        rows = [quote(day=date(2021, 10, 29), close=1001), quote(day=date(2021, 10, 28), close=999)]
        monthly = self.read(self.write(document(rows))).histories[0].monthly[0]
        self.assertEqual(monthly.month_end, date(2021, 10, 31))
        self.assertEqual(monthly.observation.trade_date, date(2021, 10, 29))
        self.assertEqual(monthly.age_calendar_days, 2)
        self.assertEqual(monthly.observation.close_integer, 1001)

    def test_zero_or_no_trades_retained_but_never_monthly_prices(self):
        rows = [quote(day=date(2021, 9, 27), close=0), quote(day=date(2021, 9, 28), trades=0),
                quote(day=date(2021, 9, 29), quantity=0), quote(day=date(2021, 9, 30))]
        history = self.read(self.write(document(rows))).histories[0]
        self.assertEqual(len(history.daily), 4)
        self.assertEqual(len(history.monthly), 1)
        self.assertEqual(history.to_dict()['non_positive_or_non_traded_records'], 3)
        zero = self.read(self.write(document([quote(close=0)]))).histories[0]
        self.assertFalse(zero.monthly)
        self.assertEqual(len(zero.missing_months), 61)

    def test_weekend_quote_rejected_and_staleness_explicit(self):
        self.rejects(document([quote(day=date(2021, 10, 30))]), 'weekend')
        result = self.read(self.write(document([quote(day=date(2021, 9, 1))])))
        self.assertEqual(result.histories[0].to_dict()['months_older_than_7_calendar_days'], ['2021-09-30'])
        self.assertFalse(result.histories[0].to_dict()['monthly_last_trading_day_verified_against_calendar'])

    def test_exact_61_month_window_no_shortened_requirement(self):
        path = self.write(document([quote()]))
        for first, cutoff in ((date(2021, 10, 1), date(2026, 9, 30)),
                              (date(2021, 9, 30), date(2026, 9, 30)),
                              (date(2021, 9, 1), date(2026, 9, 29))):
            with self.subTest(first=first, cutoff=cutoff):
                with self.assertRaisesRegex(c.CotahistError, 'exact_61_calendar_month'):
                    self.read(path, first_month=first, cutoff=cutoff)

    def six_year_fixture(self, omit=None):
        years = {year: [] for year in range(2021, 2027)}
        for index in range(61):
            absolute = 2021 * 12 + 8 + index
            year, zero_month = divmod(absolute, 12)
            month = zero_month + 1
            day = date(year, month, calendar.monthrange(year, month)[1])
            while day.weekday() >= 5:
                day = date.fromordinal(day.toordinal() - 1)
            if (year, month) != omit:
                years[year].append(quote(day=day, close=10000 + index))
        return [self.zip(document(rows, year=year), name=f'COTAHIST_A{year}.TXT',
                         archive_name=f'{year}.zip') for year, rows in years.items()]

    def test_full_six_year_stream_61_months_60_intervals_no_admission(self):
        paths = self.six_year_fixture()
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        result = self.read(paths)
        history = result.histories[0]
        self.assertEqual((len(history.daily), len(history.monthly)), (61, 61))
        self.assertTrue(history.complete_61_months)
        self.assertEqual(history.to_dict()['consecutive_monthly_price_intervals'], 60)
        self.assertEqual(history.monthly[29].observation.trade_date, date(2024, 2, 29))
        self.assertFalse(result.to_dict()['model_admission'])
        self.assertFalse(history.to_dict()['model_admission'])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_missing_month_not_forward_filled_or_zero_filled(self):
        history = self.read(self.six_year_fixture(omit=(2024, 2))).histories[0]
        self.assertEqual(len(history.monthly), 60)
        self.assertFalse(history.complete_61_months)
        self.assertEqual(history.missing_months, (date(2024, 2, 29),))
        self.assertIsNone(history.to_dict()['consecutive_monthly_price_intervals'])

    def test_zip_and_plain_receipts_distinguish_container_from_text(self):
        data = document([quote()])
        zipped = self.zip(data)
        receipt = self.read(zipped).sources[0]
        self.assertEqual(receipt.sha256, hashlib.sha256(zipped.read_bytes()).hexdigest())
        self.assertEqual(receipt.text_sha256, hashlib.sha256(data).hexdigest())
        self.assertNotEqual(receipt.sha256, receipt.text_sha256)
        self.assertEqual(len(receipt.zip_crc32), 8)
        self.assertEqual(receipt.uncompressed_bytes, len(data))
        plain = self.read(self.write(data)).sources[0]
        self.assertEqual(plain.sha256, plain.text_sha256)
        self.assertIsNone(plain.zip_crc32)

    def test_safe_zip_paths(self):
        for name in ('../a.txt', '/a.txt', 'C:/a.txt', 'x\\a.txt', 'a/../b.txt',
                     'a//b.txt', 'a./b.txt', 'NUL.txt', 'COM1/x.txt', 'a.txt:stream', 'a\x00.txt'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(c.CotahistError, 'unsafe_or_duplicate'):
                    self.read(self.zip(document([quote()]), name=name))

    def test_zip_symlink_encrypted_and_nested_data_rejected(self):
        info = zipfile.ZipInfo('a.txt')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(c.CotahistError, 'member_type'):
            self.read(self.zip(document([quote()]), info=info))
        with self.assertRaisesRegex(c.CotahistError, 'extension_size'):
            self.read(self.zip(document([quote()]), name='nested.zip'))
        path = self.zip(document([quote()]))
        raw = path.read_bytes()
        central = raw.index(b'PK\x01\x02')
        raw = replace_columns(raw, central + 8, b'\x01\x00')
        path.write_bytes(raw)
        with self.assertRaisesRegex(c.CotahistError, 'encryption'):
            self.read(path)

    def test_zip_exactly_one_txt_and_case_duplicate_members(self):
        for names in (('a.txt', 'b.txt'), ('a.txt', 'A.TXT')):
            with self.subTest(names=names):
                path = self.root / 'multi.zip'
                with zipfile.ZipFile(path, 'w') as archive:
                    for name in names:
                        archive.writestr(name, document([quote()]))
                with self.assertRaisesRegex(c.CotahistError, 'one_annual|duplicate_zip_member'):
                    self.read(path)

    def test_zip_crc_corruption_rejected_no_partial_result(self):
        data = document([quote()])
        path = self.zip(data, compression=zipfile.ZIP_STORED)
        raw = path.read_bytes()
        start = raw.index(data)
        path.write_bytes(replace_columns(raw, start + 247 + 27, b'X'))
        with self.assertRaisesRegex(c.CotahistError, 'CRC'):
            self.read(path)

    def test_zip_truncated_or_trailing_data_rejected(self):
        path = self.zip(document([quote()]))
        raw = path.read_bytes()
        for changed in (raw[:-10], raw + b'junk'):
            with self.subTest(length=len(changed)):
                path.write_bytes(changed)
                with self.assertRaises(c.CotahistError):
                    self.read(path)

    def test_central_directory_preflight_budgets_before_zipfile(self):
        path = self.zip(document([quote()]))
        with patch.object(c.zipfile, 'ZipFile', side_effect=AssertionError('must_preflight_first')):
            with self.assertRaisesRegex(c.CotahistError, 'central_directory_budget'):
                self.read(path, limits=c.Limits(max_central_directory_bytes=1))
        raw = path.read_bytes()
        end = raw.rfind(b'PK\x05\x06')
        path.write_bytes(replace_columns(raw, end + 8, struct.pack('<HH', 9, 9)))
        with self.assertRaisesRegex(c.CotahistError, 'central_directory_budget'):
            self.read(path)

    def test_zip64_end_directory_supported_without_large_allocation(self):
        path = self.zip(document([quote()]))
        raw = path.read_bytes()
        end_at = raw.rfind(b'PK\x05\x06')
        end = struct.unpack('<4s4H2IH', raw[end_at:])
        end64 = struct.pack('<4sQ2H2I4Q', b'PK\x06\x06', 44, 45, 45, 0, 0,
                            end[3], end[4], end[5], end[6])
        locator = struct.pack('<4sIQI', b'PK\x06\x07', 0, end_at, 1)
        sentinel = struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, 65535, 65535,
                               0xffffffff, 0xffffffff, 0)
        path.write_bytes(raw[:end_at] + end64 + locator + sentinel)
        self.assertEqual(self.read(path).histories[0].daily[0].close_integer, 3059)

    def test_compression_size_and_memory_limits(self):
        path = self.zip(document([quote()]))
        for limits in (c.Limits(max_compression_ratio=1), c.Limits(max_input_bytes=1),
                       c.Limits(max_archive_bytes=1), c.Limits(max_uncompressed_bytes=1),
                       c.Limits(max_total_uncompressed_bytes=1)):
            with self.subTest(limits=limits):
                with self.assertRaisesRegex(c.CotahistError, 'budget_exceeded'):
                    self.read(path, limits=limits)
        data = document([quote(day=date(2021, 9, 29)), quote()])
        self.rejects(data, 'selected_record_memory_budget', limits=c.Limits(max_selected_records=1))
        with self.assertRaisesRegex(c.CotahistError, 'one_through_six'):
            self.read([])

    def test_actual_byte_budget_not_only_zip_metadata(self):
        stream = io.BytesIO(document([quote()]))
        with self.assertRaisesRegex(c.CotahistError, 'actual_uncompressed_byte_budget'):
            c._scan_text(stream, 0, {'TEST3'}, {'TEST3': IDENTITY}, date(2021, 9, 1),
                         date(2026, 9, 30), c.Limits(), 246, {'TEST3': {}}, 0)

    def test_no_read_all_or_extract_and_nonselected_rows_not_retained(self):
        data = document([quote(ticker='XXXX4')] * 5000 + [quote()])
        paths = [self.zip(data), self.write(data)]
        with (patch.object(Path, 'read_bytes', side_effect=AssertionError('no_read_all')),
              patch.object(zipfile.ZipFile, 'extractall', side_effect=AssertionError('no_extract')),
              patch.object(zipfile.ZipFile, 'extract', side_effect=AssertionError('no_extract'))):
            for path in paths:
                with self.subTest(path=path.suffix):
                    result = self.read(path)
                    self.assertEqual(len(result.histories[0].daily), 1)
                    self.assertEqual(result.sources[0].detail_records, 5001)

    def test_stream_read_sizes_are_bounded(self):
        class Guarded(io.BytesIO):
            def read(self, size=-1):
                self.assert_size(size, 1024 * 1024)
                return super().read(size)

            def readline(self, size=-1):
                self.assert_size(size, 248)
                return super().readline(size)

            @staticmethod
            def assert_size(size, bound):
                if not 0 <= size <= bound:
                    raise AssertionError('unbounded_read')

        data = document([quote()])
        self.assertEqual(c._hash(Guarded(data), len(data)), hashlib.sha256(data).hexdigest())
        parsed = c._scan_text(Guarded(data), 0, {'TEST3'}, {'TEST3': IDENTITY}, date(2021, 9, 1),
                               date(2026, 9, 30), c.Limits(), 10000, {'TEST3': {}}, 0)
        self.assertEqual(parsed[4], 3)

    def test_hash_detects_growth_and_shrink(self):
        with self.assertRaisesRegex(c.CotahistError, 'grew_during_hash'):
            c._hash(io.BytesIO(b'abc'), 2)
        with self.assertRaisesRegex(c.CotahistError, 'shrank_during_hash'):
            c._hash(io.BytesIO(b'ab'), 3)

    def test_input_changed_during_scan_not_issued_as_verified(self):
        path = self.write(document([quote()]))
        original = c._scan_text

        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            with path.open('ab') as stream:
                stream.write(b'X')
            return result

        with patch.object(c, '_scan_text', side_effect=changing):
            with self.assertRaisesRegex(c.CotahistError, 'changed_during_read'):
                self.read(path)

    def test_same_size_input_modification_is_rejected(self):
        path = self.write(document([quote()]))
        original = c._scan_text

        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            timestamp = path.stat().st_mtime_ns + 1_000_000_000
            os.utime(path, ns=(timestamp, timestamp))
            return result

        with patch.object(c, '_scan_text', side_effect=changing):
            with self.assertRaisesRegex(c.CotahistError, 'changed_during_read'):
                self.read(path)

    def test_plain_hash_binds_parsed_bytes_even_if_mtime_is_restored(self):
        path = self.write(document([quote()]))
        original = c._scan_text

        def changing(*args, **kwargs):
            timestamp = path.stat().st_mtime_ns
            with path.open('r+b') as stream:
                stream.seek(247 + 108)
                stream.write(b'0000000003060')
            os.utime(path, ns=(timestamp, timestamp))
            return original(*args, **kwargs)

        with patch.object(c, '_scan_text', side_effect=changing):
            with self.assertRaisesRegex(c.CotahistError, 'text_input_bytes_changed_after_hash'):
                self.read(path)

    def test_zip_hash_binds_container_even_if_mtime_is_restored(self):
        path = self.zip(document([quote()]))
        original = c._scan_text

        def changing(*args, **kwargs):
            timestamp = path.stat().st_mtime_ns
            with path.open('r+b') as stream:
                stream.seek(-1, 2)
                stream.write(b'X')
            os.utime(path, ns=(timestamp, timestamp))
            return original(*args, **kwargs)

        with patch.object(c, '_scan_text', side_effect=changing):
            with self.assertRaisesRegex(c.CotahistError, 'zip_input_bytes_changed_after_hash'):
                self.read(path)

    def test_local_inputs_only_not_urls_unc_directory_or_unsupported(self):
        for path in ('https://www.b3.com.br/x.zip', '//server/share/a.zip', '\\\\server\\share\\a.zip', self.root):
            with self.subTest(path=path):
                with self.assertRaises(c.CotahistError):
                    self.read(path)
        with self.assertRaisesRegex(c.CotahistError, 'ZIP_or_TXT'):
            self.read(self.write(document([quote()]), 'input.json'))

    def test_annual_filename_and_header_year_must_agree(self):
        with self.assertRaisesRegex(c.CotahistError, 'filename_header_year_mismatch'):
            self.read(self.zip(document([quote()]), name='COTAHIST_A2022.TXT'))
        with self.assertRaisesRegex(c.CotahistError, 'filename_header_year_mismatch'):
            self.read(self.zip(document([quote()]), archive_name='COTAHIST_A2022.ZIP'))

    def test_input_file_order_does_not_select_favorable_prices(self):
        paths = self.six_year_fixture()
        forward = self.read(paths).histories[0]
        backward = self.read(list(reversed(paths))).histories[0]
        self.assertEqual([(p.trade_date, p.unit_close) for p in forward.daily],
                         [(p.trade_date, p.unit_close) for p in backward.daily])
        self.assertEqual([(p.month_end, p.observation.unit_close) for p in forward.monthly],
                         [(p.month_end, p.observation.unit_close) for p in backward.monthly])

    def test_serialization_is_typed_evidence_never_api_payload(self):
        result = self.read(self.write(document([quote()])))
        report = result.to_dict(True)
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertFalse(report['source_contract']['is_b3_api_response'])
        self.assertFalse(report['source_contract']['authenticity_verified'])
        self.assertEqual(report['histories'][0]['daily'][0]['unit_close_BRL_decimal'], '30.59')
        self.assertNotIn('dateClosingPricePriorExDate', json.dumps(report))
        self.assertNotIn('chart', report)
        self.assertNotIn('daily', result.to_dict()['histories'][0])

    def test_source_contract_pins_primary_parser_commits_and_units(self):
        references = c.SOURCE_CONTRACT['layout_references']
        self.assertTrue(any(c.RB3_COMMIT in r['url'] for r in references))
        self.assertTrue(any(c.BRASA_COMMIT in r['url'] for r in references))
        self.assertIn('100 * integer_FATCOT', c.SOURCE_CONTRACT['unit_price_exact_formula'])
        self.assertTrue(all('index' in r['access'] or 'primary_maintained_parser' in r['access'] for r in references))

    def test_cli_requires_real_scope_and_exclusive_evidence_output(self):
        from . import dividend_scope

        data = self.write(document([quote()]))
        identities = self.write(json.dumps({'schema_version': 1, 'identities': [
            {name: getattr(IDENTITY, name) for name in IDENTITY.__dataclass_fields__}]}).encode(), 'identities.json')
        output = self.root / 'evidence.json'
        argv = ['--input', str(data), '--data-dir', str(self.root), '--identities', str(identities),
                '--output', str(output)]
        with self.assertRaisesRegex(c.CotahistError, 'hash_checked_262'):
            c.main(argv)
        self.assertFalse(output.exists())
        scope = {'selected_count': 262, 'selected_tickers': ['TEST3'] + [f'X{i:03d}' for i in range(261)], 'id': 'a' * 64}
        with (patch.object(dividend_scope, 'load_scope', return_value=scope),
              patch.object(dividend_scope, 'scope_evidence', return_value=[{'kind': 'stock_selection', 'id': scope['id']}]),
              patch('builtins.print')):
            c.main(argv)
            original = output.read_bytes()
            report = json.loads(original)
            self.assertEqual(report['selected_count'], 262)
            self.assertEqual(len(report['unresolved_identity_tickers']), 261)
            self.assertFalse(report['model_admission'])
            with self.assertRaises(FileExistsError):
                c.main(argv)
            self.assertEqual(output.read_bytes(), original)

    def test_cli_selection_receipts_cannot_race_into_different_scope(self):
        from . import dividend_scope

        scope = {'selected_count': 262, 'selected_tickers': ['TEST3'], 'id': 'a' * 64}
        argv = ['--input', str(self.root / 'not-yet-provided.zip'), '--data-dir', str(self.root),
                '--identities', str(self.root / 'identities.json'), '--output', str(self.root / 'report.json')]
        with (patch.object(dividend_scope, 'load_scope', return_value=scope),
              patch.object(dividend_scope, 'scope_evidence', return_value=[{'kind': 'stock_selection', 'id': 'b' * 64}])):
            with self.assertRaisesRegex(c.CotahistError, 'selection_changed'):
                c.main(argv)
        self.assertFalse((self.root / 'report.json').exists())

    def test_json_duplicate_keys_and_invalid_identities_fail(self):
        with self.assertRaisesRegex(c.CotahistError, 'duplicate_JSON_key'):
            json.loads('{"isin":"a","isin":"b"}', object_pairs_hook=c._unique_json)
        for changes in ({'isin': ''}, {'cvm': 'unknown'}, {'identity_reference': ''}):
            with self.subTest(changes=changes):
                with self.assertRaises(c.CotahistError):
                    replace(IDENTITY, **changes)
        with self.assertRaisesRegex(c.CotahistError, 'positive_integers'):
            c.Limits(max_selected_records=0)


if __name__ == '__main__':
    unittest.main()
