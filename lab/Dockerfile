FROM python:3.12-slim
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "yt-dlp[default,curl-cffi]" gallery-dl "httpx[socks]" "requests[socks]" PySocks
# WARP tooling: wgcf registers a free WARP identity, wireproxy exposes it as a local SOCKS5 proxy (userspace, no NET_ADMIN needed)
RUN curl -fsSL https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz | tar -xz -C /usr/local/bin wireproxy \
 && TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/ViRb3/wgcf/releases/latest | sed 's#.*/tag/##') \
 && VER=${TAG#v} \
 && curl -fsSL -o /usr/local/bin/wgcf "https://github.com/ViRb3/wgcf/releases/download/${TAG}/wgcf_${VER}_linux_amd64" \
 && chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy && echo "wgcf tag: ${TAG}"
WORKDIR /app
COPY lab.py .
ENV PORT=10000
CMD ["python","lab.py"]
