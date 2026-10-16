FROM python:3.12-slim
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl libfribidi0 libharfbuzz0b && rm -rf /var/lib/apt/lists/*
# WARP tooling: wgcf registers a free Cloudflare WARP identity, wireproxy exposes it as a local SOCKS5 proxy
# (userspace WireGuard, no NET_ADMIN needed). Tags are resolved via the releases/latest redirect (no GitHub API rate limit).
RUN curl -fsSL https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz | tar -xz -C /usr/local/bin wireproxy \
 && TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/ViRb3/wgcf/releases/latest | sed 's#.*/tag/##') \
 && VER=${TAG#v} \
 && curl -fsSL -o /usr/local/bin/wgcf "https://github.com/ViRb3/wgcf/releases/download/${TAG}/wgcf_${VER}_linux_amd64" \
 && chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy && echo "wgcf tag: ${TAG}"
WORKDIR /app
COPY mediahub-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mediahub-worker/data ./data
COPY mediahub-worker/fonts ./fonts
COPY mediahub-worker/reels.py mediahub-worker/server.py ./
# Arabic shaping for the reel text needs Pillow's raqm layout; fail the build loudly if it is missing
RUN python -c "from PIL import features;assert features.check('raqm'),'raqm missing'"
ENV PORT=10000
CMD ["sh","-c","uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
