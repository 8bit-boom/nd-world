# Test image: the production nd-world image plus pytest and the current
# source, for running the full suite in the app's native Linux environment
# (no Windows/macOS quirks — os.geteuid, asyncio-proactor noise, or SQLite
# WAL locking on bind mounts). Build from the repo root and run:
#
#   docker build -f docker/test/Dockerfile -t nd-world-test:latest .
#   docker run --rm nd-world-test:latest
#
# Code changes require a rebuild (the COPY layers re-run in seconds); a bind
# mount was tried and does NOT work reliably from a Windows host — SQLite
# fails to open a fresh world.db on the mounted path even though plain file
# writes succeed.
#
# pytest versions mirror requirements-dev.txt (which can't be COPY+install'ed
# directly because its `-r requirements.txt` line resolves relative to itself).
FROM nd-world:latest
RUN pip install --no-cache-dir pytest==8.3.4 pytest-asyncio==0.25.2
COPY app/ /app/app/
COPY static/ /app/static/
COPY tests/ /app/tests/
COPY docs/ /app/docs/
# Deployment files that tests/test_job_shutdown.py reads straight from the
# repo root (graceful-shutdown config guards).
COPY Dockerfile /app/Dockerfile
COPY docker-compose.yml /app/docker-compose.yml
COPY truenas-compose.yml /app/truenas-compose.yml
WORKDIR /app
CMD ["python", "-m", "pytest", "-q"]
