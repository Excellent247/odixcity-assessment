# ── Stage 1: builder ──────────────────────────────────────────────────────────
# Full image only needed here to compile C extensions (uvloop, httptools).
# Nothing from this stage leaks into the final image.
FROM python:3.11 AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
# Slim base — no compiler, no build tools, ~700 MB smaller than python:3.11.
FROM python:3.11-slim AS runtime

# Copy only the installed packages from the builder stage
COPY --from=builder /install /usr/local

WORKDIR /app

# Create a non-root user and group, then own the workdir
RUN addgroup --system appgroup && \
    adduser  --system --ingroup appgroup --no-create-home appuser

COPY main.py .

# Drop to non-root before the process starts
USER appuser

EXPOSE 8000

# Fix 4: 4 workers instead of 1 — one per CPU core, each with its own event
# loop, MongoDB pool, Redis pool, and write-buffer thread.
# uvloop (C event loop) + httptools (C HTTP parser) reduce per-request overhead.
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--log-level", "warning", \
     "--timeout-keep-alive", "2"]