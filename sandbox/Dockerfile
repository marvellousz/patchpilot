# Isolated runner for target-repo tests. Runs with --network none, so anything the
# tests need must be installed here at build time.
FROM python:3.12-slim

RUN pip install --no-cache-dir pytest==8.3.3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
