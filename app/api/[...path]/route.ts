import { classify, validateAnswers } from "@/lib/questionnaire";
import { CONSENT_VERSION, createSession, currentUser, database, digest, expiredCookie, passwordHash, randomSecret, sameOrigin, sessionToken, throttle, verifyPassword } from "@/lib/auth";
import { engineInput } from "@/lib/recommendation";
import { callEngine } from "@/lib/recommendation-service";
import { decodeRecommendation, encodeRecommendation } from "@/lib/recommendation-storage";
import { MAX_REQUEST_BODY_BYTES, readRequestBody, RequestBodyTooLargeError } from "@/lib/request-body";
export const dynamic="force-dynamic";
function json(data:unknown,status=200,cookie?:string){return Response.json(data,{status,headers:{"Cache-Control":"no-store",...(cookie?{"Set-Cookie":cookie}:{})}});}
async function handle(request:Request){
  try {
    const path=new URL(request.url).pathname.replace(/^\/api\//,"");
    const method=request.method;
    if(method!=="GET"&&!sameOrigin(request))return json({error:"Origem da solicitação inválida."},403);
    if(Number(request.headers.get("content-length")||0)>MAX_REQUEST_BODY_BYTES)return json({error:"Solicitação muito grande."},413);
    const body=await readRequestBody(request);
    const db=database();
    if(path==="auth/register"&&method==="POST"){
      const input=JSON.parse(body) as Record<string,unknown>;
      const alias=typeof input.alias==="string"?input.alias.trim():"";
      const email=typeof input.email==="string"?input.email.trim().toLowerCase():"";
      const password=typeof input.password==="string"?input.password:"";
      if(!alias||alias.length>60||email.length>254||!/^\S+@\S+\.\S+$/.test(email)||password.length<10||password.length>128)return json({error:"Confira nome, e-mail e senha de pelo menos dez caracteres."},400);
      if(input.consent!==true)return json({error:"Autorize o tratamento dos dados de cadastro."},400);
      await throttle("register:"+(request.headers.get("oai-authenticated-user-id")||request.headers.get("cf-connecting-ip")||"local"));
      const id=crypto.randomUUID(),salt=randomSecret(),now=new Date().toISOString();
      try{await db.prepare("INSERT INTO users (id,alias,email,password_hash,password_salt,consent_at,consent_version,created_at) VALUES (?,?,?,?,?,?,?,?)").bind(id,alias,email,await passwordHash(password,salt),salt,now,CONSENT_VERSION,now).run();}
      catch(error){if(String(error).includes("UNIQUE"))return json({error:"Já existe uma conta com esse e-mail. Entre com sua senha."},409);throw error;}
      return json({user:{id,alias,email}},201,await createSession(id,request));
    }
    if(path==="auth/login"&&method==="POST"){
      const input=JSON.parse(body) as Record<string,unknown>;
      const email=typeof input.email==="string"?input.email.trim().toLowerCase():"";
      const password=typeof input.password==="string"?input.password:"";
      if(email.length>254||password.length>128||!email||!password)return json({error:"E-mail ou senha inválidos."},401);
      await throttle("login:"+email);
      const user=await db.prepare("SELECT id,alias,email,password_hash,password_salt FROM users WHERE email = ?").bind(email).first<{id:string;alias:string;email:string;password_hash:string;password_salt:string}>();
      const verified=await verifyPassword(password,user?.password_salt||"00".repeat(32),user?.password_hash||"00".repeat(32));
      if(!user||!verified)return json({error:"E-mail ou senha inválidos."},401);
      await db.prepare("DELETE FROM attempts WHERE key = ?").bind(await digest("login:"+email)).run();
      return json({user:{id:user.id,alias:user.alias,email:user.email}},200,await createSession(user.id,request));
    }
    const user=await currentUser(request);
    if(!user)return json({error:"Entre na sua conta para continuar."},401);
    if(path==="recommendation/status"&&method==="GET"){
      try{return json(await callEngine("status"));}catch{return json({error:"Motor financeiro indisponível neste ambiente."},503);}
    }
    if(path==="recommendation/market"&&method==="GET"){
      try{return json(await callEngine("market"));}catch{return json({error:"Inventário de fontes em atualização. O planejamento mantém a última base validada."},503);}
    }
    if(path==="recommendation"&&method==="GET"){
      const saved=await db.prepare("SELECT settings,result,updated_at FROM recommendations WHERE user_id = ? AND profile_updated_at = (SELECT updated_at FROM profiles WHERE user_id = ?)").bind(user.id,user.id).first<{settings:string;result:string;updated_at:string}>();
      return json({recommendation:saved?{settings:JSON.parse(saved.settings),result:await decodeRecommendation(saved.result),updatedAt:saved.updated_at}:null});
    }
    if(path==="recommendation"&&method==="POST"){
      const profile=await db.prepare("SELECT answers,status,updated_at FROM profiles WHERE user_id = ?").bind(user.id).first<{answers:string;status:string;updated_at:string}>();
      if(!profile||profile.status!=="complete")return json({error:"Conclua o questionário antes de planejar sua carteira."},409);
      let payload;
      try{payload=engineInput(JSON.parse(profile.answers),JSON.parse(body));}catch(error){return json({error:error instanceof Error?error.message:"Parâmetros inválidos."},400);}
      if(payload.settings.dataMode!=="real"||payload.settings.forecastPolicy!=="xgboost_experimental")return json({error:"O planejamento utiliza somente dados reais e previsão XGBoost."},400);
      await throttle("recommendation:"+user.id);
      let result;
      try{result=await callEngine("recommend",payload.input);}catch(error){return json({error:error instanceof Error?error.message:"Motor indisponível."},503);}
      const now=new Date().toISOString();
      const encodedResult=await encodeRecommendation(result);
      // Commit only while the profile revision and its processing permission remain current.
      const stored=await db.prepare("INSERT INTO recommendations (user_id,profile_updated_at,settings,result,consent_at,consent_version,updated_at) SELECT ?,?,?,?,?,?,? FROM profiles WHERE user_id = ? AND updated_at = ? AND status = 'complete' ON CONFLICT(user_id) DO UPDATE SET profile_updated_at=excluded.profile_updated_at,settings=excluded.settings,result=excluded.result,consent_at=excluded.consent_at,consent_version=excluded.consent_version,updated_at=excluded.updated_at").bind(user.id,profile.updated_at,JSON.stringify(payload.settings),encodedResult,now,"2026-10-09-motor-v1",now,user.id,profile.updated_at).run();
      if(!stored.meta.changes)return json({error:"Seu perfil mudou ou a autorização foi revogada durante o cálculo. Gere um novo planejamento."},409);
      return json({recommendation:{settings:payload.settings,result,updatedAt:now}});
    }
    if(path==="recommendation"&&method==="DELETE"){
      await db.batch([db.prepare("UPDATE profiles SET updated_at = ? WHERE user_id = ?").bind(new Date().toISOString(),user.id),db.prepare("DELETE FROM recommendations WHERE user_id = ?").bind(user.id)]);
      return json({ok:true});
    }
    if(path==="me"&&method==="GET"){
      const profile=await db.prepare("SELECT answers,result,status,step,consent_at,consent_version,updated_at FROM profiles WHERE user_id = ?").bind(user.id).first<{answers:string;result:string|null;status:string;step:number;consent_at:string;consent_version:string;updated_at:string}>();
      return json({user,profile:profile?{...profile,answers:JSON.parse(profile.answers),result:profile.result?JSON.parse(profile.result):null}:null});
    }
    if(path==="auth/logout"&&method==="POST"){
      await db.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(await digest(sessionToken(request))).run();
      return json({ok:true},200,expiredCookie(request));
    }
    if(path==="profile"&&method==="POST"){
      const input=JSON.parse(body) as Record<string,unknown>;
      if(input.consent!==true)return json({error:"Autorize o uso das respostas para calcular seu perfil."},400);
      const complete=input.complete===true;
      const answers=validateAnswers(input.answers,complete);
      const result=complete?classify(answers):null;
      const now=new Date().toISOString();
      const step=Number.isInteger(input.step)&&Number(input.step)>=0&&Number(input.step)<=3?Number(input.step):0;
      await db.batch([db.prepare("DELETE FROM recommendations WHERE user_id = ?").bind(user.id),db.prepare("INSERT INTO profiles (user_id,answers,result,status,step,consent_at,consent_version,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET answers=excluded.answers,result=excluded.result,status=excluded.status,step=excluded.step,consent_at=excluded.consent_at,consent_version=excluded.consent_version,updated_at=excluded.updated_at").bind(user.id,JSON.stringify(answers),result?JSON.stringify(result):null,complete?"complete":"draft",step,now,CONSENT_VERSION,now)]);
      return json({ok:true,result,updatedAt:now});
    }
    if(path==="data"&&method==="GET"){
      const profile=await db.prepare("SELECT answers,result,status,consent_at,consent_version,updated_at FROM profiles WHERE user_id = ?").bind(user.id).first();
      const recommendation=await db.prepare("SELECT settings,result,consent_at,consent_version,updated_at FROM recommendations WHERE user_id = ?").bind(user.id).first();
      return json({account:user,questionnaire:profile?{...profile,answers:JSON.parse(String(profile.answers)),result:profile.result?JSON.parse(String(profile.result)):null}:null,recommendation:recommendation?{...recommendation,settings:JSON.parse(String(recommendation.settings)),result:await decodeRecommendation(String(recommendation.result))}:null});
    }
    if(path==="profile"&&method==="DELETE"){
      await db.batch([db.prepare("DELETE FROM recommendations WHERE user_id = ?").bind(user.id),db.prepare("DELETE FROM profiles WHERE user_id = ?").bind(user.id)]);
      return json({ok:true});
    }
    if(path==="account"&&method==="DELETE"){
      const {password}=JSON.parse(body) as {password:unknown};
      if(typeof password!=="string"||password.length>128)return json({error:"Confirme sua senha para excluir a conta."},400);
      const credentials=await db.prepare("SELECT password_hash,password_salt FROM users WHERE id = ?").bind(user.id).first<{password_hash:string;password_salt:string}>();
      if(!credentials||!await verifyPassword(password,credentials.password_salt,credentials.password_hash))return json({error:"Senha incorreta. Sua conta permanece ativa."},403);
      await db.batch([db.prepare("DELETE FROM recommendations WHERE user_id = ?").bind(user.id),db.prepare("DELETE FROM profiles WHERE user_id = ?").bind(user.id),db.prepare("DELETE FROM sessions WHERE user_id = ?").bind(user.id),db.prepare("DELETE FROM users WHERE id = ?").bind(user.id)]);
      return json({ok:true},200,expiredCookie(request));
    }
    return json({error:"Operação não encontrada."},404);
  }catch(error){
    if(error instanceof RequestBodyTooLargeError)return json({error:"Solicitação muito grande."},413);
    if(error instanceof SyntaxError)return json({error:"Dados inválidos. Confira e tente novamente."},400);
    if(error instanceof Error&&/Responda|Confira|Selecione|idade de aposentadoria|Respostas inválidas|Muitas tentativas/.test(error.message))return json({error:error.message},400);
    console.error("RendaFutura: falha na operação solicitada.");
    return json({error:"Não foi possível acessar seus dados agora. Tente novamente."},503);
  }
}
export const GET=handle;
export const POST=handle;
export const DELETE=handle;
