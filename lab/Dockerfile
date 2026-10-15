FROM python:3.12-slim
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "yt-dlp[default]" gallery-dl httpx
# WARP tooling: wgcf registers a free WARP identity, wireproxy exposes it as a local SOCKS5 proxy (userspace, no NET_ADMIN needed)
RUN curl -fsSL https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz | tar -xz -C /usr/local/bin wireproxy \
 && URL=$(curl -fsSL https://api.github.com/repos/ViRb3/wgcf/releases/latest | grep browser_download_url | grep linux_amd64 | head -1 | cut -d '"' -f4) \
 && curl -fsSL -o /usr/local/bin/wgcf "$URL" && chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy
WORKDIR /app
COPY lab.py .
ENV PORT=10000
CMD ["python","lab.py"]
