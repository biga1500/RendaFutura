"""Offline, fail-closed COTAHIST annual-file reader. No downloader or model hook.

API: read_local_files(paths, selected_tickers=..., identities=...). The explicit
selection is mandatory; missing external identities remain unresolved. The CLI
binds that selection to the existing hash-checked 262-stock scope and requires a
local identity manifest: {"schema_version": 1, "identities": [{"ticker": ...,
"isin": ..., "cvm": ..., "share_class": ..., "identity_reference": ...}]}.

PREULT is an integer with two implicit decimals, quoted for FATCOT securities.
Unit BRL price = PREULT / (100 * FATCOT), NOT PREULT / 100 alone. No adjustment
for splits, bonuses, dividends or other corporate actions is performed here.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile
import zlib


class CotahistError(ValueError):
    """Invalid, ambiguous or over-budget local input; no partial result issued."""


RB3_COMMIT = '4b6f9017ec802e978085291d46cce475e78e5668'
BRASA_COMMIT = '7d8ebbc237de522931e17b88e79fb50dcd30c2be'
_CONTROL_BYTES = re.compile(rb'[\x00-\x1f\x7f]')
SOURCE_CONTRACT = {
    'schema_version': 1,
    'kind': 'cotahist_local_typed_raw_price_evidence_v1',
    'provenance': 'user_provided_local_file_not_independently_authenticated',
    'authenticity_verified': False,
    'network_access': 'none_no_downloader_no_url_inputs',
    'is_b3_api_response': False,
    'record_bytes_excluding_line_ending': 245,
    'encoding': 'latin1_single_byte',
    'record_types': {'header': '00', 'quote': '01', 'trailer': '99'},
    'trailer_count_includes_header_and_trailer': True,
    'market_code': '010',
    'currency_literal': 'R$',
    'price_field': 'PREULT_bytes_109_through_121_implicit_2_decimals',
    'quotation_factor_field': 'FATCOT_bytes_211_through_217_integer_1_or_1000',
    'unit_price_exact_formula': 'integer_PREULT / (100 * integer_FATCOT)',
    'isin_field': 'CODISI_bytes_231_through_242_literal_exact_match',
    'class_and_cvm': 'external_identity_manifest_not_present_in_COTAHIST_not_inferred',
    'monthly_rule': 'latest_observed_positive_traded_spot_quote_in_each_calendar_month',
    'no_zero_fill_or_forward_fill': True,
    'required_months': 61,
    'required_return_periods': 60,
    'raw_price_returns_are_not_total_returns': True,
    'corporate_action_reconciliation': 'required_not_performed',
    'primary_cached_quote_comparison': 'required_not_performed',
    'model_admission': False,
    'generation_date_is_retrieval_date': False,
    'retrieved_at': None,
    'layout_references': [
        {
            'url': 'https://www.b3.com.br/data/files/33/67/B9/50/D84057102C784E47AC094EA8/SeriesHistoricas_Layout.pdf',
            'access': 'search_engine_indexed_layout_metadata_only_no_B3_GET',
            'revision': '02_2020-10-05',
            'supports': '245_bytes_header_positions_PREULT_FATCOT_units_ISIN',
        },
        {
            'url': 'https://bvmf.bmfbovespa.com.br/pt-br/download/SeriesHistoricas_Layout.pdf',
            'access': 'search_engine_indexed_layout_metadata_only_no_bvmf_GET',
            'supports': 'trailer_positions_and_count_including_header_and_trailer',
        },
        {
            'url': 'https://github.com/ropensci/rb3/blob/' + RB3_COMMIT + '/inst/extdata/templates/b3-cotahist-yearly.yaml',
            'access': 'primary_maintained_parser_source_only_no_quotation_fixture',
            'supports': 'latin1_field_widths_close_implicit_2_decimals_factor_integer',
        },
        {
            'url': 'https://github.com/ropensci/rb3/blob/' + RB3_COMMIT + '/R/scraper-cotahist.R',
            'access': 'primary_maintained_parser_source_only',
            'supports': 'spot_market_10_and_prices_not_corporate_action_adjusted',
        },
        {
            'url': 'https://github.com/wilsonfreitas/brasa/blob/' + BRASA_COMMIT + '/brasa/files/templates/b3/equities/b3-cotahist-yearly.yaml',
            'access': 'primary_maintained_parser_source_only',
            'supports': 'independent_confirmation_of_field_widths_and_price_scale',
        },
    ],
    'documentation_checked_on': '2026-10-25',
}


@dataclass(frozen=True, slots=True)
class Limits:
    max_files: int = 6
    max_input_bytes: int = 8 * 1024**3
    max_archive_bytes: int = 2 * 1024**3
    max_uncompressed_bytes: int = 8 * 1024**3
    max_total_uncompressed_bytes: int = 48 * 1024**3
    max_zip_members: int = 8
    max_central_directory_bytes: int = 64 * 1024
    max_compression_ratio: int = 300
    max_selected_records: int = 1_000_000

    def __post_init__(self):
        if any(type(getattr(self, name)) is not int or getattr(self, name) <= 0
               for name in self.__dataclass_fields__):
            raise CotahistError('limits_must_be_positive_integers')


@dataclass(frozen=True, slots=True)
class InstrumentIdentity:
    ticker: str
    isin: str
    cvm: str
    share_class: str
    identity_reference: str

    def __post_init__(self):
        patterns = {'ticker': r'[A-Z0-9]{1,12}', 'isin': r'[A-Z]{2}[A-Z0-9]{9}[0-9]',
                    'cvm': r'[0-9]{1,8}', 'share_class': r'[A-Z][A-Z0-9 ]{0,19}'}
        for field, pattern in patterns.items():
            if not isinstance(getattr(self, field), str) or not re.fullmatch(pattern, getattr(self, field)):
                raise CotahistError('invalid_external_identity_' + field)
        if not isinstance(self.identity_reference, str) or not 1 <= len(self.identity_reference) <= 1000:
            raise CotahistError('external_identity_reference_required')


def _scaled(integer, places):
    digits = str(integer).zfill(places + 1)
    return digits[:-places] + '.' + digits[-places:]


@dataclass(frozen=True, slots=True)
class RawPrice:
    identity: InstrumentIdentity
    trade_date: date
    close_integer: int
    quotation_factor: int
    trades: int
    traded_quantity: int
    market_literal: str
    currency_literal: str
    isin_literal: str
    specification_literal: str
    issuer_name_literal: str
    bdi_literal: str
    distribution_literal: str
    source_index: int
    line_number: int
    record_sha256: str

    @property
    def quoted_close(self):
        return Decimal(_scaled(self.close_integer, 2))

    @property
    def unit_close(self):
        return Fraction(self.close_integer, 100 * self.quotation_factor)

    @property
    def unit_close_decimal(self):
        return _scaled(self.close_integer, 2 if self.quotation_factor == 1 else 5)

    @property
    def usable(self):
        return self.close_integer > 0 and self.trades > 0 and self.traded_quantity > 0

    def to_dict(self):
        return {
            'ticker': self.identity.ticker, 'isin_literal': self.isin_literal,
            'trade_date': self.trade_date.isoformat(),
            'PREULT_integer': self.close_integer, 'FATCOT_integer': self.quotation_factor,
            'quoted_close_decimal': str(self.quoted_close),
            'unit_close_BRL_decimal': self.unit_close_decimal,
            'unit_close_BRL_exact': {'numerator': self.unit_close.numerator,
                                     'denominator': self.unit_close.denominator},
            'trades': self.trades, 'traded_quantity': self.traded_quantity,
            'market_literal': self.market_literal, 'currency_literal': self.currency_literal,
            'specification_literal': self.specification_literal,
            'issuer_name_literal': self.issuer_name_literal, 'bdi_literal': self.bdi_literal,
            'distribution_literal': self.distribution_literal,
            'source_index': self.source_index, 'line_number': self.line_number,
            'record_sha256_excluding_line_ending': self.record_sha256,
            'usable_positive_traded_quote': self.usable,
        }


@dataclass(frozen=True, slots=True)
class MonthlyPrice:
    month_end: date
    observation: RawPrice

    @property
    def age_calendar_days(self):
        return (self.month_end - self.observation.trade_date).days

    def to_dict(self):
        return {'month_end_bucket_not_trade_date': self.month_end.isoformat(),
                'age_calendar_days': self.age_calendar_days, **self.observation.to_dict()}


@dataclass(frozen=True, slots=True)
class InstrumentHistory:
    identity: InstrumentIdentity
    daily: tuple[RawPrice, ...]
    monthly: tuple[MonthlyPrice, ...]
    missing_months: tuple[date, ...]

    @property
    def complete_61_months(self):
        return len(self.monthly) == 61 and not self.missing_months

    def to_dict(self, include_daily=False):
        value = {
            'identity': {name: getattr(self.identity, name) for name in self.identity.__dataclass_fields__},
            'identity_binding': 'exact_ticker_and_ISIN_external_CVM_and_class_not_inferred',
            'daily_records': len(self.daily),
            'non_positive_or_non_traded_records': sum(not q.usable for q in self.daily),
            'monthly_records': len(self.monthly),
            'complete_61_observed_months': self.complete_61_months,
            'consecutive_monthly_price_intervals': 60 if self.complete_61_months else None,
            'missing_months': [d.isoformat() for d in self.missing_months],
            'months_older_than_7_calendar_days': [m.month_end.isoformat() for m in self.monthly
                                                if m.age_calendar_days > 7],
            'monthly_last_trading_day_verified_against_calendar': False,
            'model_admission': False,
            'monthly': [m.to_dict() for m in self.monthly],
        }
        if include_daily:
            value['daily'] = [q.to_dict() for q in self.daily]
        return value


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    local_path: str
    sha256: str
    input_bytes: int
    member: str | None
    text_sha256: str
    zip_crc32: str | None
    year: int
    generation_date: date
    header_name_literal: str
    origin_literal: str
    detail_records: int
    total_records_including_header_and_trailer: int
    uncompressed_bytes: int
    observed_at: str
    filters: tuple[tuple[str, int], ...]

    def to_dict(self):
        value = {name: getattr(self, name) for name in self.__dataclass_fields__}
        value['generation_date'] = self.generation_date.isoformat()
        value['filters'] = dict(self.filters)
        return {**value, 'retrieved_at': None, 'generation_date_is_retrieval_date': False,
                'provenance': SOURCE_CONTRACT['provenance'], 'authenticity_verified': False,
                'integrity_checks': ['sha256', 'record_widths', 'header_trailer_counts']
                                    + (['zip_crc32'] if self.member is not None else []),
                'integrity_is_origin_authentication': False}


@dataclass(frozen=True, slots=True)
class LocalPriceEvidence:
    selected_tickers: tuple[str, ...]
    unresolved_identity_tickers: tuple[str, ...]
    first_month: date
    cutoff: date
    sources: tuple[SourceReceipt, ...]
    histories: tuple[InstrumentHistory, ...]

    def to_dict(self, include_daily=False):
        return {
            'source_contract': json.loads(json.dumps(SOURCE_CONTRACT)),
            'selection_binding': 'explicit_caller_selection_no_unrestricted_fallback',
            'selected_count': len(self.selected_tickers),
            'selected_tickers': list(self.selected_tickers),
            'unresolved_identity_tickers': list(self.unresolved_identity_tickers),
            'first_month': self.first_month.isoformat(), 'cutoff': self.cutoff.isoformat(),
            'sources': [s.to_dict() for s in self.sources],
            'histories': [h.to_dict(include_daily) for h in self.histories],
            'model_admission': False,
        }


def _month_end(year, month):
    following = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return date.fromordinal(following.toordinal() - 1)


def _months(first_month, cutoff):
    if (type(first_month) is not date or type(cutoff) is not date or first_month.day != 1
            or cutoff != _month_end(cutoff.year, cutoff.month)
            or not 2021 <= first_month.year <= cutoff.year <= 2026
            or (cutoff.year - first_month.year) * 12 + cutoff.month - first_month.month != 60):
        raise CotahistError('exact_61_calendar_month_window_with_month_end_cutoff_required')
    return tuple(_month_end((first_month.year * 12 + first_month.month - 1 + i) // 12,
                            (first_month.month - 1 + i) % 12 + 1) for i in range(61))


def _digits(raw, label):
    if not raw or not raw.isdigit():
        raise CotahistError('non_decimal_field_' + label)
    return int(raw)


def _date(raw, label):
    _digits(raw, label)
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError as error:
        raise CotahistError('invalid_date_' + label) from error


def _local_path(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or '://' in raw or raw.startswith(('\\\\', '//')):
        raise CotahistError('local_file_only_no_URL_or_UNC')
    path = Path(raw)
    if path.is_symlink():
        raise CotahistError('symlink_input_not_allowed')
    if not path.is_file():
        raise CotahistError('local_regular_file_required')
    return path.resolve()


def _stat_fingerprint(value):
    if not stat.S_ISREG(value.st_mode):
        raise CotahistError('local_regular_file_required')
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _fingerprint(stream):
    return _stat_fingerprint(os.fstat(stream.fileno()))


def _hash(stream, expected_bytes):
    digest = hashlib.sha256()
    total = 0
    while chunk := stream.read(min(1024 * 1024, expected_bytes - total + 1)):
        total += len(chunk)
        if total > expected_bytes:
            raise CotahistError('local_input_grew_during_hash')
        digest.update(chunk)
    if total != expected_bytes:
        raise CotahistError('local_input_shrank_during_hash')
    stream.seek(0)
    return digest.hexdigest()


def _zip_preflight(stream, size, limits):
    # Bound central-directory allocation BEFORE ZipFile reads it (including ZIP64).
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    at = tail.rfind(b'PK\x05\x06')
    if at < 0 or len(tail) - at < 22:
        raise CotahistError('missing_zip_end_record')
    _, disk, cd_disk, count_disk, count, cd_size, cd_offset, comment_size = struct.unpack_from('<4s4H2IH', tail, at)
    eocd_at = size - len(tail) + at
    if at + 22 + comment_size != len(tail) or disk or cd_disk or count_disk != count:
        raise CotahistError('unsupported_multidisk_or_trailing_zip_data')
    cd_end_limit = eocd_at
    if count == 65535 or cd_size == 0xffffffff or cd_offset == 0xffffffff:
        if eocd_at < 20:
            raise CotahistError('missing_zip64_locator')
        stream.seek(eocd_at - 20)
        locator = stream.read(20)
        signature, start_disk, offset, disks = struct.unpack('<4sIQI', locator)
        if signature != b'PK\x06\x07' or start_disk or disks != 1 or offset + 56 > eocd_at - 20:
            raise CotahistError('invalid_zip64_locator')
        stream.seek(offset)
        values = stream.read(56)
        if len(values) != 56:
            raise CotahistError('truncated_zip64_end_record')
        signature, record_size, _, _, disk, cd_disk, count_disk, count, cd_size, cd_offset = struct.unpack('<4sQ2H2I4Q', values)
        if (signature != b'PK\x06\x06' or not 44 <= record_size <= 1024 or disk or cd_disk
                or count_disk != count or offset + record_size + 12 != eocd_at - 20):
            raise CotahistError('invalid_zip64_end_record')
        cd_end_limit = offset
    if not 1 <= count <= limits.max_zip_members or not 0 < cd_size <= limits.max_central_directory_bytes:
        raise CotahistError('zip_central_directory_budget_exceeded')
    if cd_offset < 0 or cd_offset + cd_size != cd_end_limit:
        raise CotahistError('invalid_zip_central_directory_bounds')
    stream.seek(0)
    if stream.read(4) != b'PK\x03\x04':
        raise CotahistError('prefixed_or_non_zip_input')
    stream.seek(0)


def _zip_member(archive, limits):
    members = archive.infolist()
    if not 1 <= len(members) <= limits.max_zip_members:
        raise CotahistError('zip_member_budget_exceeded')
    names = set()
    files = []
    for info in members:
        name = info.filename
        parts = PurePosixPath(name).parts
        if (not name or info.orig_filename != name or '\\' in name or ':' in name or name.startswith('/')
                or any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in name.rstrip('/').split('/'))
                or any(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I) for p in parts)
                or any(ord(c) < 32 or ord(c) == 127 for c in name) or name.casefold() in names):
            raise CotahistError('unsafe_or_duplicate_zip_member_path')
        names.add(name.casefold())
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or stat.S_ISLNK(mode) or info.flag_bits & 1:
            raise CotahistError('unsupported_zip_member_type_or_encryption')
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise CotahistError('unsupported_zip_compression')
        if info.is_dir():
            if info.file_size or info.compress_size:
                raise CotahistError('nonempty_zip_directory')
            continue
        if (not name.lower().endswith('.txt') or not 0 < info.file_size <= limits.max_uncompressed_bytes
                or not info.compress_size or info.file_size > info.compress_size * limits.max_compression_ratio):
            raise CotahistError('zip_member_extension_size_or_expansion_budget_exceeded')
        files.append(info)
    if len(files) != 1:
        raise CotahistError('exactly_one_annual_TXT_member_required')
    return files[0]


def _scan_text(stream, source_index, selected, identities, first_month, cutoff, limits,
               remaining_bytes, daily, selected_count):
    digest = hashlib.sha256()
    total_bytes = 0
    line_number = 0
    header = None
    trailer = None
    generation = None
    year = None
    counts = Counter()
    while raw := stream.readline(248):
        line_number += 1
        total_bytes += len(raw)
        if total_bytes > min(limits.max_uncompressed_bytes, remaining_bytes):
            raise CotahistError('actual_uncompressed_byte_budget_exceeded')
        digest.update(raw)
        line = raw[:-2] if raw.endswith(b'\r\n') else raw[:-1] if raw.endswith(b'\n') else raw
        if len(line) != 245 or _CONTROL_BYTES.search(line):
            raise CotahistError('record_not_245_printable_single_byte_columns_line_' + str(line_number))
        kind = line[:2]
        if trailer is not None:
            raise CotahistError('data_after_trailer')
        if line_number == 1:
            if kind != b'00' or not re.fullmatch(rb'COTAHIST\.202[1-6]', line[2:15]):
                raise CotahistError('annual_2021_through_2026_header_required')
            if line[15:23] != b'BOVESPA ' or line[31:] != b' ' * 214:
                raise CotahistError('invalid_header_origin_or_reserved_columns')
            header = line
            year = int(line[11:15])
            generation = _date(line[23:31], 'generation')
            if generation.year < year:
                raise CotahistError('generation_precedes_reference_year')
            continue
        if kind == b'99':
            if line[2:31] != header[2:31] or line[42:] != b' ' * 203:
                raise CotahistError('trailer_header_identity_or_reserved_mismatch')
            if _digits(line[31:42], 'trailer_count') != line_number:
                raise CotahistError('trailer_record_count_mismatch_including_header_and_trailer')
            trailer = line
            continue
        if kind != b'01':
            raise CotahistError('unknown_or_repeated_record_type')
        counts['all_detail_records'] += 1
        trade_date = _date(line[2:10], 'trading')
        if trade_date.year != year or trade_date > generation:
            raise CotahistError('trading_date_outside_header_year_or_after_generation')
        ticker = line[12:24].decode('ascii').rstrip(' ')
        if ticker not in selected:
            counts['outside_selected_scope'] += 1
            continue
        if not first_month <= trade_date <= cutoff:
            counts['selected_outside_window'] += 1
            continue
        if line[24:27] != b'010':
            counts['selected_non_spot_market'] += 1
            continue
        if ticker not in identities:
            counts['selected_unresolved_external_identity'] += 1
            continue
        identity = identities[ticker]
        if line[230:242] != identity.isin.encode('ascii'):
            raise CotahistError('selected_literal_ISIN_mismatch_no_stitch_' + ticker + '_' + trade_date.isoformat())
        if line[52:56].rstrip(b' ') != b'R$':
            raise CotahistError('selected_currency_not_literal_BRL_' + ticker)
        if trade_date.weekday() >= 5:
            raise CotahistError('selected_spot_quote_on_weekend')
        factor = _digits(line[210:217], 'FATCOT')
        if factor not in (1, 1000):
            raise CotahistError('unsupported_documented_quotation_factor')
        close = _digits(line[108:121], 'PREULT')
        trades = _digits(line[147:152], 'TOTNEG')
        quantity = _digits(line[152:170], 'QUATOT')
        _digits(line[10:12], 'CODBDI')
        _digits(line[242:245], 'DISMES')
        if trade_date in daily[ticker]:
            raise CotahistError('duplicate_selected_spot_date_no_last_row_wins_' + ticker + '_' + trade_date.isoformat())
        selected_count += 1
        if selected_count > limits.max_selected_records:
            raise CotahistError('selected_record_memory_budget_exceeded')
        daily[ticker][trade_date] = RawPrice(
            identity, trade_date, close, factor, trades, quantity, '010',
            line[52:56].decode('ascii'), line[230:242].decode('ascii'),
            line[39:49].decode('latin1'), line[27:39].decode('latin1'),
            line[10:12].decode('ascii'), line[242:245].decode('ascii'),
            source_index, line_number, hashlib.sha256(line).hexdigest())
        counts['selected_raw_records'] += 1
        if not daily[ticker][trade_date].usable:
            counts['selected_non_positive_or_non_traded_records'] += 1
    if header is None or trailer is None or line_number < 3:
        raise CotahistError('header_detail_and_trailer_required')
    return year, generation, header, counts, line_number, total_bytes, digest.hexdigest(), selected_count


def read_local_files(paths, *, selected_tickers, identities, first_month=date(2021, 9, 1),
                     cutoff=date(2026, 9, 30), limits=Limits()):
    """Read only named local files; return typed raw evidence after ALL checks pass.

    Structural errors, duplicate selected dates, foreign currency and unexpected
    selected ISINs fail the whole call. No evidence is yielded before EOF/CRC and
    trailer validation. Selection may be a subset of the 262-stock scope for a
    targeted replay; the CLI always loads the complete hash-checked scope.
    """
    months = _months(first_month, cutoff)
    selected = tuple(sorted(selected_tickers))
    if (not 1 <= len(selected) <= 262 or len(set(selected)) != len(selected)
            or any(not isinstance(s, str) or not re.fullmatch(r'[A-Z0-9]{1,12}', s) for s in selected)):
        raise CotahistError('explicit_unique_selection_of_at_most_262_required')
    identity_rows = tuple(identities)
    if any(not isinstance(i, InstrumentIdentity) for i in identity_rows):
        raise CotahistError('typed_external_identities_required')
    identity_map = {i.ticker: i for i in identity_rows}
    if (len(identity_map) != len(identity_rows) or set(identity_map) - set(selected)
            or len({i.isin for i in identity_rows}) != len(identity_rows)):
        raise CotahistError('duplicate_or_out_of_scope_external_identity_no_alias_stitching')
    paths = tuple(paths)
    if not 1 <= len(paths) <= limits.max_files:
        raise CotahistError('one_through_six_explicit_local_files_required')
    local_paths = tuple(_local_path(p) for p in paths)
    if len(set(local_paths)) != len(local_paths):
        raise CotahistError('duplicate_local_input_path')
    daily = {ticker: {} for ticker in identity_map}
    receipts = []
    years = set()
    total_bytes = 0
    selected_count = 0
    for index, path in enumerate(local_paths):
        if path.suffix.lower() not in ('.zip', '.txt'):
            raise CotahistError('only_local_ZIP_or_TXT_supported')
        with path.open('rb') as stream:
            before = _fingerprint(stream)
            if not 0 < before[2] <= limits.max_input_bytes:
                raise CotahistError('input_file_byte_budget_exceeded')
            digest = _hash(stream, before[2])
            member = None
            crc = None
            try:
                if path.suffix.lower() == '.zip':
                    if before[2] > limits.max_archive_bytes:
                        raise CotahistError('archive_byte_budget_exceeded')
                    _zip_preflight(stream, before[2], limits)
                    with zipfile.ZipFile(stream) as archive:
                        info = _zip_member(archive, limits)
                        member, crc = info.filename, format(info.CRC, '08x')
                        if info.file_size + total_bytes > limits.max_total_uncompressed_bytes:
                            raise CotahistError('total_uncompressed_byte_budget_exceeded')
                        with archive.open(info, 'r') as text:
                            parsed = _scan_text(text, index, frozenset(selected), identity_map,
                                                first_month, cutoff, limits,
                                                limits.max_total_uncompressed_bytes - total_bytes,
                                                daily, selected_count)
                        if parsed[5] != info.file_size:
                            raise CotahistError('zip_declared_actual_uncompressed_size_mismatch')
                else:
                    parsed = _scan_text(stream, index, frozenset(selected), identity_map,
                                        first_month, cutoff, limits,
                                        limits.max_total_uncompressed_bytes - total_bytes,
                                        daily, selected_count)
            except (zipfile.BadZipFile, EOFError, NotImplementedError, UnicodeError, RuntimeError, zlib.error) as error:
                raise CotahistError('invalid_zip_CRC_encoding_or_stream') from error
            if member is None:
                if parsed[6] != digest:
                    raise CotahistError('text_input_bytes_changed_after_hash')
            else:
                # SEEK_END discards BufferedReader's cached ZIP directory bytes.
                stream.seek(0, os.SEEK_END)
                stream.seek(0)
                if _hash(stream, before[2]) != digest:
                    raise CotahistError('zip_input_bytes_changed_after_hash')
            # Windows path.stat and fstat can report different ctime semantics.
            # Compare ctime only on the same open handle, not across those APIs.
            if _fingerprint(stream) != before or _stat_fingerprint(path.stat())[:4] != before[:4]:
                raise CotahistError('local_input_changed_during_read')
        year, generation, header, counts, records, size, text_hash, selected_count = parsed
        if year in years:
            raise CotahistError('duplicate_annual_source_no_revision_or_favorable_source_selection')
        for filename in (path.name, member or ''):
            named_year = re.search(r'COTAHIST[_.]A?(202[1-6])(?:\.|$)', PurePosixPath(filename).name, re.I)
            if named_year and int(named_year[1]) != year:
                raise CotahistError('annual_filename_header_year_mismatch')
        years.add(year)
        total_bytes += size
        receipts.append(SourceReceipt(str(path), digest, before[2], member, text_hash, crc,
                                      year, generation, header[2:15].decode('ascii'),
                                      header[15:23].decode('ascii'), counts['all_detail_records'],
                                      records, size, datetime.now(timezone.utc).isoformat(),
                                      tuple(sorted(counts.items()))))
    histories = []
    for ticker, identity in sorted(identity_map.items()):
        quotes = tuple(daily[ticker][d] for d in sorted(daily[ticker]))
        latest = {}
        for quote in quotes:
            if quote.usable:
                latest[_month_end(quote.trade_date.year, quote.trade_date.month)] = quote
        histories.append(InstrumentHistory(identity, quotes,
                         tuple(MonthlyPrice(m, latest[m]) for m in months if m in latest),
                         tuple(m for m in months if m not in latest)))
    return LocalPriceEvidence(selected, tuple(sorted(set(selected) - set(identity_map))),
                              first_month, cutoff, tuple(receipts), tuple(histories))


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CotahistError('duplicate_JSON_key')
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, nargs='+', required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--identities', type=Path, required=True)
    parser.add_argument('--first-month', type=date.fromisoformat, default=date(2021, 9, 1))
    parser.add_argument('--cutoff', type=date.fromisoformat, default=date(2026, 9, 30))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--include-daily', action='store_true')
    args = parser.parse_args(argv)
    from .dividend_scope import load_scope, scope_evidence

    scope = load_scope(args.data_dir)
    if scope is None or scope['selected_count'] != 262:
        raise CotahistError('hash_checked_262_stock_selection_required_no_fallback')
    selection_receipts = scope_evidence(args.data_dir)
    if not any(r.get('kind') == 'stock_selection' and r.get('id') == scope['id']
               for r in selection_receipts):
        raise CotahistError('selection_changed_before_receipts_were_bound')
    identity_path = _local_path(args.identities)
    if identity_path.stat().st_size > 2_000_000:
        raise CotahistError('identity_manifest_size_exceeded')
    content = identity_path.read_bytes()
    manifest = json.loads(content, object_pairs_hook=_unique_json,
                          parse_constant=lambda _: (_ for _ in ()).throw(CotahistError('non_finite_JSON')))
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or not isinstance(manifest.get('identities'), list):
        raise CotahistError('external_identity_manifest_required')
    identities = tuple(InstrumentIdentity(**row) for row in manifest['identities'])
    result = read_local_files(args.input, selected_tickers=scope['selected_tickers'], identities=identities,
                              first_month=args.first_month, cutoff=args.cutoff)
    report = result.to_dict(args.include_daily)
    report['selection_binding'] = 'hash_checked_current_user_scope_not_point_in_time_backtest'
    report['selection_scope_id'] = scope['id']
    report['selection_evidence'] = selection_receipts
    report['external_identity_manifest'] = {'local_path': str(identity_path),
                                            'sha256': hashlib.sha256(content).hexdigest(),
                                            'independently_verified_by_this_parser': False}
    # Exclusive creation: never replace an existing proof, source or pointer.
    with args.output.open('x', encoding='utf-8', newline='\n') as output:
        for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False, indent=2).iterencode(report):
            output.write(chunk)
        output.write('\n')
    print(json.dumps({'output': str(args.output.resolve()), 'sources': len(result.sources),
                      'selected_tickers': len(result.selected_tickers),
                      'complete_61_month_histories': sum(h.complete_61_months for h in result.histories),
                      'unresolved_identities': len(result.unresolved_identity_tickers),
                      'model_admission': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
