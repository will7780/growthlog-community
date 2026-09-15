"""
OCR quality semantics (R5.3 / R7.3).

Runtime success (non-empty OCR bytes) ≠ searchable quality.
Profiles are inferred from text statistics (no LLM, no filename/TDD special-cases).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

# --- script / profile labels -------------------------------------------------
PROFILE_CJK = "cjk_primary"
PROFILE_LATIN = "latin_primary"
PROFILE_MIXED = "mixed"
PROFILE_CODE = "code_like"
PROFILE_UNKNOWN = "unknown"
PROFILE_AUTO = "auto"

VALID_PROFILES = {
    PROFILE_CJK,
    PROFILE_LATIN,
    PROFILE_MIXED,
    PROFILE_CODE,
    PROFILE_UNKNOWN,
}

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
DIGIT_RE = re.compile(r"[0-9]")
# Code/config operators & brackets — exclude prose punctuation (, . ! ? :).
CODE_SYMBOL_CHARS = set("{}[]()<>=;*/|&@#%$_\\\"'`~+-")
ASCII_VOWELS = set("aeiouAEIOU")
# Allowed whitespace that must not count as control noise.
_ALLOWED_WHITESPACE = set("\t\n\r ")

# Typical UTF-8→Latin-1 mojibake *sequences* (not single legitimate letters like ö/ñ).
MOJIBAKE_SEQUENCES = (
    "Ã§",
    "Ã¶",
    "Ã¤",
    "Ã¼",
    "Ã©",
    "Ã¨",
    "Ã ",
    "Ã±",
    "Ã®",
    "Ã¯",
    "Ã¥",
    "Ã¸",
    "Ã\xa0",
    "â€™",
    "â€œ",
    "â€",
    "Â ",
    "Â\xa0",
)
# Also catch generic "Ã" + continuation-byte-as-latin patterns.
MOJIBAKE_SEQUENCE_RE = re.compile(
    r"Ã[\x80-\xbfÀ-ÿ]|â€.|Â[\xa0 ]"
)

OCR_QUALITY_OK = "ok"
OCR_QUALITY_LOW = "low_quality"
OCR_QUALITY_UNUSABLE = "unusable"
OCR_QUALITY_EMPTY = "empty"
OCR_QUALITY_UNKNOWN = "unknown"

OCR_RUNTIME_SUCCEEDED = "succeeded"
OCR_RUNTIME_EMPTY = "empty"
OCR_RUNTIME_FAILED = "failed"
OCR_RUNTIME_UNAVAILABLE = "unavailable"
OCR_RUNTIME_DISABLED = "disabled"

# Categories treated as noise (format/private/surrogate/unassigned + most controls).
_NOISE_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn"}


# --- centralized thresholds (no scattered magic numbers) ---------------------
@dataclass(frozen=True)
class OcrQualityThresholds:
    min_chars_for_ratio: int = 20
    min_chars_for_structure: int = 40
    # profile detection
    cjk_share_primary: float = 0.45
    latin_share_primary: float = 0.45
    mixed_minor_share: float = 0.12
    code_symbol_share_floor: float = 0.06
    code_alnum_share_floor: float = 0.30
    code_line_hint_min: int = 2
    code_min_structure_signals: int = 2
    profile_confidence_floor: float = 0.35
    quality_score_ok_floor: float = 0.55
    quality_score_verify_below: float = 0.55
    default_confidence_for_score: float = 0.55
    # rejection
    printable_floor: float = 0.85
    noise_ceiling: float = 0.12
    # Any U+FFFD makes text unusable; counted once per occurrence (not double-penalized).
    replacement_char_ceiling: int = 0
    conf_unusable: float = 0.20
    conf_low: float = 0.35
    # cjk
    cjk_ratio_unusable: float = 0.05
    cjk_count_unusable: int = 3
    cjk_ratio_low: float = 0.15
    # latin / mixed coverage among meaningful chars
    latin_coverage_ok: float = 0.55
    mixed_coverage_ok: float = 0.50
    code_coverage_ok: float = 0.45
    # latin garbage (fake "English" from CJK handwriting OCR)
    latin_vowel_ratio_floor: float = 0.18
    latin_space_ratio_floor: float = 0.04
    latin_avg_token_max: float = 18.0
    latin_garbage_unique_ratio_floor: float = 0.55
    latin_token_diversity_floor: float = 0.15
    latin_min_tokens_for_diversity: int = 8


THRESHOLDS = OcrQualityThresholds()


def _is_latin_letter(ch: str) -> bool:
    if len(ch) != 1:
        return False
    if not unicodedata.category(ch).startswith("L"):
        return False
    return "LATIN" in unicodedata.name(ch, "")


def _is_latin_vowel(ch: str) -> bool:
    if ch in ASCII_VOWELS:
        return True
    if not _is_latin_letter(ch):
        return False
    base = unicodedata.normalize("NFD", ch)[:1]
    return bool(base) and base in ASCII_VOWELS


def _is_noise_char(ch: str) -> bool:
    if ch in _ALLOWED_WHITESPACE:
        return False
    if ch == "\ufffd":
        return True
    return unicodedata.category(ch) in _NOISE_CATEGORIES


def _has_disallowed_unicode(text: str) -> bool:
    """True when control/format/private/surrogate/unassigned/replacement appear (Tab/LF/CR ok)."""
    for ch in text or "":
        if ch in _ALLOWED_WHITESPACE:
            continue
        if ch == "\ufffd":
            return True
        if unicodedata.category(ch) in _NOISE_CATEGORIES:
            return True
    return False


def _has_mojibake_sequence(text: str) -> bool:
    if not text:
        return False
    for seq in MOJIBAKE_SEQUENCES:
        if seq in text:
            return True
    return MOJIBAKE_SEQUENCE_RE.search(text) is not None


@dataclass
class OcrQualityAssessment:
    ocr_runtime_status: str
    ocr_quality_status: str
    ocr_mean_confidence: Optional[float]
    cjk_ratio: float
    quality_reason: str
    search_eligible: bool
    provider_version: str
    char_count: int
    cjk_count: int
    # R7.3 extensions (safe metadata)
    content_profile: str = PROFILE_UNKNOWN
    profile_confidence: float = 0.0
    meaningful_char_count: int = 0
    latin_count: int = 0
    latin_ratio: float = 0.0
    digit_count: int = 0
    digit_ratio: float = 0.0
    code_symbol_count: int = 0
    code_symbol_ratio: float = 0.0
    printable_ratio: float = 0.0
    noise_ratio: float = 0.0
    quality_score: float = 0.0

    def to_metadata(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class _TextStats:
    char_count: int
    cjk_count: int
    cjk_ratio: float
    latin_count: int
    latin_ratio: float
    digit_count: int
    digit_ratio: float
    code_symbol_count: int
    code_symbol_ratio: float
    meaningful_char_count: int
    printable_ratio: float
    noise_ratio: float
    replacement_count: int
    line_count: int
    space_ratio: float
    vowel_ratio: float
    avg_token_len: float
    unique_latin_ratio: float
    token_diversity: float
    token_count: int
    has_mojibake_sequence: bool
    has_disallowed_unicode: bool
    code_structure_signals: int


def _ratio(num: int, den: int) -> float:
    return (float(num) / float(den)) if den else 0.0


def _count_code_structure_signals(
    text: str,
    st: "_TextStats",
    thresholds: OcrQualityThresholds,
) -> int:
    """
    Independent code/config structure signals.
    code_line_hint (multiline/indent) is a real signal and is not bypassable by
    an equivalent symbol-ratio shortcut.
    """
    signals = 0
    raw = text or ""

    # 1) Multiline or indented structure (uses code_line_hint_min).
    has_indent = bool(re.search(r"(?m)^[ \t]+\S", raw))
    if st.line_count >= thresholds.code_line_hint_min or has_indent:
        signals += 1

    # 2) Paired brackets / braces.
    if (
        (raw.count("(") > 0 and raw.count(")") > 0)
        or (raw.count("{") > 0 and raw.count("}") > 0)
        or (raw.count("[") > 0 and raw.count("]") > 0)
    ):
        signals += 1

    # 3) Assignment / operator structure.
    if re.search(r"==|!=|<=|>=|=>|\+=|-=|\*=|/=|:=|(?<![<>!=])=(?![=>])", raw):
        signals += 1

    # 4) Identifier + digit combination (or identifier near code punctuation).
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\d|\d[A-Za-z_]", raw):
        signals += 1
    elif st.digit_count > 0 and re.search(r"[A-Za-z_]{2,}", raw) and any(
        ch in raw for ch in "{}[]()="
    ):
        signals += 1

    # 5) Code separators: semicolons, or JSON/config key lines with braces.
    if ";" in raw:
        signals += 1
    elif re.search(r'(?m)^\s*["\'A-Za-z_][^:\n]{0,40}:\s*', raw) and (
        "{" in raw or "}" in raw
    ):
        signals += 1

    return signals


def compute_text_stats(text: str, *, thresholds: OcrQualityThresholds = THRESHOLDS) -> _TextStats:
    raw = text or ""
    n = len(raw)
    cjk = len(CJK_RE.findall(raw))
    latin_chars = [ch for ch in raw if _is_latin_letter(ch)]
    latin = len(latin_chars)
    digits = len(DIGIT_RE.findall(raw))
    code_syms = sum(1 for ch in raw if ch in CODE_SYMBOL_CHARS)
    meaningful = cjk + latin + digits + code_syms
    noise_chars = sum(1 for ch in raw if _is_noise_char(ch))
    # Replacement character counted once per occurrence of U+FFFD only.
    replacement = raw.count("\ufffd")
    printable = n - noise_chars
    spaces = sum(1 for ch in raw if ch.isspace())
    lines = max(1, raw.count("\n") + (1 if raw.strip() else 0))
    vowels = sum(1 for ch in latin_chars if _is_latin_vowel(ch))
    vowel_ratio = _ratio(vowels, latin)
    tokens = [t for t in re.split(r"\s+", raw.strip()) if t]
    avg_token = (sum(len(t) for t in tokens) / len(tokens)) if tokens else 0.0
    token_diversity = (
        len({t.lower() for t in tokens}) / len(tokens) if tokens else 0.0
    )
    unique_latin = len({ch.casefold() for ch in latin_chars})
    unique_latin_ratio = _ratio(unique_latin, max(1, len(latin_chars)))
    has_mojibake = _has_mojibake_sequence(raw)
    has_bad_unicode = _has_disallowed_unicode(raw)
    # Build a provisional stats object for signal counting (signals need line_count etc.).
    provisional = _TextStats(
        char_count=n,
        cjk_count=cjk,
        cjk_ratio=_ratio(cjk, n),
        latin_count=latin,
        latin_ratio=_ratio(latin, n),
        digit_count=digits,
        digit_ratio=_ratio(digits, n),
        code_symbol_count=code_syms,
        code_symbol_ratio=_ratio(code_syms, n),
        meaningful_char_count=meaningful,
        printable_ratio=_ratio(printable, n) if n else 0.0,
        noise_ratio=_ratio(noise_chars, n) if n else 0.0,
        replacement_count=replacement,
        line_count=lines,
        space_ratio=_ratio(spaces, n) if n else 0.0,
        vowel_ratio=vowel_ratio,
        avg_token_len=avg_token,
        unique_latin_ratio=unique_latin_ratio,
        token_diversity=token_diversity,
        token_count=len(tokens),
        has_mojibake_sequence=has_mojibake,
        has_disallowed_unicode=has_bad_unicode,
        code_structure_signals=0,
    )
    provisional.code_structure_signals = _count_code_structure_signals(
        raw, provisional, thresholds
    )
    return provisional


def detect_content_profile(
    text: str,
    *,
    stats: Optional[_TextStats] = None,
    thresholds: OcrQualityThresholds = THRESHOLDS,
) -> Tuple[str, float]:
    """Infer content profile from character/structure statistics only."""
    st = stats or compute_text_stats(text, thresholds=thresholds)
    if st.char_count == 0:
        return PROFILE_UNKNOWN, 0.0

    meaningful = max(st.meaningful_char_count, 1)
    cjk_share = st.cjk_count / meaningful
    latin_share = st.latin_count / meaningful
    digit_share = st.digit_count / meaningful
    code_share = st.code_symbol_count / meaningful

    looks_code = (
        st.code_structure_signals >= thresholds.code_min_structure_signals
        and st.code_symbol_ratio >= thresholds.code_symbol_share_floor
        and (st.latin_ratio + st.digit_ratio) >= thresholds.code_alnum_share_floor
        and cjk_share < thresholds.mixed_minor_share
    )
    if looks_code:
        conf = min(
            1.0,
            0.40
            + 0.10 * st.code_structure_signals
            + st.code_symbol_ratio
            + 0.15 * min(1.0, st.latin_ratio + st.digit_ratio),
        )
        return PROFILE_CODE, conf

    if (
        cjk_share >= thresholds.cjk_share_primary
        and latin_share < thresholds.mixed_minor_share
    ):
        return PROFILE_CJK, min(1.0, 0.5 + cjk_share)
    if (
        latin_share >= thresholds.latin_share_primary
        and cjk_share < thresholds.mixed_minor_share
    ):
        if _looks_like_latin_garbage(st, thresholds):
            return PROFILE_UNKNOWN, 0.25
        return PROFILE_LATIN, min(1.0, 0.5 + latin_share)
    if (
        cjk_share >= thresholds.mixed_minor_share
        and latin_share >= thresholds.mixed_minor_share
    ):
        return PROFILE_MIXED, min(1.0, 0.4 + cjk_share + latin_share)
    if cjk_share >= thresholds.mixed_minor_share:
        return PROFILE_CJK, min(1.0, 0.4 + cjk_share)
    if latin_share >= thresholds.mixed_minor_share:
        if _looks_like_latin_garbage(st, thresholds):
            return PROFILE_UNKNOWN, 0.2
        return PROFILE_LATIN, min(1.0, 0.4 + latin_share)
    if (
        digit_share + code_share >= 0.5
        and st.code_structure_signals >= thresholds.code_min_structure_signals
    ):
        return PROFILE_CODE, 0.4
    return PROFILE_UNKNOWN, 0.15


def _looks_like_latin_garbage(
    st: _TextStats,
    thresholds: OcrQualityThresholds = THRESHOLDS,
) -> bool:
    """True when latin-heavy text lacks English/code structure (handwriting soup)."""
    if st.latin_count < thresholds.min_chars_for_ratio:
        return False
    if st.has_mojibake_sequence:
        return True
    if st.char_count >= thresholds.min_chars_for_structure:
        if st.vowel_ratio < thresholds.latin_vowel_ratio_floor:
            return True
        if st.space_ratio < thresholds.latin_space_ratio_floor and st.line_count < 2:
            return True
        if st.avg_token_len > thresholds.latin_avg_token_max:
            return True
        if (
            st.token_count >= thresholds.latin_min_tokens_for_diversity
            and st.token_diversity < thresholds.latin_token_diversity_floor
            and st.cjk_count == 0
        ):
            return True
        if (
            st.unique_latin_ratio >= thresholds.latin_garbage_unique_ratio_floor
            and st.vowel_ratio < thresholds.latin_vowel_ratio_floor + 0.05
            and st.cjk_count == 0
        ):
            return True
    return False


def _resolve_profile(
    text: str,
    *,
    expected_script: str,
    stats: _TextStats,
    thresholds: OcrQualityThresholds,
) -> Tuple[str, float]:
    raw = (expected_script or PROFILE_AUTO).strip().lower()
    if raw in {"", PROFILE_AUTO, "default"}:
        return detect_content_profile(text, stats=stats, thresholds=thresholds)
    if raw in VALID_PROFILES:
        # Explicit override: still compute confidence from detection agreement.
        auto, auto_conf = detect_content_profile(text, stats=stats, thresholds=thresholds)
        conf = 0.9 if auto == raw else max(0.5, auto_conf * 0.5)
        return raw, conf
    # Unknown override string → auto
    return detect_content_profile(text, stats=stats, thresholds=thresholds)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _score_for_profile(
    profile: str,
    st: _TextStats,
    *,
    mean_confidence: Optional[float],
    thresholds: OcrQualityThresholds,
) -> float:
    conf = (
        float(mean_confidence)
        if mean_confidence is not None
        else thresholds.default_confidence_for_score
    )
    conf = _clamp01(conf)
    meaningful = max(st.meaningful_char_count, 1)
    coverage = st.meaningful_char_count / max(st.char_count, 1)
    noise_pen = max(0.0, 1.0 - st.noise_ratio * 4.0)

    if profile == PROFILE_CJK:
        base = 0.55 * st.cjk_ratio + 0.25 * conf + 0.20 * coverage
    elif profile == PROFILE_LATIN:
        structure = 0.0
        if not _looks_like_latin_garbage(st, thresholds):
            structure = 0.5 * min(1.0, st.vowel_ratio / 0.35) + 0.5 * min(
                1.0, st.space_ratio / 0.12
            )
        base = 0.40 * st.latin_ratio + 0.25 * conf + 0.20 * structure + 0.15 * coverage
    elif profile == PROFILE_MIXED:
        script_cov = (st.cjk_count + st.latin_count + st.digit_count) / meaningful
        base = 0.50 * script_cov + 0.30 * conf + 0.20 * coverage
    elif profile == PROFILE_CODE:
        alnum = (st.latin_count + st.digit_count) / meaningful
        base = (
            0.35 * min(1.0, st.code_symbol_ratio / 0.15)
            + 0.35 * alnum
            + 0.20 * conf
            + 0.10 * min(1.0, st.line_count / 4.0)
        )
    else:
        base = 0.25 * coverage + 0.25 * conf + 0.10 * st.printable_ratio
    return _clamp01(base * noise_pen)


def _assessment(
    *,
    runtime_status: str,
    quality_status: str,
    reason: str,
    search_eligible: bool,
    mean_confidence: Optional[float],
    provider_version: str,
    st: _TextStats,
    profile: str,
    profile_confidence: float,
    quality_score: float,
) -> OcrQualityAssessment:
    return OcrQualityAssessment(
        ocr_runtime_status=runtime_status,
        ocr_quality_status=quality_status,
        ocr_mean_confidence=mean_confidence,
        cjk_ratio=st.cjk_ratio,
        quality_reason=reason,
        search_eligible=search_eligible,
        provider_version=provider_version,
        char_count=st.char_count,
        cjk_count=st.cjk_count,
        content_profile=profile,
        profile_confidence=round(profile_confidence, 4),
        meaningful_char_count=st.meaningful_char_count,
        latin_count=st.latin_count,
        latin_ratio=st.latin_ratio,
        digit_count=st.digit_count,
        digit_ratio=st.digit_ratio,
        code_symbol_count=st.code_symbol_count,
        code_symbol_ratio=st.code_symbol_ratio,
        printable_ratio=st.printable_ratio,
        noise_ratio=st.noise_ratio,
        quality_score=round(quality_score, 4),
    )


def assess_ocr_text(
    text: str,
    *,
    runtime_status: str = OCR_RUNTIME_SUCCEEDED,
    mean_confidence: Optional[float] = None,
    provider: str = "",
    provider_version: str = "",
    expected_script: str = PROFILE_AUTO,
    thresholds: OcrQualityThresholds = THRESHOLDS,
) -> OcrQualityAssessment:
    """
    Assess OCR text quality.

    Production default: expected_script="auto" (profile inferred).
    Pass an explicit profile only as a compatibility override.
    """
    status = (runtime_status or OCR_RUNTIME_SUCCEEDED).strip().lower()
    st = compute_text_stats(text, thresholds=thresholds)
    prov = provider_version or provider or "unknown"
    profile, profile_conf = _resolve_profile(
        text, expected_script=expected_script, stats=st, thresholds=thresholds
    )
    score = _score_for_profile(
        profile, st, mean_confidence=mean_confidence, thresholds=thresholds
    )

    if status in {OCR_RUNTIME_DISABLED, OCR_RUNTIME_UNAVAILABLE, OCR_RUNTIME_FAILED}:
        return _assessment(
            runtime_status=status,
            quality_status=OCR_QUALITY_UNKNOWN,
            reason=f"runtime_{status}",
            search_eligible=False,
            mean_confidence=mean_confidence,
            provider_version=prov,
            st=st,
            profile=profile,
            profile_confidence=profile_conf,
            quality_score=0.0,
        )
    if status == OCR_RUNTIME_EMPTY or st.char_count == 0:
        return _assessment(
            runtime_status=OCR_RUNTIME_EMPTY if status == OCR_RUNTIME_EMPTY else status,
            quality_status=OCR_QUALITY_EMPTY,
            reason="empty_text",
            search_eligible=False,
            mean_confidence=mean_confidence,
            provider_version=prov,
            st=st,
            profile=PROFILE_UNKNOWN,
            profile_confidence=0.0,
            quality_score=0.0,
        )

    # Universal rejections: control/format/private, replacement, mojibake, high noise.
    if (
        st.replacement_count > thresholds.replacement_char_ceiling
        or st.has_mojibake_sequence
        or st.has_disallowed_unicode
    ):
        return _assessment(
            runtime_status=OCR_RUNTIME_SUCCEEDED,
            quality_status=OCR_QUALITY_UNUSABLE,
            reason="mojibake_or_disallowed_unicode",
            search_eligible=False,
            mean_confidence=mean_confidence,
            provider_version=prov,
            st=st,
            profile=profile if profile != PROFILE_LATIN else PROFILE_UNKNOWN,
            profile_confidence=min(profile_conf, 0.3),
            quality_score=min(score, 0.15),
        )
    if st.printable_ratio < thresholds.printable_floor or st.noise_ratio > thresholds.noise_ceiling:
        return _assessment(
            runtime_status=OCR_RUNTIME_SUCCEEDED,
            quality_status=OCR_QUALITY_UNUSABLE,
            reason="high_noise_or_nonprintable",
            search_eligible=False,
            mean_confidence=mean_confidence,
            provider_version=prov,
            st=st,
            profile=PROFILE_UNKNOWN,
            profile_confidence=min(profile_conf, 0.25),
            quality_score=min(score, 0.1),
        )
    if mean_confidence is not None and mean_confidence < thresholds.conf_unusable:
        return _assessment(
            runtime_status=OCR_RUNTIME_SUCCEEDED,
            quality_status=OCR_QUALITY_UNUSABLE,
            reason="confidence_unusable",
            search_eligible=False,
            mean_confidence=mean_confidence,
            provider_version=prov,
            st=st,
            profile=profile,
            profile_confidence=profile_conf,
            quality_score=min(score, 0.2),
        )

    # Profile-specific gates.
    if profile == PROFILE_CJK:
        if st.char_count >= thresholds.min_chars_for_structure and st.cjk_ratio < thresholds.cjk_ratio_unusable:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_UNUSABLE,
                reason="cjk_ratio_below_floor",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.15),
            )
        if st.char_count >= thresholds.min_chars_for_ratio and st.cjk_count < thresholds.cjk_count_unusable:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_UNUSABLE,
                reason="cjk_count_below_floor",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.15),
            )
        if mean_confidence is not None and mean_confidence < thresholds.conf_low:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="confidence_below_threshold",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.35),
            )
        if st.char_count >= thresholds.min_chars_for_structure and st.cjk_ratio < thresholds.cjk_ratio_low:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="cjk_ratio_low",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.35),
            )

    elif profile == PROFILE_LATIN:
        if _looks_like_latin_garbage(st, thresholds):
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_UNUSABLE,
                reason="latin_garbage_structure",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=PROFILE_UNKNOWN,
                profile_confidence=0.2,
                quality_score=min(score, 0.12),
            )
        coverage = st.latin_count / max(st.meaningful_char_count, 1)
        if st.char_count >= thresholds.min_chars_for_ratio and coverage < 0.35:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_UNUSABLE,
                reason="latin_coverage_below_floor",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.15),
            )
        if mean_confidence is not None and mean_confidence < thresholds.conf_low:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="confidence_below_threshold",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.35),
            )
        if coverage < thresholds.latin_coverage_ok and st.char_count >= thresholds.min_chars_for_structure:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="latin_coverage_low",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.4),
            )

    elif profile == PROFILE_MIXED:
        coverage = (st.cjk_count + st.latin_count + st.digit_count) / max(
            st.meaningful_char_count, 1
        )
        if mean_confidence is not None and mean_confidence < thresholds.conf_low:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="confidence_below_threshold",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.35),
            )
        if st.char_count >= thresholds.min_chars_for_ratio and coverage < thresholds.mixed_coverage_ok:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="mixed_coverage_low",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.4),
            )

    elif profile == PROFILE_CODE:
        coverage = (st.latin_count + st.digit_count + st.code_symbol_count) / max(
            st.meaningful_char_count, 1
        )
        if mean_confidence is not None and mean_confidence < thresholds.conf_low:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="confidence_below_threshold",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.35),
            )
        if st.char_count >= thresholds.min_chars_for_ratio and coverage < thresholds.code_coverage_ok:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="code_coverage_low",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=profile,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.4),
            )

    else:  # unknown — conservative review, never claim strong English success
        if _looks_like_latin_garbage(st, thresholds) or st.latin_ratio > 0.5 and st.cjk_count == 0:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_UNUSABLE,
                reason="unknown_latin_untrusted",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=PROFILE_UNKNOWN,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.15),
            )
        if mean_confidence is not None and mean_confidence < thresholds.conf_low:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="unknown_low_confidence",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=PROFILE_UNKNOWN,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.3),
            )
        # Unknown with weak profile confidence stays non-searchable.
        if profile_conf < thresholds.profile_confidence_floor:
            return _assessment(
                runtime_status=OCR_RUNTIME_SUCCEEDED,
                quality_status=OCR_QUALITY_LOW,
                reason="unknown_needs_review",
                search_eligible=False,
                mean_confidence=mean_confidence,
                provider_version=prov,
                st=st,
                profile=PROFILE_UNKNOWN,
                profile_confidence=profile_conf,
                quality_score=min(score, 0.35),
            )

    return _assessment(
        runtime_status=OCR_RUNTIME_SUCCEEDED,
        quality_status=OCR_QUALITY_OK,
        reason="ok",
        search_eligible=True,
        mean_confidence=mean_confidence,
        provider_version=prov,
        st=st,
        profile=profile,
        profile_confidence=profile_conf,
        quality_score=max(score, thresholds.quality_score_ok_floor),
    )


def merge_quality_into_metadata(
    metadata: Optional[Mapping[str, Any]],
    assessment: OcrQualityAssessment,
) -> Dict[str, Any]:
    out = dict(metadata or {})
    out.update(assessment.to_metadata())
    # UI must not claim "recognized" for unusable OCR.
    if not assessment.search_eligible and assessment.ocr_quality_status in {
        OCR_QUALITY_LOW,
        OCR_QUALITY_UNUSABLE,
        OCR_QUALITY_EMPTY,
    }:
        out["ocr_display_status"] = assessment.ocr_quality_status
        out["ocr_text_searchable"] = False
    else:
        out["ocr_display_status"] = assessment.ocr_quality_status
        out["ocr_text_searchable"] = bool(assessment.search_eligible)
    return out


def is_ocr_chunk_search_eligible(metadata: Optional[Mapping[str, Any]]) -> bool:
    meta = metadata or {}
    if "search_eligible" in meta:
        return bool(meta.get("search_eligible"))
    if "ocr_text_searchable" in meta:
        return bool(meta.get("ocr_text_searchable"))
    # Legacy: succeeded alone is insufficient — require quality ok if present.
    quality = str(meta.get("ocr_quality_status") or "").strip()
    if quality:
        return quality == OCR_QUALITY_OK
    return False


def provider_stability_rank(provider: str) -> int:
    """Lower is more preferred on exact ties (deterministic)."""
    order = {
        "rapidocr_ppocrv6_small": 0,
        "pytesseract": 1,
        "tesseract": 1,
        "paddleocr": 2,
        "disabled": 9,
    }
    return int(order.get(str(provider or ""), 5))
