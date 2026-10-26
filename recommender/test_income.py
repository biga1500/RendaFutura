from copy import deepcopy
import csv
from datetime import date
import json

import numpy as np
import pytest

from . import engine
from .contracts import RecommendationRequest
from .datasets import demo_dataset, real_dataset
from .simulation import bootstrap_cash_paths, bootstrap_paths, simulate
from .test_engine import investor, plan


def income_plan(**changes):
    return plan(income_mode="distributions_only", **changes)


def test_zero_coupon_appreciation_is_not_retirement_income():
    paths = np.full((3, 24, 1), 0.01)
    settings = income_plan(initial_capital=10000, monthly_contribution=0, target_income=100)
    result = simulate([1], paths, settings, detailed=True, cash_paths=np.zeros_like(paths))
    assert result["successFrequency"] == 0
    assert result["monthlyIncomeCapacity"] == [0, 0, 0]
    assert result["medianDeficit"] == 1200
    assert result["meanUnfundedIncomeFraction"] == 1
    assert result["finalWealth"] == pytest.approx([10000 * 1.01**24] * 3)


def test_distributions_are_not_double_counted_or_replaced_by_principal():
    paths = np.full((3, 24, 1), 0.01)
    settings = income_plan(initial_capital=10000, monthly_contribution=0, target_income=10000)
    result = simulate([1], paths, settings, detailed=True, cash_paths=np.full_like(paths, 0.01))
    retirement = 10000 * 1.01**12
    assert result["retirementWealth"] == pytest.approx([retirement] * 3)
    assert result["finalWealth"] == pytest.approx([retirement] * 3)
    assert result["monthlyIncomeCapacity"] == pytest.approx([retirement * .01] * 3)
    assert result["successFrequency"] == 0
    assert result["incomeCoveragePeriod"] == "annual_average"


def test_identical_returns_with_different_cash_payments_change_income():
    paths = np.full((3, 24, 1), .01)
    settings = income_plan(initial_capital=10000, monthly_contribution=0, target_income=100)
    no_cash = simulate([1], paths, settings, detailed=True, cash_paths=np.zeros_like(paths))
    paid = simulate([1], paths, settings, detailed=True, cash_paths=np.full_like(paths, .01))
    assert paid["successFrequency"] == 1
    assert no_cash["successFrequency"] == 0
    assert paid["retirementWealth"] == no_cash["retirementWealth"]
    assert paid["finalWealth"][1] < no_cash["finalWealth"][1]


def test_income_scales_with_capital_not_the_target_input():
    paths = np.full((3, 24, 1), .01)
    cash = np.full_like(paths, .01)
    def run(capital, target):
        return simulate([1], paths, income_plan(initial_capital=capital, monthly_contribution=0,
                                               target_income=target), detailed=True, cash_paths=cash)
    a, b, c = run(10000, 100), run(20000, 100), run(10000, 100000)
    assert b["monthlyIncomeCapacity"][1] == pytest.approx(2*a["monthlyIncomeCapacity"][1])
    assert a["monthlyIncomeCapacity"] == c["monthlyIncomeCapacity"]
    assert a["successFrequency"] == 1 and c["successFrequency"] == 0


def test_joint_cash_sampling_preserves_returns_inflation_and_payments():
    cash = np.arange(60)[:, None] / 10000
    inflation = np.arange(60) / 1000
    paths = bootstrap_paths(cash, inflation, 24, 5, 6, 42)
    sampled = bootstrap_cash_paths(cash, inflation, 24, 5, 6, 42)
    # (1+return)/(1+inflation)-1 + inflation/(1+inflation) = cash/(1+inflation)
    zero_cash = bootstrap_paths(np.zeros_like(cash), inflation, 24, 5, 6, 42)
    np.testing.assert_allclose(paths-zero_cash, sampled)
    np.testing.assert_array_equal(sampled, bootstrap_cash_paths(cash, inflation, 24, 5, 6, 42))


def test_missing_cash_cannot_fall_back_to_asset_sales():
    with pytest.raises(ValueError, match="pagamentos"):
        simulate([1], np.zeros((2, 24, 1)), income_plan(), detailed=True)


def test_legacy_withdrawal_capacity_is_explicitly_different():
    settings = plan()
    result = simulate([1], np.zeros((3, 24, 1)), settings, detailed=True)
    assert result["monthlyIncomeCapacity"] == [200, 200, 200]
    assert result["successFrequency"] == 1


def real_fixture():
    data = deepcopy(demo_dataset())
    indices = [0, 4]
    data.assets = [data.assets[i] for i in indices]
    data.assets[0] = data.assets[0].model_copy(update={"annual_cash_yield": 0})
    data.returns = data.returns[:, indices]
    data.mode = "real"
    data.metadata["forecast_target"] = {"id": "rights_by_record_date_v1"}
    return data


def test_real_rights_cannot_be_used_as_verified_cash(monkeypatch):
    data = real_fixture()
    monkeypatch.setattr(engine, "get_dataset", lambda _: data)
    def forbidden(*args, **kwargs):
        pytest.fail("Must not invent a cash forecast or execute optimization when cash data are missing")
    monkeypatch.setattr(engine, "get_forecast", forbidden)
    monkeypatch.setattr(engine, "minimize", forbidden)
    request = RecommendationRequest(investor=investor(2), plan=income_plan(), data_mode="real")
    result = engine.compute(request)
    assert result["status"] == "income_data_unavailable"
    assert result["missingCashAssets"] == [data.assets[1].ticker]
    assert "simulation" not in result and "allocation" not in result
    assert result["requiresPrincipalWithdrawal"] is False
    stock = result["assetAnalysis"][1]
    assert stock["indicators"]["priceEarnings"] == pytest.approx(stock["price"] / stock["indicators"]["earningsPerShare"])
    json.dumps(result, allow_nan=False)


def test_known_zero_cash_yields_zero_income_not_a_funded_retirement(monkeypatch):
    data = real_fixture()
    monkeypatch.setattr(engine, "get_dataset", lambda _: data)
    request = RecommendationRequest(investor=investor(0), plan=income_plan(), data_mode="real")
    result = engine.compute(request)
    assert result["status"] == "ready"
    assert result["incomeAnalysis"]["status"] == "no_periodic_payments"
    assert result["incomeAnalysis"]["monthlyCapacity"] == [0, 0, 0]
    assert result["incomeAnalysis"]["usesPrincipal"] is False
    assert result["simulation"]["successFrequency"] == 0
    assert result["coverage"]["status"] == "single_eligible_asset"
    json.dumps(result, allow_nan=False)


def test_complete_cash_fixture_runs_content_forecast_nsga_and_monte_carlo(monkeypatch):
    data = real_fixture()
    data.cash_returns = np.tile([0,.005], (len(data.dates),1))
    monkeypatch.setattr(engine, "get_dataset", lambda _: data)
    settings = income_plan(initial_capital=10000, monthly_contribution=100, target_income=100)
    request = RecommendationRequest(investor=investor(2), plan=settings, data_mode="real")
    result = engine.compute(request,generations=3,population=8,search_count=8)
    assert result["methods"] == ["Content-Based Filtering","XGBoost","NSGA-II","Monte Carlo"]
    assert result["incomeAnalysis"]["usesPrincipal"] is False
    assert result["incomeAnalysis"]["monthlyCapacity"][1] > 0
    assert result["parameters"]["generations"] == 3
    assert result["model"]["appliedMethod"] == result["model"]["selectedMethod"]
    assert result["model"]["usedForIncomeSimulation"] is True
    assert result["model"]["forecastHorizonMonths"] == 12
    assert result["allocation"][1]["cashYield12m"] == pytest.approx(
        result["allocation"][1]["predictedCashPerShare"] / data.assets[1].price)
    json.dumps(result, allow_nan=False)


def test_objective_differentiates_unfunded_plans_before_binary_success():
    paths = np.zeros((2, 24, 1))
    one = simulate([1], paths, plan(initial_capital=0, monthly_contribution=1, target_income=100))
    two = simulate([1], paths, plan(initial_capital=0, monthly_contribution=2, target_income=100))
    assert one["success"][0] == two["success"][0] == 0
    assert two["unfundedIncomeFraction"][0] < one["unfundedIncomeFraction"][0]


@pytest.fixture
def paid_cash_files(tmp_path):
    last = date.today().replace(day=1)
    serial = last.year * 12 + last.month - 1
    dates = []
    for offset in range(59, -1, -1):
        year, month = divmod(serial-offset, 12)
        dates.append(date(year, month+1, 1).isoformat())
    assets = [a.model_dump() | {"as_of": last.isoformat()} for a in demo_dataset().assets[:2]]
    metadata = {"assets": assets, "source": "Synthetic payment fixture, not market data.",
                "returns_include_distributions": True, "cashflow_basis": "verified_paid_cash",
                "cashflow_excludes_principal": True,
                "cashflow_source": {"url": "https://example.invalid/paid-cash-fixture", "sha256": "0"*64}}
    (tmp_path/"assets.json").write_text(json.dumps(metadata), encoding="utf8")
    for filename, fields, rows in [
        ("returns.csv", ["date", "inflation", *[a["ticker"] for a in assets]], [[day, .005, .01, .01] for day in dates]),
        ("cashflows.csv", ["date", *[a["ticker"] for a in assets]], [[day, .002, .003] for day in dates]),
    ]:
        with (tmp_path/filename).open("w",newline="",encoding="utf8") as file:
            writer=csv.writer(file);writer.writerow(fields);writer.writerows(rows)
    return tmp_path, metadata


def test_cash_loader_requires_payment_basis_evidence_and_no_principal(paid_cash_files):
    folder, metadata = paid_cash_files
    data = real_dataset(folder)
    np.testing.assert_allclose(data.cash_returns, np.tile([.002,.003],(60,1)))
    for key, value, message in [("cashflow_basis", "record_date", "pagamentos"),
                                ("cashflow_excludes_principal", False, "principal"),
                                ("cashflow_source", {}, "fonte")]:
        changed = metadata | {key:value}
        (folder/"assets.json").write_text(json.dumps(changed),encoding="utf8")
        with pytest.raises(ValueError,match=message):real_dataset(folder)


def test_reported_cash_loader_requires_explicit_retrospective_qualification(paid_cash_files):
    folder, metadata = paid_cash_files
    metadata.update(cashflow_basis='reported_historical_cash_reconciled',
                    data_quality_policy='RF-CALENDAR-RECONCILED-1',
                    point_in_time_archive_verified=False, cash_settlement_verified=False)
    (folder/'assets.json').write_text(json.dumps(metadata), encoding='utf8')
    assert real_dataset(folder).metadata['cash_settlement_verified'] is False
    for field, value in [('point_in_time_archive_verified', True), ('cash_settlement_verified', True),
                         ('data_quality_policy', 'unreviewed')]:
        (folder/'assets.json').write_text(json.dumps(metadata | {field: value}), encoding='utf8')
        with pytest.raises(ValueError, match='politica retrospectiva'):
            real_dataset(folder)


@pytest.mark.parametrize("value",[-.1,2,float("nan")])
def test_cash_loader_rejects_invalid_distributions(paid_cash_files,value):
    folder,_=paid_cash_files
    path=folder/"cashflows.csv"
    lines=path.read_text().splitlines()
    day=lines[1].split(',')[0]
    lines[1]=f"{day},{value},0.003"
    path.write_text('\n'.join(lines),encoding="utf8")
    with pytest.raises(ValueError,match="Pagamentos invalidos"):real_dataset(folder)
