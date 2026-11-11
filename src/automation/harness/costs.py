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


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    key = model.split("/")[-1]
    rates = _PRICING.get(key, (1.0, 3.0))
    return round((tokens_in * rates[0] + tokens_out * rates[1]) / 1_000_000, 6)
