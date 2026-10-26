import { env } from "cloudflare:workers";

export async function callEngine(path:"status"|"recommend"|"market", data?:unknown) {
  const {RECOMMENDER_URL:url,RECOMMENDER_TOKEN:token} = env;
  if(!url||!token)throw new Error("Motor financeiro não configurado neste ambiente.");
  const base = new URL(url);
  if(base.protocol!=="https:"&&!(base.protocol==="http:"&&["127.0.0.1","localhost"].includes(base.hostname)))throw new Error("Motor financeiro precisa de uma conexão segura.");
  const response = await fetch(new URL(`/${path}`,base),{
    method:data?"POST":"GET",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},
    body:data?JSON.stringify(data):undefined,signal:AbortSignal.timeout(path==="recommend"?300_000:90_000),
  });
  const result = await response.json() as {detail?:unknown};
  if(!response.ok)throw new Error(typeof result.detail==="string"?result.detail:"Motor financeiro indisponível. Tente novamente.");
  return result;
}
