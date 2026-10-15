// Upstash Redis over REST (works from serverless, no TCP). Supports both env naming schemes.
const URL_=()=>process.env.UPSTASH_REDIS_REST_URL||process.env.KV_REST_API_URL||"";
const TOK=()=>process.env.UPSTASH_REDIS_REST_TOKEN||process.env.KV_REST_API_TOKEN||"";
export const redisOn=()=>!!(URL_()&&TOK());
export async function pipe(cmds){
  const r=await fetch(URL_().replace(/\/$/,"")+"/pipeline",{method:"POST",headers:{authorization:"Bearer "+TOK(),"content-type":"application/json"},body:JSON.stringify(cmds),signal:AbortSignal.timeout(4000)});
  if(!r.ok)throw Error("redis http "+r.status);
  const j=await r.json();
  return j.map(x=>{if(x&&x.error)throw Error("redis "+x.error);return x.result});
}
export async function cmd(...c){return (await pipe([c]))[0]}
export const obj=a=>{const o={};if(Array.isArray(a))for(let i=0;i<a.length;i+=2)o[a[i]]=a[i+1];return o};
