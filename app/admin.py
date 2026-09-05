# Administrative Portal — owner-only. Every endpoint here requires
# is_admin=True on the caller's own User row; there is no self-service way
# to become an admin (that would be a serious security hole), so the
# platform owner bootstraps their own account with a one-time direct SQL
# UPDATE after this ships (see setup instructions).

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.auth import get_current_user_id
from app.models import (
    User, ConsumerAccount, BusinessAccount, PortalClient, PortalDocument,
    SchedulingSettings, Booking, CalendarConnection, WatchlistEntry, Signal,
    Vendor, Bill, Invoice,
)

router = APIRouter(prefix="/api/admin")


def get_current_admin_user_id(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)) -> int:
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user_id


# --- User directory ---

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

@router.get("/users")
async def list_users(
    search: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    admin_id: int = Depends(get_current_admin_user_id),
    db: Session = Depends(get_db),
):
    page = max(1, page)
    page_size = min(max(1, page_size), MAX_PAGE_SIZE)

    query = db.query(User)
    if search:
        like = f"%{search}%"
        query = query.filter(
            (User.email.ilike(like)) | (User.first_name.ilike(like)) | (User.last_name.ilike(like))
        )
    total = query.count()
    users = query.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    user_ids = [u.id for u in users]
    counts_by_model = {
        'vendors': Vendor, 'invoices': Invoice, 'portal_clients': PortalClient,
        'bookings': Booking, 'watchlist_entries': WatchlistEntry,
    }
    usage = {uid: {} for uid in user_ids}
    if user_ids:
        for label, model in counts_by_model.items():
            rows = (
                db.query(model.user_id, func.count(model.id))
                .filter(model.user_id.in_(user_ids))
                .group_by(model.user_id)
                .all()
            )
            counts = dict(rows)
            for uid in user_ids:
                usage[uid][label] = counts.get(uid, 0)

    return {
        'users': [
            {
                'id': u.id,
                'email': u.email,
                'first_name': u.first_name,
                'last_name': u.last_name,
                'account_type': u.account_type,
                'is_active': u.is_active,
                'is_admin': u.is_admin,
                'is_comped': u.is_comped,
                'created_at': u.created_at.isoformat() if u.created_at else None,
                'usage': usage.get(u.id, {}),
            }
            for u in users
        ],
        'total': total,
        'page': page,
        'page_size': page_size,
    }

@router.post("/users/{target_user_id}/comp")
async def set_comped(
    target_user_id: int, comped: bool,
    admin_id: int = Depends(get_current_admin_user_id),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == target_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    user.is_comped = comped
    db.commit()
    return {'status': 'success', 'user_id': user.id, 'is_comped': user.is_comped}


# --- KPIs (first pass — built entirely from data already in the DB) ---

@router.get("/kpis")
async def get_kpis(admin_id: int = Depends(get_current_admin_user_id), db: Session = Depends(get_db)):
    total_users = db.query(func.count(User.id)).scalar()
    comped_users = db.query(func.count(User.id)).filter(User.is_comped.is_(True)).scalar()

    by_account_type = dict(
        db.query(User.account_type, func.count(User.id)).group_by(User.account_type).all()
    )

    cutoff = datetime.utcnow() - timedelta(days=30)
    signups_by_day_rows = (
        db.query(func.date(User.created_at), func.count(User.id))
        .filter(User.created_at >= cutoff)
        .group_by(func.date(User.created_at))
        .order_by(func.date(User.created_at))
        .all()
    )
    signups_by_day = [{'date': str(d), 'count': c} for d, c in signups_by_day_rows]

    tool_counts = {
        'consumer_accounts': db.query(func.count(ConsumerAccount.id)).scalar(),
        'business_accounts': db.query(func.count(BusinessAccount.id)).scalar(),
        'portal_clients': db.query(func.count(PortalClient.id)).scalar(),
        'portal_documents': db.query(func.count(PortalDocument.id)).scalar(),
        'scheduling_pages': db.query(func.count(SchedulingSettings.id)).scalar(),
        'bookings': db.query(func.count(Booking.id)).scalar(),
        'calendar_connections': db.query(func.count(CalendarConnection.id)).scalar(),
        'trading_watchlist_entries': db.query(func.count(WatchlistEntry.id)).scalar(),
        'trading_signals_generated': db.query(func.count(Signal.id)).scalar(),
        'vendors': db.query(func.count(Vendor.id)).scalar(),
        'bills': db.query(func.count(Bill.id)).scalar(),
        'invoices': db.query(func.count(Invoice.id)).scalar(),
    }

    return {
        'total_users': total_users,
        'comped_users': comped_users,
        'users_by_account_type': by_account_type,
        'signups_last_30_days': signups_by_day,
        'tool_usage_counts': tool_counts,
    }


# --- System status (which optional integrations are actually configured) ---

@router.get("/system-status")
async def get_system_status(admin_id: int = Depends(get_current_admin_user_id)):
    # Presence only — never echo the actual secret values back over the API.
    return {
        'integrations': {
            'finnhub_market_data': bool(os.environ.get('FINNHUB_API_KEY')),
            'sendgrid_email': bool(os.environ.get('SENDGRID_API_KEY')),
            'twilio_sms': bool(os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN')),
            'google_calendar_oauth': bool(os.environ.get('GOOGLE_CALENDAR_CLIENT_ID') and os.environ.get('GOOGLE_CALENDAR_CLIENT_SECRET')),
            'microsoft_calendar_oauth': bool(os.environ.get('MICROSOFT_CALENDAR_CLIENT_ID') and os.environ.get('MICROSOFT_CALENDAR_CLIENT_SECRET')),
            'trading_cron_secret': bool(os.environ.get('CRON_SECRET')),
            'calendar_token_encryption': bool(os.environ.get('CALENDAR_TOKEN_ENCRYPTION_KEY')),
        },
    }
