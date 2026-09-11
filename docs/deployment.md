# Deployment

The base Docker deployment deliberately has no CPU or RAM limits; automatic shared mode derives capacity from the effective cgroup/host resources and preserves headroom. Administrators can apply the optional resource override:

```sh
docker compose -f docker-compose.yml -f docker-compose.resources.example.yml up --build
```

The image uses the official Python 3.11 slim image on Debian Trixie and its distro-provided FFmpeg/FFprobe packages, plus Deno 2.9.5, pinned yt-dlp, EJS and curl-cffi support. Image construction requires a curl-cffi binary wheel and runs bounded FFmpeg/FFprobe startup checks. It runs as non-root UID/GID 1000 and drops Linux capabilities. Docker-managed `bot_data` and `bot_logs` named volumes persist SQLite, jobs, and logs without creating root-owned bind directories on first startup.

## Authorized yt-dlp session

Public downloads work without cookies wherever the source permits them. Some sites challenge cloud/server IPs and require a legitimate signed-in session. An operator may export a Netscape-format cookie file for yt-dlp and mount it read-only; the bot never accepts cookie uploads through Telegram and never stores cookie contents in SQLite.

Set `YTDLP_COOKIES_HOST_PATH` to the host file, then start with the optional override:

```sh
docker compose -f docker-compose.yml -f docker-compose.cookies.example.yml up --build
```

The override maps the file to `/run/secrets/yt-dlp-cookies.txt` and sets `YTDLP_COOKIES_FILE` inside the container. Keep the host file outside the repository with restrictive permissions. Never commit it. Using an account/session carries site-specific policy and security risk; authenticated access does not enable DRM, paywall, private-content, or access-control bypass.

The health check requires a recent application heartbeat and a readable SQLite database; it does not report healthy before async initialization succeeds.
To use host-visible storage, create writable directories before starting the service and override the mounts deliberately, for example `./data:/app/data` and `./logs:/app/logs`. On Linux, assign both directories to UID/GID 1000 (or grant an equivalent narrowly scoped permission). Do not switch the container to root to work around ownership.
