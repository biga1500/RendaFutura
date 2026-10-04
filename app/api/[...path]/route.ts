import { classify, validateAnswers } from "@/lib/questionnaire";
import { CONSENT_VERSION, createSession, currentUser, database, digest, expiredCookie, passwordHash, randomSecret, sameOrigin, sessionToken, throttle, verifyPassword } from "@/lib/auth";
export const dynamic="force-dynamic";
function json(data:unknown,status=200,cookie?:string){return Response.json(data,{status,headers:{"Cache-Control":"no-store",...(cookie?{"Set-Cookie":cookie}:{})}});}
async function handle(request:Request){
  try {
    const path=new URL(request.url).pathname.replace(/^\/api\//,"");
    const method=request.method;
    if(method!=="GET"&&!sameOrigin(request))return json({error:"Origem da solicitação inválida."},403);
    if(Number(request.headers.get("content-length")||0)>16384)return json({error:"Solicitação muito grande."},413);
    const db=database();
    if(path==="auth/register"&&method==="POST"){
      const input=await request.json() as Record<string,unknown>;
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
      const input=await request.json() as Record<string,unknown>;
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
    if(path==="me"&&method==="GET"){
      const profile=await db.prepare("SELECT answers,result,status,step,consent_at,consent_version,updated_at FROM profiles WHERE user_id = ?").bind(user.id).first<{answers:string;result:string|null;status:string;step:number;consent_at:string;consent_version:string;updated_at:string}>();
      return json({user,profile:profile?{...profile,answers:JSON.parse(profile.answers),result:profile.result?JSON.parse(profile.result):null}:null});
    }
    if(path==="auth/logout"&&method==="POST"){
      await db.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(await digest(sessionToken(request))).run();
      return json({ok:true},200,expiredCookie(request));
    }
    if(path==="profile"&&method==="POST"){
      const input=await request.json() as Record<string,unknown>;
      if(input.consent!==true)return json({error:"Autorize o uso das respostas para calcular seu perfil."},400);
      const complete=input.complete===true;
      const answers=validateAnswers(input.answers,complete);
      const result=complete?classify(answers):null;
      const now=new Date().toISOString();
      const step=Number.isInteger(input.step)&&Number(input.step)>=0&&Number(input.step)<=3?Number(input.step):0;
      await db.prepare("INSERT INTO profiles (user_id,answers,result,status,step,consent_at,consent_version,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET answers=excluded.answers,result=excluded.result,status=excluded.status,step=excluded.step,consent_at=excluded.consent_at,consent_version=excluded.consent_version,updated_at=excluded.updated_at").bind(user.id,JSON.stringify(answers),result?JSON.stringify(result):null,complete?"complete":"draft",step,now,CONSENT_VERSION,now).run();
      return json({ok:true,result,updatedAt:now});
    }
    if(path==="data"&&method==="GET"){
      const profile=await db.prepare("SELECT answers,result,status,consent_at,consent_version,updated_at FROM profiles WHERE user_id = ?").bind(user.id).first();
      return json({account:user,questionnaire:profile?{...profile,answers:JSON.parse(String(profile.answers)),result:profile.result?JSON.parse(String(profile.result)):null}:null});
    }
    if(path==="profile"&&method==="DELETE"){
      await db.prepare("DELETE FROM profiles WHERE user_id = ?").bind(user.id).run();
      return json({ok:true});
    }
    if(path==="account"&&method==="DELETE"){
      const {password}=await request.json() as {password:unknown};
      if(typeof password!=="string"||password.length>128)return json({error:"Confirme sua senha para excluir a conta."},400);
      const credentials=await db.prepare("SELECT password_hash,password_salt FROM users WHERE id = ?").bind(user.id).first<{password_hash:string;password_salt:string}>();
      if(!credentials||!await verifyPassword(password,credentials.password_salt,credentials.password_hash))return json({error:"Senha incorreta. Sua conta permanece ativa."},403);
      await db.batch([db.prepare("DELETE FROM profiles WHERE user_id = ?").bind(user.id),db.prepare("DELETE FROM sessions WHERE user_id = ?").bind(user.id),db.prepare("DELETE FROM users WHERE id = ?").bind(user.id)]);
      return json({ok:true},200,expiredCookie(request));
    }
    return json({error:"Operação não encontrada."},404);
  }catch(error){
    if(error instanceof SyntaxError)return json({error:"Dados inválidos. Confira e tente novamente."},400);
    if(error instanceof Error&&/Responda|Confira|Selecione|idade de aposentadoria|Respostas inválidas|Muitas tentativas/.test(error.message))return json({error:error.message},400);
    console.error("RendaFutura: falha na operação solicitada.");
    return json({error:"Não foi possível acessar seus dados agora. Tente novamente."},503);
  }
}
export const GET=handle;
export const POST=handle;
export const DELETE=handle;
