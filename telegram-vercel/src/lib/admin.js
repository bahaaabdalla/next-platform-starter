import {pipe,cmd,obj} from "./redis.js";
import {tg,send,botId,sleep} from "./tg.js";
import {P,today,monthKey,getCfg,account,describe,downloads} from "./quota.js";
import {NAME,SHORT,LONG,ADMIN_COMMANDS,COMMANDS} from "./texts.js";
const GB=1e9,fmtB=n=>n>=GB?(n/GB).toFixed(2)+" GB":(n/1e6).toFixed(1)+" MB";
const cleanChan=s=>{const m=String(s||"").trim().match(/^(?:https?:\/\/t\.me\/)?@?([A-Za-z][A-Za-z0-9_]{3,31})$/);return m?"@"+m[1]:""};

export async function statsText(){
  const d=today(),m=monthKey();
  const [users,act,dl,dlm,bw,chans]=await pipe([["SCARD",P+"users"],["SCARD",P+"act:"+d],["GET",P+"dl:"+d],["GET",P+"dlm:"+m],["GET",P+"bw:"+m],["SMEMBERS",P+"chans"]]);
  const c=await getCfg();
  return `📊 لوحة التحكم\n👥 المستخدمين: ${users||0}\n🔥 نشطين النهاردة: ${act||0}\n📥 تحميلات النهاردة: ${dl||0} · الشهر: ${dlm||0}\n🌐 ترافيك Render الشهر ده (تقديري): ${fmtB(Number(bw)||0)} من 5 GB\n\n⚙️ الإعدادات:\n• القناة الإجبارية: ${c.main||"غير محددة"}\n• قنوات المكافأة: ${(chans||[]).length}\n• هدية البداية: ${c.welcome} · يوميًا: ${c.daily} · مكافأة القناة: +${c.bonus}\n• الصيانة: ${c.maint?"🔴 شغالة":"🟢 مقفولة"}`;
}
export const panelKb=maint=>({inline_keyboard:[[{text:"🔄 تحديث",callback_data:"adm:refresh"},{text:maint?"🟢 إيقاف الصيانة":"🔴 تشغيل الصيانة",callback_data:"adm:maint"}],[{text:"📣 القنوات",callback_data:"adm:chans"},{text:"🌐 الترافيك",callback_data:"adm:bw"}]]});
export async function panel(id){const c=await getCfg();return send(id,await statsText(),panelKb(c.maint))}

async function checkChannel(ch){
  try{
    const bot=await botId(),chat=await tg("getChat",{chat_id:ch}),me=await tg("getChatMember",{chat_id:ch,user_id:bot});
    if(!["administrator","creator"].includes(me.status))return {ok:false,msg:"⚠️ البوت مش أدمن في "+ch+". ضيفه أدمن الأول (صلاحية عرض الأعضاء كفاية) وجرّب تاني."};
    return {ok:true,title:chat.title};
  }catch(e){return {ok:false,msg:"⚠️ مش قادر أوصل للقناة "+ch+". اتأكد إنها عامة وإن البوت أدمن فيها."}}
}
export async function channelsText(){
  const [c,chans]=[await getCfg(),await cmd("SMEMBERS",P+"chans")];
  return `📣 القنوات\n• الإجبارية: ${c.main||"غير محددة"}\n• قنوات المكافأة (${chans.length}):\n${chans.map(x=>"  "+x).join("\n")||"  (لا يوجد)"}\n\n/setmain @name · /addchannel @name · /delchannel @name`;
}
export async function bwText(){
  const m=monthKey(),bw=Number(await cmd("GET",P+"bw:"+m))||0;
  return `🌐 الترافيك الخارج من Render خلال ${m}: ${fmtB(bw)}\nالمجاني: 5 GB (الزيادة 0.15$ لكل GB).\nالرقم تقديري من اللي البوت أرسله؛ قارنه بلوحة Render.\nهيجيلك تنبيه عند 4.5 GB.`;
}
const uid=s=>/^\d{5,15}$/.test(String(s||""))?String(s):"";
async function setup(id){
  const out=[];
  const run=async(n,f)=>{try{await f();out.push("✅ "+n)}catch(e){out.push("❌ "+n+" ("+e.message.slice(0,60)+")")}};
  await run("الاسم",()=>tg("setMyName",{name:NAME}));
  await run("الوصف الطويل",()=>tg("setMyDescription",{description:LONG}));
  await run("الوصف المختصر",()=>tg("setMyShortDescription",{short_description:SHORT}));
  await run("أوامر المستخدمين",()=>tg("setMyCommands",{commands:COMMANDS}));
  await run("أوامر الأدمن",()=>tg("setMyCommands",{commands:ADMIN_COMMANDS,scope:{type:"chat",chat_id:Number(id)}}));
  return send(id,"⚙️ تطبيق إعدادات البوت:\n"+out.join("\n"));
}

// Returns true when the message was an admin command (handled).
export async function adminCommand(body,id){
  const [cmdName,...rest]=body.split(/\s+/);const name=cmdName.replace(/@\w+$/,"").toLowerCase(),a=rest;
  const arg=body.slice(cmdName.length).trim();
  switch(name){
  case "/admin":await panel(id);return true;
  case "/setup":await setup(id);return true;
  case "/stats":await send(id,await statsText());return true;
  case "/bw":await send(id,await bwText());return true;
  case "/channels":await send(id,await channelsText());return true;
  case "/user":{const u=uid(a[0]);if(!u){await send(id,"الاستخدام: /user 123456789");return true}
    const x=await account(u,await getCfg());
    if(!x.user.reg&&!x.user.terms){await send(id,"المستخدم ده لسه ماسجلش.");return true}
    await send(id,`👤 ${x.user.name||""} ${x.user.uname?"@"+x.user.uname:""}\nID: ${u}\nمسجّل: ${x.user.cdate||"-"}\nمحظور: ${x.banned?"نعم":"لا"} · غير محدود: ${x.unlimited?"نعم":"لا"}\nحد يومي مخصص: ${x.user.lim??"افتراضي"}\n${describe(x)}`);return true}
  case "/grant":{const u=uid(a[0]),n=Number(a[1]);if(!u||!Number.isFinite(n)){await send(id,"الاستخدام: /grant ID عدد_التحميلات");return true}
    await cmd("HINCRBY",P+"u:"+u,"wel",String(Math.round(n*3)));await send(id,"✅ تمت إضافة "+n+" تحميل للمستخدم "+u);return true}
  case "/ban":{const u=uid(a[0]);if(!u){await send(id,"الاستخدام: /ban ID");return true}await cmd("HSET",P+"u:"+u,"ban","1");await send(id,"⛔ تم حظر "+u);return true}
  case "/unban":{const u=uid(a[0]);if(!u){await send(id,"الاستخدام: /unban ID");return true}await cmd("HSET",P+"u:"+u,"ban","0");await send(id,"✅ تم فك الحظر عن "+u);return true}
  case "/unlimited":{const u=uid(a[0]),on=(a[1]||"on").toLowerCase()!=="off";if(!u){await send(id,"الاستخدام: /unlimited ID on|off");return true}await cmd("HSET",P+"u:"+u,"unl",on?"1":"0");await send(id,(on?"♾ تم منح استخدام غير محدود لـ ":"تم إلغاء الاستخدام غير المحدود عن ")+u);return true}
  case "/limit":case "/welcome":case "/bonus":{const n=Number(a[0]);if(!Number.isInteger(n)||n<0||n>1000){await send(id,"الاستخدام: "+name+" رقم");return true}
    const f={"/limit":"daily","/welcome":"welcome","/bonus":"bonus"}[name];await cmd("HSET",P+"cfg",f,String(n));
    await send(id,"✅ تم الضبط: "+({daily:"الحد اليومي",welcome:"هدية البداية",bonus:"مكافأة القناة"})[f]+" = "+n+" تحميل"+(f==="welcome"?" (يسري على المستخدمين الجدد فقط)":""));return true}
  case "/maint":{const on=(a[0]||"").toLowerCase()==="on";if(!["on","off"].includes((a[0]||"").toLowerCase())){await send(id,"الاستخدام: /maint on|off");return true}await cmd("HSET",P+"cfg","maint",on?"1":"0");await send(id,on?"🔴 وضع الصيانة شغال: المستخدمين مش هيقدروا يحمّلوا.":"🟢 تم إيقاف الصيانة.");return true}
  case "/addchannel":case "/setmain":{const ch=cleanChan(a[0]);if(!ch){await send(id,"الاستخدام: "+name+" @channel_username");return true}
    const r=await checkChannel(ch);if(!r.ok){await send(id,r.msg);return true}
    if(name==="/setmain"){await cmd("HSET",P+"cfg","main",ch);await send(id,"✅ القناة الإجبارية الآن: "+ch+" ("+r.title+")\nأي مستخدم لازم يشترك فيها عشان يستخدم البوت.")}
    else{await cmd("SADD",P+"chans",ch);await send(id,"✅ تمت إضافة "+ch+" ("+r.title+") لقنوات المكافأة.")}
    return true}
  case "/delchannel":{const ch=cleanChan(a[0]);if(!ch){await send(id,"الاستخدام: /delchannel @channel");return true}
    const c=await getCfg();await cmd("SREM",P+"chans",ch);if(c.main.toLowerCase()===ch.toLowerCase())await cmd("HDEL",P+"cfg","main");await send(id,"🗑 تم حذف "+ch);return true}
  case "/broadcast":{if(!arg){await send(id,"الاستخدام: /broadcast نص الرسالة");return true}
    const ids=await cmd("SMEMBERS",P+"users");let ok=0,fail=0,i=0;const end=Date.now()+48000;
    for(;i<ids.length&&Date.now()<end;i++){try{await send(ids[i],arg);ok++}catch{fail++}await sleep(40)}
    await send(id,`📣 تم الإرسال: ${ok} · فشل: ${fail}`+(i<ids.length?` · متبقي ${ids.length-i} (الوقت خلص، أعد الإرسال لاحقًا)`:""));return true}
  }
  return false;
}
export async function adminCallback(d,id){
  if(d==="adm:refresh"||d==="adm:maint"){
    if(d==="adm:maint"){const c=await getCfg();await cmd("HSET",P+"cfg","maint",c.maint?"0":"1")}
    return panel(id);
  }
  if(d==="adm:chans")return send(id,await channelsText());
  if(d==="adm:bw")return send(id,await bwText());
}
