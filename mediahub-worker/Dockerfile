FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY mediahub-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mediahub-worker/server.py .
ENV PORT=10000
CMD ["sh","-c","uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
