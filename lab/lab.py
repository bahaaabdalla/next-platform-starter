"""BMediaHub lab: tries every candidate route for YouTube / Instagram from a Render free instance
and prints one 'LAB|' line per attempt. Throwaway diagnostics, not part of the production worker."""
import os, re, subprocess, threading, time, json, http.server, socketserver
import httpx

YT = {'watch': 'https://www.youtube.com/watch?v=4Jufjw8_PmU',
      'youtu.be': 'https://youtu.be/4Jufjw8_PmU',
      'shorts': 'https://www.youtube.com/shorts/4Jufjw8_PmU'}
IG = {'reel': ('reel', 'DeZrE2phzDx', 'https://www.instagram.com/reels/DeZrE2phzDx/'),
      'post': ('p', 'DaVtQaVDgyx', 'https://www.instagram.com/p/DaVtQaVDgyx/')}
CLIENTS = [None, 'tv', 'mweb', 'web_safari', 'android_vr', 'web_embedded', 'tv_simply']
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


def yt_probe(route, label, url, client=None, proxy=None):
    cmd = ['yt-dlp', '--dump-single-json', '--skip-download', '--no-playlist', '--no-warnings', '-4',
           '--socket-timeout', '15', '--extractor-retries', '1', '--retries', '1']
    if client:
        cmd += ['--extractor-args', 'youtube:player_client=' + client]
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 80)
    if rc == 0:
        try:
            info = json.loads(out)
            rec('YT', route, label, client or 'default', 'OK', 'formats=%d' % len(info.get('formats', [])), short(info.get('title'), 40))
            return True
        except Exception:
            pass
    rec('YT', route, label, client or 'default', 'FAIL', short(err or out))
    return False


def ig_ytdlp(route, label, url, proxy=None):
    cmd = ['yt-dlp', '--dump-single-json', '--skip-download', '--no-warnings', '--socket-timeout', '15', '--retries', '1']
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 70)
    rec('IG', route, label, 'yt-dlp', 'OK' if rc == 0 else 'FAIL', '' if rc == 0 else short(err or out))


def ig_gallery(route, label, url, proxy=None):
    cmd = ['gallery-dl', '--config-ignore', '--no-input', '-g', '--http-timeout', '15', '-R', '1']
    if proxy:
        cmd += ['--proxy', proxy]
    rc, out, err = run(cmd + [url], 70)
    urls = [l for l in out.splitlines() if l.startswith('http')]
    rec('IG', route, label, 'gallery-dl', 'OK urls=%d' % len(urls) if urls else 'FAIL', short(err or out) if not urls else '')


def ig_http(route, label, kind, code, proxy=None):
    """Embed page and third-party OG mirrors. Only reports whether a media URL is present, never saves anything."""
    targets = [('embed', 'https://www.instagram.com/%s/%s/embed/captioned/' % (kind, code), UA)]
    for host in ('www.ddinstagram.com', 'kkinstagram.com', 'www.vxinstagram.com'):
        targets.append((host, 'https://%s/%s/%s/' % (host, kind, code), 'TelegramBot (like TwitterBot)'))
    for name, url, ua in targets:
        try:
            with httpx.Client(proxy=proxy, follow_redirects=False, timeout=httpx.Timeout(20, connect=10), headers={'User-Agent': ua}) as c:
                r = c.get(url)
            body = r.text or ''
            hit = [k for k in ('video_url', 'display_url', 'og:video', 'og:image', 'EmbeddedMediaImage') if k in body]
            loc = r.headers.get('location', '')
            rec('IG', route, label, name, 'HTTP %d' % r.status_code, 'markers=%s' % ','.join(hit) if hit else 'no-media-markers',
                ('redirect->' + short(loc, 80)) if loc else '')
        except Exception as e:
            rec('IG', route, label, name, 'ERR', type(e).__name__, short(str(e), 100))


def start_warp():
    try:
        rc, out, err = run(['wgcf', 'register', '--accept-tos'], 60)
        rec('WARP', 'register', 'OK' if rc == 0 else 'FAIL', short(err or out, 120) if rc else '')
        if rc:
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
    rec('ENV', 'versions', short(run(['yt-dlp', '--version'])[1], 30), short(run(['gallery-dl', '--version'])[1], 30))
    routes = [('direct', None)]
    if start_warp():
        routes.append(('warp', SOCKS))
    for route, proxy in routes:
        for client in CLIENTS:
            yt_probe(route, 'watch', YT['watch'], client, proxy)
        for label in ('youtu.be', 'shorts'):
            yt_probe(route, label, YT[label], None, proxy)
        for label, (kind, code, url) in IG.items():
            ig_ytdlp(route, label, url, proxy)
            ig_gallery(route, label, url, proxy)
            ig_http(route, label, kind, code, proxy)
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
