# Invoicing — professional invoices with line items, tax, partial payments,
# and a shareable public view link. Reuses PortalClient (from Client Portal)
# as the customer record rather than a second, parallel Customer model, so
# there's one client list across both tools instead of two that can drift
# out of sync.
#
# Like Bill in Vendor & AP, an invoice's status (draft/sent/paid/overdue/
# partial) is never stored — always derived from sent_at, the live sum of
# InvoicePayment rows, and due_date, so it can't drift out of sync with what
# actually happened.

import secrets
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models import Invoice, InvoiceLineItem, InvoicePayment, PortalClient, User
from app.auth import get_current_user_id

router = APIRouter(prefix="/api/invoicing")

VALID_PAYMENT_METHODS = {'check', 'ach', 'card', 'wire', 'cash', 'other'}
FIRST_INVOICE_NUMBER = 1001

AGING_BUCKETS = [
    ('current', None, 0),
    ('1-30', 1, 30),
    ('31-60', 31, 60),
    ('61-90', 61, 90),
    ('90+', 91, None),
]


# --- Pydantic payloads ---

class LineItemInput(BaseModel):
    description: str = Field(..., min_length=1)
    quantity: Decimal = Field(default=Decimal(1), gt=0)
    unit_price: Decimal = Field(..., ge=0)

class InvoiceCreate(BaseModel):
    client_id: int
    issue_date: datetime
    due_date: datetime
    tax_rate: Decimal = Field(default=Decimal(0), ge=0, le=100)
    notes: str | None = None
    line_items: list[LineItemInput] = Field(..., min_length=1)

class InvoiceUpdate(BaseModel):
    issue_date: datetime | None = None
    due_date: datetime | None = None
    tax_rate: Decimal | None = Field(None, ge=0, le=100)
    notes: str | None = None
    line_items: list[LineItemInput] | None = Field(None, min_length=1)

class PaymentCreate(BaseModel):
    amount: Decimal = Field(..., gt=0)
    payment_date: datetime
    payment_method: str = 'other'
    reference_number: str | None = None
    notes: str | None = None


# --- Ownership + derivation helpers ---

def get_owned_invoice(invoice_id: int, user_id: int, db: Session) -> Invoice:
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.user_id == user_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    return invoice

def get_owned_client(client_id: int, user_id: int, db: Session) -> PortalClient:
    client = db.query(PortalClient).filter(PortalClient.id == client_id, PortalClient.user_id == user_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found.")
    return client

def next_invoice_number(user_id: int, db: Session) -> str:
    """Scans every one of this user's invoice numbers (not just their most
    recent one) and picks max+1 — using only the last invoice would silently
    reset to INV-1001 the moment that one invoice's number doesn't parse
    (manual edit, future import), potentially minting a second, customer-
    facing INV-1001 for the same user. A DB-level unique constraint on
    (user_id, invoice_number) is the backstop if this ever collides anyway."""
    numbers = db.query(Invoice.invoice_number).filter(Invoice.user_id == user_id).all()
    highest = FIRST_INVOICE_NUMBER - 1
    for (number,) in numbers:
        try:
            n = int(number.rsplit('-', 1)[-1])
        except (ValueError, IndexError, AttributeError):
            continue
        highest = max(highest, n)
    return f'INV-{highest + 1}'

def line_items_subtotal(line_items: list[InvoiceLineItem]) -> Decimal:
    return sum((li.quantity * li.unit_price for li in line_items), Decimal(0))

def invoice_total(line_items: list[InvoiceLineItem], tax_rate: Decimal) -> Decimal:
    subtotal = line_items_subtotal(line_items)
    return subtotal * (1 + tax_rate / 100)

def paid_totals_by_invoice(invoice_ids: list[int], db: Session) -> dict[int, Decimal]:
    if not invoice_ids:
        return {}
    rows = (
        db.query(InvoicePayment.invoice_id, func.coalesce(func.sum(InvoicePayment.amount), 0))
        .filter(InvoicePayment.invoice_id.in_(invoice_ids))
        .group_by(InvoicePayment.invoice_id)
        .all()
    )
    return {invoice_id: Decimal(total) for invoice_id, total in rows}

def days_overdue(due_date: datetime, now: datetime) -> int:
    return (now.date() - due_date.date()).days

def derive_invoice_status(invoice: Invoice, total: Decimal, amount_paid: Decimal, now: datetime) -> str:
    if invoice.sent_at is None:
        return 'draft'
    if amount_paid >= total:
        return 'paid'
    if days_overdue(invoice.due_date, now) > 0:
        return 'overdue'
    if amount_paid > 0:
        return 'partial'
    return 'sent'

def serialize_line_item(li: InvoiceLineItem) -> dict:
    return {
        'id': li.id,
        'description': li.description,
        'quantity': float(li.quantity),
        'unit_price': float(li.unit_price),
        'amount': float(li.quantity * li.unit_price),
        'sort_order': li.sort_order,
    }

def serialize_payment(payment: InvoicePayment) -> dict:
    return {
        'id': payment.id,
        'invoice_id': payment.invoice_id,
        'amount': float(payment.amount),
        'payment_date': payment.payment_date.isoformat(),
        'payment_method': payment.payment_method,
        'reference_number': payment.reference_number,
        'notes': payment.notes,
        'created_at': payment.created_at.isoformat() if payment.created_at else None,
    }

def serialize_invoice(invoice: Invoice, line_items: list[InvoiceLineItem], amount_paid: Decimal, now: datetime, client: PortalClient | None = None) -> dict:
    subtotal = line_items_subtotal(line_items)
    total = invoice_total(line_items, invoice.tax_rate)
    status = derive_invoice_status(invoice, total, amount_paid, now)
    return {
        'id': invoice.id,
        'client_id': invoice.client_id,
        'client_name': client.name if client else None,
        'client_email': client.email if client else None,
        'invoice_number': invoice.invoice_number,
        'issue_date': invoice.issue_date.isoformat(),
        'due_date': invoice.due_date.isoformat(),
        'tax_rate': float(invoice.tax_rate),
        'notes': invoice.notes,
        'subtotal': float(subtotal),
        'total': float(total),
        'amount_paid': float(amount_paid),
        'balance': float(total - amount_paid),
        'status': status,
        'days_overdue': days_overdue(invoice.due_date, now) if status == 'overdue' else 0,
        'is_sent': invoice.sent_at is not None,
        'sent_at': invoice.sent_at.isoformat() if invoice.sent_at else None,
        'public_token': invoice.public_token,
        'line_items': [serialize_line_item(li) for li in line_items],
        'created_at': invoice.created_at.isoformat() if invoice.created_at else None,
    }

def replace_line_items(invoice_id: int, items: list[LineItemInput], db: Session) -> None:
    db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == invoice_id).delete()
    for i, item in enumerate(items):
        db.add(InvoiceLineItem(
            invoice_id=invoice_id, description=item.description,
            quantity=item.quantity, unit_price=item.unit_price, sort_order=i,
        ))


# --- Invoices ---

@router.post("/invoices")
async def create_invoice(payload: InvoiceCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    get_owned_client(payload.client_id, user_id, db)  # 404s if not this user's client

    # next_invoice_number() is read-then-write with no lock, so two
    # concurrent creates for the same user could both compute the same
    # number — the (user_id, invoice_number) unique constraint catches that
    # at insert time; retry once with a freshly recomputed number rather
    # than surfacing a raw 500 for what's a rare, recoverable race.
    for attempt in range(2):
        invoice = Invoice(
            user_id=user_id, client_id=payload.client_id,
            invoice_number=next_invoice_number(user_id, db),
            issue_date=payload.issue_date, due_date=payload.due_date,
            tax_rate=payload.tax_rate, notes=payload.notes,
            public_token=secrets.token_urlsafe(32),
        )
        db.add(invoice)
        try:
            db.flush()  # assign invoice.id before inserting line items
            break
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise HTTPException(status_code=409, detail="Could not assign an invoice number — please try again.")

    replace_line_items(invoice.id, payload.line_items, db)
    db.commit()
    db.refresh(invoice)

    line_items = db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == invoice.id).order_by(InvoiceLineItem.sort_order).all()
    return serialize_invoice(invoice, line_items, Decimal(0), datetime.utcnow())

@router.get("/invoices")
async def list_invoices(status: str | None = None, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    invoices = db.query(Invoice).filter(Invoice.user_id == user_id).order_by(Invoice.due_date.asc()).all()
    invoice_ids = [i.id for i in invoices]

    line_items_by_invoice: dict[int, list[InvoiceLineItem]] = {i: [] for i in invoice_ids}
    if invoice_ids:
        for li in db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id.in_(invoice_ids)).order_by(InvoiceLineItem.sort_order).all():
            line_items_by_invoice[li.invoice_id].append(li)

    paid_totals = paid_totals_by_invoice(invoice_ids, db)
    clients = {c.id: c for c in db.query(PortalClient).filter(PortalClient.id.in_([i.client_id for i in invoices])).all()} if invoices else {}
    now = datetime.utcnow()

    serialized = [
        serialize_invoice(inv, line_items_by_invoice[inv.id], paid_totals.get(inv.id, Decimal(0)), now, clients.get(inv.client_id))
        for inv in invoices
    ]
    if status is not None:
        serialized = [s for s in serialized if s['status'] == status]
    return {'invoices': serialized}

@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    invoice = get_owned_invoice(invoice_id, user_id, db)
    line_items = db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == invoice.id).order_by(InvoiceLineItem.sort_order).all()
    payments = db.query(InvoicePayment).filter(InvoicePayment.invoice_id == invoice.id).order_by(InvoicePayment.payment_date.desc()).all()
    amount_paid = sum((p.amount for p in payments), Decimal(0))
    client = db.query(PortalClient).filter(PortalClient.id == invoice.client_id).first()

    result = serialize_invoice(invoice, line_items, amount_paid, datetime.utcnow(), client)
    result['payments'] = [serialize_payment(p) for p in payments]
    return result

@router.put("/invoices/{invoice_id}")
async def update_invoice(invoice_id: int, payload: InvoiceUpdate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    invoice = get_owned_invoice(invoice_id, user_id, db)
    for field in ('issue_date', 'due_date', 'tax_rate', 'notes'):
        value = getattr(payload, field)
        if value is not None:
            setattr(invoice, field, value)
    if payload.line_items is not None:
        replace_line_items(invoice.id, payload.line_items, db)
    db.commit()
    db.refresh(invoice)

    line_items = db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == invoice.id).order_by(InvoiceLineItem.sort_order).all()
    payments = db.query(InvoicePayment).filter(InvoicePayment.invoice_id == invoice.id).all()
    amount_paid = sum((p.amount for p in payments), Decimal(0))
    client = db.query(PortalClient).filter(PortalClient.id == invoice.client_id).first()
    return serialize_invoice(invoice, line_items, amount_paid, datetime.utcnow(), client)

@router.delete("/invoices/{invoice_id}")
async def delete_invoice(invoice_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    invoice = get_owned_invoice(invoice_id, user_id, db)
    has_payments = db.query(InvoicePayment).filter(InvoicePayment.invoice_id == invoice.id).first() is not None
    if has_payments:
        raise HTTPException(status_code=400, detail="Cannot delete an invoice with recorded payments. Void the payments first.")
    db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == invoice.id).delete()
    db.delete(invoice)
    db.commit()
    return {'status': 'success'}

@router.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """Marks the invoice sent (idempotent — sending an already-sent invoice
    again just returns its existing public link) so the client's public view
    goes live and the status starts deriving as sent/overdue/etc. instead of
    draft."""
    invoice = get_owned_invoice(invoice_id, user_id, db)
    if invoice.sent_at is None:
        invoice.sent_at = datetime.utcnow()
        db.commit()
        db.refresh(invoice)
    return {'status': 'success', 'public_token': invoice.public_token, 'sent_at': invoice.sent_at.isoformat()}


# --- Payments ---

@router.post("/invoices/{invoice_id}/payments")
async def record_payment(invoice_id: int, payload: PaymentCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    invoice = get_owned_invoice(invoice_id, user_id, db)
    if payload.payment_method not in VALID_PAYMENT_METHODS:
        raise HTTPException(status_code=400, detail=f"payment_method must be one of {sorted(VALID_PAYMENT_METHODS)}")

    line_items = db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == invoice.id).all()
    total = invoice_total(line_items, invoice.tax_rate)

    # Read-then-check-then-insert, not row-locked — same accepted, low-risk
    # tradeoff documented in Vendor & AP's record_payment for a single owner
    # manually keying in payments through this UI.
    existing_paid = db.query(func.coalesce(func.sum(InvoicePayment.amount), 0)).filter(InvoicePayment.invoice_id == invoice.id).scalar()
    existing_paid = Decimal(existing_paid)
    remaining = total - existing_paid
    if payload.amount > remaining:
        raise HTTPException(
            status_code=400,
            detail=f"Payment of {payload.amount} exceeds the remaining balance of {remaining}.",
        )

    payment = InvoicePayment(
        invoice_id=invoice.id, amount=payload.amount, payment_date=payload.payment_date,
        payment_method=payload.payment_method, reference_number=payload.reference_number,
        notes=payload.notes,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return serialize_payment(payment)

@router.delete("/payments/{payment_id}")
async def void_payment(payment_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    payment = (
        db.query(InvoicePayment)
        .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .filter(InvoicePayment.id == payment_id, Invoice.user_id == user_id)
        .first()
    )
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found.")
    db.delete(payment)
    db.commit()
    return {'status': 'success'}


# --- Aging / summary ---

@router.get("/summary")
async def get_invoicing_summary(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    invoices = db.query(Invoice).filter(Invoice.user_id == user_id, Invoice.sent_at.isnot(None)).all()
    invoice_ids = [i.id for i in invoices]

    line_items_by_invoice: dict[int, list[InvoiceLineItem]] = {i: [] for i in invoice_ids}
    if invoice_ids:
        for li in db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id.in_(invoice_ids)).all():
            line_items_by_invoice[li.invoice_id].append(li)

    paid_totals = paid_totals_by_invoice(invoice_ids, db)
    now = datetime.utcnow()

    bucket_totals = {label: Decimal(0) for label, _, _ in AGING_BUCKETS}
    status_counts = {'sent': 0, 'partial': 0, 'overdue': 0, 'paid': 0}
    total_outstanding = Decimal(0)
    total_overdue = Decimal(0)

    for invoice in invoices:
        line_items = line_items_by_invoice[invoice.id]
        total = invoice_total(line_items, invoice.tax_rate)
        paid = paid_totals.get(invoice.id, Decimal(0))
        balance = total - paid
        status = derive_invoice_status(invoice, total, paid, now)
        status_counts[status] += 1

        if balance <= 0:
            continue

        total_outstanding += balance
        overdue = days_overdue(invoice.due_date, now)
        if overdue > 0:
            total_overdue += balance
        for label, lo, hi in AGING_BUCKETS:
            if lo is None:
                if overdue <= 0:
                    bucket_totals[label] += balance
                    break
            elif hi is None:
                if overdue >= lo:
                    bucket_totals[label] += balance
                    break
            elif lo <= overdue <= hi:
                bucket_totals[label] += balance
                break

    return {
        'total_outstanding': float(total_outstanding),
        'total_overdue': float(total_overdue),
        'invoice_counts': status_counts,
        'aging_buckets': [{'label': label, 'amount': float(bucket_totals[label])} for label, _, _ in AGING_BUCKETS],
    }


# --- Public (no auth) ---

@router.get("/public/{token}")
async def get_public_invoice(token: str, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.public_token == token, Invoice.sent_at.isnot(None)).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    line_items = db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == invoice.id).order_by(InvoiceLineItem.sort_order).all()
    payments = db.query(InvoicePayment).filter(InvoicePayment.invoice_id == invoice.id).all()
    amount_paid = sum((p.amount for p in payments), Decimal(0))
    client = db.query(PortalClient).filter(PortalClient.id == invoice.client_id).first()

    owner = db.query(User).filter(User.id == invoice.user_id).first()

    result = serialize_invoice(invoice, line_items, amount_paid, datetime.utcnow(), client)
    owner_name = ' '.join(part for part in (owner.first_name, owner.last_name) if part) if owner else None
    result['from_name'] = owner_name or None
    result.pop('public_token', None)  # no reason to echo the secret back inside its own public page
    return result
