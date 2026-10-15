"""BMediaHub lab v2: tries candidate routes for YouTube / Instagram from a Render free instance
and prints one 'LAB|' line per attempt. Throwaway diagnostics, not part of the production worker."""
import os, re, subprocess, threading, time, json, glob, shutil, http.server, socketserver
import httpx

YT = {'watch': 'https://www.youtube.com/watch?v=4Jufjw8_PmU',
      'youtu.be': 'https://youtu.be/4Jufjw8_PmU',
      'shorts': 'https://www.youtube.com/shorts/4Jufjw8_PmU'}
IG = [('reel', 'https://www.instagram.com/reels/DeZrE2phzDx/'),
      ('post', 'https://www.instagram.com/p/DaVtQaVDgyx/')]
for i, u in enumerate([x.strip() for x in os.getenv('IG_EXTRA', '').split(',') if x.strip()]):
    IG.append(('extra%d' % (i + 1), u))
CLIENTS = [None, 'mweb', 'android_vr', 'web_embedded']
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
SOCKS = 'socks5h://127.0.0.1:1080'
REPORT = []


def rec(*parts):
    line = 'LAB|' + '|'.join(str(p) for p in parts)
    REPORT.append(line)
    print(line, flush=True)


def run(cmd, timeout=70):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ''), (p.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, '', 'timeout'


def short(text, n=170):
    return re.sub(r'\s+', ' ', text or '').strip()[:n]


def egress(proxy=None):
    cmd = ['curl', '-s', '-m', '15', 'https://www.cloudflare.com/cdn-cgi/trace']
    if proxy:
        cmd[1:1] = ['--proxy', proxy]
    rc, out, err = run(cmd, 25)
    info = dict(l.split('=', 1) for l in out.splitlines() if '=' in l)
    return 'ip=%s warp=%s colo=%s loc=%s' % (info.get('ip'), info.get('warp'), info.get('colo'), info.get('loc')) if info else 'FAIL ' + short(err)


def info_of(out):
    try:
        info = json.loads(out)
        if info.get('_type') == 'playlist' and info.get('entries'):
            ents = [e for e in info['entries'] if e]
            return 'entries=%d formats0=%d' % (len(ents), len((ents[0] or {}).get('formats', [])))
        return 'formats=%d ext=%s' % (len(info.get('formats', [])), info.get('ext'))
    except Exception:
        return 'unparsed'


def yt_probe(route, label, url, client=None, proxy=None):
    cmd = ['yt-dlp', '--dump-single-json', '--skip-download', '--no-playlist', '--no-warnings', '-4',
           '--socket-timeout', '15', '--extractor-retries', '1', '--retries', '1']
    if client:
        cmd += ['--extractor-args', 'youtube:player_client=' + client]
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 80)
    if rc == 0:
        rec('YT', route, label, client or 'default', 'OK', info_of(out))
    else:
        rec('YT', route, label, client or 'default', 'FAIL', short(err or out))


def download(tag, route, url, proxy=None, extra=None):
    """Real download of the smallest format (<=15MB) to prove the media itself is reachable, not just metadata."""
    d = '/tmp/lab_dl'
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    cmd = ['yt-dlp', '--no-playlist', '--no-warnings', '--socket-timeout', '15', '--retries', '1', '--max-filesize', '40M',
           '-f', 'bv*+ba/b', '-S', '+size,+br', '--merge-output-format', 'mp4', '-o', d + '/%(id)s.%(ext)s']
    if proxy:
        cmd += ['--proxy', proxy]
    if extra:
        cmd += extra
    rc, out, err = run(cmd + [url], 150)
    files = glob.glob(d + '/*')
    size = sum(os.path.getsize(f) for f in files)
    if files and size > 10000:
        rec('DL', tag, route, 'OK', 'bytes=%d' % size)
    else:
        rec('DL', tag, route, 'FAIL', short(err or out))


def ig_ytdlp(route, label, url, proxy=None, imp=False):
    cmd = ['yt-dlp', '--dump-single-json', '--skip-download', '--no-warnings', '--socket-timeout', '15', '--retries', '1']
    if imp:
        cmd += ['--impersonate', 'chrome']
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 70)
    rec('IG', route, label, 'yt-dlp+imp' if imp else 'yt-dlp', 'OK ' + info_of(out) if rc == 0 else 'FAIL', '' if rc == 0 else short(err or out))


def ig_gallery(route, label, url, proxy=None):
    cmd = ['gallery-dl', '--config-ignore', '--no-input', '-g', '--http-timeout', '15', '-R', '1']
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 70)
    urls = [l for l in out.splitlines() if l.startswith('http')]
    rec('IG', route, label, 'gallery-dl', 'OK urls=%d' % len(urls) if urls else 'FAIL', short(err or out) if not urls else '')


def ig_http(route, label, kind, code, proxy=None):
    """Embed page and third-party mirror. Reports whether a media URL is exposed and whether it is fetchable."""
    targets = [('embed', 'https://www.instagram.com/%s/%s/embed/captioned/' % (kind, code), UA),
               ('kkinstagram', 'https://kkinstagram.com/%s/%s/' % (kind, code), 'TelegramBot (like TwitterBot)')]
    for name, url, ua in targets:
        try:
            with httpx.Client(proxy=proxy, follow_redirects=False, timeout=httpx.Timeout(20, connect=10), headers={'User-Agent': ua}) as c:
                r = c.get(url)
                body = r.text or ''
                hit = [k for k in ('video_url', 'display_url', 'og:video', 'og:image', 'EmbeddedMediaImage') if k in body]
                loc = r.headers.get('location', '')
                extra = ''
                if loc and ('fbcdn' in loc or 'cdninstagram' in loc):
                    h = c.get(loc, headers={'Range': 'bytes=0-1023', 'User-Agent': UA})
                    extra = 'cdn->HTTP %d type=%s' % (h.status_code, h.headers.get('content-type'))
                elif loc:
                    extra = 'redirect->' + short(loc, 70)
            rec('IG', route, label, name, 'HTTP %d' % r.status_code, ('markers=' + ','.join(hit)) if hit else 'no-media-markers', extra)
        except Exception as e:
            rec('IG', route, label, name, 'ERR', type(e).__name__, short(str(e), 100))


def start_warp():
    try:
        rc, out, err = run(['wgcf', 'register', '--accept-tos'], 60)
        if rc:
            rec('WARP', 'register', 'FAIL', short(err or out, 120))
            return False
        rc, out, err = run(['wgcf', 'generate'], 30)
        if rc or not os.path.exists('wgcf-profile.conf'):
            rec('WARP', 'generate', 'FAIL', short(err or out))
            return False
        with open('wgcf-profile.conf') as f:
            conf = f.read()
        with open('wireproxy.conf', 'w') as f:
            f.write(conf + '\n[Socks5]\nBindAddress = 127.0.0.1:1080\n')
        subprocess.Popen(['wireproxy', '-c', 'wireproxy.conf'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(6)
        state = egress(SOCKS)
        rec('WARP', 'egress', state)
        return 'warp=on' in state or 'warp=plus' in state
    except Exception as e:
        rec('WARP', 'setup', 'ERR', type(e).__name__, short(str(e)))
        return False


def main():
    rec('ENV', 'direct-egress', egress())
    rec('ENV', 'ig-urls', len(IG))
    warp = start_warp()
    if os.getenv('LAB_MODE') == 'ytdl' and warp:
        for client in CLIENTS:
            ex = ['--extractor-args', 'youtube:player_client=' + client] if client else None
            download('youtube-' + (client or 'default'), 'warp', YT['watch'], SOCKS, ex)
        download('youtube-shorts', 'warp', YT['shorts'], SOCKS)
        rec('DONE', time.strftime('%H:%M:%S'))
        return
    routes = [('warp', SOCKS)] if warp else []
    routes.append(('direct', None))
    for route, proxy in routes:
        for client in (CLIENTS if route == 'warp' else [None]):
            yt_probe(route, 'watch', YT['watch'], client, proxy)
        for label in ('youtu.be', 'shorts'):
            yt_probe(route, label, YT[label], None, proxy)
        download('youtube', route, YT['watch'], proxy)
        for label, url in IG:
            m = re.search(r'/(reels?|p|tv)/([A-Za-z0-9_-]+)', url)
            kind = 'p' if (m and m.group(1) == 'p') else 'reel'
            code = m.group(2) if m else ''
            ig_ytdlp(route, label, url, proxy)
            ig_ytdlp(route, label, url, proxy, imp=True)
            ig_gallery(route, label, url, proxy)
            ig_http(route, label, kind, code, proxy)
            download('instagram-' + label, route, url, proxy)
    rec('DONE', time.strftime('%H:%M:%S'))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = '\n'.join(REPORT).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    threading.Thread(target=main, daemon=True).start()
    with socketserver.ThreadingTCPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), Handler) as s:
        s.serve_forever()
