FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ARXIV_CONFIG=/app/config.yml \
    ARXIV_DATA_DIR=/data \
    TZ=America/Los_Angeles \
    PORT=8765

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 arxiv \
    && useradd --uid 10001 --gid arxiv --no-create-home arxiv \
    && mkdir /data \
    && chown arxiv:arxiv /data \
    && chmod 700 /data

COPY slackbot_daily_arxiv.py bot_server.py web_app.py web_state.py digest_service.py arxiv_source.py ./
COPY important_people.txt keywords.txt ./
COPY templates/ ./templates/
COPY static/ ./static/

USER arxiv
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"

CMD ["python", "web_app.py"]
