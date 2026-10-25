"""Stock nature-video pool for reel backgrounds (Pexels + Pixabay, both free for commercial use with modification).

Clips are fetched in batches by theme, normalised once (1080x1920 or 1920x1080, 25 fps, silent, <=12 s) and kept on
disk with a review status. Only clips marked 'ok' are ever used under Quran text: a human (or Claude) reviews a
contact sheet first, because a stock search can always return people or unsuitable shots.

CLI (inside the worker container):
  python stock.py fetch sea v 30        # theme, orientation v|h, how many new clips
  python stock.py sheet [theme] [v|h]   # contact sheet of pending clips -> /data/stock/sheet.jpg (+ ids)
  python stock.py set ok  px123 pb456   # approve / reject ('no') clips
  python stock.py stats
"""
import json,os,random,re,subprocess,sys,threading,time
from pathlib import Path
import httpx

DATA=Path(os.getenv('MEDIA_DATA_DIR','/tmp/bmediahub'))
ROOT=DATA/'stock';ROOT.mkdir(parents=True,exist_ok=True)
INDEX=ROOT/'pool.json';_lock=threading.Lock()
PEXELS_KEY=os.getenv('PEXELS_API_KEY','');PIXABAY_KEY=os.getenv('PIXABAY_API_KEY','')
CLIP_SEC=12.0

# theme -> (Arabic label, search queries). Queries avoid people-heavy subjects; review still decides.
THEMES={
 'sea':('بحر',['ocean waves aerial','calm sea horizon','turquoise sea coast','sea waves rocks','ocean drone 4k','sea sunset waves']),
 'mountains':('جبال',['mountains aerial','snowy mountain peaks','mountain landscape clouds','alps drone']),
 'forest':('غابات',['forest aerial','green forest sunlight','misty forest','pine forest drone']),
 'sky':('سماء وسحاب',['clouds timelapse','sky clouds sunset','above the clouds','blue sky clouds moving','sun rays clouds']),
 'rain':('مطر',['rain on window','rain drops leaves','rain nature','raindrops water surface']),
 'desert':('صحراء',['desert dunes aerial','sand dunes','desert sunset','sahara landscape']),
 'snow':('تلج',['snow falling forest','winter landscape snow','snowy mountains','snowfall nature']),
 'water':('أنهار وشلالات',['waterfall nature','river stream forest','mountain river','waterfall aerial']),
 'sunset':('غروب وشروق',['sunset nature','sunrise landscape','golden hour field','sunset sea']),
 'night':('ليل ونجوم',['starry night sky','milky way timelapse','moon night sky','night sky stars','moon clouds night']),
 'flowers':('زهور وحقول',['flower field','wheat field wind','meadow flowers','lavender field']),
 'kaaba':('الكعبة ومكة',['kaaba','mecca','masjid al haram','makkah']),
 'mosque':('مساجد',['mosque','mosque aerial','mosque sunset','mosque minaret']),
 'calm':('هادية',['mosque night','rain window night','dark clouds sky','foggy mountains','street lamp night','candle dark','night city lights bokeh','misty lake dawn']),
}
CROWD_OK={'kaaba','mosque'}   # the Haram is never empty: only close-up faces disqualify a clip there
SIZE={'v':(1080,1920),'h':(1920,1080)}

def load():
    try:return json.loads(INDEX.read_text())
    except Exception:return []
def save(items):
    tmp=INDEX.with_suffix('.tmp');tmp.write_text(json.dumps(items,ensure_ascii=False));tmp.replace(INDEX)

def run(args,timeout=600):
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if p.returncode:raise RuntimeError('ffmpeg failed: '+p.stderr.decode('utf-8','ignore')[-300:])

# ---------- providers ----------
def pexels(query,orient,page):
    if not PEXELS_KEY:return []
    r=httpx.get('https://api.pexels.com/videos/search',params={'query':query,'orientation':'portrait' if orient=='v' else 'landscape',
                'size':'large','per_page':40,'page':page},headers={'Authorization':PEXELS_KEY},timeout=30);r.raise_for_status()
    W,H=SIZE[orient];out=[]
    for v in r.json().get('videos',[]):
        if not 6<=v.get('duration',0)<=90:continue
        files=[f for f in v.get('video_files',[]) if f.get('file_type')=='video/mp4' and (f.get('width') or 0)>=W and (f.get('height') or 0)>=H]
        if not files:continue
        f=min(files,key=lambda f:f['width']*f['height'])   # smallest file that still covers 1080p
        out.append({'id':'px%d'%v['id'],'url':f['link'],'author':(v.get('user') or {}).get('name',''),'link':v.get('url',''),'src':'Pexels','dur':v['duration']})
    return out

def pixabay(query,orient,page):
    if not PIXABAY_KEY:return []
    r=httpx.get('https://pixabay.com/api/videos/',params={'key':PIXABAY_KEY,'q':query,'video_type':'film','per_page':50,'page':page,'safesearch':'true'},timeout=30)
    r.raise_for_status();W,H=SIZE[orient];out=[]
    for v in r.json().get('hits',[]):
        if not 6<=v.get('duration',0)<=90:continue
        best=None
        for k in ('medium','large'):
            f=(v.get('videos') or {}).get(k) or {}
            w,h=f.get('width') or 0,f.get('height') or 0
            if not f.get('url'):continue
            # portrait from a landscape source only when the centre crop still has >=1080 px width
            ok=(w>=W and h>=H) if (orient=='h')==(w>=h) else (orient=='v' and h>=1920 and h*9/16>=1080)
            if ok:best=f;break
        if best:out.append({'sharp':max(best.get('width') or 0,best.get('height') or 0)>=3000 or (best.get('height') or 0)>(best.get('width') or 0),'id':'pb%d'%v['id'],'url':best['url'],'author':v.get('user',''),'link':v.get('pageURL',''),'src':'Pixabay','dur':v['duration']})
    return out

# ---------- fetch + normalise ----------
def normalise(src,dest,orient,dur):
    W,H=SIZE[orient];start=1.0 if dur>CLIP_SEC+2 else 0.0
    run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-ss','%.2f'%start,'-i',str(src),'-t','%.2f'%min(CLIP_SEC,dur-start),'-an',
         '-vf','scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1,fps=25,format=yuv420p'%(W,H,W,H),
         '-c:v','libx264','-preset','veryfast','-crf','20','-movflags','+faststart',str(dest)])

def fetch(theme,orient='v',n=20,log=print):
    if theme not in THEMES:raise ValueError('unknown theme')
    items=load();have={i['id'] for i in items};added=0;queries=list(THEMES[theme][1]);random.shuffle(queries)
    for q in queries:
        for page in (1,2,3):
            cands=[]
            for prov in (pexels,pixabay):
                try:cands+=prov(q,orient,page)
                except Exception as e:log('provider error',prov.__name__,type(e).__name__,str(e)[:120])
            random.shuffle(cands)
            for c in cands:
                key=c['id']+'_'+orient
                if key in have:continue
                dest=ROOT/theme/(key+'.mp4');dest.parent.mkdir(parents=True,exist_ok=True);raw=dest.with_suffix('.raw')
                try:
                    with httpx.stream('GET',c['url'],timeout=httpx.Timeout(120,connect=20),follow_redirects=True) as r:
                        r.raise_for_status();n_b=0
                        with open(raw,'wb') as fh:
                            for ch in r.iter_bytes(1<<16):
                                n_b+=len(ch)
                                if n_b>400*1024*1024:raise RuntimeError('too big')
                                fh.write(ch)
                    normalise(raw,dest,orient,c['dur'])
                except Exception as e:
                    log('skip',c['id'],type(e).__name__,str(e)[:100]);dest.unlink(missing_ok=True);continue
                finally:raw.unlink(missing_ok=True)
                rec=dict(c,id=key,theme=theme,orient=orient,file=str(dest.relative_to(ROOT)),status='pending',added=int(time.time()))
                with _lock:
                    items=load();items.append(rec);save(items)
                have.add(key);added+=1;log('added',key,c['src'],theme)
                if added>=n:return added
    return added

# ---------- review ----------
def sheet(theme=None,orient=None,limit=48,dest=None):
    """Contact sheet (3 frames per clip, id labels) of pending clips for review."""
    from PIL import Image,ImageDraw
    items=[i for i in load() if i['status']=='pending' and (not theme or i['theme']==theme) and (not orient or i['orient']==orient)][:limit]
    if not items:return None,[]
    tiles=[]
    for it in items:
        f=ROOT/it['file'];tmp=ROOT/('_t_%s.jpg'%it['id'])
        try:
            run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(f),'-vf','fps=3/12,scale=-2:220,tile=3x1','-frames:v','1',str(tmp)],60)
            im=Image.open(tmp).convert('RGB');tmp.unlink(missing_ok=True)
        except Exception:continue
        c=Image.new('RGB',(im.width,im.height+22),'black');c.paste(im,(0,22));ImageDraw.Draw(c).text((4,4),it['id']+'  '+it['theme'],fill='yellow')
        tiles.append(c)
    cols=3 if items[0]['orient']=='v' else 2;w=max(t.width for t in tiles);h=max(t.height for t in tiles)
    out=Image.new('RGB',(w*cols,h*((len(tiles)+cols-1)//cols)),'black')
    for k,t in enumerate(tiles):out.paste(t,((k%cols)*w,(k//cols)*h))
    dest=Path(dest or ROOT/'sheet.jpg');out.save(dest,quality=80)
    return dest,[i['id'] for i in items]

def set_status(status,ids):
    with _lock:
        items=load();n=0
        for i in items:
            if i['id'] in ids:
                i['status']=status;n+=1
                if status=='no':(ROOT/i['file']).unlink(missing_ok=True)
        if status=='no':items=[i for i in items if not (i['id'] in ids)]
        save(items)
    return n

def stats():
    s={}
    for i in load():
        k=(i['theme'],i['orient']);d=s.setdefault('%s/%s'%k,{'ok':0,'pending':0});d[i['status']]=d.get(i['status'],0)+1
    return s

def themes_available(orient):
    return sorted({i['theme'] for i in load() if i['status']=='ok' and i['orient']==orient})

def pick(theme,orient,n):
    """n approved clip paths for a reel: the theme's clips, topped up from any theme; [] if the pool is empty."""
    ok=[i for i in load() if i['status']=='ok' and i['orient']==orient and (ROOT/i['file']).exists()]
    if not ok:return [],[]
    mine=[i for i in ok if i['theme']==theme] if theme and theme!='mix' else ok
    random.shuffle(mine);sel=mine[:n]
    if len(sel)<n:
        rest=[i for i in ok if i not in sel];random.shuffle(rest);sel+=rest[:n-len(sel)]
    return [ROOT/i['file'] for i in sel],sorted({i['src'] for i in sel})


# ---------- live mode: one fresh video per reel, nothing kept ----------
def _frames(path,work,n=5):
    dur=0.0
    try:dur=float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(path)],capture_output=True,text=True,timeout=30).stdout.strip() or 0)
    except Exception:pass
    out=[]
    for k in range(n):
        f=Path(work)/('_chk%d.jpg'%k)
        t=max(0.0,dur*(k+0.5)/n) if dur else k
        try:run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-ss','%.2f'%t,'-i',str(path),'-frames:v','1','-vf','scale=640:-2',str(f)],60);out.append(f)
        except Exception:pass
    return out

LUMA_HI=float(os.getenv('STOCK_LUMA_HI','225'))   # verse band brighter than this = blown out even with a text panel (glare, white-out)
LUMA_LO=float(os.getenv('STOCK_LUMA_LO','32'))    # whole frame darker than this = a dull black reel

def screen(path,work,crowd_ok=False):
    """One pass over 5 sampled frames: (people, band, mean). band = brightness (0-255) of the centre area where the
    verses sit; mean = whole frame. People: faces in 3+ frames or a confident person shape (HOG > 1.0) in 2+
    (tuned on real clips: a child close-up hits faces 5/5; mossy rocks give at most 2 stray face hits)."""
    try:
        import cv2,numpy as np
    except Exception:return False,0.0,128.0
    hog=cv2.HOGDescriptor();hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    faces=[cv2.CascadeClassifier(cv2.data.haarcascades+n) for n in ('haarcascade_frontalface_default.xml','haarcascade_profileface.xml')]
    fhits=phits=0;bands=[];means=[]
    for f in _frames(path,work):
        im=cv2.imread(str(f));f.unlink(missing_ok=True)
        if im is None:continue
        g=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY);h,w=g.shape
        # vertical reels show the centre of the clip; the verse block sits around 30-62% of the height
        bands.append(float(np.percentile(g[int(h*.30):int(h*.62),int(w*.30):int(w*.70)],70)));means.append(float(g.mean()))
        if any(len(c.detectMultiScale(g,scaleFactor=1.1,minNeighbors=7,minSize=(36,36))) for c in faces):fhits+=1
        _,wt=hog.detectMultiScale(im,winStride=(8,8),padding=(8,8),scale=1.05)
        wt=np.array(wt).ravel()
        if wt.size and float(wt.max())>1.0:phits+=1
    band=float(np.median(bands)) if bands else 0.0;mean=float(np.median(means)) if means else 128.0
    return (fhits>=3 if crowd_ok else (fhits>=3 or phits>=2)),band,mean

def has_people(path,work):return screen(path,work)[0]

def tone_ok(band,mean):return band<=LUMA_HI and mean>=LUMA_LO

def live_candidates(theme,orient,mindur,strict=False):
    """Fresh candidates for one reel. Small themes (Kaaba, mosques) have only a handful of clips, so a random later
    page can be empty: fall back to page 1, then to the non-strict quality rule, then to every query."""
    qs=list(THEMES.get(theme,THEMES['sea'])[1]) if theme in THEMES else [q for k,t in THEMES.items() if k not in CROWD_OK for q in t[1]]   # 'mix' = nature only
    random.shuffle(qs)
    def search(queries,st,ed=False):
        out=[]
        for q in queries:
            for page in ((1,) if ed else dict.fromkeys((random.randint(1,2),1))):
                got=[]
                for prov in (pexels,pixabay_live):
                    try:got+=[c for c in (prov(q,orient,page,st,ed) if prov is pixabay_live else ([] if ed else prov(q,orient,page))) if c['dur']>=mindur]
                    except Exception as e:print('stock provider',prov.__name__,type(e).__name__,str(e)[:100],flush=True)
                out+=got
                if got:break
            if len(out)>=10:break
        return out
    # Pixabay's editors' choice (hand-picked, high quality) first, then popular results
    out=search(qs[:3],strict,True)
    if len(out)<4:out+=search(qs[:2],strict)
    if len(out)<3 and strict:out+=search(qs[:2],False)
    if len(out)<3:out+=search(qs[2:],False)
    seen=set();out=[c for c in out if not (c['id'] in seen or seen.add(c['id']))]
    # quality first, but keep variety: shuffle inside tiers (editors' choice > sharp > popular)
    random.shuffle(out)
    out.sort(key=lambda c:(not c.get('ed'),not (strict and c.get('sharp')),-min(c.get('pop',0),5000)//1000))
    return out

def pixabay_live(query,orient,page,strict=False,editors=False):
    """Like pixabay() but accepts 1080p landscape for vertical reels too (the reel is 720x1280, a centre crop of 1080p is enough)."""
    if not PIXABAY_KEY:return []
    r=httpx.get('https://pixabay.com/api/videos/',params={'key':PIXABAY_KEY,'q':query,'video_type':'film','per_page':30,'page':page,'safesearch':'true','order':'popular',**({'editors_choice':'true'} if editors else {})},timeout=30)
    r.raise_for_status();out=[]
    for v in r.json().get('hits',[]):
        vids=v.get('videos') or {};best=None
        # vertical reels crop the centre of a landscape clip: 4K keeps it sharp if the file is small enough to fetch fast;
        # horizontal reels (1280x720) are fine from 1080p
        order=('large','medium') if orient=='v' else ('medium','large')
        for k in order:
            f=vids.get(k) or {}
            if not f.get('url') or (f.get('height') or 0)<1080 or (f.get('size') or 0)>(100 if k=='large' else 150)*1024*1024:continue
            # a 1080x1920 reel cut from a landscape 1080p clip would be upscaled ~1.8x and look soft: vertical reels
            # take 4K, or 1080p only when the clip itself is portrait
            if strict and orient=='v' and k=='medium' and (f.get('width') or 0)>(f.get('height') or 0):continue
            best=f;break
        if best:out.append({'sharp':max(best.get('width') or 0,best.get('height') or 0)>=3000 or (best.get('height') or 0)>(best.get('width') or 0),'id':'pb%d'%v['id'],'url':best['url'],'author':v.get('user',''),'link':v.get('pageURL',''),'src':'Pixabay','dur':v.get('duration',0),'pop':int(v.get('downloads',0))+20*int(v.get('likes',0)),'ed':editors})
    return out

def live_pick(theme,orient,dur,work,tries=6,hd=False):
    """Search, download and screen one video for this reel. Returns (path, source, band) or (None, None, 0).
    Rejects people and clips too bright behind the verses or too dark overall; if every candidate fails only on
    brightness, the least bright one is used (the render darkens it). The file is deleted with the work folder."""
    spare=None
    for c in live_candidates(theme,orient,min(dur,8),hd)[:tries]:
        dest=Path(work)/('stock_%s.mp4'%c['id'])
        try:
            with httpx.stream('GET',c['url'],timeout=httpx.Timeout(90,connect=20),follow_redirects=True) as r:
                r.raise_for_status();n=0
                with open(dest,'wb') as fh:
                    for ch in r.iter_bytes(1<<16):
                        n+=len(ch)
                        if n>150*1024*1024:raise RuntimeError('too big')
                        fh.write(ch)
            people,band,mean=screen(dest,work,theme in CROWD_OK)
            if people:print('stock skip (people)',c['id'],flush=True);dest.unlink(missing_ok=True);continue
            if not tone_ok(band,mean):
                print('stock skip (tone) band %.0f mean %.0f'%(band,mean),c['id'],flush=True)
                score=max(band-LUMA_HI,LUMA_LO-mean)
                if spare is None or score<spare[0]:
                    if spare:spare[1].unlink(missing_ok=True)
                    spare=(score,dest,c['src'],band)
                else:dest.unlink(missing_ok=True)
                continue
            if spare:spare[1].unlink(missing_ok=True)
            print('stock ok band %.0f mean %.0f'%(band,mean),c['id'],flush=True)
            return dest,c['src'],band
        except Exception as e:
            print('stock skip',c['id'],type(e).__name__,str(e)[:100],flush=True);dest.unlink(missing_ok=True)
    if spare:return spare[1],spare[2],spare[3]
    return None,None,0.0

# ---------- high-resolution free photos (Wikimedia Commons) for themes where good video is scarce ----------
# Only free licences (public domain, CC0, CC BY, CC BY-SA); the authors are credited in the reel caption.
PHOTO_THEMES={'kaaba':['Kaaba','Masjid al-Haram','Kaaba night','Mataf Kaaba','Great Mosque of Mecca']}
PHOTO_DIR=ROOT/'photos';PHOTO_DIR.mkdir(parents=True,exist_ok=True)
UA={'User-Agent':'BMediaHub/1.0 (reel backgrounds; contact bahaadev.duckdns.org)'}
FREE_LIC=re.compile(r'^(cc0|public domain|pd|cc[ -]by([ -]sa)?([ -][0-9.]+)?)$',re.I)
BAD_TITLE=re.compile(r'(map|plan|diagram|drawing|logo|svg|coin|stamp|banknote|poster|painting|manuscript|sketch|model)',re.I)

def _photo_index(theme):
    f=PHOTO_DIR/(theme+'.json')
    try:return json.loads(f.read_text())
    except Exception:return {'ts':0,'items':[]}

def commons_refresh(theme,want=40):
    """Search Commons, keep big free-licence photos without close-up faces, cache them (about 0.5-1.5 MB each)."""
    idx=_photo_index(theme);have={i['id'] for i in idx['items']};bad=set(idx.get('bad',[]))
    for q in PHOTO_THEMES[theme]:
        if len(idx['items'])>=want:break
        try:r=httpx.get('https://commons.wikimedia.org/w/api.php',headers=UA,timeout=30,params={'action':'query','format':'json','generator':'search',
            'gsrsearch':'filetype:bitmap '+q,'gsrnamespace':6,'gsrlimit':50,'prop':'imageinfo','iiprop':'url|size|extmetadata','iiurlwidth':4000,'iiurlheight':2400})
        except Exception as e:print('commons search',type(e).__name__,flush=True);continue
        for pg in (r.json().get('query',{}).get('pages') or {}).values():
            pid=str(pg.get('pageid'));ii=(pg.get('imageinfo') or [{}])[0];meta=ii.get('extmetadata') or {}
            if pid in have or pid in bad or BAD_TITLE.search(pg.get('title','')):continue
            lic=re.sub('<[^>]+>','',(meta.get('LicenseShortName') or {}).get('value','')).strip()
            if not FREE_LIC.match(lic) or (ii.get('width') or 0)<1900 or (ii.get('height') or 0)<1900:continue
            dest=PHOTO_DIR/(theme+'_'+pid+'.jpg')
            try:
                b=httpx.get(ii.get('thumburl') or ii['url'],headers=UA,timeout=60,follow_redirects=True);b.raise_for_status();dest.write_bytes(b.content)
                people,band,mean=screen_photo(dest)
                if people or mean<25:dest.unlink(missing_ok=True);bad.add(pid);continue
            except Exception as e:print('commons get',pid,type(e).__name__,flush=True);dest.unlink(missing_ok=True);continue
            artist=re.sub('<[^>]+>','',(meta.get('Artist') or {}).get('value','')).strip()[:40]
            idx['items'].append({'id':pid,'file':dest.name,'lic':lic,'by':artist,'band':band,'w':ii.get('width'),'h':ii.get('height')});have.add(pid)
            if len(idx['items'])>=want:break
    idx['ts']=time.time();idx['bad']=sorted(bad)[-500:]
    (PHOTO_DIR/(theme+'.json')).write_text(json.dumps(idx,ensure_ascii=False))
    return len(idx['items'])

def screen_photo(path):
    """(close-up faces, centre brightness, mean brightness) for one photo."""
    try:
        import cv2,numpy as np
        im=cv2.imread(str(path));h,w=im.shape[:2];s=900/max(h,w);im=cv2.resize(im,(int(w*s),int(h*s)))
        g=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY);h,w=g.shape
        fc=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
        big=[f for f in fc.detectMultiScale(g,scaleFactor=1.1,minNeighbors=8,minSize=(int(h*0.08),int(h*0.08)))]
        return bool(big),float(np.percentile(g[int(h*.30):int(h*.62),int(w*.30):int(w*.70)],70)),float(g.mean())
    except Exception:return False,128.0,128.0

def photo_pick(theme,n=5):
    """n random cached photos for the theme (refreshing the cache when it is small or older than a week).
    Returns ([paths], credit text, median brightness)."""
    idx=_photo_index(theme)
    if len(idx['items'])<12 or time.time()-idx['ts']>7*86400:
        try:commons_refresh(theme);idx=_photo_index(theme)
        except Exception as e:print('commons refresh',type(e).__name__,str(e)[:100],flush=True)
    # only photos a human approved ('ok'); new ones from a refresh wait for review (python stock.py photos kaaba -> sheet)
    items=[i for i in idx['items'] if i.get('ok') and (PHOTO_DIR/i['file']).exists()]
    if not items:return [],'',0.0
    pick=random.sample(items,min(n,len(items)))
    by=sorted({i['by'] for i in pick if i['by']})[:3]
    credit='Wikimedia Commons'+(' ('+'، '.join(by)+')' if by else '')
    bands=sorted(i['band'] for i in pick)
    return [PHOTO_DIR/i['file'] for i in pick],credit,bands[len(bands)//2]

if __name__=='__main__':
    a=sys.argv[1:]
    if a[:1]==['fetch']:print('added',fetch(a[1],a[2] if len(a)>2 else 'v',int(a[3]) if len(a)>3 else 20))
    elif a[:1]==['sheet']:d,ids=sheet(a[1] if len(a)>1 and a[1]!='-' else None,a[2] if len(a)>2 else None);print(d);print(' '.join(ids))
    elif a[:1]==['set']:print('updated',set_status(a[1],set(a[2:])))
    elif a[:1]==['stats']:print(json.dumps(stats(),ensure_ascii=False,indent=1))
    elif a[:1]==['photo_ok']:   # photo_ok kaaba <ids...>  approve photos by Commons page id
        f=PHOTO_DIR/(a[1]+'.json');idx=json.loads(f.read_text());ids=set(a[2:])
        for i in idx['items']:i['ok']=i.get('ok') or i['id'] in ids
        f.write_text(json.dumps(idx,ensure_ascii=False));print('approved',sum(1 for i in idx['items'] if i.get('ok')))
    else:print(__doc__)
