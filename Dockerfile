# Multi-stage: the build stage carries compilers and headers, the runtime stage
# does not. Keeping build tooling out of the shipped image cuts both the image
# size and the attack surface.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Run as an unprivileged user. If the process is ever compromised, it should not
# also be root inside the container.
RUN useradd --create-home --uid 10001 marketpulse
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY sql ./sql
COPY scripts ./scripts
COPY evals ./evals
COPY bench ./bench
COPY pyproject.toml README.md ./

USER marketpulse
EXPOSE 8000

# Liveness only -- a readiness probe here would restart the container during a
# transient Postgres blip, which is exactly the wrong response.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
