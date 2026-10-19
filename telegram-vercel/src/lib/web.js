// Web admin panel: /panel (single page) + /api/panel/* (JSON). Access is a signed link the bot sends to an admin (/web);
// /web off rotates the signing version so every old link stops working.
import crypto from "node:crypto";
import {pipe,cmd,obj} from "./redis.js";
import {tg,send,sleep} from "./tg.js";
import {P,monthKey,getCfg,isAdmin,PLANS} from "./quota.js";
import {decideRequest} from "./pay.js";
import {PANEL_HTML} from "./panel_html.js";

export const PANEL_BASE="https://bmediahub-telegram.vercel.app";
const KEY=()=>process.env.TG_WEBHOOK_SECRET||process.env.MEDIA_WORKER_SECRET||"";
const sign=(s,v)=>crypto.createHmac("sha256",KEY()+":panel:"+v).update(s).digest("base64url");
const dstr=t=>new Date(t).toLocaleDateString("en-CA",{timeZone:"Africa/Cairo"});
const n=v=>Number(v)||0;

export async function webLink(uid,days=30){
  const v=(await cmd("HGET",P+"cfg","webv"))||"0",body=uid+"."+(Date.now()+days*864e5);
  return PANEL_BASE+"/panel#t="+body+"."+sign(body,v);
}
export async function webRevoke(){await cmd("HINCRBY",P+"cfg","webv","1")}
async function auth(req){
  if(!KEY())return null;
  const t=String(req.headers.authorization||"").replace(/^Bearer\s+/i,""),m=t.match(/^(\d{5,15})\.(\d{13})\.([\w-]{43})$/);
  if(!m||Number(m[2])<Date.now()||!isAdmin(m[1]))return null;
  const v=(await cmd("HGET",P+"cfg","webv"))||"0",good=Buffer.from(sign(m[1]+"."+m[2],v)),got=Buffer.from(m[3]);
  return good.length===got.length&&crypto.timingSafeEqual(good,got)?m[1]:null;
}

// ---------- data ----------
async function allUsers(){
  const ids=(await cmd("SMEMBERS",P+"users"))||[],out=[];
  for(let i=0;i<ids.length;i+=1000){
    const part=ids.slice(i,i+1000),rows=await pipe(part.map(u=>["HMGET",P+"u:"+u,"reg","cdate","name","uname","plan","pexp","ban","src","unl","nowm"]));
    part.forEach((u,j)=>{const r=rows[j]||[];out.push({id:u,reg:n(r[0]),cdate:r[1]||"",name:r[2]||"",uname:r[3]||"",pro:r[4]==="pro"&&n(r[5])>Date.now(),pexp:n(r[5]),ban:r[6]==="1",src:r[7]||"",unl:r[8]==="1",nowm:r[9]==="1"})});
  }
  return out;
}
async function overview(){
  const now=Date.now(),days=[...Array(14)].map((_,i)=>dstr(now-(13-i)*864e5)),m=monthKey();
  const r=await pipe([...days.flatMap(d=>[["SCARD",P+"act:"+d],["GET",P+"dl:"+d],["GET",P+"rl:"+d]]),["GET",P+"dlm:"+m],["GET",P+"rlm:"+m],["HGETALL",P+"paystats"],["HGET",P+"rev",m],["LRANGE",P+"payq","0","199"]]);
  const k=days.length*3,users=await allUsers(),newBy={};users.forEach(u=>{if(u.cdate)newBy[u.cdate]=(newBy[u.cdate]||0)+1});
  const q=r[k+4]||[],st=q.length?await pipe(q.map(id=>["HGET",P+"payreq:"+id,"status"])):[];
  const c=await getCfg();
  return {days:days.map((d,i)=>({d,active:n(r[i*3]),dl:n(r[i*3+1]),reels:n(r[i*3+2]),new:newBy[d]||0})),
    users:users.length,pro:users.filter(u=>u.pro).length,banned:users.filter(u=>u.ban).length,
    month:{dl:n(r[k]),reels:n(r[k+1]),revenue:n(r[k+3])},pay:{...obj(r[k+2]),pending:st.filter(x=>x==="pending").length},
    cfg:{maint:c.maint,launch:c.launch,freecap:c.freecap,price:c.price,paynum:c.paynum}};
}
async function payments(){
  const ids=(await cmd("LRANGE",P+"payq","0","99"))||[];if(!ids.length)return [];
  const rows=await pipe(ids.map(i=>["HMGET",P+"payreq:"+i,"status","user","phone","name","uname","ts","at","exp","amount"]));
  return ids.map((id,i)=>{const x=rows[i]||[];return {id,status:x[0]||"gone",user:x[1],phone:x[2],name:x[3],uname:x[4],ts:n(x[5]),at:n(x[6]),exp:n(x[7]),amount:x[8]||""}}).filter(x=>x.status!=="gone");
}
async function sources(){
  const [a,b]=await pipe([["HGETALL",P+"src"],["HGETALL",P+"srcpaid"]]);const s=obj(a),p=obj(b);
  return [...new Set([...Object.keys(s),...Object.keys(p)])].map(k=>({src:k,visits:n(s[k]),paid:n(p[k])})).sort((x,y)=>y.paid-x.paid||y.visits-x.visits);
}
async function health(){
  const t0=Date.now();let redisMs=-1,worker=null,hook=null;
  try{await cmd("PING");redisMs=Date.now()-t0}catch{}
  try{const W=process.env.MEDIA_WORKER_URL;const t=Date.now(),r=await fetch(W.replace(/\/$/,"")+"/",{signal:AbortSignal.timeout(6000)});worker={ok:r.ok,ms:Date.now()-t}}catch{worker={ok:false}}
  try{const w=await tg("getWebhookInfo");hook={pending:w.pending_update_count||0,error:w.last_error_message||"",errorAt:(w.last_error_date||0)*1000}}catch{}
  return {redisMs,worker,hook,redisHost:(process.env.UPSTASH_REDIS_REST_URL||process.env.KV_REST_API_URL||"").replace(/^https?:\/\//,"").split("/")[0]};
}
const SETTINGS={
  paynum:v=>{v=String(v).replace(/\D/g,"");if(!/^01[0125]\d{8}$/.test(v))throw Error("رقم فودافون كاش غير صحيح");return v},
  payinsta:v=>{v=String(v).trim();if(v&&!/^[\w.@-]{3,60}$/.test(v))throw Error("InstaPay غير صحيح");return v},
  price:v=>{v=Number(v);if(!(Number.isInteger(v)&&v>0&&v<100000))throw Error("سعر غير صحيح");return String(v)},
  freecap:v=>{v=Number(v);if(!(Number.isInteger(v)&&v>=0&&v<=100000))throw Error("رقم غير صحيح");return String(v)},
  daily:v=>{v=Number(v);if(!(Number.isInteger(v)&&v>=0&&v<=1000))throw Error("رقم غير صحيح");return String(v)},
  welcome:v=>{v=Number(v);if(!(Number.isInteger(v)&&v>=0&&v<=1000))throw Error("رقم غير صحيح");return String(v)},
  bonus:v=>{v=Number(v);if(!(Number.isInteger(v)&&v>=0&&v<=1000))throw Error("رقم غير صحيح");return String(v)},
  maint:v=>v?"1":"0",
  contact:v=>{v=String(v).trim();if(v&&!/^(@[A-Za-z][A-Za-z0-9_]{3,31}|https:\/\/t\.me\/(\+[A-Za-z0-9_-]{6,64}|[A-Za-z][A-Za-z0-9_]{3,31}))$/.test(v))throw Error("حساب التواصل لازم @username أو رابط t.me");return v},
  launchDays:v=>{v=Number(v);if(!(Number.isInteger(v)&&v>=0&&v<=60))throw Error("الأيام من 0 لـ 60");return v?String(Date.now()+v*864e5):""},
};
async function setSettings(b){
  const set=[],del=[];
  for(const [k,f] of Object.entries(SETTINGS))if(b[k]!==undefined){const v=f(b[k]),field=k==="launchDays"?"launch":k;if(v==="")del.push(field);else set.push(field,v)}
  const c=[];if(set.length)c.push(["HSET",P+"cfg",...set]);if(del.length)c.push(["HDEL",P+"cfg",...del]);if(c.length)await pipe(c);
  const x=await getCfg();return {cfg:x};
}
async function userAction(b,admin){
  const u=String(b.uid||"");if(!/^\d{5,15}$/.test(u))throw Error("ID غير صحيح");
  const cur=obj(await cmd("HGETALL",P+"u:"+u));if(!cur.reg&&!cur.terms)throw Error("المستخدم ده مش مسجّل");
  switch(b.act){
  case "pro":{const d=Number(b.days||30);if(!(d>0&&d<=366))throw Error("الأيام من 1 لـ 366");
    const base=Math.max(Date.now(),cur.plan==="pro"?n(cur.pexp):0),exp=base+d*864e5;await cmd("HSET",P+"u:"+u,"plan","pro","pexp",String(exp));
    await send(Number(u),"🎉 تم تفعيل "+PLANS.pro.name+" لحد "+dstr(exp)+". ابدأ من /reel").catch(()=>{});return {exp}}
  case "unpro":await cmd("HDEL",P+"u:"+u,"plan","pexp");return {};
  case "ban":await cmd("HSET",P+"u:"+u,"ban","1");return {};
  case "unban":await cmd("HSET",P+"u:"+u,"ban","0");return {};
  case "nowm":await cmd("HSET",P+"u:"+u,"nowm",cur.nowm==="1"?"0":"1");return {};
  case "unl":await cmd("HSET",P+"u:"+u,"unl",cur.unl==="1"?"0":"1");return {};
  case "grant":{const k=Number(b.n);if(!(Number.isInteger(k)&&k>0&&k<=1000))throw Error("عدد غير صحيح");await cmd("HINCRBY",P+"u:"+u,"wel",String(k*3));return {}}
  case "msg":{const t=String(b.text||"").trim().slice(0,3500);if(!t)throw Error("الرسالة فاضية");await send(Number(u),t);return {}}
  }
  throw Error("إجراء غير معروف");
}
async function broadcast(b){
  const text=String(b.text||"").trim().slice(0,3500);if(!text)throw Error("الرسالة فاضية");
  const ids=(await cmd("SMEMBERS",P+"users"))||[],from=Math.max(0,Number(b.from)||0),end=Date.now()+45000;let ok=0,fail=0,i=from;
  for(;i<ids.length&&Date.now()<end;i++){try{await send(ids[i],text);ok++}catch{fail++}await sleep(40)}
  return {ok,fail,next:i<ids.length?i:0,total:ids.length};
}

function out(res,x,s=200){res.writeHead(s,{"content-type":"application/json;charset=utf-8","cache-control":"no-store"});return res.end(JSON.stringify(x))}
export async function panelRoute(req,res,p){
  if(p==="/panel"||p==="/panel/"){
    res.writeHead(200,{"content-type":"text/html;charset=utf-8","cache-control":"no-store","x-frame-options":"DENY","referrer-policy":"no-referrer","x-robots-tag":"noindex"});
    return res.end(PANEL_HTML),true;
  }
  if(!p.startsWith("/api/panel/"))return false;
  const admin=await auth(req).catch(()=>null);
  if(!admin)return out(res,{ok:false,error:"auth"},401),true;
  const op=p.slice("/api/panel/".length),b=typeof req.body==="string"?JSON.parse(req.body||"{}"):(req.body||{});
  try{
    if(op==="photo"){
      const rid=new URL(req.url,"http://x").searchParams.get("rid")||"";if(!/^[a-z0-9]{6,20}$/.test(rid))return out(res,{ok:false},400),true;
      const fid=await cmd("HGET",P+"payreq:"+rid,"photo");if(!fid)return out(res,{ok:false},404),true;
      const f=await tg("getFile",{file_id:fid}),r=await fetch("https://api.telegram.org/file/bot"+process.env.TG_BOT_TOKEN+"/"+f.file_path,{signal:AbortSignal.timeout(15000)});
      res.writeHead(200,{"content-type":r.headers.get("content-type")||"image/jpeg","cache-control":"private, max-age=3600"});
      return res.end(Buffer.from(await r.arrayBuffer())),true;
    }
    let data;
    switch(op){
    case "overview":data=await overview();break;
    case "payments":data=await payments();break;
    case "decide":data=await decideRequest(String(b.rid||""),b.act==="a"?"a":"r",admin);break;
    case "users":data=(await allUsers()).sort((x,y)=>y.reg-x.reg);break;
    case "user":data=await userAction(b,admin);break;
    case "sources":data=await sources();break;
    case "settings":data=b.set?await setSettings(b.set):{cfg:await getCfg()};break;
    case "health":data=await health();break;
    case "broadcast":data=await broadcast(b);break;
    default:return out(res,{ok:false,error:"not_found"},404),true;
    }
    return out(res,{ok:true,data}),true;
  }catch(e){console.error("panel",op,e.message);return out(res,{ok:false,error:e.message.slice(0,200)},400),true}
}
