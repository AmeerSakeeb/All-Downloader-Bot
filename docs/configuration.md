# Configuration

Required values are `BOT_TOKEN` and one or more `ADMIN_USER_IDS`. Configured admins are bootstrapped as allowed owners at startup.

`RESOURCE_MODE` is `auto-shared`, `auto-dedicated`, or `manual`. Zero stage maxima in automatic modes mean dynamically calculated capacity; positive values are ceilings. Disk protection combines `DISK_SAFETY_HEADROOM_FRACTION`, optional `DISK_SAFETY_HEADROOM_GB`, current free space, live job growth, and transactional reservations. `MAX_JOB_SIZE_BYTES=0` disables an explicit per-job cap.

`MEDIA_SESSION_TTL` controls only interactive menus. Existing jobs retain execution snapshots independently. `USE_LOCAL_API=true` makes aiogram use `LOCAL_API_BASE_URL` with local-server semantics.

`MAX_COLLECTION_ITEMS` (default 50) bounds playlist/carousel metadata retained per extraction. `MAX_BATCH_URLS` (default 10) bounds URLs accepted from one Telegram message. Both limits prevent a user action from launching an unbounded workload; collection entries still require explicit selection.

`SCHEDULER_MAX_WORKERS=0` means automatic adaptive task capacity, derived from the resource governor's extraction/download budgets, active stages and host pressure. A positive value is an administrator-selected hard ceiling, not a promised worker count. Falling capacity prevents new admissions; it does not kill healthy active work. Auto Shared reserves a smaller share of host capacity for this bot so other services can coexist. Auto Dedicated is more aggressive; Manual uses configured stage limits and bypasses automatic CPU/memory pressure admission checks. Disk protection remains enabled in all modes.

Interactive extraction retries resource admission every two seconds for up to approximately 30 seconds, then asks the user to resend if the host remains busy. Persisted jobs waiting for resources are retried by the scheduler using `SCHEDULER_RETRY_SECONDS`.

`MAX_QUEUED_JOBS_PER_USER` (default 3) and `MAX_TOTAL_QUEUED_JOBS` (default 50) count all active jobs, including queued, claimed, waiting and running jobs. Admission is checked atomically with insertion in SQLite. Administrators do not bypass either safeguard.

Delivery policy is centralized in `TelegramService`: Standard transport uses its configured code policy of 50 MiB; Local transport uses `LOCAL_API_MAX_FILE_SIZE_MB` (default 2000 MiB). Preflight sums the selected video and companion audio sizes, not twice that sum: retained inputs plus merged output are a separate temporary disk requirement. Exact and available approximate metadata sizes inform preflight; missing sizes may proceed unless the known portion alone exceeds the limit. Muxing overhead and inaccurate metadata can change the result, so the actual output file is checked again before upload. Rejection never triggers compression, transcoding or format substitution.

`BANDWIDTH_CEILING` remains wired as a per-download subprocess rate limit (bytes/second), not an aggregate host shaper. `MAX_RETRIES` controls downloader retries. Unimplemented extraction/DNS cache TTL knobs are intentionally not exposed in Phase 1.

Health uses `DATA_DIR/health.json` and `DATABASE_PATH`, resolved from the same settings/environment as the application. The JSON heartbeat records timestamp, database probe success and whether the scheduler service task is alive. The local healthcheck requires a heartbeat no older than 90 seconds and a readable migrated database. A dead scheduler or stale heartbeat fails health; no Telegram request is made. Configure `DATA_DIR`, `JOBS_DIR`, `DATABASE_PATH` and `LOG_DIR` explicitly when changing deployment paths; they are independent settings.

Extraction uses `YTDLP_TIMEOUT` as the per-process attempt ceiling, `YTDLP_SOCKET_TIMEOUT` for individual network operations, and `YTDLP_EXTRACTOR_RETRIES` for conservative extractor retries. `YTDLP_IMPERSONATION_FALLBACK` permits one eligible retry using `YTDLP_IMPERSONATE_TARGET`; this is a transport fallback and never changes selected-format identity. `YTDLP_COOKIES_FILE` is optional and must name a mounted regular Netscape cookie file. Empty keeps public `--no-cookies` behavior; a configured missing file fails safely.
