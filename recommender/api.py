from contextlib import asynccontextmanager
from hmac import compare_digest
import os
from threading import BoundedSemaphore

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from .contracts import RecommendationRequest
from .datasets import get_dataset
from .engine import ENGINE_VERSION, compute
from .catalog import summary as catalog_summary
from .data_coverage import coverage_summary
from .treasury_analysis import current_data_revision

slots = BoundedSemaphore(1)


@asynccontextmanager
async def lifespan(app):
    if len(os.environ.get("RECOMMENDER_TOKEN", "")) < 32:
        raise RuntimeError("Configure RECOMMENDER_TOKEN com pelo menos 32 caracteres.")
    yield


app = FastAPI(title="RendaFutura Motor", version=ENGINE_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def authorize(authorization: str = Header(default="")):
    expected = os.environ.get("RECOMMENDER_TOKEN", "")
    if not expected or not compare_digest(authorization, "Bearer " + expected):
        raise HTTPException(status_code=401, detail="Acesso restrito ao servidor RendaFutura.")


@app.middleware("http")
async def limit_body(request: Request, call_next):
    # Browser requests use the authenticated same-origin web proxy, never this
    # loopback service directly. Do not enable permissive CORS here.
    if request.headers.get("origin"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Use a API de mesma origem do RendaFutura."}, status_code=403)
    if request.method == "POST":
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Solicitação muito grande."}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return {"status": "ok", "version": ENGINE_VERSION}


@app.get("/status", dependencies=[Depends(authorize)])
def status():
    configured = False
    message = "Base real não configurada."
    try:
        dataset = get_dataset("real")
        configured = True
        message = dataset.source
    except (ValueError, OSError, KeyError):
        pass
    try:
        catalog = catalog_summary()
    except (ValueError, OSError, KeyError):
        catalog = None
    from .market_inventory import inventory_summary
    from .universe import preparation_summary
    assets = [a.ticker for a in dataset.assets] if configured else []
    contract_keys = []
    coverage = coverage_summary(dataset if configured else None)
    if configured and os.environ.get('RF_CURRENT_CONTRACT_PORTFOLIOS') == '1':
        from .treasury_market import read_current_instruments
        from .market_inventory import market_root
        from .current_portfolio import current_coverage
        try:
            current, _ = read_current_instruments(market_root())
            assets = [a.ticker for a in dataset.assets if a.asset_class == 'acoes'] + [i.asset.ticker for i in current]
            contract_keys = [i.offer['kind'] + '|' + i.offer['maturity'] for i in current]
            coverage = current_coverage(dataset, current)
        except (ValueError, OSError, KeyError) as error:
            configured = False
            message = 'Base de contratos atuais indisponivel: ' + str(error)
    return {"engineVersion": ENGINE_VERSION, "realDataAvailable": configured, "realDataMessage": message,
            "cashIncomeDataAvailable": configured and dataset.cash_returns is not None,
            "cashIncomeMessage": "Historico de pagamentos carregado; sujeito a validacao e as hipoteses do modelo." if configured and dataset.cash_returns is not None else "Historico de pagamentos efetivos nao configurado; direitos por data-com nao substituem caixa recebido.",
            "realDataReference": dataset.dates[-1] if configured else None,
            "realDataRevision": current_data_revision(dataset) if configured else None,
            "realDataAssets": assets if configured else [],
            "currentContractPortfolios": os.environ.get('RF_CURRENT_CONTRACT_PORTFOLIOS') == '1',
            "currentContractKeys": contract_keys,
            "catalog": catalog, "marketInventory": inventory_summary(), "dataCoverage": coverage,
            "universePreparation": preparation_summary(dataset) if configured else None,
            "demoAvailable": True, "financiallyValidated": False}


@app.get("/market", dependencies=[Depends(authorize)])
def market():
    from .market_inventory import load_inventory
    try:
        return load_inventory()
    except (OSError, ValueError, KeyError):
        raise HTTPException(status_code=503, detail="Inventario de fontes ainda nao disponivel.") from None


@app.post("/recommend", dependencies=[Depends(authorize)])
def recommend(input: RecommendationRequest):
    if not slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Motor ocupado. Tente novamente em alguns instantes.")
    try:
        return compute(input)
    except (ValueError, OSError, KeyError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        slots.release()
