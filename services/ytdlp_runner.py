"""yt-dlp CLI adapter that never writes the operator's read-only cookie jar."""

import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar


def main() -> None:
    # yt-dlp otherwise saves its cookie jar at exit, including with --skip-download.
    # The mounted file is the immutable input; refreshed cookies live only in RAM.
    YoutubeDLCookieJar.save = lambda self, *args, **kwargs: None
    yt_dlp.main()


if __name__ == "__main__":
    main()
