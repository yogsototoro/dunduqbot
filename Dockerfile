FROM python:3.14.7-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md ./
COPY bot/ ./bot/

RUN pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 dunduq
USER dunduq

CMD ["python", "-m", "bot.receiver"]
