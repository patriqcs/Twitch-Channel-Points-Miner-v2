# For local multi-process dev via `honcho start` (the Docker image runs uvicorn
# directly as PID 1 instead). The miner processes are spawned by the backend's
# MinerManager, so only the web process is declared here.
web: uvicorn backend.main:app --host 0.0.0.0 --port ${WEB_PORT:-8000}
