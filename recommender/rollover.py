"""Monthly Treasury reinvestment scenarios, not contractual redemption pricing."""

from datetime import date

import numpy as np


def rollover_schedule(assets, bonds, reference, months):
    size = len(assets)
    if not isinstance(bonds, list) or any(not isinstance(row, dict) for row in bonds):
        raise ValueError("Metadados de vencimentos precisam ser uma lista de registros.")
    identity = np.eye(size)
    mapping = np.broadcast_to(identity, (months, size, size)).copy()
    tickers = [row.get("ticker") for row in bonds]
    if any(not isinstance(ticker, str) for ticker in tickers) or len(set(tickers)) != len(tickers):
        raise ValueError("Vencimentos sem identificacao ou com titulos duplicados.")
    details = {row["ticker"]: row for row in bonds if row.get("ticker") in {a.ticker for a in assets}}
    if not details:
        return mapping, [], np.array([identity])
    reference = date.fromisoformat(reference)
    for row in details.values():
        try:
            maturity = date.fromisoformat(row["maturity"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Data de vencimento ausente ou invalida.") from error
        if maturity.isoformat() != row["maturity"] or maturity <= reference:
            raise ValueError("O titulo precisa de um vencimento futuro valido na data-base.")
    events = []
    for original, asset in enumerate(assets):
        if asset.ticker not in details:
            continue
        detail = details[asset.ticker]
        peers = [i for i, a in enumerate(assets) if a.ticker in details
                 and details[a.ticker].get("kind") == detail.get("kind")
                 and a.asset_class == asset.asset_class and a.risk_level <= asset.risk_level
                 and a.liquidity_days <= asset.liquidity_days
                 and a.minimum_years <= asset.minimum_years and a.complex == asset.complex]
        if not peers or not isinstance(detail.get("kind"), str) or not detail["kind"].strip():
            raise ValueError("Metadados insuficientes para reinvestir na mesma classe e risco.")
        current, hypothetical = original, False
        for month in range(months):
            number = reference.year * 12 + reference.month + month
            year, month_index = divmod(number, 12)
            calendar = date(year, month_index + 1, 1)
            if not hypothetical:
                maturity = date.fromisoformat(details[assets[current].ticker]["maturity"])
                # Monthly resolution: switch at the first month after maturity.
                if (calendar.year, calendar.month) > (maturity.year, maturity.month):
                    later = [i for i in peers if date.fromisoformat(details[assets[i].ticker]["maturity"]) > calendar]
                    source = assets[current].ticker
                    if later:
                        current = min(later, key=lambda i: details[assets[i].ticker]["maturity"])
                    else:
                        hypothetical = True
                    events.append({"month": month + 1, "calendarMonth": calendar.isoformat()[:7],
                                   "referenceTicker": asset.ticker, "fromTicker": source,
                                   "toTicker": None if hypothetical else assets[current].ticker,
                                   "class": detail["kind"],
                                   "basis": "historical_same_class_proxy" if hypothetical else "later_maturity_in_sample",
                                   "principalCountedAsIncome": False})
            mapping[month, original] = 0
            if hypothetical:
                mapping[month, original, peers] = 1 / len(peers)
            else:
                mapping[month, original, current] = 1
    # Every state is needed when checking historical volatility, including the
    # initial allocation: changing maturity must not bypass the profile limit.
    states = np.unique(np.concatenate([identity[None], mapping]), axis=0)
    return mapping, events, states


def apply_rollover(paths, cash_paths, mapping):
    if paths.shape[1:] != mapping.shape[:2] or mapping.shape[1] != mapping.shape[2]:
        raise ValueError("Mapeamento de reinvestimento incompativel com os cenarios.")
    if not np.isfinite(mapping).all() or np.any(mapping < 0) or not np.allclose(mapping.sum(axis=2), 1):
        raise ValueError("O reinvestimento deve conservar o capital, sem pesos negativos ou alavancagem.")
    rolled = np.einsum("smb,mab->sma", paths, mapping, optimize=True)
    cash = None if cash_paths is None else np.einsum("smb,mab->sma", cash_paths, mapping, optimize=True)
    return rolled, cash


def worst_volatility(weights, covariance, states):
    covariance_states = np.einsum("kab,bc,kdc->kad", states, covariance, states, optimize=True)
    variance = np.einsum("ia,kab,ib->ik", np.atleast_2d(weights), covariance_states,
                         np.atleast_2d(weights), optimize=True)
    return np.sqrt(np.maximum(0, variance).max(axis=1))
