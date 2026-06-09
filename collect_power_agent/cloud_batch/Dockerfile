# cloud_batch/Dockerfile
# Build context: project root  (docker build -f cloud_batch/Dockerfile .)
FROM python:3.11-slim

WORKDIR /workspace

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
# 1. Main project deps (needed by app/*.py subprocesses)
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 2. cloud_batch service deps
COPY cloud_batch/requirements.txt ./cloud_batch_requirements.txt
RUN pip install --no-cache-dir -r cloud_batch_requirements.txt

# Copy source
COPY . .

ENV PYTHONPATH=/workspace
ENV PORT=8080

# Use gunicorn with 1 worker so background threads share state
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", \
     "--timeout", "0", "cloud_batch.entrypoint:app"]
