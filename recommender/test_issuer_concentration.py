"""Explicit issuer identity caps share classes without inferring ticker prefixes."""

from types import SimpleNamespace

import numpy as np
from pydantic import ValidationError
import pytest

from . import engine
from .contracts import Asset, Investor, Plan, RecommendationRequest
from .datasets import Dataset
from .portfolios import asset_priority, candidate_seeds, feasible_unique


def issuer_assets():
    common = dict(price=20, minimum_years=0, liquidity_days=1, as_of="2026-09-30",
                  source="Synthetic issuer-concentration fixture; not market data.")
    # Sharing an ID with a non-stock must not cap the liquidity anchor.
    assets = [Asset(ticker="CASH", name="Synthetic liquidity", asset_class="liquidez",
                    risk_level=0, annual_cash_yield=0, issuer_id="CVM:TEST-TAEE", **common)]
    for ticker in ("TAEE3", "TAEE4", "OTHER3"):
        same_issuer = ticker != "OTHER3"
        assets.append(Asset(ticker=ticker, name=f"Synthetic {ticker}", asset_class="acoes",
                            risk_level=1, issuer_id="CVM:TEST-TAEE" if same_issuer else "CVM:TEST-OTHER",
                            features=[2 if same_issuer else .2, .8, 4, 3, .5, .3, 1], **common))
    return assets


@pytest.mark.parametrize("value", ["", " ", " CVM:123", "CVM:123 ", "CVM: 123", "x" * 101, 123, [], True])
def test_issuer_id_rejects_blank_non_string_and_unbounded_identifiers(value):
    with pytest.raises(ValidationError):
        Asset.model_validate(issuer_assets()[1].model_dump() | {"issuer_id": value})


def test_issuer_id_is_optional_and_preserves_explicit_identity():
    record = issuer_assets()[1].model_dump()
    record.pop("issuer_id")
    assert Asset.model_validate(record).issuer_id is None
    assert Asset.model_validate(record | {"issuer_id": None}).issuer_id is None
    assert Asset.model_validate(record | {"issuer_id": "CVM:123"}).issuer_id == "CVM:123"


def test_taee_share_classes_share_one_cap_in_bounds_repair_and_candidate_seeds():
    assets = issuer_assets()
    upper, matrix, bounds, feasible, _, _ = engine.bounds_for(assets, 2)
    np.testing.assert_array_equal(matrix[-1], [0, 1, 1, 0])
    assert matrix.shape == (3, 4) and bounds[-1] == .25
    assert feasible[0] >= .1
    overconcentrated = np.array([.5, .25, .25, 0])
    assert np.any(matrix @ overconcentrated > bounds)
    repaired = engine.project_weights(overconcentrated, upper, matrix, bounds, feasible)
    assert repaired[1:3].sum() <= .25 + 1e-7
    assert repaired.sum() == pytest.approx(1)
    assert repaired[0] > .25  # The non-stock with the same ID remains unconstrained by issuer.

    volatility_of = lambda weights: np.linalg.norm(np.atleast_2d(weights), axis=1) * .01
    dates = [f"2025-{month:02d}-01" for month in range(1, 13)] * 2
    history = np.zeros((24, 4))
    annual_cash = np.array([0, .1, .1, .01])
    ranking = asset_priority(assets, history, None, dates, annual_cash)
    seeds = candidate_seeds(assets, ranking, annual_cash, upper, matrix, bounds, feasible,
                            volatility_of, .28, engine.project_weights)
    assert len(seeds) > 1
    assert np.all(seeds[:, 1:3].sum(axis=1) <= .25 + 1e-7)
    assert seeds[:, 1:3].sum(axis=1).max() == pytest.approx(.25)
    accepted = feasible_unique([overconcentrated, repaired], upper, matrix, bounds, volatility_of, .28)
    np.testing.assert_allclose(accepted, [repaired])


@pytest.mark.parametrize("identities", [(None, None), ("CVM:ONE", "CVM:TWO"), ("CVM:ONE", None)])
def test_missing_or_distinct_ids_do_not_infer_shared_issuer_from_taee_prefix(identities):
    assets = issuer_assets()
    assets[1] = assets[1].model_copy(update={"issuer_id": identities[0]})
    assets[2] = assets[2].model_copy(update={"issuer_id": identities[1]})
    _, matrix, bounds, _, _, _ = engine.bounds_for(assets, 2)
    assert matrix.shape == (2, 4)
    assert np.all(matrix @ [.5, .25, .25, 0] <= bounds)


def test_nsga_repair_and_every_returned_portfolio_respect_combined_issuer_cap(monkeypatch):
    assets = issuer_assets()
    dates = [f"2025-{month:02d}-01" for month in range(1, 13)]
    history = np.tile([.002, .004, .004, .003], (12, 1))
    dataset = Dataset(assets, history, np.zeros(12), dates, [], "demo", assets[0].source)
    model = SimpleNamespace(predict=lambda features: np.asarray(features)[:, 0])
    monkeypatch.setattr(engine, "get_dataset", lambda _: dataset)
    monkeypatch.setattr(engine, "get_forecast", lambda *args, **kwargs: (
        model, {"selectedMethod": "xgboost", "xgboostMae": .1, "baselineMae": .2}))
    original_project, original_minimize = engine.project_weights, engine.minimize
    repairs, populations = [], []

    def project(*args):
        weights = original_project(*args)
        assert weights[1:3].sum() <= .25 + 1e-7
        repairs.append(weights)
        return weights

    def minimize(*args, **kwargs):
        result = original_minimize(*args, **kwargs)
        population = result.pop.get("X")
        assert np.all(population[:, 1:3].sum(axis=1) <= .25 + 1e-7)
        populations.append(population)
        return result

    monkeypatch.setattr(engine, "project_weights", project)
    monkeypatch.setattr(engine, "minimize", minimize)
    request = RecommendationRequest(
        investor=Investor(risk_level=2, knowledge="Inicial", withdrawal_years=6, high_liquidity=False),
        plan=Plan(age=30, retirement_age=31, end_age=32, initial_capital=100000,
                  monthly_contribution=100, target_income=100, annual_cost_percent=0,
                  seed=42, simulations=500), data_mode="demo")
    result = engine.compute(request, generations=3, population=8, search_count=4)
    assert repairs and populations
    assert result["parameters"]["issuerMaximum"] == .25
    for portfolio in [result, *result["portfolios"]]:
        allocation = portfolio["allocation"]
        assert sum(row["weight"] for row in allocation if row["ticker"] in {"TAEE3", "TAEE4"}) <= .25 + 1e-7
        assert sum(row["weight"] for row in allocation) == pytest.approx(1)
        assert sum(row["weight"] for row in allocation if row["assetClass"] == "liquidez") >= .1 - 1e-7
        assert portfolio["portfolio"]["annualVolatility"] <= .28 + 1e-7
    for alternative in result["alternatives"]:
        assert sum(row["percent"] for row in alternative["weights"] if row["ticker"] in {"TAEE3", "TAEE4"}) <= 25 + 1e-5
