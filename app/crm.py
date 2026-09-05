# CRM & Sales Pipeline — benchmarked against Salesforce, HubSpot CRM, and
# Pipedrive: a visual pipeline of deals grouped by stage, an activity/notes
# timeline per deal, and pipeline reporting (value by stage, win rate).
#
# Reuses PortalClient as the contact record (same as Invoicing) rather than
# a third parallel Customer model — one contact list across Client Portal,
# Invoicing, and the CRM.
#
# Differentiator none of the three competitors above can offer: a won deal
# converts to a real Invoicing invoice in one click, because this platform
# already owns both. Two point tools can't do that; they'd need a third-
# party integration (e.g. a HubSpot-QuickBooks connector) to approximate it.

import secrets
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import Deal, DealNote, PortalClient, Invoice, InvoiceLineItem
from app.auth import get_current_user_id
from app.invoicing import next_invoice_number

router = APIRouter(prefix="/api/crm")

OPEN_STAGES = ['lead', 'qualified', 'proposal', 'negotiation']
CLOSED_STAGES = ['won', 'lost']
VALID_STAGES = OPEN_STAGES + CLOSED_STAGES

# Matches Deal.value / InvoiceLineItem.unit_price -> Numeric(12, 2): 10
# digits before the decimal point.
MAX_DEAL_VALUE = Decimal('9999999999.99')


def _non_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError('must not be blank')
    return value


# --- Pydantic payloads ---

class DealCreate(BaseModel):
    client_id: int
    title: str = Field(..., min_length=1, max_length=255)
    value: Decimal = Field(default=Decimal(0), ge=0, le=MAX_DEAL_VALUE)
    expected_close_date: datetime | None = None
    notes: str | None = None

    _check_title = field_validator('title')(_non_blank)

class DealUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    value: Decimal | None = Field(None, ge=0, le=MAX_DEAL_VALUE)
    expected_close_date: datetime | None = None
    notes: str | None = None

    _check_title = field_validator('title')(_non_blank)

class StageUpdate(BaseModel):
    stage: str

class NoteCreate(BaseModel):
    body: str = Field(..., min_length=1)

    _check_body = field_validator('body')(_non_blank)


# --- Ownership helpers ---

def get_owned_deal(deal_id: int, user_id: int, db: Session) -> Deal:
    deal = db.query(Deal).filter(Deal.id == deal_id, Deal.user_id == user_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return deal

def get_owned_client(client_id: int, user_id: int, db: Session) -> PortalClient:
    client = db.query(PortalClient).filter(PortalClient.id == client_id, PortalClient.user_id == user_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found.")
    return client

def serialize_deal(deal: Deal, client: PortalClient | None = None) -> dict:
    return {
        'id': deal.id,
        'client_id': deal.client_id,
        'client_name': client.name if client else None,
        'client_email': client.email if client else None,
        'title': deal.title,
        'value': float(deal.value),
        'stage': deal.stage,
        'expected_close_date': deal.expected_close_date.isoformat() if deal.expected_close_date else None,
        'notes': deal.notes,
        'closed_at': deal.closed_at.isoformat() if deal.closed_at else None,
        'invoice_id': deal.invoice_id,
        'created_at': deal.created_at.isoformat() if deal.created_at else None,
    }

def serialize_note(note: DealNote) -> dict:
    return {
        'id': note.id,
        'deal_id': note.deal_id,
        'body': note.body,
        'created_at': note.created_at.isoformat() if note.created_at else None,
    }


# --- Deals ---

@router.post("/deals")
async def create_deal(payload: DealCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    client = get_owned_client(payload.client_id, user_id, db)
    deal = Deal(
        user_id=user_id, client_id=payload.client_id, title=payload.title,
        value=payload.value, expected_close_date=payload.expected_close_date,
        notes=payload.notes,
    )
    db.add(deal)
    db.commit()
    db.refresh(deal)
    return serialize_deal(deal, client)

@router.get("/deals")
async def list_deals(stage: str | None = None, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    query = db.query(Deal).filter(Deal.user_id == user_id)
    if stage:
        query = query.filter(Deal.stage == stage)
    deals = query.order_by(Deal.created_at.desc()).all()

    clients = {c.id: c for c in db.query(PortalClient).filter(PortalClient.id.in_([d.client_id for d in deals])).all()} if deals else {}
    return {'deals': [serialize_deal(d, clients.get(d.client_id)) for d in deals]}

@router.get("/deals/{deal_id}")
async def get_deal(deal_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    deal = get_owned_deal(deal_id, user_id, db)
    client = db.query(PortalClient).filter(PortalClient.id == deal.client_id).first()
    notes = db.query(DealNote).filter(DealNote.deal_id == deal.id).order_by(DealNote.created_at.desc()).all()
    result = serialize_deal(deal, client)
    result['activity'] = [serialize_note(n) for n in notes]
    return result

@router.put("/deals/{deal_id}")
async def update_deal(deal_id: int, payload: DealUpdate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    deal = get_owned_deal(deal_id, user_id, db)
    for field in ('title', 'value', 'expected_close_date', 'notes'):
        value = getattr(payload, field)
        if value is not None:
            setattr(deal, field, value)
    db.commit()
    db.refresh(deal)
    client = db.query(PortalClient).filter(PortalClient.id == deal.client_id).first()
    return serialize_deal(deal, client)

@router.delete("/deals/{deal_id}")
async def delete_deal(deal_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    deal = get_owned_deal(deal_id, user_id, db)
    db.query(DealNote).filter(DealNote.deal_id == deal.id).delete()
    db.delete(deal)
    db.commit()
    return {'status': 'success'}

@router.post("/deals/{deal_id}/stage")
async def update_stage(deal_id: int, payload: StageUpdate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if payload.stage not in VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of {VALID_STAGES}")
    deal = get_owned_deal(deal_id, user_id, db)
    deal.stage = payload.stage
    if payload.stage in CLOSED_STAGES:
        if deal.closed_at is None:
            deal.closed_at = datetime.utcnow()
    else:
        deal.closed_at = None  # reopening a previously closed deal
    db.commit()
    db.refresh(deal)
    client = db.query(PortalClient).filter(PortalClient.id == deal.client_id).first()
    return serialize_deal(deal, client)


# --- Activity / notes ---

@router.post("/deals/{deal_id}/notes")
async def add_note(deal_id: int, payload: NoteCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    deal = get_owned_deal(deal_id, user_id, db)
    note = DealNote(deal_id=deal.id, body=payload.body)
    db.add(note)
    db.commit()
    db.refresh(note)
    return serialize_note(note)


# --- Deal -> Invoice conversion (the differentiator) ---

@router.post("/deals/{deal_id}/convert-to-invoice")
async def convert_to_invoice(deal_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    deal = get_owned_deal(deal_id, user_id, db)
    if deal.stage != 'won':
        raise HTTPException(status_code=400, detail="Only a deal marked 'won' can be converted to an invoice.")
    if deal.invoice_id is not None:
        raise HTTPException(status_code=400, detail="This deal has already been converted to an invoice.")

    now = datetime.utcnow()
    # Mirrors create_invoice() in app/invoicing.py: next_invoice_number()
    # is read-then-write with no lock, so retry once against the
    # (user_id, invoice_number) unique constraint rather than surfacing a
    # raw 500 for what's a rare, recoverable race.
    for attempt in range(2):
        invoice = Invoice(
            user_id=user_id, client_id=deal.client_id,
            invoice_number=next_invoice_number(user_id, db),
            issue_date=now, due_date=now,
            tax_rate=0, notes=f"Generated from CRM deal: {deal.title}",
            public_token=secrets.token_urlsafe(32),
        )
        db.add(invoice)
        try:
            db.flush()
            break
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise HTTPException(status_code=409, detail="Could not assign an invoice number — please try again.")

    db.add(InvoiceLineItem(invoice_id=invoice.id, description=deal.title, quantity=1, unit_price=deal.value, sort_order=0))
    db.flush()

    # Atomic claim: the UPDATE's WHERE clause re-checks invoice_id IS NULL
    # at the database level, so two concurrent requests can't both "win" —
    # only one UPDATE can match the row. The check above is just a fast
    # path; this is what actually prevents a double conversion. No
    # SELECT ... FOR UPDATE is needed, so nothing holds a row lock across
    # the invoice_number lookup or the retry loop above.
    claimed = db.query(Deal).filter(
        Deal.id == deal_id, Deal.user_id == user_id, Deal.invoice_id.is_(None),
    ).update({'invoice_id': invoice.id}, synchronize_session=False)

    if claimed == 0:
        db.rollback()  # someone else converted this deal first — discard our invoice
        raise HTTPException(status_code=400, detail="This deal has already been converted to an invoice.")

    db.commit()
    db.refresh(invoice)
    return {'status': 'success', 'invoice_id': invoice.id, 'invoice_number': invoice.invoice_number}


# --- Pipeline summary / reporting ---

@router.get("/summary")
async def get_pipeline_summary(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    deals = db.query(Deal).filter(Deal.user_id == user_id).all()

    value_by_stage = {stage: Decimal(0) for stage in OPEN_STAGES}
    counts_by_stage = {stage: 0 for stage in VALID_STAGES}
    won_value = Decimal(0)

    for deal in deals:
        counts_by_stage[deal.stage] = counts_by_stage.get(deal.stage, 0) + 1
        if deal.stage in OPEN_STAGES:
            value_by_stage[deal.stage] += deal.value
        elif deal.stage == 'won':
            won_value += deal.value

    won_count = counts_by_stage.get('won', 0)
    lost_count = counts_by_stage.get('lost', 0)
    closed_count = won_count + lost_count
    win_rate = round(won_count / closed_count * 100, 1) if closed_count > 0 else None

    return {
        'pipeline_value_by_stage': {stage: float(v) for stage, v in value_by_stage.items()},
        'deal_counts_by_stage': counts_by_stage,
        'won_value': float(won_value),
        'win_rate': win_rate,
    }
