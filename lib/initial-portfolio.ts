import type { Allocation, AssetAnalysis } from "./recommendation";

export function initialPortfolio(allocation:Allocation[], analysis:AssetAnalysis[], capital:number, contribution:number) {
  if(!Number.isFinite(capital)||capital<0||!Number.isFinite(contribution)||contribution<0)throw new Error("Invalid portfolio budget.");
  const capitalCents=Math.floor(capital*100);
  const contributionCents=Math.floor(contribution*100);
  const assets=new Map(analysis.map(asset=>[asset.ticker,asset]));
  const weightSum=allocation.reduce((sum,asset)=>sum+asset.weight,0);
  if(!Number.isFinite(weightSum)||Math.abs(weightSum-1)>1e-6||allocation.some(asset=>!Number.isFinite(asset.weight)||asset.weight<0))throw new Error("Invalid portfolio weights.");
  const rows=allocation.filter(asset=>asset.weight>1e-12).map(asset=>{
    const weight=asset.weight/weightSum;
    const source=assets.get(asset.ticker);
    const budget=Math.floor(capitalCents*weight)/100;
    const nextContribution=Math.floor(contributionCents*weight)/100;
    const quote=source?.currentQuote;
    const quoteDay=quote?Date.parse(quote.as_of.slice(0,10)+"T00:00:00Z"):null;
    const quoteAge=quoteDay===null?null:(Date.now()-quoteDay)/86400000;
    const fresh=quoteAge!==null&&Number.isFinite(quoteAge)&&quoteAge>=0&&quoteAge<8;
    const value=quote?quote.purchase_available&&fresh?quote.price:null:source?.price;
    const price=value!=null&&Number.isFinite(value)&&value>0?value:null;
    const step=asset.assetClass==="acoes"?1:asset.ticker.startsWith("TD-")?(quote?quote.minimum_fraction:0.01):null;
    const valid=source?.eligible&&price!==null&&Number.isFinite(price)&&price>0&&step!==null;
    let units=valid?Math.floor(budget/price/step+1e-10):0;
    let amount=valid?Math.max(0,Math.ceil(units*step*price*100-1e-8)/100):0;
    if(amount>budget){units=Math.max(0,units-1);amount=Math.max(0,Math.ceil(units*step!*price!*100-1e-8)/100);}
    if(quote?.minimum_investment!=null&&amount<quote.minimum_investment){units=0;amount=0;}
    const quantity=valid?Number((units*step!).toFixed(2)):null;
    const yield12m=Number.isFinite(asset.cashYield12m)&&asset.cashYield12m>=0?asset.cashYield12m:null;
    const historicalPaidYield=source?.annualCashPaymentsYield??yield12m;
    const cashPerUnit=asset.predictedCashPerShare!=null?asset.predictedCashPerShare:
      historicalPaidYield!==null&&source?source.price*historicalPaidYield:null;
    return {ticker:asset.ticker,name:asset.name,weight,price,asOf:quote?.as_of??source?.asOf??asset.asOf,
      quoteBasis:quote?.basis??"historical_model_price",marketStatus:quote?.market_status??"unknown",
      snapshotAt:quote?.snapshot_at??null,retrievedAt:quote?.retrieved_at??null,lastTradeAt:quote?.last_trade_at??null,
      budget,nextContribution,quantity,amount,annualCash:budget===0?0:yield12m===null||quantity===null?null:
        quote&&cashPerUnit!==null?quantity*cashPerUnit:amount*yield12m};
  });
  const allocatedCents=rows.reduce((sum,row)=>sum+Math.round(row.amount*100),0);
  const annualCash=rows.every(row=>row.annualCash!==null)?rows.reduce((sum,row)=>sum+row.annualCash!,0):null;
  const knownAnnualCash=rows.reduce((sum,row)=>sum+(row.annualCash??0),0);
  const unquotedBudget=rows.filter(row=>row.quantity===null).reduce((sum,row)=>sum+row.budget,0);
  return {rows,capital:capitalCents/100,allocated:allocatedCents/100,remainder:(capitalCents-allocatedCents)/100,
    annualCash,monthlyAverage:annualCash===null?null:annualCash/12,knownAnnualCash,unquotedBudget};
}
