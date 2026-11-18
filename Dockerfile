FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WF_DB_URL=sqlite:////data/whalefollow.db
WORKDIR /app
COPY pyproject.toml README.md ./
COPY whalefollow ./whalefollow
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"
VOLUME ["/data"]
EXPOSE 8787
# Реальные данные Polymarket: интерфейс + сбор данных + агент (ставки виртуальные до приёмки)
CMD ["sh", "-c", "whalefollow init-db && whalefollow run --host 0.0.0.0"]
