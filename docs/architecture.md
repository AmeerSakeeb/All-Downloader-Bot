# Architecture

Aiogram routers handle commands, URLs, and callback actions. Access middleware is installed on both message and callback observers. Extractors produce complete `MediaSession` inventories. SQLite stores sessions and transactionally snapshots a selected video and optional companion audio into `DownloadJob`.

`JobScheduler` is a persistent, wakeable loop. It uses `ResourceGovernor` leases around extraction/download/merge/upload stages. `ProcessSupervisor` owns subprocess handles and groups for yt-dlp, FFprobe, and FFmpeg. `FileManager` confines each job to a validated directory. `TelegramService` delivers media and persists the returned Telegram `file_id`.

Interrupted active jobs are requeued at startup. Terminal jobs release reservations and job-local files.

Phase 2 keeps all extracted formats in the session and layers `FavoriteMatcher` presentation over them. SQLite v4 stores flexible ordered rules, per-user delivery/detail settings, collection/filter drafts, exact Telegram-file cache entries, coalesced-job subscribers, and persisted global resource mode. `selection.py` is the single admission path for delivery preflight, cache reuse, exact active-job coalescing, queue limits, and job creation.

Collections and multi-URL batches store only opaque item IDs in callback data. An item reuses embedded genuine formats or is extracted on explicit selection. Auxiliary source assets use controlled streaming; video/audio jobs retain exact fingerprints and use the same scheduler/no-transcoding path.
