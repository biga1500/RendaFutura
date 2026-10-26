# RendaFutura

## Motor RendaFutura RF-MOTOR-1.12

The existing TypeScript/React/Cloudflare D1 application now calls a separate
Python service for content-based eligibility, XGBoost cash-flow regression,
NSGA-II allocation and joint block-bootstrap Monte Carlo retirement scenarios.
The engine does not receive account identity, credentials or raw questionnaire
answers. It receives only derived risk/knowledge/liquidity attributes and plan
parameters. Consent is separate; changing/deleting the profile invalidates the
saved plan. Export and account deletion include the saved planning data.

### Current-contract retirement pipeline

The local launcher enables `RF_CURRENT_CONTRACT_PORTFOLIOS=1`. With real data
and distributions-only income, `current_portfolio.py` uses reconciled stocks
and the actual current Treasury offers, not the nine older Treasury reference
series retained for historical regression tests. An unavailable current offer
book fails closed rather than silently selecting the old Treasury pipeline.

Every catalogued stock code is checked independently. Catalogue membership,
readable financial fields, historical model readiness, current indicative quote
availability and investor-profile eligibility are different states. Positive
native security-identity conflicts, subscription instruments and cancelled or
suspended issuer registrations cannot use an alternative parser to bypass a
purchase block. Unknown evidence is not a zero payment or a delisting.

Eligible equities receive XGBoost next-12-month gross cash-per-share forecasts.
The first-year scenario replacement preserves cash per original share when
prices change; it is not a fixed yield on a growing share price. Longer horizons
use synchronized historical blocks and disclose that the one-year model is not
a decades-long profit prediction. NSGA-II searches a bounded, ranked shortlist,
with a current maturity selected for each eligible Treasury family. All current
offers still receive their independent contractual cash analysis.

Treasury scenarios use dated QuantLib cash schedules, observed family yield
factors and BCB inflation/Selic. They do not manufacture historical prices for
new maturities. The portfolio ledger separates original cost principal from
economic interest, reinvests principal in the same family/risk and withdraws only
interest after retirement. Taxes and monthly calendar approximations are stated
limitations. There is no annual cross-asset rebalancing: risk is rechecked from
the drifting portfolio, accounting for contributions and income withdrawals.
Independent Monte Carlo draws reassess finalist portfolios. These are
experimental scenarios, not calibrated guarantees or financial validation.

Optional alternative provider quotes retain separate provider-snapshot,
retrieval and known-last-trade timestamps. A newer provider snapshot does not
prove a newer executed trade and never becomes historical price evidence.

`python -m recommender.collect_inventory --daily-histories` attempts every
catalogued code, including stocks whose current quote is stale. Existing daily
originals are reused by default. Verified results update independent inventory
fields without granting model readiness or changing the active model pointer.
Use `--refresh` only as an intentional source revision; source hash changes
require reconciliation before a new model can be published.

`python -m recommender.prepare_inventory` rebuilds every stock under the same
cash/price/share-scale and annual-account policy. It stages an immutable
candidate; it does not publish by default. Validate all persona fixtures and
review additions/removals, then activate the exact tested candidate with
`--activate-candidate <snapshot-folder>`. The local browser test also checks
lossless saved-result/export storage, account cleanup and responsive screens.

The historical Treasury proxy/rollover descriptions below document the retained
legacy test path; they do not override this current-contract local pipeline.

### Local execution

Create `.recommender-venv` with Python, then install
`recommender/requirements.txt`. Run `scripts/run-recommender-local.ps1`;
`-CheckOnly` verifies runtime detection without starting services. Supply
`-NodePath <absolute-node-executable>` to override the detected Node runtime.
The helper selects free loopback ports and starts
the web app and Uvicorn in hidden processes. A random service token is passed
through inherited environment variables, not command arguments or source.
Non-secret URLs and owned PIDs are recorded in
`.sites-runtime/recommender-local.json`; logs are in the same ignored folder.
Stop only these owned PIDs when the local preview is no longer needed.

After generating/building migrations, apply only pending SQL locally with the
documented D1 command below. `0001_fair_la_nuit.sql` adds `recommendations`.

### Data and limits

No financial production dataset is included. Demonstration mode requires an
explicit user selection and uses fictional assets, prices and synthetic
training examples. Real mode never silently falls back to demonstration.

The operator can supply an independently audited dataset through `RF_DATA_DIR`:

- `assets.json`: `assets` using the strict `Asset` schema in
  `recommender/contracts.py`, `source` and `returns_include_distributions: true`.
- `returns.csv`: ordered contiguous monthly `date,inflation,<ticker>...` columns;
  returns are decimals and must include distributions (60 to 1200 months).
- `observations.csv`: for eligible equities, `as_of,published_at,label_end`, the
  seven ordered `FEATURES` in `recommender/datasets.py`, and `target` (gross
  next-12-month distributions per share). The previous-cash baseline uses the
  same units. Training labels crossing validation/test cutoffs are purged.

Asset prices, financial feature dates, corporate-action adjustments, liquidity
and risk labels must be reviewed by the operator. Dataset validation checks
format, dates, finite values and minimum history; it does not certify financial
quality. Real files and their forecasting model are reloaded for each request;
only the fixed fictional demonstration dataset/model are cached in-process.

The cash forecast learns a scaled correction to the previous 12-month cash
distribution, not an absolute amount pooled across companies. Monetary features
and the residual target use `max(previous_cash, 1)` as their per-row scale;
the inverse transform restores BRL per share. Depth is selected only on the
purged chronological validation set. The report retains the XGBoost test errors
and the previous-cash benchmark separately. The authenticated web planner only
accepts real data and `forecast_policy=xgboost_experimental`: XGBoost is applied
to eligible stocks even if it loses to the benchmark. The benchmark remains in
the evaluation report, not as a selectable forecasting model. The internal
Python API still supports `validation` and synthetic fixtures for controlled
engineering comparisons; the user-facing web API rejects those alternatives.
This policy does not promote the model to financially validated status.
Test labels do not select the method or calibrate scenario uncertainty.
The revised design has already been inspected against this retrospective
dataset and still needs a new prospective evaluation and portfolio backtest.
Public collectors below produce snapshots with recorded sources; missing required data never
becomes synthetic market data. No personal data is used for model training.

The selected annual cash forecast is divided by the current share price and
enters the NSGA-II cash-yield objective. In the first 12 scenario months,
validation residuals perturb that annual forecast, sampled payment weights
distribute it over months, and sampled inflation converts it to real cash.
Shared calibration dates preserve observed company shocks when available.
Empty sampled payment years use equal monthly scenario weights, not a claim
about contractual payment dates. Historical total returns have their cash
component REPLACED, never double-counted. The ex-cash price component stays
unchanged: this is an explicit scenario assumption, not a learned price model.
After month 12, scenarios revert to joint historical resampling. The one-year
XGBoost model is not extrapolated through several decades of retirement.
Validation residuals are a small retrospective uncertainty proxy, not a
guaranteed or independently calibrated confidence interval.

Academic allocation limits are profile-dependent, not CVM-prescribed:
equities at most 0/40/65%, liquidity at least 25/15/10%, individual stocks at most
25%, annual historical real volatility at most 8/16/28%. Infeasibility is reported
without relaxing these limits. The equal-weight reference is omitted if it
violates the volatility limit. Search uses population 32, 24 generations and 96
common scenarios; up to six finalists are reevaluated on independently drawn
500/1000/2000 scenarios. Selection prioritizes full retirement-income coverage,
then lower unfunded-income fraction, lower volatility, and greater estimated cash.

### Inverse planning and reinvestment

The selected portfolio is also evaluated with per-scenario linear capital and
contribution factors. `planning.py` solves for retirement capital and the real
monthly contribution needed to cover the target on at least 80% of the sampled
paths, without changing the selected weights, risk limits or questionnaire.
The 80% threshold is an academic assumption, not a financial guarantee or CVM
rule. Empirical order statistics avoid interpolating unattainable coverage.
Zero-income paths can make the required capital/contribution non-finite; the
API returns null with `no_finite_solution`, never a made-up finite amount.
The displayed historical geometric real return is a reference only: it is not
used as a fixed future compounding rate.

At the first month after a Treasury reference matures, `rollover.py` maps its
simulated capital to the next prepared maturity of the exact same Treasury
kind, asset class and no greater risk, minimum horizon, complexity or liquidity
delay. A title already maturing on the switching date is not a valid successor.
When no known successor exists, an explicitly hypothetical historical basket of
eligible same-kind securities is used; this does not invent an issued title or
future rate. Both return and paid-cash paths use the same convex mapping.
Principal is never added to income. Historical volatility constraints check the
maximum across the initial allocation and all replacement states.

This is monthly market-value scenario reinvestment, not contractual redemption
pricing. It does not split maturity proceeds into accrued interest/principal,
calculate the final contractual coupon, or model lots and personal taxes.
Consequently, zero-coupon references still have no periodic cash distributions;
that does NOT establish that their maturity interest is zero. These limits are
explicit in the interface and remain matters for financial-method review.

The web app uses real purchasing-power targets and contributions, dividend and
interest income only, annual rebalancing and an effective annual cost assumption;
it never sells principal to fill income gaps. The legacy total-return API mode
allows principal withdrawals and is not used by the web app. Personal taxes and
individual longevity are not modeled. Total returns already contain historical
distributions; scenario forecasts replace that cash component. Monte Carlo coverage
frequency and Wilson sampling intervals are conditional outputs, not true
retirement probabilities. There is no financial or expert validation yet.

### Verification and hosting

Run `python -m pytest recommender -q` inside the venv, `node
node_modules/typescript/bin/tsc --noEmit`, and `node scripts/run-framework.mjs
build`. Focused web tests: `node --test scripts/test-initial-portfolio.mjs
scripts/test-retirement-goal.mjs scripts/test-request-body.mjs`.

`node scripts/test-live-portfolio-browser.mjs <local-url> <personas.json>`
checks conservative, moderate and aggressive synthetic profiles with real market
data, field alignment at four widths, mandatory XGBoost, the separate `#carteira`
screen, browser navigation, initial budgets/quantities, zero capital, saved
results and archived-demo suppression. It deletes its disposable account.
Captures and evidence are ignored under `.sites-runtime/live-portfolio-check`.
Older `test-recommender-*`, `test-rendafutura-e2e` and dual-policy integration
scripts describe historical demonstration flows, not the real-only web contract.
Algorithm and data-validation regressions remain in `recommender/test_audit.py`.

The initial-position table uses only current capital, rounds shares to integers
and Treasury fractions down to 0.01, and leaves residual cash uninvested. Monthly
income is an annual gross estimate divided by 12, not a payment calendar. Future
contributions are a separate budget column and never inflate initial income.
Reference Treasury prices are historical sale PU, not executable purchase quotes;
costs, taxes, offer availability and minimum purchase amounts require verification.
Monte Carlo still uses continuous target weights, not rounded purchase quantities.

### Safety and income portfolio comparisons

The local `C:/dividends/api.py` functions `_montar_carteiras` and `_avaliar_compra`
were inspected read-only. The former combines filtered dividend payers by
historical payment-month coverage and yield, with an ex-date bonus; the latter
scores fundamentals separately. RendaFutura adapts the idea, not the cached
prices, ex-date bonuses, constant-yield projections, US assets or crypto.

`recommender/portfolios.py` ranks eligible instruments by configured risk,
available earnings/FCF/payout alerts, historical real volatility, then income.
Treasury instruments are not scored as companies. Greedy payment-coverage
subsets, a minimum-volatility reference and a maximum-income constrained
reference seed NSGA-II. Every candidate still obeys profile risk, liquidity,
single-stock, equity and worst-rollover-state volatility limits.

Distinct lower-volatility, higher-income and goal-oriented compositions are
compared, not relabeled copies of one allocation. A single feasible composition
is shown only once. Each receives its own full Monte Carlo and inverse-planning
result on identical independent final paths, making comparisons controlled.
The browser switches allocation, initial quantities, income, chart, funding
target and historical payment calendar together. Export includes the actively
viewed portfolio as well as the complete saved comparison.

The month pattern requires payments in at least half of the observed occurrences
of each calendar month over at most 36 months. It uses paid-cash history, never
ex-dates, and is not an announced future calendar or guarantee. Relative safety
is a historical market-risk proxy, not a credit rating or loss guarantee.

The published Worker needs a separately hosted HTTPS Python service and secure
`RECOMMENDER_URL` / `RECOMMENDER_TOKEN` bindings. The local helper's loopback URL
cannot serve a cloud Worker. No public deployment of the new engine is performed
by the local helper; an unconfigured environment shows an unavailable state.

A clean full-stack starter running on [vinext](https://github.com/cloudflare/vinext), with optional Cloudflare D1 and Drizzle support.

## Prerequisites

- Node.js `>=22.13.0`
- Portable: Windows, macOS, or Linux; no Bash required
- Managed Linux: managed Linux runtime with Bash, `flock`, `curl`, `sha256sum`, and GNU `timeout`
- Git is required only for publishing

## Sites Lifecycle

The Sites initializer copies the shared starter and selects managed-linux only when `SITES_MANAGED_LINUX_CONTAINER=1`; otherwise it selects portable. It saves the selection only in ignored `.sites-runtime/execution-profile.json`. Both profiles copy/configure first, then use the plugin's separate `install-dependencies.mjs` step to measure installation independently. Edit source under `app/` and follow the Sites skill for installation, preview, builds, and publishing.

Run `node <plugin-root>/scripts/configure-execution-profile.mjs` only when the profile is unknown for the current checkout and environment. Profile changes do not alter tracked source or require reinstalling otherwise-valid dependencies; restart an existing preview to use the new selection. Do not commit or upload `.sites-runtime/`.

This starter does not use `wrangler.jsonc`.

`install:ci` runs `npm ci` once against the shared lockfile, disables parent-workspace discovery, and includes required dev/optional dependencies despite production/omit settings. Sharp defaults to prebuilt binaries unless explicitly configured otherwise. Do not overlap installers.

- **Portable:** Preserve host HOME, npm cache, registry, proxy, temporary paths, retry/concurrency settings, and lifecycle-script policy. Use `--prefer-offline --no-audit --no-fund`.
- **Managed Linux:** Use the existing project-local HOME/cache/tmp setup and Linux install lock, tarball preflight, and timeout. Restore the image-seeded npm cache only when its lockfile hash matches; retain network fallback. Builds keep their existing timeout. These helpers are not invoked by the portable profile.

`scripts/sites-env.mjs` preserves the caller's HOME, npm cache, proxy, XDG, and temporary-directory configuration while defaulting Wrangler and Miniflare state to the checkout. If npm reports an unwritable cache, select a writable path with `npm_config_cache` for that install. The `dev` and `start` scripts also keep Wrangler logs inside the checkout. Generated `.sites-runtime/` and `.wrangler/` directories are disposable and ignored by Git.

On portable, `npm run dev` uses `vinext dev` with HMR, starting at port 5173. Vinext records the running server in ignored `.vinext/` state, rejects an ordinary duplicate launch, and recovers stale state after a stopped process; exactly simultaneous starts can race. Pass `--port <port>` or `--hostname <host>` after `npm run dev --` when needed; keep portable previews on loopback.

For browser QA on managed Linux, use `sites-preview start`. The project's dev script runs Vite and accepts the supervisor's `--host 0.0.0.0 --port 4173 --strictPort` arguments. The internal browser uses `http://terminal.local:4173/`; it is not a user-facing URL. The supervisor owns the preview lifecycle. The ignored local profile survives the supervisor's cleared process environment.

The portable profile simulates ChatGPT sign-in only for loopback development requests. Visit `/signin-with-chatgpt?return_to=/` to sign in as `local_seedy` (`seedy@sites.test`, display name `Seedy`) and `/signout-with-chatgpt?return_to=/` to sign out. The development cookie preserves that identity across server restarts. Mock auth is disabled in the managed-linux profile and is not included in production builds; hosted authentication remains dispatch-owned.

The Worker uses `vinext/server/fetch-handler`, including Vinext's config-aware image handling. After building, `npm start` runs that Worker locally through Wrangler on `127.0.0.1`, sharing `.wrangler/state` with dev preview and local D1 migrations; it does not deploy the site or simulate sign-in. Use the URL printed by the server. Pass `npm start -- --port <port>` to select a different built-preview port.

Local previews use Miniflare's placeholder `Request.cf` metadata without a network lookup. Set `CLOUDFLARE_CF_FETCH_ENABLED=true` to opt into fetching preview metadata; this setting does not change hosted request metadata.

Local tool usage metrics are disabled by default. Set `WRANGLER_SEND_METRICS=true` to opt in.

## Included Shape

- edit site code under `app/`
- `app/chatgpt-auth.ts` provides optional dispatch-owned ChatGPT sign-in helpers
- `.openai/hosting.json` declares optional Sites D1 and R2 bindings
- `vite.config.ts` simulates declared bindings for local development
- `db/index.ts` reads the D1 binding from the Cloudflare Worker environment
- `db/schema.ts` starts intentionally empty
- `@cloudflare/workers-types` provides Worker types; `cloudflare-env.d.ts` declares optional `DB`/`BUCKET` bindings—update these declarations if binding names change
- `examples/d1/` contains an optional D1 example surface
- `drizzle.config.ts` supports local migration generation when needed

## Workspace Auth Headers

Signed-in visitors receive both `oai-authenticated-user-id` and `oai-authenticated-user-email`. Private Sites require every visitor to sign in; public Sites may also have anonymous visitors, for whom neither header is present.

The user ID is stable for the same user on the same Site and different across Sites. Use it as the durable user key; use email and name for display or contact purposes.

SIWC-authenticated workspace sites may also receive `oai-authenticated-user-full-name` when the user's SIWC profile has a non-empty `name` claim. The full-name value is percent-encoded UTF-8 and is accompanied by `oai-authenticated-user-full-name-encoding: percent-encoded-utf-8`.

Treat the full name as optional and fall back to email when it is absent:

```tsx
import { headers } from "next/headers";

export default async function Home() {
  const requestHeaders = await headers();
  const userId = requestHeaders.get("oai-authenticated-user-id");
  const email = requestHeaders.get("oai-authenticated-user-email");
  const encodedFullName = requestHeaders.get("oai-authenticated-user-full-name");
  const fullName =
    encodedFullName &&
    requestHeaders.get("oai-authenticated-user-full-name-encoding") ===
      "percent-encoded-utf-8"
      ? decodeURIComponent(encodedFullName)
      : null;

  const displayName = fullName ?? email;
  // ...
}
```

## Optional Dispatch-Owned ChatGPT Sign-In

Import the ready-to-use helpers from `app/chatgpt-auth.ts` when the site needs optional or required ChatGPT sign-in:

- Use `getChatGPTUser()` for optional signed-in UI.
- Use the returned `userId` as the stable user key for user-owned records; do not use email as a durable identifier.
- Use `requireChatGPTUser(returnTo)` for server-rendered pages that should send anonymous visitors through Sign in with ChatGPT.
- In a Server Component, start sign-in with `<a href={chatGPTSignInPath(returnTo)} target="_top">`. The auth helper module is server-only; do not import it into a Client Component.
- Do not use `fetch`, XHR, a client-side router, or a framework link that can prefetch the sign-in route. SIWC must start as a top-level navigation.
- Never request the AuthAPI authorization endpoint directly. The dispatch-owned `/signin-with-chatgpt` route must start the SIWC flow.
- Use `chatGPTSignOutPath(returnTo)` for browser sign-out links or actions.
- Pass a same-origin relative `returnTo` path for the destination after sign-in or sign-out. The helper validates and safely encodes it.
- Mark protected pages with `export const dynamic = "force-dynamic"` because they depend on per-request identity headers.

Dispatch owns `/signin-with-chatgpt`, `/signout-with-chatgpt`, `/callback`, the OAuth cookies, and identity header injection. Do not implement app routes for those reserved paths. Routes that do not import and call the helper remain anonymous-compatible.

SIWC establishes identity only; it does not prove workspace membership. Use the Sites hosting platform's access policy controls for workspace-wide restrictions, or enforce explicit server-side membership or allowlist checks.

Use SIWC for account pages, user-specific dashboards, saved records, and write actions tied to the current ChatGPT user. Leave public content anonymous.

## Local D1 migrations

For a D1-backed local preview, generate SQL with `npm run db:generate`. Build once through the Sites skill's build entrypoint (or `npm run build` for standalone use) to generate `dist/server/wrangler.json`, rebuilding if bindings change. From the project root, apply each pending migration in order:

```sh
node --import ./scripts/sites-env.mjs ./node_modules/wrangler/bin/wrangler.js d1 execute DB --local --config dist/server/wrangler.json --persist-to .wrangler/state --file drizzle/0000_example.sql
```

Replace the filename with the pending migration and `DB` with your D1 binding name if different. Use `.wrangler/state`, not `.wrangler/state/v3`; Wrangler adds the versioned directories. Do not replay migrations already applied locally. This updates only the preview database; publishing applies production migrations separately.

## Diagnostic Commands

- `npm run install:ci`: perform the one locked dependency install
- `npm run dev`: start the Vite/Vinext development server
- `npm run build`: build the deployable Sites artifact
- `npm run start`: preview the built Worker locally with D1/R2 support
- `npm run db:generate`: generate Drizzle migrations after schema changes

When using the Sites plugin, follow its skill instructions for installation, builds, and publishing. These npm commands remain available for standalone use.

The portable build runs Vinext directly without a host `timeout` command. The managed-linux build uses `scripts/build-verified.sh` and its existing `SITES_BUILD_TIMEOUT` setting.

## Learn More

## Retirement income without selling principal

The web planner now submits `income_mode=distributions_only`. The target entered
by the user is not a forecast. The engine does not sell portfolio principal to
cover an income shortfall. Legacy `total_return` results are explicitly marked
as withdrawal plans in the UI and must not be cited as dividend-income results.

Income scenarios require verified cash payments aligned with the total-return
history. An optional `cashflows.csv` has the same dates and ticker columns as
`returns.csv`, with gross paid cash divided by the position value at the start
of each month. It must exclude redemption/amortization of principal. Its
metadata in `assets.json` must include `cashflow_basis=verified_paid_cash`,
`cashflow_excludes_principal=true` and a `cashflow_source` object with an HTTPS
`url` and the original evidence's `sha256`. These are technical provenance
requirements, not financial certification. Missing payments are not replaced
with announced rights or an interest rate divided by twelve.

Returns, paid cash and inflation use identical bootstrap indices. During
accumulation, total returns include reinvested distributions. During retirement,
cash is removed once from those total returns and paid out; the model does not
sell principal to make up a deficit. Fees and market changes can still change
principal value. The reported monthly amount is an annual-payment average,
not a promise of payment every month. Its capacity is the lowest annual average
over the retirement phase, calculated per scenario before the percentiles.

The original snapshot contained zero-coupon bonds and equity rights by
data-com, not complete paid histories. The paid-cash collector below adds
observed distributions and coupon bonds. A portfolio of documented
non-paying bonds still reports zero periodic income. If eligible equities lack paid
cash coverage, the engine returns `income_data_unavailable`, with indicators and
filters, rather than a fabricated projection or a principal-withdrawal fallback.
The UI reports catalog coverage and displays P/E, earnings per share, free cash
flow, payout and liabilities/assets alongside the reasons for exclusion.

The NSGA-II goal objective uses the mean fraction of income left unfunded;
binary success alone provides no differentiation when all portfolios miss a
large target. Risk, liquidity and concentration limits remain unchanged.

## Paid-cash market snapshot

Run `.recommender-venv/Scripts/python.exe -m recommender.collect_income_data`
to collect, validate and atomically publish the paid-cash snapshot. Use
`--no-publish` to inspect a candidate without changing the active dataset.
The 2026-09 reference contains PETR4, VALE3 and nine Treasury securities,
including six semiannual coupon bonds. It uses 60 aligned return/payment
months and longer historical payment/fundamental observations for training.

Petrobras payments come from the issuer's linked Excel file: PETR4, status
PAGO/PAGO PRESCRITO, reported payment date and gross declared component.
Future/unpaid rows are excluded; later Selic corrections are not added.
Vale payments use the v2 brapi payment history, not record dates. The missing
date of the 2019 JCP is reconciled against the issuer's 2020-07-29 release;
other missing current dates/deadlines block collection. Old events outside
the documented 2015+ scope are excluded, not used as zero-paid training labels.
Provider verification is not an independent completeness certification.

Treasury cash uses the official coupon-only resource, including actual dates
and unit coupon values, excluding principal redemption. Securities missing
an expected observed coupon or 61 consecutive prices are excluded, not filled.
Stock prices must use explicit brapi v2 `rawClose`; ambiguous `close` values
are refused. Total monthly returns are constructed from unadjusted prices plus
paid cash. The aligned cash ratio is removed once during retirement.
The monthly convention assumes eligible holdings and does not track individual
record-date quantities or receivables after trades. Reinvestment after maturity
is modelled using the same-class policy above, not contractual cash pricing.

The target `cash_payments_12m_v2` uses paid dividend/JCP amounts and is not
comparable to the old rights target. XGBoost is trained/evaluated with purged
temporal validation and a separate test. Forecasts now influence the cash
objective, allocation search, and the first-year cash/return paths, including
validation-error uncertainty. `model.appliedMethod`, `applicationPolicy`,
`usedForIncomeSimulation` and `forecastHorizonMonths` expose the actual policy.
No claim of superior AI performance is made if the baseline is better. The
initial two-stock sample's 36 overlapping temporal test observations gave
XGBoost MAE 3.125 versus baseline MAE 2.023 BRL/share. See the expanded sample
below for current metrics; these are forecast errors, not portfolio returns.

Each snapshot includes `cashflows.csv`, `payment-ledger.json`, file hashes,
source URLs and excluded-bond reasons in addition to prices and observations.
Classification and risk/liquidity/concentration limits are unchanged. A
conservative case with only zero-coupon liquidity remains zero-income.

### Free access and catalogue

Set `BRAPI_API_KEY` in the collector environment, or run
`scripts/configure-brapi.ps1` to store a Windows-DPAPI-protected local credential
under ignored `.sites-runtime`. It is never sent to the frontend, placed in
URLs, or printed. `python -m recommender.probe_brapi` runs a small sequential
access audit without dumping response bodies. On this account, BBAS3 basic
quotes/short history returned 200, long history returned 400, and dividends
returned 403. Sandbox raw history was available for the prepared sample.
No paid subscription is required or purchased by the app.

`python -m recommender.catalog` builds a separate public catalogue: 778 active
stock/unit entries and 58 Treasury instruments with a recorded 2026-10-15
price at the time of collection. That is not a claim of complete training data
or that every Treasury instrument is available to buy today. The modelling
sample now contains 44 instruments (35 shares and nine Treasury titles), with
60 concurrent monthly returns through 2026-09-30. Fractional aliases account
for 404 catalogue codes, not 404 additional companies. The remaining exclusions
are recorded individually; catalogue membership is not enough for modelling.

### Expanded public-data sample (2026-10-24)

`python -m recommender.collect_expanded_data` constructs an unpublished immutable
candidate. It retains the reconciled Petrobras/Vale/Treasury base, scans the
remaining 352 ordinary/preferred-share codes, maps exact B3 issuer/class/ISIN
identities, collects public Investidor10 dated installment tables and Yahoo
closing-price histories, and crosses payment amounts and historical quotes
against B3. Yahoo ex-dates are never used as payment dates. Future dated
installments reconcile declared rights but do not enter paid cash. Recent
share transformations, discrepancies, absent dates and unsupported unit
compositions are rejected, not imputed. Training never predates the available
previous-payment window or crosses an unreconciled share scale.

Official annual CVM DFP filings supply class-specific EPS, identified capex,
operating cash and liabilities, with fiscal/publication dates. Missing bank or
other sector accounts are not replaced with invented free cash flow. Payment
tables are third-party reports, not bank statements or proof of completeness.
This retrospective sample still has availability/survivorship bias.

The final sample contains 2,909 training-window observations and 591 payment
events within the return window. Temporal test: XGBoost MAE 0.7531 versus
baseline 0.5817 BRL/share (627 overlapping observations). It still does not beat
the reference overall. XGBoost remains the explicit experimental web policy;
validation residuals, not test labels, drive uncertainty calibration.

The loader admits up to 1,000 prepared instruments. Every eligible stock gets
a forecast and ranking before a deterministic shortlist of at most 24 assets
is optimized. Up to 12 eligible Treasury anchors are preserved. Safety,
income, historical payment-month coverage and sector alternate in selection;
simulated outcomes do not choose the shortlist. Eligible, optimized and
deferred counts/tickers are separate. Explicit CVM issuer IDs enforce a shared
25% equity concentration cap across ON/PN classes of the same company.

Treasury prices refreshed successfully; the coupon resource returned HTTP 429
and its previous valid cache was retained. Three additional coupon titles
remain blocked for absent observed August 2026 payments. Other recent titles
do not have 61 actual monthly price anchors; Educa+/Renda+ amortization and
legacy IGP-M structures are not silently treated as coupon-only instruments.
No premium API plan was purchased, no access controls bypassed, and no
principal repayments were reclassified as income.

Run `python -m recommender.audit_expanded_market <personas.json> <output-folder>`
with `RF_DATA_DIR` pointing to the candidate to check all eight synthetic
profiles and every compared portfolio. `--publish` on the collector explicitly
activates a candidate after validation; do this only after reviewing its audit.
The local development watcher ignores market archives, runtime reports and the
Python environment so OneDrive file locks cannot crash frontend file watching.

Share transformations are accepted only after price and payment amounts reconcile
to B3 reference evidence in every relevant share-count regime. Spin-offs and
unresolved events remain excluded. Financial-sector industrial FCF can be
explicitly null, with the account method retained; no operating-cash proxy or
zero is substituted. XGBoost uses its native missing-value handling for this
specific feature. Limited per-issuer training is disclosed as pooled inference.

Current observed quotes are separate from the monthly model price. Official
Treasury purchase offers come from `recommender.treasury_current`; an old bond
absent from that catalogue gets no new-purchase quantity. Initial income uses
cash per unit, not an old yield multiplied by a new price. Unquoted target
allocations remain unallocated; income on confirmed positions is shown as a
partial amount, never the income of the complete target portfolio.

`python -m recommender.treasury_file_import <official-coupon.csv> --apply`
validates and archives a user-provided Treasury coupon file without changing the
input. Missing or revised prior payments require review before cache activation.
`recommender.historical_import` provides CSV/XLSX validation and empty templates;
`recommender.historical_provider` is an optional offline adapter requiring an
exact B3 identity register and explicit coverage. It is not an automatic importer
of arbitrary downloaded spreadsheets into the active dataset.

`python -m recommender.data_coverage --data-dir recommender/data/market --output
recommender/data/market/coverage/summary.json` provides a read-only diagnostic:
prepared/catalogued counts, saved access observations, source dates and actual
exclusion reasons. The authenticated interface and saved results expose these
counts without credentials. ITUB4/MGLU3 sandbox access works, but reported share
actions and financial features still require consistent per-share normalization.
Other Treasury gaps include insufficient monthly histories, missing observed
coupons and unsupported amortizing/indexed structures. Not every gap is an API
subscription issue.

The browser calls `/api/recommendation` on its own origin. The web server
authenticates the session, validates consent/profile/settings, then calls the
loopback Python service with a server-only bearer token. Both layers reject
cross-origin mutations/direct browser engine requests; no wildcard CORS is
enabled. The browser never calls brapi or port 8765 directly. Web bodies are
limited to 16,384 actual streamed bytes before storage/JSON parsing, independent
of Content-Length; the engine has a separate 4096-byte streaming cap.

`python -m recommender.audit_integration <personas.json> <output-folder>`
replays eight persona fixtures and compares policies for profiles with equities.
`node scripts/test-integrated-income.mjs <local-url> <personas.json> <output-folder>`
then verifies browser/API/engine parity, both policies, screenshots, export,
persistence, revocation, account cleanup and mobile layout. These tests use
synthetic people and are engineering checks, not financial validation.

## Independent market inventory

`python -m recommender.collect_inventory --as-of 2026-10-24 --complete` collects
and publishes an inventory independently of the validated simulation snapshot.
It uses official open CVM FCA identities, DFP/ITR financial accounts and the IPE
index of proventos documents. Current Treasury offers, historical prices and
observed coupons are separate official downloads. Equity quotes are explicitly
secondary Yahoo observations, not certified exchange prices. Existing secondary
payment pages are parsed by semantic column headers rather than a fixed table ID.
Petrobras has a bounded issuer-XLSX primary fallback; the CVM IPE index for other
issuers discovers documents but does not itself supply per-share cash payments.

Each component retains its own date, status, raw file, source URL and SHA-256.
Missing payments do not erase an available quote or balance sheet, and document
references never become paid-cash observations. Financial issuers do not receive
industrial free-cash-flow estimates. Units retain their declared composition,
but an unknown unit-level EPS remains null rather than using ordinary-share EPS.
There is no minimum payment count or 61-month history gate for catalogue entry.
FCA-declared listings and the older catalogue are not proof of active trading.

`python -m recommender.current_quote_inventory collect` makes bounded BASIC-only
BRAPI requests for missing or stale catalogue references. It reuses immutable
receipts, never requests premium dividends/history, and stops on access/rate
denials. `publish-inventory --report <reported-path>` is a separate offline step
which refuses a changed base or running model preparation. Provider snapshots
remain indicative: snapshot, collection and known-trade clocks are distinct,
and updating the catalogue does not approve a model asset or executable order.

Native source corrections retain original bytes and the replaced interpretation
in their audit. These include the Petrobras workbook's duplicated-s
extraordinary-dividend label, unavailable reference-date sentinels and reviewed
older CVM notice revisions. A missing quote date never becomes a price, and an
unresolved notice is not repaired by guessing a year or grossing up a net amount.

The authenticated `GET /api/recommendation/market` displays the inventory in
"Ativos e fontes de dados". A separate immutable `inventory/current.json` pointer
cannot alter the model's `current.json`: collecting more instruments is not proof
that their histories are reconciled for XGBoost/NSGA-II/Monte Carlo. The current
simulation sample and reference date remain unchanged by this command. Historical
Treasury coupon coverage can end before the collection date; missing coupons and
unvalidated principal/interest splits are not filled with invented income.

`python -m recommender.collect_inventory --as-of 2026-10-24 --daily-histories`
acquires missing daily Yahoo histories into a separate cache, without replacing
the earlier reconciled files or making any automated B3 requests.
`python -m recommender.prepare_inventory` then attempts every inventory code,
reads cached annual CVM accounts independently, and builds an unpublished model
candidate. It records one verdict per code, missing evidence, and prior histories
retained from the preceding model. New admissions require explicit payment
evidence, not merely a past scheduled date. An unchanged number of model columns
is not reported as an enlarged simulation sample. Review/test the candidate
before using `--publish`; the active snapshot is never modified in place.

Engine results include the frozen `universeScreening`: every stock receives
separate data-readiness and profile checks. The results table shows annual
accounts when available, a reason for every data-pending code, and XGBoost values
only for prepared, profile-eligible equities. Unprepared stocks are not assigned
fictional returns, dividends, covariance or portfolio weights. The NSGA-II search
still uses the documented bounded shortlist after eligible assets are ranked.

Raw open files are cached locally for reproducibility. No automated B3 website
collection, authentication bypass, paid subscription or API-key disclosure is
used. Providers' rate/access errors stop acquisition and remain visible as data
gaps, not zero payments.

## Legacy rights snapshot

The real forecasting target `rights_by_record_date_v1` is gross DIVIDENDO/JCP
entitlement per share attributed uniformly to the reported data-com
(`lastDatePrior`). It excludes RENDIMENTO and later monetary corrections. It is
not actual paid cash, a payment calendar or monthly retirement income. Missing
data-com is not reconstructed from an ex-date or payment date. The forecasting
target changed after a data-quality review; its errors must not be compared
directly with those of the previous paid-cash target. Retirement paths continue
to use total returns without adding this indicator to simulated wealth.

Run `.recommender-venv/Scripts/python.exe -m recommender.collect_market_data --refresh`
from this checkout. No market API key is needed for the initial PETR4/VALE3
universe: brapi permits those tickers without authentication. An optional
`BRAPI_API_KEY` is read only from the server environment and sent as a Bearer
header, never placed in a URL or in the browser.

The collector downloads adjusted daily prices and paid cash distributions from
brapi, annual DFP statements with actual disclosure dates from CVM, monthly IPCA
(SGS 433) from BCB, and zero-coupon bond prices from Tesouro Transparente. It
retains raw downloads, URLs, collection times, SHA-256 digests, and an explicit
methodology/limitations list under the ignored `recommender/data/market/` folder.
Each immutable snapshot contains `assets.json`, `returns.csv`,
`observations.csv`, and `collection-report.json`. An atomically replaced pointer
selects one complete version, preventing readers from mixing files from separate
collections. A blocked collection does not silently continue with an old version.
Missing observations are not replaced by synthetic ones. Cash events with unknown
payment dates exclude affected windows unless an explicit reconciliation records
a primary-source URL, evidence hash, raw-event identity and verified fields.
For a reported payment without an approval date, historical availability is no
earlier than the payment itself; the collector does not invent an approval date.
Coverage and excluded financial statements are recorded in the collection report.

`scripts/run-recommender-local.ps1` loads this directory automatically when
present; override it with `-DataDirectory` or set `RF_DATA_DIR` when running
uvicorn directly. Re-run the collection after each completed month. The engine
rejects snapshots older than 62 days. The simulation uses gross real historical
observations, not streaming quotes, and remains experimental. Bond reinvestment
uses the explicit class-proxy policy; zero-coupon price appreciation is not
monthly cash income. The model's reported baseline comparison must not be interpreted
as proof of predictive skill or as financial validation.

- [vinext Documentation](https://github.com/cloudflare/vinext)
- [Drizzle D1 Guide](https://orm.drizzle.team/docs/get-started/d1-new)
