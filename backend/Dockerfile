FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data CLIPFORGE_ENV=production

# ffmpeg for video, fonts for burned-in captions (Noto covers Bengali, Devanagari, Arabic, Latin)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-noto-core fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* && fc-cache -f

RUN useradd --create-home --uid 10001 clipforge && mkdir -p /data && chown clipforge /data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=clipforge . .
USER clipforge

EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','10000')+'/health',timeout=4)"
# One process: the SQLite-backed job queue runs worker threads inside it.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
