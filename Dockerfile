FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /opt/tiktok-shop-monitor

COPY requirements.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends socat \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid monitor --create-home monitor
COPY --chown=monitor:monitor app ./app
COPY --chown=monitor:monitor scripts ./scripts
COPY --chown=monitor:monitor configs ./configs
COPY --chown=monitor:monitor migrations ./migrations
COPY --chown=monitor:monitor main.py pyproject.toml ./
COPY --chown=monitor:monitor data/seed ./data/seed
RUN mkdir -p data/samples/runtime data/exports/reports data/collections && chown -R monitor:monitor data
USER monitor

CMD ["python", "-m", "scripts.run_api"]
