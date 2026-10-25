#!/bin/bash
set -e
curl -fsSL https://bmediahub-telegram.vercel.app/dl/w-66a789aa035d7fd6f8c98393.tgz -o /tmp/w.tgz
echo "bec333ecb0e524292fe6e61c7d6a8f947e47db5e49dbaf9171140873e9e3eee3  /tmp/w.tgz" | sha256sum -c
tar xzf /tmp/w.tgz -C /opt/bmh/mediahub-worker
cd /opt/bmh && docker compose up -d --build worker
echo; echo "=== DONE: worker updated ==="
