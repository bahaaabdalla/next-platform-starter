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
TITLE_FONT=str(HERE/'fonts'/'ArefRuqaa-Bold.ttf')            # surah name on top
SIG_AR_FONT=str(HERE/'fonts'/'ArefRuqaa-Regular.ttf')        # page signature, Arabic
SIG_LAT_FONT=str(HERE/'fonts'/'GreatVibes-Regular.ttf')      # page signature, Latin script
EVERYAYAH='https://everyayah.com/data/'
FPS=25;XF=0.8   # crossfade seconds between backgrounds
SIZES={'9:16':(720,1280),'16:9':(1280,720)}
# Full-surah recordings with per-ayah timestamps (quran.com / quranicaudio): one continuous cut, no joins at all.
QDC={'afasy':7,'basit':2,'husary':6,'minshawi':9,'sudais':3,'shatri':4,'shuraym':10,'rifai':5}
QDC_API='https://api.quran.com/api/v4/chapter_recitations/%d/%d?segments=true'
RECITERS={   # key -> (Arabic name, everyayah folder)
 'afasy':('مشاري راشد العفاسي','Alafasy_128kbps'),
 'basit':('عبد الباسط عبد الصمد','Abdul_Basit_Murattal_192kbps'),
 'husary':('محمود خليل الحصري','Husary_128kbps'),
 'minshawi':('محمد صديق المنشاوي','Minshawy_Murattal_128kbps'),
 'sudais':('عبد الرحمن السديس','Abdurrahmaan_As-Sudais_192kbps'),
 'maher':('ماهر المعيقلي','MaherAlMuaiqly128kbps'),
 'dossari':('ياسر الدوسري','Yasser_Ad-Dussary_128kbps'),
 'shatri':('أبو بكر الشاطري','Abu_Bakr_Ash-Shaatree_128kbps'),
 'shuraym':('سعود الشريم','Saood_ash-Shuraym_128kbps'),
 'rifai':('هاني الرفاعي','Hani_Rifai_192kbps'),
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
 if p.returncode:
  err=[l for l in p.stderr.decode('utf-8','ignore').splitlines() if l.strip()]
  print(argv[0],'stderr:',' | '.join(err[:6])[:600],flush=True)   # the first lines carry the real cause
  raise RuntimeError(argv[0]+' failed: '+' | '.join(err[-3:])[-300:])
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

GAP=0.0;LEAD=0.4;TAIL=0.8   # no added pause between ayat (only the reciter's own breath), silence before/after
def clean_ayah(src,dest):
 """One ayah as plain PCM keeping the reciter's own breath around it: only silence beyond ~0.3 s before and
 ~0.45 s after the voice is removed, so joined ayat pause like the real recitation (no glued, cut-off feel).
 (Concatenating the MP3s directly decodes each file's first frame without its bit reservoir, which can blip.)"""
 af=('silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.30,areverse,'
     'silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.45,afade=t=in:d=0.005,areverse,afade=t=in:d=0.005')
 run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(src),'-af',af,'-ac','2','-ar','44100','-c:a','pcm_s16le',str(dest)],120)
 d=duration(dest)
 if d<0.3:   # trimming ate it (very quiet file): fall back to the untouched recitation
  run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(src),'-ac','2','-ar','44100','-c:a','pcm_s16le',str(dest)],120);d=duration(dest)
 return dest,d

def silence(sec,dest):
 run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t','%.3f'%sec,'-c:a','pcm_s16le',str(dest)],30)
 return dest

def recitation(ayat,work,hardcap):
 """Join cleaned ayat with natural pauses. Returns (audio.m4a, total seconds, [(ayah, start, end_of_its_screen_time)])."""
 parts=[silence(LEAD,work/'lead.wav')];gap=silence(GAP,work/'gap.wav');tail=silence(TAIL,work/'tail.wav')
 t=LEAD;spans=[]
 for k,(num,f,_) in enumerate(ayat):
  w,d=clean_ayah(f,work/('a%03d.wav'%k))
  if k and GAP>0:parts.append(gap);t+=GAP
  parts.append(w);spans.append([num,t,t+d]);t+=d
 parts.append(tail);total=min(t+TAIL,LEAD+hardcap+TAIL)
 for i in range(len(spans)-1):spans[i][2]=spans[i+1][1]   # a verse stays up through the pause until the next one starts
 spans[-1][2]=total
 lst=work/'list.txt';lst.write_text(''.join("file '%s'\n"%str(p).replace("'","'\\''") for p in parts))
 audio=work/'audio.m4a'
 run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-f','concat','-safe','0','-i',str(lst),
      '-af','afade=t=out:st=%.3f:d=0.6'%max(0,total-0.6),'-t','%.3f'%total,'-c:a','aac','-b:a','160k',str(audio)],180)
 for p in work.glob('*.wav'):p.unlink(missing_ok=True)
 return audio,total,[tuple(x) for x in spans if x[1]<total-0.05]

def qdc_chapter(cache,key,s):
 """(surah mp3 path, {ayah: (from_ms, to_ms)}) for reciters with full-surah recordings, else None."""
 if key not in QDC:return None
 d=cache/('qdc_%s'%key);d.mkdir(parents=True,exist_ok=True)
 meta=d/('%03d.json'%s);mp3=d/('%03d.mp3'%s)
 if not meta.exists():
  r=httpx.get(QDC_API%(QDC[key],s),timeout=httpx.Timeout(20,connect=10),follow_redirects=True);r.raise_for_status()
  af=r.json().get('audio_file') or {}
  ts={int(t['verse_key'].split(':')[1]):(int(t['timestamp_from']),int(t['timestamp_to']),int((t.get('segments') or [[0,t['timestamp_from']]])[0][1])) for t in af.get('timestamps') or []}
  if not af.get('audio_url') or not ts:raise RuntimeError('no timings')
  meta.write_text(json.dumps({'url':af['audio_url'],'ts':ts}))
 m=json.loads(meta.read_text());ts={int(k):v for k,v in m['ts'].items()}
 if not mp3.exists() or mp3.stat().st_size<10000:
  tmp=mp3.with_suffix('.part')
  with httpx.stream('GET',m['url'],timeout=httpx.Timeout(60,connect=15),follow_redirects=True) as r:
   r.raise_for_status();n=0
   with open(tmp,'wb') as fh:
    for chunk in r.iter_bytes(1<<16):
     n+=len(chunk)
     if n>200*1024*1024:raise RuntimeError('surah file too big')
     fh.write(chunk)
  tmp.replace(mp3)
 os.utime(mp3)
 return mp3,ts

def continuous_recitation(mp3,ts,a,to,maxdur,hardcap,work):
 """One uninterrupted cut of the real recording from ayah a. Returns (audio, total, spans) like recitation()."""
 picked=[];start=None;last_end=None
 for i in range(a,to+1):
  if i not in ts:break
  f,t,w=ts[i]
  begin=(0 if i==1 else max(f,w-250)) if start is None else None   # ayah 1 keeps the opening basmala
  st=begin if start is None else start
  if picked and (t-st)/1000>maxdur:break
  if start is None:start=st
  picked.append((i,f,t));last_end=t
  if (t-start)/1000>=maxdur:break
 if not picked:raise RuntimeError('no timings for ayah')
 cut=min((last_end-start)/1000,hardcap)
 total=LEAD+cut+TAIL
 audio=work/'audio.m4a'
 run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-ss','%.3f'%(start/1000),'-t','%.3f'%cut,'-i',str(mp3),
      '-af','afade=t=in:d=0.03,adelay=%d:all=1,apad=pad_dur=%.2f,afade=t=out:st=%.3f:d=0.6'%(int(LEAD*1000),TAIL,max(0,total-0.6)),
      '-t','%.3f'%total,'-ac','2','-ar','44100','-c:a','aac','-b:a','160k',str(audio)],180)
 spans=[];off=LEAD-start/1000
 for k,(i,f,t) in enumerate(picked):
  t0=LEAD if k==0 else f/1000+off
  t1=picked[k+1][1]/1000+off if k+1<len(picked) else total
  if t0<total-0.05:spans.append((i,t0,min(t1,total)))
 return audio,total,spans,[(i,None,None) for i,_,_ in picked]

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
 # Reels/Shorts safe zone: clear of the top bar, the bottom ~22% (account name + caption) and the right-side buttons.
 if W<H:return dict(size=50,maxw=W-150,lh=100,maxlines=6,cy=int(H*0.45))
 return dict(size=44,maxw=W-260,lh=86,maxlines=4,cy=int(H*0.47))

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

def clean_sig(t):
 t=' '.join(''.join(c for c in str(t or '') if c.isprintable() and not '\u200b'<=c<='\u200f' and not '\u202a'<=c<='\u202e' and not '\u2066'<=c<='\u2069').split())
 return t[:30] or None

def is_arabic(t):return any('\u0600'<=c<='\u06ff' or '\u0750'<=c<='\u077f' or '\ufb50'<=c<='\ufdff' or '\ufe70'<=c<='\ufeff' for c in t)

def chrome_png(W,H,label,wm,dest,vignette=False,logo=None,title=None,sig=None):
 """Static layer: surah title (or a small label) on top, optional page signature under the verses, watermark
 (logo image if provided, else text) at the bottom, optional vignette."""
 from PIL import Image,ImageDraw,ImageFilter
 img=Image.new('RGBA',(W,H),(0,0,0,0));sh=Image.new('RGBA',(W,H),(0,0,0,0))
 if vignette:sh=vignette_layer(W,H)
 d=ImageDraw.Draw(img);ds=ImageDraw.Draw(sh)
 if title:
  tf=font(TITLE_FONT,62 if W<H else 50);y=int(H*(0.10 if W<H else 0.07))
  for dd,fill,off in ((ds,(0,0,0,230),3),(d,(255,255,255,245),0)):
   dd.text((W//2+off,y+off),title,font=tf,fill=fill,anchor='mt',direction='rtl',language='ar')
 elif label:
  lf=font(UI_FONT,34 if W<H else 30);y=int(H*0.07)
  for dd,fill,off in ((ds,(0,0,0,200),2),(d,(255,255,255,220),0)):
   dd.text((W//2+off,y+off),label,font=lf,fill=fill,anchor='mt',direction='rtl',language='ar')
 if sig:
  ar=is_arabic(sig);sf=font(SIG_AR_FONT if ar else SIG_LAT_FONT,(46 if ar else 50) if W<H else (38 if ar else 42))
  y=int(H*(0.745 if W<H else 0.835));kw=dict(direction='rtl',language='ar') if ar else {}
  for dd,fill,off in ((ds,(0,0,0,220),2),(d,(255,255,255,235),0)):
   dd.text((W//2+off,y+off),sig,font=sf,fill=fill,anchor='mm',**kw)
 if wm:
  placed=False
  if logo and Path(logo).exists():
   try:
    with Image.open(logo) as lg:
     lg=lg.convert('RGBA');tw=int(W*(0.22 if W<H else 0.13));lg=lg.resize((tw,max(1,int(lg.height*tw/lg.width))),Image.LANCZOS)
     a=lg.split()[3].point(lambda v:int(v*0.85));lg.putalpha(a)
     img.alpha_composite(lg,((W-lg.width)//2,H-int(H*0.05)-lg.height));placed=True
   except Exception:placed=False
  if not placed:
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

# ---------- render (two light passes so peak memory stays far below 512 MB) ----------
FF=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-filter_complex_threads','1','-filter_threads','1']
def x264(crf,preset):
 # FFmpeg sizes threads from the host's core count (dozens on shared hosts) and every x264 frame thread plus the
 # lookahead holds full frames; pin them low.
 return ['-c:v','libx264','-preset',preset,'-crf',str(crf),'-pix_fmt','yuv420p','-threads',os.getenv('REEL_THREADS','2'),
         '-x264-params','rc-lookahead=10:sync-lookahead=0:ref=2']

def plan_segments(total,n_bg):
 n=max(1,n_bg);seg=(total+XF*(n-1))/n
 return [seg]*n

def segments_for_cuts(cuts,total):
 """Still durations so each crossfade is centred on a cut time."""
 pts=[0.0]+list(cuts)+[total];segs=[]
 for i in range(len(pts)-1):segs.append(pts[i+1]-pts[i]+(XF/2 if i>0 else 0)+(XF/2 if i<len(pts)-2 else 0))
 return segs

def scene_cuts(spans,total,minlen=5.0,maxlen=10.0):
 """Background changes land in the pause before an ayah (never mid-verse); a long ayah gets evenly split scenes."""
 cuts=[];last=0.0
 bounds=[max(0.0,t0-GAP/2) for _,t0,_ in spans[1:]]+[total]
 for b in bounds:
  while b-last>maxlen+minlen:   # one long stretch: split it evenly
   n=math.ceil((b-last)/maxlen);step=(b-last)/n;last+=step;cuts.append(last)
  if b<total and b-last>=minlen and total-b>=minlen*0.6:cuts.append(b);last=b
 return cuts

def fit_cover(src,W,H,dest,scale=1.18):
 """Any photo -> exactly (W,H)*scale, centre-cropped, EXIF-rotated. Big phone photos are shrunk here, not in FFmpeg."""
 from PIL import Image,ImageOps
 BW,BH=int(W*scale)&~1,int(H*scale)&~1
 with Image.open(src) as im:
  im=ImageOps.exif_transpose(im).convert('RGB')
  r=max(BW/im.width,BH/im.height);im=im.resize((max(BW,int(im.width*r+0.5)),max(BH,int(im.height*r+0.5))),Image.LANCZOS)
  l,t=(im.width-BW)//2,(im.height-BH)//2;im.crop((l,t,l+BW,t+BH)).save(dest,quality=90)
 return dest

def cycle_track(W,H,stills,total,every,dest,move=True):
 """Backgrounds that change every `every` seconds, cycling through the stills: one seamless loop unit
 (b0..bK-1 crossfading back into b0) rendered once, then looped to the reel length - light on memory."""
 k=len(stills)
 if k<=1 or every<=0 or total<=every+XF:
  return stills_track(W,H,stills[:1],total,dest,move)
 raw=Path(str(dest)+'.raw.mp4');unit=Path(str(dest)+'.unit.mp4')
 # chain b0..bK-1,b0' ; b0' pans exactly like b0, so the frame at XF into b0' equals the frame at XF into b0
 seq=list(stills)+[stills[0]];segs=[every+XF]*k+[XF+0.2];pan=[every+XF]*(k+1)
 stills_track(W,H,seq,k*every+XF+0.12,raw,move,segs=segs,pan=pan,dirs=list(range(k))+[0])
 run(list(FF)+['-ss','%.3f'%XF,'-i',str(raw),'-t','%.3f'%(k*every),'-an']+x264(16,'ultrafast')+[str(unit)],300)
 raw.unlink(missing_ok=True)
 run(list(FF)+['-stream_loop','-1','-i',str(unit),'-t','%.3f'%total,'-c','copy',str(dest)],300)
 unit.unlink(missing_ok=True)
 return dest

def stills_track(W,H,stills,total,dest,move=True,segs=None,pan=None,dirs=None):
 """Pass 1: slow pans over the stills with crossfades -> silent mezzanine video."""
 n=len(stills);segs=segs or plan_segments(total,n);args=list(FF);fl=[]
 for b,sg in zip(stills,segs):args+=['-loop','1','-framerate','1','-t','%.3f'%(sg+1),'-i',str(b)]
 for i,sg in enumerate(segs):
  dx,dy=[(1,0),(-1,0),(0,1),(0,-1)][(dirs[i] if dirs else i)%4] if move else (0,0)
  pl=pan[i] if pan else sg
  x='(iw-ow)/2' if dx==0 else ('(iw-ow)*t/%.3f'%pl if dx>0 else '(iw-ow)*(1-t/%.3f)'%pl)
  y='(ih-oh)/2' if dy==0 else ('(ih-oh)*t/%.3f'%pl if dy>0 else '(ih-oh)*(1-t/%.3f)'%pl)
  # trailing fps: FFmpeg 7 xfade needs a declared constant rate
  fl.append('[%d:v]fps=%d,trim=duration=%.3f,setpts=PTS-STARTPTS,crop=%d:%d:%s:%s,setsar=1,format=yuv420p,fps=%d[b%d]'%(i,FPS,sg,W,H,x,y,FPS,i))
 cur='b0';off=0.0
 for i in range(1,n):
  off+=segs[i-1]-XF
  fl.append('[%s][b%d]xfade=transition=fade:duration=%.2f:offset=%.3f[x%d]'%(cur,i,XF,off,i));cur='x%d'%i
 args+=['-filter_complex',';'.join(fl),'-map','['+cur+']','-t','%.3f'%total,'-r',str(FPS)]+x264(16,'ultrafast')+['-an',str(dest)]
 run(args,int(os.getenv('REEL_TIMEOUT','1200')))
 return dest

def video_track(W,H,src,total,dest):
 """Pass 1 for a video source: cover-crop to the frame, muted, looped if it is shorter than the reel."""
 args=list(FF)+['-stream_loop','-1','-i',str(src),'-t','%.3f'%total,
  '-vf','scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1,fps=%d,format=yuv420p'%(W,H,W,H,FPS)]+x264(18,'ultrafast')+['-an',str(dest)]
 run(args,int(os.getenv('REEL_TIMEOUT','1200')))
 return dest

def compose(W,H,track,total,pages,chrome,audio,out):
 """Pass 2: text pages + chrome (label, watermark/logo, vignette) over the track, plus the audio."""
 args=list(FF)+['-i',str(track)]
 for (p,_,_),_,_ in pages:args+=['-i',str(p)]
 args+=['-i',str(chrome)]
 if audio:args+=['-i',str(audio)]
 fl=[];cur='0:v'
 for k,((p,px,py),t0,t1) in enumerate(pages):
  fl.append("[%s][%d:v]overlay=%d:%d:eof_action=repeat:enable='between(t,%.3f,%.3f)'[t%d]"%(cur,k+1,px,py,t0,t1,k));cur='t%d'%k
 ci=len(pages)+1
 fl.append('[%s][%d:v]overlay=0:0:eof_action=repeat,format=yuv420p[vout]'%(cur,ci))
 args+=['-filter_complex',';'.join(fl),'-map','[vout]']
 if audio:args+=['-map','%d:a'%(ci+1),'-c:a','aac','-b:a','128k']
 else:args+=['-an']
 args+=['-t','%.3f'%total,'-r',str(FPS)]+x264(24,os.getenv('REEL_PRESET','veryfast'))+['-maxrate','2500k','-bufsize','5000k','-movflags','+faststart',str(out)]
 run(args,int(os.getenv('REEL_TIMEOUT','1200')))
 return out

def backgrounds(W,H,work,n,library=()):
 """Stills for the reel: the uploaded background library when available, else generated placeholders."""
 lib=[p for p in library if Path(p).exists()]
 if lib:
  random.shuffle(lib);pick=[lib[i%len(lib)] for i in range(n)]
  return [fit_cover(p,W,H,work/('bg%d.jpg'%i)) for i,p in enumerate(pick)]
 seed=random.randrange(1000)
 return [placeholder_bg(W,H,seed+i,work/('bg%d.jpg'%i)) for i in range(n)]

def render_quran(spec,work,cache,on_stage=None,library=(),logo=None):
 """Returns (video_path, info). Raises ValueError for bad input, RuntimeError for failures."""
 s,a,to=validate(spec)
 W,H=SIZES[spec['ar']]
 hardcap=float(spec.get('cap',60));maxdur=min(float(spec.get('maxdur',30)),hardcap)
 name,folder=RECITERS[spec['reciter']]
 work.mkdir(parents=True,exist_ok=True)
 src=None
 try:src=qdc_chapter(cache,spec['reciter'],s)
 except Exception as e:print('qdc fallback',spec['reciter'],s,type(e).__name__,str(e)[:120],flush=True)
 if src:
  if on_stage:on_stage('render')
  audio,total_v,spans,ayat=continuous_recitation(src[0],src[1],a,to,maxdur,hardcap,work)
 else:   # reciters without a full-surah recording: per-ayah files joined gaplessly
  ayat,total=pick_ayat(cache,folder,s,a,to,maxdur,hardcap)
  if on_stage:on_stage('render')
  audio,total_v,spans=recitation(ayat,work,hardcap)
 # verse pages timed to their recitation (each ayah's pages share its spoken time; the last one holds through the pause)
 pages=[];k=0
 for num,t0,t1 in spans:
  text=ayah_text(s,num).translate(TANWEEN)+' ﴿'+str(num).translate(AR_DIGITS)+'﴾'
  pg=ayah_pages(text,W,H);weights=[sum(len(l) for l in p) for p in pg];tot=sum(weights) or 1
  t=t0
  for i,(p,wt) in enumerate(zip(pg,weights)):
   end=t1 if i==len(pg)-1 else t+(t1-t0)*wt/tot
   pages.append([text_png(p,W,H,work/('p%03d.png'%k)),t,min(end,total_v)]);k+=1;t=end
 ayat=[a for a in ayat if any(a[0]==x[0] for x in spans)]
 last=ayat[-1][0]
 label='سورة '+surah_name(s)+' · '+(('الآية '+str(a).translate(AR_DIGITS)) if last==a else ('الآيات من '+str(a).translate(AR_DIGITS)+' إلى '+str(last).translate(AR_DIGITS)))
 chrome=chrome_png(W,H,label,spec.get('wm') or '',work/'chrome.png',bool(spec.get('vig',True)),logo,title='سورة '+surah_name(s),sig=clean_sig(spec.get('sig')))
 t0=time.time()
 mode=spec.get('bgmode') or 'every'
 if mode=='ayah':
  cuts=scene_cuts(spans,total_v)[:11]
  bgs=backgrounds(W,H,work,len(cuts)+1,library)
  track=stills_track(W,H,bgs,total_v,work/'track.mp4',segs=segments_for_cuts(cuts,total_v))
 else:
  every=0 if mode=='static' else max(2.0,min(float(spec.get('bgsec') or 5),30.0))
  n=1 if mode=='static' else max(2,min(int(spec.get('bgn') or 4),8))
  bgs=backgrounds(W,H,work,n,library)
  track=cycle_track(W,H,bgs,total_v,every,work/'track.mp4')
 for b in bgs:Path(b).unlink(missing_ok=True)
 out=compose(W,H,track,total_v,pages,chrome,audio,work/'reel.mp4')
 info=dict(surah=s,name=surah_name(s),frm=a,to=last,reciter=name,duration=total_v,width=W,height=H,render_s=round(time.time()-t0,1),
           text=' '.join(ayah_text(s,n).translate(TANWEEN)+' ﴿'+str(n).translate(AR_DIGITS)+'﴾' for n,_,_ in ayat))
 return out,info

# ---------- free-form reels: your audio / your photos / a video, plus optional on-screen script ----------
def prep_audio(src,start,dur,dest):
 """Cut [start, start+dur] from any audio/video file, normalise to AAC with a short fade out. Returns (path, seconds)."""
 full=duration(src)
 if full<=0.5:raise ValueError('audio too short')
 start=max(0.0,min(float(start),max(0.0,full-1)))
 dur=max(1.0,min(float(dur),full-start))
 run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-ss','%.3f'%start,'-t','%.3f'%dur,'-i',str(src),'-vn','-ac','2','-ar','44100',
      '-af','afade=t=in:d=0.15,afade=t=out:st=%.3f:d=0.6'%max(0,dur-0.6),'-c:a','aac','-b:a','128k',str(dest)],180)
 return dest,dur

def script_chunks(text,maxw=9):
 """Split the on-screen script into short subtitle chunks (sentences, then at most ~9 words)."""
 import re
 parts=[p.strip() for p in re.split(r'(?<=[.!?؟。\n،؛])\s+|\n+',text.strip()) if p.strip()]
 out=[]
 for p in parts:
  w=p.split()
  for i in range(0,len(w),maxw):out.append(' '.join(w[i:i+maxw]))
 return out[:120]

def script_pages(text,W,H,total,work,start=0.3):
 from PIL import Image,ImageDraw
 chunks=script_chunks(text)
 if not chunks:return []
 size=46 if W<H else 40;f=font(UI_FONT,size);d=ImageDraw.Draw(Image.new('L',(8,8)))
 wts=[max(6,len(c)) for c in chunks];tot=sum(wts);span=max(0.5,total-start-0.2);t=start;pages=[]
 for k,(c,wt) in enumerate(zip(chunks,wts)):
  dur=span*wt/tot;lines=wrap(d,c,f,W-140)[:3]
  pages.append([caption_png(lines,W,H,work/('s%03d.png'%k),f,size),t,t+dur]);t+=dur
 return pages

def caption_png(lines,W,H,dest,f,size):
 """Subtitle-style text in the lower third on a soft dark band (readable over any photo or video)."""
 from PIL import Image,ImageDraw,ImageFilter
 lh=int(size*1.55);hgt=lh*len(lines)+30;top=int(H*(0.70 if W<H else 0.74))-hgt//2
 img=Image.new('RGBA',(W,H),(0,0,0,0));d=ImageDraw.Draw(img)
 widest=max(d.textlength(l,font=f,direction='rtl',language='ar') for l in lines)
 bx0=int(W/2-widest/2-28);bx1=int(W/2+widest/2+28)
 band=Image.new('RGBA',(W,H),(0,0,0,0));ImageDraw.Draw(band).rounded_rectangle((bx0,top,bx1,top+hgt),radius=22,fill=(0,0,0,135))
 img=Image.alpha_composite(band,img);d=ImageDraw.Draw(img)
 for i,l in enumerate(lines):d.text((W//2,top+15+i*lh),l,font=f,fill=(255,255,255,255),anchor='mt',direction='rtl',language='ar')
 box=img.getbbox() or (0,0,2,2);box=(max(0,box[0]-2)&~1,max(0,box[1]-2)&~1,min(W,box[2]+2),min(H,box[3]+2))
 img.crop(box).save(dest)
 return dest,box[0],box[1]

def render_media(spec,work,audio_src=None,images=(),video_src=None,library=(),logo=None):
 """Free-form reel. audio_src: any local audio/video file; images: local photos; video_src: local video used muted."""
 W,H=SIZES[spec['ar']]
 cap=float(spec.get('cap',60));want=max(3.0,min(float(spec.get('dur',30)),cap))
 work.mkdir(parents=True,exist_ok=True);t0=time.time()
 audio=None;total=want
 if audio_src:
  audio,total=prep_audio(audio_src,float(spec.get('start',0)),want,work/'audio.m4a')
 total=max(2.0,total)
 if video_src:
  track=video_track(W,H,video_src,total,work/'track.mp4')
 else:
  if images:
   n=len(images);per=total/n
   stills=[fit_cover(p,W,H,work/('st%02d.jpg'%i)) for i,p in enumerate(images)]
   if per<2.2:stills=stills[:max(1,int(total//2.2))]   # keep at least ~2 s per photo so transitions stay readable
  else:
   stills=backgrounds(W,H,work,max(1,min(8,math.ceil(total/7))),library)
  track=stills_track(W,H,stills,total,work/'track.mp4',move=bool(spec.get('move',True)))
  for b in stills:Path(b).unlink(missing_ok=True)
 pages=script_pages(spec.get('script') or '',W,H,total,work)
 chrome=chrome_png(W,H,'',spec.get('wm') or '',work/'chrome.png',bool(spec.get('vig',True)),logo)
 out=compose(W,H,track,total,pages,chrome,audio,work/'reel.mp4')
 return out,dict(duration=total,width=W,height=H,render_s=round(time.time()-t0,1))

def caption(info):
 head='📖 سورة %s · %s\n🎙 القارئ: %s'%(info['name'],('الآية %d'%info['frm']) if info['frm']==info['to'] else ('الآيات %d–%d'%(info['frm'],info['to'])),info['reciter'])
 tags='#قرآن #تلاوة #سورة_%s #ريلز_قرآن'%info['name'].replace(' ','_')
 room=1000-len(head)-len(tags)-8
 body=info['text'] if len(info['text'])<=room else info['text'][:room].rsplit(' ',1)[0]+' …'
 return head+'\n\n'+body+'\n\n'+tags
