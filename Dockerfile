FROM python:3.12-slim

# rar: the official RARLAB command-line compressor (Debian non-free). It creates the .rar archives.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends rar tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && command -v rar && rar | head -2

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# Set by the publish workflow (GitHub Actions run number, commit, time); shown in the web UI
ARG BUILD_NUMBER=dev
ARG BUILD_SHA=
ARG BUILD_DATE=
ENV BUILD_NUMBER=$BUILD_NUMBER BUILD_SHA=$BUILD_SHA BUILD_DATE=$BUILD_DATE

ENV DATA_DIR=/data \
    PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)" || exit 1

CMD ["sh", "-c", "exec waitress-serve --listen=0.0.0.0:${PORT} --threads=8 main:app"]
