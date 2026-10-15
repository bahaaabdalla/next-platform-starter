import os,time,hmac,hashlib,uuid,subprocess,shutil,threading,re,json,zipfile
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
import httpx
from fastapi import FastAPI,Header,HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL
app=FastAPI(docs_url=None,redoc_url=None)
DATA=Path('/tmp/bmediahub');DATA.mkdir(parents=True,exist_ok=True)
jobs={};lock=threading.Lock();pool=ThreadPoolExecutor(max_workers=2)
hosts=('youtu.be','youtube.com','tiktok.com','instagram.com','facebook.com','fb.watch')
def check_url(url):
 try:
  u=urlsplit(url);h=(u.hostname or '').lower().strip('.')
  return u.scheme=='https' and not u.username and not u.password and len(url)<1800 and any(h==x or h.endswith('.'+x) for x in hosts)
 except: return False
def auth(v):
 k=os.getenv('MEDIA_WORKER_SECRET','')
 if not k or not v or not hmac.compare_digest(k,v):raise HTTPException(403,'Unauthorized')
def send(method,**kw):
 t=os.environ['TG_BOT_TOKEN']
 r=httpx.post('https://api.telegram.org/bot'+t+'/'+method,json=kw,timeout=30)
 if r.status_code>=400:
  try: detail=r.json().get('description','unknown')
  except: detail='unknown'
  print('telegram api failed',r.status_code,detail[:180],flush=True)
  r.raise_for_status()
def talk(id,txt,kb=None):
 try:
  kw=dict(chat_id=id,text=txt[:4096],disable_web_page_preview=True)
  if kb is not None:kw['reply_markup']=kb
  send('sendMessage',**kw)
 except Exception as e:print('telegram message error',type(e).__name__,flush=True)
def sig(j,exp):
 key=os.environ['DOWNLOAD_SIGNING_SECRET']
 return hmac.new(key.encode(),(j+':'+str(exp)).encode(),hashlib.sha256).hexdigest()
def link(j,exp):
 return os.environ['MEDIA_PUBLIC_BASE_URL'].rstrip('/')+'/download/'+j+'?until='+str(exp)+'&sig='+sig(j,exp)

def site(url):
 h=(urlsplit(url).hostname or '').lower()
 if 'youtube.com' in h or h=='youtu.be':return 'youtube'
 if 'instagram.com' in h:return 'instagram'
 if 'tiktok.com' in h:return 'tiktok'
 return 'facebook'

def run_limited(argv,timeout):
 try:
  return subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,check=False)
 except subprocess.TimeoutExpired:
  raise RuntimeError('timeout after '+str(timeout)+'s')

def probe(url,client=None):
 cmd=['yt-dlp','--dump-single-json','--skip-download','--no-playlist','--no-warnings','--socket-timeout','12','--extractor-retries','1','--retries','1']
 if client:cmd+=['--extractor-args','youtube:player_client='+client]
 cmd.append(url)
 p=run_limited(cmd,45)
 if p.returncode:raise RuntimeError((p.stderr or p.stdout)[-900:])
 try:return json.loads(p.stdout)
 except Exception:raise RuntimeError('yt-dlp returned invalid metadata')

def gallery_files(url,folder):
 folder.mkdir(parents=True,exist_ok=True)
 cmd=['gallery-dl','--config-ignore','--no-input','--no-mtime','--http-timeout','12','-R','1','--range','1-35','--filesize-max','20M','--filter',"extension in ('jpg','jpeg','png','webp','mp4','webm')",'-D',str(folder),url]
 p=run_limited(cmd,85)
 files=[f for f in folder.rglob('*') if f.is_file() and f.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.mp4','.webm') and f.stat().st_size<=20*1024*1024]
 files=sorted(files,key=lambda f:str(f))
 if not files:raise RuntimeError('gallery-dl: '+(p.stderr or p.stdout)[-650:])
 return files

def send_album(job,images):
 # Telegram accepts 2-10 photos in one media group.
 if not images:return
 for offset in range(0,min(len(images),30),10):
  batch=images[offset:offset+10]
  if len(batch)==1:
   with open(batch[0],'rb') as fh:
    with httpx.Client(timeout=httpx.Timeout(120,connect=20)) as c:
     rr=c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/sendPhoto',data={'chat_id':str(job['chat'])},files={'photo':(batch[0].name,fh,'image/jpeg')})
     rr.raise_for_status()
   continue
  with ExitStack() as stack:
   media=[];uploads={}
   for i,f in enumerate(batch):
    part='photo'+str(i)
    uploads[part]=(f.name,stack.enter_context(open(f,'rb')),'image/png' if f.suffix.lower()=='.png' else 'image/jpeg')
    media.append({'type':'photo','media':'attach://'+part})
   with httpx.Client(timeout=httpx.Timeout(150,connect=20)) as c:
    rr=c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/sendMediaGroup',data={'chat_id':str(job['chat']),'media':json.dumps(media)},files=uploads)
    rr.raise_for_status()

def prepare_file(job,f):
 exp=int(time.time())+3600
 # Store the completed media for a signed fallback link.
 job.update(state='complete',file=str(f),expires=exp)
 if f.stat().st_size<=45*1024*1024:
  try:
   upload(f,job)
   return
  except Exception as e:print('telegram file upload failed',type(e).__name__,flush=True)
 talk(job['chat'],'🔗 الملف جاهز. رابط تحميل صالح لمدة ساعة:\n'+link(job['id'],exp))

def deliver_gallery(job,url):
 folder=DATA/job['id']
 files=gallery_files(url,folder/'gallery')
 images=[f for f in files if f.suffix.lower() in ('.jpg','.jpeg','.png','.webp')]
 if images:
  # Preserve each picture individually and offer all pictures as an archive.
  talk(job['chat'],'🖼 تم استخراج '+str(len(images))+' صورة. جاري إرسال الصور ونسخة ZIP…')
  archive=folder/'photos.zip'
  with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
   for i,f in enumerate(images):z.write(f,arcname=str(i+1).zfill(2)+f.suffix.lower())
  try:send_album(job,images)
  except Exception as e:print('album upload failed',type(e).__name__,str(e)[:130],flush=True)
  prepare_file(job,archive)
  return
 videos=[f for f in files if f.suffix.lower() in ('.mp4','.webm')]
 if videos:
  talk(job['chat'],'🎬 تم استخراج الفيديو عن طريق المسار البديل. جاري إرساله…')
  prepare_file(job,max(videos,key=lambda f:f.stat().st_size))
  return
 raise RuntimeError('gallery-dl found no usable media')

def fail_reason(job,error):
 err=str(error).lower()
 p=site(job['url'])
 if 'timeout' in err:return '❌ انتهت مهلة الفحص. المنصة لم تستجب في الوقت المحدد، جرّب الرابط لاحقًا.'
 if p=='youtube' and ('not a bot' in err or 'sign in' in err):
  return '❌ YouTube يطلب تحققًا من عنوان سيرفر Render. الفيديو ليس بالضرورة خاصًا، والتجربة من سيرفر آخر قد تنجح.'
 if p=='instagram':
  return '❌ Instagram لم يسمح باستخراج الوسائط من هذا السيرفر، حتى بعد تجربة المسار البديل. تأكد أن الريل عام.'
 if p=='tiktok' and ('photo/' in job['url'] or 'unsupported' in err):
  return '❌ تعذر استخراج صور TikTok من الرابط. جرّب الرابط الكامل المنشور علنًا بدل الرابط المختصر.'
 if 'private' in err or 'login' in err:return '❌ قد يكون المحتوى خاصًا أو يتطلب تسجيل دخول.'
 return '❌ تعذر الوصول للوسائط من '+p+'. جرّب منشورًا عامًا آخر.'

def inspect(j):
 job=jobs[j];p=site(job['url'])
 try:
  job['id']=j
  # TikTok photo-mode is not a yt-dlp video. Handle it with gallery-dl.
  if p=='tiktok' and '/photo/' in job['url']:
   deliver_gallery(job,job['url']);return
  try:
   info=probe(job['url'])
  except Exception as e:
   if p=='tiktok' and ('unsupported url' in str(e).lower() or '/photo/' in str(e)):
    m=re.search(r'https://(?:www\.)?tiktok\.com/@[^/\s]+/photo/\d+',str(e))
    photo_url=m.group(0) if m else job['url']
    deliver_gallery(job,photo_url);return
   if p=='instagram':
    try:deliver_gallery(job,job['url']);return
    except Exception as e2:raise RuntimeError('instagram gallery fallback: '+str(e2)[:300]) from e
   if p=='youtube' and ('not a bot' in str(e).lower() or 'sign in' in str(e).lower()):
    last=e
    for client in ('tv,web_embedded','web_safari'):
     try:
      info=probe(job['url'],client)
      job['youtube_client']=client
      break
     except Exception as ex:last=ex
    else:raise last
   else:raise
  if not isinstance(info,dict) or info.get('_type') in ('playlist','multi_video'):raise ValueError('single video only')
  heights=sorted(set(int(f['height']) for f in info.get('formats',[]) if isinstance(f,dict) and f.get('vcodec') not in (None,'none') and isinstance(f.get('height'),(int,float)) and 144<=int(f['height'])<=2160))
  wanted=[v for v in (360,480,720,1080) if v in heights] or heights[-4:]
  job.update(state='ready',title=str(info.get('title') or 'Video')[:100],duration=info.get('duration'),thumbnail=info.get('thumbnail'),qualities=wanted)
  buttons=[[{'text':'📲 أفضل جودة في الشات','callback_data':'a:'+j+':chat:best'},{'text':'🔗 رابط خارجي','callback_data':'a:'+j+':link:best'}]]
  for v in wanted:buttons.append([{'text':str(v)+'p شات','callback_data':'a:'+j+':chat:'+str(v)},{'text':str(v)+'p رابط','callback_data':'a:'+j+':link:'+str(v)}])
  buttons.extend([[{'text':'🎵 MP3','callback_data':'a:'+j+':audio:best'},{'text':'🖼 الغلاف','callback_data':'a:'+j+':cover:best'}],
   [{'text':'🎞 GIF','callback_data':'a:'+j+':gif:best'},{'text':'📦 ضغط','callback_data':'a:'+j+':compress:best'},{'text':'WebM','callback_data':'a:'+j+':webm:best'}]])
  duration=int(job.get('duration') or 0)
  talk(job['chat'],'🎬 '+job['title']+'\n⏱ '+str(duration//60)+':'+str(duration%60).zfill(2)+'\nاختار الجودة والأداة:',{'inline_keyboard':buttons})
 except Exception as e:
  job['state']='failed'; print('inspect failure',p,type(e).__name__,str(e)[:330],flush=True)
  talk(job['chat'],fail_reason(job,e))

def upload(file,job):
 typ={'.mp3':'sendAudio','.gif':'sendAnimation','.webm':'sendDocument','.zip':'sendDocument'}.get(file.suffix,'sendVideo')
 key={'.mp3':'audio','.gif':'animation','.webm':'document','.zip':'document'}.get(file.suffix,'video')
 with open(file,'rb') as f:
  with httpx.Client(timeout=httpx.Timeout(180,connect=20)) as c:
   r=c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/'+typ,data={'chat_id':str(job['chat'])},files={key:(file.name,f)})
   r.raise_for_status()
def process(j):
 job=jobs[j];folder=DATA/j;folder.mkdir(exist_ok=True)
 try:
  talk(job['chat'],'⏳ جاري تجهيز الملف…')
  limit=120*1024*1024
  quality=job.get('quality','best')
  selector='bv*+ba/b' if quality=='best' else 'bv*[height<='+str(int(quality))+']+ba/b[height<='+str(int(quality))+']/b'
  def progress(d):
   if d.get('downloaded_bytes',0)>limit:raise ValueError('too large')
  cmd=['yt-dlp','--no-playlist','--no-warnings','--socket-timeout','15','--retries','1','--extractor-retries','1','--max-filesize','120M','--merge-output-format','mp4','--format',selector,'-o',str(folder/'video.%(ext)s')]
  if job.get('youtube_client'):cmd+=['--extractor-args','youtube:player_client='+job['youtube_client']]
  p=run_limited(cmd+[job['url']],200)
  if p.returncode:raise RuntimeError((p.stderr or p.stdout)[-500:])
  candidates=[p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4','.webm','.mkv')]
  if not candidates:raise ValueError('no media file')
  f=max(candidates,key=lambda p:p.stat().st_size)
  if f.stat().st_size>limit:raise ValueError('file too large')
  mode=job['mode']
  if mode in ('audio','gif','compress','webm'):
   ext={'audio':'.mp3','gif':'.gif','compress':'.mp4','webm':'.webm'}[mode]
   out=folder/('result'+ext)
   opt={'audio':['-vn','-codec:a','libmp3lame','-b:a','160k'],
        'gif':['-t','15','-vf','fps=10,scale=480:-1:flags=lanczos','-loop','0'],
        'compress':['-c:v','libx264','-preset','veryfast','-crf','30','-c:a','aac','-b:a','96k'],
        'webm':['-c:v','libvpx-vp9','-b:v','0','-crf','36','-deadline','realtime','-cpu-used','5','-c:a','libopus','-b:a','96k']}[mode]
   subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(f)]+opt+[str(out)],check=True,timeout=240,capture_output=True)
   f=out
  if f.stat().st_size>limit:raise ValueError('converted file too large')
  if mode!='link':
   prepare_file(job,f)
  else:
   exp=int(time.time())+3600;job.update(state='complete',file=str(f),expires=exp)
   talk(job['chat'],'🔗 رابط تنزيل صالح لمدة ساعة:\n'+link(j,exp))
 except Exception as e:
  print('processing failure',type(e).__name__,str(e)[:200],flush=True);job['state']='failed';talk(job['chat'],fail_reason(job,e))
class NewJob(BaseModel):
 url:str;chat_id:int;user_id:int;update_id:int|None=None
class Action(BaseModel):
 chat_id:int;user_id:int;mode:str;quality:str='best'
@app.get('/')
def health():return {'ok':True,'worker':'BMediaHub','free_tools':['download','audio','gif','compress','webm']}
@app.post('/jobs')
def create(p:NewJob,x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret)
 if not check_url(p.url) or p.chat_id!=p.user_id or p.chat_id<=0:raise HTTPException(422,'invalid input')
 with lock:
  for k,v in jobs.items():
   if p.update_id is not None and v.get('update_id')==p.update_id:return {'id':k,'created':False}
  count=sum(v['user']==p.user_id and v['state']!='failed' and v['created']>time.time()-86400 for v in jobs.values())
  if count>=10:raise HTTPException(429,'daily limit')
  j=uuid.uuid4().hex[:16];jobs[j]=dict(url=p.url,chat=p.chat_id,user=p.user_id,update_id=p.update_id,created=time.time(),state='preparing')
 pool.submit(inspect,j);return {'id':j,'created':True}
@app.post('/jobs/{j}/action')
def action(j:str,p:Action,x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret)
 with lock:
  job=jobs.get(j)
  if not job:raise HTTPException(404,'missing')
  if job['user']!=p.user_id or job['chat']!=p.chat_id:raise HTTPException(403,'wrong user')
  if p.mode=='cover':
   if not job.get('thumbnail'):raise HTTPException(422,'no cover')
   pool.submit(send,'sendPhoto',chat_id=p.chat_id,photo=job['thumbnail']);return {'ok':True}
  if p.mode not in ('chat','link','audio','gif','compress','webm') or (p.quality!='best' and not (p.quality.isdigit() and int(p.quality) in job.get('qualities',[]))):raise HTTPException(422,'invalid option')
  if job['state']!='ready':raise HTTPException(409,'not ready')
  job.update(state='processing',mode=p.mode,quality=p.quality)
 pool.submit(process,j);return {'ok':True}
@app.post('/stats')
def stats(x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret);return {'users':len(set(v['user'] for v in jobs.values())),'total_jobs':len(jobs),'completed':sum(v['state']=='complete' for v in jobs.values()),'failed':sum(v['state']=='failed' for v in jobs.values()),'today':sum(v['created']>time.time()-86400 for v in jobs.values())}
@app.get('/download/{j}')
def download(j:str,until:int=0,sig:str=''):
 job=jobs.get(j);now=int(time.time())
 if not job or until<now or until>now+7200 or not sig or not hmac.compare_digest(sig,sig_for(j,until)):raise HTTPException(403,'expired')
 file=Path(job.get('file',''))
 if job.get('expires')!=until or not file.is_file() or file.parent!=DATA/j:raise HTTPException(404,'missing')
 return FileResponse(file,filename='mediahub'+file.suffix,headers={'Cache-Control':'private, no-store','X-Robots-Tag':'noindex'})
def sig_for(j,until):return sig_fn(j,until)
def sig_fn(j,until):return sig(j,until)
