# BlissPoint Trading Signals — educational/informational technical-analysis
# signals with manual execution by the user. This is NOT investment advice;
# every signal and every alert carries that disclaimer. No broker API
# integration on purpose (compliance-safe): the user always executes
# manually on their own broker.
#
# Adapted from a spec written for Supabase/Vercel/Node onto this app's
# actual stack: FastAPI + Postgres + Cloud Run, JWT auth via
# get_current_user_id (not Supabase RLS), and a Cloud Scheduler job hitting
# a CRON_SECRET-protected endpoint (not Vercel Cron).
#
# Signals are generated once per (symbol, timeframe) — not per user — even
# though many users may watch the same symbol; a user's "current signals"
# is computed by joining their watchlist against recent rows here, so N
# users watching AAPL still costs one market-data fetch and one signal row.

import os
from datetime import datetime, timedelta, timezone, time as time_type
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models import WatchlistEntry, Signal, AlertPreference, SignalAlert, TradeLog, User
from app.auth import get_current_user_id
from app.market_data import fetch_ohlcv, fetch_quote, is_market_data_configured
from app.signal_engine import score_signal

router = APIRouter(prefix="/api/trading")

DISCLAIMER = (
    "Educational/informational only — not investment advice. AI-generated "
    "signals are not a guarantee of future results, and past performance "
    "does not indicate future performance. Trading involves risk of loss; "
    "you are solely responsible for any trades you choose to execute."
)

# A pending signal that hasn't hit its target or stop within this window is
# marked 'expired' rather than left open forever — old, stale signals
# shouldn't stay "pending" indefinitely and skew the success-rate stats.
SIGNAL_OUTCOME_EXPIRY_DAYS = 5

# Bucketed purely for the historical success-rate report — has no effect on
# signal generation itself (SIGNAL_CONFIDENCE_THRESHOLD in signal_engine.py
# is the actual generation cutoff).
CONFIDENCE_TIERS = [(65, 74), (75, 84), (85, 100)]

VALID_TIMEFRAMES = {'5min', '15min', '1h', '1day'}
VALID_STRATEGIES = {'momentum', 'mean_reversion', 'breakout', 'hybrid'}
VALID_SIDES = {'long', 'short'}
MAX_WATCHLIST_SYMBOLS = 15  # a technical safeguard against runaway API spend, not a paywall
SIGNAL_DEDUP_WINDOW_HOURS = 2  # don't re-signal the same symbol/timeframe within this window

CRON_SECRET = os.environ.get("CRON_SECRET")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "alerts@blisspointaccess.com")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")


# ============================================
# Request models
# ============================================

class WatchlistEntryCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    timeframe: str = Field(default='1h')
    strategy_type: str = Field(default='hybrid')

class AlertPreferenceUpdate(BaseModel):
    email_enabled: bool = True
    sms_enabled: bool = False
    sms_phone: str | None = None
    min_confidence: int = Field(default=75, ge=0, le=100)
    quiet_hours_start: str | None = None  # "HH:MM", interpreted in UTC
    quiet_hours_end: str | None = None
    digest_mode: bool = False

class TradeLogCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: str = Field(default='long')
    shares: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    entry_at: str
    signal_id: int | None = None

class TradeLogClose(BaseModel):
    exit_price: Decimal = Field(gt=0)
    exit_at: str


# ============================================
# Helpers
# ============================================

def parse_hhmm(value: str) -> time_type:
    try:
        h, m = value.split(':')
        return time_type(int(h), int(m))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid time '{value}' — expected HH:MM.")

def parse_iso_datetime(value: str, field_name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} — expected an ISO 8601 timestamp.")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

def get_or_create_alert_preference(user_id: int, db: Session) -> AlertPreference:
    pref = db.query(AlertPreference).filter(AlertPreference.user_id == user_id).first()
    if pref:
        return pref
    pref = AlertPreference(user_id=user_id)
    db.add(pref)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent first-time calls (two tabs opening /preferences, or
        # two watchlist matches for the same user firing in the same
        # /generate pass) can both reach here and both try to insert — the
        # unique constraint on user_id rejects the loser. Same pattern as
        # get_or_create_scheduling_settings in app/main.py.
        db.rollback()
        pref = db.query(AlertPreference).filter(AlertPreference.user_id == user_id).first()
        if not pref:
            raise
        return pref
    db.refresh(pref)
    return pref

def serialize_signal(s: Signal) -> dict:
    return {
        'id': s.id,
        'symbol': s.symbol,
        'timeframe': s.timeframe,
        'signal_type': s.signal_type,
        'confidence': s.confidence,
        'entry_price': float(s.entry_price),
        'target_price': float(s.target_price) if s.target_price is not None else None,
        'stop_loss': float(s.stop_loss) if s.stop_loss is not None else None,
        'risk_reward_ratio': float(s.risk_reward_ratio) if s.risk_reward_ratio is not None else None,
        'reason': s.reason,
        'explanation': s.explanation or [],
        'market_condition': s.market_condition,
        'outcome': s.outcome,
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'disclaimer': DISCLAIMER,
    }

def serialize_trade(t: TradeLog) -> dict:
    return {
        'id': t.id,
        'signal_id': t.signal_id,
        'symbol': t.symbol,
        'side': t.side,
        'shares': float(t.shares),
        'entry_price': float(t.entry_price),
        'entry_at': t.entry_at.isoformat() if t.entry_at else None,
        'exit_price': float(t.exit_price) if t.exit_price is not None else None,
        'exit_at': t.exit_at.isoformat() if t.exit_at else None,
        'status': t.status,
        'pnl': float(t.pnl) if t.pnl is not None else None,
        'roi_pct': float(t.roi_pct) if t.roi_pct is not None else None,
        'created_at': t.created_at.isoformat() if t.created_at else None,
    }

def is_market_hours_now() -> bool:
    now_et = datetime.now(ZoneInfo('America/New_York'))
    if now_et.weekday() >= 5:  # Saturday/Sunday
        return False
    open_time, close_time = time_type(9, 30), time_type(16, 0)
    return open_time <= now_et.time() <= close_time

def resolve_pending_signal_outcomes(db: Session) -> None:
    """Checks every unresolved past signal against the current price and
    marks it target_hit / stop_hit / expired. This is what makes the
    success-rate report real (computed from what actually happened
    afterward) instead of a guess — it costs one cheap quote call per
    distinct pending symbol, not a full OHLCV fetch."""
    pending = db.query(Signal).filter(Signal.outcome == 'pending').all()
    if not pending:
        return

    now = datetime.utcnow()
    expiry_cutoff = now - timedelta(days=SIGNAL_OUTCOME_EXPIRY_DAYS)
    quote_cache: dict[str, float | None] = {}

    for signal in pending:
        if signal.symbol not in quote_cache:
            quote_cache[signal.symbol] = fetch_quote(signal.symbol)
        price = quote_cache[signal.symbol]

        outcome = None
        # target_price/stop_loss are nullable at the DB level; a row missing
        # either can't be evaluated against price, but should still be
        # eligible to expire rather than wedge this loop for every symbol.
        if price is not None and signal.target_price is not None and signal.stop_loss is not None:
            target, stop = float(signal.target_price), float(signal.stop_loss)
            if signal.signal_type == 'buy':
                if price >= target:
                    outcome = 'target_hit'
                elif price <= stop:
                    outcome = 'stop_hit'
            else:
                if price <= target:
                    outcome = 'target_hit'
                elif price >= stop:
                    outcome = 'stop_hit'

        if outcome is None and signal.created_at and signal.created_at <= expiry_cutoff:
            outcome = 'expired'

        if outcome:
            signal.outcome = outcome
            signal.outcome_at = now

    db.commit()

def is_quiet_hours(pref: AlertPreference, now_utc: datetime) -> bool:
    if not pref.quiet_hours_start or not pref.quiet_hours_end:
        return False
    current = now_utc.time()
    start, end = pref.quiet_hours_start, pref.quiet_hours_end
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end  # wraps past midnight

def send_email_alert(to_email: str, signal: Signal) -> bool:
    if not SENDGRID_API_KEY:
        return False
    subject = f"{signal.symbol} {signal.signal_type.upper()} signal ({signal.confidence}% confidence)"
    html = f"""
        <p><strong>{signal.symbol}</strong> — {signal.signal_type.upper()} signal, {signal.confidence}% confidence</p>
        <p>Entry: ${signal.entry_price} &nbsp; Target: ${signal.target_price} &nbsp; Stop Loss: ${signal.stop_loss}</p>
        <p>Reason: {signal.reason}</p>
        <p style="color:#888;font-size:12px;">{DISCLAIMER}</p>
    """
    try:
        resp = requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={'Authorization': f'Bearer {SENDGRID_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'personalizations': [{'to': [{'email': to_email}]}],
                'from': {'email': SENDGRID_FROM_EMAIL},
                'subject': subject,
                'content': [{'type': 'text/html', 'value': html}],
            },
            timeout=10,
        )
        return resp.status_code < 300
    except Exception:
        return False

def send_sms_alert(to_phone: str, signal: Signal) -> bool:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER):
        return False
    body = (
        f"{signal.symbol} {signal.signal_type.upper()} @ ${signal.entry_price} "
        f"({signal.confidence}% conf). Target ${signal.target_price}, stop ${signal.stop_loss}. "
        f"Not investment advice."
    )
    try:
        resp = requests.post(
            f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json',
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={'To': to_phone, 'From': TWILIO_PHONE_NUMBER, 'Body': body},
            timeout=10,
        )
        return resp.status_code < 300
    except Exception:
        return False


# ============================================
# Status
# ============================================

@router.get("/status")
async def get_trading_status(user_id: int = Depends(get_current_user_id)):
    return {
        'market_data_configured': is_market_data_configured(),
        'email_configured': bool(SENDGRID_API_KEY),
        'sms_configured': bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER),
        'disclaimer': DISCLAIMER,
    }


# ============================================
# Watchlist
# ============================================

@router.get("/watchlist")
async def list_watchlist(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    entries = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id, WatchlistEntry.is_active == True
    ).order_by(WatchlistEntry.created_at.desc()).all()
    return {
        'entries': [
            {'id': e.id, 'symbol': e.symbol, 'timeframe': e.timeframe, 'strategy_type': e.strategy_type,
             'created_at': e.created_at.isoformat() if e.created_at else None}
            for e in entries
        ]
    }

@router.post("/watchlist")
async def add_watchlist_entry(
    payload: WatchlistEntryCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)
):
    symbol = payload.symbol.strip().upper()
    if not symbol.isalnum():
        raise HTTPException(status_code=400, detail="Symbol may only contain letters and numbers.")
    if payload.timeframe not in VALID_TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"timeframe must be one of {sorted(VALID_TIMEFRAMES)}.")
    if payload.strategy_type not in VALID_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"strategy_type must be one of {sorted(VALID_STRATEGIES)}.")

    current_count = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id, WatchlistEntry.is_active == True
    ).count()
    if current_count >= MAX_WATCHLIST_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"Watchlist limit reached ({MAX_WATCHLIST_SYMBOLS} symbols).")

    existing = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id, WatchlistEntry.symbol == symbol,
        WatchlistEntry.timeframe == payload.timeframe, WatchlistEntry.is_active == True
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"{symbol} ({payload.timeframe}) is already on your watchlist.")

    entry = WatchlistEntry(
        user_id=user_id, symbol=symbol, timeframe=payload.timeframe, strategy_type=payload.strategy_type
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {'id': entry.id, 'symbol': entry.symbol, 'timeframe': entry.timeframe, 'strategy_type': entry.strategy_type}

@router.delete("/watchlist/{entry_id}")
async def remove_watchlist_entry(entry_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    entry = db.query(WatchlistEntry).filter(WatchlistEntry.id == entry_id, WatchlistEntry.user_id == user_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Watchlist entry not found.")
    db.delete(entry)
    db.commit()
    return {'status': 'success'}


# ============================================
# Alert preferences
# ============================================

@router.get("/preferences")
async def get_preferences(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    pref = get_or_create_alert_preference(user_id, db)
    return {
        'email_enabled': pref.email_enabled,
        'sms_enabled': pref.sms_enabled,
        'sms_phone': pref.sms_phone,
        'min_confidence': pref.min_confidence,
        'quiet_hours_start': pref.quiet_hours_start.strftime('%H:%M') if pref.quiet_hours_start else None,
        'quiet_hours_end': pref.quiet_hours_end.strftime('%H:%M') if pref.quiet_hours_end else None,
        'digest_mode': pref.digest_mode,
    }

@router.put("/preferences")
async def update_preferences(
    payload: AlertPreferenceUpdate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)
):
    pref = get_or_create_alert_preference(user_id, db)
    pref.email_enabled = payload.email_enabled
    pref.sms_enabled = payload.sms_enabled
    pref.sms_phone = payload.sms_phone
    pref.min_confidence = payload.min_confidence
    pref.quiet_hours_start = parse_hhmm(payload.quiet_hours_start) if payload.quiet_hours_start else None
    pref.quiet_hours_end = parse_hhmm(payload.quiet_hours_end) if payload.quiet_hours_end else None
    pref.digest_mode = payload.digest_mode
    db.commit()
    return {'status': 'success'}


# ============================================
# Signals
# ============================================

@router.get("/signals")
async def list_current_signals(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    watchlist = db.query(WatchlistEntry).filter(
        WatchlistEntry.user_id == user_id, WatchlistEntry.is_active == True
    ).all()
    if not watchlist:
        return {'signals': [], 'unseen_count': 0}

    pairs = {(w.symbol, w.timeframe) for w in watchlist}
    cutoff = datetime.utcnow() - timedelta(hours=SIGNAL_DEDUP_WINDOW_HOURS)
    recent = db.query(Signal).filter(Signal.created_at >= cutoff).order_by(Signal.created_at.desc()).all()
    signals = [s for s in recent if (s.symbol, s.timeframe) in pairs]

    pref = get_or_create_alert_preference(user_id, db)
    unseen_count = len(signals) if not pref.last_viewed_signals_at else len(
        [s for s in signals if s.created_at > pref.last_viewed_signals_at]
    )
    return {'signals': [serialize_signal(s) for s in signals], 'unseen_count': unseen_count}

@router.post("/signals/mark-viewed")
async def mark_signals_viewed(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    pref = get_or_create_alert_preference(user_id, db)
    pref.last_viewed_signals_at = datetime.utcnow()
    db.commit()
    return {'status': 'success'}

@router.get("/signals/history")
async def list_signal_history(days: int = 30, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    watchlist = db.query(WatchlistEntry).filter(WatchlistEntry.user_id == user_id).all()
    if not watchlist:
        return {'signals': []}
    pairs = {(w.symbol, w.timeframe) for w in watchlist}
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = db.query(Signal).filter(Signal.created_at >= cutoff).order_by(Signal.created_at.desc()).all()
    signals = [s for s in rows if (s.symbol, s.timeframe) in pairs]
    return {'signals': [serialize_signal(s) for s in signals]}

@router.get("/signals/{signal_id}")
async def get_signal_detail(signal_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    # Signals carry no personal/confidential data (they're derived purely
    # from public market data and identical for anyone watching the same
    # symbol/timeframe), so any authenticated user may look one up by id —
    # unlike everything else in this app, there's no per-user ownership to
    # scope this to.
    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")
    return serialize_signal(signal)


# ============================================
# Trade logging & performance
# ============================================

@router.post("/trades")
async def log_trade(payload: TradeLogCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if payload.side not in VALID_SIDES:
        raise HTTPException(status_code=400, detail=f"side must be one of {sorted(VALID_SIDES)}.")
    if payload.signal_id is not None and not db.query(Signal).filter(Signal.id == payload.signal_id).first():
        raise HTTPException(status_code=404, detail="Referenced signal not found.")

    trade = TradeLog(
        user_id=user_id,
        signal_id=payload.signal_id,
        symbol=payload.symbol.strip().upper(),
        side=payload.side,
        shares=payload.shares,
        entry_price=payload.entry_price,
        entry_at=parse_iso_datetime(payload.entry_at, 'entry_at'),
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return serialize_trade(trade)

@router.get("/trades")
async def list_trades(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    trades = db.query(TradeLog).filter(TradeLog.user_id == user_id).order_by(TradeLog.entry_at.desc()).all()
    return {'trades': [serialize_trade(t) for t in trades]}

@router.post("/trades/{trade_id}/close")
async def close_trade(
    trade_id: int, payload: TradeLogClose, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)
):
    trade = db.query(TradeLog).filter(TradeLog.id == trade_id, TradeLog.user_id == user_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    if trade.status == 'closed':
        raise HTTPException(status_code=400, detail="This trade is already closed.")

    trade.exit_price = payload.exit_price
    trade.exit_at = parse_iso_datetime(payload.exit_at, 'exit_at')
    trade.status = 'closed'

    if trade.side == 'long':
        trade.pnl = (trade.exit_price - trade.entry_price) * trade.shares
    else:
        trade.pnl = (trade.entry_price - trade.exit_price) * trade.shares
    cost_basis = trade.entry_price * trade.shares
    trade.roi_pct = (trade.pnl / cost_basis * 100) if cost_basis > 0 else Decimal('0')

    db.commit()
    db.refresh(trade)
    return serialize_trade(trade)

@router.get("/performance")
async def get_performance(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    trades = db.query(TradeLog).filter(TradeLog.user_id == user_id).all()
    closed = [t for t in trades if t.status == 'closed']
    open_trades = [t for t in trades if t.status == 'open']

    wins = [t for t in closed if t.pnl is not None and t.pnl > 0]
    losses = [t for t in closed if t.pnl is not None and t.pnl <= 0]
    total_pnl = sum((t.pnl for t in closed if t.pnl is not None), Decimal('0'))
    avg_roi = (sum((t.roi_pct for t in closed if t.roi_pct is not None), Decimal('0')) / len(closed)) if closed else None

    return {
        'total_trades': len(trades),
        'open_trades': len(open_trades),
        'closed_trades': len(closed),
        'win_rate': round(len(wins) / len(closed) * 100, 1) if closed else None,
        'total_pnl': float(total_pnl),
        'avg_roi_pct': float(avg_roi) if avg_roi is not None else None,
        'avg_win': float(sum((t.pnl for t in wins), Decimal('0')) / len(wins)) if wins else None,
        'avg_loss': float(sum((t.pnl for t in losses), Decimal('0')) / len(losses)) if losses else None,
        'best_trade': float(max((t.pnl for t in closed if t.pnl is not None), default=0)) if closed else None,
        'worst_trade': float(min((t.pnl for t in closed if t.pnl is not None), default=0)) if closed else None,
    }

@router.get("/success-rate")
async def get_success_rate(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """An honest, empirically-computed historical hit rate — NOT a
    per-signal probability of success. Confidence (in each signal's own
    payload) measures how many indicators agreed; this measures what
    actually happened afterward to past signals, system-wide, bucketed by
    that same confidence score. Small sample sizes are flagged rather than
    hidden behind a single misleading percentage."""
    signals = db.query(Signal).filter(Signal.outcome.in_(['target_hit', 'stop_hit', 'expired'])).all()

    def tier_stats(lo: int, hi: int) -> dict:
        tier_signals = [s for s in signals if lo <= s.confidence <= hi]
        resolved = [s for s in tier_signals if s.outcome in ('target_hit', 'stop_hit')]
        hits = [s for s in resolved if s.outcome == 'target_hit']
        return {
            'confidence_range': f'{lo}-{hi}%',
            'sample_size': len(resolved),
            'hit_rate': round(len(hits) / len(resolved) * 100, 1) if resolved else None,
            'expired_count': len(tier_signals) - len(resolved),
            '_hits': len(hits),
        }

    tiers = [tier_stats(lo, hi) for lo, hi in CONFIDENCE_TIERS]
    # Derived as a sum of the tiers (rather than independently filtered from
    # `signals`) so `overall` can never drift from the tier breakdown even if
    # SIGNAL_CONFIDENCE_THRESHOLD is ever changed without updating
    # CONFIDENCE_TIERS to match — a signal outside every tier's range simply
    # doesn't count anywhere, instead of inflating `overall` invisibly.
    overall_resolved = sum(t['sample_size'] for t in tiers)
    overall_hits = sum(t.pop('_hits') for t in tiers)

    return {
        'overall': {
            'sample_size': overall_resolved,
            'hit_rate': round(overall_hits / overall_resolved * 100, 1) if overall_resolved else None,
        },
        'by_confidence_tier': tiers,
        'disclaimer': (
            "Historical results across all past signals system-wide — not a prediction for any "
            "individual future signal. Small sample sizes are not statistically reliable."
        ),
    }


# ============================================
# Signal generation job (Cloud Scheduler -> here every 5 min)
# ============================================

@router.post("/generate")
async def generate_signals(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    if not CRON_SECRET:
        raise HTTPException(status_code=503, detail="Signal generation is not configured on this server yet.")
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="Invalid cron secret.")

    if not is_market_hours_now():
        return {'status': 'skipped', 'reason': 'outside market hours (9:30-16:00 ET, Mon-Fri)'}
    if not is_market_data_configured():
        return {'status': 'skipped', 'reason': 'market data provider not configured'}

    resolve_pending_signal_outcomes(db)

    pairs = db.query(WatchlistEntry.symbol, WatchlistEntry.timeframe).filter(
        WatchlistEntry.is_active == True
    ).distinct().all()

    symbols_checked = 0
    signals_generated = 0
    alerts_sent = 0
    now_utc = datetime.utcnow()
    cutoff = now_utc - timedelta(hours=SIGNAL_DEDUP_WINDOW_HOURS)

    for symbol, timeframe in pairs:
        symbols_checked += 1

        already_signaled = db.query(Signal).filter(
            Signal.symbol == symbol, Signal.timeframe == timeframe, Signal.created_at >= cutoff
        ).first()
        if already_signaled:
            continue

        ohlcv = fetch_ohlcv(symbol, timeframe, count=100)
        if not ohlcv or len(ohlcv['closes']) < 30:
            continue
        # A flaky/partial Finnhub response could return mismatched-length
        # arrays (e.g. highs shorter than closes) — the indicator functions
        # index across all four in lockstep and would raise IndexError, so
        # skip this one symbol rather than let it happen.
        lengths = {len(ohlcv['highs']), len(ohlcv['lows']), len(ohlcv['closes']), len(ohlcv['volumes'])}
        if len(lengths) != 1:
            continue

        try:
            result = score_signal(ohlcv['highs'], ohlcv['lows'], ohlcv['closes'], ohlcv['volumes'])
            if not result:
                continue

            signal = Signal(symbol=symbol, timeframe=timeframe, **result)
            db.add(signal)
            db.commit()
            db.refresh(signal)
            signals_generated += 1

            watchers = db.query(WatchlistEntry).filter(
                WatchlistEntry.symbol == symbol, WatchlistEntry.timeframe == timeframe,
                WatchlistEntry.is_active == True
            ).all()
            for watcher in watchers:
                pref = get_or_create_alert_preference(watcher.user_id, db)
                if signal.confidence < pref.min_confidence or pref.digest_mode:
                    continue
                if is_quiet_hours(pref, now_utc):
                    continue

                user = db.query(User).filter(User.id == watcher.user_id).first()
                if not user:
                    continue

                if pref.email_enabled:
                    sent = send_email_alert(user.email, signal)
                    db.add(SignalAlert(
                        user_id=watcher.user_id, signal_id=signal.id, delivery_method='email',
                        status='sent' if sent else ('failed' if SENDGRID_API_KEY else 'skipped')
                    ))
                    if sent:
                        alerts_sent += 1

                if pref.sms_enabled and pref.sms_phone:
                    sent = send_sms_alert(pref.sms_phone, signal)
                    db.add(SignalAlert(
                        user_id=watcher.user_id, signal_id=signal.id, delivery_method='sms',
                        status='sent' if sent else ('failed' if TWILIO_ACCOUNT_SID else 'skipped')
                    ))
                    if sent:
                        alerts_sent += 1
                db.commit()
        except Exception:
            # One symbol's bad data or an alert-dispatch failure shouldn't
            # abort every other symbol still waiting in this run.
            db.rollback()
            continue

    return {'status': 'success', 'symbols_checked': symbols_checked, 'signals_generated': signals_generated, 'alerts_sent': alerts_sent}
