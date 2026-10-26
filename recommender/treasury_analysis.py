"""Actual current-offer cash scenarios alongside, not disguised as, optimization.

This boundary evaluates every current contract for the investor. It deliberately
does not claim the original lot's payouts include future purchases/reinvestment.
The portfolio optimizer must consume a stateful reinvestment ledger before its
historical Treasury positions can be replaced by these current instruments.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from .datasets import dataset_revision
from .macro_factors import factor_scenarios, load_macro_factors
from .treasury_cash_paths import initial_lot_cash_paths
from .treasury_market import read_current_instruments
from .universe import profile_reasons

POLICY = 'current_treasury_contracts_dated_coupons_drifting_risk_per_unit_forecast_v3'


def current_data_revision(dataset, folder=None):
    # dataset_revision also binds stock selection when Treasury sources are absent.
    base = dataset_revision(dataset)
    if not isinstance(getattr(dataset, 'metadata', None), dict) or not dataset.metadata.get('stock_universe_preparation'):
        return base
    from .market_inventory import market_root

    root = market_root(folder)
    source = root / 'raw/current/official-investir.json'
    macro = root / 'raw/primary-research/macro/current.json'
    if not source.is_file() or not macro.is_file():
        return base
    if source.is_symlink() or macro.is_symlink() or source.stat().st_size > 4_000_000 or macro.stat().st_size > 10_000:
        raise ValueError('unsafe_current_treasury_revision_sources')
    yields = root / 'raw/tesouro-precos.csv'
    if yields.is_symlink() or yields.is_file() and yields.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('unsafe_treasury_yield_revision_source')
    revision = json.dumps([base, POLICY, hashlib.sha256(source.read_bytes()).hexdigest(),
        hashlib.sha256(macro.read_bytes()).hexdigest(),
        hashlib.sha256(yields.read_bytes()).hexdigest() if yields.is_file() else None], ensure_ascii=True)
    return hashlib.sha256(revision.encode()).hexdigest()


def analyze_current_treasury(request, dataset, folder=None):
    if not isinstance(getattr(dataset, 'metadata', None), dict) or not dataset.metadata.get('stock_universe_preparation'):
        return None
    from .market_inventory import market_root

    root = market_root(folder)
    if not (root / 'raw/current/official-investir.json').is_file():
        return None
    instruments, source = read_current_instruments(root)
    history, macro_digest = load_macro_factors(root)
    plan = request.plan
    months = 12 * (plan.end_age - plan.age)
    factors = factor_scenarios(history, months, plan.simulations, plan.block_months,
        (plan.seed + 131071) % 2_000_000_001)
    retirement = 12 * (plan.retirement_age - plan.age)
    rows = []
    quantiles = lambda values: np.percentile(values, [10, 50, 90]).tolist()
    for instrument in instruments:
        cash = initial_lot_cash_paths(instrument, factors)
        income = cash['economic_income_real']
        reasons = profile_reasons(instrument.asset, request.investor)
        rows.append({'ticker': instrument.asset.ticker, 'name': instrument.asset.name,
            'kind': instrument.offer['kind'], 'maturity': instrument.offer['maturity'],
            'isin': instrument.offer.get('isin'), 'quoteDate': instrument.offer['as_of'],
            'price': instrument.asset.price, 'rateText': instrument.offer.get('buy_rate_text'),
            'sourceUrl': instrument.offer['source_url'],
            'profileCompatible': not reasons, 'profileReasons': reasons,
            'riskLevel': instrument.asset.risk_level, 'minimumYears': instrument.asset.minimum_years,
            'liquidityDays': instrument.asset.liquidity_days, 'modifiedDurationYears': instrument.duration_years,
            'cashScenarioCalculated': True, 'includedInPortfolioOptimization': False,
            'quantityBasis': 'one_current_quote_unit_not_a_portfolio_allocation',
            'income12MonthsReal': quantiles(income[:, :12].sum(axis=1)),
            'incomeFirstRetirementYearOriginalLotReal': quantiles(income[:, retirement:retirement + 12].sum(axis=1)),
            'incomeHorizonOriginalLotReal': quantiles(income.sum(axis=1)),
            'originalCostReturned12MonthsReal': float(cash['acquisition_cost_released_real'][:12].sum()),
            'originalCostReturnedHorizonReal': float(cash['acquisition_cost_released_real'].sum()),
            'remainingOriginalCostReal': cash['remaining_acquisition_cost'],
            'nominalAnchorBasis': cash['index_anchor_basis'],
            'principalCountedAsIncome': False, 'futurePaymentsObserved': False,
            'postMaturityIncomeGenerated': False, 'reinvestmentIncluded': False,
            'institutionRestriction': instrument.pricing.get('institution_restriction'),
            'paymentDates': cash['payment_calendar'],
            'contractSources': (instrument.spec['provenance']['methodology_sources'] if instrument.spec else
                               instrument.pricing['methodology_sources'])})
    return {'policy': POLICY, 'evaluatedContracts': len(rows), 'cashScenariosCalculated': len(rows),
        'compatibleContracts': sum(row['profileCompatible'] for row in rows),
        'optimizedContracts': 0, 'currentQuotesSource': source, 'macroSources': history['sources'],
        'macroReference': history['reference_date'], 'macroFactorSha256': macro_digest,
        'scenarios': plan.simulations, 'quantiles': [10, 50, 90], 'rows': rows,
        'indexAnchorsAreObservedOfficialVna': False, 'historicalPriceSeriesCreated': False,
        'cashSettlementConfirmed': False,
        'scope': 'original_current_quote_lot_only_not_full_reinvestment_portfolio',
        'taxCostBasis': 'gross_before_individual_taxes_and_costs',
        'optimizationIntegrationStatus': 'pending_stateful_same_class_reinvestment_ledger',
        'dataRevision': current_data_revision(dataset, root)}
