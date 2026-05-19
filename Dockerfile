# ---- Base Python image ----
FROM python:3.11-slim

WORKDIR /app

# ---- Install system deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Copy project ----
COPY pyproject.toml ./
COPY app/ ./app/
COPY scripts/ ./scripts/

# ---- Install with uv ----
RUN pip install uv -q && \
    uv pip install --system -e .

# ---- Create dirs ----
RUN mkdir -p session

# ---- Expose dashboard ----
EXPOSE 8080

# ---- Default command: dashboard ----
CMD ["uvicorn", "app.dashboard.server:app", "--host", "0.0.0.0", "--port", "8080"]
