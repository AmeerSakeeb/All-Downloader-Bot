# Deployment

The base Docker deployment deliberately has no CPU or RAM limits; automatic shared mode derives capacity from the effective cgroup/host resources and preserves headroom. Administrators can apply the optional resource override:

```sh
docker compose -f docker-compose.yml -f docker-compose.resources.example.yml up --build
```

The image includes FFmpeg, FFprobe, Deno 2.9.5, current yt-dlp, and EJS support. It runs as non-root UID/GID 1000 and drops Linux capabilities. Docker-managed `bot_data` and `bot_logs` named volumes persist SQLite, jobs, and logs without creating root-owned bind directories on first startup.

The health check requires a recent application heartbeat and a readable SQLite database; it does not report healthy before async initialization succeeds.
To use host-visible storage, create writable directories before starting the service and override the mounts deliberately, for example `./data:/app/data` and `./logs:/app/logs`. On Linux, assign both directories to UID/GID 1000 (or grant an equivalent narrowly scoped permission). Do not switch the container to root to work around ownership.
