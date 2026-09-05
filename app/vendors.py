# Vendor & Accounts Payable Management — vendor records, bills owed to them,
# and payments recorded against those bills. Bill status (paid/overdue/
# partial/unpaid) is never stored — it's derived on every read from the
# live sum of BillPayment rows vs. the bill's due_date, so it can never
# drift out of sync with the actual payment history the way a cached status
# column could (the same "derive, don't cache" discipline used for
# Trading Signals' success-rate math).

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import Vendor, Bill, BillPayment
from app.auth import get_current_user_id

router = APIRouter(prefix="/api/ap")

VALID_PAYMENT_TERMS = {'due_on_receipt', 'net_15', 'net_30', 'net_45', 'net_60'}
VALID_PAYMENT_METHODS = {'check', 'ach', 'card', 'wire', 'cash', 'other'}

AGING_BUCKETS = [
    ('current', None, 0),      # not yet due
    ('1-30', 1, 30),
    ('31-60', 31, 60),
    ('61-90', 61, 90),
    ('90+', 91, None),
]


# --- Pydantic payloads ---

class VendorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    payment_terms: str = 'net_30'
    notes: str | None = None

class VendorUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    payment_terms: str | None = None
    notes: str | None = None

class BillCreate(BaseModel):
    vendor_id: int
    bill_number: str | None = None
    bill_date: datetime
    due_date: datetime
    amount: Decimal = Field(..., gt=0)
    category: str | None = None
    memo: str | None = None

class BillUpdate(BaseModel):
    bill_number: str | None = None
    bill_date: datetime | None = None
    due_date: datetime | None = None
    amount: Decimal | None = Field(None, gt=0)
    category: str | None = None
    memo: str | None = None

class PaymentCreate(BaseModel):
    amount: Decimal = Field(..., gt=0)
    payment_date: datetime
    payment_method: str = 'other'
    reference_number: str | None = None
    notes: str | None = None


# --- Ownership + derivation helpers ---

def get_owned_vendor(vendor_id: int, user_id: int, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id, Vendor.user_id == user_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found.")
    return vendor

def get_owned_bill(bill_id: int, user_id: int, db: Session) -> Bill:
    bill = db.query(Bill).filter(Bill.id == bill_id, Bill.user_id == user_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found.")
    return bill

def paid_totals_by_bill(bill_ids: list[int], db: Session) -> dict[int, Decimal]:
    """One grouped query for N bills instead of an N+1 SUM per bill."""
    if not bill_ids:
        return {}
    rows = (
        db.query(BillPayment.bill_id, func.coalesce(func.sum(BillPayment.amount), 0))
        .filter(BillPayment.bill_id.in_(bill_ids))
        .group_by(BillPayment.bill_id)
        .all()
    )
    return {bill_id: Decimal(total) for bill_id, total in rows}

def days_overdue(due_date: datetime, now: datetime) -> int:
    return (now.date() - due_date.date()).days

def derive_bill_status(amount: Decimal, amount_paid: Decimal, due_date: datetime, now: datetime) -> str:
    if amount_paid >= amount:
        return 'paid'
    if days_overdue(due_date, now) > 0:
        return 'overdue'
    if amount_paid > 0:
        return 'partial'
    return 'unpaid'

def serialize_bill(bill: Bill, amount_paid: Decimal, now: datetime) -> dict:
    status = derive_bill_status(bill.amount, amount_paid, bill.due_date, now)
    return {
        'id': bill.id,
        'vendor_id': bill.vendor_id,
        'bill_number': bill.bill_number,
        'bill_date': bill.bill_date.isoformat(),
        'due_date': bill.due_date.isoformat(),
        'amount': float(bill.amount),
        'amount_paid': float(amount_paid),
        'balance': float(bill.amount - amount_paid),
        'status': status,
        'days_overdue': days_overdue(bill.due_date, now) if status == 'overdue' else 0,
        'category': bill.category,
        'memo': bill.memo,
        'created_at': bill.created_at.isoformat() if bill.created_at else None,
    }

def serialize_payment(payment: BillPayment) -> dict:
    return {
        'id': payment.id,
        'bill_id': payment.bill_id,
        'amount': float(payment.amount),
        'payment_date': payment.payment_date.isoformat(),
        'payment_method': payment.payment_method,
        'reference_number': payment.reference_number,
        'notes': payment.notes,
        'created_at': payment.created_at.isoformat() if payment.created_at else None,
    }


# --- Vendors ---

@router.post("/vendors")
async def create_vendor(payload: VendorCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if payload.payment_terms not in VALID_PAYMENT_TERMS:
        raise HTTPException(status_code=400, detail=f"payment_terms must be one of {sorted(VALID_PAYMENT_TERMS)}")
    vendor = Vendor(
        user_id=user_id, name=payload.name.strip(), email=payload.email, phone=payload.phone,
        address=payload.address, payment_terms=payload.payment_terms, notes=payload.notes,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return serialize_vendor(vendor, Decimal(0))

@router.get("/vendors")
async def list_vendors(include_inactive: bool = False, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    query = db.query(Vendor).filter(Vendor.user_id == user_id)
    if not include_inactive:
        query = query.filter(Vendor.is_active == True)
    vendors = query.order_by(Vendor.name).all()

    vendor_ids = [v.id for v in vendors]
    bills = db.query(Bill).filter(Bill.vendor_id.in_(vendor_ids)).all() if vendor_ids else []
    paid_totals = paid_totals_by_bill([b.id for b in bills], db)
    now = datetime.utcnow()

    balance_by_vendor: dict[int, Decimal] = {}
    for b in bills:
        paid = paid_totals.get(b.id, Decimal(0))
        if paid < b.amount:
            balance_by_vendor[b.vendor_id] = balance_by_vendor.get(b.vendor_id, Decimal(0)) + (b.amount - paid)

    return {'vendors': [serialize_vendor(v, balance_by_vendor.get(v.id, Decimal(0))) for v in vendors]}

def serialize_vendor(vendor: Vendor, outstanding_balance: Decimal) -> dict:
    return {
        'id': vendor.id,
        'name': vendor.name,
        'email': vendor.email,
        'phone': vendor.phone,
        'address': vendor.address,
        'payment_terms': vendor.payment_terms,
        'notes': vendor.notes,
        'is_active': vendor.is_active,
        'outstanding_balance': float(outstanding_balance),
        'created_at': vendor.created_at.isoformat() if vendor.created_at else None,
    }

@router.get("/vendors/{vendor_id}")
async def get_vendor(vendor_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    vendor = get_owned_vendor(vendor_id, user_id, db)
    bills = db.query(Bill).filter(Bill.vendor_id == vendor.id).order_by(Bill.due_date.desc()).all()
    paid_totals = paid_totals_by_bill([b.id for b in bills], db)
    now = datetime.utcnow()

    outstanding = sum(
        (b.amount - paid_totals.get(b.id, Decimal(0))
         for b in bills if paid_totals.get(b.id, Decimal(0)) < b.amount),
        Decimal(0),
    )
    result = serialize_vendor(vendor, outstanding)
    result['bills'] = [serialize_bill(b, paid_totals.get(b.id, Decimal(0)), now) for b in bills]
    return result

@router.put("/vendors/{vendor_id}")
async def update_vendor(vendor_id: int, payload: VendorUpdate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    vendor = get_owned_vendor(vendor_id, user_id, db)
    if payload.payment_terms is not None and payload.payment_terms not in VALID_PAYMENT_TERMS:
        raise HTTPException(status_code=400, detail=f"payment_terms must be one of {sorted(VALID_PAYMENT_TERMS)}")
    for field in ('name', 'email', 'phone', 'address', 'payment_terms', 'notes'):
        value = getattr(payload, field)
        if value is not None:
            setattr(vendor, field, value.strip() if field == 'name' else value)
    db.commit()
    db.refresh(vendor)
    bills = db.query(Bill).filter(Bill.vendor_id == vendor.id).all()
    paid_totals = paid_totals_by_bill([b.id for b in bills], db)
    outstanding = sum(
        (b.amount - paid_totals.get(b.id, Decimal(0))
         for b in bills if paid_totals.get(b.id, Decimal(0)) < b.amount),
        Decimal(0),
    )
    return serialize_vendor(vendor, outstanding)

@router.post("/vendors/{vendor_id}/deactivate")
async def deactivate_vendor(vendor_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    vendor = get_owned_vendor(vendor_id, user_id, db)
    vendor.is_active = False
    db.commit()
    return {'status': 'success'}

@router.post("/vendors/{vendor_id}/reactivate")
async def reactivate_vendor(vendor_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    vendor = get_owned_vendor(vendor_id, user_id, db)
    vendor.is_active = True
    db.commit()
    return {'status': 'success'}


# --- Bills ---

@router.post("/bills")
async def create_bill(payload: BillCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    get_owned_vendor(payload.vendor_id, user_id, db)  # 404s if not this user's vendor
    bill = Bill(
        user_id=user_id, vendor_id=payload.vendor_id, bill_number=payload.bill_number,
        bill_date=payload.bill_date, due_date=payload.due_date, amount=payload.amount,
        category=payload.category, memo=payload.memo,
    )
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return serialize_bill(bill, Decimal(0), datetime.utcnow())

@router.get("/bills")
async def list_bills(
    vendor_id: int | None = None, status: str | None = None,
    user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db),
):
    query = db.query(Bill).filter(Bill.user_id == user_id)
    if vendor_id is not None:
        query = query.filter(Bill.vendor_id == vendor_id)
    bills = query.order_by(Bill.due_date.asc()).all()

    paid_totals = paid_totals_by_bill([b.id for b in bills], db)
    now = datetime.utcnow()
    serialized = [serialize_bill(b, paid_totals.get(b.id, Decimal(0)), now) for b in bills]
    if status is not None:
        serialized = [s for s in serialized if s['status'] == status]
    return {'bills': serialized}

@router.get("/bills/{bill_id}")
async def get_bill(bill_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    bill = get_owned_bill(bill_id, user_id, db)
    payments = db.query(BillPayment).filter(BillPayment.bill_id == bill.id).order_by(BillPayment.payment_date.desc()).all()
    amount_paid = sum((p.amount for p in payments), Decimal(0))
    result = serialize_bill(bill, amount_paid, datetime.utcnow())
    result['payments'] = [serialize_payment(p) for p in payments]
    return result

@router.put("/bills/{bill_id}")
async def update_bill(bill_id: int, payload: BillUpdate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    bill = get_owned_bill(bill_id, user_id, db)
    for field in ('bill_number', 'bill_date', 'due_date', 'amount', 'category', 'memo'):
        value = getattr(payload, field)
        if value is not None:
            setattr(bill, field, value)
    db.commit()
    db.refresh(bill)
    payments = db.query(BillPayment).filter(BillPayment.bill_id == bill.id).all()
    amount_paid = sum((p.amount for p in payments), Decimal(0))
    return serialize_bill(bill, amount_paid, datetime.utcnow())

@router.delete("/bills/{bill_id}")
async def delete_bill(bill_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    bill = get_owned_bill(bill_id, user_id, db)
    has_payments = db.query(BillPayment).filter(BillPayment.bill_id == bill.id).first() is not None
    if has_payments:
        raise HTTPException(status_code=400, detail="Cannot delete a bill with recorded payments. Void the payments first.")
    db.delete(bill)
    db.commit()
    return {'status': 'success'}


# --- Payments ---

@router.post("/bills/{bill_id}/payments")
async def record_payment(bill_id: int, payload: PaymentCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    bill = get_owned_bill(bill_id, user_id, db)
    if payload.payment_method not in VALID_PAYMENT_METHODS:
        raise HTTPException(status_code=400, detail=f"payment_method must be one of {sorted(VALID_PAYMENT_METHODS)}")

    existing_paid = db.query(func.coalesce(func.sum(BillPayment.amount), 0)).filter(BillPayment.bill_id == bill.id).scalar()
    existing_paid = Decimal(existing_paid)
    remaining = bill.amount - existing_paid
    if payload.amount > remaining:
        raise HTTPException(
            status_code=400,
            detail=f"Payment of {payload.amount} exceeds the remaining balance of {remaining}.",
        )

    payment = BillPayment(
        bill_id=bill.id, amount=payload.amount, payment_date=payload.payment_date,
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
        db.query(BillPayment)
        .join(Bill, Bill.id == BillPayment.bill_id)
        .filter(BillPayment.id == payment_id, Bill.user_id == user_id)
        .first()
    )
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found.")
    db.delete(payment)
    db.commit()
    return {'status': 'success'}


# --- Aging / summary ---

@router.get("/summary")
async def get_ap_summary(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    bills = db.query(Bill).filter(Bill.user_id == user_id).all()
    paid_totals = paid_totals_by_bill([b.id for b in bills], db)
    now = datetime.utcnow()

    bucket_totals = {label: Decimal(0) for label, _, _ in AGING_BUCKETS}
    status_counts = {'unpaid': 0, 'partial': 0, 'overdue': 0, 'paid': 0}
    total_outstanding = Decimal(0)
    total_overdue = Decimal(0)
    vendor_balances: dict[int, Decimal] = {}

    for bill in bills:
        paid = paid_totals.get(bill.id, Decimal(0))
        balance = bill.amount - paid
        status = derive_bill_status(bill.amount, paid, bill.due_date, now)
        status_counts[status] += 1

        if balance <= 0:
            continue  # fully paid bills don't count toward outstanding/aging

        total_outstanding += balance
        vendor_balances[bill.vendor_id] = vendor_balances.get(bill.vendor_id, Decimal(0)) + balance

        overdue = days_overdue(bill.due_date, now)
        if overdue > 0:
            total_overdue += balance
        for label, lo, hi in AGING_BUCKETS:
            if lo is None:  # 'current' — not yet due
                if overdue <= 0:
                    bucket_totals[label] += balance
                    break
            elif hi is None:  # open-ended top bucket
                if overdue >= lo:
                    bucket_totals[label] += balance
                    break
            elif lo <= overdue <= hi:
                bucket_totals[label] += balance
                break

    vendor_names = dict(db.query(Vendor.id, Vendor.name).filter(Vendor.id.in_(vendor_balances.keys())).all()) if vendor_balances else {}
    top_vendors = sorted(
        ({'vendor_id': vid, 'vendor_name': vendor_names.get(vid, 'Unknown'), 'balance': float(bal)}
         for vid, bal in vendor_balances.items()),
        key=lambda v: v['balance'], reverse=True,
    )[:5]

    return {
        'total_outstanding': float(total_outstanding),
        'total_overdue': float(total_overdue),
        'bill_counts': status_counts,
        'aging_buckets': [{'label': label, 'amount': float(bucket_totals[label])} for label, _, _ in AGING_BUCKETS],
        'top_vendors_by_balance': top_vendors,
    }
