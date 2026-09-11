# Format matching

Every extractor format with a real audio or video stream is retained. Codec normalization recognizes H.264/AVC, H.265/HEVC, VP9, AV1, AAC, Opus, MP3, Vorbis, and FLAC while preserving the raw codec string. `mp4v` remains `Other`; it is MPEG-4 Part 2, not H.264.

Video-only selection ranks companion audio by avoiding commentary/descriptive tracks, then English, original/default identity, common codecs, and bitrate. The exact format IDs and material fingerprints are stored. Re-extraction must find the same format IDs and codec/dimension/FPS/container/stream identity; substitution is forbidden.

Phase 2 favorite rules are ordered, per-user, multi-select criteria over codec, arbitrary resolution, arbitrary FPS and container. Any is exclusive with specific values. Matching evaluates exact preferred combinations without removing formats from `MediaSession`; each combination receives one deterministic source-format winner according to Best quality, Smallest file, or Prefer ready/muxed. Advanced filters alter only presentation.

Direct opaque files do not infer codecs from filename extensions. HLS and DASH manifests are delegated to yt-dlp for genuine variant discovery.
