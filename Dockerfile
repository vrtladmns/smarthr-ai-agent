FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-recruiter.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements-recruiter.txt

COPY --chown=appuser:appuser config.py llm_factory.py recruiter_agent.py ./
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser deploy ./deploy

EXPOSE 8081

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8081/health || exit 1

CMD ["python", "recruiter_agent.py", "--serve-graph-webhook"]
