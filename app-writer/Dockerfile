# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11 AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

COPY --from=builder /install /usr/local

WORKDIR /app

RUN addgroup --system appgroup && \
    adduser  --system --ingroup appgroup --no-create-home appuser

COPY app.py .

USER appuser

CMD ["python", "app.py"]