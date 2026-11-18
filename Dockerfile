FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WF_DB_URL=sqlite:////data/whalefollow.db WF_LOG_FORMAT=json
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY whalefollow ./whalefollow
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"
VOLUME ["/data"]
EXPOSE 8787
# Живо ли приложение: /api/auth отвечает и без пароля
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/auth', timeout=4)" || exit 1
# Реальные данные Polymarket: интерфейс + сбор данных + агент (ставки виртуальные до приёмки).
# init-db обновляет схему базы миграциями (старая база помечается как стартовая версия).
CMD ["sh", "-c", "whalefollow init-db && whalefollow run --host 0.0.0.0"]
