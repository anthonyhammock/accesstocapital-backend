# Balance Sheet — benchmarked against QuickBooks, Wave, and FreshBooks:
# all three require the owner to either connect a bank feed or manually
# keep a real double-entry ledger before their balance sheet report means
# anything. This platform already owns the pieces a small business's
# balance sheet is made of — the Bookkeeping ledger, Invoicing, and
# Vendor & AP — so this report derives Cash, Accounts Receivable, and
# Accounts Payable directly from them instead of asking the owner to
# re-enter numbers a second time.
#
# What it can't derive: things with no feed anywhere on the platform yet
# (equipment, inventory, a loan balance) and owner contributions/draws.
# Those are simple manual entries — BalanceSheetItem for the former,
# OwnerEquityEntry for the latter (which doubles as the Statement of
# Owner's Equity roll-forward).
#
# This ledger is single-entry (income/expense, not a real chart of
# accounts), so Total Equity is presented as two honest, addable parts
# rather than one number pretending to be more precise than the
# underlying data supports:
#   - Cash-Basis Equity: cumulative net income from the ledger, plus
#     contributions, less draws — the part actually backed by cash.
#   - Other Net Assets: Accounts Receivable + other manual assets, less
#     Accounts Payable + other manual liabilities — real value the
#     business holds that hasn't hit the cash ledger yet (an unpaid
#     invoice, a bill not yet paid, a piece of equipment).
# Their sum always equals Total Assets − Total Liabilities exactly, by
# construction, so the sheet always balances without a fabricated plug.

import io
import csv
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from fastapi.responses import StreamingResponse

from app.database import get_db
from app.models import (
    Transaction, Invoice, InvoiceLineItem, Bill, Vendor,
    BalanceSheetItem, OwnerEquityEntry,
)
from app.auth import get_current_user_id
from app.invoicing import invoice_total, paid_totals_by_invoice, derive_invoice_status
from app.vendors import paid_totals_by_bill, derive_bill_status

router = APIRouter()

VALID_SIDES = {'asset', 'liability'}
VALID_ENTRY_TYPES = {'contribution', 'draw'}


# --- Pydantic payloads ---

# Numeric(14, 2) columns back these — 12 digits before the decimal point.
MAX_AMOUNT = Decimal('999999999999.99')

class BalanceSheetItemCreate(BaseModel):
    side: str
    name: str = Field(..., min_length=1, max_length=255)
    amount: Decimal = Field(..., ge=0, le=MAX_AMOUNT)
    notes: str | None = Field(None, max_length=2000)

class BalanceSheetItemUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    amount: Decimal | None = Field(None, ge=0, le=MAX_AMOUNT)
    notes: str | None = Field(None, max_length=2000)

class OwnerEquityEntryCreate(BaseModel):
    entry_type: str
    amount: Decimal = Field(..., gt=0, le=MAX_AMOUNT)
    entry_date: datetime
    description: str | None = Field(None, max_length=2000)


# --- Ownership helpers ---

def get_owned_item(item_id: int, user_id: int, db: Session) -> BalanceSheetItem:
    item = db.query(BalanceSheetItem).filter(BalanceSheetItem.id == item_id, BalanceSheetItem.user_id == user_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Balance sheet item not found.")
    return item

def get_owned_equity_entry(entry_id: int, user_id: int, db: Session) -> OwnerEquityEntry:
    entry = db.query(OwnerEquityEntry).filter(OwnerEquityEntry.id == entry_id, OwnerEquityEntry.user_id == user_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Equity entry not found.")
    return entry

def serialize_item(item: BalanceSheetItem) -> dict:
    return {
        'id': item.id, 'side': item.side, 'name': item.name,
        'amount': float(item.amount), 'notes': item.notes,
        'created_at': item.created_at.isoformat() if item.created_at else None,
    }

def serialize_equity_entry(entry: OwnerEquityEntry) -> dict:
    return {
        'id': entry.id, 'entry_type': entry.entry_type, 'amount': float(entry.amount),
        'entry_date': entry.entry_date.isoformat(), 'description': entry.description,
        'created_at': entry.created_at.isoformat() if entry.created_at else None,
    }


# --- Manual asset/liability items ---

@router.post("/api/balance-sheet/items")
async def create_item(payload: BalanceSheetItemCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if payload.side not in VALID_SIDES:
        raise HTTPException(status_code=400, detail="side must be 'asset' or 'liability'.")
    item = BalanceSheetItem(user_id=user_id, side=payload.side, name=payload.name, amount=payload.amount, notes=payload.notes)
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize_item(item)

@router.get("/api/balance-sheet/items")
async def list_items(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    items = db.query(BalanceSheetItem).filter(BalanceSheetItem.user_id == user_id).order_by(BalanceSheetItem.created_at).all()
    return {'items': [serialize_item(i) for i in items]}

@router.put("/api/balance-sheet/items/{item_id}")
async def update_item(item_id: int, payload: BalanceSheetItemUpdate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    item = get_owned_item(item_id, user_id, db)
    for field in ('name', 'amount', 'notes'):
        value = getattr(payload, field)
        if value is not None:
            setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return serialize_item(item)

@router.delete("/api/balance-sheet/items/{item_id}")
async def delete_item(item_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    item = get_owned_item(item_id, user_id, db)
    db.delete(item)
    db.commit()
    return {'status': 'success'}


# --- Owner contributions / draws ---

@router.post("/api/balance-sheet/equity-entries")
async def create_equity_entry(payload: OwnerEquityEntryCreate, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if payload.entry_type not in VALID_ENTRY_TYPES:
        raise HTTPException(status_code=400, detail="entry_type must be 'contribution' or 'draw'.")
    entry = OwnerEquityEntry(
        user_id=user_id, entry_type=payload.entry_type, amount=payload.amount,
        entry_date=payload.entry_date, description=payload.description,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return serialize_equity_entry(entry)

@router.get("/api/balance-sheet/equity-entries")
async def list_equity_entries(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    entries = db.query(OwnerEquityEntry).filter(OwnerEquityEntry.user_id == user_id).order_by(OwnerEquityEntry.entry_date.desc()).all()
    return {'entries': [serialize_equity_entry(e) for e in entries]}

@router.delete("/api/balance-sheet/equity-entries/{entry_id}")
async def delete_equity_entry(entry_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    entry = get_owned_equity_entry(entry_id, user_id, db)
    db.delete(entry)
    db.commit()
    return {'status': 'success'}


# --- The report itself ---

def compute_balance_sheet(user_id: int, db: Session) -> dict:
    now = datetime.utcnow()

    # Cash-basis retained earnings: every income/expense transaction ever
    # recorded, same ledger P&L and Cash Flow already use.
    txs = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    revenue = sum((t.amount for t in txs if t.transaction_type == 'income'), Decimal(0))
    expenses = sum((t.amount for t in txs if t.transaction_type == 'expense'), Decimal(0))
    retained_earnings = revenue - expenses

    contributions = db.query(OwnerEquityEntry).filter(OwnerEquityEntry.user_id == user_id, OwnerEquityEntry.entry_type == 'contribution').all()
    draws = db.query(OwnerEquityEntry).filter(OwnerEquityEntry.user_id == user_id, OwnerEquityEntry.entry_type == 'draw').all()
    total_contributions = sum((e.amount for e in contributions), Decimal(0))
    total_draws = sum((e.amount for e in draws), Decimal(0))

    cash = retained_earnings + total_contributions - total_draws
    cash_basis_equity = cash  # same figure, presented as the equity-side label

    # Accounts Receivable: balance remaining on every invoice that's been
    # sent but not yet fully paid.
    invoices = db.query(Invoice).filter(Invoice.user_id == user_id).all()
    line_items_by_invoice = {}
    if invoices:
        for li in db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id.in_([i.id for i in invoices])).all():
            line_items_by_invoice.setdefault(li.invoice_id, []).append(li)
    paid_by_invoice = paid_totals_by_invoice([i.id for i in invoices], db)

    accounts_receivable = Decimal(0)
    for inv in invoices:
        line_items = line_items_by_invoice.get(inv.id, [])
        total = invoice_total(line_items, inv.tax_rate)
        paid = paid_by_invoice.get(inv.id, Decimal(0))
        status = derive_invoice_status(inv, total, paid, now)
        if status in ('sent', 'partial', 'overdue'):
            accounts_receivable += (total - paid)

    # Accounts Payable: balance remaining on every unpaid/partial/overdue bill.
    bills = db.query(Bill).filter(Bill.user_id == user_id).all()
    paid_by_bill = paid_totals_by_bill([b.id for b in bills], db)

    accounts_payable = Decimal(0)
    for bill in bills:
        paid = paid_by_bill.get(bill.id, Decimal(0))
        status = derive_bill_status(bill.amount, paid, bill.due_date, now)
        if status in ('unpaid', 'partial', 'overdue'):
            accounts_payable += (bill.amount - paid)

    manual_items = db.query(BalanceSheetItem).filter(BalanceSheetItem.user_id == user_id).all()
    other_assets = sum((i.amount for i in manual_items if i.side == 'asset'), Decimal(0))
    other_liabilities = sum((i.amount for i in manual_items if i.side == 'liability'), Decimal(0))

    total_assets = cash + accounts_receivable + other_assets
    total_liabilities = accounts_payable + other_liabilities

    # The reconciling half of equity: real value the business holds that
    # hasn't hit the cash ledger yet. Always equals total_assets minus
    # total_liabilities minus cash_basis_equity — never a separate guess.
    other_net_assets = accounts_receivable + other_assets - accounts_payable - other_liabilities
    total_equity = cash_basis_equity + other_net_assets

    return {
        'assets': {
            'cash': float(cash),
            'accounts_receivable': float(accounts_receivable),
            'other_assets': float(other_assets),
            'total': float(total_assets),
        },
        'liabilities': {
            'accounts_payable': float(accounts_payable),
            'other_liabilities': float(other_liabilities),
            'total': float(total_liabilities),
        },
        'equity': {
            'retained_earnings': float(retained_earnings),
            'owner_contributions': float(total_contributions),
            'owner_draws': float(total_draws),
            'cash_basis_equity': float(cash_basis_equity),
            'other_net_assets': float(other_net_assets),
            'total': float(total_equity),
        },
        'total_liabilities_and_equity': float(total_liabilities + total_equity),
        'as_of': now.date().isoformat(),
    }

@router.get("/api/reports/balance-sheet")
async def balance_sheet(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    return compute_balance_sheet(user_id, db)

@router.get("/api/reports/balance-sheet/export")
async def export_balance_sheet_csv(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    report = compute_balance_sheet(user_id, db)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([f"Balance Sheet — as of {report['as_of']}"])
    writer.writerow([])
    writer.writerow(['Assets'])
    writer.writerow(['  Cash', f"{report['assets']['cash']:.2f}"])
    writer.writerow(['  Accounts Receivable', f"{report['assets']['accounts_receivable']:.2f}"])
    writer.writerow(['  Other Assets', f"{report['assets']['other_assets']:.2f}"])
    writer.writerow(['Total Assets', f"{report['assets']['total']:.2f}"])
    writer.writerow([])
    writer.writerow(['Liabilities'])
    writer.writerow(['  Accounts Payable', f"{report['liabilities']['accounts_payable']:.2f}"])
    writer.writerow(['  Other Liabilities', f"{report['liabilities']['other_liabilities']:.2f}"])
    writer.writerow(['Total Liabilities', f"{report['liabilities']['total']:.2f}"])
    writer.writerow([])
    writer.writerow(["Owner's Equity"])
    writer.writerow(['  Cash-Basis Equity (Retained Earnings + Contributions - Draws)', f"{report['equity']['cash_basis_equity']:.2f}"])
    writer.writerow(['  Other Net Assets (A/R + Other Assets - A/P - Other Liabilities)', f"{report['equity']['other_net_assets']:.2f}"])
    writer.writerow(["Total Owner's Equity", f"{report['equity']['total']:.2f}"])
    writer.writerow([])
    writer.writerow(['Total Liabilities & Equity', f"{report['total_liabilities_and_equity']:.2f}"])

    filename = f"balance-sheet-{report['as_of']}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
