FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/app /app/app
COPY backend/migrations /app/migrations
COPY backend/alembic.ini /app/alembic.ini
RUN pip install --no-cache-dir .

RUN mkdir -p /data/db /data/frames /data/thumbs /data/backups
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["python", "-m", "app.cli"]
CMD ["serve"]
