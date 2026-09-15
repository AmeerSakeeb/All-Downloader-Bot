# Any Video Downloader Bot

A private Telegram bot that inventories source formats, lets an authorized user choose an exact stream, downloads that identity, optionally combines a companion audio stream with FFmpeg stream copy, and sends the result to Telegram.

## Phase status

- Phase 1 foundation: implemented, including allowlist administration, controlled outbound networking, persistent exact jobs, adaptive resource admission, restart recovery, durable cancellation and lossless stream-copy merging.
- Phase 2 experience: implemented, including preferred-format rules, preferred/all-format views, settings, source audio/assets, bounded collections/batches, exact Telegram `file_id` reuse, active-job coalescing and the Admin Control Center.
- Phase 3: implemented and undergoing final independent review/private beta validation. The project is not released, and local checks do not certify every third-party site.

## Run

1. Copy `.env.example` to `.env` and set `BOT_TOKEN` and at least one `ADMIN_USER_IDS` value.
2. Install `requirements.txt` (or `requirements-dev.txt` for validation).
3. Ensure `ffmpeg`, `ffprobe`, `yt-dlp`, and Deno are on `PATH`.
4. Run `python -m bot.main`.

Docker: `docker compose up --build`. The base Compose file applies no CPU/RAM locks. To impose administrator-selected limits, combine it with `docker-compose.resources.example.yml`.

### Telegram delivery modes

- **Standard:** run `docker-compose.yml` by itself. The bot uses Telegram's standard cloud Bot API and the standard upload limit.
- **Local large-file mode:** first operate a separate, verified Telegram Local Bot API deployment and shared Docker network. Then combine `docker-compose.yml` with `docker-compose.local-api.example.yml`, set `USE_LOCAL_API=true`, and configure that existing server's network and service URL. The larger configured ceiling becomes active only after the bot successfully calls Telegram `getMe` through that Local API transport.

`docker-compose.local-api.example.yml` does **not** start, configure, or manage a Telegram Local Bot API server. It only connects this bot to an existing external deployment. API credentials and Local API server secrets remain outside this repository.

Public extraction uses no cookies. If a site legitimately requires the operator's authorized session, mount a Netscape-format yt-dlp cookie file read-only with `docker-compose.cookies.example.yml` and set `YTDLP_COOKIES_HOST_PATH` to its host location. Never commit the file or send it through Telegram. Account/session use is subject to each site's rules and risks; DRM and paywall bypass remain unsupported.

See [configuration](docs/configuration.md), [deployment](docs/deployment.md), and [security](docs/security.md).
