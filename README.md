# Any Video Downloader Bot

A private Telegram bot that inventories source formats, lets an authorized user choose an exact stream, downloads that identity, optionally combines a companion audio stream with FFmpeg stream copy, and sends the result to Telegram.

## Phase status

- Phase 1 foundation: implemented, including allowlist administration, controlled outbound networking, persistent exact jobs, adaptive resource admission, restart recovery, durable cancellation and lossless stream-copy merging.
- Phase 2 experience: implemented, including flexible favorite rules, preferred/all-format views, settings, source audio/assets, bounded collections/batches, exact Telegram `file_id` reuse, active-job coalescing and lightweight admin panels. Local tests do not certify every third-party site; Phase 3 review/hardening remains.
- Phase 3: planned. Broader operations, observability, and deployment refinements are not implemented.

## Run

1. Copy `.env.example` to `.env` and set `BOT_TOKEN` and at least one `ADMIN_USER_IDS` value.
2. Install `requirements.txt` (or `requirements-dev.txt` for validation).
3. Ensure `ffmpeg`, `ffprobe`, `yt-dlp`, and Deno are on `PATH`.
4. Run `python -m bot.main`.

Docker: `docker compose up --build`. The base Compose file applies no CPU/RAM locks. To impose administrator-selected limits, combine it with `docker-compose.resources.example.yml`.

See [configuration](docs/configuration.md), [deployment](docs/deployment.md), and [security](docs/security.md).
