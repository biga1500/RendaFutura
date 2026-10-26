"""Observed BCB monthly IPCA/Selic factors, separate from projected bond paths."""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.request

import numpy as np

from .collect_market_data import ROOT, month_end, write_json
from .market_inventory import market_root
from .simulation import block_indices

SERIES = {
    'inflation': {'code': 433, 'unit': 'percent_monthly',
        'documentation': 'https://dadosabertos.bcb.gov.br/dataset/433-indice-nacional-de-precos-ao-consumidor-amplo-ipca'},
    'selic': {'code': 4390, 'unit': 'percent_monthly_accumulated',
        'documentation': 'https://dadosabertos.bcb.gov.br/dataset/4390-taxa-de-juros---selic-acumulada-no-mes'},
}


def parse_monthly(content, cutoff, *, nonnegative=False):
    if not isinstance(content, bytes) or len(content) > 4_000_000:
        raise ValueError('macro_source_size_limit')
    rows = json.loads(content)
    if not isinstance(rows, list) or not rows or len(rows) > 2000:
        raise ValueError('macro_source_schema_changed')
    output = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('data'), str) or not re.fullmatch(r'\d{2}/\d{2}/\d{4}', row['data']):
            raise ValueError('invalid_macro_source_date')
        day = datetime.strptime(row['data'], '%d/%m/%Y').date()
        end = month_end(day.year, day.month)
        if end > cutoff:
            continue
        text = row.get('valor')
        if not isinstance(text, str) or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
            raise ValueError('invalid_macro_source_value')
        value = float(Decimal(text) / 100)
        if not math.isfinite(value) or value <= -1 or nonnegative and value < 0:
            raise ValueError('invalid_macro_source_value')
        key = end.isoformat()
        if key in output:
            raise ValueError('duplicate_macro_source_month')
        output[key] = value
    return output


def align_factors(inflation, selic, start, cutoff):
    if start > cutoff or cutoff != month_end(cutoff.year, cutoff.month):
        raise ValueError('macro_reference_requires_month_end')
    first = start.year * 12 + start.month - 1
    last = cutoff.year * 12 + cutoff.month - 1
    dates = [month_end(number // 12, number % 12 + 1).isoformat() for number in range(first, last + 1)]
    if any(day not in inflation or day not in selic for day in dates):
        raise ValueError('missing_observed_macro_month_no_imputation')
    return {'dates': dates, 'inflation': [inflation[day] for day in dates], 'selic': [selic[day] for day in dates]}


def factor_scenarios(history, months, count, block, seed):
    values = np.array([history['inflation'], history['selic']], dtype=float).T
    if (values.shape != (len(history['dates']), 2) or not np.isfinite(values).all()
            or np.any(values <= -1) or np.any(values[:, 1] < 0)):
        raise ValueError('invalid_observed_macro_history')
    if type(months) is not int or not 1 <= months <= 1224 or type(count) is not int or not 1 <= count <= 2000:
        raise ValueError('macro_scenario_limit')
    draws = block_indices(len(values), months, count, block, seed)
    sampled = values[draws]
    return {'inflation': sampled[:, :, 0], 'selic': sampled[:, :, 1],
            'basis': 'joint_block_bootstrap_observed_monthly_macro_factors',
            'future_values_observed': False, 'bond_price_history_created': False,
            'observed_period_start': history['dates'][0], 'observed_period_end': history['dates'][-1]}


def load_macro_factors(folder):
    folder = market_root(folder)
    pointer = json.loads((folder / 'raw/primary-research/macro/current.json').read_text(encoding='utf-8'))
    relative = pointer.get('file', '')
    if not re.fullmatch(r'raw/primary-research/macro/factors-[0-9a-f]{64}\.json', relative):
        raise ValueError('unsafe_macro_factor_pointer')
    path = folder / relative
    if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.stat().st_size > 100_000:
        raise ValueError('unsafe_macro_factor_cache')
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != pointer.get('sha256'):
        raise ValueError('macro_factor_integrity_mismatch')
    data = json.loads(body)
    cutoff = date.fromisoformat(data['reference_date'])
    sources, parsed = data.get('sources', []), {}
    if len(sources) != 2:
        raise ValueError('macro_factor_sources_missing')
    for source in sources:
        role = source.get('role')
        if role not in SERIES or role in parsed or source.get('series_code') != SERIES[role]['code']:
            raise ValueError('macro_factor_series_identity_mismatch')
        name = source.get('file', '')
        if not re.fullmatch(r'raw/primary-research/macro/' + str(SERIES[role]['code']) + r'-[0-9a-f]{64}\.json', name):
            raise ValueError('unsafe_macro_source_cache')
        raw = folder / name
        if raw.is_symlink() or not raw.resolve().is_relative_to(folder) or raw.stat().st_size > 4_000_000:
            raise ValueError('unsafe_macro_source_cache')
        content = raw.read_bytes()
        expected_prefix = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{SERIES[role]['code']}/dados?formato=json&dataInicial="
        if not source.get('url', '').startswith(expected_prefix) or hashlib.sha256(content).hexdigest() != source.get('sha256'):
            raise ValueError('macro_source_integrity_or_identity_mismatch')
        parsed[role] = parse_monthly(content, cutoff, nonnegative=role == 'selic')
    first = date.fromisoformat(data['dates'][0])
    expected = align_factors(parsed['inflation'], parsed['selic'], first, cutoff)
    if (data['dates'] != expected['dates'] or any(not np.allclose(data[key], expected[key], rtol=1e-12, atol=1e-15)
            for key in ('inflation', 'selic')) or data.get('partial_months_included') is not False):
        raise ValueError('macro_factors_disagree_with_observed_sources')
    return data, pointer['sha256']


def collect(folder, cutoff, *, start=date(2020, 1, 1)):
    folder = market_root(folder)
    if cutoff > date.today():
        raise ValueError('future_macro_reference')
    target = folder / 'raw/primary-research/macro'
    target.mkdir(parents=True, exist_ok=True)
    sources, parsed = [], {}
    now = datetime.now(timezone.utc)
    for key, definition in SERIES.items():
        url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{definition['code']}/dados?formato=json"
               f"&dataInicial={start.strftime('%d/%m/%Y')}&dataFinal={cutoff.strftime('%d/%m/%Y')}")
        request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.url != url:
                raise ValueError('unexpected_macro_source_redirect')
            body = response.read(4_000_001)
        parsed[key] = parse_monthly(body, cutoff, nonnegative=key == 'selic')
        digest = hashlib.sha256(body).hexdigest()
        path = target / (str(definition['code']) + '-' + digest + '.json')
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(body)
        elif path.read_bytes() != body:
            raise ValueError('macro_immutable_cache_conflict')
        sources.append({'role': key, 'series_code': definition['code'], 'url': url,
            'documentation_url': definition['documentation'], 'unit': definition['unit'],
            'file': path.relative_to(folder).as_posix(), 'sha256': digest, 'bytes': len(body),
            'retrieved_at': now.isoformat(), 'provider': 'BCB SGS', 'authority': 'primary'})
    aligned = align_factors(parsed['inflation'], parsed['selic'], start, cutoff)
    result = {'schema_version': 1, 'reference_date': cutoff.isoformat(), 'observed_at': now.isoformat(),
        **aligned, 'sources': sources, 'values_unit': 'fraction_per_month',
        'point_in_time_archive_verified': False, 'revisions_may_exist': True,
        'partial_months_included': False, 'bond_price_history_created': False,
        'active_model_changed': False}
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, allow_nan=False).encode()).hexdigest()
    output = target / ('factors-' + digest + '.json')
    write_json(output, result)
    write_json(target / 'current.json', {'file': output.relative_to(folder).as_posix(),
        'sha256': hashlib.sha256(output.read_bytes()).hexdigest()})
    print(json.dumps({'observed_months': len(result['dates']), 'reference_date': cutoff.isoformat(),
        'sources': [source['series_code'] for source in sources], 'output': str(output)}), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--cutoff', type=date.fromisoformat, required=True)
    args = parser.parse_args()
    collect(args.data_dir, args.cutoff)
