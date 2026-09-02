FROM python:3.11-slim AS builder
WORKDIR /app

# Install dependencies into a virtual environment (cached layer, not reinstalled in prod stage)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Run tests — fail the image build if any test fails
RUN python -m pytest tests/ -x -q --no-header --tb=short 2>&1 || \
    (echo "Tests failed — image build aborted." && exit 1)

# ── Production image ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS production
WORKDIR /app

# Copy the virtual environment from builder (no re-install needed)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source from builder
COPY --from=builder /app .

# Create data directories
RUN mkdir -p data/synthetic data/models db

EXPOSE 8000
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Default audit DB path — overridable via AUDIT_DB_PATH env var
ENV AUDIT_DB_PATH=/app/db/audit.db

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
