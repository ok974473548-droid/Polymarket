# Интерфейс (frontend/: Svelte + TypeScript) собирается здесь; в образ попадает только готовая сборка.
FROM node:22-slim AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WF_DB_URL=sqlite:////data/whalefollow.db WF_LOG_FORMAT=json
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY whalefollow ./whalefollow
COPY --from=web /src/whalefollow/web ./whalefollow/web
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"
# расширение DuckDB для чтения истории прямо из файла SQLite — ставится при сборке, а не при первом запуске
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL sqlite')"
RUN mkdir -p /data
# База — в /data: в docker compose это том wf-data, на Railway — Volume с путём /data
# (инструкцию VOLUME Railway не принимает, поэтому её здесь нет).
EXPOSE 8787
# Живо ли приложение: /api/auth отвечает и без пароля
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/auth' % os.environ.get('PORT', '8787'), timeout=4)" || exit 1
# Реальные данные Polymarket: интерфейс + сбор данных + агент (ставки виртуальные до приёмки).
# init-db обновляет схему базы миграциями (старая база помечается как стартовая версия).
# Порт — из PORT (его задаёт Railway), иначе 8787.
CMD ["sh", "-c", "whalefollow init-db && exec whalefollow run --host 0.0.0.0 --port ${PORT:-8787}"]
