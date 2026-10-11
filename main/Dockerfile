# Build from the repo root:  docker build -f main/Dockerfile .
# One image, two processes (see docker-compose.yml): the web UI and the relay bridge.
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SQLITE_PATH=/data/db.sqlite3 AGENT_DIST_DIR=/app/agent
WORKDIR /app
RUN useradd --system --create-home app && install -d -o app -g app /data
COPY main/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main/ .
# Not served to anyone: the dashboard hashes these so install/upgrade commands only accept
# exactly this agent code from GitHub.
COPY agent/ /app/agent/
RUN SECRET_KEY=build DEBUG=True python manage.py collectstatic --noinput
USER app
VOLUME /data
EXPOSE 8000
# One worker: the login throttle and SQLite both prefer a single process; threads give
# concurrency for HTMX polling.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "30", "--access-logfile", "-"]
