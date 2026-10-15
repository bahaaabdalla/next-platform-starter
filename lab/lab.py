"""BMediaHub lab v3: measures how reliable WARP egress is for YouTube (IPv4-forced vs dual-stack, several fresh
identities) and re-tests Instagram links. Prints one 'LAB|' line per attempt. Throwaway diagnostics."""
import os, re, subprocess, threading, time, json, glob, shutil, http.server, socketserver
import httpx

YT = {'watch': 'https://www.youtube.com/watch?v=4Jufjw8_PmU',
      'youtu.be': 'https://youtu.be/4Jufjw8_PmU',
      'shorts': 'https://www.youtube.com/shorts/4Jufjw8_PmU'}
IG = [('reel1', 'https://www.instagram.com/reels/DeZrE2phzDx/'),
      ('post1', 'https://www.instagram.com/p/DaVtQaVDgyx/')]
for i, u in enumerate([x.strip() for x in os.getenv('IG_EXTRA', '').split(',') if x.strip()]):
    IG.append(('extra%d' % (i + 1), u))
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
SOCKS_LOCAL = 'socks5://127.0.0.1:1080'   # DNS resolved locally -> with -4 the tunnel is only asked for IPv4 targets
SOCKS_REMOTE = 'socks5h://127.0.0.1:1080'
REPORT = []
WP = {'proc': None}


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


def egress(proxy=None, v4=False):
    cmd = ['curl', '-s', '-m', '15', 'https://www.cloudflare.com/cdn-cgi/trace']
    if v4:
        cmd.insert(1, '-4')
    if proxy:
        cmd[1:1] = ['--proxy', proxy]
    rc, out, err = run(cmd, 25)
    info = dict(l.split('=', 1) for l in out.splitlines() if '=' in l)
    return 'ip=%s warp=%s colo=%s' % (info.get('ip'), info.get('warp'), info.get('colo')) if info else 'FAIL ' + short(err)


def info_of(out):
    try:
        info = json.loads(out)
        if info.get('_type') == 'playlist' and info.get('entries'):
            ents = [e for e in info['entries'] if e]
            return 'entries=%d formats0=%d' % (len(ents), len((ents[0] or {}).get('formats', [])))
        return 'formats=%d ext=%s' % (len(info.get('formats', [])), info.get('ext'))
    except Exception:
        return 'unparsed'


def yt_probe(tag, url, proxy=None):
    cmd = ['yt-dlp', '--dump-single-json', '--skip-download', '--no-playlist', '--no-warnings', '-4',
           '--socket-timeout', '15', '--extractor-retries', '1', '--retries', '1']
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 80)
    rec('YT', tag, 'OK' if rc == 0 else 'FAIL', info_of(out) if rc == 0 else short(err or out, 90))
    return rc == 0


def download(tag, url, proxy=None, extra=None):
    d = '/tmp/lab_dl'
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    cmd = ['yt-dlp', '--no-playlist', '--no-warnings', '-4', '--socket-timeout', '15', '--retries', '1', '--max-filesize', '40M',
           '-f', 'bv*+ba/b', '-S', '+size,+br', '--merge-output-format', 'mp4', '-o', d + '/%(id)s.%(ext)s']
    if proxy:
        cmd += ['--proxy', proxy]
    if extra:
        cmd += extra
    rc, out, err = run(cmd + [url], 150)
    files = glob.glob(d + '/*')
    size = sum(os.path.getsize(f) for f in files)
    if files and size > 10000:
        rec('DL', tag, 'OK', 'bytes=%d' % size)
        return True
    rec('DL', tag, 'FAIL', short(err or out, 90))
    return False


def ig_ytdlp(label, url, proxy=None, imp=False):
    cmd = ['yt-dlp', '--dump-single-json', '--skip-download', '--no-warnings', '--socket-timeout', '15', '--retries', '1']
    if imp:
        cmd += ['--impersonate', 'chrome']
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 70)
    rec('IG', label, 'yt-dlp+imp' if imp else 'yt-dlp', 'OK ' + info_of(out) if rc == 0 else 'FAIL ' + short(err or out, 110))


def ig_gallery(label, url, proxy=None):
    cmd = ['gallery-dl', '--config-ignore', '--no-input', '-g', '--http-timeout', '15', '-R', '1']
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 70)
    urls = [l for l in out.splitlines() if l.startswith('http')]
    rec('IG', label, 'gallery-dl', ('OK urls=%d' % len(urls)) if urls else 'FAIL ' + short(err or out, 90))


def ig_http(label, kind, code):
    targets = [('embed', 'https://www.instagram.com/%s/%s/embed/captioned/' % (kind, code), UA),
               ('kkinstagram', 'https://kkinstagram.com/%s/%s/' % (kind, code), 'TelegramBot (like TwitterBot)')]
    for name, url, ua in targets:
        try:
            with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(20, connect=10), headers={'User-Agent': ua}) as c:
                r = c.get(url)
                body = r.text or ''
                hit = [k for k in ('video_url', 'display_url', 'og:video', 'og:image', 'EmbeddedMediaImage') if k in body]
                loc = r.headers.get('location', '')
                extra = ''
                if loc and ('fbcdn' in loc or 'cdninstagram' in loc):
                    h = c.get(loc, headers={'Range': 'bytes=0-1023', 'User-Agent': UA})
                    extra = 'cdn->HTTP %d' % h.status_code
                elif loc:
                    extra = 'redirect->' + short(loc, 60)
            rec('IG', label, name, 'HTTP %d' % r.status_code, ('markers=' + ','.join(hit)) if hit else 'no-media-markers', extra)
        except Exception as e:
            rec('IG', label, name, 'ERR', type(e).__name__)


def stop_warp():
    p = WP.get('proc')
    if p:
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            p.kill()
        WP['proc'] = None
    time.sleep(1)


def start_warp(v4only):
    """Fresh WARP identity each call. v4only strips the IPv6 address so the tunnel only carries IPv4."""
    stop_warp()
    for f in ('wgcf-account.toml', 'wgcf-profile.conf', 'wireproxy.conf'):
        if os.path.exists(f):
            os.remove(f)
    rc, out, err = run(['wgcf', 'register', '--accept-tos'], 60)
    if rc:
        rec('WARP', 'register', 'FAIL', short(err or out, 100))
        return False
    rc, out, err = run(['wgcf', 'generate'], 30)
    if rc or not os.path.exists('wgcf-profile.conf'):
        rec('WARP', 'generate', 'FAIL', short(err or out, 100))
        return False
    conf = open('wgcf-profile.conf').read()
    if v4only:
        out_lines = []
        for line in conf.splitlines():
            if line.startswith('Address'):
                addrs = [a.strip() for a in line.split('=', 1)[1].split(',') if ':' not in a]
                line = 'Address = ' + ', '.join(addrs)
            elif line.startswith('AllowedIPs'):
                line = 'AllowedIPs = 0.0.0.0/0'
            out_lines.append(line)
        conf = '\n'.join(out_lines)
    open('wireproxy.conf', 'w').write(conf + '\n[Socks5]\nBindAddress = 127.0.0.1:1080\n')
    WP['proc'] = subprocess.Popen(['wireproxy', '-c', 'wireproxy.conf'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    return True


def ig_all(label, url):
    m = re.search(r'/(reels?|p|tv)/([A-Za-z0-9_-]+)', url)
    kind = 'p' if (m and m.group(1) == 'p') else 'reel'
    code = m.group(2) if m else ''
    ig_ytdlp(label, url)
    ig_ytdlp(label, url, imp=True)
    ig_gallery(label, url)
    ig_http(label, kind, code)
    download('instagram-' + label, url)


def main():
    rec('ENV', 'direct-egress', egress())
    for label, url in IG:
        ig_all(label, url)
    for i in range(5):
        v4 = True
        if not start_warp(v4only=v4):
            continue
        rec('WARP', 'id%d-v4only' % i, egress(SOCKS_LOCAL, v4=True))
        ok = yt_probe('id%d-v4only-localdns' % i, YT['watch'], SOCKS_LOCAL)
        if ok:
            download('youtube-id%d-v4only' % i, YT['watch'], SOCKS_LOCAL)
        if i < 2:  # compare against dual-stack on the same fresh identity
            if start_warp(v4only=False):
                rec('WARP', 'id%d-dual' % i, egress(SOCKS_REMOTE))
                yt_probe('id%d-dual-remotedns' % i, YT['watch'], SOCKS_REMOTE)
    stop_warp()
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
