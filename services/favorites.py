"""Flexible, deterministic favorite-format matching without inventory mutation."""

from dataclasses import dataclass
from itertools import product

from core.models import FavoriteFormatRule, MatchingStrategy, MediaFormat

COMMON_HEIGHTS = {2160, 1440, 1080, 720, 480, 360, 240}
COMMON_FPS = {24, 25, 30, 50, 60}
COMMON_CONTAINERS = {"mp4", "webm", "mkv"}


@dataclass(frozen=True)
class FavoriteMatchResult:
    formats: list[MediaFormat]
    matched_rule_ids: tuple[str, ...]
    total_combinations: int
    unavailable_combinations: int


class FavoriteMatcher:
    """Match rules against genuine formats; never changes or fabricates formats."""

    @classmethod
    def match(
        cls,
        formats: list[MediaFormat],
        rules: list[FavoriteFormatRule],
        strategy: MatchingStrategy = MatchingStrategy.BEST_QUALITY,
    ) -> FavoriteMatchResult:
        videos = [fmt for fmt in formats if fmt.is_video]
        enabled = sorted((rule for rule in rules if rule.enabled), key=lambda r: (r.priority, r.rule_id))
        selected: list[MediaFormat] = []
        matched_ids: list[str] = []
        unavailable = 0
        total_combinations = 0
        for rule in enabled:
            rule_matched = False
            combinations = product(
                rule.codecs, rule.resolutions, rule.fps_values, rule.containers
            )
            for codec, resolution, fps, container in combinations:
                total_combinations += 1
                combination_rule = rule.model_copy(update={
                    "codecs": [codec], "resolutions": [resolution],
                    "fps_values": [fps], "containers": [container],
                })
                candidates = [fmt for fmt in videos if cls.matches_rule(fmt, combination_rule)]
                if not candidates:
                    unavailable += 1
                    continue
                rule_matched = True
                fmt = sorted(candidates, key=lambda item: cls._rank(item, strategy))[0]
                if all(
                    existing.internal_key != fmt.internal_key for existing in selected
                ):
                    selected.append(fmt)
            if rule_matched:
                matched_ids.append(rule.rule_id)
        return FavoriteMatchResult(
            selected, tuple(matched_ids), total_combinations, unavailable
        )

    @classmethod
    def matches_rule(cls, fmt: MediaFormat, rule: FavoriteFormatRule) -> bool:
        return (
            cls._codec(fmt, rule.codecs)
            and cls._resolution(fmt, rule.resolutions)
            and cls._fps(fmt, rule.fps_values)
            and cls._container(fmt, rule.containers)
        )

    @staticmethod
    def _any(values: list[str]) -> bool:
        return "any" in values

    @classmethod
    def _codec(cls, fmt: MediaFormat, values: list[str]) -> bool:
        if cls._any(values):
            return True
        normalized = fmt.vcodec_normalized.value.lower()
        raw = (fmt.vcodec_raw or "").lower()
        aliases = {
            "h265": ("h.265", "hevc", "h265", "hev1", "hvc1"),
            "h264": ("h.264", "avc", "h264", "avc1"),
            "vp9": ("vp9", "vp09"),
            "av1": ("av1", "av01"),
        }
        for value in values:
            if value == "other":
                if not any(token in normalized or token in raw for group in aliases.values() for token in group):
                    return True
            elif any(token in normalized or token in raw for token in aliases.get(value, (value,))):
                return True
        return False

    @classmethod
    def _resolution(cls, fmt: MediaFormat, values: list[str]) -> bool:
        if cls._any(values):
            return True
        height = fmt.height
        for value in values:
            digits = "".join(ch for ch in value if ch.isdigit())
            if value == "other" and (height is None or height not in COMMON_HEIGHTS):
                return True
            if digits and height == int(digits):
                return True
        return False

    @classmethod
    def _fps(cls, fmt: MediaFormat, values: list[str]) -> bool:
        if cls._any(values) or "best" in values:
            return True
        fps = round(fmt.fps) if fmt.fps is not None else None
        for value in values:
            digits = "".join(ch for ch in value if ch.isdigit())
            if value == "other" and (fps is None or fps not in COMMON_FPS):
                return True
            if digits and fps == int(digits):
                return True
        return False

    @classmethod
    def _container(cls, fmt: MediaFormat, values: list[str]) -> bool:
        if cls._any(values):
            return True
        ext = (fmt.ext or "").lower()
        for value in values:
            if value == "other" and ext not in COMMON_CONTAINERS:
                return True
            if value == "mkv-compatible" and ext == "mkv":
                return True
            if value == ext:
                return True
        return False

    @staticmethod
    def _rank(fmt: MediaFormat, strategy: MatchingStrategy) -> tuple:
        quality = (fmt.height or 0, fmt.fps or 0, fmt.tbr or fmt.vbr or 0)
        stable = (fmt.format_id, fmt.internal_key)
        if strategy == MatchingStrategy.SMALLEST_FILE:
            size = fmt.effective_size if fmt.effective_size is not None else 2**63
            return (size, -quality[0], -quality[1], stable)
        if strategy == MatchingStrategy.PREFER_READY:
            ready = fmt.is_muxed or not fmt.requires_separate_audio
            return (not ready, -quality[0], -quality[1], -(quality[2]), stable)
        return (-quality[0], -quality[1], -(quality[2]), not fmt.is_muxed, stable)
