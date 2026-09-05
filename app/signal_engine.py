from app.indicators import rsi, macd, bollinger_bands, ema, atr, volume_spike

# Only turn indicator agreement into an actual signal once weighted
# confidence clears this bar — below it, indicators are mixed/uncertain and
# staying silent is more useful than a low-conviction alert.
SIGNAL_CONFIDENCE_THRESHOLD = 65

# Stop distance is this many ATRs from entry (volatility-sized, not a fixed
# percentage); target is set at a fixed multiple of the stop distance so
# every signal carries the same built-in risk:reward ratio.
STOP_ATR_MULTIPLE = 1.5
TARGET_RISK_REWARD = 2.5


def score_signal(highs: list[float], lows: list[float], closes: list[float], volumes: list[float]) -> dict | None:
    """Given OHLCV history (oldest -> newest), return a signal dict or None
    if no confident direction emerges. Never raises on thin history —
    indicators that can't compute yet simply abstain rather than voting."""
    votes = []  # each: (direction: -1 sell / 1 buy, weight, human-readable reason)

    r = rsi(closes, 14)
    if r is not None:
        if r < 30:
            votes.append((1, 1.0, f"RSI oversold ({r:.0f})"))
        elif r > 70:
            votes.append((-1, 1.0, f"RSI overbought ({r:.0f})"))

    m = macd(closes)
    if m is not None and m['histogram_prev'] is not None:
        if m['histogram_prev'] <= 0 and m['histogram'] > 0:
            votes.append((1, 1.5, "bullish MACD crossover"))
        elif m['histogram_prev'] >= 0 and m['histogram'] < 0:
            votes.append((-1, 1.5, "bearish MACD crossover"))
        elif m['histogram'] > 0:
            votes.append((1, 0.5, "MACD histogram positive"))
        elif m['histogram'] < 0:
            votes.append((-1, 0.5, "MACD histogram negative"))

    bb = bollinger_bands(closes, 20, 2)
    if bb is not None:
        price = closes[-1]
        if price <= bb['lower']:
            votes.append((1, 1.0, "price at/below lower Bollinger Band"))
        elif price >= bb['upper']:
            votes.append((-1, 1.0, "price at/above upper Bollinger Band"))

    ema9, ema21, ema50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    market_condition = 'ranging'
    if ema9 is not None and ema21 is not None and ema50 is not None:
        if ema9 > ema21 > ema50:
            market_condition = 'uptrend'
            votes.append((1, 1.0, "uptrend (9/21/50 EMA aligned)"))
        elif ema9 < ema21 < ema50:
            market_condition = 'downtrend'
            votes.append((-1, 1.0, "downtrend (9/21/50 EMA aligned)"))

    if votes and volumes and volume_spike(volumes):
        # Volume has no inherent direction of its own — it only confirms
        # whichever way the other indicators already lean, so it's gated
        # behind `votes` already being non-empty and never introduces a
        # signal by itself.
        leaning = 1 if sum(v[0] * v[1] for v in votes) > 0 else -1
        votes.append((leaning, 0.5, "volume spike confirms move"))

    if not votes:
        return None

    total_weight = sum(v[1] for v in votes)
    weighted_sum = sum(v[0] * v[1] for v in votes)
    confidence = round(abs(weighted_sum) / total_weight * 100)
    direction = 'buy' if weighted_sum > 0 else 'sell' if weighted_sum < 0 else None

    if direction is None or confidence < SIGNAL_CONFIDENCE_THRESHOLD:
        return None

    matching_side = 1 if direction == 'buy' else -1
    reasons = [v[2] for v in votes if v[0] == matching_side]

    entry_price = closes[-1]
    atr_value = atr(highs, lows, closes, 14) or (entry_price * 0.01)  # 1% fallback if ATR isn't computable yet
    stop_distance = atr_value * STOP_ATR_MULTIPLE
    target_distance = stop_distance * TARGET_RISK_REWARD

    if direction == 'buy':
        stop_loss = entry_price - stop_distance
        target_price = entry_price + target_distance
    else:
        stop_loss = entry_price + stop_distance
        target_price = entry_price - target_distance

    return {
        'signal_type': direction,
        'confidence': confidence,
        'entry_price': round(entry_price, 4),
        'target_price': round(target_price, 4),
        'stop_loss': round(stop_loss, 4),
        'risk_reward_ratio': TARGET_RISK_REWARD,
        'reason': ' + '.join(reasons),
        'market_condition': market_condition,
    }
