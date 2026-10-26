import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def validate_features(values):
    if len(values) != 7:
        raise ValueError("Financial features must contain seven values.")
    for index, value in enumerate(values):
        if value is None:
            if index != 3 or values[6] != 2:
                raise ValueError("Only financial-sector FCF may be explicitly null.")
        elif not math.isfinite(value):
            raise ValueError("Financial features must be finite or explicitly null financial-sector FCF.")
    return values


class Investor(StrictModel):
    risk_level: int = Field(ge=0, le=2)
    knowledge: Literal["Inicial", "Intermediário", "Avançado"]
    withdrawal_years: int = Field(ge=0, le=6)
    high_liquidity: bool


class Plan(StrictModel):
    income_mode: Literal["total_return", "distributions_only"] = "total_return"
    age: int = Field(ge=18, le=100)
    retirement_age: int = Field(ge=19, le=105)
    end_age: int = Field(ge=20, le=120)
    initial_capital: float = Field(ge=0, le=100_000_000)
    monthly_contribution: float = Field(ge=0, le=1_000_000)
    target_income: float = Field(gt=0, le=1_000_000)
    annual_cost_percent: float = Field(ge=0, le=5)
    seed: int = Field(ge=0, le=2_000_000_000)
    simulations: Literal[500, 1000, 2000] = 1000
    block_months: Literal[3, 6, 12] = 6

    @model_validator(mode="after")
    def check_ages(self):
        if not self.age < self.retirement_age < self.end_age:
            raise ValueError("As idades devem seguir: atual < aposentadoria < final do cenário.")
        return self


class RecommendationRequest(StrictModel):
    investor: Investor
    plan: Plan
    data_mode: Literal["demo", "real"]
    forecast_policy: Literal["validation", "xgboost_experimental"] = "validation"


class CurrentQuote(StrictModel):
    price: float | None = Field(default=None, gt=0)
    as_of: str
    source_url: str
    purchase_available: bool
    basis: str
    snapshot_at: str | None = None
    retrieved_at: str | None = None
    last_trade_at: str | None = None
    last_trade_source_url: str | None = None
    market_status: str = 'unknown'
    minimum_fraction: float | None = Field(default=.01, gt=0)
    minimum_investment: float | None = Field(default=None, ge=0)


class Asset(StrictModel):
    ticker: str = Field(min_length=1, max_length=30)
    issuer_id: str | None = Field(default=None, strict=True, min_length=1, max_length=100, pattern=r"^\S+$")
    name: str = Field(min_length=1, max_length=100)
    asset_class: Literal["liquidez", "renda_fixa", "acoes"]
    risk_level: int = Field(ge=0, le=2)
    liquidity_days: int = Field(ge=0, le=3650)
    minimum_years: int = Field(ge=0, le=30)
    complex: bool = False
    price: float = Field(gt=0)
    annual_cash_yield: float | None = Field(default=None, ge=0, le=1)
    features: list[float | None] | None = None
    financial_method: dict | None = None
    current_quote: CurrentQuote | None = None
    as_of: str
    source: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def check_features(self):
        if self.features is not None:
            validate_features(self.features)
        return self
