# ================================
# File: Dockerfile
# ================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 libdbus-1-3 libbluetooth3 \
      dbus bluez bluez-tools \
      build-essential python3-dev libevdev-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY pihub ./pihub
CMD ["python", "-m", "pihub.app"]
