// Reel studio wizard: Quran reels and free-form reels (your audio / photos / link / script).
// Wizard state lives in Redis (JSON per user); uploaded photos go to a Redis list so album photos arriving in
// parallel webhooks never overwrite each other. Settings are one message edited in place.
import {tg,send} from "./tg.js";
import {cmd,pipe} from "./redis.js";
import {P,getCfg,reelState,isAdmin} from "./quota.js";
import {SURAH,AYAT,parseVerse} from "./quran.js";
import {planLine} from "./admin.js";
import {PLANS_TEXT} from "./texts.js";

let X={};   // {gate,deny,wake,wk,getPlatform,hasWorker} injected by api/index.js
export const initReel=ctx=>{X=ctx};

const RECITERS=[["afasy","مشاري العفاسي"],["basit","عبد الباسط"],["husary","الحصري"],["minshawi","المنشاوي"],["sudais","السديس"],["maher","ماهر المعيقلي"],["dossari","ياسر الدوسري"],["shatri","أبو بكر الشاطري"]];
const RNAME=Object.fromEntries(RECITERS);
// ---------- recitation length estimate (before the reel is made) ----------
// everyayah serves constant-bitrate MP3s, so length ~= bytes*8/bitrate. The real bitrate is read once per reciter from an
// MP3 frame header (falls back to the folder's nominal kbps); per-ayah seconds are cached in Redis for good.
const FOLDER={afasy:["Alafasy_128kbps",128],basit:["Abdul_Basit_Murattal_192kbps",192],husary:["Husary_128kbps",128],minshawi:["Minshawy_Murattal_128kbps",128],sudais:["Abdurrahmaan_As-Sudais_192kbps",192],maher:["MaherAlMuaiqly128kbps",128],dossari:["Yasser_Ad-Dussary_128kbps",128],shatri:["Abu_Bakr_Ash-Shaatree_128kbps",128]};
const ayahUrl=(k,s,a)=>"https://everyayah.com/data/"+FOLDER[k][0]+"/"+String(s).padStart(3,"0")+String(a).padStart(3,"0")+".mp3";
const BR1=[0,32,40,48,56,64,80,96,112,128,160,192,224,256,320],BR2=[0,8,16,24,32,40,48,56,64,80,96,112,128,144,160];
export function mp3Kbps(b){
  let i=0;
  if(b.length>10&&b[0]===0x49&&b[1]===0x44&&b[2]===0x33)i=10+((b[6]&127)<<21|(b[7]&127)<<14|(b[8]&127)<<7|(b[9]&127));
  for(;i+3<b.length;i++){
    if(b[i]!==0xff||(b[i+1]&0xe0)!==0xe0)continue;
    const ver=(b[i+1]>>3)&3,layer=(b[i+1]>>1)&3,bi=b[i+2]>>4;
    if(ver===1||layer!==1||bi===0||bi===15)continue;   // need MPEG layer III with a real bitrate
    return (ver===3?BR1:BR2)[bi];
  }
  return 0;
}
async function kbpsFor(k){
  const key=P+"qbr:"+k;
  try{const v=Number(await cmd("GET",key));if(v>0)return v}catch{}
  try{
    const r=await fetch(ayahUrl(k,1,1),{headers:{range:"bytes=0-16383"},signal:AbortSignal.timeout(4000)});
    const kb=mp3Kbps(new Uint8Array(await r.arrayBuffer()));
    if(kb>0){await cmd("SET",key,String(kb),"EX","2592000").catch(()=>{});return kb}
  }catch{}
  return FOLDER[k][1];
}
async function ayahSecs(k,s,list){
  const key=P+"qd:"+k,f=list.map(a=>s+":"+a);
  let got=[];try{got=await cmd("HMGET",key,...f)||[]}catch{}
  const out=list.map((a,i)=>Number(got[i])||0),miss=list.map((a,i)=>i).filter(i=>!out[i]);
  if(miss.length){
    const kb=await kbpsFor(k),save=[];
    await Promise.all(miss.map(async i=>{try{
      const r=await fetch(ayahUrl(k,s,list[i]),{method:"HEAD",signal:AbortSignal.timeout(4000)});
      const n=Number(r.headers.get("content-length"));if(r.ok&&n>1000){out[i]=Math.round(n*8/(kb*1000)*10)/10;save.push(f[i],String(out[i]))}
    }catch{}}));
    if(save.length)await cmd("HSET",key,...save).catch(()=>{});
  }
  return out;
}
// Seconds per ayah from frm, enough to fill the plan cap (or to the requested last ayah). null when unknown.
async function estimate(st,cap){
  const last=st.to||Math.min(AYAT[st.surah-1],st.frm+40),est=[];let sum=0;
  for(let a=st.frm;a<=last&&sum<=cap+1;a+=10){
    const list=[];for(let i=a;i<=Math.min(last,a+9);i++)list.push(i);
    const d=await ayahSecs(st.reciter,st.surah,list);if(d.some(x=>!x))return null;
    for(const x of d){est.push(x);sum+=x}
  }
  return est.length?est:null;
}
// Mirrors the worker: whole ayat while they fit, at least one, never past the plan cap.
function simulate(est,maxdur,cap){
  let n=0,t=0;for(const d of est){if(n&&t+d>maxdur)break;n++;t+=d;if(t>=maxdur)break}
  return {n,t:Math.min(t,cap)};
}
const MAXF=20*1024*1024,MAX_IMG=20;
const rwKey=u=>P+"rw:"+u,imgKey=u=>P+"rwi:"+u;
async function rwGet(u){try{const v=await cmd("GET",rwKey(u));return v?JSON.parse(v):null}catch{return null}}
const rwSet=(u,st)=>cmd("SET",rwKey(u),JSON.stringify(st),"EX","3600");
const rwDel=u=>pipe([["DEL",rwKey(u)],["DEL",imgKey(u)]]).catch(()=>{});
// Page signature: an optional name drawn under the verses, remembered per user.
const sigKey=u=>P+"sig:"+u;
async function sigGet(u){try{return (await cmd("GET",sigKey(u)))||""}catch{return ""}}
export function cleanSig(t){
  t=String(t||"").replace(/[\u0000-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g,"").replace(/\s+/g," ").trim();
  if(!t||t.length>30||/https?:|www\.|t\.me\/|\.(com|net|org|me|io)\b/i.test(t))return "";
  return t;
}
const SIG_HELP="✍️ اكتب اسم صفحتك زي ما عايزه يظهر تحت الآيات (لحد 30 حرف، من غير روابط).\nمثال: @my_page أو صفحة نور الهدى\n\nهيتحفظ لك وتقدر تغيّره أو تشيله في أي وقت.";
const sigKb={inline_keyboard:[[{text:"🚫 من غير توقيع",callback_data:"rs:g0"},{text:"↩️ رجوع",callback_data:"rs:gb"}]]};
const ar=n=>String(n).replace(/\d/g,d=>"٠١٢٣٤٥٦٧٨٩"[d]);
const secs=n=>n<60?n+"ث":(n%60?Math.floor(n/60)+"د "+(n%60)+"ث":(n/60)+" د");
export async function contactLine(){try{const c=(await getCfg()).contact||"";return c?"\n\nللاشتراك كلّم "+c+" (دفع فودافون كاش).":"\n\nللاشتراك كلّم الإدارة."}catch{return ""}}

const VERSE_HELP="📖 اكتب السورة ورقم الآية (أو مدى آيات):\n\nأمثلة:\n• الملك\n• البقرة 255\n• سورة مريم من الآية 37 للآية 42\n• 2:255\n• آية الكرسي\n\nلو كتبت السورة بس هبدأ من أولها، والريل بيقف عند آخر آية كاملة جوة المدة اللي هتختارها.";
const MEDIA_HELP="🎨 ريل من ملفاتك — ابعتلي اللي عندك (أي ترتيب):\n\n🎙 صوت: ملف صوت أو voice (محاضرة، تلاوة، دعاء…)\n🔗 أو رابط فيديو عام: آخد منه الصوت أو الفيديو نفسه\n🖼 صور: لحد 20 صورة تتعرض ورا بعض بحركة وانتقالات\n📝 سكريبت: ابعته كرسالة نصية ويظهر مكتوب على الفيديو\n\nلو مفيش صور ولا فيديو هستخدم خلفيات جاهزة. كل ملف لحد 20 ميجا.\nلما تخلص دوس «✅ كمّل».";
const pickKb={inline_keyboard:[[{text:"📖 ريل قرآن",callback_data:"rk:q"}],[{text:"🎨 ريل من صوتك / صورك / رابط",callback_data:"rk:m"}],[{text:"✖️ إلغاء",callback_data:"rs:x"}]]};
const collectKb={inline_keyboard:[[{text:"✅ كمّل",callback_data:"rm:go"},{text:"✖️ إلغاء",callback_data:"rs:x"}]]};

export async function reelStart(id,user){
  const g=await X.gate(user);if(g)return X.deny(id,g);
  const r=await reelState(user);
  if(r.used>=r.plan.daily)return send(id,"⏳ استخدمت ريلز النهاردة ("+r.used+" من "+r.plan.daily+").\n"+planLine(r.plan)+"\n\n"+PLANS_TEXT+(await contactLine()));
  if(r.freeFull)return send(id,"⏳ الريلز المجانية خلصت النهاردة لكل المستخدمين بسبب الضغط. ارجع بكرة، أو اشترك وتعمل لحد 10 ريلز يوميًا بأولوية.\n\n"+PLANS_TEXT+(await contactLine()));
  await rwDel(user);await rwSet(user,{step:"pick"});
  return send(id,"🎬 صانع الريلز\nعايز تعمل ريل من إيه؟",pickKb);
}

// ---------- incoming messages while a wizard (or an admin capture) is active ----------
const bigFile=f=>f&&Number(f.file_size||0)>MAXF;
function mediaOf(m){
  if(m.photo?.length){const p=m.photo[m.photo.length-1];return {type:"img",f:p,w:p.width,h:p.height}}
  const d=m.document;
  if(d&&/^image\//.test(d.mime_type||""))return {type:"img",f:d,w:d.thumbnail?.width||d.thumb?.width||0,h:d.thumbnail?.height||d.thumb?.height||0,doc:true};
  if(m.voice)return {type:"audio",f:m.voice,name:"voice"};
  if(m.audio)return {type:"audio",f:m.audio,name:m.audio.title||m.audio.file_name||"audio"};
  if(d&&/^audio\//.test(d.mime_type||""))return {type:"audio",f:d,name:d.file_name||"audio"};
  if(m.video)return {type:"video",f:m.video};
  if(m.video_note)return {type:"video",f:m.video_note};
  if(d&&/^video\//.test(d.mime_type||""))return {type:"video",f:d};
  return null;
}
async function adminCapture(m,id,user){
  const mode=await cmd("GET",P+"admcap:"+user).catch(()=>null);if(!mode)return false;
  const md=mediaOf(m);if(!md||md.type!=="img")return false;
  if(mode==="logo"){
    await pipe([["HSET",P+"cfg","logo",md.f.file_id],["DEL",P+"admcap:"+user]]);
    await send(id,"✅ اتحفظ اللوجو، وهيظهر مكان العلامة المكتوبة في الريلز.\n(الأفضل PNG بخلفية شفافة يتبعت كملف)");return true;
  }
  const o=md.w&&md.h?(md.h>=md.w?"916":"169"):"916";
  await cmd("SADD",P+"bgs:"+o,md.f.file_id);
  if(!m.media_group_id||(await cmd("SET",P+"mg:"+m.media_group_id,"1","NX","EX","60"))==="OK"){
    const [a,b]=await pipe([["SCARD",P+"bgs:916"],["SCARD",P+"bgs:169"]]);
    await send(id,"🖼 اتضافت للمكتبة ("+(o==="916"?"طولي 9:16":"عرضي 16:9")+").\nالمكتبة: "+a+" طولي · "+b+" عرضي.\nكمّل ابعت، أو /bg off للإنهاء.");
  }
  return true;
}
async function collectSummary(user,st){
  const n=Number(await cmd("LLEN",imgKey(user)).catch(()=>0))||0;
  const parts=[];if(st.audio)parts.push("🎙 صوت");if(st.video)parts.push("🎬 فيديو");if(st.url)parts.push("🔗 رابط");if(n)parts.push("🖼 "+n+" صورة");if(st.script)parts.push("📝 سكريبت");
  return {n,text:"عندك: "+(parts.join(" · ")||"لسه مفيش حاجة")+"\nابعت حاجة كمان أو دوس «✅ كمّل»."};
}
// Returns true when the message was consumed by the reel studio.
export async function reelMessage(m,id,user,body){
  if(isAdmin(user)&&await adminCapture(m,id,user))return true;
  const st=await rwGet(user);if(!st)return false;
  const md=mediaOf(m);
  if(st.step==="verse"){
    if(!body||md||/https:\/\//i.test(body))return false;
    const v=parseVerse(body);
    if(!v){await send(id,"⚠️ مش قادر أفهم السورة أو الآية. "+VERSE_HELP);return true}
    await rwSet(user,{step:"reciter",surah:v.surah,frm:v.frm,to:v.to});
    const rows=[];for(let i=0;i<RECITERS.length;i+=2)rows.push(RECITERS.slice(i,i+2).map(([k,n])=>({text:"🎙 "+n,callback_data:"rr:"+k})));rows.push([{text:"✖️ إلغاء",callback_data:"rs:x"}]);
    await send(id,"📖 سورة "+SURAH[v.surah-1]+" · "+(v.to?("الآيات من "+ar(v.frm)+" إلى "+ar(v.to)):("من آية "+ar(v.frm)))+"\n\nاختار القارئ:",{inline_keyboard:rows});
    return true;
  }
  if(st.step==="settings"&&st.await==="sig"){
    if(!body||md||body.startsWith("/"))return false;
    const s=cleanSig(body);
    if(!s){await send(id,"⚠️ الاسم لازم يكون لحد 30 حرف ومن غير روابط. جرّب تاني.",sigKb);return true}
    st.sig=s;delete st.await;await cmd("SET",sigKey(user),s).catch(()=>{});await rwSet(user,st);
    const {plan}=await reelState(user);const v=quranView(st,plan);
    if(st.mid)await tg("editMessageText",{chat_id:id,message_id:st.mid,text:v.text,reply_markup:v.kb}).catch(()=>send(id,v.text,v.kb));else await send(id,v.text,v.kb);
    return true;
  }
  if(st.step==="collect"){
    if(md){
      if(bigFile(md.f)){await send(id,"⚠️ الملف ده أكبر من 20 ميجا (حد تيليجرام للبوتات). ابعته أصغر، أو ابعت رابطه.",collectKb);return true}
      if(md.type==="img"){
        const n=await cmd("RPUSH",imgKey(user),md.f.file_id);await cmd("EXPIRE",imgKey(user),"3600");
        if(n>MAX_IMG){await cmd("LTRIM",imgKey(user),"0",String(MAX_IMG-1));await send(id,"⚠️ الحد "+MAX_IMG+" صورة، والزيادة اتجاهلت.",collectKb);return true}
        if(m.media_group_id&&(await cmd("SET",P+"mg:"+m.media_group_id,"1","NX","EX","60"))!=="OK")return true;   // one reply per album
        const s=await collectSummary(user,st);await send(id,"🖼 استلمت"+(m.media_group_id?" الصور":" الصورة")+".\n"+s.text,collectKb);return true;
      }
      if(md.type==="audio"){st.audio=md.f.file_id;st.audioName=String(md.name||"").slice(0,40)}
      if(md.type==="video")st.video=md.f.file_id;
      await rwSet(user,st);const s=await collectSummary(user,st);
      await send(id,(md.type==="audio"?"🎙 استلمت الصوت.":"🎬 استلمت الفيديو (هيتستخدم كصورة من غير صوته).")+"\n"+s.text,collectKb);return true;
    }
    if(!body||body.startsWith("/"))return false;
    const u=body.match(/https:\/\/[^\s<>"'()[\]]+/i);
    if(u){
      const url=u[0].replace(/[.,!?،؛]+$/,"");
      if(!X.getPlatform(url)){await send(id,"⚠️ الرابط ده مش مدعوم. ابعت رابط عام من YouTube أو TikTok أو Instagram أو Facebook.",collectKb);return true}
      st.url=url;await rwSet(user,st);const s=await collectSummary(user,st);
      await send(id,"🔗 استلمت الرابط (هتختار في الإعدادات تاخد منه الصوت ولا الفيديو).\n"+s.text,collectKb);return true;
    }
    st.script=body.slice(0,1500);await rwSet(user,st);const s=await collectSummary(user,st);
    await send(id,"📝 استلمت السكريبت، وهيظهر مكتوب على الفيديو.\n"+s.text,collectKb);return true;
  }
  if(st.step==="msettings"&&body&&!md){
    const t=body.replace(/[٠-٩]/g,d=>"٠١٢٣٤٥٦٧٨٩".indexOf(d)).trim();const mm=t.match(/^(\d{1,3})(?::(\d{1,2}))?$/);
    if(!mm)return false;
    st.start=Math.min(3600,mm[2]!==undefined?Number(mm[1])*60+Number(mm[2]):Number(mm[1]));await rwSet(user,st);
    const {plan}=await reelState(user);const n=Number(await cmd("LLEN",imgKey(user)))||0;const v=mediaView(st,plan,n);
    if(st.mid)await tg("editMessageText",{chat_id:id,message_id:st.mid,text:v.text,reply_markup:v.kb}).catch(()=>send(id,v.text,v.kb));else await send(id,v.text,v.kb);
    return true;
  }
  return false;
}

// ---------- settings views ----------
function durRows(st,plan,prefix){
  const DUR=[10,15,20,30,60,...(plan.cap>=120?[120]:[]),...(plan.cap>=180?[180]:[])];
  const lab=n=>(st.dur===n?"✅ ":"")+(n<60?n+"ث":n===60?"دقيقة":(n/60)+" د");
  const rows=[DUR.slice(0,5).map(n=>({text:lab(n),callback_data:prefix+"d:"+n}))];
  if(DUR.length>5)rows.push(DUR.slice(5).map(n=>({text:lab(n),callback_data:prefix+"d:"+n})));
  return rows;
}
const sizeRow=(st,prefix)=>[{text:(st.ar==="9:16"?"✅ ":"")+"📱 9:16 ريلز/شورتس",callback_data:prefix+"a:916"},{text:(st.ar==="16:9"?"✅ ":"")+"🖥 16:9 يوتيوب",callback_data:prefix+"a:169"}];
function quranView(st,plan){
  const rows=durRows(st.fit?{...st,dur:-1}:st,plan,"rs:");
  const est=st.est||null,all=est&&st.to&&est.length===st.to-st.frm+1?est.reduce((x,y)=>x+y,0):null;
  if(st.to&&est)rows.push([{text:(st.fit?"✅ ":"")+"📏 على قد الآيات"+(all?" (~"+secs(Math.ceil(Math.min(all,plan.cap)))+")":""),callback_data:"rs:f"}]);
  rows.push(sizeRow(st,"rs:"));
  rows.push([{text:"🌑 الفينييت: "+(st.vig?"شغال":"مقفول"),callback_data:"rs:v"},{text:"✍️ التوقيع: "+(st.sig?st.sig.slice(0,14):"مفيش"),callback_data:"rs:g"}]);
  rows.push([{text:"🎬 اعمل الريل",callback_data:"rs:go"},{text:"✖️ إلغاء",callback_data:"rs:x"}]);
  const range=st.to?("من آية "+ar(st.frm)+" إلى "+ar(st.to)):("من آية "+ar(st.frm));
  const lim=st.fit?plan.cap:st.dur;
  let info="⏱ أقصى مدة: "+(st.fit?"على قد الآيات":secs(st.dur)+" (بيقف عند آخر آية كاملة)");
  if(est){
    const sm=simulate(est,lim,plan.cap),upto=st.frm+sm.n-1,len=secs(Math.round(sm.t+1));
    if(st.to){
      if(all!==null)info+="\n🕰 الآيات كلها بصوت "+RNAME[st.reciter]+": حوالي "+secs(Math.round(all));
      if(upto>=st.to)info+="\n✅ المدة كفاية للآيات كلها · الريل هيطلع حوالي "+len;
      else if(all!==null&&all>plan.cap)info+="\n⚠️ الآيات دي أطول من حد باقتك ("+secs(plan.cap)+")، هيتقرأ لحد آية "+ar(upto)+" بس · الريل حوالي "+len;
      else info+="\n⚠️ بالمدة دي هيتقرأ لحد آية "+ar(upto)+" بس (الريل حوالي "+len+"). دوس «📏 على قد الآيات» تتقري كلها";
    }else info+="\n🕰 في المدة دي هيتقرأ من آية "+ar(st.frm)+" لحد آية "+ar(upto)+" · الريل حوالي "+len;
    if(sm.n===1&&est[0]>lim+1)info+="\nℹ️ أول آية لوحدها أطول من المدة، فهتتقري كاملة";
  }
  const text="🎬 إعدادات الريل\n\n📖 سورة "+SURAH[st.surah-1]+" · "+range+"\n🎙 القارئ: "+RNAME[st.reciter]+"\n"+info+"\n📐 المقاس: "+st.ar+"\n🌑 الفينييت: "+(st.vig?"شغال":"مقفول")+"\n✍️ توقيع الصفحة: "+(st.sig||"مفيش")+"\n💧 العلامة المائية: "+(plan.wm?"موجودة (تتلغي بالاشتراك /plans)":plan.launch?"ملغية (هدية فترة الإطلاق)":"ملغية");
  return {text,kb:{inline_keyboard:rows}};
}
const LINK_LABEL={audio:"الصوت بس",video:"الفيديو من غير صوته",both:"الفيديو بصوته"};
const STARTS=[0,5,10,15,20,30,45,60,90,120,180,240,300];
const hasAudio=st=>!!(st.audio||(st.url&&st.link!=="video"));
function mediaView(st,plan,nimg){
  const rows=durRows(st,plan,"ms:");
  if(hasAudio(st))rows.push([{text:"⏩ يبدأ من الثانية: "+st.start,callback_data:"ms:s"}]);
  if(st.url)rows.push([{text:"🔗 من الرابط: "+LINK_LABEL[st.link],callback_data:"ms:l"}]);
  rows.push(sizeRow(st,"ms:"));
  const r2=[{text:"🌑 الفينييت: "+(st.vig?"شغال":"مقفول"),callback_data:"ms:v"}];
  if(nimg&&!st.video&&!(st.url&&st.link!=="audio"))r2.push({text:"🎞 حركة الصور: "+(st.move?"شغالة":"مقفولة"),callback_data:"ms:m"});
  rows.push(r2);
  rows.push([{text:"🎬 اعمل الريل",callback_data:"ms:go"},{text:"✖️ إلغاء",callback_data:"rs:x"}]);
  const visual=st.video?"🎬 الفيديو اللي بعته (من غير صوته)":(st.url&&st.link!=="audio")?"🎬 فيديو الرابط":nimg?"🖼 صورك ("+nimg+")":"🌄 خلفيات جاهزة";
  const audio=st.audio?"🎙 الصوت اللي بعته":(st.url&&st.link!=="video")?"🔗 صوت الرابط":"بدون صوت";
  const text="🎬 إعدادات الريل\n\n🎞 الصورة: "+visual+"\n🔊 الصوت: "+audio+(hasAudio(st)?" · يبدأ من الثانية "+st.start+" (تقدر تكتب رقم الثانية أو 1:30)":"")+"\n⏱ المدة: "+secs(st.dur)+(hasAudio(st)?" (أو لحد آخر الصوت لو أقصر)":"")+"\n📝 السكريبت: "+(st.script?"هيظهر على الفيديو":"مفيش")+"\n📐 المقاس: "+st.ar+"\n🌑 الفينييت: "+(st.vig?"شغال":"مقفول")+"\n💧 العلامة المائية: "+(plan.wm?"موجودة (تتلغي بالاشتراك /plans)":"ملغية");
  return {text,kb:{inline_keyboard:rows}};
}

async function libraryFor(st){
  const o=st.ar==="16:9"?"169":"916";
  try{const [c,bgs]=await pipe([["HGET",P+"cfg","logo"],["SRANDMEMBER",P+"bgs:"+o,"6"]]);return {logo:c||"",bgs:bgs||[]}}catch{return {logo:"",bgs:[]}}
}
async function submit(id,user,plan,used,payload,retryKb,edit){
  if((await cmd("SET",P+"lock:reel:"+user,"1","NX","EX","15"))!=="OK")return;
  const unlock=()=>cmd("DEL",P+"lock:reel:"+user).catch(()=>{});
  const g=await X.gate(user);if(g){await unlock();return X.deny(id,g)}
  if(used>=plan.daily){await unlock();return edit("⏳ استخدمت ريلز النهاردة ("+used+" من "+plan.daily+").\n\n"+PLANS_TEXT+(await contactLine()))}
  if(plan.freeFull){await unlock();return edit("⏳ الريلز المجانية خلصت النهاردة لكل المستخدمين بسبب الضغط. ارجع بكرة، أو اشترك وتعمل لحد 10 ريلز يوميًا بأولوية.\n\n"+PLANS_TEXT+(await contactLine()))}
  if(!X.hasWorker()){await unlock();return send(id,"⚠️ خادم المعالجة غير مربوط.")}
  await edit("⏳ جاري إرسال طلب الريل…");
  if(!(await X.wake(id))){await unlock();return send(id,"⌛ الخادم لسه بيصحى. دوس «اعمل الريل» تاني بعد دقيقة.",retryKb)}
  const j=await X.wk("/reels",{chat_id:id,user_id:user,cap:plan.cap,wm:plan.wm?"@BMEDIAHUB_BOT":"",...payload});
  await unlock();
  if(j.error)return send(id,"⚠️ خادم المعالجة مش متاح مؤقتًا. جرّب تاني.",retryKb);
  if(j.httpStatus===409)return send(id,"⏳ عندك ريل لسه بيتعمل. استنى يوصلك وبعدين اعمل التاني.");
  if(j.httpStatus===429)return send(id,"🚦 الطابور مليان دلوقتي. جرّب بعد كام دقيقة.",retryKb);
  if(j.httpStatus>=400)return send(id,"⚠️ تعذر عمل الريل بالإعدادات دي.");
  await rwDel(user);
  return send(id,"✅ استلمت طلب الريل"+(j.position>1?" · ترتيبك في الطابور: "+j.position:"")+".\nهيوصلك الفيديو هنا أول ما يخلص (عادة من دقيقتين لـ 6 دقايق).");
}

export async function reelCallback(q,d,id,user){
  const mid=q.message?.message_id;
  const edit=(text,kb)=>tg("editMessageText",{chat_id:id,message_id:mid,text,disable_web_page_preview:true,...(kb?{reply_markup:kb}:{})}).catch(e=>{if(!/not modified/.test(e.message))return send(id,text,kb)});
  if(d==="reel:new")return reelStart(id,user);
  if(d.startsWith("rc:"))return reelContinue(id,user,d);
  if(d==="rs:x"){await rwDel(user);return edit("✖️ تم إلغاء الريل. تقدر تبدأ من جديد من /reel")}
  const st=await rwGet(user);
  if(!st)return send(id,"⌛ الجلسة دي انتهت. ابدأ من جديد من /reel");
  if(d==="rk:q"){await rwSet(user,{step:"verse"});return edit(VERSE_HELP)}
  if(d==="rk:m"){await pipe([["DEL",imgKey(user)]]);await rwSet(user,{step:"collect"});return edit(MEDIA_HELP,collectKb)}
  const {plan,used}=await reelState(user);
  // ----- free-form reels -----
  if(d.startsWith("rm:")||d.startsWith("ms:")){
    if(st.step!=="collect"&&st.step!=="msettings")return;
    const n=Number(await cmd("LLEN",imgKey(user)))||0;
    if(d==="rm:go"){
      if(!(st.audio||st.video||st.url||n))return send(id,"⚠️ ابعت صوت أو صور أو رابط الأول، وبعدين دوس «✅ كمّل».",collectKb);
      Object.assign(st,{step:"msettings",mid,dur:Math.min(30,plan.cap),start:0,ar:"9:16",vig:true,move:true,link:st.audio?"video":(n?"audio":"both")});
      await rwSet(user,st);const v=mediaView(st,plan,n);return edit(v.text,v.kb);
    }
    if(st.step!=="msettings")return;
    st.mid=mid;
    if(d.startsWith("ms:d:")){const x=Number(d.slice(5));if(!(x>0&&x<=plan.cap))return;st.dur=x}
    else if(d==="ms:s"){const i=STARTS.indexOf(Number(st.start));st.start=STARTS[(i+1)%STARTS.length]}
    else if(d==="ms:l"){const o=["audio","video","both"];st.link=st.audio?"video":o[(o.indexOf(st.link)+1)%3]}   // with your own audio the link can only be the picture
    else if(d==="ms:a:916")st.ar="9:16";else if(d==="ms:a:169")st.ar="16:9";else if(d==="ms:v")st.vig=!st.vig;else if(d==="ms:m")st.move=!st.move;
    else if(d==="ms:go"){
      const imgs=n?await cmd("LRANGE",imgKey(user),"0",String(MAX_IMG-1)):[];const lib=await libraryFor(st);
      return submit(id,user,plan,used,{source:"media",audio_file:st.audio||"",video_file:st.video||"",url:st.url||"",link_mode:st.url?st.link:"audio",images:imgs,script:st.script||"",start:Number(st.start)||0,dur:Math.min(st.dur,plan.cap),move:!!st.move,ar:st.ar,vig:!!st.vig,logo:lib.logo,bgs:lib.bgs},mediaView(st,plan,n).kb,edit);
    }else return;
    await rwSet(user,st);const v=mediaView(st,plan,n);return edit(v.text,v.kb);
  }
  // ----- Quran reels -----
  if(!st.surah)return send(id,"⌛ الجلسة دي انتهت. ابدأ من جديد من /reel");
  if(d.startsWith("rr:")){
    const k=d.slice(3);if(!RNAME[k])return;
    Object.assign(st,{step:"settings",reciter:k,dur:Math.min(30,plan.cap),ar:st.ar||"9:16",vig:st.vig??true,fit:false,sig:await sigGet(user)});
    st.est=await estimate(st,plan.cap).catch(()=>null);
    if(st.to&&st.est&&st.est.length===st.to-st.frm+1&&st.est.reduce((x,y)=>x+y,0)>st.dur)st.fit=true;   // default: read every requested ayah
    await rwSet(user,st);const v=quranView(st,plan);return edit(v.text,v.kb);
  }
  if(!st.reciter)return;
  if(d.startsWith("rs:d:")){const n=Number(d.slice(5));if(!(n>0&&n<=plan.cap))return;st.dur=n;st.fit=false}
  else if(d==="rs:f"){if(!st.to)return;st.fit=true}
  else if(d==="rs:g"){st.await="sig";st.mid=mid;await rwSet(user,st);return edit(SIG_HELP,sigKb)}
  else if(d==="rs:g0"){st.sig="";delete st.await;await cmd("DEL",sigKey(user)).catch(()=>{})}
  else if(d==="rs:gb"){delete st.await}
  else if(d==="rs:a:916")st.ar="9:16";else if(d==="rs:a:169")st.ar="16:9";else if(d==="rs:v")st.vig=!st.vig;
  else if(d==="rs:go"){
    const lib=await libraryFor(st);
    return submit(id,user,plan,used,{source:"quran",surah:st.surah,frm:st.frm,to:st.to||null,reciter:st.reciter,maxdur:st.fit?plan.cap:Math.min(st.dur,plan.cap),ar:st.ar,vig:!!st.vig,sig:st.sig||"",logo:lib.logo,bgs:lib.bgs},quranView(st,plan).kb,edit);
  }else return;
  await rwSet(user,st);const v=quranView(st,plan);return edit(v.text,v.kb);
}
// "▶️ كمّل من آية …" under a finished Quran reel: rc:<surah>:<from>:<to|0>:<reciter>:<916|169>:<vig 0|1>
async function reelContinue(id,user,d){
  const [,s,a,to,k,o,vg]=d.split(":"),surah=Number(s),frm=Number(a),last=Number(to)||0;
  if(!(surah>=1&&surah<=114)||!(frm>=1&&frm<=AYAT[surah-1])||(last&&!(last>=frm&&last<=AYAT[surah-1]))||!RNAME[k])return;
  const g=await X.gate(user);if(g)return X.deny(id,g);
  const {plan,used}=await reelState(user);
  if(used>=plan.daily)return send(id,"⏳ استخدمت ريلز النهاردة ("+used+" من "+plan.daily+").\n"+planLine(plan)+"\n\n"+PLANS_TEXT+(await contactLine()));
  if(plan.freeFull)return send(id,"⏳ الريلز المجانية خلصت النهاردة لكل المستخدمين بسبب الضغط. ارجع بكرة، أو اشترك وتعمل لحد 10 ريلز يوميًا بأولوية.\n\n"+PLANS_TEXT+(await contactLine()));
  await rwDel(user);
  const st={step:"settings",surah,frm,to:last||null,reciter:k,dur:Math.min(30,plan.cap),ar:o==="169"?"16:9":"9:16",vig:vg!=="0",fit:false,sig:await sigGet(user)};
  st.est=await estimate(st,plan.cap).catch(()=>null);
  if(st.to&&st.est&&st.est.length===st.to-st.frm+1&&st.est.reduce((x,y)=>x+y,0)>st.dur)st.fit=true;
  await rwSet(user,st);const v=quranView(st,plan);return send(id,v.text,v.kb);
}
export const isReelCallback=d=>d==="reel:new"||/^(rr|rs|rk|rm|ms|rc):/.test(d);
