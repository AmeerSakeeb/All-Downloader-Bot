# Troubleshooting

- “Stale menu”: the configured interactive TTL elapsed; send the URL again. Existing queued jobs are unaffected.
- “Waiting for resources”: memory, CPU/load, stage concurrency, or protected disk headroom is constrained. The scheduler retries automatically.
- “Cannot combine losslessly”: none of the safe MP4/WebM/MKV stream-copy attempts passed FFmpeg and post-merge codec verification. Transcoding is intentionally not a fallback.
- YouTube format loss: confirm Deno 2.3+ and `yt-dlp-ejs` are installed. Live site changes remain an upstream limitation.
- Container unhealthy: inspect `data/health.json`, database permissions, and scheduler logs.
- Local Bot API connection refused from Docker: do not use `localhost` for a separate container. Join the configured external shared network and use its service hostname, or publish the host port and configure an explicit host-gateway mapping.
- Local Bot API rejects a newly migrated bot: explicitly call Telegram's cloud Bot API `logOut` method once before redirecting the bot to the local server. This is an operator migration action and is never automated at startup.
- Delivery-size rejection: Standard Telegram Bot API is limited to 50 MB by this bot's transport policy; Local Bot API supports up to the configured 2000 MB ceiling. The bot checks the actual completed file and never transcodes it to fit.

Validation commands: `python -m compileall -q .`, `pytest -q`, `ruff check .`, `mypy ...`, `docker compose config`, and `docker build .`.
