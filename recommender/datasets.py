import csv
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
import hashlib
from pathlib import Path
import json
import os
import re
from urllib.parse import urlparse

import numpy as np

from .contracts import Asset, validate_features

FEATURES = ["previous_cash", "regularity", "profit_per_share", "free_cash_per_share", "payout", "debt_ratio", "sector"]
MAX_REAL_ASSETS = 1000


@dataclass
class Dataset:
    assets: list[Asset]
    returns: np.ndarray
    inflation: np.ndarray
    dates: list[str]
    observations: list[dict]
    mode: str
    source: str
    metadata: dict = field(default_factory=dict)
    cash_returns: np.ndarray | None = None


def dataset_revision(dataset):
    metadata = getattr(dataset, 'metadata', {})
    if not isinstance(metadata, dict):
        return None
    preparation = metadata.get('stock_universe_preparation', {})
    prepared = preparation.get('prepared_at') if isinstance(preparation, dict) else None
    value = prepared or metadata.get('collected_at')
    base = value if isinstance(value, str) and value else None
    selection = metadata.get('stock_selection')
    if selection is None:
        return base
    if not isinstance(selection, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(selection.get('id'))):
        raise ValueError('invalid_stock_selection_revision')
    revision = json.dumps([base, selection['id']], ensure_ascii=True)
    return hashlib.sha256(revision.encode()).hexdigest()


def _scoped_metadata(folder, metadata):
    from .dividend_scope import OUTSIDE_REASON, scope_evidence, scope_metadata

    selection = scope_metadata(folder)
    if selection is None:
        return metadata, None
    allowed = frozenset(selection['selectedTickers'])
    assets = [row for row in metadata['assets'] if row['asset_class'] != 'acoes' or row['ticker'] in allowed]
    ready = {row['ticker'] for row in assets if row['asset_class'] == 'acoes'}
    nonstocks = {row['ticker'] for row in assets if row['asset_class'] != 'acoes'}
    metadata = {**metadata, 'assets': assets, 'stock_selection': selection,
                'sources': [row for row in metadata.get('sources', [])
                            if row.get('kind') not in ('stock_selection', 'stock_selection_source')]
                           + scope_evidence(folder)}
    for key in ('collector_stocks', 'expanded_stocks', 'cash_calendar_date_qualifications'):
        if key in metadata:
            metadata[key] = [row for row in metadata[key]
                             if (row if isinstance(row, str) else row['ticker']) in allowed | nonstocks]
    preparation = metadata.get('stock_universe_preparation')
    if isinstance(preparation, dict):
        stocks = []
        for row in preparation['stocks']:
            selected = row['ticker'] in allowed
            effective = {**row, 'model_ready': row['ticker'] in ready, 'in_user_scope': selected}
            if not selected:
                effective.update(source_model_ready=row['model_ready'], reason_codes=[OUTSIDE_REASON],
                                 preparation_basis=OUTSIDE_REASON, source_review_reason_codes=[])
            stocks.append(effective)
        catalogue = {row['ticker'] for row in stocks}
        selected_count = len(catalogue & allowed)
        metadata['stock_universe_preparation'] = {
            **preparation, 'stocks': stocks, 'evaluated_stocks': len(stocks),
            'ready_stocks': len(ready), 'selected_stocks': selected_count,
            'pending_stocks': selected_count - len(ready),
            'outside_user_scope_stocks': len(stocks) - selected_count,
            'unmatched_selected_tickers': sorted(allowed - catalogue),
            'stock_selection': selection,
        }
        for key in ('added_stocks', 'removed_stocks', 'revalidated_stocks'):
            if key in preparation:
                metadata['stock_universe_preparation'][key] = [ticker for ticker in preparation[key] if ticker in allowed]
    return metadata, allowed


def demo_dataset() -> Dataset:
    rng = np.random.default_rng(20261009)
    dates = [f"{2010 + m // 12:04d}-{m % 12 + 1:02d}-01" for m in range(200)]
    market = rng.normal(0.008, 0.045, len(dates))
    inflation = np.clip(rng.normal(0.004, 0.0018, len(dates)), -0.01, 0.035)
    assets, columns, observations = [], [], []
    specs = [("CAIXA-A", "Liquidez demonstrativa A", "liquidez", 0),
             ("CAIXA-B", "Liquidez demonstrativa B", "liquidez", 0),
             ("FIXA-A", "Renda fixa demonstrativa A", "renda_fixa", 0),
             ("FIXA-B", "Renda fixa demonstrativa B", "renda_fixa", 0)]
    specs += [(f"ACAO-{i+1}", f"Companhia fictícia {i+1}", "acoes", 1 if i < 3 else 2) for i in range(6)]
    for i, (ticker, name, kind, risk) in enumerate(specs):
        is_stock = kind == "acoes"
        price = 20 + i * 5
        if is_stock:
            returns = np.clip(market * (0.7 + i / 20) + rng.normal(0, 0.025, len(dates)), -0.6, 0.6)
        else:
            returns = rng.normal(0.0075 if kind == "liquidez" else 0.009, 0.0005 if kind == "liquidez" else 0.012, len(dates))
        features = None
        if is_stock:
            for t in range(24, 188):
                profit = float(3 + i / 2 + rng.normal(0, 0.8))
                cash = max(0.05, profit * 0.45 + rng.normal(0, 0.15))
                f = [cash, 0.8, profit, profit * 0.9, 0.45, 0.5, float(i % 3)]
                label = max(0, profit * 0.44 + rng.normal(0, 0.25))
                observations.append({"as_of": dates[t], "published_at": dates[t], "label_end": dates[t+12],
                                     "features": f, "target": label, "baseline": cash})
            profit = float(3 + i / 2 + rng.normal(0, 0.8))
            cash = max(0.05, profit * 0.45 + rng.normal(0, 0.15))
            features = [cash, 0.8, profit, profit * 0.9, 0.45, 0.5, float(i % 3)]
        assets.append(Asset(ticker=ticker, name=name, asset_class=kind, risk_level=risk,
                            liquidity_days=1 if kind == "liquidez" else 2 if is_stock else 30,
                            minimum_years=5 if is_stock else 0, price=price,
                            annual_cash_yield=None if is_stock else 0.09,
                            features=features, as_of=dates[-1], source="Gerador sintético RF-DEMO-1; não representa ativos de mercado."))
        columns.append(returns)
    return Dataset(assets, np.column_stack(columns), inflation, dates, observations, "demo",
                   "Dados integralmente fictícios, gerados com semente 20261009.")


def read_csv(path: Path, required: list[str]) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not set(required).issubset(fields):
            raise ValueError(f"Colunas ausentes ou duplicadas em {path.name}.")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"Linha incompleta ou desalinhada em {path.name}.")
    return rows


def iso_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use datas no formato AAAA-MM-DD.")
    return parsed


def real_dataset(folder: Path) -> Dataset:
    folder = Path(folder)
    pointer_path = folder / "current.json"
    if pointer_path.exists():
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if not isinstance(pointer, dict) or pointer.get("schema_version") != 1:
            raise ValueError("Ponteiro de snapshot invalido.")
        if pointer.get("status") == "blocked":
            raise ValueError("Base real bloqueada: " + str(pointer.get("reason", "cobertura incompleta")))
        relative = pointer.get("snapshot")
        if pointer.get("status") not in ("ready", "partial") or not isinstance(relative, str):
            raise ValueError("Ponteiro de snapshot invalido.")
        path = Path(relative)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "snapshots":
            raise ValueError("Diretorio de snapshot invalido.")
        resolved = (folder / path).resolve()
        if not resolved.is_relative_to((folder / "snapshots").resolve()):
            raise ValueError("Diretorio de snapshot fora da base.")
        if not all((resolved / name).is_file() for name in
                   ("assets.json", "returns.csv", "observations.csv", "collection-report.json")):
            raise ValueError("Snapshot apontado esta incompleto.")
        # Resolve once: all subsequent reads stay on this immutable version.
        folder = resolved
    metadata = json.loads((folder / "assets.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("assets"), list):
        raise ValueError("Os metadados devem conter uma lista de ativos.")
    metadata, allowed = _scoped_metadata(folder, metadata)
    assets = [Asset.model_validate(row) for row in metadata["assets"]]
    if not 2 <= len(assets) <= MAX_REAL_ASSETS or len({a.ticker for a in assets}) != len(assets):
        raise ValueError(f"A base precisa de 2 a {MAX_REAL_ASSETS} ativos distintos.")
    if metadata.get("returns_include_distributions") is not True:
        raise ValueError("Declare séries de retorno total, incluindo distribuições.")
    source = metadata.get("source", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Documente a origem da base financeira.")
    rows = read_csv(folder / "returns.csv", ["date", "inflation", *[a.ticker for a in assets]])
    dates = [row["date"] for row in rows]
    if not 60 <= len(rows) <= 1200 or dates != sorted(set(dates)):
        raise ValueError("Forneça 60 a 1200 observações mensais, únicas e ordenadas.")
    parsed_dates = [iso_date(value) for value in dates]
    months = [value.year * 12 + value.month for value in parsed_dates]
    if any(b - a != 1 for a, b in zip(months, months[1:])):
        raise ValueError("As séries mensais devem estar alinhadas, sem lacunas.")
    if parsed_dates[-1] > date.today():
        raise ValueError("A base não pode conter observações futuras.")
    if (date.today() - parsed_dates[-1]).days > 62:
        raise ValueError("Atualize o histórico de retornos (máximo de 62 dias).")
    returns = np.array([[float(row[a.ticker]) for a in assets] for row in rows])
    inflation = np.array([float(row["inflation"]) for row in rows])
    if not np.isfinite(returns).all() or not np.isfinite(inflation).all() or np.any(returns <= -1) or np.any(inflation <= -1):
        raise ValueError("Retornos ou inflação inválidos.")
    for asset in assets:
        as_of = iso_date(asset.as_of)
        if as_of > date.today() or (date.today() - as_of).days > 62:
            raise ValueError("Atualize os dados de referência dos ativos (máximo de 62 dias).")
        if asset.features is not None:
            validate_features(asset.features)
    observations = []
    if (folder / "observations.csv").exists():
        nonstocks = {asset.ticker for asset in assets if asset.asset_class != 'acoes'}
        for row in read_csv(folder / "observations.csv", ["as_of", "published_at", "label_end", *FEATURES, "target"]):
            # Unattributed rows cannot establish membership in a configured scope.
            if allowed is not None and row.get('ticker') not in allowed | nonstocks:
                continue
            as_of, published, end = [iso_date(row[k]) for k in ("as_of", "published_at", "label_end")]
            if isinstance(metadata.get("forecast_target"), dict) and metadata["forecast_target"].get("id") == "rights_by_record_date_v1":
                if (row.get("forecast_target_id") != "rights_by_record_date_v1" or row.get("event_basis") != "record_date"
                        or row.get("event_date_field") != "lastDatePrior"):
                    raise ValueError("Alvo ou base temporal do CSV diverge de rights_by_record_date_v1.")
                if iso_date(row.get("features_available_at", "")) > as_of:
                    raise ValueError("Disponibilidade dos atributos posterior a data de referencia.")
            if isinstance(metadata.get("forecast_target"), dict) and metadata["forecast_target"].get("id") == "cash_payments_12m_v2":
                if (row.get("forecast_target_id") != "cash_payments_12m_v2" or row.get("event_basis") != "payment"
                        or row.get("event_date_field") != "paymentDate"):
                    raise ValueError("O alvo de pagamentos exige paymentDate e base de caixa, nao data-com.")
                if iso_date(row.get("features_available_at", "")) > as_of:
                    raise ValueError("Disponibilidade dos atributos posterior a data de referencia.")
            if not published <= as_of < end <= date.today() or end.year * 12 + end.month - (as_of.year * 12 + as_of.month) != 12:
                raise ValueError("Datas dos exemplos causam vazamento temporal ou horizonte incorreto.")
            # DictWriter serializes an explicit None as an empty CSV cell.
            f = [None if k == "free_cash_per_share" and row[k] == "" else float(row[k]) for k in FEATURES]
            validate_features(f)
            target, baseline = float(row["target"]), float(row["previous_cash"])
            if not np.isfinite([target, baseline]).all() or min(target, baseline) < 0:
                raise ValueError("Valores inválidos nos exemplos de treinamento.")
            observation = {"as_of": row["as_of"], "published_at": row["published_at"], "label_end": row["label_end"],
                           "features": f, "target": target, "baseline": baseline}
            if row.get("ticker"):
                observation["ticker"] = row["ticker"]
            observations.append(observation)
    cash_returns = None
    cash_path = folder / "cashflows.csv"
    if cash_path.is_file():
        for name, key in (("cashflows.csv", "cashflows_sha256"), ("payment-ledger.json", "payment_ledger_sha256")):
            if metadata.get(key) and hashlib.sha256((folder / name).read_bytes()).hexdigest() != metadata[key]:
                raise ValueError("Integridade dos pagamentos divergente: " + name)
        cash_basis = metadata.get("cashflow_basis")
        if cash_basis not in ("verified_paid_cash", "reported_historical_cash_reconciled"):
            raise ValueError("Documente os pagamentos efetivos antes de carregar cashflows.csv.")
        if cash_basis == "reported_historical_cash_reconciled" and (
                metadata.get("data_quality_policy") != "RF-CALENDAR-RECONCILED-1"
                or metadata.get("point_in_time_archive_verified") is not False
                or metadata.get("cash_settlement_verified") is not False):
            raise ValueError("Documente a politica retrospectiva dos pagamentos reportados sem afirmar liquidacao ou arquivo point-in-time.")
        if metadata.get("cashflow_excludes_principal") is not True:
            raise ValueError("Os pagamentos de renda precisam excluir amortizacao ou devolucao do principal.")
        evidence = metadata.get("cashflow_source", {})
        if (not isinstance(evidence, dict) or urlparse(str(evidence.get("url", ""))).scheme != "https"
                or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256", "")))):
            raise ValueError("Informe a fonte HTTPS e o hash SHA-256 dos pagamentos, sem incluir amortizacao do principal.")
        cash_rows = read_csv(cash_path, ["date", *[a.ticker for a in assets]])
        if [row["date"] for row in cash_rows] != dates:
            raise ValueError("Os pagamentos precisam dos mesmos meses dos retornos totais.")
        cash_returns = np.array([[float(row[a.ticker]) for a in assets] for row in cash_rows])
        if (not np.isfinite(cash_returns).all() or np.any(cash_returns < 0)
                or np.any(cash_returns > 1 + returns)):
            raise ValueError("Pagamentos invalidos: informe caixa por valor investido no inicio do mes, sem resgate do principal.")
    return Dataset(assets, returns, inflation, dates, observations, "real", source, metadata, cash_returns)


@lru_cache(maxsize=1)
def cached_demo_dataset() -> Dataset:
    return demo_dataset()


def get_dataset(mode: str) -> Dataset:
    if mode == "demo":
        return cached_demo_dataset()
    if mode != "real":
        raise ValueError("Modo de dados desconhecido.")
    folder = os.environ.get("RF_DATA_DIR")
    if not folder:
        raise ValueError("Base real não configurada. Use explicitamente o modo demonstrativo ou configure RF_DATA_DIR.")
    return real_dataset(Path(folder))
