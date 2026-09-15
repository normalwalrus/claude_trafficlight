# Build context is the repo root (see docker-compose.yml) so the image can
# carry the hook payload it installs onto the host at startup.
FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/app.py server/bootstrap.py server/entrypoint.sh ./
COPY server/static ./static

# Installed into the host's ~/.claude by bootstrap.py, and used in-container to
# read transcripts when the host has no Python.
COPY hooks ./hookpayload
COPY scripts/install_hooks.py ./install_hooks.py

RUN chmod +x /app/entrypoint.sh

EXPOSE 8787

CMD ["/app/entrypoint.sh"]
