FROM python:3.11-slim AS builder
WORKDIR /app

# Install dependencies first (cached layer)
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

COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=builder /app .

# Create data directories
RUN mkdir -p data/synthetic data/models

EXPOSE 8000
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
