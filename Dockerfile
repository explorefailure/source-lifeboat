FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN groupadd --system --gid 10001 repository-backup \
    && useradd --system --uid 10001 --gid repository-backup --home-dir /app repository-backup

WORKDIR /app
COPY --chown=repository-backup:repository-backup src ./src
RUN mkdir -p /data \
    && chown repository-backup:repository-backup /data

USER repository-backup
EXPOSE 8080
VOLUME ["/data"]
CMD ["python", "-m", "repository_backup.server"]
