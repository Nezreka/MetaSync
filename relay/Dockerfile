FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8791

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --uid 10001 --no-create-home relay

# Fix the copied modes with a plain chmod, as root. Build-context
# files can arrive with restrictive modes (660 from the build
# machine's checkout) and COPY preserves them, locking the relay
# user out of its own code (first fly deploy, oct 28 2026:
# PermissionError on __init__.py). The COPY --chmod flag did not
# take effect on fly's remote builder (same crash), so the mode
# fix is a real chmod run step. The code stays root-owned; the
# relay user only reads it. The last line proves it at build time:
# the build fails unless the relay user can really read the
# package (a bare import is not enough — an unreadable directory
# still imports as an empty namespace package, so assert __file__).
COPY metasync_relay/ ./metasync_relay/
RUN chmod -R a+rX /app/metasync_relay
RUN cd /app && su relay -s /bin/sh -c "python3 -c 'import metasync_relay; assert metasync_relay.__file__, \"unreadable\"'"

USER relay

EXPOSE 8791

# The relay has no HTTP surface (pure WebSocket by design), so the
# health probe is a bare TCP connect: the listener accepting is the
# health signal. This is a container-local probe only — Fly runs
# NO service checks for this app, deliberately (see fly.toml:
# an HTTP check against a WebSocket-only listener could never
# pass); on Fly, liveness is the restart policy plus nodes
# reconnecting. This covers plain `docker run` deployments.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import socket; s = socket.create_connection(('127.0.0.1', 8791), timeout=4); s.close()"

CMD ["python", "-m", "metasync_relay"]
