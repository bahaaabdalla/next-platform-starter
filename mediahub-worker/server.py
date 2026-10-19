import os,time,hmac,hashlib,uuid,subprocess,shutil,threading,re,json,zipfile,base64
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
import httpx
import reels,stock
from fastapi import FastAPI,Header,HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app=FastAPI(docs_url=None,redoc_url=None)
DATA=Path(os.getenv('MEDIA_DATA_DIR','/tmp/bmediahub'));DATA.mkdir(parents=True,exist_ok=True)
jobs={};lock=threading.Lock();pool=ThreadPoolExecutor(max_workers=2)
hosts=('youtu.be','youtube.com','tiktok.com','instagram.com','facebook.com','fb.watch')
PHOTO_EXT=('.jpg','.jpeg','.png','.webp');VIDEO_EXT=('.mp4','.webm')
DAILY_LIMIT=int(os.getenv('DAILY_LIMIT','100000'))   # per-user quotas are enforced by the Vercel front (Redis); this is only a safety net
USAGE_URL=os.getenv('USAGE_URL','https://bmediahub-telegram-bahaaabdallas-projects.vercel.app/api/usage')
JOB_FILES_TTL=2*3600   # downloaded files / photo buttons stay valid this long
JOB_META_TTL=86400     # light job records are kept for the daily quota window
IG_RE=re.compile(r'/(?:[A-Za-z0-9_.]+/)?(reels?|p|tv)/([A-Za-z0-9_-]+)')

# ---------- WARP egress (YouTube only) ----------
# YouTube rejects Render's datacenter IP. A free Cloudflare WARP identity exposed as a local SOCKS5 proxy
# (userspace WireGuard via wireproxy, IPv4 only) was measured to pass the bot check from Render.
WARP_ON=os.getenv('WARP_ENABLED','1')=='1'
WARP_PORT=1080
WARP_PROXY='socks5://127.0.0.1:%d'%WARP_PORT   # local DNS + -4 => tunnel only carries IPv4
WARP_DIR=Path('/tmp/warp')
WARP={'up':False,'proc':None}
YT_CLIENTS=(None,'mweb','android_vr')
RETRYABLE=('not a bot','sign in','403','forbidden','page needs to be reloaded','requested format','timeout','429','too many requests')

def warp_check():
 try:
  p=subprocess.run(['curl','-s','-4','-m','12','--proxy',WARP_PROXY,'https://www.cloudflare.com/cdn-cgi/trace'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=20)
  return 'warp=on' in p.stdout or 'warp=plus' in p.stdout
 except Exception:return False

def warp_stop():
 p=WARP.get('proc')
 if p:
  try:
   p.terminate();p.wait(5)
  except Exception:
   try:p.kill()
   except Exception:pass
 WARP['proc']=None;WARP['up']=False

def warp_build_conf():
 shutil.rmtree(WARP_DIR,ignore_errors=True);WARP_DIR.mkdir(parents=True)
 for argv in (['wgcf','register','--accept-tos'],['wgcf','generate']):
  r=subprocess.run(argv,cwd=WARP_DIR,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
  if r.returncode:return False
 lines=[]
 for line in (WARP_DIR/'wgcf-profile.conf').read_text().splitlines():
  if line.startswith('Address'):
   addrs=[a.strip() for a in line.split('=',1)[1].split(',') if ':' not in a]
   line='Address = '+', '.join(addrs)
  elif line.startswith('AllowedIPs'):line='AllowedIPs = 0.0.0.0/0'
  lines.append(line)
 (WARP_DIR/'wireproxy.conf').write_text('\n'.join(lines)+'\n[Socks5]\nBindAddress = 127.0.0.1:%d\n'%WARP_PORT)
 return True

def warp_supervisor():
 fails=0
 while WARP_ON:
  try:
   proc=WARP['proc']
   if proc is None or proc.poll() is not None or not WARP['up']:
    warp_stop()
    if warp_build_conf():
     WARP['proc']=subprocess.Popen(['wireproxy','-c',str(WARP_DIR/'wireproxy.conf')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
     time.sleep(6)
     WARP['up']=warp_check()
    fails=0 if WARP['up'] else fails+1
    print('warp state up=',WARP['up'],flush=True)
   else:WARP['up']=warp_check()
  except Exception as e:print('warp supervisor error',type(e).__name__,flush=True)
  time.sleep(60 if WARP['up'] else min(300,30*(fails+1)))

def yt_attempts():
 out=[(c,WARP_PROXY) for c in YT_CLIENTS] if WARP['up'] else []
 out.append((None,None))   # direct as the last resort
 return out

def retryable(err):
 e=str(err).lower();return any(k in e for k in RETRYABLE)

# ---------- instagram cookies (optional, from a secondary account) ----------
def load_ig_cookies():
 b=os.getenv('IG_COOKIES_B64','').strip()
 if not b:return
 try:
  f=DATA/'ig_cookies.txt';f.write_bytes(base64.b64decode(b));os.chmod(f,0o600)
 except Exception:print('ig cookies could not be decoded',flush=True)

def ig_cookie_file():
 f=DATA/'ig_cookies.txt'
 return str(f) if f.exists() else None

# ---------- helpers ----------
def check_url(url):
 try:
  u=urlsplit(url);h=(u.hostname or '').lower().strip('.')
  return u.scheme=='https' and not u.username and not u.password and len(url)<1800 and any(h==x or h.endswith('.'+x) for x in hosts)
 except: return False

def normalize(url):
 """Instagram links come with tracking params and /reels/ or /user/reel/ shapes; reduce them to /reel|p|tv/<code>/."""
 u=urlsplit(url);h=(u.hostname or '').lower()
 if h.endswith('instagram.com') and not u.path.startswith('/share/'):
  m=IG_RE.search(u.path)
  if m:return 'https://www.instagram.com/%s/%s/'%('reel' if m.group(1).startswith('reel') else m.group(1),m.group(2))
 return url

def auth(v):
 k=os.getenv('MEDIA_WORKER_SECRET','')
 if not k or not v or not hmac.compare_digest(k,v):raise HTTPException(403,'Unauthorized')
def report(job,kind,nbytes):
 """Tell the Vercel front what was delivered: credit is charged there (video once per job, photo per action) and outbound bytes are counted. Never blocks or fails a delivery."""
 def run():
  try:httpx.post(USAGE_URL,json={'user':job.get('user'),'job':job.get('id'),'kind':kind,'bytes':int(nbytes)},headers={'x-worker-secret':os.getenv('MEDIA_WORKER_SECRET','')},timeout=10)
  except Exception as e:print('usage report failed',type(e).__name__,flush=True)
 threading.Thread(target=run,daemon=True).start()
def charge_kind(job):return 'photo' if job.get('kind')=='photos' else 'video'

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

def ytdlp(url,extra,timeout,proxy=None,client=None,cookies=None):
 cmd=['yt-dlp','--no-playlist','--no-warnings','--socket-timeout','15','--retries','1','--extractor-retries','1']+extra
 if client:cmd+=['--extractor-args','youtube:player_client='+client]
 if proxy:cmd+=['--proxy',proxy,'-4']
 if cookies:cmd+=['--cookies',cookies]
 return run_limited(cmd+[url],timeout)

def probe(url,client=None,proxy=None,cookies=None):
 p=ytdlp(url,['--dump-single-json','--skip-download'],45,proxy,client,cookies)
 if p.returncode:raise RuntimeError((p.stderr or p.stdout)[-900:])
 try:return json.loads(p.stdout)
 except Exception:raise RuntimeError('yt-dlp returned invalid metadata')

def youtube_probe(job):
 last=None
 for client,proxy in yt_attempts():
  try:
   info=probe(job['url'],client,proxy);job['yt']=(client,proxy);return info
  except Exception as e:
   last=e;print('youtube attempt failed',client,bool(proxy),str(e)[:120].replace('\n',' '),flush=True)
   if not retryable(e):raise
 raise last

def gallery_files(url,folder,cookies=None):
 folder.mkdir(parents=True,exist_ok=True)
 cmd=['gallery-dl','--config-ignore','--no-input','--no-mtime','--http-timeout','12','-R','1','--range','1-35','--filesize-max','20M','--filter',"extension in ('jpg','jpeg','png','webp','mp4','webm')",'-D',str(folder)]
 if cookies:cmd+=['--cookies',cookies]
 p=run_limited(cmd+[url],85)
 files=[f for f in folder.rglob('*') if f.is_file() and f.suffix.lower() in PHOTO_EXT+VIDEO_EXT and f.stat().st_size<=20*1024*1024]
 files=sorted(files,key=lambda f:str(f))
 if not files:raise RuntimeError('gallery-dl: '+(p.stderr or p.stdout)[-650:])
 return files

# ---------- photos: panel + on-demand actions ----------
SHEET_TILE=280;SHEET_COLS=4;SHEET_MAX=12   # small compressed preview sheets: up to 12 numbered thumbnails each

def contact_sheet(files,first,dest):
 """One compressed JPEG with numbered thumbnails so the user can tell which image to pick."""
 from PIL import Image,ImageDraw,ImageFont,ImageOps
 T=SHEET_TILE;rows=(len(files)+SHEET_COLS-1)//SHEET_COLS
 sheet=Image.new('RGB',(SHEET_COLS*T,rows*T),(22,22,22))
 try:font=ImageFont.load_default(size=40)
 except TypeError:font=ImageFont.load_default()
 draw=ImageDraw.Draw(sheet)
 for i,f in enumerate(files):
  col,row=i%SHEET_COLS,i//SHEET_COLS;x,y=col*T,row*T
  try:
   with Image.open(f) as im:
    im=ImageOps.exif_transpose(im).convert('RGB');im.thumbnail((T-8,T-8));sheet.paste(im,(x+(T-im.width)//2,y+(T-im.height)//2))
  except Exception:pass
  draw.rectangle((x+4,y+4,x+66,y+56),fill=(0,0,0));draw.text((x+12,y+6),str(first+i),fill=(255,255,255),font=font)
 sheet.save(dest,'JPEG',quality=70,optimize=True)
 return dest

def tg_photo(job,path,caption,kb):
 with open(path,'rb') as fh:
  with httpx.Client(timeout=httpx.Timeout(60,connect=15)) as c:
   c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/sendPhoto',data={'chat_id':str(job['chat']),'caption':caption,'reply_markup':json.dumps(kb)},files={'photo':('preview.jpg',fh,'image/jpeg')}).raise_for_status()
 report(job,None,Path(path).stat().st_size)

def photo_previews(job):
 """Preview sheets, each followed by its own number buttons (tap a number = download that image)."""
 j=job['id'];files=job['photos'];folder=DATA/j;folder.mkdir(parents=True,exist_ok=True)
 for start in range(0,len(files),SHEET_MAX):
  chunk=files[start:start+SHEET_MAX];first=start+1
  sheet=contact_sheet(chunk,first,folder/('preview%02d.jpg'%(start//SHEET_MAX+1)))
  btns=[{'text':str(first+i),'callback_data':'a:'+j+':photo:'+str(first+i)} for i in range(len(chunk))]
  kb={'inline_keyboard':[btns[k:k+6] for k in range(0,len(btns),6)]}
  tg_photo(job,sheet,'👆 اضغط رقم الصورة اللي عايز تحمّلها ('+str(first)+'–'+str(first+len(chunk)-1)+')',kb)

def photo_panel(job):
 j=job['id'];n=len(job['photos'])
 if n==1:
  kb=[[{'text':'📥 تحميل الصورة','callback_data':'a:'+j+':photos:all'}]]
  talk(job['chat'],'🖼 تم العثور على صورة واحدة.',{'inline_keyboard':kb});return
 try:photo_previews(job)
 except Exception as e:
  print('preview failed',type(e).__name__,str(e)[:150],flush=True);photo_picker(job)
 kb=[[{'text':'📥 كل الصور ('+str(n)+')','callback_data':'a:'+j+':photos:all'}],
     [{'text':'📦 ملف ZIP','callback_data':'a:'+j+':photos:zip'}]]
 talk(job['chat'],'🖼 '+str(n)+' صورة. اختار صورة من المعاينة فوق، أو حمّل الكل:',{'inline_keyboard':kb})

def photo_picker(job):
 j=job['id'];n=len(job['photos']);rows=[];row=[]
 for i in range(1,n+1):
  row.append({'text':str(i),'callback_data':'a:'+j+':photo:'+str(i)})
  if len(row)==5:rows.append(row);row=[]
 if row:rows.append(row)
 talk(job['chat'],'🔢 اختار رقم الصورة (من 1 إلى '+str(n)+'):',{'inline_keyboard':rows})

def photo_zip(job):
 archive=DATA/job['id']/'photos.zip'
 archive.parent.mkdir(parents=True,exist_ok=True)
 if not archive.exists():
  with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
   for i,f in enumerate(job['photos']):z.write(f,arcname=str(i+1).zfill(2)+Path(f).suffix.lower())
 return archive

def photo_action(j,mode,quality):
 job=jobs.get(j)
 if not job or not job.get('photos'):return
 try:
  files=[Path(f) for f in job['photos']]
  if mode=='photos' and quality=='all':
   talk(job['chat'],'⏳ جاري إرسال الصور…');send_album(job,files)
  elif mode=='photos' and quality=='pick':
   try:photo_previews(job)
   except Exception:photo_picker(job)
  elif mode=='photos' and quality=='zip':
   talk(job['chat'],'⏳ جاري تجهيز ملف ZIP…');prepare_file(job,photo_zip(job))
  elif mode=='photo':send_single(job,files[int(quality)-1])
 except Exception as e:
  print('photo action failure',type(e).__name__,str(e)[:150],flush=True)
  talk(job['chat'],'❌ تعذر إرسال الصور. جرّب مرة أخرى.')

def present_media(job,files):
 images=[f for f in files if f.suffix.lower() in PHOTO_EXT]
 videos=[f for f in files if f.suffix.lower() in VIDEO_EXT]
 if images:
  job.update(state='ready',kind='photos',photos=[str(f) for f in images][:30])
  photo_panel(job)
  for v in videos[:3]:
   try:upload(v,job,kind='video')
   except Exception as e:print('mixed video upload failed',type(e).__name__,flush=True)
  return
 if videos:
  talk(job['chat'],'🎬 تم استخراج الفيديو. جاري إرساله…')
  prepare_file(job,max(videos,key=lambda f:f.stat().st_size));return
 raise RuntimeError('no usable media')

def deliver_gallery(job,url):
 cookies=ig_cookie_file() if site(url)=='instagram' else None
 present_media(job,gallery_files(url,DATA/job['id']/'gallery',cookies))

# ---------- instagram ----------
def instagram_graphql(job):
 """Read publicly accessible media metadata without Instagram account cookies (fragile, kept as a late fallback)."""
 match=IG_RE.search(urlsplit(job['url']).path)
 if not match:raise RuntimeError('unsupported Instagram URL')
 code=match.group(2)
 headers={
  'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
  'X-IG-App-ID':'936619743392459','Referer':'https://www.instagram.com/'}
 doc=os.getenv('IG_GRAPHQL_DOC_ID','27128499623469141')
 with httpx.Client(headers=headers,follow_redirects=False,timeout=httpx.Timeout(14,connect=7)) as client:
  client.get('https://www.instagram.com/')
  csrf=client.cookies.get('csrftoken','')
  if csrf:headers['X-CSRFToken']=csrf
  variables={'shortcode':code,'__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider':False}
  response=client.post('https://www.instagram.com/graphql/query',headers=headers,data={'doc_id':doc,'variables':json.dumps(variables,separators=(',',':'))})
  if response.status_code!=200:raise RuntimeError('Instagram GraphQL HTTP '+str(response.status_code))
  try:body=response.json()
  except Exception:raise RuntimeError('Instagram GraphQL non-JSON response')
  group=((body.get('data') or {}).get('xdt_api__v1__media__shortcode__web_info') or {}).get('items') or []
  if not group:raise RuntimeError('Instagram public GraphQL returned no media')
  item=group[0];medias=[]
  for element in (item.get('carousel_media') or [item])[:20]:
   versions=element.get('video_versions') or []
   if versions and versions[0].get('url'):medias.append(('video',versions[0]['url']))
   else:
    images=(element.get('image_versions2') or {}).get('candidates') or []
    if images and images[0].get('url'):medias.append(('image',images[0]['url']))
  if not medias:raise RuntimeError('Instagram public GraphQL returned no downloadable formats')
  folder=DATA/job['id']/'instagram';folder.mkdir(parents=True,exist_ok=True);paths=[]
  def trusted(h):return h.endswith('.fbcdn.net') or h.endswith('.cdninstagram.com') or h in ('fbcdn.net','cdninstagram.com')
  with httpx.Client(timeout=httpx.Timeout(35,connect=8),follow_redirects=True,headers={'User-Agent':headers['User-Agent']}) as media_client:
   for i,(kind,source) in enumerate(medias):
    if urlsplit(source).scheme!='https' or not trusted((urlsplit(source).hostname or '').lower()):raise RuntimeError('untrusted Instagram media host')
    target=folder/(str(i+1).zfill(2)+('.mp4' if kind=='video' else '.jpg'))
    with media_client.stream('GET',source) as stream:
     stream.raise_for_status()
     if not trusted((urlsplit(str(stream.url)).hostname or '').lower()):raise RuntimeError('untrusted Instagram media redirect')
     size=0
     with open(target,'wb') as fd:
      for chunk in stream.iter_bytes(chunk_size=131072):
       size+=len(chunk)
       if size>90*1024*1024:raise RuntimeError('Instagram media too large')
       fd.write(chunk)
    paths.append(target)
 present_media(job,paths)

def instagram_flow(job):
 """yt-dlp first (measured to work for public reels/posts from Render), then the older routes. Returns info for a single video or None when already delivered."""
 cookies=ig_cookie_file();errors=[]
 try:
  info=probe(job['url'],cookies=cookies)
  if isinstance(info,dict) and info.get('_type') not in ('playlist','multi_video'):return info
  errors.append('multi-item post')
 except Exception as e:errors.append(str(e)[:300])
 print('instagram yt-dlp route failed',errors[-1][:160].replace('\n',' '),flush=True)
 for name,fn in (('graphql',instagram_graphql),('gallery',lambda jb:deliver_gallery(jb,jb['url']))):
  try:fn(job);return None
  except Exception as e:
   errors.append(name+': '+str(e)[:200]);print('instagram',name,'route failed',str(e)[:140].replace('\n',' '),flush=True)
 raise RuntimeError('instagram routes failed: '+' | '.join(errors)[:700])

# ---------- delivery ----------
def send_single(job,f,count=True):
 f=Path(f);big=f.stat().st_size>9*1024*1024
 typ,key=('sendDocument','document') if big else ('sendPhoto','photo')
 with open(f,'rb') as fh:
  with httpx.Client(timeout=httpx.Timeout(120,connect=20)) as c:
   c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/'+typ,data={'chat_id':str(job['chat'])},files={key:(f.name,fh,'image/png' if f.suffix.lower()=='.png' else 'image/jpeg')}).raise_for_status()
 if count:report(job,'photo',f.stat().st_size)

def send_album(job,images):
 # Telegram accepts 2-10 photos per media group; oversized pictures go one by one as documents.
 images=[Path(f) for f in images][:30]
 small=[f for f in images if f.stat().st_size<=9*1024*1024]
 for f in images:
  if f not in small:send_single(job,f,count=False)
 for offset in range(0,len(small),10):
  batch=small[offset:offset+10]
  if len(batch)==1:send_single(job,batch[0],count=False);continue
  with ExitStack() as stack:
   media=[];uploads={}
   for i,f in enumerate(batch):
    part='photo'+str(i)
    uploads[part]=(f.name,stack.enter_context(open(f,'rb')),'image/png' if f.suffix.lower()=='.png' else 'image/jpeg')
    media.append({'type':'photo','media':'attach://'+part})
   with httpx.Client(timeout=httpx.Timeout(150,connect=20)) as c:
    c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/sendMediaGroup',data={'chat_id':str(job['chat']),'media':json.dumps(media)},files=uploads).raise_for_status()
 report(job,'photo',sum(f.stat().st_size for f in images))

def upload(file,job,kind=None):
 file=Path(file);big=file.stat().st_size>9*1024*1024
 typ={'.mp3':'sendAudio','.gif':'sendAnimation','.webm':'sendDocument','.zip':'sendDocument'}.get(file.suffix)
 if typ is None and file.suffix.lower() in PHOTO_EXT:typ='sendDocument' if big else 'sendPhoto'
 typ=typ or 'sendVideo'
 key={'sendAudio':'audio','sendAnimation':'animation','sendDocument':'document','sendPhoto':'photo'}.get(typ,'video')
 with open(file,'rb') as f:
  with httpx.Client(timeout=httpx.Timeout(180,connect=20)) as c:
   c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/'+typ,data={'chat_id':str(job['chat'])},files={key:(file.name,f)}).raise_for_status()
 report(job,kind or charge_kind(job),file.stat().st_size)

def prepare_file(job,f):
 f=Path(f);exp=int(time.time())+3600
 # Store the completed media for a signed fallback link. Photo jobs stay 'ready' so their buttons keep working.
 job.update(file=str(f),expires=exp)
 if job.get('kind')!='photos':job['state']='complete'
 if f.stat().st_size<=45*1024*1024:
  try:
   upload(f,job);return
  except Exception as e:print('telegram file upload failed',type(e).__name__,flush=True)
 talk(job['chat'],'🔗 الملف جاهز. رابط تحميل صالح لمدة ساعة:\n'+link(job['id'],exp));report(job,charge_kind(job),0)

def fail_reason(job,error):
 err=str(error).lower();p=site(job['url'])
 if 'too large' in err or 'larger than' in err:return '❌ الملف أكبر من الحد المسموح (120MB). جرّب جودة أقل.'
 if 'conversion failed' in err:return '❌ فشل تحويل الملف بالأداة دي. جرّب أداة تانية أو جودة أقل.'
 if 'timeout' in err:return '❌ انتهت مهلة الفحص. المنصة لم تستجب في الوقت المحدد، جرّب الرابط لاحقًا.'
 if p=='youtube':
  if 'not a bot' in err or 'sign in' in err:return '❌ YouTube رفض الطلب (حماية ضد الروبوتات) حتى بعد تجربة المسارات البديلة. جرّب مرة ثانية بعد دقيقة.'
  if '403' in err or 'forbidden' in err:return '❌ جوجل رفض تحميل الملف (403). جرّب مرة ثانية.'
  if 'private' in err or 'unavailable' in err:return '❌ الفيديو خاص أو غير متاح.'
 if p=='instagram':
  m='❌ تعذر استخراج هذا المنشور من Instagram. غالبًا المحتوى مقيّد أو يتطلب تسجيل دخول، أو منشور صور لا يدعمه المسار العام.'
  return m+('\n(جُرّب أيضًا بحساب مسجّل.)' if ig_cookie_file() else '')
 if p=='tiktok' and ('photo/' in job['url'] or 'unsupported' in err):
  return '❌ تعذر استخراج صور TikTok من الرابط. جرّب الرابط الكامل المنشور علنًا بدل الرابط المختصر.'
 if 'private' in err or 'login' in err:return '❌ قد يكون المحتوى خاصًا أو يتطلب تسجيل دخول.'
 return '❌ تعذر الوصول للوسائط من '+p+'. جرّب منشورًا عامًا آخر.'

def inspect(j):
 job=jobs[j];p=site(job['url'])
 try:
  job['id']=j
  # TikTok photo-mode is not a yt-dlp video. Handle it with gallery-dl and let the user choose what to download.
  if p=='tiktok' and '/photo/' in job['url']:
   deliver_gallery(job,job['url']);return
  if p=='instagram':
   info=instagram_flow(job)
   if info is None:return
  elif p=='youtube':info=youtube_probe(job)
  else:
   try:info=probe(job['url'])
   except Exception as e:
    if p=='tiktok' and ('unsupported url' in str(e).lower() or '/photo/' in str(e)):
     m=re.search(r'https://(?:www\.)?tiktok\.com/@[^/\s]+/photo/\d+',str(e))
     deliver_gallery(job,m.group(0) if m else job['url']);return
    raise
  if not isinstance(info,dict) or info.get('_type') in ('playlist','multi_video'):raise ValueError('single video only')
  heights=sorted(set(int(f['height']) for f in info.get('formats',[]) if isinstance(f,dict) and f.get('vcodec') not in (None,'none') and isinstance(f.get('height'),(int,float)) and 144<=int(f['height'])<=2160))
  wanted=[v for v in (360,480,720,1080) if v in heights] or heights[-4:]
  job.update(state='ready',probed=True,title=str(info.get('title') or 'Video')[:100],duration=info.get('duration'),thumbnail=info.get('thumbnail'),qualities=wanted,desc=str(info.get('description') or '').strip()[:3500])
  buttons=[[{'text':'📲 أفضل جودة في الشات','callback_data':'a:'+j+':chat:best'},{'text':'🔗 رابط خارجي','callback_data':'a:'+j+':link:best'}]]
  for v in wanted:buttons.append([{'text':str(v)+'p شات','callback_data':'a:'+j+':chat:'+str(v)},{'text':str(v)+'p رابط','callback_data':'a:'+j+':link:'+str(v)}])
  buttons.extend([[{'text':'🎵 MP3','callback_data':'a:'+j+':audio:best'},{'text':'🖼 الغلاف','callback_data':'a:'+j+':cover:best'}],
   [{'text':'🎞 GIF','callback_data':'a:'+j+':gif:best'},{'text':'📦 ضغط','callback_data':'a:'+j+':compress:best'},{'text':'WebM','callback_data':'a:'+j+':webm:best'}]])
  if job.get('desc'):buttons.append([{'text':'📝 الوصف / الكابشن','callback_data':'a:'+j+':caption:best'}])
  duration=int(job.get('duration') or 0)
  talk(job['chat'],'🎬 '+job['title']+'\n⏱ '+str(duration//60)+':'+str(duration%60).zfill(2)+'\nاختار الجودة والأداة:',{'inline_keyboard':buttons})
 except Exception as e:
  job['state']='failed'; print('inspect failure',p,type(e).__name__,str(e)[:330],flush=True)
  talk(job['chat'],fail_reason(job,e))

def fetch_video(job,folder,selector):
 p=site(job['url']);cookies=ig_cookie_file() if p=='instagram' else None
 if p=='youtube':
  first=job.get('yt');atts=([tuple(first)] if first else [])+[a for a in yt_attempts() if not first or a!=tuple(first)]
  atts=atts[:3]
 else:atts=[(None,None)]
 last=None
 for client,proxy in atts:
  r=ytdlp(job['url'],['--max-filesize','120M','--merge-output-format','mp4','--format',selector,'-o',str(folder/'video.%(ext)s')],200,proxy,client,cookies)
  if not r.returncode:return
  last=RuntimeError((r.stderr or r.stdout)[-500:])
  print('download attempt failed',p,client,bool(proxy),str(last)[:120].replace('\n',' '),flush=True)
  if not retryable(last):break
  for f in folder.glob('video.*'):f.unlink(missing_ok=True)
 raise last

def source_file(job):
 """The full video already downloaded for this job (kept for 2h so every tool can reuse it), or None."""
 s=job.get('src')
 return Path(s) if s and Path(s).is_file() else None

def process(j):
 job=jobs[j];folder=DATA/j;folder.mkdir(exist_ok=True)
 try:
  talk(job['chat'],'⏳ جاري تجهيز الملف…')
  limit=120*1024*1024
  mode=job['mode'];quality=job.get('quality','best')
  f=source_file(job)
  # Tools (MP3/GIF/compress/WebM) work on whatever video we already have; chat/link reuse it only when the quality matches.
  if not (f and (mode in ('audio','gif','compress','webm') or job.get('src_q')==quality)):
   for old in folder.glob('video.*'):old.unlink(missing_ok=True)
   selector='bv*+ba/b' if quality=='best' else 'bv*[height<='+str(int(quality))+']+ba/b[height<='+str(int(quality))+']/b'
   fetch_video(job,folder,selector)
   candidates=[p for p in folder.glob('video.*') if p.is_file() and p.suffix.lower() in ('.mp4','.webm','.mkv')]
   if not candidates:raise ValueError('file too large or no media file')
   f=max(candidates,key=lambda p:p.stat().st_size)
   if f.stat().st_size>limit:raise ValueError('file too large')
   job['src']=str(f);job['src_q']=quality
  if mode in ('audio','gif','compress','webm'):
   ext={'audio':'.mp3','gif':'.gif','compress':'.mp4','webm':'.webm'}[mode]
   out=folder/('result'+ext)
   opt={'audio':['-vn','-codec:a','libmp3lame','-b:a','160k'],
        'gif':['-t','15','-vf','fps=10,scale=480:-1:flags=lanczos','-loop','0'],
        'compress':['-c:v','libx264','-preset','veryfast','-crf','30','-c:a','aac','-b:a','96k'],
        'webm':['-c:v','libvpx-vp9','-b:v','0','-crf','36','-deadline','realtime','-cpu-used','5','-c:a','libopus','-b:a','96k']}[mode]
   try:subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(f)]+opt+[str(out)],check=True,timeout=240,capture_output=True)
   except subprocess.TimeoutExpired:raise RuntimeError('timeout after 240s')
   except subprocess.CalledProcessError as e:
    print('ffmpeg failed',mode,(e.stderr or b'')[-300:],flush=True);raise RuntimeError('conversion failed')
   f=out
  if f.stat().st_size>limit:raise ValueError('converted file too large')
  if mode!='link':
   prepare_file(job,f)
  else:
   exp=int(time.time())+3600;job.update(state='complete',file=str(f),expires=exp)
   talk(job['chat'],'🔗 رابط تنزيل صالح لمدة ساعة:\n'+link(j,exp));report(job,'video',0)
 except Exception as e:
  print('processing failure',type(e).__name__,str(e)[:200],flush=True)
  if not source_file(job):
   for old in folder.glob('video.*'):old.unlink(missing_ok=True)
  job['state']='ready' if job.get('probed') else 'failed'   # a failed tool must not lock the buttons; the user can retry
  talk(job['chat'],fail_reason(job,e))
 finally:
  job['busy']=False

# ---------- reel studio ----------
reel_pool=ThreadPoolExecutor(max_workers=1)   # one render at a time: the free CPU is tiny and downloads must stay responsive
REEL_QUEUE=int(os.getenv('REEL_QUEUE','4'))
reel_q=[];reel_lock=threading.Lock()

def tg_video(chat,path,caption,info,markup=None):
 data={'chat_id':str(chat),'caption':caption[:1024],'supports_streaming':'true','width':str(info['width']),'height':str(info['height']),'duration':str(int(info['duration']))}
 if markup:data['reply_markup']=json.dumps(markup,ensure_ascii=False)
 with open(path,'rb') as fh:
  with httpx.Client(timeout=httpx.Timeout(240,connect=20)) as c:
   c.post('https://api.telegram.org/bot'+os.environ['TG_BOT_TOKEN']+'/sendVideo',
    data=data,
    files={'video':('reel.mp4',fh,'video/mp4')}).raise_for_status()

TG_FILE_MAX=20*1024*1024   # Bot API getFile limit
def tg_file(file_id,dest):
 """Download a Telegram file by file_id (photos, voice notes, audio, small videos). Raises ValueError if it is too big."""
 t=os.environ['TG_BOT_TOKEN']
 r=httpx.post('https://api.telegram.org/bot'+t+'/getFile',json={'file_id':file_id},timeout=20)
 j=r.json() if r.headers.get('content-type','').startswith('application/json') else {}
 if not j.get('ok'):
  if 'too big' in str(j.get('description','')).lower():raise ValueError('file too big')
  raise RuntimeError('telegram getFile failed: '+str(j.get('description',''))[:80])
 fp=j['result']['file_path'];size=j['result'].get('file_size') or 0
 if size>TG_FILE_MAX:raise ValueError('file too big')
 dest=Path(dest).with_suffix(Path(fp).suffix or Path(dest).suffix)
 with httpx.stream('GET','https://api.telegram.org/file/bot'+t+'/'+fp,timeout=httpx.Timeout(60,connect=15)) as st:
  st.raise_for_status();n=0
  with open(dest,'wb') as fh:
   for ch in st.iter_bytes(131072):
    n+=len(ch)
    if n>TG_FILE_MAX+1024:raise ValueError('file too big')
    fh.write(ch)
 return dest

def cached_tg(file_id,folder):
 """Library backgrounds and the logo are reused across reels: keep them on disk (until the instance restarts)."""
 folder.mkdir(parents=True,exist_ok=True)
 key=hashlib.sha1(file_id.encode()).hexdigest()[:20]
 hit=[f for f in folder.glob(key+'.*') if f.stat().st_size>0]
 if hit:return hit[0]
 return tg_file(file_id,folder/(key+'.jpg'))

def fetch_link(url,folder,want_video):
 """Media from a public link for a reel: best audio only, or a <=720p video (with its audio). Same routes as downloads (WARP for YouTube)."""
 folder.mkdir(parents=True,exist_ok=True)
 p=site(url);cookies=ig_cookie_file() if p=='instagram' else None
 sel='bv*[height<=720]+ba/b[height<=720]/b' if want_video else 'ba/b'
 atts=yt_attempts()[:3] if p=='youtube' else [(None,None)]
 last=None
 for client,proxy in atts:
  for f in folder.glob('link.*'):f.unlink(missing_ok=True)
  r=ytdlp(url,['--max-filesize','80M','--format',sel]+(['--merge-output-format','mp4'] if want_video else [])+['-o',str(folder/'link.%(ext)s')],240,proxy,client,cookies)
  got=[f for f in folder.glob('link.*') if f.is_file() and not f.name.endswith('.part')]
  if not r.returncode and got:return max(got,key=lambda f:f.stat().st_size)
  last=RuntimeError('link download failed: '+(r.stderr or r.stdout)[-300:])
  if not retryable(last):break
 raise last or RuntimeError('link download failed')

def reel_fail_text(e,quran=True):
 m=str(e).lower()
 if 'too big' in m:return '❌ الملف أكبر من 20 ميجا (حد تيليجرام للبوتات). ابعته أصغر أو ابعت رابطه.'
 if 'audio too short' in m:return '❌ الصوت قصير جدًا أو مش واضح. ابعت ملف صوت تاني.'
 if 'link download failed' in m:return '❌ مقدرتش أجيب الميديا من الرابط ده. اتأكد إنه عام وجرّب تاني.'
 if isinstance(e,ValueError):return '❌ بيانات الريل مش صحيحة. جرّب تاني.' if not quran else '❌ بيانات الريل مش صحيحة (السورة أو الآيات). جرّب تاني.'
 if quran and ('everyayah' in m or 'http' in m or 'connect' in m or 'recitation' in m):return '❌ مقدرتش أجيب التلاوة دلوقتي. جرّب بعد شوية أو اختار قارئ تاني.'
 if 'timeout' in m:return '❌ الرندر أخد وقت أكتر من المسموح. جرّب مدة أقصر.'
 return '❌ حصلت مشكلة أثناء عمل الريل. جرّب تاني.'

def reel_assets(spec,work):
 """Library backgrounds (for this aspect ratio) and the logo, both shared Telegram files cached on disk."""
 lib=[]
 for fid in (spec.get('bgs') or [])[:8]:
  try:lib.append(cached_tg(fid,DATA/'bgcache'))
  except Exception as e:print('bg fetch failed',type(e).__name__,flush=True)
 logo=None
 if spec.get('logo') and spec.get('wm'):
  try:logo=cached_tg(spec['logo'],DATA/'bgcache')
  except Exception as e:print('logo fetch failed',type(e).__name__,flush=True)
 return lib,logo

def continue_markup(spec,info):
 """When the reel stopped before the requested last ayah (or the surah end), offer to carry on from the next one."""
 end=int(spec.get('to') or 0) or reels.ayah_count(int(info['surah']))
 nxt=int(info['to'])+1
 if nxt>end:return None
 d='rc:%d:%d:%d:%s:%s:%d'%(info['surah'],nxt,int(spec.get('to') or 0),spec['reciter'],'169' if spec.get('ar')=='16:9' else '916',1 if spec.get('vig',True) else 0)
 return {'inline_keyboard':[[{'text':'▶️ كمّل من آية '+str(nxt).translate(reels.AR_DIGITS),'callback_data':d}]]}

def quran_reel(spec,work,lib,logo,on_stage=None):
 """One Quran reel. Background: stock pool -> one live screened nature video -> library/placeholder stills."""
 clips=[];credit=[]
 if spec.get('bgsrc')=='stock' or (not lib and spec.get('bgsrc')!='img'):
  n=1 if spec.get('bgmode')=='static' else max(2,min(int(spec.get('bgn') or 4),8))
  orient='h' if spec.get('ar')=='16:9' else 'v'
  clips,credit=stock.pick(spec.get('theme') or 'mix',orient,n)
  if not clips:   # live: one fresh, screened video for the whole reel, deleted with the work folder
   v,src,band=stock.live_pick(spec.get('theme') or 'mix',orient,float(spec.get('maxdur') or 30),work,hd=bool(spec.get('hd')))
   if v:clips,credit=[v],[src];spec['dim']=reels.dim_for(band);spec['plate']=reels.plate_for(band)
  if clips:lib=[]
 out,info=reels.render_quran(spec,work,DATA/'qcache',on_stage=on_stage,library=lib,logo=logo,clips=clips)
 cap=reels.caption(info)+(('\n🎞️ الخلفية: '+' و'.join(credit)) if credit else '')
 return out,info,cap

def batch_job(rid,spec,work,lib,logo):
 """Ready-made pack: several Quran reels in one queue slot, each sent as soon as it is done and charged as one reel."""
 items=spec.get('items') or [];done=0
 talk(spec['chat_id'],'⚡ بدأت أجهّز %d ريلز جاهزة للنشر. هيوصلوا واحد ورا التاني.'%len(items))
 for i,it in enumerate(items):
  sub=dict(spec);sub.update(it);sub.pop('items',None);sub.pop('dim',None);sub.pop('plate',None);w=work/('b%d'%i)
  try:
   w.mkdir(parents=True,exist_ok=True);t0=time.time()
   out,info,cap=quran_reel(sub,w,list(lib),logo)
   tg_video(spec['chat_id'],out,'(%d/%d) '%(i+1,len(items))+cap,info,None)
   print('batch reel ok',rid,i,'surah',info['surah'],'dur',round(info['duration'],1),'total',round(time.time()-t0,1),flush=True)
   report({'user':spec['user_id'],'id':rid+'-'+str(i)},'reel',out.stat().st_size);done+=1
  except Exception as e:print('batch reel failure',rid,i,type(e).__name__,str(e)[:300],flush=True)
  finally:shutil.rmtree(w,ignore_errors=True)
 if done<len(items):talk(spec['chat_id'],'⚠️ اتعمل %d من %d. اللي ماتعملش مش متحسوب عليك.'%(done,len(items)))
 else:talk(spec['chat_id'],'✅ الريلز الـ %d جاهزين للنشر. انشرهم على مدار اليوم عشان كل واحد ياخد فرصته.'%len(items))

def reel_job(rid,spec):
 work=DATA/'reels'/rid;job={'user':spec['user_id'],'id':rid};quran=spec.get('source')=='quran'
 try:
  t0=time.time();work.mkdir(parents=True,exist_ok=True)
  lib,logo=reel_assets(spec,work);markup=None
  if quran and spec.get('items'):
   batch_job(rid,spec,work,lib,logo);return
  if quran:
   out,info,cap=quran_reel(spec,work,lib,logo,on_stage=lambda st:talk(spec['chat_id'],'🎬 بدأ تجهيز الفيديو…'))
   markup=continue_markup(spec,info)
  else:
   talk(spec['chat_id'],'🎬 بدأ تجهيز الفيديو…')
   src=work/'src';src.mkdir(exist_ok=True)
   audio=video=None;images=[]
   if spec.get('audio_file'):audio=tg_file(spec['audio_file'],src/'audio.bin')
   if spec.get('video_file'):video=tg_file(spec['video_file'],src/'video.mp4')
   if spec.get('url'):
    mode=spec.get('link_mode') or 'audio'
    got=fetch_link(spec['url'],src,want_video=mode in ('video','both'))
    if mode in ('audio','both') and not audio:audio=got
    if mode in ('video','both') and not video:video=got
   for i,fid in enumerate((spec.get('images') or [])[:20]):
    images.append(tg_file(fid,src/('img%02d.jpg'%i)))
   out,info=reels.render_media(spec,work,audio_src=audio,images=images,video_src=video,library=lib,logo=logo)
   for f in src.iterdir():f.unlink(missing_ok=True)
   cap='🎬 الريل جاهز'+(' · @BMEDIAHUB_BOT' if spec.get('wm') else '')
  tg_video(spec['chat_id'],out,cap,info,markup)
  print('reel ok',rid,spec.get('source'),'dur',round(info['duration'],1),'render',info['render_s'],'total',round(time.time()-t0,1),'bytes',out.stat().st_size,flush=True)
  report(job,'reel',out.stat().st_size)
 except Exception as e:
  print('reel failure',rid,type(e).__name__,str(e)[:300],flush=True);talk(spec['chat_id'],reel_fail_text(e,quran))
 finally:
  shutil.rmtree(work,ignore_errors=True)
  with reel_lock:
   if rid in reel_q:reel_q.remove(rid)
  reels.trim_cache(DATA/'qcache')

class ReelReq(BaseModel):
 chat_id:int;user_id:int;source:str='quran'
 surah:int=0;frm:int=0;to:int|None=None;reciter:str='';maxdur:float=30   # quran
 audio_file:str='';video_file:str='';url:str='';link_mode:str='audio';images:list[str]=[];script:str='';start:float=0;dur:float=30;move:bool=True   # media
 cap:float=60;ar:str='9:16';vig:bool=True;wm:str='';logo:str='';bgs:list[str]=[];sig:str='';bgmode:str='every';bgsec:float=5;bgn:int=4;bgsrc:str='';theme:str='mix';hd:bool=False;lines:int=0
 items:list[dict]=[]   # ready-made pack: [{surah,frm,to,reciter,theme}]

FID=re.compile(r'^[A-Za-z0-9_-]{10,200}$')
@app.post('/reels')
def reel_create(p:ReelReq,x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret)
 if p.chat_id!=p.user_id or p.chat_id<=0 or p.source not in ('quran','media') or p.ar not in reels.SIZES:raise HTTPException(422,'invalid input')
 spec=p.model_dump()
 spec['cap']=max(5,min(float(spec['cap']),600));spec['wm']=str(spec['wm'])[:40];spec['sig']=reels.clean_sig(spec['sig']) or ''
 if spec['bgmode'] not in ('every','static','ayah'):spec['bgmode']='every'
 spec['bgsec']=max(2.0,min(float(spec['bgsec']),30.0));spec['bgn']=max(1,min(int(spec['bgn']),8))
 spec['bgs']=[x for x in spec['bgs'] if FID.match(x)][:8]
 if spec['bgsrc'] not in ('','stock','lib','img'):spec['bgsrc']=''
 if spec['theme'] not in stock.THEMES:spec['theme']='mix'
 spec['lines']=max(0,min(int(spec['lines']),3))
 if spec['logo'] and not FID.match(spec['logo']):spec['logo']=''
 if p.source=='quran':
  spec['maxdur']=max(5,min(float(spec['maxdur']),spec['cap']))
  if spec['items']:
   clean=[]
   for it in spec['items'][:5]:
    x={'surah':int(it.get('surah') or 0),'frm':int(it.get('frm') or 0),'to':int(it.get('to') or 0) or None,'reciter':str(it.get('reciter') or ''),'theme':str(it.get('theme') or 'mix'),'maxdur':max(5.0,min(float(it.get('maxdur') or spec['maxdur']),spec['cap']))}
    if x['theme'] not in stock.THEMES:x['theme']='mix'
    try:reels.validate({**spec,**x})
    except Exception:raise HTTPException(422,'invalid reel')
    clean.append(x)
   spec['items']=clean;spec.update({k:clean[0][k] for k in ('surah','frm','to','reciter')})
  try:reels.validate(spec)
  except Exception:raise HTTPException(422,'invalid reel')
 else:
  spec['dur']=max(3,min(float(spec['dur']),spec['cap']));spec['start']=max(0,min(float(spec['start']),3600))
  spec['images']=[x for x in spec['images'] if FID.match(x)][:20];spec['script']=str(spec['script'])[:1500]
  for k in ('audio_file','video_file'):
   if spec[k] and not FID.match(spec[k]):raise HTTPException(422,'invalid file')
  if spec['url'] and not check_url(spec['url']):raise HTTPException(422,'invalid url')
  if spec['link_mode'] not in ('audio','video','both'):spec['link_mode']='audio'
  if not (spec['audio_file'] or spec['video_file'] or spec['url'] or spec['images']):raise HTTPException(422,'nothing to render')
 with reel_lock:
  if any(r.startswith(str(p.user_id)+'-') for r in reel_q):raise HTTPException(409,'busy')
  if len(reel_q)>=REEL_QUEUE:raise HTTPException(429,'queue full')
  rid=str(p.user_id)+'-'+uuid.uuid4().hex[:10];reel_q.append(rid);pos=len(reel_q)
 reel_pool.submit(reel_job,rid,spec)
 return {'ok':True,'id':rid,'position':pos}

# ---------- housekeeping ----------
def janitor_pass(now=None):
 now=now or time.time()
 with lock:
  for j,v in list(jobs.items()):
   age=now-v['created']
   if age>JOB_META_TTL:
    shutil.rmtree(DATA/j,ignore_errors=True);del jobs[j]
   elif age>JOB_FILES_TTL and not v.get('expired'):
    shutil.rmtree(DATA/j,ignore_errors=True)
    v['expired']=True
    if v.get('state')!='failed':v['state']='expired'
    for k in ('file','src','photos','thumbnail','qualities','desc'):v.pop(k,None)

def janitor():
 while True:
  time.sleep(600)
  try:janitor_pass()
  except Exception as e:print('janitor error',type(e).__name__,flush=True)

# ---------- API ----------
class NewJob(BaseModel):
 url:str;chat_id:int;user_id:int;update_id:int|None=None
class Action(BaseModel):
 chat_id:int;user_id:int;mode:str;quality:str='best'
@app.get('/')
@app.head('/')
def health():return {'ok':True,'worker':'BMediaHub','warp':WARP['up'],'free_tools':['download','audio','gif','compress','webm','photos','reels'],'reel_queue':len(reel_q)}
@app.post('/jobs')
def create(p:NewJob,x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret)
 if not check_url(p.url) or p.chat_id!=p.user_id or p.chat_id<=0:raise HTTPException(422,'invalid input')
 url=normalize(p.url)
 with lock:
  for k,v in jobs.items():
   if p.update_id is not None and v.get('update_id')==p.update_id:return {'id':k,'created':False}
  count=sum(v['user']==p.user_id and v['state']!='failed' and v['created']>time.time()-86400 for v in jobs.values())
  if count>=DAILY_LIMIT:raise HTTPException(429,'daily limit')
  j=uuid.uuid4().hex[:16];jobs[j]=dict(url=url,chat=p.chat_id,user=p.user_id,update_id=p.update_id,created=time.time(),state='preparing')
 pool.submit(inspect,j);return {'id':j,'created':True}
@app.post('/jobs/{j}/action')
def action(j:str,p:Action,x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret)
 with lock:
  job=jobs.get(j)
  if not job:raise HTTPException(404,'missing')
  if job['user']!=p.user_id or job['chat']!=p.chat_id:raise HTTPException(403,'wrong user')
  if p.mode in ('photos','photo'):
   if job.get('kind')!='photos':raise HTTPException(409,'not a photo job')
   if job.get('expired') or not job.get('photos'):raise HTTPException(410,'expired')
   n=len(job['photos'])
   if p.mode=='photos' and p.quality not in ('all','pick','zip'):raise HTTPException(422,'invalid option')
   if p.mode=='photo' and not (p.quality.isdigit() and 1<=int(p.quality)<=n):raise HTTPException(422,'invalid option')
   pool.submit(photo_action,j,p.mode,p.quality);return {'ok':True}
  if job.get('expired'):raise HTTPException(410,'expired')
  if job.get('kind')=='photos':raise HTTPException(409,'photo job')
  if p.mode=='caption':
   if not job.get('desc'):raise HTTPException(422,'no description')
   pool.submit(talk,p.chat_id,'📝 '+job['desc']);return {'ok':True}
  if p.mode=='cover':
   if not job.get('thumbnail'):raise HTTPException(422,'no cover')
   pool.submit(send,'sendPhoto',chat_id=p.chat_id,photo=job['thumbnail']);return {'ok':True}
  if p.mode not in ('chat','link','audio','gif','compress','webm') or (p.quality!='best' and not (p.quality.isdigit() and int(p.quality) in job.get('qualities',[]))):raise HTTPException(422,'invalid option')
  # Any tool can run on a probed video at any time, also after an earlier download finished (state 'complete').
  if not job.get('probed'):raise HTTPException(409,'not ready')
  if job.get('busy'):raise HTTPException(409,'busy')
  job.update(state='processing',busy=True,mode=p.mode,quality=p.quality)
 pool.submit(process,j);return {'ok':True}
@app.post('/stats')
def stats(x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret);return {'users':len(set(v['user'] for v in jobs.values())),'total_jobs':len(jobs),'completed':sum(v['state']=='complete' for v in jobs.values()),'failed':sum(v['state']=='failed' for v in jobs.values()),'today':sum(v['created']>time.time()-86400 for v in jobs.values()),'warp':WARP['up']}

class StockFetch(BaseModel):
 theme:str;orient:str='v';n:int=20
@app.post('/stock/fetch')
def stock_fetch(p:StockFetch,x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret)
 if p.theme not in stock.THEMES or p.orient not in ('v','h'):raise HTTPException(422,'invalid')
 threading.Thread(target=lambda:print('stock fetch',p.theme,p.orient,'added',stock.fetch(p.theme,p.orient,max(1,min(p.n,60))),flush=True),daemon=True).start()
 return {'ok':True}
@app.get('/stock/stats')
def stock_stats(x_worker_secret:str|None=Header(None)):
 auth(x_worker_secret);return {'themes':{k:v[0] for k,v in stock.THEMES.items()},'pool':stock.stats()}
@app.get('/download/{j}')
def download(j:str,until:int=0,sig:str=''):
 job=jobs.get(j);now=int(time.time())
 if not job or until<now or until>now+7200 or not sig or not hmac.compare_digest(sig,sig_for(j,until)):raise HTTPException(403,'expired')
 file=Path(job.get('file',''))
 if job.get('expires')!=until or not file.is_file() or file.parent!=DATA/j:raise HTTPException(404,'missing')
 report(job,None,file.stat().st_size)
 return FileResponse(file,filename='mediahub'+file.suffix,headers={'Cache-Control':'private, no-store','X-Robots-Tag':'noindex'})
def sig_for(j,until):return sig_fn(j,until)
def sig_fn(j,until):return sig(j,until)

if os.getenv('BMH_NO_BG')!='1':
 load_ig_cookies()
 threading.Thread(target=warp_supervisor,daemon=True).start()
 threading.Thread(target=janitor,daemon=True).start()
