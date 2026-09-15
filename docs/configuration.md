# Configuration

Required values are `BOT_TOKEN` and one or more `ADMIN_USER_IDS`. Configured admins are bootstrapped as allowed owners at startup.

`RESOURCE_MODE` is `auto-shared`, `auto-dedicated`, or `manual`. The bootstrap stage targets are two extractions, two downloads, one lossless merge and one upload. They are maximum targets, not promised simultaneous work: Auto modes may reduce effective capacity under live pressure. Disk protection combines `DISK_SAFETY_HEADROOM_FRACTION`, optional `DISK_SAFETY_HEADROOM_GB`, current free space, live job growth, and transactional reservations. `MAX_JOB_SIZE_BYTES=0` disables an explicit per-job cap.

Environment variables provide bootstrap defaults for safe operational settings. Administrator changes made in Telegram are validated by `RuntimeSettingsService`, stored in SQLite, applied live, and remain authoritative across bot/container/VM restarts until Reset to Default removes the override. This applies to the batch limit, stage concurrency targets, queue limits, bandwidth ceiling and resource mode. Secrets and deployment paths—including `BOT_TOKEN`, database/storage paths, network endpoints and cookie/profile paths—remain environment/deployment-only and are never editable through Telegram.

`MEDIA_SESSION_TTL` controls only interactive menus. Existing jobs retain execution snapshots independently. `USE_LOCAL_API=false` is the safe default and uses Telegram's cloud Bot API with the project's 50 MB upload ceiling. `USE_LOCAL_API=true` makes aiogram use `LOCAL_API_BASE_URL` with local-server semantics and `LOCAL_API_MAX_FILE_SIZE_MB` (default 2000). That configured larger ceiling remains inactive until Telegram `getMe` succeeds through the Local transport. When the bot runs in Docker, `localhost` is the bot container itself; use a shared-network service hostname or an explicit host-gateway address for an independently running Local Bot API server.

`MAX_COLLECTION_ITEMS` (default 50) bounds playlist/carousel metadata retained per extraction. `MAX_BATCH_URLS` (default 4, runtime range 1–10) bounds deduplicated URLs accepted from one Telegram message. Multi-link messages create one persisted batch session and one status card; children are analyzed independently, so one failed link does not destroy the rest.

`SCHEDULER_MAX_WORKERS=0` means automatic adaptive task capacity, derived from the resource governor's extraction/download budgets, active stages and host pressure. A positive value is an administrator-selected hard ceiling, not a promised worker count. Falling capacity prevents new admissions; it does not kill healthy active work. Auto Shared reserves a smaller share of host capacity for this bot so other services can coexist. Auto Dedicated is more aggressive; Manual uses configured stage limits and bypasses automatic CPU/memory pressure admission checks. Disk protection remains enabled in all modes.

Accepted analysis waits asynchronously for extraction capacity and resumes automatically; temporary CPU/RAM/slot pressure does not ask the user to resend. Persisted download jobs denied a stage lease enter `WAITING_RESOURCES` and are fairly reclaimed by the scheduler using `SCHEDULER_RETRY_SECONDS`. Genuine admission failures remain an administrative pause, a per-user/global queue limit, or critical protected disk headroom.

`MAX_QUEUED_JOBS_PER_USER` (default 6) and `MAX_TOTAL_QUEUED_JOBS` (default 50) count all active jobs, including queued, claimed, waiting and running jobs. Six permits the normal two-active plus four-waiting workflow while stage concurrency remains independently governed. Admission is checked atomically with insertion in SQLite. Administrators do not bypass either safeguard.

Delivery policy is centralized in `TelegramService`: Standard transport uses its configured code policy of 50 MiB; Local transport uses `LOCAL_API_MAX_FILE_SIZE_MB` (default 2000 MiB). Preflight sums the selected video and companion audio sizes, not twice that sum: retained inputs plus merged output are a separate temporary disk requirement. Exact and available approximate metadata sizes inform preflight; missing sizes may proceed unless the known portion alone exceeds the limit. Muxing overhead and inaccurate metadata can change the result, so the actual output file is checked again before upload. Rejection never triggers compression, transcoding or format substitution.

`BANDWIDTH_CEILING` is a per-active-download-process rate limit (bytes/second), not an aggregate bot-wide host shaper. `MAX_RETRIES` controls downloader retries. Unimplemented extraction/DNS cache TTL knobs are intentionally absent.

Health uses `DATA_DIR/health.json` and `DATABASE_PATH`, resolved from the same settings/environment as the application. The JSON heartbeat records timestamp, database status, and scheduler, controlled-proxy, and maintenance liveness; it also records whether maintenance last succeeded within the configured interval plus 120 seconds. The local healthcheck requires a heartbeat no older than 90 seconds, all of those signals to be healthy, and a readable migrated database. No Telegram request is made. Configure `DATA_DIR`, `JOBS_DIR`, `DATABASE_PATH` and `LOG_DIR` explicitly when changing deployment paths; they are independent settings.

Extraction uses `YTDLP_TIMEOUT` as the per-process attempt ceiling, `YTDLP_SOCKET_TIMEOUT` for individual network operations, and `YTDLP_EXTRACTOR_RETRIES` for conservative extractor retries. `YTDLP_IMPERSONATION_FALLBACK` permits one eligible retry using `YTDLP_IMPERSONATE_TARGET`; this is a transport fallback and never changes selected-format identity. `YTDLP_COOKIES_FILE` is optional and must name a mounted regular Netscape cookie file. Empty keeps public `--no-cookies` behavior; a configured missing file fails safely.

When `YTDLP_COOKIES_FILE` is configured, `YTDLP_COOKIE_DOMAINS` must specify its source and cookie scope. For the common YouTube hosts use `youtube.com,youtu.be`. Do not add unrelated Google domains. If the jar genuinely contains cookies for additional domains, use an explicit named profile with `cookie_domains`. `ADMIN_USER_IDS` and `YTDLP_COOKIE_DOMAINS` accept empty values where allowed, one value, comma-separated values, or JSON arrays; surrounding whitespace is trimmed, and domains are lowercased, stripped of a trailing dot, validated, and deduplicated.

`YTDLP_PROFILES_FILE` may point to a read-only JSON file containing named operator profiles:

```json
[
  {
    "name": "youtube-owner",
    "source_domains": [
      "youtube.com",
      "youtu.be"
    ],
    "cookie_domains": [
      "youtube.com"
    ],
    "path": "/run/secrets/youtube-cookies.txt",
    "enabled": true
  }
]
```

`source_domains` controls which media source URLs may select the profile. `cookie_domains` controls which domains may exist inside its Netscape cookie jar. If `cookie_domains` is omitted, it defaults conservatively to `source_domains`. The legacy `domains` property remains supported and maps to both scopes. Cookie contents must never be placed in this JSON. Profile JSON and cookie jars remain operator-managed, read-only runtime mounts.

`MAX_COLLECTION_DEPTH` (default 3) limits nested collection traversal. The maintenance service runs once during startup and then every `MAINTENANCE_INTERVAL_SECONDS` (default 900). Each bounded pass removes expired UI drafts after `UI_DRAFT_TTL_HOURS` (24), cache rows after `CACHE_RETENTION_DAYS` (90), terminal job history after `JOB_HISTORY_DAYS` (30), stale reservations/subscribers, expired media sessions, and up to 100 safe orphan job directories older than `ORPHAN_GRACE_HOURS` (24). `BACKUP_RETENTION_COUNT` (5) controls rotation of private, manually requested SQLite online backups.
