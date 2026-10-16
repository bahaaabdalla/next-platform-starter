"""Reel studio: Quran recitation reels (verified text + per-ayah recitation) over moving backgrounds.

Text: bundled Uthmani text (QuranEnc / King Fahd complex encoding, via the quran-json package, CC BY-SA 4.0).
Audio: one MP3 per ayah from everyayah.com, so every ayah's on-screen time equals its real recitation time.
Video: placeholder backgrounds (until the generated library exists) panning slowly, crossfades, optional vignette,
centered verse pages, surah label and an optional watermark. Output H.264/AAC, 720p.
"""
import os,json,math,random,shutil,subprocess,threading,time
from pathlib import Path
import httpx

HERE=Path(__file__).resolve().parent
DATA_FILE=HERE/'data'/'quran.json'
FONT=os.getenv('QURAN_FONT',str(HERE/'fonts'/'AmiriQuran-Regular.ttf'))
UI_FONT=os.getenv('UI_FONT',str(HERE/'fonts'/'Amiri-Regular.ttf'))
EVERYAYAH='https://everyayah.com/data/'
FPS=25;XF=0.8   # crossfade seconds between backgrounds
SIZES={'9:16':(720,1280),'16:9':(1280,720)}
RECITERS={   # key -> (Arabic name, everyayah folder)
 'afasy':('مشاري راشد العفاسي','Alafasy_128kbps'),
 'basit':('عبد الباسط عبد الصمد','Abdul_Basit_Murattal_192kbps'),
 'husary':('محمود خليل الحصري','Husary_128kbps'),
 'minshawi':('محمد صديق المنشاوي','Minshawy_Murattal_128kbps'),
 'sudais':('عبد الرحمن السديس','Abdurrahmaan_As-Sudais_192kbps'),
 'maher':('ماهر المعيقلي','MaherAlMuaiqly128kbps'),
 'dossari':('ياسر الدوسري','Yasser_Ad-Dussary_128kbps'),
 'shatri':('أبو بكر الشاطري','Abu_Bakr_Ash-Shaatree_128kbps'),
}
# The bundled text uses the King Fahd complex encoding for "open" tanween; map it to the standard Unicode marks the font draws.
TANWEEN=str.maketrans({'ٗ':'ࣰ','ٞ':'ࣱ','ٖ':'ࣲ'})
AR_DIGITS=str.maketrans('0123456789','٠١٢٣٤٥٦٧٨٩')
_Q=None;_qlock=threading.Lock()

def quran():
 global _Q
 with _qlock:
  if _Q is None:_Q=json.loads(DATA_FILE.read_text(encoding='utf-8'))
 return _Q

def surah_name(s):return quran()[s-1][0]
def ayah_count(s):return len(quran()[s-1][1])
def ayah_text(s,a):return quran()[s-1][1][a-1]

def validate(spec):
 s=int(spec.get('surah',0));a=int(spec.get('frm',0));to=int(spec.get('to') or 0)
 if not 1<=s<=114:raise ValueError('bad surah')
 n=ayah_count(s)
 if not 1<=a<=n:raise ValueError('bad ayah')
 if to and not a<=to<=n:raise ValueError('bad range')
 if spec.get('reciter') not in RECITERS:raise ValueError('bad reciter')
 if spec.get('ar') not in SIZES:raise ValueError('bad size')
 return s,a,(to or min(n,a+40))

# ---------- audio ----------
def run(argv,timeout):
 p=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
 if p.returncode:raise RuntimeError(argv[0]+' failed: '+p.stderr.decode('utf-8','ignore')[-300:])
 return p.stdout.decode('utf-8','ignore')

def duration(f):
 return float(run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(f)],30).strip() or 0)

def fetch_ayah(cache,folder,s,a):
 """Per-ayah recitation, cached on disk (the same ayat get requested again and again)."""
 d=cache/folder;d.mkdir(parents=True,exist_ok=True)
 f=d/('%03d%03d.mp3'%(s,a))
 if f.exists() and f.stat().st_size>1000:return f
 r=httpx.get(EVERYAYAH+folder+'/'+f.name,timeout=httpx.Timeout(25,connect=10),follow_redirects=True)
 r.raise_for_status()
 if len(r.content)<1000:raise RuntimeError('empty recitation file')
 tmp=f.with_suffix('.part');tmp.write_bytes(r.content);tmp.replace(f)
 return f

def trim_cache(cache,limit=400*1024*1024):
 try:
  files=sorted((p for p in cache.rglob('*.mp3')),key=lambda p:p.stat().st_atime)
  total=sum(p.stat().st_size for p in files)
  for p in files:
   if total<=limit:break
   total-=p.stat().st_size;p.unlink(missing_ok=True)
 except Exception:pass

def pick_ayat(cache,folder,s,a,to,maxdur,hardcap):
 """Whole ayat from `a` while they fit in maxdur (at least one). A single ayah longer than the plan cap is cut with a fade."""
 out=[];total=0.0
 for i in range(a,to+1):
  f=fetch_ayah(cache,folder,s,i);d=duration(f)
  if out and total+d>maxdur:break
  out.append((i,f,d));total+=d
  if total>=maxdur:break
 return out,min(total,hardcap)

# ---------- text ----------
def font(path,size):
 from PIL import ImageFont
 return ImageFont.truetype(path,size,layout_engine=ImageFont.Layout.RAQM)

def wrap(draw,text,f,maxw):
 lines=[];line=''
 for w in text.split():
  cand=(line+' '+w).strip()
  if line and draw.textlength(cand,font=f,direction='rtl',language='ar')>maxw:lines.append(line);line=w
  else:line=cand
 if line:lines.append(line)
 return lines

def layout(W,H):
 if W<H:return dict(size=50,maxw=W-100,lh=100,maxlines=7,cy=int(H*0.47))
 return dict(size=44,maxw=W-200,lh=86,maxlines=4,cy=int(H*0.5))

def ayah_pages(text,W,H):
 """Split one ayah into screen pages (lists of lines)."""
 from PIL import Image,ImageDraw
 L=layout(W,H);d=ImageDraw.Draw(Image.new('L',(8,8)));f=font(FONT,L['size'])
 lines=wrap(d,text,f,L['maxw'])
 return [lines[i:i+L['maxlines']] for i in range(0,len(lines),L['maxlines'])] or [[text]]

def text_png(lines,W,H,dest):
 from PIL import Image,ImageDraw,ImageFilter
 L=layout(W,H);f=font(FONT,L['size'])
 img=Image.new('RGBA',(W,H),(0,0,0,0));sh=Image.new('RGBA',(W,H),(0,0,0,0))
 top=L['cy']-len(lines)*L['lh']//2
 ds=ImageDraw.Draw(sh);dt=ImageDraw.Draw(img)
 for i,ln in enumerate(lines):
  y=top+i*L['lh']
  ds.text((W//2+2,y+3),ln,font=f,fill=(0,0,0,230),anchor='mt',direction='rtl',language='ar')
  dt.text((W//2,y),ln,font=f,fill=(255,255,255,255),anchor='mt',direction='rtl',language='ar')
 sh=sh.filter(ImageFilter.GaussianBlur(6))
 full=Image.alpha_composite(sh,img);box=full.getbbox() or (0,0,2,2)
 box=(max(0,box[0]-4)&~1,max(0,box[1]-4)&~1,min(W,box[2]+4),min(H,box[3]+4))
 full.crop(box).save(dest)
 return dest,box[0],box[1]   # only the text box is overlaid: blending a full frame per page was a large part of the cost

def chrome_png(W,H,label,wm,dest,vignette=False):
 """Static layer: surah label on top, watermark at the bottom."""
 from PIL import Image,ImageDraw,ImageFilter
 img=Image.new('RGBA',(W,H),(0,0,0,0));sh=Image.new('RGBA',(W,H),(0,0,0,0))
 if vignette:sh=vignette_layer(W,H)
 d=ImageDraw.Draw(img);ds=ImageDraw.Draw(sh)
 lf=font(UI_FONT,34 if W<H else 30);y=int(H*0.07)
 for dd,fill,off in ((ds,(0,0,0,200),2),(d,(255,255,255,220),0)):
  dd.text((W//2+off,y+off),label,font=lf,fill=fill,anchor='mt',direction='rtl',language='ar')
 if wm:
  wf=font(UI_FONT,26);yb=H-int(H*0.06)
  for dd,fill,off in ((ds,(0,0,0,170),2),(d,(255,255,255,160),0)):
   dd.text((W//2+off,yb+off),wm,font=wf,fill=fill,anchor='mb')
 Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3)),img).save(dest)
 return dest

def vignette_layer(W,H):
 """Black with a radial alpha ramp: dark corners, clear center."""
 from PIL import Image
 import math as m
 w,h=W//8,H//8;mask=Image.new('L',(w,h))
 cx,cy=w/2,h/2;rmax=m.hypot(cx,cy);px=mask.load()
 for y in range(h):
  for x in range(w):
   r=m.hypot((x-cx)/cx,(y-cy)/cy)/m.sqrt(2)
   px[x,y]=int(max(0,min(1,(r-0.45)/0.55))**1.6*200)
 mask=mask.resize((W,H),Image.BILINEAR)
 v=Image.new('RGBA',(W,H),(0,0,0,0));v.putalpha(mask);return v

# ---------- backgrounds ----------
PALETTES=[((12,24,40),(38,74,96)),((24,18,40),(86,60,110)),((10,30,26),(40,96,80)),((36,22,14),(120,80,40)),
          ((14,14,30),(40,60,120)),((30,12,20),(110,46,64)),((8,26,36),(20,110,120)),((22,26,14),(90,104,50))]

def placeholder_bg(W,H,seed,dest):
 """Soft gradient with bokeh lights (stand-in until the generated background library is uploaded)."""
 from PIL import Image,ImageDraw,ImageFilter
 rnd=random.Random(seed);a,b=PALETTES[seed%len(PALETTES)]
 BW,BH=int(W*1.18),int(H*1.18)
 img=Image.new('RGB',(BW,BH));px=ImageDraw.Draw(img)
 for y in range(BH):
  t=y/BH;px.line([(0,y),(BW,y)],fill=tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3)))
 glow=Image.new('RGB',(BW,BH),(0,0,0));g=ImageDraw.Draw(glow)
 for _ in range(26):
  r=rnd.randint(int(BW*0.02),int(BW*0.09));x=rnd.randint(0,BW);y=rnd.randint(0,BH);c=rnd.randint(40,120)
  g.ellipse((x-r,y-r,x+r,y+r),fill=(c,c,int(c*0.9)))
 glow=glow.filter(ImageFilter.GaussianBlur(int(BW*0.02)))
 from PIL import ImageChops
 ImageChops.add(img,glow).save(dest,quality=90)
 return dest

# ---------- render ----------
def plan_segments(total,n_bg):
 n=max(1,n_bg);seg=(total+XF*(n-1))/n
 return [seg]*n

def build_cmd(W,H,bgs,total,pages,chrome,audio,out):
 n=len(bgs);segs=plan_segments(total,n)
 args=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y']
 # Every still is decoded once: backgrounds at 1 fps (the fps filter repeats frames in memory), overlays as single frames
 # that the overlay filter keeps showing (repeatlast). Decoding PNGs 25x per second was the main render cost.
 for b,s in zip(bgs,segs):args+=['-loop','1','-framerate','1','-t','%.3f'%(s+1),'-i',str(b)]
 for (p,_,_),_,_ in pages:args+=['-i',str(p)]
 args+=['-i',str(chrome)]
 args+=['-i',str(audio)]
 fl=[]
 for i,s in enumerate(segs):
  dx,dy=[(1,0),(-1,0),(0,1),(0,-1)][i%4]
  x='(iw-ow)/2' if dx==0 else ('(iw-ow)*t/%.3f'%s if dx>0 else '(iw-ow)*(1-t/%.3f)'%s)
  y='(ih-oh)/2' if dy==0 else ('(ih-oh)*t/%.3f'%s if dy>0 else '(ih-oh)*(1-t/%.3f)'%s)
  fl.append('[%d:v]fps=%d,trim=duration=%.3f,setpts=PTS-STARTPTS,crop=%d:%d:%s:%s,setsar=1,format=yuv420p[b%d]'%(i,FPS,s,W,H,x,y,i))
 cur='b0';off=0.0
 for i in range(1,n):
  off+=segs[i-1]-XF
  fl.append('[%s][b%d]xfade=transition=fade:duration=%.2f:offset=%.3f[x%d]'%(cur,i,XF,off,i));cur='x%d'%i
 for k,((p,px,py),t0,t1) in enumerate(pages):
  idx=n+k
  fl.append("[%s][%d:v]overlay=%d:%d:eof_action=repeat:enable='between(t,%.3f,%.3f)'[t%d]"%(cur,idx,px,py,t0,t1,k));cur='t%d'%k
 ci=n+len(pages)
 fl.append('[%s][%d:v]overlay=0:0:eof_action=repeat,format=yuv420p[vout]'%(cur,ci))
 ai=ci+1
 args+=['-filter_complex',';'.join(fl),'-map','[vout]','-map','%d:a'%ai,'-t','%.3f'%total,
        '-r',str(FPS),'-c:v','libx264','-preset',os.getenv('REEL_PRESET','veryfast'),'-crf','24','-maxrate','2500k','-bufsize','5000k',
        '-c:a','aac','-b:a','128k','-movflags','+faststart','-shortest',str(out)]
 return args

def render_quran(spec,work,cache,on_stage=None):
 """Returns (video_path, info). Raises ValueError for bad input, RuntimeError for failures."""
 s,a,to=validate(spec)
 W,H=SIZES[spec['ar']]
 hardcap=float(spec.get('cap',60));maxdur=min(float(spec.get('maxdur',30)),hardcap)
 name,folder=RECITERS[spec['reciter']]
 work.mkdir(parents=True,exist_ok=True)
 ayat,total=pick_ayat(cache,folder,s,a,to,maxdur,hardcap)
 if on_stage:on_stage('render')
 # audio: short lead-in, recitation, fade out at the end
 lst=work/'list.txt';lst.write_text(''.join("file '%s'\n"%str(f).replace("'","'\\''") for _,f,_ in ayat))
 lead=0.4;total_v=total+lead+0.6
 audio=work/'audio.m4a'
 run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-f','concat','-safe','0','-i',str(lst),
      '-af','adelay=%d:all=1,apad=pad_dur=1,afade=t=out:st=%.3f:d=0.6'%(int(lead*1000),max(0,total_v-0.6)),
      '-t','%.3f'%total_v,'-c:a','aac','-b:a','128k',str(audio)],120)
 # verse pages timed to their recitation
 pages=[];t=lead;k=0
 for num,f,d in ayat:
  text=ayah_text(s,num).translate(TANWEEN)+' ﴿'+str(num).translate(AR_DIGITS)+'﴾'
  pg=ayah_pages(text,W,H);weights=[sum(len(l) for l in p) for p in pg];tot=sum(weights) or 1
  for p,wt in zip(pg,weights):
   dur=d*wt/tot
   if t<total_v-0.05:pages.append([text_png(p,W,H,work/('p%03d.png'%k)),t,min(t+dur,total_v)]);k+=1
   t+=dur
 if pages:pages[-1][2]=total_v   # keep the last page up through the fade-out
 last=ayat[-1][0]
 label='سورة '+surah_name(s)+' · '+(('الآية '+str(a).translate(AR_DIGITS)) if last==a else ('الآيات من '+str(a).translate(AR_DIGITS)+' إلى '+str(last).translate(AR_DIGITS)))
 chrome=chrome_png(W,H,label,spec.get('wm') or '',work/'chrome.png',bool(spec.get('vig',True)))
 n_bg=max(1,min(8,math.ceil(total_v/7)))
 seed=random.randrange(1000)
 bgs=[placeholder_bg(W,H,seed+i,work/('bg%d.jpg'%i)) for i in range(n_bg)]
 out=work/'reel.mp4'
 t0=time.time()
 run(build_cmd(W,H,bgs,total_v,pages,chrome,audio,out),int(os.getenv('REEL_TIMEOUT','1200')))
 info=dict(surah=s,name=surah_name(s),frm=a,to=last,reciter=name,duration=total_v,width=W,height=H,render_s=round(time.time()-t0,1),
           text=' '.join(ayah_text(s,n).translate(TANWEEN)+' ﴿'+str(n).translate(AR_DIGITS)+'﴾' for n,_,_ in ayat))
 return out,info

def caption(info):
 head='📖 سورة %s · %s\n🎙 القارئ: %s'%(info['name'],('الآية %d'%info['frm']) if info['frm']==info['to'] else ('الآيات %d–%d'%(info['frm'],info['to'])),info['reciter'])
 tags='#قرآن #تلاوة #سورة_%s #ريلز_قرآن'%info['name'].replace(' ','_')
 room=1000-len(head)-len(tags)-8
 body=info['text'] if len(info['text'])<=room else info['text'][:room].rsplit(' ',1)[0]+' …'
 return head+'\n\n'+body+'\n\n'+tags
