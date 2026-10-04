import { env } from "cloudflare:workers";
export const CONSENT_VERSION="2026-10-04-v1";
const COOKIE="rf_session";
const hex=(input:ArrayBuffer|Uint8Array)=>Array.from(new Uint8Array(input),b=>b.toString(16).padStart(2,"0")).join("");
const unhex=(input:string)=>new Uint8Array(input.match(/.{2}/g)!.map(n=>parseInt(n,16)));
export function database(){if(!env.DB)throw new Error("Armazenamento indisponível.");return env.DB;}
export async function digest(input:string){return hex(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(input)));}
export async function passwordHash(password:string,salt:string){
  const key=await crypto.subtle.importKey("raw",new TextEncoder().encode(password),"PBKDF2",false,["deriveBits"]);
  return hex(await crypto.subtle.deriveBits({name:"PBKDF2",hash:"SHA-256",salt:unhex(salt),iterations:100000},key,256));
}
export const randomSecret=()=>hex(crypto.getRandomValues(new Uint8Array(32)));
export async function verifyPassword(password:string,salt:string,expected:string){
  const candidate=await passwordHash(password,salt);
  let difference=candidate.length^expected.length;
  for(let i=0;i<candidate.length;i++)difference|=candidate.charCodeAt(i)^(expected.charCodeAt(i)||0);
  return difference===0;
}
export function sessionToken(request:Request){return request.headers.get("cookie")?.split(";").map(s=>s.trim()).find(s=>s.startsWith(COOKIE+"="))?.slice(COOKIE.length+1)||"";}
export async function currentUser(request:Request){
  const token=sessionToken(request);
  if(!/^[a-f0-9]{64}$/.test(token))return null;
  return database().prepare("SELECT users.id, users.alias, users.email, users.consent_at, users.consent_version, users.created_at FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token_hash = ? AND sessions.expires_at > ?").bind(await digest(token),Date.now()).first<{id:string;alias:string;email:string;consent_at:string;consent_version:string;created_at:string}>();
}
export async function createSession(userId:string,request:Request){
  const token=randomSecret();
  await database().batch([database().prepare("DELETE FROM sessions WHERE expires_at <= ?").bind(Date.now()),database().prepare("INSERT INTO sessions (token_hash,user_id,expires_at) VALUES (?,?,?)").bind(await digest(token),userId,Date.now()+7*86400000)]);
  return `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800${new URL(request.url).protocol==="https:"?"; Secure":""}`;
}
export const expiredCookie=(request:Request)=>`${COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0${new URL(request.url).protocol==="https:"?"; Secure":""}`;
export async function throttle(key:string){
  const db=database(),now=Date.now(),hashed=await digest(key);
  await db.prepare("DELETE FROM attempts WHERE expires_at <= ?").bind(now).run();
  await db.prepare("INSERT INTO attempts (key,count,expires_at) VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count = attempts.count + 1").bind(hashed,now+600000).run();
  const entry=await db.prepare("SELECT count FROM attempts WHERE key = ?").bind(hashed).first<{count:number}>();
  if(entry&&entry.count>8)throw new Error("Muitas tentativas. Aguarde dez minutos antes de tentar novamente.");
}
export function sameOrigin(request:Request){const origin=request.headers.get("origin");return origin===new URL(request.url).origin;}
