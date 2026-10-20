// Landing-page analytics: per ad code, per Cairo day — visits, time on page, how far people scroll, button clicks,
// and (filled in by the bot) bot starts and paid subscriptions. One Redis hash per day: field "<src>|<event>".
import {pipe,obj} from "./redis.js";
import {P} from "./quota.js";

export const LP_EVENTS=["view","t5","t15","t30","t60","sec:how","sec:proof","sec:price","sec:faq","sec:final","faq","click","click_pro"];
const OK=new Set(LP_EVENTS),TTL=String(100*86400);
const day=(t=Date.now())=>new Date(t).toLocaleDateString("en-CA",{timeZone:"Africa/Cairo"});
const cleanSrc=s=>{s=String(s||"").toLowerCase();return /^[a-z0-9_]{1,32}$/.test(s)?s:"lp"};

export async function trackLp(b){
  const src=cleanSrc(b.src),evs=[...new Set((Array.isArray(b.e)?b.e:[]).map(String))].filter(e=>OK.has(e)).slice(0,20);
  if(!evs.length)return {ok:false};
  const k=P+"lpa:"+day();
  await pipe([...evs.map(e=>["HINCRBY",k,src+"|"+e,"1"]),["EXPIRE",k,TTL]]);
  return {ok:true};
}
// called by the bot: a landing visitor started the bot / paid
export async function countLp(src,ev){const k=P+"lpa:"+day();await pipe([["HINCRBY",k,cleanSrc(src)+"|"+ev,"1"],["EXPIRE",k,TTL]])}

export async function lpStats(days){
  days=Math.max(1,Math.min(90,Number(days)||7));
  const ds=[];for(let i=0;i<days;i++)ds.push(day(Date.now()-i*864e5));
  const raw=await pipe(ds.map(d=>["HGETALL",P+"lpa:"+d]));
  const by={},daily=ds.map((d,i)=>{const h=obj(raw[i]);let v=0;
    for(const [f,n] of Object.entries(h)){const j=f.lastIndexOf("|"),s=f.slice(0,j),e=f.slice(j+1);(by[s]??={})[e]=(by[s][e]||0)+Number(n);if(e==="view")v+=Number(n)}
    return {day:d,views:v}}).reverse();
  const rows=Object.entries(by).map(([src,e])=>({src,...e})).sort((a,b)=>(b.view||0)-(a.view||0));
  return {days,rows,daily};
}
