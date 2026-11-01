"""Classify errors from model calls: is this the model/infra being *unavailable*
(retry when it recovers) or a *genuine* failure (a real bug / bad request)?

Unavailable = transient network/endpoint conditions: connection resets, timeouts,
rate limits (429), server errors (5xx), forbidden (403 — the endpoint blocking us,
seen as nginx "403 Forbidden"), and malformed/compressed responses from a proxy
(zlib decompression errors). Out of money (402) is here too: after a top-up the same
endpoint answers again, so the work waits instead of dying in five quick retries. A
wrong API key (401 AuthenticationError) is NOT here: fixing it means a new key, so it
should surface as a real failure.
"""

from __future__ import annotations

import zlib


class ReplyTruncated(RuntimeError):
    """Модель исчерпала max_tokens, не договорив: ответ оборван и непригоден.

    Отдельный тип, потому что это не сбой провайдера, а нехватка лимита — чаще всего
    рассуждающая модель потратила его на размышления. Без этой ошибки обрезанный ответ
    выглядел бы как «модель ничего не нашла», и задачи из письма молча терялись."""

# Substrings of stored error strings that mean "model/infra was unavailable". Used to
# reclassify already-persisted failures (we only have the string, not the exception).
_UNAVAILABLE_MARKERS = (
    "insufficient balance",
    "error code: 402",
    "permissiondenied",
    "403 forbidden",
    "ratelimit",
    "rate limit",
    "apiconnection",
    "apitimeout",
    "connection error",
    "connection reset",
    "internalservererror",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "decompress",
    "incorrect header check",
    "unknown compression method",
)


# Что сказать человеку про сохранённую ошибку вызова модели. Порядок важен: «402» и
# «401» должны разбираться до общего «провайдер был недоступен», иначе кончившиеся
# деньги выглядят как сетевой сбой, и человек ждёт, что само пройдёт.
_EXPLAINED: tuple[tuple[tuple[str, ...], str], ...] = (
    (("insufficient balance", "insufficient_quota", "exceeded your current quota",
      "error code: 402", "402 -"),
     "у провайдера модели кончились средства"),
    (("authenticationerror", "invalid api key", "invalid_api_key", "error code: 401"),
     "провайдер не принял ключ"),
    (("ratelimit", "rate limit", "error code: 429"),
     "провайдер ограничил частоту запросов"),
    (("finish_reason=length", "stop_reason=max_tokens"),
     "ответ модели не уместился в лимит токенов — если модель рассуждает, включите "
     "«без рассуждений» в подключении"),
    (_UNAVAILABLE_MARKERS, "провайдер был недоступен"),
)


def explain(text: str | None) -> str | None:
    """Причина отказа модели человеческими словами, или None — сказать нечего.

    Незнакомую ошибку не прячем и не пересказываем: показываем её хвост как есть.
    Молчание тут хуже непонятного текста — по нему хотя бы можно искать."""
    if not text or not text.strip():
        return None
    low = text.lower()
    for markers, said in _EXPLAINED:
        if any(m in low for m in markers):
            return said
    return "провайдер ответил: " + text.strip().splitlines()[-1][:120]


def message_is_model_unavailable(text: str | None) -> bool:
    """Best-effort classification from a stored error *string*."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _UNAVAILABLE_MARKERS)


def is_model_unavailable(exc: BaseException) -> bool:
    """Classify a live exception, walking the __cause__/__context__ chain (the openai
    SDK wraps transport errors)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if _one_is_unavailable(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _one_is_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, (zlib.error, ConnectionError, TimeoutError)):
        return True
    try:
        import openai
    except Exception:  # noqa: BLE001 — SDK optional / import failure shouldn't crash classification
        openai = None  # type: ignore[assignment]
    if openai is not None:
        transient = tuple(
            c
            for c in (
                getattr(openai, "APIConnectionError", None),
                getattr(openai, "APITimeoutError", None),
                getattr(openai, "RateLimitError", None),
                getattr(openai, "InternalServerError", None),
                getattr(openai, "PermissionDeniedError", None),  # 403 endpoint block
            )
            if isinstance(c, type)
        )
        if transient and isinstance(exc, transient):
            return True
        status_err = getattr(openai, "APIStatusError", None)
        if isinstance(status_err, type) and isinstance(exc, status_err):
            code = getattr(exc, "status_code", None)
            if code in (402, 403, 408, 409, 425, 429) or (isinstance(code, int) and code >= 500):
                return True
    # Fallback to the message (covers Anthropic SDK and anything not typed above).
    return message_is_model_unavailable(str(exc))
