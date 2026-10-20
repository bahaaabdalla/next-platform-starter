// Subscription requests: the user pays by Vodafone Cash / InstaPay, sends the sender number + a screenshot,
// and every admin gets the request with Approve / Reject buttons. Approving activates the paid plan for 30 days
// (stacked on top of any remaining days). Nothing is charged by the bot itself.
import {send,tg} from "./tg.js";
import {cmd,pipe,obj} from "./redis.js";
import {P,ADMINS,PLANS,getCfg,mins} from "./quota.js";
import {purchase} from "./capi.js";

const TTL=900,DAYS=30;
const k=u=>P+"pay:"+u;
const day=t=>new Date(Number(t)).toLocaleDateString("en-CA",{timeZone:"Africa/Cairo"});
const esc=s=>String(s||"").replace(/[<>&]/g,"");

export async function payInfo(){const c=await getCfg();return {num:c.paynum||"",price:c.price||"50",insta:c.payinsta||""}}

export async function payStart(id,user){
  const {num,price,insta}=await payInfo();
  if(!num&&!insta)return send(id,"💳 الاشتراك بيتفعل يدوي حاليًا. كلّم الإدارة: "+((await getCfg()).contact||"من /help"));
  const open=await cmd("GET",P+"payopen:"+user);
  if(open)return send(id,"⏳ عندك طلب اشتراك قيد المراجعة بالفعل. هيوصلك إشعار أول ما يتراجع.");
  await cmd("SET",k(user),JSON.stringify({step:"phone"}),"EX",String(TTL));
  return send(id,"💎 الاشتراك في الباقة المميزة — "+price+" جنيه / 30 يوم\n\n"+
    "1️⃣ حوّل "+price+" جنيه على:\n"+(num?"📱 فودافون كاش: "+num+"\n":"")+(insta?"🏦 InstaPay: "+insta+"\n":"")+
    "\n2️⃣ ابعتلي هنا رقم الموبايل اللي حوّلت منه.\n3️⃣ وبعده صورة (سكرين) التحويل.\n\nوهيتفعل اشتراكك أول ما يتراجع (عادة خلال ساعات).\nبالاشتراك إنت موافق على شروط الاستخدام /terms",
    {inline_keyboard:[[{text:"✖️ إلغاء",callback_data:"pay:x"}]]});
}

// Returns true when the message belonged to an open payment request.
export async function payMessage(m,id,user,body){
  const raw=await cmd("GET",k(user));if(!raw)return false;
  const st=JSON.parse(raw);
  if(body==="/cancel"){await cmd("DEL",k(user));await send(id,"تم إلغاء طلب الاشتراك.");return true}
  const cancelKb={inline_keyboard:[[{text:"✖️ إلغاء الاشتراك",callback_data:"pay:x"}]]};
  // a user who changed their mind must never get stuck: the second unrelated message cancels the request
  // and is handled normally (a link, a surah name, ...)
  const strike=async()=>{if((st.miss||0)>=1){await cmd("DEL",k(user));await send(id,"تم إلغاء طلب الاشتراك. تقدر ترجعله في أي وقت من /subscribe");return false}
    st.miss=(st.miss||0)+1;await cmd("SET",k(user),JSON.stringify(st),"EX",String(TTL));return true};
  if(st.step==="phone"){
    const ph=body.replace(/[\s-]/g,"").replace(/[٠-٩]/g,d=>"٠١٢٣٤٥٦٧٨٩".indexOf(d)).replace(/^\+?20/,"0");
    if(!/^01[0125]\d{8}$/.test(ph)){if(!(await strike()))return false;await send(id,"⚠️ ابعت رقم الموبايل اللي حوّلت منه (11 رقم يبدأ بـ 01).\nمش عايز تشترك دلوقتي؟ دوس إلغاء.",cancelKb);return true}
    await cmd("SET",k(user),JSON.stringify({step:"shot",phone:ph}),"EX",String(TTL));
    await send(id,"✅ تمام. ابعت دلوقتي صورة سكرين التحويل.",cancelKb);return true;
  }
  if(st.step==="shot"){
    const photo=m.photo?.length?m.photo[m.photo.length-1].file_id:(m.document&&/^image\//.test(m.document.mime_type||"")?m.document.file_id:"");
    if(!photo){if(!(await strike()))return false;await send(id,"📸 محتاج صورة سكرين التحويل (صورة مش نص).\nمش عايز تكمل؟ دوس إلغاء.",cancelKb);return true}
    const rid=Date.now().toString(36)+Math.random().toString(36).slice(2,6);
    const who=[m.from?.first_name,m.from?.last_name].filter(Boolean).join(" ");
    await pipe([["HSET",P+"payreq:"+rid,"user",String(user),"phone",st.phone,"photo",photo,"name",who,"uname",m.from?.username||"","ts",String(Date.now()),"status","pending"],
      ["EXPIRE",P+"payreq:"+rid,String(90*86400)],["LPUSH",P+"payq",rid],["LTRIM",P+"payq","0","499"],["SET",P+"payopen:"+user,rid,"EX",String(3*86400)],["DEL",k(user)]]);
    const {price}=await payInfo();
    const cap="💳 طلب اشتراك جديد\n👤 "+esc(who)+(m.from?.username?" @"+m.from.username:"")+"\n🆔 "+user+"\n📱 حوّل من: "+st.phone+"\n💰 المطلوب: "+price+" جنيه\n🕒 "+new Date().toLocaleString("en-GB",{timeZone:"Africa/Cairo"});
    const kb={inline_keyboard:[[{text:"✅ قبول (30 يوم)",callback_data:"pr:a:"+rid},{text:"❌ رفض",callback_data:"pr:r:"+rid}]]};
    const msgs=[];
    for(const a of ADMINS()){try{const r=await tg("sendPhoto",{chat_id:Number(a),photo,caption:cap,reply_markup:kb});msgs.push(a+":"+r.message_id)}catch(e){console.error("pay notify",e.message)}}
    await cmd("HSET",P+"payreq:"+rid,"cap",cap,"msgs",msgs.join(",")).catch(()=>{});
    await send(id,"📨 وصل طلبك وهيتراجع قريب. هيوصلك إشعار هنا أول ما يتفعل. شكرًا 💙");
    return true;
  }
  return false;
}

// Decide a request (from the Telegram buttons or the web panel). Returns {ok,note}.
export async function decideRequest(rid,act,adminId){
  if(!/^[a-z0-9]{6,20}$/.test(rid||"")||!["a","r"].includes(act))return {ok:false,note:"طلب غير صالح"};
  const r=obj(await cmd("HGETALL",P+"payreq:"+rid));if(!r.user)return {ok:false,note:"الطلب مش موجود"};
  if(r.status!=="pending")return {ok:false,note:"الطلب اتراجع قبل كده ("+(r.status==="approved"?"مقبول":"مرفوض")+")"};
  // claim it atomically so two admins (or the bot + the web panel) can't both decide
  if((await cmd("SET",P+"paylock:"+rid,"1","NX","EX","30"))!=="OK")return {ok:false,note:"جاري التنفيذ…"};
  const u=r.user;let note;
  if(act==="a"){
    const cur=obj(await cmd("HGETALL",P+"u:"+u)),base=Math.max(Date.now(),cur.plan==="pro"?Number(cur.pexp)||0:0),exp=base+DAYS*86400000;
    const {price}=await payInfo();
    await pipe([["HSET",P+"u:"+u,"plan","pro","pexp",String(exp)],["HSET",P+"payreq:"+rid,"status","approved","by",String(adminId),"at",String(Date.now()),"exp",String(exp),"amount",String(price)],
      ["DEL",P+"payopen:"+u],["HINCRBY",P+"paystats","approved","1"],["HINCRBY",P+"srcpaid",cur.src||"direct","1"],["HINCRBY",P+"rev",new Date().toLocaleDateString("en-CA",{timeZone:"Africa/Cairo"}).slice(0,7),String(Number(price)||0)]]);
    try{await purchase(u,price,r.phone,rid)}catch(e){console.error("capi buy",e.message)}
    const pc=await getCfg();
    await send(Number(u),"🎉 تم تفعيل "+PLANS.pro.name+" لحد "+day(exp)+".\n"+pc.pDaily+" ريلز يوميًا، لحد "+mins(pc.pCap)+"، بجودة 1080 ومن غير علامة مائية"+(pc.packOn?"، وزرار «⚡ ريلز جاهزة» بقى مفتوح لك":"")+". ابدأ من /reel").catch(()=>{});
    note="✅ اتقبل · الاشتراك لحد "+day(exp);
  }else{
    await pipe([["HSET",P+"payreq:"+rid,"status","rejected","by",String(adminId),"at",String(Date.now())],["DEL",P+"payopen:"+u],["HINCRBY",P+"paystats","rejected","1"]]);
    const c=await getCfg();
    await send(Number(u),"⚠️ ماقدرناش نأكد التحويل بتاع طلب الاشتراك."+(c.contact?"\nلو حوّلت فعلًا كلّم "+c.contact+" ومعاك سكرين التحويل.":"\nلو حوّلت فعلًا ابعت الطلب تاني من /plans.")).catch(()=>{});
    note="❌ اترفض";
  }
  // update the request photo in every admin chat (buttons removed)
  for(const x of String(r.msgs||"").split(",").filter(Boolean)){const [chat,mid]=x.split(":");
    try{await tg("editMessageCaption",{chat_id:Number(chat),message_id:Number(mid),caption:(r.cap||"💳 طلب اشتراك")+"\n\n"+note+" — بواسطة "+adminId})}catch{}}
  return {ok:true,note,legacy:!r.msgs,cap:r.cap};
}

// Admin buttons pr:a:<rid> / pr:r:<rid>. Returns a short toast text.
export async function payDecision(q,d,adminId){
  const [,act,rid]=d.split(":");const x=await decideRequest(rid,act,adminId);
  if(x.ok&&x.legacy)try{await tg("editMessageCaption",{chat_id:q.message.chat.id,message_id:q.message.message_id,caption:(q.message.caption||"")+"\n\n"+x.note+" — بواسطة "+adminId})}catch{}
  return x.note;
}

export async function sourcesText(){
  const [a,b]=await pipe([["HGETALL",P+"src"],["HGETALL",P+"srcpaid"]]);const s=obj(a),pd=obj(b);
  const keys=[...new Set([...Object.keys(s),...Object.keys(pd)])].sort((x,y)=>(Number(s[y])||0)-(Number(s[x])||0));
  if(!keys.length)return "📊 مفيش زيارات من روابط إعلانات لسه.\nاستخدم روابط بالشكل ده:\nt.me/BMEDIAHUB_BOT?start=fb1\nt.me/BMEDIAHUB_BOT?start=fb1_sub (يفتح الاشتراك مباشرة)";
  return "📊 مصادر المستخدمين (روابط الإعلانات)\n\n"+keys.slice(0,40).map(k=>"• "+k+" — دخول: "+(s[k]||0)+" · اشتراكات: "+(pd[k]||0)).join("\n");
}

export async function payStatsText(){
  const [ids,st]=await pipe([["LRANGE",P+"payq","0","49"],["HGETALL",P+"paystats"]]);
  const s=obj(st);let pend=0;const rows=[];
  if(ids?.length){const all=await pipe(ids.map(i=>["HMGET",P+"payreq:"+i,"status","user","phone","ts"]));
    all.forEach((x,i)=>{if(x&&x[0]==="pending"){pend++;if(rows.length<10)rows.push("• "+x[1]+" — "+x[2]+" — "+day(x[3]))}})}
  const {num,price,insta}=await payInfo();
  return "💳 الاشتراكات\nالسعر: "+price+" جنيه · فودافون كاش: "+(num||"مش متحدد")+(insta?" · InstaPay: "+insta:"")+
    "\nمقبول: "+(s.approved||0)+" · مرفوض: "+(s.rejected||0)+" · مستني مراجعة (آخر 50): "+pend+(rows.length?"\n\n"+rows.join("\n"):"")+
    "\n\nالأوامر: /paynum 01xxxxxxxxx · /payinsta اسم@instapay · /price 50";
}

// Any other button or command abandons a half-filled subscription request.
export async function payAbandon(user){try{await cmd("DEL",k(user))}catch{}}
