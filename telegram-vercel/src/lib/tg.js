const TOKEN=()=>process.env.TG_BOT_TOKEN;
export async function tg(method,payload={}){
  const r=await fetch("https://api.telegram.org/bot"+TOKEN()+"/"+method,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload),signal:AbortSignal.timeout(10000)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok||!j.ok){const e=Error("Telegram HTTP "+r.status+(j.description?": "+j.description:""));e.status=r.status;throw e}
  return j.result;
}
export const send=(id,text,kb)=>tg("sendMessage",{chat_id:id,text:String(text).slice(0,4096),disable_web_page_preview:true,...(kb?{reply_markup:kb}:{})});
let me;
export async function botId(){if(!me)me=(await tg("getMe")).id;return me}
export const sleep=ms=>new Promise(r=>setTimeout(r,ms));
