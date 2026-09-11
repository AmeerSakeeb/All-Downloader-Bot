# Security

All protected Telegram routes fail closed and consult the SQLite allowlist before extraction. Dynamic Telegram text uses HTML escaping. Logs remove URL query strings/fragments and avoid raw signed media URLs.

HTTP/HTTPS traffic from yt-dlp is routed through a loopback-only validating proxy. Every connection, redirect, and reconnect is resolved before contact; all resolved addresses must be globally routable. The proxy pins the socket to a validated address, reducing DNS-rebinding/TOCTOU exposure. Loopback, private, link-local, multicast, reserved, unspecified, shared, and metadata destinations are blocked for IPv4 and IPv6.

FFmpeg/FFprobe accept local `Path` inputs only. FFmpeg maps one video and one audio stream and exposes no encoder/argument surface. Job directories reject traversal and symlink escape. The bot does not support cookies, authenticated/private media, DRM bypass, or arbitrary FFmpeg options.
