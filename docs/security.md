# Security

All protected Telegram routes fail closed and consult the SQLite allowlist before extraction. Dynamic Telegram text uses HTML escaping. Logs remove URL query strings/fragments and avoid raw signed media URLs.

HTTP/HTTPS traffic from yt-dlp is routed through a loopback-only validating proxy. Every connection, redirect, and reconnect is resolved before contact; all resolved addresses must be globally routable. The proxy pins the socket to a validated address, reducing DNS-rebinding/TOCTOU exposure. Loopback, private, link-local, multicast, reserved, unspecified, shared, and metadata destinations are blocked for IPv4 and IPv6.

FFmpeg/FFprobe accept local `Path` inputs only. FFmpeg maps one video and one audio stream and exposes no encoder/argument surface. Job directories reject traversal and symlink escape. Optional operator-managed cookies may be mounted read-only for a legitimate authorized yt-dlp session; cookie content is never accepted through Telegram, logged, persisted, committed, placed in profile JSON, or copied into the image. DRM, paywall/access-control bypass and arbitrary FFmpeg options remain unsupported.

Single-cookie configuration requires `YTDLP_COOKIE_DOMAINS` to scope both source selection and cookie-jar validation; the YouTube example uses only `youtube.com,youtu.be`. Named profiles separate routing (`source_domains`) from the domains permitted in the Netscape jar (`cookie_domains`). Omitted `cookie_domains` defaults to `source_domains`, while legacy `domains` maps conservatively to both. The Authorized Sessions admin screen shows only profile name, domain scopes, and status; it never exposes host/runtime paths, filenames, or cookie contents.

Maintenance deletes only bounded expired metadata and safe orphan job directories under configured retention rules. Administrator-requested database backups use SQLite online backup into the private runtime backup directory. Backups are mode-restricted, retained according to configuration, and are never committed, published, or automatically sent through Telegram.
