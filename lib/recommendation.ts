import { z } from "zod";
import { classify, validateAnswers, type Answers } from "./questionnaire";

export const planSettingsSchema = z.object({
  incomeMode: z.literal("distributions_only").default("distributions_only"),
  endAge: z.number().int().min(20).max(120),
  monthlyContribution: z.number().finite().min(0).max(1_000_000),
  annualCostPercent: z.number().finite().min(0).max(5),
  simulations: z.union([z.literal(500), z.literal(1000), z.literal(2000)]),
  blockMonths: z.union([z.literal(3), z.literal(6), z.literal(12)]),
  seed: z.number().int().min(0).max(2_000_000_000),
  dataMode: z.enum(["demo", "real"]),
  forecastPolicy: z.enum(["validation", "xgboost_experimental"]).default("xgboost_experimental"),
  consent: z.literal(true),
}).strict();
export type PlanSettings = z.infer<typeof planSettingsSchema>;
export type DataCoverage = {status:string;counts:{catalogue:number|null;modelReady:number|null;catalogueGap:number|null};credentialConfigured:boolean;gaps:{assetClass:string;reason:string;count:number;examples:string[]}[];sandbox:{ticker:string;modelReady:boolean;blockers:string[]}[];accessProbe:{status:string;checkedAt:string|null;fileModifiedAt:string|null;checks:{name:string;symbol:string;httpStatus:number|null;available:boolean|null}[]};dates:{referenceDate:string|null;latestSourceRetrievedAt:string|null}};
export type EngineStatus = {engineVersion:string;realDataRevision?:string|null;realDataAvailable:boolean;realDataMessage:string;realDataReference?:string|null;realDataAssets?:string[];currentContractPortfolios?:boolean;currentContractKeys?:string[];cashIncomeDataAvailable?:boolean;cashIncomeMessage?:string;catalog?:{stocks:number;treasury:number;treasuryReference:string;collectedAt:string}|null;marketInventory?:{asOf:string;collectedAt:string;counts:Record<string,number>}|null;universePreparation?:{evaluatedStocks:number;readyStocks:number;pendingDataStocks:number;asOf:string;preparedAt:string}|null;dataCoverage?:DataCoverage|null;demoAvailable:boolean;financiallyValidated:false};
export type DividendYieldSelection = {id:string;rule:string;sourceSHA256:string;sourceDate:null;sourceRows:number;selectedTickersCount:number;zeroYieldTickersCount:number;matchedCatalogTickersCount:number;unmatchedTickers:string[];outsideScopeStocks:number;selectedReadyStocks:number;selectedPendingStocks:number;treasuryUnchanged:true};
export type UniverseScreening = {
  asOf:string;modelReference:string;preparedAt:string;inventorySha256:string;
  dividendYieldSelection?:DividendYieldSelection|null;
  counts:{stocks:number;readyStocks:number;pendingDataStocks:number;annualAccountsStocks:number;profileCompatibleStocks:number;excludedByProfileStocks:number;eligibleStocks:number;forecastedStocks:number;selectedScopeStocks?:number;excludedBySelectionStocks?:number};
  stocks:{ticker:string;name:string;dataReady:boolean;profileCompatible:boolean|null;profileReasons:string[];dataReasons:string[];dataReasonCodes:string[];eligible:boolean;riskLevel:number;minimumYears:number;liquidityDays:number;
    selectionCompatible?:boolean;selectionReasons?:string[];
    observedQuote?:{value?:number|null;as_of?:string;status?:string};financialPeriod?:string|null;predictedCashPerShare:number|null;
    annualAccounts?:{available:boolean;accounts_scope?:string|null;fiscal_end?:string|null;published_at?:string|null;earnings_per_share?:number|null;source_url?:string|null}|null;
    preparationBasis?:string;sourceReviewReasons?:string[];
  }[];
  missingDataAreZero:false;unpreparedAssetsAllocated:false;
};
export type Allocation = {ticker:string;name:string;assetClass:string;percent:number;weight:number;cashYield12m:number;predictedCashPerShare:number|null;source:string;asOf:string;cashMethod:string;reason:string};
export type FinancialMethod = {id:string;accounts_scope:string;profit_account:string|null;basic_eps_account:string|null;assets_account:string;equity_account:string|null;liabilities_method:string;liabilities_accounts:string[];capex_accounts:string[];fcf_absence_reason:string|null};
export type CurrentQuote = {price:number|null;as_of:string;source_url:string;purchase_available:boolean;basis:string;market_status?:string;minimum_fraction:number|null;minimum_investment:number|null;snapshot_at?:string|null;retrieved_at?:string|null;last_trade_at?:string|null;last_trade_source_url?:string|null};
export type TreasuryOffer = {name:string;kind:string;maturity:string;buy_price:number|null;buy_rate:number|null;buy_rate_text:string|null;as_of:string;market_status:string;offered_for_purchase:boolean|null;support_status:string};
export type AssetAnalysis = {ticker:string;name:string;price:number;eligible:boolean;exclusionReasons:string[];riskLevel:number;minimumYears:number;liquidityDays:number;historicalAnnualRealReturn:number|null;historicalAnnualVolatility:number|null;scenarioAnnualVolatility?:number;projectedRealIncomeYield12m?:number;historyMonths:number;source:string;asOf:string;currentQuote?:CurrentQuote|null;distributionBasis?:"payment_date"|"record_date"|"projected_contract_not_historical_payment";annualCashPaymentsYield?:number|null;financialMethod?:FinancialMethod|null;indicators:{previousAnnualRightsPerShare?:number;previousAnnualCashPerShare?:number;trailingDistributionYield:number;distributionMonthFraction:number;earningsPerShare:number;freeCashFlowPerShare:number|null;freeCashFlowAbsenceReason?:string|null;payout:number;liabilitiesToAssets:number;priceEarnings:number|null}|null};
type Coverage = {catalogAssets:number;eligibleAssets:number;optimizedAssets?:number;allocatedAssets:number;status:string;multiplePositions:boolean};
export type IncomeDiagnostic = {status:"income_data_unavailable";engineVersion:string;dataRevision?:string|null;dataMode:"real";referenceDate:string;message:string;missingCashAssets:string[];assetAnalysis:AssetAnalysis[];coverage:Coverage;universeScreening?:UniverseScreening|null;dataCoverage?:DataCoverage|null;methods:string[];datasetSource:string;financiallyValidated:false;requiresPrincipalWithdrawal:false};
export type AssetPriority = {ticker:string;name:string;assetClass:string;riskLevel:number;annualVolatility:number;cashYield12m:number;qualityAlerts:string[];historicalPaymentMonths:number[];historicalConsistency:number|null;paymentHistoryMonths:number;safetyOrder:number};
export type ComparedPortfolio = {id:string;label:string;safetyRank:number;allocation:Allocation[];simulation:EngineCalculation["simulation"];planning:EngineCalculation["planning"];portfolio:EngineCalculation["portfolio"];incomeAnalysis:NonNullable<EngineCalculation["incomeAnalysis"]>;coverage:Coverage;historicalPaymentCalendar:{month:number;tickers:string[]}[];historicalCoverageMonths:number;usesEquityForecast:boolean};
export type EngineCalculation = {
  status?:"ready";
  engineVersion:string;dataMode:"demo"|"real";datasetSource:string;referenceDate:string;financiallyValidated:false;
  dataRevision?:string|null;
  currentTreasuryAnalysis?:{
    evaluatedContracts:number;cashScenariosCalculated:number;compatibleContracts:number;optimizedContracts:number;
    scenarios:number;macroReference:string;scope:string;optimizationIntegrationStatus:string;portfolioReinvestmentIncluded?:boolean;
    rows:{ticker:string;name:string;kind:string;maturity:string;price:number;quoteDate:string;rateText:string|null;
      profileCompatible:boolean;profileReasons:string[];income12MonthsReal:number[];
      originalCostReturned12MonthsReal:number;incomeFirstRetirementYearOriginalLotReal:number[];
      nominalAnchorBasis:string;includedInPortfolioOptimization:boolean;institutionRestriction:string|null;
      principalCountedAsIncome:false;reinvestmentIncluded:false;futurePaymentsObserved:false;
    }[];
  }|null;
  forecastTarget?:{id:string;label?:string;date_field?:string}|null;
  marketData?:{collectedAt:string|null;cashflowBasis?:string;dataQualityPolicy?:string|null;pointInTimeArchiveVerified?:boolean|null;cashSettlementVerified?:boolean|null;cashflowTaxBasis?:string;totalReturnMethod?:string;currentTreasuryOffers?:TreasuryOffer[];historyStart:string;historyMonths:number;sources:{url?:string|null;file?:string;sha256:string;retrieved_at?:string|null}[];bonds:{ticker:string;maturity:string;coupon?:boolean;kind?:string;quote_date?:string;buy_rate?:number;cash_coupon?:number}[]};
  methods:string[];allocation:Allocation[];excluded:{ticker:string;reasons:string[]}[];warnings:string[];
  portfolio:{annualVolatility:number;cashYield12m:number;baselineSuccessFrequency:number|null;historicalAnnualRealReturn?:number;usesFixedReturnRate?:false};
  planning?:{status:string;criterion:string;attainmentCriterion:number;scenarioCount:number;requiredRetirementCapital:number|null;requiredRetirementCapitalNullReason?:string|null;requiredMonthlyContribution:number|null;requiredInitialCapitalWithCurrentContribution:number|null;currentMonthlyContribution:number;monthlyContributionGap:number|null;currentPlanSuccessFrequency:number;zeroIncomeScenarioFraction:number;portfolioReoptimized:false;usesPrincipalForIncome:false;taxBasis:string;contributionTiming:string}|null;
  plan:{income_mode?:"total_return"|"distributions_only";age:number;retirement_age:number;end_age:number;initial_capital:number;monthly_contribution:number;target_income:number;annual_cost_percent:number;simulations:number;seed:number;block_months:number};
  simulation:{successFrequency:number;samplingInterval95:number[];retirementWealth:number[];finalWealth:number[];medianDeficit:number;depletionFrequency:number;monthlyIncomeCapacity?:number[];meanUnfundedIncomeFraction?:number;incomeCoveragePeriod?:string;chart:{age:number;p10:number;p50:number;p90:number}[]};
  coverage?:Coverage;assetAnalysis?:AssetAnalysis[];dataCoverage?:DataCoverage|null;universeScreening?:UniverseScreening|null;
  portfolios?:ComparedPortfolio[];selectedPortfolioId?:string;assetRanking?:AssetPriority[];
  comparisonLabel?:string;historicalPaymentCalendar?:{month:number;tickers:string[]}[];historicalCoverageMonths?:number;usesEquityForecast?:boolean;
  construction?:{method:string;referenceProject:string;candidateCount:number;rankingRule:string;portfolioOrder:string;calendarBasis:string;calendarThreshold:number;commonFinalSeed:number;commonFinalScenarios:number;safetyIsGuarantee:false};
  rollover?:{policy:"same_class_same_risk";monthlyApproximation:boolean;contractualRedemptionPricing:boolean;principalCountedAsIncome:false;unknownFutureInstruments:string;events:{month:number;calendarMonth:string;referenceTicker:string;fromTicker:string;toTicker:string|null;class:string;basis:string;principalCountedAsIncome:false}[]};
  incomeAnalysis?:{method:string;status:string;usesPrincipal:boolean;monthlyCapacity:number[];targetMonthlyIncome:number;medianMonthlyGap:number;endAge:number;isDividendForecast:boolean};
  model:{mae:number;rmse:number;baselineMae:number;beatsBaseline:boolean;selectedMethod:"xgboost"|"baseline";appliedMethod:"xgboost"|"baseline";usedForIncomeSimulation?:boolean;applicationPolicy?:"validation"|"xgboost_experimental";forecastHorizonMonths?:number;uncertaintyBasis?:string;appliedTestMae:number;xgboostMae:number;xgboostRmse:number;validationMae:number;validationBaselineMae:number;byTicker?:Record<string,{rows:number;xgboostMae:number;baselineMae:number}>;trainRows:number;validationRows:number;testRows:number;financiallyValidated:false}|null;
  alternatives:{successFrequency:number;annualVolatility:number;cashYield12m:number;weights:{ticker:string;percent:number}[]}[];
  parameters:{population:number;generations:number;searchScenarios:number;finalScenarios:number;searchSeed:number;finalSeed:number;blockMonths:number;annualRebalance:boolean;volatilityBasis?:string;equityMaximum:number;liquidityMinimum:number;stockMaximum:number;annualVolatilityMaximum:number};
};
export type EngineResult = EngineCalculation | IncomeDiagnostic;
export type SavedRecommendation = {result:EngineResult;settings:PlanSettings;updatedAt:string}|null;

export function engineInput(rawAnswers:unknown, rawSettings:unknown) {
  const answers:Answers = validateAnswers(rawAnswers,true);
  const parsed = planSettingsSchema.safeParse(rawSettings);
  if(!parsed.success)throw new Error("Confira os parâmetros do planejamento e autorize seu processamento.");
  const settings = parsed.data;
  const retirementAge = Number(answers.retirementAge);
  if(settings.endAge <= retirementAge)throw new Error("Confira a idade final: ela deve ser maior que a idade de aposentadoria.");
  const contribution = String(answers.contribution);
  const range = contribution === "Não consigo aportar no momento" ? [0,0]
    : contribution === "Até R$ 500" ? [0,500]
    : contribution === "De R$ 501 a R$ 1.500" ? [501,1500]
    : contribution === "De R$ 1.501 a R$ 3.000" ? [1501,3000] : [3000.01,1_000_000];
  if(settings.monthlyContribution < range[0] || settings.monthlyContribution > range[1])throw new Error("Confira o aporte: ele deve corresponder à faixa declarada no questionário. Revise as respostas para mudar a faixa.");
  const result = classify(answers);
  return {settings,input:{
    data_mode:settings.dataMode,
    forecast_policy:settings.forecastPolicy,
    investor:{risk_level:["Conservador","Moderado","Arrojado"].indexOf(result.profile),knowledge:result.knowledge,
      withdrawal_years:[0,1,3,6][["Em menos de 1 ano","Entre 1 e 3 anos","Entre 3 e 5 anos","Depois de 5 anos"].indexOf(String(answers.withdrawal))],
      high_liquidity:answers.liquidity === "Muito provável"},
    plan:{income_mode:settings.incomeMode,age:Number(answers.age),retirement_age:retirementAge,end_age:settings.endAge,
      initial_capital:Number(answers.initialCapital),monthly_contribution:settings.monthlyContribution,
      target_income:Number(answers.targetIncome),annual_cost_percent:settings.annualCostPercent,
      seed:settings.seed,simulations:settings.simulations,block_months:settings.blockMonths},
  }};
}
