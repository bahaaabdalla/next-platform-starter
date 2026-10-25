import {pipe,cmd,obj} from "./redis.js";
import {tg} from "./tg.js";
export const VIDEO=3,P="bm:";
export const today=()=>new Date().toLocaleDateString("en-CA",{timeZone:"Africa/Cairo"});
export const monthKey=()=>today().slice(0,7);
const num=(v,d)=>{if(v===undefined||v===null||v==="")return d;const n=Number(v);return Number.isFinite(n)?n:d};
export const ADMINS=()=>(process.env.ADMIN_USER_IDS||"5375428860").split(",").map(x=>x.trim()).filter(Boolean);
export const isAdmin=id=>ADMINS().includes(String(id));

export async function getCfg(){
  return parseCfg(obj(await cmd("HGETALL",P+"cfg")));
}
const parseCfg=o=>({tutvid:o.tutvid||"",main:o.main||"",maint:o.maint==="1",daily:num(o.daily,2),welcome:num(o.welcome,5),bonus:num(o.bonus,5),contact:o.contact||"",launch:num(o.launch,0),freecap:num(o.freecap,0),paynum:o.paynum||"",payinsta:o.payinsta||"",price:o.price||"50",
  // reel plans (editable from the web panel)
  fDaily:num(o.f_daily,1),fCap:num(o.f_cap,60),fHd:o.f_hd==="1",pDaily:num(o.p_daily,10),pCap:num(o.p_cap,180),packOn:o.pack!=="0",packMax:num(o.pack_max,5),chanDays:num(o.chan_days,-1)});
// Config + user state in ONE Redis round trip (the gate runs on every message and tap, so it must be fast).
export async function load(id){
  const d=today();
  const [cfgRaw,u,used,bonus]=await pipe([["HGETALL",P+"cfg"],["HGETALL",P+"u:"+id],["GET",P+"use:"+id+":"+d],["GET",P+"bonus:"+id+":"+d]]);
  const c=parseCfg(obj(cfgRaw));
  return {c,a:buildAccount(id,c,u,used,bonus)};
}
// Everything the gate needs about one user, in a single round trip.
export async function account(id,c){
  const d=today();
  const [u,used,bonus]=await pipe([["HGETALL",P+"u:"+id],["GET",P+"use:"+id+":"+d],["GET",P+"bonus:"+id+":"+d]]);
  return buildAccount(id,c,u,used,bonus);
}
function buildAccount(id,c,u,used,bonus){
  const d=today();
  const user=obj(u),usedToday=num(used,0),bonusToday=num(bonus,0);
  const wel=num(user.wel,0);
  const perDay=num(user.lim,c.daily)*VIDEO;
  const allow=(user.cdate&&user.cdate<d)?perDay:0;       // daily allowance starts the Cairo day after registration
  const dailyLeft=allow+bonusToday-usedToday;
  const remaining=wel+Math.max(0,dailyLeft);
  return {user,wel,usedToday,bonusToday,allow,remaining,banned:user.ban==="1",terms:user.terms==="1",unlimited:isAdmin(id)||user.unl==="1"};
}
export const downloads=u=>Math.floor(u/VIDEO);
export function describe(a){
  if(a.unlimited)return "♾ رصيدك غير محدود.";
  const dl=downloads(a.remaining),ph=a.remaining;
  return `🎟 رصيدك: ${dl} تحميل فيديو (أو ${ph} عملية صور)\n🎁 هدية البداية المتبقية: ${downloads(a.wel)} تحميل\n📅 النهاردة استهلكت: ${(a.usedToday/VIDEO).toFixed(1).replace(/\.0$/,"")} من ${(a.allow+a.bonusToday)/VIDEO} تحميل يومي`;
}
// Accept terms (and register on first time: welcome gift + start date). Two round trips, idempotent.
export async function acceptTerms(id,from,c){
  const name=[from?.first_name,from?.last_name].filter(Boolean).join(" ").slice(0,60);
  const ex=await cmd("HGET",P+"u:"+id,"reg");
  if(ex){await pipe([["HSET",P+"u:"+id,"terms","1"],["SADD",P+"users",String(id)]]);return false}
  await pipe([["HSET",P+"u:"+id,"reg",String(Date.now()),"cdate",today(),"wel",String(c.welcome*VIDEO),"terms","1","name",name,"uname",from?.username||""],["SADD",P+"users",String(id)]]);
  return true;
}
// Take credit: the welcome pool first, then today's allowance. Never blocks (balance may dip slightly negative).
export async function spend(id,units){
  const w=num(await cmd("HGET",P+"u:"+id,"wel"),0),take=Math.min(Math.max(w,0),units),rest=units-take,cmds=[];
  if(take>0)cmds.push(["HINCRBY",P+"u:"+id,"wel",String(-take)]);
  if(rest>0){const k=P+"use:"+id+":"+today();cmds.push(["INCRBY",k,String(rest)],["EXPIRE",k,"259200"])}
  if(cmds.length)await pipe(cmds);
}
export async function member(chan,uid){
  const key=P+"mem:"+chan.toLowerCase()+":"+uid;
  try{if(await cmd("GET",key)==="1")return true}catch{}
  try{
    const r=await tg("getChatMember",{chat_id:chan,user_id:Number(uid)});
    const ok=["creator","administrator","member"].includes(r.status)||(r.status==="restricted"&&r.is_member===true);
    if(ok)await cmd("SET",key,"1","EX","600");
    return ok;
  }catch(e){console.error("member check",chan,e.message);return null}
}
export const chanLink=c=>"https://t.me/"+c.replace(/^@/,"");
export async function bonusChannels(id,c){
  const [all,claimed]=await pipe([["SMEMBERS",P+"chans"],["SMEMBERS",P+"chs:"+id]]);
  return (all||[]).filter(x=>x.toLowerCase()!==c.main.toLowerCase()&&!(claimed||[]).map(y=>y.toLowerCase()).includes(x.toLowerCase()));
}
// Returns {ok,units,channel} or {ok:false,reason}
export async function claimBonus(id,c){
  const d=today(),k=P+"bonus:"+id+":"+d;
  if(num(await cmd("GET",k),0)>0)return {ok:false,reason:"today"};
  const free=await bonusChannels(id,c);
  if(!free.length)return {ok:false,reason:"none"};
  for(const ch of free){
    if(await member(ch,id)===true){
      await pipe([["SADD",P+"chs:"+id,ch],["INCRBY",k,String(c.bonus*VIDEO)],["EXPIRE",k,"259200"]]);
      return {ok:true,units:c.bonus,channel:ch};
    }
  }
  return {ok:false,reason:"nosub",free};
}
// ---------- reel studio plans ----------
// One paid plan, activated by the admin (/plan ID pro [days]) after a manual payment; it expires on its own.
// Users still on the old 50/100 keys are read as "pro" with the same expiry.
export const PLANS={"0":{name:"مجاني",daily:1,cap:60,wm:true},"pro":{name:"الباقة المميزة",daily:10,cap:180,wm:false}};
export const planKey=k=>{k=String(k||"0").toLowerCase();return k==="50"||k==="100"?"pro":PLANS[k]?k:""};
// cfg (optional) carries the launch window: free reels have no watermark until cfg.launch.
export function reelPlan(id,user,cfg){
  let k=planKey(user.plan)||"0";if(k!=="0"&&!(Number(user.pexp)>Date.now()))k="0";
  const p={...PLANS[k],key:k,exp:k!=="0"?Number(user.pexp):0};
  if(user.nowm==="1")p.wm=false;
  if(k==="0"&&cfg&&cfg.launch>Date.now()){p.wm=false;p.launch=cfg.launch}
  if(cfg){p.daily=k==="0"?cfg.fDaily:cfg.pDaily;p.cap=k==="0"?cfg.fCap:cfg.pCap}
  if(Number(user.rlim)>0)p.daily=Number(user.rlim);   // per-user override set from the web panel
  p.hd=k!=="0"||!!(cfg&&cfg.fHd);
  if(isAdmin(id)){p.daily=1e9;p.cap=Math.max(180,p.cap);p.hd=true}
  return p;
}
export async function reelState(id){
  const d=today();
  const [u,used,cfgRaw,fu]=await pipe([["HGETALL",P+"u:"+id],["GET",P+"reel:"+id+":"+d],["HGETALL",P+"cfg"],["GET",P+"rlf:"+d]]);
  const user=obj(u),c=parseCfg(obj(cfgRaw)),plan=reelPlan(id,user,c);
  // free reels share a global daily cap (0 = no cap) so a free rush never slows the paid queue down
  const freeFull=plan.key==="0"&&!isAdmin(id)&&c.freecap>0&&num(fu,0)>=c.freecap;
  plan.freeFull=freeFull;
  return {user,plan,used:num(used,0),freeFull};
}
// Worker usage report: charge once per video job, once per photo action; always count bandwidth.
export async function usage({user,job,kind,bytes}){
  const d=today(),m=monthKey(),cmds=[];let charged=0;
  const n=Math.max(0,Math.floor(Number(bytes)||0));
  let newBw=null;
  if(n>0){const r=await pipe([["INCRBY",P+"bw:"+m,String(n)],["EXPIRE",P+"bw:"+m,"5184000"]]);newBw=r[0]}
  if(kind==="reel"&&user&&job){
    if((await cmd("SET",P+"chg:"+job,"1","NX","EX","172800"))==="OK"){
      const k=P+"reel:"+user+":"+d;
      const free=reelPlan(user,obj(await cmd("HGETALL",P+"u:"+user))).key==="0"&&!isAdmin(user);
      await pipe([["INCR",k],["EXPIRE",k,"259200"],["INCR",P+"rl:"+d],["EXPIRE",P+"rl:"+d,"259200"],["INCR",P+"rlm:"+m],["EXPIRE",P+"rlm:"+m,"5184000"],["SADD",P+"act:"+d,String(user)],["EXPIRE",P+"act:"+d,"259200"],
        ...(free?[["INCR",P+"rlf:"+d],["EXPIRE",P+"rlf:"+d,"259200"]]:[])]);
      charged=1;
    }
    return {charged,bandwidth:newBw};
  }
  if(kind==="video"||kind==="photo"){
    let count=true;
    if(kind==="video"&&job){count=(await cmd("SET",P+"chg:"+job,"1","NX","EX","172800"))==="OK"}
    if(count&&user){
      charged=kind==="video"?VIDEO:1;
      const a=await account(user,await getCfg());
      if(!a.unlimited)await spend(user,charged);
      cmds.push(["SADD",P+"act:"+d,String(user)],["EXPIRE",P+"act:"+d,"259200"],["INCR",P+"dl:"+d],["EXPIRE",P+"dl:"+d,"259200"],["INCR",P+"dlm:"+m],["EXPIRE",P+"dlm:"+m,"5184000"]);
      await pipe(cmds);
    }
  }
  return {charged,bandwidth:newBw};
}

// Plans text built from the live settings (price, limits) so the bot never shows stale numbers.
export const mins=s=>s%60?(s<60?s+" ثانية":Math.floor(s/60)+" دقيقة و"+(s%60)+" ثانية"):(s/60===1?"دقيقة":s/60===2?"دقيقتين":s/60+" دقايق");
export function plansText(c){
  return "🎬 باقات صانع الريلز\n\n🆓 المجاني: "+(c.fDaily===1?"ريل واحد":c.fDaily+" ريلز")+" يوميًا لحد "+mins(c.fCap)+(c.fHd?" بجودة 1080":"")+".\n💎 الباقة المميزة: "+c.price+" جنيه في الشهر. "+c.pDaily+" ريلز يوميًا، لحد "+mins(c.pCap)+"، بجودة 1080، ومن غير علامة مائية"+(c.packOn?"، وزرار «ريلز جاهزة» ("+(c.packMax>=5?"3 أو 5":"3")+" ريلز بضغطة واحدة)":"")+"، وأولوية في الطابور.";
}
