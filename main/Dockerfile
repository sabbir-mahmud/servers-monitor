# Build from the repo root:  docker build -f main/Dockerfile .
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/main
RUN useradd --system --create-home app
COPY main/requirements.txt /app/main/
RUN pip install --no-cache-dir -r requirements.txt
COPY main/ /app/main/
# The agent files are served to servers by /agent/files/.
COPY agent/ /app/agent/
RUN SECRET_KEY=build DEBUG=True python manage.py collectstatic --noinput
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", "--timeout", "30", "--access-logfile", "-"]
