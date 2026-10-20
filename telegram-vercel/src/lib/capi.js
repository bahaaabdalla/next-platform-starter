// Meta Conversions API: the landing page hands the visitor's click ids (fbc/fbp) to the bot through the
// /start code, and the bot reports what Meta can't see inside Telegram: Lead (first /start) and Purchase (approved payment).
import crypto from "node:crypto";
import {cmd,pipe,obj} from "./redis.js";
import {P} from "./quota.js";

const PIXEL=process.env.META_PIXEL_ID||"1093807586621158";
const TOKEN=process.env.META_CAPI_TOKEN||"";
const SITE=process.env.LP_URL||"https://bmediahub-telegram.vercel.app/";
const sha=v=>crypto.createHash("sha256").update(String(v).trim().toLowerCase()).digest("hex");
const ID=/^[a-z0-9]{10}$/;
export const capiOn=()=>!!TOKEN;

// landing page → save the click context under a short id (7 days), referenced later by /start <src>_l<id>
export async function saveVisit(b,req){
  const id=String(b.id||"");if(!ID.test(id))return {ok:false};
  const clean=(v,n)=>String(v||"").replace(/[^\w.\-:=/]/g,"").slice(0,n);
  const ip=String(req.headers["x-forwarded-for"]||"").split(",")[0].trim().slice(0,64);
  const f=["fbc",clean(b.fbc,200),"fbp",clean(b.fbp,120),"src",clean(b.src,32),"ip",ip,"ua",String(req.headers["user-agent"]||"").slice(0,400),"url",clean(b.url,300),"ts",String(Date.now())];
  await pipe([["HSET",P+"lp:"+id,...f],["EXPIRE",P+"lp:"+id,"604800"]]);
  return {ok:true};
}

async function post(events){
  if(!TOKEN)return;
  try{
    const r=await fetch("https://graph.facebook.com/v21.0/"+PIXEL+"/events?access_token="+encodeURIComponent(TOKEN),
      {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({data:events}),signal:AbortSignal.timeout(4000)});
    if(!r.ok)console.error("capi",r.status,(await r.text()).slice(0,300));
  }catch(e){console.error("capi",e.message)}
}
function userData(u,user,extra={}){
  const d={external_id:[sha("tg"+user)]};
  if(u.fbc)d.fbc=u.fbc;if(u.fbp)d.fbp=u.fbp;
  if(u.ip)d.client_ip_address=u.ip;if(u.ua)d.client_user_agent=u.ua;
  if(u.fn)d.fn=[sha(u.fn)];
  return {...d,...extra};
}

// /start <src>_l<id>: link the landing visit to the Telegram user (once) and send Lead with the browser's event id (dedup)
export async function linkVisit(user,lpid,from){
  if(!ID.test(lpid||""))return;
  const v=obj(await cmd("HGETALL",P+"lp:"+lpid));if(!v.ts)return;
  const first=await cmd("HSETNX",P+"u:"+user,"lpid",lpid);
  if(first!==1&&first!=="1")return;   // already linked: one Lead per user
  const keep=["fbc","fbp","ip","ua"].flatMap(k=>v[k]?["m_"+k,v[k]]:[]);
  if(keep.length)await cmd("HSET",P+"u:"+user,...keep);
  const fn=String(from?.first_name||"").replace(/[^\p{L}]/gu,"");
  await post([{event_name:"Lead",event_time:Math.floor(Date.now()/1000),event_id:"lead_"+lpid,action_source:"website",
    event_source_url:v.url||SITE,user_data:userData({...v,fn},user),custom_data:{content_name:v.src||"lp"}}]);
}

// approved payment → Purchase (value in EGP); the sender phone from the request improves matching
export async function purchase(user,amount,phone,rid){
  if(!TOKEN)return;
  const u=obj(await cmd("HGETALL",P+"u:"+user));
  const v={fbc:u.m_fbc,fbp:u.m_fbp,ip:u.m_ip,ua:u.m_ua};
  let ph=String(phone||"").replace(/\D/g,"");if(/^01\d{9}$/.test(ph))ph="2"+ph;
  await post([{event_name:"Purchase",event_time:Math.floor(Date.now()/1000),event_id:"buy_"+rid,action_source:"website",
    event_source_url:SITE,user_data:userData(v,user,ph?{ph:[sha(ph)]}:{}),custom_data:{currency:"EGP",value:Number(amount)||0,content_name:"pro_monthly"}}]);
}
