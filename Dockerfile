FROM python:3.14-slim

# Headless matplotlib — it's in your requirements, and this avoids a
# GUI-backend crash if anything imports it inside the container.
ENV MPLBACKEND=Agg
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first so this layer caches between builds
COPY python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code and the data it reads, preserving the python/ + data/ layout
COPY python/ ./python/
COPY data/ ./data/

# Run from python/ so "../data", "templates" and "static" resolve
# exactly as they do when you run uvicorn locally
WORKDIR /app/python

# SQLite lives here and MUST be a mounted volume. Anything written inside the
# image is destroyed on the next `docker pull` of a rebuilt tag, which would
# silently wipe every snapshot. Note this is /app/state and NOT /app/data —
# mounting over /app/data would shadow the CSVs copied in above.
ENV FPL_DB_PATH=/app/state/fpl_companion.db
VOLUME ["/app/state"]

EXPOSE 8000

# Single worker on purpose: your app holds rated data in an in-memory
# `state` dict that /api/mode and /api/refresh mutate. Multiple workers
# would each keep their own copy and drift out of sync.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]