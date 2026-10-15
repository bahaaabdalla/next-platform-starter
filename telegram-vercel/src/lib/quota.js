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
const parseCfg=o=>({main:o.main||"",maint:o.maint==="1",daily:num(o.daily,2),welcome:num(o.welcome,5),bonus:num(o.bonus,5)});
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
// Worker usage report: charge once per video job, once per photo action; always count bandwidth.
export async function usage({user,job,kind,bytes}){
  const d=today(),m=monthKey(),cmds=[];let charged=0;
  const n=Math.max(0,Math.floor(Number(bytes)||0));
  let newBw=null;
  if(n>0){const r=await pipe([["INCRBY",P+"bw:"+m,String(n)],["EXPIRE",P+"bw:"+m,"5184000"]]);newBw=r[0]}
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
