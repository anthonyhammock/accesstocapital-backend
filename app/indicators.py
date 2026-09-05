# Pure-Python technical indicators — no pandas/numpy dependency, since the
# rest of this codebase is already dependency-light and these are simple
# enough to implement directly and unit-test against hand-computed values.
# Every function takes plain lists ordered oldest-to-newest (index 0 is the
# earliest candle) and returns None (or an empty structure) when there isn't
# enough history yet, rather than raising — the signal engine treats "not
# enough data" as "this indicator doesn't vote," not an error.


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA series (seeded with the initial SMA), not just the latest
    value — MACD needs the series to compute its own signal line on top."""
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for price in values[period:]:
        result.append((price - result[-1]) * multiplier + result[-1])
    return result


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. Needs at least period+1 closes (period changes)."""
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if len(closes) < slow + signal:
        return None

    ema_fast_series = ema_series(closes, fast)
    ema_slow_series = ema_series(closes, slow)
    # ema_fast_series[0] corresponds to closes[fast-1]; ema_slow_series[0]
    # corresponds to closes[slow-1] — align them to the same closes index
    # before subtracting.
    offset = slow - fast
    macd_line = [ema_fast_series[i + offset] - ema_slow_series[i] for i in range(len(ema_slow_series))]

    signal_line = ema_series(macd_line, signal)
    if not signal_line:
        return None
    histogram = [macd_line[-len(signal_line) + i] - signal_line[i] for i in range(len(signal_line))]

    return {
        'macd': macd_line[-1],
        'signal': signal_line[-1],
        'histogram': histogram[-1],
        'histogram_prev': histogram[-2] if len(histogram) >= 2 else None,
    }


def bollinger_bands(closes: list[float], period: int = 20, std_dev: float = 2) -> dict | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance ** 0.5
    return {
        'upper': mean + std_dev * std,
        'middle': mean,
        'lower': mean - std_dev * std,
    }


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Wilder's Average True Range. closes[i-1] is the prior close used for
    gap-aware true range at index i."""
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(highs)):
        true_ranges.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def volume_spike(volumes: list[float], lookback: int = 20, threshold: float = 2.0) -> bool:
    """True if the latest volume is at least `threshold`x the average of the
    `lookback` bars before it (the latest bar itself is excluded from its
    own baseline, or every spike would partly average itself away)."""
    if len(volumes) < lookback + 1:
        return False
    baseline = volumes[-lookback - 1:-1]
    avg_volume = sum(baseline) / lookback
    if avg_volume == 0:
        return False
    return volumes[-1] >= avg_volume * threshold
