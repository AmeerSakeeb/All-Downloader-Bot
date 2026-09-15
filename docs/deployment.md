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

The override maps the file to `/run/secrets/yt-dlp-cookies.txt`, sets `YTDLP_COOKIES_FILE`, and scopes the single profile to `youtube.com,youtu.be`. Keep the host file outside the repository with restrictive permissions. Never commit it. Do not add unrelated Google domains automatically. If the jar genuinely contains additional domains, configure an explicit named profile with separate `source_domains` and `cookie_domains` as documented in [configuration](configuration.md). Profile JSON must contain only metadata and fake/runtime paths, never cookie contents; mount both configuration and cookie files read-only. Using an account/session carries site-specific policy and security risk; authenticated access does not enable DRM, paywall, private-content, or access-control bypass.

The admin panel can pause/resume new admissions, inspect users/resources/diagnostics and the active queue, cancel a shared job after confirmation, refresh or enable/disable authorized profiles, run retention/orphan maintenance, and request a private database backup. Backup uses SQLite's online backup API rather than copying the live database/WAL, writes mode-restricted files under `DATA_DIR/backups`, protects disk headroom, and rotates to `BACKUP_RETENTION_COUNT`. Database backups are never committed, published, or automatically sent through Telegram.

The health check requires a heartbeat no older than 90 seconds, a readable migrated SQLite database, and healthy database, scheduler, controlled-proxy, and maintenance signals. It does not contact Telegram or report healthy before async initialization succeeds.

## Existing Telegram Local Bot API server

The base deployment keeps `USE_LOCAL_API=false`. It does not bundle or build Telegram Local Bot API. Docker container loopback is isolated, so `http://localhost:8081` works only when the bot itself is running directly on the same host—not when Local Bot API is a separate container.

For an already-running Local Bot API container, attach both containers to the same user-defined Docker network, ensure that server is reachable there as `telegram-local-api:8081` (or set another explicit service hostname in `TELEGRAM_LOCAL_API_BASE_URL`), set `TELEGRAM_LOCAL_API_NETWORK` to that existing network's name, and start the bot with:

```sh
docker compose -f docker-compose.yml -f docker-compose.local-api.example.yml up --build
```

The override only joins the supplied external network and configures this bot; it does not create, start, configure, or manage the Local Bot API service. As an alternative, publish the Local Bot API port on the Docker host, add an explicit `host.docker.internal:host-gateway` mapping on Linux, and set `LOCAL_API_BASE_URL=http://host.docker.internal:8081` in a private override.

Before moving a bot from Telegram's cloud Bot API to a Local Bot API server, the operator must explicitly invoke the Bot API `logOut` method against the cloud endpoint, then direct the bot to the local endpoint. Do this once as a deliberate migration step using a secret-safe client; the downloader never performs `logOut` automatically at startup. The Standard Bot API path retains the 50 MB bot upload ceiling. Local Bot API permits uploads up to 2000 MB, bounded by `LOCAL_API_MAX_FILE_SIZE_MB`, only after the bot successfully calls Telegram `getMe` through the configured Local transport. A generic HTTP response, including 404, never activates large-file delivery. Actual-file runtime preflight remains enforced in both modes.

To use host-visible storage, create writable directories before starting the service and override the mounts deliberately, for example `./data:/app/data` and `./logs:/app/logs`. On Linux, assign both directories to UID/GID 1000 (or grant an equivalent narrowly scoped permission). Do not switch the container to root to work around ownership.
