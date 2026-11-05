FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PASHTOON_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pashtoon_site ./pashtoon_site
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
USER app
# Everything that matters (database, uploaded photos and videos, backups) lives in /data: mount a volume there.
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
# One process only: the background jobs (backups, held alerts, tiffin meals) run inside it.
CMD ["uvicorn", "pashtoon_site.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
