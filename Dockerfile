FROM python:3.11-slim

# System deps for eccodes (HRRR GRIB2 decoding) and timezone data
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes0 \
    libeccodes-tools \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data dir for SQLite + HRRR cache
RUN mkdir -p /data /data/hrrr_cache
ENV POLYWX_DATA_DIR=/data \
    HERBIE_SAVE_DIR=/data/hrrr_cache

CMD ["python", "-m", "src.main"]
