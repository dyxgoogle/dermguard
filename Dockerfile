FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Create non-root runtime user for Cloud Run security best practices
RUN groupadd --gid 10001 dermguard && \
    useradd --uid 10001 --gid dermguard --create-home --shell /sbin/nologin dermguard

COPY pyproject.toml README.md LICENSE ./
COPY dermguard ./dermguard
COPY specs ./specs

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[gcp]" && \
    python -m dermguard.data.seed_ingredients && \
    mkdir -p /app/data && \
    chown -R dermguard:dermguard /app

USER dermguard

EXPOSE 8080

CMD ["sh", "-c", "uvicorn dermguard.server:fastapi_app --host 0.0.0.0 --port ${PORT:-8080}"]
