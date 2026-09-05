import os
import time
import requests

# Optional at import time, same pattern as the calendar-sync integrations —
# this feature ships disabled until a real Finnhub key exists, instead of
# taking the rest of the API down for everything else in the meantime.
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

RESOLUTION_MAP = {'5min': '5', '15min': '15', '1h': '60', '1day': 'D'}
SECONDS_PER_BAR = {'5': 5 * 60, '15': 15 * 60, '60': 60 * 60, 'D': 24 * 60 * 60}


def is_market_data_configured() -> bool:
    return bool(FINNHUB_API_KEY)


def fetch_ohlcv(symbol: str, timeframe: str, count: int = 100) -> dict | None:
    """Returns {'highs': [...], 'lows': [...], 'closes': [...], 'volumes': [...]}
    oldest-to-newest, or None if unavailable/misconfigured/erroring. Never
    raises — a bad symbol, a rate limit, or a Finnhub outage should skip
    that one symbol for this run, not take down the whole generation job."""
    if not FINNHUB_API_KEY:
        return None

    resolution = RESOLUTION_MAP.get(timeframe, '60')
    seconds_per_bar = SECONDS_PER_BAR.get(resolution, 3600)
    now = int(time.time())
    # Padded 50% extra to absorb weekends/holidays where the market was
    # closed, so we still end up with `count` real trading bars.
    lookback = int(count * seconds_per_bar * 1.5)

    try:
        resp = requests.get(
            'https://finnhub.io/api/v1/stock/candle',
            params={
                'symbol': symbol,
                'resolution': resolution,
                'from': now - lookback,
                'to': now,
                'token': FINNHUB_API_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('s') != 'ok' or not data.get('c'):
            return None
        return {
            'highs': data['h'],
            'lows': data['l'],
            'closes': data['c'],
            'volumes': data['v'],
        }
    except Exception:
        return None
