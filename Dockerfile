FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

WORKDIR /app

COPY pyproject.toml README.md ./
COPY moex_watcher ./moex_watcher
RUN pip install . && useradd --create-home --uid 10001 watcher && mkdir -p /app/data && chown watcher /app/data

USER watcher
VOLUME ["/app/data"]

ENTRYPOINT ["moex-watcher"]
CMD ["watch"]
