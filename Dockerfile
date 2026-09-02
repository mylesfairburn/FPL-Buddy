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

# Not root.
#
# The app reads data/ and templates/ and writes only to /app/state, so it has
# never needed root - it had it because that is the default and nothing forced
# the question. A container process that is root is root on the host kernel for
# the purposes of a container escape, and this one parses JSON from the public
# internet on every request.
#
# /app/state is chowned because the VOLUME below is what the app writes to, and
# a mount inherits the ownership of the directory beneath it when Docker creates
# it. An EXISTING host directory keeps its own ownership, so a bind mount made
# before this change may need `chown -R 10001:10001` on the host once - see
# deploy/README.md.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/state \
    && chown -R appuser:appuser /app
USER appuser

# Is the process actually able to answer, as opposed to merely running.
#
# The failure this catches is the one the app is built to survive: it serves
# without ratings rather than crashing, so "the container is up" has never
# implied "the site works". /api/ai/status is the endpoint that is exempt from
# the warm-up gate, so this reports healthy as soon as uvicorn is listening
# rather than failing for the twenty seconds the ratings take to build.
#
# start-period covers a slow first boot on the VM; the interval is generous
# because nothing acts on this automatically - it makes `docker ps` tell the
# truth, which is what you want at the point you are already debugging.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen(\
'http://127.0.0.1:8000/api/ai/status', timeout=8)" || exit 1

# Single worker on purpose: your app holds rated data in an in-memory
# `state` dict that /api/mode and /api/refresh mutate. Multiple workers
# would each keep their own copy and drift out of sync.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]