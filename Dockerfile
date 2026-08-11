# Spot trading framework — production-oriented container
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPER_TRADING=true

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md requirements.txt ./
COPY app ./app
COPY docs ./docs

RUN pip install --no-cache-dir -e .

RUN mkdir -p data reports logs

# Default: paper backtest (never live by default)
CMD ["python", "-m", "app.cli", "backtest"]
