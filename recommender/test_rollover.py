import numpy as np
import pytest

from .contracts import Asset, Plan
from .rollover import apply_rollover, rollover_schedule, worst_volatility
from .simulation import simulate


def assets():
    return [Asset(ticker=ticker, name=ticker, asset_class=kind, risk_level=risk,
                  liquidity_days=1, minimum_years=years, price=100, annual_cash_yield=flow,
                  as_of="2026-09-30", source="Synthetic test fixture")
            for ticker, kind, risk, years, flow in [("SELIC", "liquidez", 0, 0, 0),
                                                   ("PRE2027", "renda_fixa", 1, 2, .1),
                                                   ("PRE2029", "renda_fixa", 1, 2, .1)]]


def bonds():
    return [{"ticker": ticker, "kind": kind, "maturity": maturity}
            for ticker, kind, maturity in [("SELIC", "Tesouro Selic", "2027-03-01"),
                                           ("PRE2027", "Tesouro Prefixado com Juros Semestrais", "2027-01-01"),
                                           ("PRE2029", "Tesouro Prefixado com Juros Semestrais", "2029-01-01")]]


def test_known_successor_then_explicit_hypothetical_class_proxy():
    mapping, events, states = rollover_schedule(assets(), bonds(), "2026-09-30", 48)
    np.testing.assert_array_equal(mapping[3, 1], [0, 1, 0])
    np.testing.assert_array_equal(mapping[4, 1], [0, 0, 1])
    np.testing.assert_array_equal(mapping[28, 1], [0, .5, .5])
    assert any(e["fromTicker"] == "PRE2027" and e["toTicker"] == "PRE2029" for e in events)
    assert any(e["basis"] == "historical_same_class_proxy" and e["toTicker"] is None for e in events)
    assert all(e["principalCountedAsIncome"] is False for e in events)
    np.testing.assert_allclose(mapping.sum(axis=2), 1)
    assert len(states) > 1


def test_rollover_keeps_cash_distinct_from_principal_and_does_not_create_income():
    mapping, _, _ = rollover_schedule(assets(), bonds(), "2026-09-30", 36)
    paths, cash = apply_rollover(np.zeros((2, 36, 3)), np.zeros((2, 36, 3)), mapping)
    plan = Plan(income_mode="distributions_only", age=30, retirement_age=31, end_age=33,
                initial_capital=100000, monthly_contribution=0, target_income=1000,
                annual_cost_percent=0, seed=42, simulations=500)
    result = simulate([0, 1, 0], paths, plan, detailed=True, cash_paths=cash)
    assert result["monthlyIncomeCapacity"] == [0, 0, 0]
    assert result["finalWealth"] == [100000, 100000, 100000]
    assert result["successFrequency"] == 0


def test_successor_returns_and_paid_cash_are_mapped_together():
    mapping, _, _ = rollover_schedule(assets(), bonds(), "2026-09-30", 36)
    paths = np.broadcast_to([.001, .002, .003], (2, 36, 3)).copy()
    cash = np.broadcast_to([0, .001, .002], (2, 36, 3)).copy()
    rolled, payments = apply_rollover(paths, cash, mapping)
    assert rolled[0, 4, 1] == .003
    assert payments[0, 4, 1] == .002
    assert np.all(payments <= 1 + rolled)


def test_rollover_risk_cannot_be_hidden_by_initial_short_duration():
    _, _, states = rollover_schedule(assets(), bonds(), "2026-09-30", 36)
    covariance = np.diag([.01**2, .03**2, .20**2])
    assert worst_volatility([0, 1, 0], covariance, states)[0] == pytest.approx(.20)


def test_incompatible_class_risk_cannot_be_selected_as_successor():
    data = assets()
    data[2] = data[2].model_copy(update={"risk_level": 2})
    mapping, _, _ = rollover_schedule(data, bonds(), "2026-09-30", 36)
    np.testing.assert_array_equal(mapping[:, 1], np.broadcast_to([0, 1, 0], (36, 3)))


def test_negative_or_leveraged_rollover_mapping_is_rejected():
    mapping = np.eye(3)[None]
    mapping[0, 0] *= 2
    with pytest.raises(ValueError, match="conservar"):
        apply_rollover(np.zeros((1, 1, 3)), np.zeros((1, 1, 3)), mapping)


def test_cannot_purchase_a_successor_maturing_on_the_switch_date():
    records = bonds()
    records[1]["maturity"] = "2027-01-31"
    records[2]["maturity"] = "2027-02-01"
    _, events, _ = rollover_schedule(assets(), records, "2026-09-30", 12)
    first = next(event for event in events if event["referenceTicker"] == "PRE2027")
    assert first["calendarMonth"] == "2027-02"
    assert first["toTicker"] is None
    assert first["basis"] == "historical_same_class_proxy"


@pytest.mark.parametrize("records", [None, [None], [{"ticker": None}],
                                     [bonds()[0], bonds()[0]],
                                     [bonds()[0] | {"maturity": "invalid"}],
                                     [bonds()[0] | {"maturity": "2020-01-01"}],
                                     [bonds()[0] | {"kind": []}]])
def test_invalid_or_expired_maturity_metadata_is_rejected(records):
    with pytest.raises(ValueError):
        rollover_schedule(assets(), records, "2026-09-30", 36)
