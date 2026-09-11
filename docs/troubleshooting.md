# Troubleshooting

- “Stale menu”: the configured interactive TTL elapsed; send the URL again. Existing queued jobs are unaffected.
- “Waiting for resources”: memory, CPU/load, stage concurrency, or protected disk headroom is constrained. The scheduler retries automatically.
- “Cannot combine losslessly”: none of the safe MP4/WebM/MKV stream-copy attempts passed FFmpeg and post-merge codec verification. Transcoding is intentionally not a fallback.
- YouTube format loss: confirm Deno 2.3+ and `yt-dlp-ejs` are installed. Live site changes remain an upstream limitation.
- Container unhealthy: inspect `data/health.json`, database permissions, and scheduler logs.

Validation commands: `python -m compileall -q .`, `pytest -q`, `ruff check .`, `mypy ...`, `docker compose config`, and `docker build .`.
