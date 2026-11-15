# Cost per 1M tokens (input, output) in USD
_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini":               (0.15,   0.60),
    "gpt-4o":                    (2.50,  10.00),
    "gpt-6-luna":                (0.10,   0.50),
    "gpt-6-sol":                 (2.00,  10.00),
    # Anthropic
    "claude-haiku-4-5-20251001": (1.00,   5.00),
    "claude-sonnet-4-6":         (3.00,  15.00),
    "claude-haiku-5-5":          (0.10,   0.50),
    "claude-sonnet-5-5":         (2.00,  10.00),
    "claude-opus-5-5":           (4.00,  20.00),
    # Gemini
    "gemini-3.5-flash":          (1.50,   9.00),  # launch price; no longer on Google's page
    "gemini-3.1-flash-lite":     (0.04,   0.15),
    "gemini-2.5-pro":            (1.25,  10.00),
    "gemini-2.5-flash":          (0.075,  0.30),
    "gemini-2.5-flash-lite":     (0.04,   0.15),
    # Google raises Gemini 4 to $4/$20 on 2026-12-01 and 3.8 Flash to $1.50/$7.50
    # on 2027-01-01 — update these then.
    "gemini-3.5-flash-lite":     (0.30,   2.50),
    "gemini-3.8-flash":          (0.75,   3.75),
    "gemini-4-argon":            (2.00,  10.00),
    "gemini-4-carbon":           (2.00,  10.00),
    # Legacy (kept for existing run history)
    "gemini-3-flash-preview":    (0.075,  0.30),
    "gemini-2.0-flash":          (0.10,   0.40),
    "gemini-2.0-flash-lite":     (0.04,   0.15),
    "gemini-1.5-flash":          (0.075,  0.30),
    "gemini-1.5-pro":            (1.25,   5.00),
}


# Cache rates per 1M tokens (read, write) in USD, from the providers' pricing pages
# (2026-11). Only verified models are listed: a model missing here is charged the full
# input rate for cache reads and 1.25x input for cache writes, which can overestimate
# but never underestimate. Writes are Anthropic's 5-minute-TTL rate; OpenAI and Gemini
# (implicit caching) have no write charge, so their write rate is just the input rate.
_CACHE_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI — "cached input"
    "gpt-4o-mini":               (0.075,  0.15),
    "gpt-4o":                    (1.25,   2.50),
    "gpt-6-luna":                (0.01,   0.10),
    "gpt-6-sol":                 (0.20,   2.00),
    # Anthropic — "cache hits and refreshes", "5m cache writes"
    "claude-haiku-4-5-20251001": (0.10,   1.25),
    "claude-sonnet-4-6":         (0.30,   3.75),
    "claude-haiku-5-5":          (0.01,   0.125),
    "claude-sonnet-5-5":         (0.10,   2.50),
    "claude-opus-5-5":           (0.20,   5.00),
    # Gemini — "context caching" (storage fee applies to explicit caches only)
    "gemini-3.1-flash-lite":     (0.025,  0.04),
    "gemini-2.5-pro":            (0.125,  1.25),
    "gemini-2.5-flash":          (0.03,   0.075),
    "gemini-2.5-flash-lite":     (0.01,   0.04),
    "gemini-3.5-flash-lite":     (0.03,   0.30),
    "gemini-3.8-flash":          (0.075,  0.75),  # $0.15 from 2027-01-01
    # Already the 2026-12-01 rate ($0.10 until then), so this is not stale in weeks.
    "gemini-4-argon":            (0.20,   2.00),
    "gemini-4-carbon":           (0.20,   2.00),
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int,
                  cached_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """Cost in USD. ``tokens_in`` is the whole prompt; ``cached_tokens`` (cache reads)
    and ``cache_write_tokens`` are the parts of it billed at cache rates."""
    key = model.split("/")[-1]
    rates = _PRICING.get(key, (1.0, 3.0))
    read_rate, write_rate = _CACHE_PRICING.get(key, (rates[0], rates[0] * 1.25))
    # Sub-counts can never exceed the prompt; clamp so bad usage data can't go negative.
    write = min(max(cache_write_tokens or 0, 0), tokens_in)
    read = min(max(cached_tokens or 0, 0), tokens_in - write)
    uncached = tokens_in - write - read
    total = uncached * rates[0] + read * read_rate + write * write_rate + tokens_out * rates[1]
    return round(total / 1_000_000, 6)
