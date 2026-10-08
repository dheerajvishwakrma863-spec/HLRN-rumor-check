# syntax=docker/dockerfile:1
# HLRN container image (Streamlit dashboard; the same image also runs the bot webhook).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1 \
    HLRN_DB_PATH=/app/data/hlrn.db

WORKDIR /app

# Install dependencies first so this layer is cached until requirements.txt changes.
# (opencv-python-headless ships manylinux wheels, so no apt packages are needed.)
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user; /app/data is the persistent volume (SQLite lives here).
RUN useradd --create-home --uid 10001 hlrn \
    && mkdir -p /app/data \
    && chown -R hlrn:hlrn /app
USER hlrn
VOLUME ["/app/data"]

EXPOSE 8501 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=3).status==200 else 1)"

# Seed demo rumours (idempotent), then start the dashboard.
CMD ["sh", "-c", "python seed_data.py --no-verify && streamlit run app.py --server.port=8501 --server.address=0.0.0.0"]
