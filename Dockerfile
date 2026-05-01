FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

# Deno: JS runtime that yt-dlp uses to solve YouTube's anti-bot challenge.
RUN ARCH=$(uname -m) \
    && case "$ARCH" in \
         x86_64)  DENO_ARCH=x86_64-unknown-linux-gnu ;; \
         aarch64) DENO_ARCH=aarch64-unknown-linux-gnu ;; \
         *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
       esac \
    && curl -fL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && chmod +x /usr/local/bin/deno

WORKDIR /app
COPY server.py .

ENV HOST=0.0.0.0 \
    PORT=10000 \
    STREAMFLOW_DIR=/data \
    DEFAULT_BROWSER=none \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 10000

CMD ["python", "server.py"]
