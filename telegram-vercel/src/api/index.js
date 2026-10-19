import {tg,send,sleep} from "../lib/tg.js";
import {redisOn,cmd,pipe} from "../lib/redis.js";
import {P,isAdmin,ADMINS,getCfg,account,load,describe,acceptTerms,member,chanLink,bonusChannels,claimBonus,usage,monthKey,reelState} from "../lib/quota.js";
import {planLine} from "../lib/admin.js";
import {initReel,reelStart,reelCallback,reelMessage,isReelCallback,contactLine} from "../lib/reel.js";
import {adminCommand,adminCallback} from "../lib/admin.js";
import {TERMS,HOWTO,ONBOARD_HEAD,PLANS_TEXT} from "../lib/texts.js";
import {payStart,payMessage,payDecision} from "../lib/pay.js";
import {panelRoute} from "../lib/web.js";

const SECRET=process.env.TG_WEBHOOK_SECRET,WORKER=process.env.MEDIA_WORKER_URL,WKEY=process.env.MEDIA_WORKER_SECRET;
const ALLOWED=(process.env.ALLOWED_USER_IDS||"").split(",").map(x=>x.trim()).filter(Boolean);const allowed=id=>!ALLOWED.length||ALLOWED.includes(String(id));
const WH=process.env.TG_PUBLIC_WEBHOOK_URL||"https://bmediahub-telegram.vercel.app/api/webhook";
const menu={inline_keyboard:[[{text:"🎬 صانع الريلز",callback_data:"reel:new"}],[{text:"📥 فيديو",callback_data:"tool:video"},{text:"🎵 صوت MP3",callback_data:"tool:audio"}],[{text:"🖼 الغلاف",callback_data:"tool:cover"},{text:"📦 ضغط",callback_data:"tool:compress"}],[{text:"🎞 GIF",callback_data:"tool:gif"},{text:"🔁 WebM",callback_data:"tool:webm"}],[{text:"🖼 صور",callback_data:"tool:photos"},{text:"📝 الوصف",callback_data:"tool:caption"}],[{text:"⚡ تنشيط",callback_data:"tool:wake"},{text:"ℹ️ الحالة",callback_data:"tool:status"}]]};
const TOOL_AR={video:"📥 تحميل فيديو",audio:"🎵 تحويل لصوت MP3",cover:"🖼 غلاف الفيديو",compress:"📦 ضغط الفيديو",gif:"🎞 تحويل GIF",webm:"🔁 تحويل WebM"};
const MODES=["chat","link","cover","audio","gif","compress","webm","photos","photo","caption"],FREE_MODES=["cover","caption"];
function ret(res,x,s=200){res.writeHead(s,{"content-type":"application/json;charset=utf-8","cache-control":"no-store"});return res.end(JSON.stringify(x))}
function getPlatform(s){try{const u=new URL(s);if(u.protocol!=="https:")return "";const h=u.hostname.replace(/^www\./,"").toLowerCase();if(h==="youtu.be"||h==="youtube.com"||h.endsWith(".youtube.com"))return "YouTube";if(h==="tiktok.com"||h.endsWith(".tiktok.com"))return "TikTok";if(h==="instagram.com"||h.endsWith(".instagram.com"))return "Instagram";if(h==="facebook.com"||h.endsWith(".facebook.com")||h==="fb.watch")return "Facebook";}catch{}return ""}
async function wk(path,data){if(!WORKER||!WKEY)return {error:"not_configured"};try{const r=await fetch(WORKER.replace(/\/$/,"")+path,{method:"POST",headers:{"content-type":"application/json","x-worker-secret":WKEY},body:JSON.stringify(data),signal:AbortSignal.timeout(8000)});return {httpStatus:r.status,...await r.json()};}catch{return {error:"unavailable"}}}
async function ping(ms){try{const r=await fetch(WORKER.replace(/\/$/,"")+"/",{signal:AbortSignal.timeout(ms)});return r.ok}catch{return false}}
async function wake(id){if(await ping(3000))return true;await send(id,"⏳ جاري تنشيط الخادم... ممكن ياخد حوالي دقيقة، وهكمل تلقائيًا أول ما يصحى.");const end=Date.now()+40000;while(Date.now()<end){if(await ping(5000))return true;await sleep(1500)}return false}

// ---------- gate: ban / maintenance / terms / mandatory channel / credit ----------
async function remember(user,url){if(url)try{await cmd("SET",P+"pend:"+user,url,"EX","3600")}catch{}}
async function exhausted(user,c,a,pending){
  await remember(user,pending);
  const free=a.bonusToday>0?[]:await bonusChannels(user,c);
  let t="⏳ خلصت حصتك النهاردة.\n"+describe(a)+"\n\n📅 الحصة بتتجدد 12 بالليل بتوقيت القاهرة ("+c.daily+" تحميل يوميًا).";
  const rows=[];
  if(free.length){t+="\n\n🎁 عايز تكمّل دلوقتي؟ اشترك في أي قناة من دول وهتاخد +"+c.bonus+" تحميلات لباقي اليوم (مرة واحدة يوميًا):";for(const ch of free.slice(0,4))rows.push([{text:"📣 "+ch,url:chanLink(ch)}]);rows.push([{text:"✅ اشتركت، فعّل المكافأة",callback_data:"bonus"}])}
  else if(a.bonusToday>0)t+="\n\n🎁 مكافأة النهاردة اتفعّلت بالفعل. نشوفك بكرة!";
  return {text:t,kb:rows.length?{inline_keyboard:rows}:undefined};
}
// One screen for onboarding: terms (+ disclaimer) and the mandatory channel, finished by a single button.
function onboard(c,needTerms){
  const rows=[];if(c.main)rows.push([{text:"📣 اشترك في القناة",url:chanLink(c.main)}]);
  rows.push([{text:needTerms?(c.main?"✅ اشتركت وأوافق على الشروط":"✅ أوافق على الشروط"):"✅ اشتركت، تحقق",callback_data:"go"}]);
  const text=needTerms
    ?ONBOARD_HEAD+TERMS+(c.main?"\n\n👇 خطوتين بس: اشترك في "+c.main+"، وبعدها دوس «اشتركت وأوافق على الشروط».":"")
    :"📣 عشان تكمل لازم تشترك في قناتنا:\n"+c.main+"\nوبعدها دوس «اشتركت، تحقق».";
  return {text,kb:{inline_keyboard:rows}};
}
async function gate(user,{credit=false,pending=null}={}){
  if(!redisOn()||isAdmin(user))return null;
  try{
    const {c,a}=await load(user);
    if(a.banned)return {text:"⛔ تم إيقاف حسابك من استخدام البوت."};
    if(c.maint)return {text:"🛠 البوت في صيانة دلوقتي. جرّب بعد شوية."};
    if(!a.terms){await remember(user,pending);return onboard(c,true)}
    if(c.main&&(await member(c.main,user))===false){await remember(user,pending);return onboard(c,false)}
    if(credit&&!a.unlimited&&a.remaining<=0)return await exhausted(user,c,a,pending);
    return null;
  }catch(e){console.error("gate",e.message);return null}   // fail-open: never lock users out because Redis is down
}
const deny=(id,g)=>send(id,g.text,g.kb);

async function proceed(id,user,url,updateId){
  const platform=getPlatform(url);if(!platform)return send(id,"⚠️ رابط غير مدعوم.");
  if(!WORKER||!WKEY)return send(id,"✅ تعرفت على رابط "+platform+".\n\n⚠️ استقبال الرسائل شغال، لكن تحميل الفيديو نفسه لسه غير متاح: خدمة معالجة الميديا ما اتوصلتش. مش هنبعت روابط وهمية.");
  if(!(await wake(id)))return send(id,"⌛ الخادم لسه بيصحى. ابعت الرابط تاني بعد حوالي دقيقة.");
  const j=await wk("/jobs",{url,chat_id:id,user_id:user,update_id:updateId??null});
  if(j.error||j.httpStatus>=400)return send(id,j.httpStatus===429?"⏱ وصلت للحد اليومي.":"⚠️ خادم المعالجة غير متاح مؤقتًا.");
  if(j.created!==false)return send(id,"🔎 استلمت الرابط وجاري فحصه.");
}
async function resume(id,user){
  if(!redisOn())return;
  try{const url=await cmd("GETDEL",P+"pend:"+user);if(url)await proceed(id,user,url,null)}catch(e){console.error("resume",e.message)}
}
const welcome=(c)=>"🎉 أهلاً بيك في Media hub saver!\n🎁 معاك "+c.welcome+" تحميلات هدية للبداية، وبعدها "+c.daily+" تحميل يوميًا.\n\nابعت رابط فيديو أو صور عام من YouTube أو TikTok أو Instagram أو Facebook.\n\nلشرح النظام اضغط /help\nلمعرفة رصيدك اضغط /me";

initReel({gate,deny,wake,wk,getPlatform,hasWorker:()=>!!(WORKER&&WKEY)});

async function message(m,u){
  if(!m||m.chat?.type!=="private")return;
  const id=m.chat.id,body=String(m.text||m.caption||"").trim(),user=m.from?.id;
  if(!allowed(user))return send(id,"🔒 البوت ده خاص بصاحبه حاليًا.");
  if(body.startsWith("/id"))return send(id,"🆔 رقم حسابك: "+user);
  if(redisOn()&&isAdmin(user)&&body.startsWith("/")){try{if(await adminCommand(body,id))return}catch(e){console.error("admin",e.message);return send(id,"⚠️ خطأ في تنفيذ الأمر: "+e.message.slice(0,120))}}
  if(redisOn()&&(!body.startsWith("/")||body==="/cancel")){try{if(await payMessage(m,id,user,body))return}catch(e){console.error("pay msg",e.message)}}
  // reel studio first: it consumes photos/voice/audio/links/script while a reel is being built (and admin library uploads)
  if(redisOn()&&!body.startsWith("/")){try{if(await reelMessage(m,id,user,body))return}catch(e){console.error("reel msg",e.message)}}
  if(body.startsWith("/start")){
    // deep links for ads: t.me/BMEDIAHUB_BOT?start=<source>  (…_sub opens the subscription, …_reel opens the reel maker)
    const src=(body.split(/\s+/)[1]||"").toLowerCase();
    if(redisOn()&&/^[a-z0-9_]{1,32}$/.test(src)){const base=src.replace(/(^|_)(sub|reel)$/,"")||src;try{await pipe([["HINCRBY",P+"src",base,"1"],["HSETNX",P+"u:"+user,"src",base]])}catch(e){console.error("src",e.message)}}
    if(redisOn()&&!isAdmin(user)){const g=await gate(user);if(g)return deny(id,g)}
    if(redisOn()&&/(^|_)sub$/.test(src))return payStart(id,user);
    if(redisOn()&&/(^|_)reel$/.test(src))return reelStart(id,user);
    return send(id,redisOn()?welcome(await getCfg()):"🎬 أهلاً بيك!\nابعت رابط فيديو عام.",menu);
  }
  if(body.startsWith("/terms"))return send(id,TERMS);
  if(body.startsWith("/reel")){if(!redisOn())return send(id,"⚠️ صانع الريلز مش متاح دلوقتي.");return reelStart(id,user)}
  if(body.startsWith("/plans")||body.startsWith("/subscribe")){
    if(!redisOn())return send(id,PLANS_TEXT);
    if(body.startsWith("/subscribe")){const g=await gate(user);if(g)return deny(id,g);return payStart(id,user)}
    const c=await getCfg();return send(id,PLANS_TEXT+(c.paynum||c.payinsta?"":await contactLine()),c.paynum||c.payinsta?{inline_keyboard:[[{text:"💳 اشترك الآن",callback_data:"pay:start"}]]}:undefined);
  }
  if(body.startsWith("/me")){
    if(!redisOn())return send(id,"ℹ️ نظام الرصيد غير مفعّل حاليًا.");
    const g=await gate(user);if(g)return deny(id,g);
    const [acc,rs]=await Promise.all([account(user,await getCfg()),reelState(user)]);
    return send(id,describe(acc)+"\n\n🎬 ريلز النهاردة: "+rs.used+" من "+(rs.plan.daily>1e6?"∞":rs.plan.daily)+"\n"+planLine(rs.plan));
  }
  if(body.startsWith("/help"))return send(id,"📖 الأوامر:\n/start — القائمة\n/reel — صانع الريلز\n/plans — باقات الريلز\n/me — رصيدي\n/help — المساعدة\n/terms — الشروط\n/status — الحالة\n/tools — الأدوات\n/wake — تنشيط الخادم\n/id — رقم حسابك\n\n"+HOWTO+"\n\nابعت رابط عام من المنصات الأربع.",menu);
  if(body.startsWith("/tools"))return send(id,"🛠 أدوات: فيديو، MP3، غلاف، ضغط، GIF، WebM، وصف المنشور، وتحميل الصور (كلها / صورة معينة / ZIP عند الطلب).",menu);
  if(body.startsWith("/wake")){if(!WORKER||!WKEY)return send(id,"⚠️ خادم المعالجة غير مربوط.");return send(id,(await wake(id))?"✅ الخادم صاحي وجاهز. ابعت الرابط.":"⌛ الخادم لسه بيصحى، جرّب /wake تاني بعد دقيقة.")}
  if(body.startsWith("/status"))return send(id,"🟢 Vercel webhook: online\n🎬 Media worker: "+(WORKER&&WKEY?"configured":"not connected yet")+"\n🗄 Redis: "+(redisOn()?"connected":"off"));
  if(body.startsWith("/stats"))return send(id,"📊 الإحصائيات متاحة لصاحب البوت فقط.");
  const murl=body.match(/https:\/\/[^\s<>"'()[\]]+/i);
  if(!murl)return send(id,"🔗 ابعت رابط فيديو عام من YouTube أو TikTok أو Instagram أو Facebook.",menu);
  const url=murl[0].replace(/[.,!?،؛]+$/,"");
  if(!getPlatform(url))return send(id,"⚠️ رابط غير مدعوم.");
  const g=await gate(user,{credit:true,pending:url});if(g)return deny(id,g);
  return proceed(id,user,url,u.update_id);
}

// Answer the callback right away (stops the button spinner) while the real work runs in parallel.
async function callback(q){
  const d=String(q.data||""),needsToast=(d==="go"||d==="terms:ok"||d==="chk"||d==="bonus");
  let acked=false;
  const ack=(text,alert=false)=>{if(acked)return Promise.resolve();acked=true;return tg("answerCallbackQuery",{callback_query_id:q.id,...(text?{text:String(text).slice(0,190),show_alert:alert}:{})}).catch(()=>{})};
  const early=needsToast?null:ack();
  try{await callbackWork(q,d,ack)}finally{await early;await ack()}
}
async function callbackWork(q,d,ack){
  const id=q.message?.chat?.id,user=q.from?.id;if(!id||!user)return;if(!allowed(user))return;
  if(d.startsWith("adm:")){if(isAdmin(user)&&redisOn())return adminCallback(d,id);return}
  if(d==="pay:start"){if(!redisOn())return;const g=await gate(user);if(g)return deny(id,g);return payStart(id,user)}
  if(d==="pay:x"){if(redisOn())await cmd("DEL",P+"pay:"+user);return send(id,"تم إلغاء طلب الاشتراك.")}
  if(d.startsWith("pr:")){if(!isAdmin(user)||!redisOn())return;const t=await payDecision(q,d,user);return ack(t,true)}
  if(isReelCallback(d)){if(!redisOn())return;return reelCallback(q,d,id,user)}
  if(d==="go"||d==="terms:ok"||d==="chk"){
    if(!redisOn())return;
    // ignore rapid repeated taps (each tap used to produce another message)
    if((await cmd("SET",P+"lock:go:"+user,"1","NX","EX","8"))!=="OK")return;
    const {c,a}=await load(user);
    if(a.banned)return ack("⛔ تم إيقاف حسابك.",true);
    const mem=c.main&&!isAdmin(user)?await member(c.main,user):true;
    if(mem===false){await cmd("DEL",P+"lock:go:"+user);return ack("⚠️ لسه مش مشترك في القناة. اشترك الأول وبعدين دوس الزرار تاني.",true)}
    if(!a.terms)await acceptTerms(user,q.from,c);
    const text=a.terms?"✅ تم التحقق. ابعت الرابط وأنا أكمل.\n\n"+welcome(c):welcome(c);
    try{await tg("editMessageText",{chat_id:id,message_id:q.message.message_id,text,disable_web_page_preview:true,reply_markup:menu})}
    catch{await send(id,text,menu)}
    return resume(id,user);
  }
  if(d==="bonus"){
    if(!redisOn())return;const g0=await gate(user);if(g0)return deny(id,g0);
    const c=await getCfg(),r=await claimBonus(user,c);
    if(r.ok){await send(id,"🎉 تم تفعيل +"+r.units+" تحميلات لباقي النهاردة (اشتراكك في "+r.channel+").");return resume(id,user)}
    if(r.reason==="today")return send(id,"🎁 المكافأة اتفعّلت النهاردة بالفعل. نشوفك بكرة!");
    if(r.reason==="none")return send(id,"مفيش قنوات إضافية متاحة ليك دلوقتي. الحصة بتتجدد بكرة.");
    return ack("مش لاقيك مشترك في أي قناة جديدة. اشترك في واحدة من القنوات اللي فوق وبعدين دوس الزرار تاني.",true);
  }
  if(d==="tool:wake"){if(!WORKER||!WKEY)return send(id,"⚠️ خادم المعالجة غير مربوط.");return send(id,(await wake(id))?"✅ الخادم صاحي وجاهز. ابعت الرابط.":"⌛ الخادم لسه بيصحى، اضغط تنشيط تاني بعد دقيقة.")}
  if(d==="tool:status")return send(id,"🟢 البوت شغال.\n🎬 خادم التحميل: "+(WORKER&&WKEY?"مربوط":"غير مربوط"));
  if(d.startsWith("tool:")){const t=d.slice(5);return send(id,t==="photos"?"🖼 ابعت رابط بوست صور (TikTok أو Instagram) وهتظهرلك معاينة بالأرقام وأزرار: كل الصور / صورة معينة / ملف ZIP.":t==="caption"?"📝 ابعت رابط فيديو، وبعد الفحص هتلاقي زرار «📝 الوصف / الكابشن» يرسللك نص المنشور كامل علشان تنسخه (من غير تكلفة).":"🛠 "+(TOOL_AR[t]||t)+"\nابعت رابط فيديو عام، وبعد الفحص هتلاقي الأداة دي في اللوحة اللي هتظهرلك.")}
  if(d.startsWith("a:")&&WORKER&&WKEY){
    const p=d.split(":");
    if(p.length===4&&/^[0-9a-f]{16}$/.test(p[1])&&MODES.includes(p[2])){
      let credit=!FREE_MODES.includes(p[2]);
      if(credit&&redisOn()){try{if(await cmd("EXISTS",P+"chg:"+p[1]))credit=false}catch{}}   // tools on an already-paid video are free
      const g=await gate(user,{credit});if(g)return deny(id,g);
      const j=await wk("/jobs/"+p[1]+"/action",{user_id:user,chat_id:id,mode:p[2],quality:p[3]});
      return send(id,j.error?"⚠️ فشل تنفيذ الأداة.":j.httpStatus===404||j.httpStatus===410?"⌛ انتهت صلاحية الأزرار دي. ابعت الرابط من جديد.":j.httpStatus===409?(j.detail==="busy"?"⏳ لسه بجهّز طلب سابق على نفس الفيديو. استنى يوصلك وبعدها اختار الأداة.":j.detail==="not ready"?"⏳ الفيديو لسه بيتفحص أو الفحص فشل. ابعت الرابط تاني لو اتأخر.":"⚠️ الأداة دي مش متاحة لنوع المحتوى ده."):j.httpStatus>=400?"⚠️ تعذر تنفيذ الطلب.":(p[2]==="photos"&&p[3]==="pick")?"🔢 اختار رقم الصورة.":p[2]==="caption"?"📝 جاري إرسال الوصف.":"✅ استلمت طلبك.");
    }
  }
}

// Worker -> Vercel: report a completed delivery (charges credit once, counts outbound bandwidth, alerts at 4.5 GB).
const BW_ALERTS=[];   // the worker moved from Render (5 GB/month) to our own VPS: no bandwidth cap to warn about
async function handleUsage(b){
  const user=Number(b.user)||0,r=await usage({user,job:/^(?:[0-9a-f]{16}|\d{5,15}-[0-9a-f]{10})$/.test(String(b.job||""))?b.job:"",kind:b.kind,bytes:b.bytes});
  if(r.bandwidth!==null){
    const prev=r.bandwidth-Math.max(0,Math.floor(Number(b.bytes)||0));
    for(const [t,msg] of BW_ALERTS)if(prev<t&&r.bandwidth>=t&&(await cmd("SET",P+"bwalert:"+monthKey()+":"+t,"1","NX","EX","3000000"))==="OK"){
      for(const a of ADMINS())await send(a,msg+"\nالاستهلاك المسجّل: "+(r.bandwidth/1e9).toFixed(2)+" GB").catch(()=>{});
    }
  }
  return r;
}

async function activate(){const before=await tg("getWebhookInfo");if(before.url!==WH)await tg("setWebhook",{url:WH,secret_token:SECRET,allowed_updates:["message","callback_query"],drop_pending_updates:false,max_connections:10});const info=await tg("getWebhookInfo");return {registered:info.url===WH,url:info.url,pending:info.pending_update_count,lastError:info.last_error_message||null}}
// Backup: the VPS calls this (worker secret) page by page and stores the JSON; values are dumped by type, with TTLs.
async function backupPage(cursor){
  const r=await cmd("SCAN",String(cursor||"0"),"COUNT","300");const next=r[0],keys=r[1]||[];
  if(!keys.length)return {cursor:next,items:[]};
  const types=await pipe(keys.map(k=>["TYPE",k]));
  const get=keys.map((k,i)=>({string:["GET",k],hash:["HGETALL",k],set:["SMEMBERS",k],zset:["ZRANGE",k,"0","-1","WITHSCORES"],list:["LRANGE",k,"0","-1"]})[types[i]]||["TYPE",k]);
  const vals=await pipe(get),ttls=await pipe(keys.map(k=>["PTTL",k]));
  return {cursor:next,items:keys.map((k,i)=>({k,t:types[i],v:vals[i],ttl:ttls[i]}))};
}
export default async function handler(req,res){const p=(req.url||"").split("?")[0];
if(req.method==="GET"&&(p==="/"||p==="/index.html")){res.writeHead(200,{"content-type":"text/html;charset=utf-8"});return res.end('<!doctype html><html lang="ar" dir="rtl"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Media hub saver</title><style>body{font-family:system-ui;background:#f4f9f9;display:grid;place-items:center;min-height:100vh;margin:0;color:#1b4965}main{background:white;padding:36px;border-radius:20px;max-width:580px;margin:20px}a{display:inline-block;background:#1b4965;color:white;border-radius:12px;padding:15px 24px;text-decoration:none}p{line-height:1.8}</style><main><h1>🎬 Media hub saver</h1><p>Telegram webhook يعمل على Vercel. تحميل الميديا يحتاج خادم معالجة منفصل.</p><a href="https://t.me/BMEDIAHUB_BOT">افتح بوت تليجرام</a></main></html>')}
if(req.method==="GET"&&(p==="/api/health"||p==="/health"))return ret(res,{ok:true,telegramConfigured:!!(process.env.TG_BOT_TOKEN&&SECRET),workerConfigured:!!(WORKER&&WKEY),redisConfigured:redisOn(),platforms:["youtube","tiktok","instagram","facebook"]});
if(req.method==="GET"&&(p==="/api/activate"||p==="/activate")){try{return ret(res,{ok:true,...await activate()})}catch(e){console.error("activate",e.message);return ret(res,{ok:false,error:"activation_failed"},502)}}
if(req.method==="POST"&&(p==="/api/usage"||p==="/usage")){if(!WKEY||req.headers["x-worker-secret"]!==WKEY)return ret(res,{ok:false},403);if(!redisOn())return ret(res,{ok:true,skipped:"no_redis"});try{const b=typeof req.body==="string"?JSON.parse(req.body):(req.body||{});return ret(res,{ok:true,...await handleUsage(b)})}catch(e){console.error("usage",e.message);return ret(res,{ok:false},500)}}
if(req.method==="POST"&&(p==="/api/backup"||p==="/backup")){if(!WKEY||req.headers["x-worker-secret"]!==WKEY)return ret(res,{ok:false},403);if(!redisOn())return ret(res,{ok:false,error:"no_redis"},503);try{const b=typeof req.body==="string"?JSON.parse(req.body||"{}"):(req.body||{});return ret(res,{ok:true,...await backupPage(b.cursor)})}catch(e){console.error("backup",e.message);return ret(res,{ok:false},500)}}
if(req.method==="POST"&&(p==="/api/webhook"||p==="/webhook")){if(!SECRET||req.headers["x-telegram-bot-api-secret-token"]!==SECRET)return ret(res,{ok:false},403);try{const t0=Date.now(),u=typeof req.body==="string"?JSON.parse(req.body):req.body;if(u?.message)await message(u.message,u);if(u?.callback_query)await callback(u.callback_query);console.log("upd",u?.callback_query?"cb:"+String(u.callback_query.data||"").slice(0,10):"msg",Date.now()-t0+"ms");return ret(res,{ok:true})}catch(e){console.error("webhook",e.message);return ret(res,{ok:false},500)}}
if(p==="/panel"||p==="/panel/"||p.startsWith("/api/panel/")){if(!redisOn())return ret(res,{ok:false,error:"no_redis"},503);try{if(await panelRoute(req,res,p))return}catch(e){console.error("panel",e.message);return ret(res,{ok:false},500)}}
return ret(res,{error:"not_found"},404)}
