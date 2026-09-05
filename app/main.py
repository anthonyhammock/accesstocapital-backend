from fastapi import FastAPI, Depends, HTTPException, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, extract, func
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from decimal import Decimal
import csv
import io
import json
from datetime import datetime, timezone, timedelta, time as time_type
from zoneinfo import ZoneInfo, available_timezones
import os
import re
import secrets
import urllib.parse
import uuid
import requests
from cryptography.fernet import Fernet

from app.database import get_db, engine
from app.models import (
    Base, DeductionRule, CategorizationRule, Transaction, TaxSummary, User,
    ConsumerAccount, BusinessAccount, PortalClient, PortalDocument, PortalComment,
    SchedulingSettings, AvailabilityRule, Booking, CalendarConnection,
)
from app.auth import create_access_token, get_current_user_id, create_oauth_state_token, verify_oauth_state_token
from app.trading import router as trading_router
from app.vendors import router as vendors_router
from app.invoicing import router as invoicing_router
import bcrypt

app = FastAPI(title="BlissPoint Tax & Credit", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://accesstocapital-web.vercel.app",
    ],
    allow_origin_regex=r"https://accesstocapital.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trading_router)
app.include_router(vendors_router)
app.include_router(invoicing_router)

# ============================================
# PYDANTIC MODELS
# ============================================

class RegisterRequest(BaseModel):
    email: str
    password: str
    first_name: str
    last_name: str
    account_type: str
    business_name: str = None

class LoginRequest(BaseModel):
    email: str
    password: str

class AddBusinessRequest(BaseModel):
    business_name: str
    ein: str = None
    business_type: str = None
    annual_revenue: Decimal = None

class PortalClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str | None = None
    notes: str | None = None

class PortalCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5000)

class SchedulingSettingsUpdate(BaseModel):
    timezone: str
    meeting_duration_minutes: int = Field(gt=0, le=480)
    buffer_minutes: int = Field(ge=0, le=120)
    min_notice_hours: int = Field(ge=0, le=336)
    is_active: bool = True

class AvailabilityRuleInput(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: str
    end_time: str

class AvailabilityRulesUpdate(BaseModel):
    rules: list[AvailabilityRuleInput]

class BookingCreate(BaseModel):
    start_at: str
    guest_name: str = Field(min_length=1, max_length=255)
    guest_email: str = Field(min_length=3, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)

class TransactionInput(BaseModel):
    merchant_name: str
    amount: Decimal
    transaction_date: str
    deduction_code: str = None

class CalculationRequest(BaseModel):
    tax_year: int
    entity_type: str
    transactions: list = []
    officer_wages: Decimal = Decimal(0)

class QuestionnaireAnswer(BaseModel):
    deduction_code: str
    amount: Decimal = Decimal(0)

class QuestionnaireSubmission(BaseModel):
    tax_year: int
    entity_type: str
    answers: list[QuestionnaireAnswer]
    officer_wages: Decimal = Decimal(0)

# Numeric(12,2) tops out at 10 integer digits; amount must be positive —
# the sign is carried by transaction_type, not the amount itself, so a
# negative amount would silently corrupt the income/expense/net totals.
MAX_TRANSACTION_AMOUNT = Decimal('9999999999.99')

class TransactionCreate(BaseModel):
    transaction_date: str  # 'YYYY-MM-DD'
    merchant_name: str
    amount: Decimal = Field(gt=0, le=MAX_TRANSACTION_AMOUNT)
    transaction_type: str = 'expense'  # 'income' or 'expense'
    cash_flow_category: str = 'operating'  # 'operating', 'investing', or 'financing'
    deduction_code: str = None
    description: str = None

class TransactionUpdate(BaseModel):
    transaction_date: str = None
    merchant_name: str = None
    amount: Decimal | None = Field(default=None, gt=0, le=MAX_TRANSACTION_AMOUNT)
    transaction_type: str = None
    cash_flow_category: str = None
    deduction_code: str = None
    description: str = None

# ============================================
# QUESTIONNAIRE TEXT — plain language, no tax jargon
# ============================================

QUESTION_TEXT = {
    'BUS_MEALS_ENTERTAINMENT': 'Did you pay for meals while doing business — client meetings, business meals, or entertaining customers?',
    'BUS_SAAS_TOOLS': 'Did you pay for business software, apps, or online subscriptions (like Stripe, AWS, or similar tools)?',
    'BUS_OFFICE_SUPPLIES': 'Did you buy office supplies like pens, paper, or folders for your business?',
    'BUS_PRINTER_SUPPLIES': 'Did you buy printer ink or toner for business use?',
    'BUS_OFFICE_FURNITURE': 'Did you buy office furniture like a desk, chair, or filing cabinet?',
    'BUS_COMPUTER_EQUIPMENT': 'Did you buy a computer, laptop, or monitor for business use?',
    'BUS_TRAVEL_AIRFARE': 'Did you pay for airfare for a business trip?',
    'BUS_TRAVEL_HOTEL': 'Did you pay for a hotel or lodging during business travel?',
    'BUS_TRAVEL_RENTAL_CAR': 'Did you rent a car for business travel?',
    'BUS_TRAVEL_PARKING_TOLLS': 'Did you pay for parking or tolls while traveling for business?',
    'BUS_TRAVEL_TAXI_RIDESHARE': 'Did you use a taxi, Uber, or Lyft for business purposes?',
    'BUS_TRAVEL_BAGGAGE': 'Did you pay baggage fees during business travel?',
    'BUS_OFFICE_RENT': 'Did you pay rent for a commercial office space?',
    'BUS_EQUIPMENT_RENTAL': 'Did you rent equipment like a copier or server for your business?',
    'BUS_VEHICLE_LEASE': 'Did you lease a vehicle for business use?',
    'BUS_STORAGE_RENTAL': 'Did you rent a storage unit for business inventory or equipment?',
    'BUS_VEHICLE_GAS': 'Did you pay for gas or fuel in a vehicle used for business?',
    'BUS_VEHICLE_OIL_CHANGE': 'Did you pay for oil changes on a business vehicle?',
    'BUS_VEHICLE_MAINTENANCE': 'Did you pay for repairs or maintenance on a business vehicle?',
    'BUS_VEHICLE_INSURANCE': 'Did you pay insurance on a vehicle used for business?',
    'BUS_ACCOUNTANT_BOOKKEEPER': 'Did you pay an accountant or bookkeeper for business services?',
    'BUS_LEGAL_FEES': 'Did you pay an attorney for business legal services?',
    'BUS_PHONE_INTERNET': 'Did you pay for phone or internet service used for your business?',
    'BUS_BANK_FEES': 'Did you pay any business bank account fees or charges?',
    'BUS_LOAN_INTEREST': 'Did you pay interest on a business loan?',
}

# ============================================
# SHARED CALCULATION LOGIC
# Used by both the CSV-upload path and the questionnaire path,
# so IRC rules and form-line mapping never drift between the two.
# ============================================

def compute_tax_summary(user_id: int, tax_year: int, entity_type: str, officer_wages: Decimal, db: Session):
    txs = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        extract('year', Transaction.transaction_date) == tax_year
    ).all()

    total = Decimal(0)
    lines = {}
    details = []

    for tx in txs:
        if not tx.deduction_code:
            continue

        rule = db.query(DeductionRule).filter(
            DeductionRule.deduction_code == tx.deduction_code
        ).first()

        if not rule:
            continue

        amount = tx.amount
        limit = None

        if rule.meals_50_percent:
            amount = tx.amount * Decimal('0.50')
            limit = '50% meals'

        total += amount

        if entity_type == 'SOLE_PROP':
            line = rule.form_mapping_sole_prop
        elif entity_type == 'S_CORP':
            line = rule.form_mapping_s_corp
        else:
            line = rule.form_mapping_c_corp

        if line not in lines:
            lines[line] = {'total': Decimal(0), 'count': 0}

        lines[line]['total'] += amount
        lines[line]['count'] += 1

        details.append({
            'merchant': tx.merchant_name,
            'original': float(tx.amount),
            'deductible': float(amount),
            'limit': limit,
            'line': line
        })

    if entity_type != 'SOLE_PROP' and officer_wages > 0:
        total += officer_wages
        line = 'Form 1120-S Line 7' if entity_type == 'S_CORP' else 'Form 1120 Line 12'
        lines[line] = {'total': officer_wages, 'count': 1}

    breakdown_json = json.dumps({k: float(v['total']) for k, v in lines.items()})

    existing = db.query(TaxSummary).filter(
        TaxSummary.user_id == user_id,
        TaxSummary.tax_year == tax_year
    ).first()

    if existing:
        existing.entity_type = entity_type
        existing.total_deductions = total
        existing.officer_wages = officer_wages
        existing.form_line_breakdown = breakdown_json
    else:
        db.add(TaxSummary(
            user_id=user_id,
            tax_year=tax_year,
            entity_type=entity_type,
            total_deductions=total,
            officer_wages=officer_wages,
            form_line_breakdown=breakdown_json,
            status='draft'
        ))

    db.commit()

    return {
        'status': 'success',
        'total': float(total),
        'lines': {k: {'total': float(v['total']), 'count': v['count']} for k, v in lines.items()},
        'details': details[:20]
    }

# ============================================
# HELPER: CSV MERCHANT MATCHING
# ============================================

def categorize_merchant(merchant: str, db: Session) -> dict:
    exact = db.query(CategorizationRule).filter(
        CategorizationRule.rule_type == 'exact_vendor'
    ).order_by(CategorizationRule.priority).all()

    for rule in exact:
        if rule.rule_value.upper() in merchant.upper():
            return {'code': rule.deduction_code, 'confidence': 0.95, 'method': 'exact'}

    regex_rules = db.query(CategorizationRule).filter(
        CategorizationRule.rule_type == 'regex_pattern'
    ).order_by(CategorizationRule.priority).all()

    for rule in regex_rules:
        try:
            if re.search(rule.rule_value, merchant, re.IGNORECASE):
                return {'code': rule.deduction_code, 'confidence': 0.80, 'method': 'regex'}
        except:
            pass

    return {'code': None, 'confidence': 0, 'method': 'none'}

# ============================================
# QUESTIONNAIRE ENDPOINTS (new)
# ============================================

# ============================================
# AUTH ENDPOINTS
# ============================================

ACCOUNTS_PER_BUREAU_SET = 3
ADDITIONAL_BUSINESS_MONTHLY_FEE = Decimal('50.00')
CONSUMER_MONTHLY_FEE = Decimal('10.00')

# Cloud Run's default max request body is 32MB — stay well under that so a
# large upload fails with our own clear 413, not a generic proxy error.
MAX_PORTAL_FILE_SIZE = 15 * 1024 * 1024

def mask_ein(ein: str | None) -> str | None:
    """Show only the last 4 digits — never return a full EIN over the API."""
    if not ein:
        return None
    digits = re.sub(r'\D', '', ein)
    if len(digits) < 4:
        return 'XX-XXXXXXX'
    return f"XX-XXX{digits[-4:]}"

def mask_account_number(number: str | None) -> str | None:
    """Show only the last 4 characters of an account number."""
    if not number:
        return None
    if len(number) <= 4:
        return '•' * len(number)
    return '•' * (len(number) - 4) + number[-4:]

def sync_account_type(user: User, db: Session):
    """Recompute account_type from what the user actually has (rather than only
    what they picked at signup), so it never drifts out of sync after adding a
    business or a personal account later."""
    has_consumer = db.query(ConsumerAccount).filter(ConsumerAccount.user_id == user.id).first() is not None
    has_business = db.query(BusinessAccount).filter(BusinessAccount.user_id == user.id).first() is not None

    if has_consumer and has_business:
        user.account_type = 'both'
    elif has_business:
        user.account_type = 'business'
    elif has_consumer:
        user.account_type = 'consumer'

    db.commit()

def provision_business_accounts(user_id: int, business_name: str, ein: str, business_type: str, annual_revenue, db: Session) -> str:
    """Create one business's set of tradeline accounts, tagged with a shared
    business_group_id so they can be told apart from any other business the
    same user has. Returns the new business_group_id."""
    group_id = str(uuid.uuid4())
    for i in range(1, ACCOUNTS_PER_BUREAU_SET + 1):
        db.add(BusinessAccount(
            user_id=user_id,
            business_group_id=group_id,
            business_name=business_name,
            ein=ein,
            business_type=business_type,
            annual_revenue=annual_revenue,
            current_balance=Decimal(0),
            reported_to_bureaus=False
        ))
    return group_id

def provision_credit_builder_accounts(user: User, business_name: str, db: Session):
    """Create the tradeline accounts a new subscriber gets reported to the bureaus.
    Accounts start unfunded (no credit limit, $0 balance, not yet reported) —
    the real limit is set once the funding/escrow step exists."""
    if user.account_type in ('consumer', 'both'):
        for i in range(1, ACCOUNTS_PER_BUREAU_SET + 1):
            db.add(ConsumerAccount(
                user_id=user.id,
                account_name=f"Credit Builder Account {i}",
                current_balance=Decimal(0),
                reported_to_bureaus=False
            ))

    if user.account_type in ('business', 'both'):
        provision_business_accounts(user.id, business_name, None, None, None, db)

    db.commit()

@app.post("/api/auth/register")
async def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    if payload.account_type not in ('consumer', 'business', 'both'):
        raise HTTPException(status_code=400, detail="account_type must be 'consumer', 'business', or 'both'.")

    if payload.account_type in ('business', 'both') and not payload.business_name:
        raise HTTPException(status_code=400, detail="Business name is required for a business account.")

    hashed = bcrypt.hashpw(payload.password.encode('utf-8'), bcrypt.gensalt())

    user = User(
        email=payload.email,
        password_hash=hashed.decode('utf-8'),
        first_name=payload.first_name,
        last_name=payload.last_name,
        account_type=payload.account_type,
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    provision_credit_builder_accounts(user, payload.business_name, db)

    return {'status': 'success', 'user_id': user.id}

@app.post("/api/auth/login")
async def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()

    if not user or not bcrypt.checkpw(payload.password.encode('utf-8'), user.password_hash.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")

    return {
        'status': 'success',
        'token': create_access_token(user.id),
        'user': {
            'id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'account_type': user.account_type
        }
    }


@app.get("/api/me")
async def get_me(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """Fresh account info, not the possibly-stale copy cached in the frontend's
    localStorage from login — account_type in particular can change any time a
    business or personal account is added later."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return {
        'id': user.id,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'account_type': user.account_type
    }

@app.get("/api/consumer-accounts")
async def get_consumer_accounts(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    accounts = db.query(ConsumerAccount).filter(ConsumerAccount.user_id == user_id).all()
    return {
        'accounts': [
            {
                'id': a.id,
                'account_name': a.account_name,
                'account_number': mask_account_number(a.account_number),
                'credit_limit': float(a.credit_limit) if a.credit_limit is not None else None,
                'current_balance': float(a.current_balance) if a.current_balance is not None else None,
                'payment_status': a.payment_status,
                'reported_to_bureaus': a.reported_to_bureaus
            }
            for a in accounts
        ]
    }

@app.post("/api/consumer-accounts")
async def add_consumer_account(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """Add personal credit-building to an account that doesn't have it yet — the
    consumer-side counterpart to /api/businesses. Unlike businesses, a person
    only needs one set of these, so this is a one-time addition, not something
    you can repeat once you already have one."""
    existing = db.query(ConsumerAccount).filter(ConsumerAccount.user_id == user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="You already have a personal credit builder account.")

    for i in range(1, ACCOUNTS_PER_BUREAU_SET + 1):
        db.add(ConsumerAccount(
            user_id=user_id,
            account_name=f"Credit Builder Account {i}",
            current_balance=Decimal(0),
            reported_to_bureaus=False
        ))
    db.commit()

    user = db.query(User).filter(User.id == user_id).first()
    sync_account_type(user, db)

    return {'status': 'success', 'monthly_fee': float(CONSUMER_MONTHLY_FEE)}

@app.get("/api/business-accounts")
async def get_business_accounts(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    accounts = db.query(BusinessAccount).filter(BusinessAccount.user_id == user_id).all()
    return {
        'accounts': [
            {
                'id': a.id,
                'business_name': a.business_name,
                'ein': mask_ein(a.ein),
                'credit_limit': float(a.credit_limit) if a.credit_limit is not None else None,
                'current_balance': float(a.current_balance) if a.current_balance is not None else None,
                'reported_to_bureaus': a.reported_to_bureaus
            }
            for a in accounts
        ]
    }

@app.get("/api/businesses")
async def get_businesses(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """List the distinct businesses a user has (each is a group of ACCOUNTS_PER_BUREAU_SET
    tradeline accounts sharing a business_group_id), with the monthly fee for each."""
    accounts = db.query(BusinessAccount).filter(BusinessAccount.user_id == user_id).order_by(BusinessAccount.created_at).all()

    groups = {}
    for a in accounts:
        key = a.business_group_id or f"legacy-{a.business_name}-{a.ein or ''}"
        if key not in groups:
            groups[key] = {
                'business_group_id': a.business_group_id,
                'business_name': a.business_name,
                'ein': mask_ein(a.ein),
                'business_type': a.business_type,
                'annual_revenue': float(a.annual_revenue) if a.annual_revenue is not None else None,
                'monthly_fee': float(ADDITIONAL_BUSINESS_MONTHLY_FEE),
                'billing_status': 'pending_payment_setup',
                'account_ids': []
            }
        groups[key]['account_ids'].append(a.id)

    return {'businesses': list(groups.values())}

@app.post("/api/businesses")
async def add_business(payload: AddBusinessRequest, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """Add an additional business for an existing user (e.g. an owner of multiple
    businesses who each need their own 3 reported tradeline accounts).
    NOTE: real payment collection isn't wired up yet — this creates the business's
    accounts and reports a monthly_fee of $50, but does not charge anything. The
    business is created in 'pending_payment_setup' billing status until Stripe
    billing is built; that's expected to catch up and start real billing then."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    group_id = provision_business_accounts(
        user.id, payload.business_name, payload.ein, payload.business_type, payload.annual_revenue, db
    )
    db.commit()
    sync_account_type(user, db)

    return {
        'status': 'success',
        'business_group_id': group_id,
        'monthly_fee': float(ADDITIONAL_BUSINESS_MONTHLY_FEE),
        'billing_status': 'pending_payment_setup'
    }

@app.get("/api/tax/questionnaire-questions")
async def get_questionnaire_questions(db: Session = Depends(get_db)):
    """Plain-language questions for the guided walkthrough, one per business deduction rule."""
    rules = db.query(DeductionRule).order_by(DeductionRule.deduction_name).all()
    return {
        'questions': [
            {
                'deduction_code': r.deduction_code,
                'question': QUESTION_TEXT.get(r.deduction_code, f"Did you have {r.deduction_name} expenses?"),
                'meals_50_percent': r.meals_50_percent
            }
            for r in rules
        ]
    }

@app.post("/api/tax/submit-questionnaire")
async def submit_questionnaire(payload: QuestionnaireSubmission, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """Save walkthrough answers as transactions, then calculate deductions from them."""
    try:
        year_start = datetime(payload.tax_year, 1, 1)
        year_end = datetime(payload.tax_year, 12, 31, 23, 59, 59)

        # Clear previous questionnaire answers for this user/year so re-submitting
        # (e.g. after changing an answer) doesn't double-count old ones.
        db.query(Transaction).filter(
            Transaction.user_id == user_id,
            Transaction.category == 'questionnaire',
            Transaction.transaction_date >= year_start,
            Transaction.transaction_date <= year_end
        ).delete()

        for answer in payload.answers:
            if answer.amount <= 0:
                continue

            rule = db.query(DeductionRule).filter(
                DeductionRule.deduction_code == answer.deduction_code
            ).first()

            if not rule:
                continue

            db.add(Transaction(
                user_id=user_id,
                transaction_date=datetime(payload.tax_year, 12, 31),
                merchant_name=rule.deduction_name,
                amount=answer.amount,
                deduction_code=answer.deduction_code,
                category='questionnaire',
                confidence_score=1.0
            ))

        db.commit()

        return compute_tax_summary(
            user_id, payload.tax_year, payload.entity_type,
            payload.officer_wages, db
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

# ============================================
# CSV UPLOAD ENDPOINT (existing, unchanged)
# ============================================

@app.post("/api/tax/upload-csv")
async def upload_csv(
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    try:
        contents = await file.read()
        text_io = io.StringIO(contents.decode('utf-8'))
        reader = csv.DictReader(text_io)

        if not reader.fieldnames:
            raise ValueError("CSV is empty")

        transactions = []
        for row_num, row in enumerate(reader, start=2):
            try:
                merchant = row.get('Description') or row.get('Merchant') or 'Unknown'
                amount_str = row.get('Amount') or row.get('amount') or '0'
                date_str = row.get('Date') or row.get('date') or ''

                try:
                    amount = Decimal(str(amount_str).replace('$', '').replace(',', ''))
                except:
                    amount = Decimal('0')

                if amount == 0:
                    continue

                try:
                    tx_date = datetime.strptime(date_str, '%m/%d/%Y').date()
                except:
                    try:
                        tx_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    except:
                        tx_date = datetime.now().date()

                cat = categorize_merchant(merchant, db)

                tx = Transaction(
                    user_id=user_id,
                    merchant_name=merchant,
                    amount=amount,
                    transaction_date=tx_date,
                    deduction_code=cat.get('code'),
                    category=cat.get('method'),
                    confidence_score=cat.get('confidence')
                )
                db.add(tx)
                transactions.append(tx)

            except Exception as e:
                print(f"Row {row_num} error: {e}")
                continue

        db.commit()

        return {
            'status': 'success',
            'count': len(transactions),
            'transactions': [
                {
                    'id': t.id,
                    'date': t.transaction_date.strftime('%Y-%m-%d') if t.transaction_date else '',
                    'merchant': t.merchant_name,
                    'amount': float(t.amount),
                    'category': t.category,
                    'confidence': t.confidence_score
                }
                for t in transactions[:50]
            ]
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

# ============================================
# CALCULATE ENDPOINT (existing, now uses shared function)
# ============================================

@app.post("/api/tax/calculate-deductions")
async def calculate(payload: CalculationRequest, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    try:
        return compute_tax_summary(
            user_id, payload.tax_year, payload.entity_type,
            payload.officer_wages, db
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

# ============================================
# BOOKKEEPING & EXPENSE MANAGEMENT
# Every endpoint here is scoped to Depends(get_current_user_id) — list/summary/
# export filter by it, and update/delete additionally re-check
# Transaction.user_id == user_id on the fetched row so one account can never
# read or modify another's ledger even by guessing a transaction id.
# ============================================

VALID_TRANSACTION_TYPES = ('income', 'expense')
VALID_CASH_FLOW_CATEGORIES = ('operating', 'investing', 'financing')

def parse_tx_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        raise HTTPException(status_code=400, detail="transaction_date must be in YYYY-MM-DD format.")

def serialize_transaction(t: Transaction) -> dict:
    return {
        'id': t.id,
        'date': t.transaction_date.strftime('%Y-%m-%d') if t.transaction_date else None,
        'merchant': t.merchant_name,
        'amount': float(t.amount),
        'transaction_type': t.transaction_type,
        'cash_flow_category': t.cash_flow_category,
        'deduction_code': t.deduction_code,
        'category': t.category,
        'description': t.description,
        'confidence': t.confidence_score,
        'source': t.bank_csv_source,
    }

@app.get("/api/bookkeeping/transactions")
async def list_bookkeeping_transactions(
    year: int | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    if year is not None:
        query = query.filter(extract('year', Transaction.transaction_date) == year)
    txs = query.order_by(Transaction.transaction_date.desc()).all()
    return {'transactions': [serialize_transaction(t) for t in txs]}

@app.post("/api/bookkeeping/transactions")
async def create_bookkeeping_transaction(
    payload: TransactionCreate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    if payload.transaction_type not in VALID_TRANSACTION_TYPES:
        raise HTTPException(status_code=400, detail="transaction_type must be 'income' or 'expense'.")
    if payload.cash_flow_category not in VALID_CASH_FLOW_CATEGORIES:
        raise HTTPException(status_code=400, detail="cash_flow_category must be 'operating', 'investing', or 'financing'.")

    tx = Transaction(
        user_id=user_id,
        transaction_date=parse_tx_date(payload.transaction_date),
        merchant_name=payload.merchant_name,
        amount=payload.amount,
        transaction_type=payload.transaction_type,
        cash_flow_category=payload.cash_flow_category,
        deduction_code=payload.deduction_code,
        description=payload.description,
        category='manual',
        confidence_score=1.0
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return {'status': 'success', 'transaction': serialize_transaction(tx)}

@app.put("/api/bookkeeping/transactions/{transaction_id}")
async def update_bookkeeping_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    tx = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == user_id
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found.")

    if payload.transaction_date is not None:
        tx.transaction_date = parse_tx_date(payload.transaction_date)
    if payload.merchant_name is not None:
        tx.merchant_name = payload.merchant_name
    if payload.amount is not None:
        tx.amount = payload.amount
    if payload.transaction_type is not None:
        if payload.transaction_type not in VALID_TRANSACTION_TYPES:
            raise HTTPException(status_code=400, detail="transaction_type must be 'income' or 'expense'.")
        tx.transaction_type = payload.transaction_type
    if payload.cash_flow_category is not None:
        if payload.cash_flow_category not in VALID_CASH_FLOW_CATEGORIES:
            raise HTTPException(status_code=400, detail="cash_flow_category must be 'operating', 'investing', or 'financing'.")
        tx.cash_flow_category = payload.cash_flow_category
    if payload.deduction_code is not None:
        tx.deduction_code = payload.deduction_code
    if payload.description is not None:
        tx.description = payload.description

    db.commit()
    db.refresh(tx)
    return {'status': 'success', 'transaction': serialize_transaction(tx)}

@app.delete("/api/bookkeeping/transactions/{transaction_id}")
async def delete_bookkeeping_transaction(
    transaction_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    tx = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == user_id
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found.")

    db.delete(tx)
    db.commit()
    return {'status': 'success'}

@app.get("/api/bookkeeping/summary")
async def bookkeeping_summary(
    year: int | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    if year is not None:
        query = query.filter(extract('year', Transaction.transaction_date) == year)
    txs = query.all()

    income = sum((t.amount for t in txs if t.transaction_type == 'income'), Decimal(0))
    expenses = sum((t.amount for t in txs if t.transaction_type == 'expense'), Decimal(0))

    return {
        'income': float(income),
        'expenses': float(expenses),
        'net': float(income - expenses),
        'transaction_count': len(txs)
    }

@app.get("/api/bookkeeping/export")
async def export_bookkeeping_csv(
    year: int | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    if year is not None:
        query = query.filter(extract('year', Transaction.transaction_date) == year)
    txs = query.order_by(Transaction.transaction_date).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Merchant', 'Type', 'Cash Flow Activity', 'Category', 'Amount', 'Description'])
    for t in txs:
        writer.writerow([
            t.transaction_date.strftime('%Y-%m-%d') if t.transaction_date else '',
            t.merchant_name,
            t.transaction_type,
            t.cash_flow_category,
            t.deduction_code or t.category or '',
            float(t.amount),
            t.description or ''
        ])

    filename = f"bookkeeping-{year or 'all'}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ============================================
# P&L STATEMENT
# Built entirely from the bookkeeping ledger above — same
# Depends(get_current_user_id) scoping, no new trust surface.
# ============================================

def compute_profit_and_loss(user_id: int, year: int | None, db: Session) -> dict:
    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    if year is not None:
        query = query.filter(extract('year', Transaction.transaction_date) == year)
    txs = query.all()

    rule_names = {r.deduction_code: r.deduction_name for r in db.query(DeductionRule).all()}

    revenue_total = Decimal(0)
    expense_total = Decimal(0)
    expense_by_category = {}

    for t in txs:
        if t.transaction_type == 'income':
            revenue_total += t.amount
        elif t.transaction_type == 'expense':
            expense_total += t.amount
            label = rule_names.get(t.deduction_code, 'Uncategorized') if t.deduction_code else 'Uncategorized'
            expense_by_category[label] = expense_by_category.get(label, Decimal(0)) + t.amount

    by_category = sorted(
        ({'label': k, 'total': float(v)} for k, v in expense_by_category.items()),
        key=lambda row: -row['total']
    )

    return {
        'year': year,
        'revenue': {'total': float(revenue_total)},
        'expenses': {'total': float(expense_total), 'by_category': by_category},
        'net_income': float(revenue_total - expense_total)
    }

@app.get("/api/reports/profit-and-loss")
async def profit_and_loss(
    year: int | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    return compute_profit_and_loss(user_id, year, db)

@app.get("/api/reports/profit-and-loss/export")
async def export_profit_and_loss_csv(
    year: int | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    report = compute_profit_and_loss(user_id, year, db)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([f"Profit & Loss Statement — {year or 'All Years'}"])
    writer.writerow([])
    writer.writerow(['Revenue', f"{report['revenue']['total']:.2f}"])
    writer.writerow([])
    writer.writerow(['Expenses'])
    for row in report['expenses']['by_category']:
        writer.writerow([f"  {row['label']}", f"{row['total']:.2f}"])
    writer.writerow(['Total Expenses', f"{report['expenses']['total']:.2f}"])
    writer.writerow([])
    writer.writerow(['Net Income', f"{report['net_income']:.2f}"])

    filename = f"profit-and-loss-{year or 'all'}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ============================================
# CASH FLOW STATEMENT
# Same ledger, same Depends(get_current_user_id) scoping as P&L. Every
# transaction nets its cash effect (+amount if income, -amount if expense)
# into whichever of the three activity sections it's tagged with
# (cash_flow_category — defaults to 'operating', reclassified on the
# transaction itself for asset purchases/sales or loan/equity/draw
# activity). This reports net cash flow BY activity for the period, not a
# beginning/ending bank balance — the app has no bank-balance data to
# report one honestly.
# ============================================

def compute_cash_flow(user_id: int, year: int | None, db: Session) -> dict:
    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    if year is not None:
        query = query.filter(extract('year', Transaction.transaction_date) == year)
    txs = query.all()

    net_by_category = {c: Decimal(0) for c in VALID_CASH_FLOW_CATEGORIES}

    for t in txs:
        if t.transaction_type == 'income':
            signed_amount = t.amount
        elif t.transaction_type == 'expense':
            signed_amount = -t.amount
        else:
            continue
        category = t.cash_flow_category if t.cash_flow_category in VALID_CASH_FLOW_CATEGORIES else 'operating'
        net_by_category[category] += signed_amount

    net_change_in_cash = sum(net_by_category.values(), Decimal(0))

    return {
        'year': year,
        'operating': float(net_by_category['operating']),
        'investing': float(net_by_category['investing']),
        'financing': float(net_by_category['financing']),
        'net_change_in_cash': float(net_change_in_cash)
    }

@app.get("/api/reports/cash-flow")
async def cash_flow(
    year: int | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    return compute_cash_flow(user_id, year, db)

@app.get("/api/reports/cash-flow/export")
async def export_cash_flow_csv(
    year: int | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    report = compute_cash_flow(user_id, year, db)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([f"Cash Flow Statement — {year or 'All Years'}"])
    writer.writerow([])
    writer.writerow(['Net Cash from Operating Activities', f"{report['operating']:.2f}"])
    writer.writerow(['Net Cash from Investing Activities', f"{report['investing']:.2f}"])
    writer.writerow(['Net Cash from Financing Activities', f"{report['financing']:.2f}"])
    writer.writerow([])
    writer.writerow(['Net Change in Cash', f"{report['net_change_in_cash']:.2f}"])

    filename = f"cash-flow-{year or 'all'}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ============================================
# CLIENT PORTAL & DOCUMENT COLLABORATION HUB
# ============================================
# Lets a business/consumer account holder ("owner") share documents with an
# outside client who never signs up for an account — the client reaches
# their own documents through an unguessable magic-link token instead of a
# login. Everything under /api/portal/public/* is scoped ONLY by that
# token; it must never accept a client_id, user_id, or document_id from the
# caller without first checking it belongs to that token's client.

def get_owned_client(client_id: int, user_id: int, db: Session) -> PortalClient:
    client = db.query(PortalClient).filter(
        PortalClient.id == client_id, PortalClient.user_id == user_id
    ).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found.")
    return client

def get_owned_document(document_id: int, user_id: int, db: Session) -> PortalDocument:
    doc = db.query(PortalDocument).filter(
        PortalDocument.id == document_id, PortalDocument.user_id == user_id
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return doc

def get_active_client_by_token(token: str, db: Session) -> PortalClient:
    client = db.query(PortalClient).filter(
        PortalClient.portal_token == token, PortalClient.is_active == True
    ).first()
    if not client:
        raise HTTPException(status_code=404, detail="This portal link is invalid or has been revoked.")
    return client

def serialize_portal_document(doc: PortalDocument) -> dict:
    return {
        'id': doc.id,
        'filename': doc.filename,
        'content_type': doc.content_type,
        'file_size': doc.file_size,
        'uploaded_by': doc.uploaded_by,
        'created_at': doc.created_at.isoformat() if doc.created_at else None,
    }

def serialize_portal_comment(comment: PortalComment) -> dict:
    return {
        'id': comment.id,
        'author': comment.author,
        'body': comment.body,
        'created_at': comment.created_at.isoformat() if comment.created_at else None,
    }

def sanitize_portal_filename(filename: str) -> str:
    """Strip control characters (including CR/LF, which could otherwise be
    smuggled into the Content-Disposition header via a crafted upload) from
    a client- or owner-supplied filename before it's stored or served."""
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', filename).strip()
    return cleaned or 'document'

def content_disposition_attachment(filename: str) -> str:
    """Build a Content-Disposition header value that's safe for any
    filename. Header values are Latin-1 only, so a plain filename="..."
    with e.g. CJK characters or an emoji raises UnicodeEncodeError at
    response time — encode it as filename* per RFC 5987 instead, with an
    ASCII-safe filename= as a fallback for older clients."""
    ascii_fallback = filename.encode('ascii', 'replace').decode('ascii').replace('?', '_')
    encoded = urllib.parse.quote(filename, safe='')
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'

async def read_portal_upload(file: UploadFile) -> bytes:
    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(contents) > MAX_PORTAL_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large — max {MAX_PORTAL_FILE_SIZE // (1024 * 1024)}MB."
        )
    return contents

def portal_download_response(doc: PortalDocument) -> StreamingResponse:
    # Always served as an attachment — a browser never renders an uploaded
    # file inline under our origin, which would let a malicious HTML/SVG
    # upload run script against a signed-in owner's or client's session.
    return StreamingResponse(
        iter([doc.file_data]),
        media_type=doc.content_type or 'application/octet-stream',
        headers={"Content-Disposition": content_disposition_attachment(doc.filename)}
    )

# --- Owner-authenticated endpoints ---

@app.post("/api/portal/clients")
async def create_portal_client(
    payload: PortalClientCreate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    client = PortalClient(
        user_id=user_id,
        name=payload.name,
        email=payload.email,
        notes=payload.notes,
        portal_token=secrets.token_urlsafe(32),
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return {
        'id': client.id,
        'name': client.name,
        'email': client.email,
        'notes': client.notes,
        'portal_token': client.portal_token,
        'is_active': client.is_active,
        'created_at': client.created_at.isoformat(),
        'document_count': 0,
    }

@app.get("/api/portal/clients")
async def list_portal_clients(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    clients = db.query(PortalClient).filter(
        PortalClient.user_id == user_id
    ).order_by(PortalClient.created_at.desc()).all()

    counts = dict(
        db.query(PortalDocument.client_id, func.count(PortalDocument.id))
        .filter(PortalDocument.user_id == user_id)
        .group_by(PortalDocument.client_id)
        .all()
    )

    return {
        'clients': [
            {
                'id': c.id,
                'name': c.name,
                'email': c.email,
                'notes': c.notes,
                'portal_token': c.portal_token,
                'is_active': c.is_active,
                'created_at': c.created_at.isoformat() if c.created_at else None,
                'document_count': counts.get(c.id, 0),
            }
            for c in clients
        ]
    }

@app.get("/api/portal/clients/{client_id}")
async def get_portal_client(client_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    client = get_owned_client(client_id, user_id, db)
    documents = db.query(PortalDocument).filter(
        PortalDocument.client_id == client.id, PortalDocument.user_id == user_id
    ).order_by(PortalDocument.created_at.desc()).all()

    return {
        'id': client.id,
        'name': client.name,
        'email': client.email,
        'notes': client.notes,
        'portal_token': client.portal_token,
        'is_active': client.is_active,
        'created_at': client.created_at.isoformat() if client.created_at else None,
        'documents': [serialize_portal_document(d) for d in documents],
    }

@app.post("/api/portal/clients/{client_id}/regenerate-link")
async def regenerate_portal_link(client_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """Invalidate the old magic link and issue a new one — e.g. if the old
    link was shared with the wrong person by mistake."""
    client = get_owned_client(client_id, user_id, db)
    client.portal_token = secrets.token_urlsafe(32)
    db.commit()
    return {'status': 'success', 'portal_token': client.portal_token}

@app.post("/api/portal/clients/{client_id}/revoke")
async def revoke_portal_client(client_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    """Disable the portal link without deleting the client's document history."""
    client = get_owned_client(client_id, user_id, db)
    client.is_active = False
    db.commit()
    return {'status': 'success'}

@app.post("/api/portal/clients/{client_id}/reactivate")
async def reactivate_portal_client(client_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    client = get_owned_client(client_id, user_id, db)
    client.is_active = True
    db.commit()
    return {'status': 'success'}

@app.post("/api/portal/clients/{client_id}/documents")
async def upload_portal_document_as_owner(
    client_id: int,
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    client = get_owned_client(client_id, user_id, db)
    contents = await read_portal_upload(file)

    doc = PortalDocument(
        user_id=user_id,
        client_id=client.id,
        filename=sanitize_portal_filename(file.filename or 'document'),
        content_type=file.content_type,
        file_size=len(contents),
        file_data=contents,
        uploaded_by='owner',
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return serialize_portal_document(doc)

@app.get("/api/portal/documents/{document_id}/download")
async def download_portal_document_as_owner(document_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    doc = get_owned_document(document_id, user_id, db)
    return portal_download_response(doc)

@app.delete("/api/portal/documents/{document_id}")
async def delete_portal_document(document_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    doc = get_owned_document(document_id, user_id, db)
    db.query(PortalComment).filter(PortalComment.document_id == doc.id).delete()
    db.delete(doc)
    db.commit()
    return {'status': 'success'}

@app.get("/api/portal/documents/{document_id}/comments")
async def list_portal_comments_as_owner(document_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    doc = get_owned_document(document_id, user_id, db)
    comments = db.query(PortalComment).filter(
        PortalComment.document_id == doc.id
    ).order_by(PortalComment.created_at.asc()).all()
    return {'comments': [serialize_portal_comment(c) for c in comments]}

@app.post("/api/portal/documents/{document_id}/comments")
async def add_portal_comment_as_owner(
    document_id: int,
    payload: PortalCommentCreate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    doc = get_owned_document(document_id, user_id, db)
    comment = PortalComment(document_id=doc.id, author='owner', body=payload.body)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return serialize_portal_comment(comment)

# --- Public, token-scoped endpoints (no login) ---

@app.get("/api/portal/public/{token}")
async def get_public_portal(token: str, db: Session = Depends(get_db)):
    client = get_active_client_by_token(token, db)
    documents = db.query(PortalDocument).filter(PortalDocument.client_id == client.id).order_by(
        PortalDocument.created_at.desc()
    ).all()
    return {
        'client_name': client.name,
        'documents': [serialize_portal_document(d) for d in documents],
    }

@app.get("/api/portal/public/{token}/documents/{document_id}/download")
async def download_public_portal_document(token: str, document_id: int, db: Session = Depends(get_db)):
    client = get_active_client_by_token(token, db)
    doc = db.query(PortalDocument).filter(
        PortalDocument.id == document_id, PortalDocument.client_id == client.id
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return portal_download_response(doc)

@app.post("/api/portal/public/{token}/documents")
async def upload_public_portal_document(token: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    client = get_active_client_by_token(token, db)
    contents = await read_portal_upload(file)

    doc = PortalDocument(
        user_id=client.user_id,
        client_id=client.id,
        filename=sanitize_portal_filename(file.filename or 'document'),
        content_type=file.content_type,
        file_size=len(contents),
        file_data=contents,
        uploaded_by='client',
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return serialize_portal_document(doc)

@app.get("/api/portal/public/{token}/documents/{document_id}/comments")
async def list_public_portal_comments(token: str, document_id: int, db: Session = Depends(get_db)):
    client = get_active_client_by_token(token, db)
    doc = db.query(PortalDocument).filter(
        PortalDocument.id == document_id, PortalDocument.client_id == client.id
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    comments = db.query(PortalComment).filter(
        PortalComment.document_id == doc.id
    ).order_by(PortalComment.created_at.asc()).all()
    return {'comments': [serialize_portal_comment(c) for c in comments]}

@app.post("/api/portal/public/{token}/documents/{document_id}/comments")
async def add_public_portal_comment(token: str, document_id: int, payload: PortalCommentCreate, db: Session = Depends(get_db)):
    client = get_active_client_by_token(token, db)
    doc = db.query(PortalDocument).filter(
        PortalDocument.id == document_id, PortalDocument.client_id == client.id
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    comment = PortalComment(document_id=doc.id, author='client', body=payload.body)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return serialize_portal_comment(comment)

# ============================================
# SCHEDULING & BOOKING LINKS
# ============================================
# Lets an account holder ("owner") publish weekly availability and get a
# public booking link — a visitor picks a real open slot and books it with
# no account of their own, the same no-login pattern as the Client Portal.
# Everything is stored in UTC; the owner's stored timezone converts their
# weekly rules into concrete slots, and the guest's browser converts those
# UTC instants into their own local time for display.

MAX_BOOKING_WINDOW_DAYS = 21

def generate_booking_slug(user: User) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', f"{user.first_name}-{user.last_name}".lower()).strip('-') or 'book'
    suffix = secrets.token_hex(3)
    return f"{base}-{suffix}"[:64]

def get_unique_booking_slug(user: User, db: Session) -> str:
    for _ in range(5):
        slug = generate_booking_slug(user)
        if not db.query(SchedulingSettings).filter(SchedulingSettings.booking_slug == slug).first():
            return slug
    return secrets.token_hex(8)

def get_or_create_scheduling_settings(user_id: int, db: Session) -> SchedulingSettings:
    settings = db.query(SchedulingSettings).filter(SchedulingSettings.user_id == user_id).first()
    if settings:
        return settings
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    settings = SchedulingSettings(user_id=user_id, booking_slug=get_unique_booking_slug(user, db))
    db.add(settings)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent first-time requests (e.g. a double-click, or two
        # tabs loading settings at once) can both reach here and both try to
        # insert — the unique constraint on user_id rejects the loser. That's
        # not an error case, it just means the winner's row is the answer.
        db.rollback()
        settings = db.query(SchedulingSettings).filter(SchedulingSettings.user_id == user_id).first()
        if not settings:
            raise
        return settings
    db.refresh(settings)
    return settings

def parse_hhmm(value: str) -> time_type:
    try:
        hour_str, minute_str = value.split(':')
        return time_type(int(hour_str), int(minute_str))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid time '{value}' — expected HH:MM.")

def serialize_settings(settings: SchedulingSettings, rules: list) -> dict:
    return {
        'timezone': settings.timezone,
        'meeting_duration_minutes': settings.meeting_duration_minutes,
        'buffer_minutes': settings.buffer_minutes,
        'min_notice_hours': settings.min_notice_hours,
        'booking_slug': settings.booking_slug,
        'is_active': settings.is_active,
        'availability': [
            {'day_of_week': r.day_of_week, 'start_time': r.start_time.strftime('%H:%M'), 'end_time': r.end_time.strftime('%H:%M')}
            for r in rules
        ],
    }

def serialize_booking(b: Booking) -> dict:
    return {
        'id': b.id,
        'guest_name': b.guest_name,
        'guest_email': b.guest_email,
        'notes': b.notes,
        'start_at': b.start_at.replace(tzinfo=timezone.utc).isoformat(),
        'end_at': b.end_at.replace(tzinfo=timezone.utc).isoformat(),
        'status': b.status,
        'created_at': b.created_at.isoformat() if b.created_at else None,
    }

def compute_available_slots(settings: SchedulingSettings, db: Session) -> list[dict]:
    """Weekly availability rules (in the owner's timezone) minus existing
    confirmed bookings and the minimum-notice window, projected forward
    MAX_BOOKING_WINDOW_DAYS days. Returns UTC instants."""
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo('UTC')

    rules = db.query(AvailabilityRule).filter(AvailabilityRule.user_id == settings.user_id).all()
    rules_by_day = {}
    for r in rules:
        rules_by_day.setdefault(r.day_of_week, []).append(r)

    now_utc = datetime.now(timezone.utc)
    earliest_bookable = now_utc + timedelta(hours=settings.min_notice_hours)
    window_end = now_utc + timedelta(days=MAX_BOOKING_WINDOW_DAYS)

    existing = db.query(Booking).filter(
        Booking.user_id == settings.user_id,
        Booking.status == 'confirmed',
        Booking.end_at >= now_utc.replace(tzinfo=None),
    ).all()
    existing_intervals = [
        (b.start_at.replace(tzinfo=timezone.utc), b.end_at.replace(tzinfo=timezone.utc))
        for b in existing
    ]
    existing_intervals.extend(get_external_busy_intervals(settings, now_utc, window_end, db))

    duration = timedelta(minutes=settings.meeting_duration_minutes)
    step = timedelta(minutes=settings.meeting_duration_minutes + settings.buffer_minutes)

    slots = []
    today_local = now_utc.astimezone(tz).date()
    for day_offset in range(MAX_BOOKING_WINDOW_DAYS):
        day_local = today_local + timedelta(days=day_offset)
        for rule in rules_by_day.get(day_local.weekday(), []):
            slot_start_local = datetime.combine(day_local, rule.start_time, tzinfo=tz)
            day_end_local = datetime.combine(day_local, rule.end_time, tzinfo=tz)

            while slot_start_local + duration <= day_end_local:
                slot_start_utc = slot_start_local.astimezone(timezone.utc)

                # A wall-clock time that doesn't exist (the "spring forward"
                # DST gap, e.g. 2:30 AM on the day US clocks skip from 2 to
                # 3) still constructs without error — Python silently
                # resolves it using the pre-transition offset. Round-tripping
                # back to local time exposes that mismatch so we can skip
                # it, instead of offering a slot an hour off from what the
                # owner's own calendar would show.
                if slot_start_utc.astimezone(tz).replace(tzinfo=None) != slot_start_local.replace(tzinfo=None):
                    slot_start_local += step
                    continue

                slot_end_utc = slot_start_utc + duration
                if earliest_bookable <= slot_start_utc <= window_end:
                    overlaps = any(slot_start_utc < be and slot_end_utc > bs for bs, be in existing_intervals)
                    if not overlaps:
                        slots.append({'start_at': slot_start_utc.isoformat(), 'end_at': slot_end_utc.isoformat()})

                slot_start_local += step

    slots.sort(key=lambda s: s['start_at'])
    return slots

# --- Owner-authenticated endpoints ---

@app.get("/api/scheduling/settings")
async def get_scheduling_settings_endpoint(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    settings = get_or_create_scheduling_settings(user_id, db)
    rules = db.query(AvailabilityRule).filter(AvailabilityRule.user_id == user_id).order_by(
        AvailabilityRule.day_of_week, AvailabilityRule.start_time
    ).all()
    return serialize_settings(settings, rules)

@app.put("/api/scheduling/settings")
async def update_scheduling_settings(
    payload: SchedulingSettingsUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    if payload.timezone not in available_timezones():
        raise HTTPException(status_code=400, detail="Unrecognized timezone.")

    settings = get_or_create_scheduling_settings(user_id, db)
    settings.timezone = payload.timezone
    settings.meeting_duration_minutes = payload.meeting_duration_minutes
    settings.buffer_minutes = payload.buffer_minutes
    settings.min_notice_hours = payload.min_notice_hours
    settings.is_active = payload.is_active
    db.commit()

    rules = db.query(AvailabilityRule).filter(AvailabilityRule.user_id == user_id).order_by(
        AvailabilityRule.day_of_week, AvailabilityRule.start_time
    ).all()
    return serialize_settings(settings, rules)

@app.post("/api/scheduling/settings/regenerate-link")
async def regenerate_booking_link(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    settings = get_or_create_scheduling_settings(user_id, db)
    user = db.query(User).filter(User.id == user_id).first()
    settings.booking_slug = get_unique_booking_slug(user, db)
    db.commit()
    return {'status': 'success', 'booking_slug': settings.booking_slug}

@app.put("/api/scheduling/availability")
async def set_availability(
    payload: AvailabilityRulesUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    parsed = []
    for r in payload.rules:
        start = parse_hhmm(r.start_time)
        end = parse_hhmm(r.end_time)
        if start >= end:
            raise HTTPException(status_code=400, detail="Each day's start time must be before its end time.")
        parsed.append((r.day_of_week, start, end))

    get_or_create_scheduling_settings(user_id, db)  # ensure settings row exists
    db.query(AvailabilityRule).filter(AvailabilityRule.user_id == user_id).delete()
    for day_of_week, start, end in parsed:
        db.add(AvailabilityRule(user_id=user_id, day_of_week=day_of_week, start_time=start, end_time=end))
    db.commit()
    return {'status': 'success'}

@app.get("/api/scheduling/bookings")
async def list_scheduling_bookings(
    include_past: bool = False,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    query = db.query(Booking).filter(Booking.user_id == user_id, Booking.status == 'confirmed')
    if not include_past:
        query = query.filter(Booking.end_at >= datetime.utcnow())
    bookings = query.order_by(Booking.start_at.asc()).all()
    return {'bookings': [serialize_booking(b) for b in bookings]}

@app.post("/api/scheduling/bookings/{booking_id}/cancel")
async def cancel_scheduling_booking(booking_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    booking = db.query(Booking).filter(Booking.id == booking_id, Booking.user_id == user_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")
    booking.status = 'cancelled'
    db.commit()
    return {'status': 'success'}

# --- Public, slug-scoped endpoints (no login) ---

@app.get("/api/scheduling/public/{slug}")
async def get_public_scheduling_page(slug: str, db: Session = Depends(get_db)):
    settings = db.query(SchedulingSettings).filter(
        SchedulingSettings.booking_slug == slug, SchedulingSettings.is_active == True
    ).first()
    if not settings:
        raise HTTPException(status_code=404, detail="This booking link is invalid or has been disabled.")

    user = db.query(User).filter(User.id == settings.user_id).first()
    owner_name = ' '.join(p for p in [user.first_name, user.last_name] if p) if user else ''
    owner_name = owner_name or 'BlissPoint Access'

    return {
        'owner_name': owner_name,
        'timezone': settings.timezone,
        'meeting_duration_minutes': settings.meeting_duration_minutes,
        'slots': compute_available_slots(settings, db),
    }

@app.post("/api/scheduling/public/{slug}/book")
async def book_public_slot(slug: str, payload: BookingCreate, db: Session = Depends(get_db)):
    # Locks the settings row for the duration of this transaction so two
    # concurrent booking requests for the same owner can't both pass the
    # availability check for the same slot — the second one blocks until
    # the first commits, then re-checks and correctly sees it's taken.
    settings = db.query(SchedulingSettings).filter(
        SchedulingSettings.booking_slug == slug, SchedulingSettings.is_active == True
    ).with_for_update().first()
    if not settings:
        raise HTTPException(status_code=404, detail="This booking link is invalid or has been disabled.")

    try:
        start_at = datetime.fromisoformat(payload.start_at.replace('Z', '+00:00'))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start time.")
    if start_at.tzinfo is None:
        raise HTTPException(status_code=400, detail="start_at must include a timezone offset.")

    start_at_utc = start_at.astimezone(timezone.utc)
    end_at_utc = start_at_utc + timedelta(minutes=settings.meeting_duration_minutes)

    valid_starts = {slot['start_at'] for slot in compute_available_slots(settings, db)}
    if start_at_utc.isoformat() not in valid_starts:
        raise HTTPException(status_code=409, detail="That time is no longer available. Please choose another.")

    booking = Booking(
        user_id=settings.user_id,
        guest_name=payload.guest_name,
        guest_email=payload.guest_email,
        notes=payload.notes,
        start_at=start_at_utc.replace(tzinfo=None),
        end_at=end_at_utc.replace(tzinfo=None),
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return serialize_booking(booking)

# ============================================
# CALENDAR SYNC (Google / Microsoft — read-only busy-time blocking)
# ============================================
# An owner connects an outside calendar via OAuth; we only ever read its
# busy/free time (never create, edit, or delete anything on it) and
# subtract those busy blocks from compute_available_slots, so the booking
# page can't offer a time the owner is actually unavailable for elsewhere.
#
# All of GOOGLE_CLIENT_ID/SECRET, MICROSOFT_CLIENT_ID/SECRET, and
# CALENDAR_TOKEN_ENCRYPTION_KEY are optional at startup on purpose — this
# feature ships disabled until real OAuth app credentials exist, without
# taking the rest of the API down for everything else in the meantime.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET")
MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CALENDAR_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CALENDAR_CLIENT_SECRET")
CALENDAR_TOKEN_ENCRYPTION_KEY = os.environ.get("CALENDAR_TOKEN_ENCRYPTION_KEY")

# Must exactly match what's registered as the OAuth redirect URI with each
# provider, and where the browser lands after a successful/failed connect.
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000")

GOOGLE_REDIRECT_URI = f"{BACKEND_BASE_URL}/api/scheduling/calendar/callback/google"
MICROSOFT_REDIRECT_URI = f"{BACKEND_BASE_URL}/api/scheduling/calendar/callback/microsoft"


def get_token_fernet() -> Fernet:
    if not CALENDAR_TOKEN_ENCRYPTION_KEY:
        raise HTTPException(status_code=503, detail="Calendar sync is not configured on this server yet.")
    try:
        return Fernet(CALENDAR_TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Calendar sync is misconfigured on this server.")

def encrypt_token(value: str) -> str:
    return get_token_fernet().encrypt(value.encode()).decode()

def decrypt_token(value: str) -> str:
    return get_token_fernet().decrypt(value.encode()).decode()

def upsert_calendar_connection(
    user_id: int, provider: str, email: str | None,
    access_token: str, refresh_token: str, expires_in: int, db: Session
) -> None:
    connection = db.query(CalendarConnection).filter(
        CalendarConnection.user_id == user_id, CalendarConnection.provider == provider
    ).first()
    if not connection:
        connection = CalendarConnection(user_id=user_id, provider=provider)
        db.add(connection)
    connection.provider_email = email
    connection.access_token_encrypted = encrypt_token(access_token)
    connection.refresh_token_encrypted = encrypt_token(refresh_token)
    connection.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    db.commit()

def get_valid_google_access_token(connection: CalendarConnection, db: Session) -> str:
    if connection.token_expires_at > datetime.utcnow() + timedelta(minutes=2):
        return decrypt_token(connection.access_token_encrypted)

    resp = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'refresh_token': decrypt_token(connection.refresh_token_encrypted),
        'grant_type': 'refresh_token',
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    connection.access_token_encrypted = encrypt_token(data['access_token'])
    connection.token_expires_at = datetime.utcnow() + timedelta(seconds=data.get('expires_in', 3600))
    db.commit()
    return data['access_token']

def get_valid_microsoft_access_token(connection: CalendarConnection, db: Session) -> str:
    if connection.token_expires_at > datetime.utcnow() + timedelta(minutes=2):
        return decrypt_token(connection.access_token_encrypted)

    resp = requests.post('https://login.microsoftonline.com/common/oauth2/v2.0/token', data={
        'client_id': MICROSOFT_CLIENT_ID,
        'client_secret': MICROSOFT_CLIENT_SECRET,
        'refresh_token': decrypt_token(connection.refresh_token_encrypted),
        'grant_type': 'refresh_token',
        'scope': 'offline_access Calendars.Read User.Read',
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    connection.access_token_encrypted = encrypt_token(data['access_token'])
    # Microsoft rotates refresh tokens on use; if a new one comes back, the
    # old one stops working, so we must store the replacement or the next
    # refresh attempt fails with an unrecoverable invalid_grant.
    if data.get('refresh_token'):
        connection.refresh_token_encrypted = encrypt_token(data['refresh_token'])
    connection.token_expires_at = datetime.utcnow() + timedelta(seconds=data.get('expires_in', 3600))
    db.commit()
    return data['access_token']

def fetch_google_busy_intervals(connection: CalendarConnection, time_min: datetime, time_max: datetime, db: Session) -> list[tuple[datetime, datetime]]:
    try:
        access_token = get_valid_google_access_token(connection, db)
        resp = requests.post(
            'https://www.googleapis.com/calendar/v3/freeBusy',
            headers={'Authorization': f'Bearer {access_token}'},
            json={
                'timeMin': time_min.isoformat(),
                'timeMax': time_max.isoformat(),
                'items': [{'id': 'primary'}],
            },
            timeout=10,
        )
        resp.raise_for_status()
        busy = resp.json().get('calendars', {}).get('primary', {}).get('busy', [])
        return [
            (
                datetime.fromisoformat(b['start'].replace('Z', '+00:00')),
                datetime.fromisoformat(b['end'].replace('Z', '+00:00')),
            )
            for b in busy
        ]
    except Exception:
        # A connected calendar that's temporarily unreachable (expired grant,
        # network hiccup, provider outage) shouldn't take the booking page
        # down for every visitor — worst case we show a slot the owner would
        # rather have hidden, not a 500.
        return []

def fetch_microsoft_busy_intervals(connection: CalendarConnection, time_min: datetime, time_max: datetime, db: Session) -> list[tuple[datetime, datetime]]:
    # getSchedule's "schedules" array takes real mailbox addresses, not the
    # "me" path-segment convention used elsewhere in Graph — there's no
    # valid value to send without a known address, so degrade to no busy
    # data rather than sending a request that looks plausible but silently
    # queries the wrong (or no) mailbox.
    if not connection.provider_email:
        return []

    try:
        access_token = get_valid_microsoft_access_token(connection, db)
        resp = requests.post(
            'https://graph.microsoft.com/v1.0/me/calendar/getSchedule',
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
            json={
                'schedules': [connection.provider_email],
                'startTime': {'dateTime': time_min.strftime('%Y-%m-%dT%H:%M:%S'), 'timeZone': 'UTC'},
                'endTime': {'dateTime': time_max.strftime('%Y-%m-%dT%H:%M:%S'), 'timeZone': 'UTC'},
                'availabilityViewInterval': 30,
            },
            timeout=10,
        )
        resp.raise_for_status()
        schedules = resp.json().get('value', [])
        intervals = []
        for schedule in schedules:
            for item in schedule.get('scheduleItems', []):
                if item.get('status') == 'free':
                    continue
                start = datetime.fromisoformat(item['start']['dateTime']).replace(tzinfo=timezone.utc)
                end = datetime.fromisoformat(item['end']['dateTime']).replace(tzinfo=timezone.utc)
                intervals.append((start, end))
        return intervals
    except Exception:
        return []

def get_external_busy_intervals(settings: SchedulingSettings, time_min: datetime, time_max: datetime, db: Session) -> list[tuple[datetime, datetime]]:
    if not CALENDAR_TOKEN_ENCRYPTION_KEY:
        return []
    connections = db.query(CalendarConnection).filter(CalendarConnection.user_id == settings.user_id).all()
    intervals = []
    for connection in connections:
        if connection.provider == 'google':
            intervals.extend(fetch_google_busy_intervals(connection, time_min, time_max, db))
        elif connection.provider == 'microsoft':
            intervals.extend(fetch_microsoft_busy_intervals(connection, time_min, time_max, db))
    return intervals

@app.get("/api/scheduling/calendar/providers")
async def get_calendar_providers(user_id: int = Depends(get_current_user_id)):
    return {
        'google': bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and CALENDAR_TOKEN_ENCRYPTION_KEY),
        'microsoft': bool(MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET and CALENDAR_TOKEN_ENCRYPTION_KEY),
    }

@app.get("/api/scheduling/calendar/connections")
async def list_calendar_connections(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    connections = db.query(CalendarConnection).filter(CalendarConnection.user_id == user_id).all()
    return {
        'connections': [
            {
                'id': c.id,
                'provider': c.provider,
                'provider_email': c.provider_email,
                'connected_at': c.created_at.isoformat() if c.created_at else None,
            }
            for c in connections
        ]
    }

@app.delete("/api/scheduling/calendar/connections/{connection_id}")
async def disconnect_calendar(connection_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    connection = db.query(CalendarConnection).filter(
        CalendarConnection.id == connection_id, CalendarConnection.user_id == user_id
    ).first()
    if not connection:
        raise HTTPException(status_code=404, detail="Calendar connection not found.")
    db.delete(connection)
    db.commit()
    return {'status': 'success'}

@app.get("/api/scheduling/calendar/connect/google")
async def connect_google_calendar(user_id: int = Depends(get_current_user_id)):
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and CALENDAR_TOKEN_ENCRYPTION_KEY):
        raise HTTPException(status_code=503, detail="Google Calendar sync is not configured on this server yet.")
    state = create_oauth_state_token(user_id, 'google')
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'openid email https://www.googleapis.com/auth/calendar.freebusy',
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
    }
    return {'authorization_url': f'https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}'}

@app.get("/api/scheduling/calendar/callback/google")
async def google_calendar_callback(
    code: str | None = None, state: str | None = None, error: str | None = None,
    db: Session = Depends(get_db)
):
    if error or not code or not state:
        return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=google')

    try:
        user_id = verify_oauth_state_token(state, 'google')
    except HTTPException:
        # A raw 400 here would land a real user (whose consent screen took
        # too long, or who double-clicked "Connect" and used a stale link)
        # on a bare error page instead of back in the app — every other
        # failure in this flow redirects with calendar_error, so this
        # should too.
        return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=google')

    try:
        token_resp = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': GOOGLE_REDIRECT_URI,
        }, timeout=10)
        token_resp.raise_for_status()
        token_data = token_resp.json()

        if 'refresh_token' not in token_data:
            # We always pass prompt=consent above specifically so Google
            # always issues one — if it's still missing, something's wrong
            # enough that we shouldn't save a connection we can never renew.
            return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=google')

        userinfo_resp = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f"Bearer {token_data['access_token']}"}, timeout=10
        )
        userinfo_resp.raise_for_status()
        email = userinfo_resp.json().get('email')

        upsert_calendar_connection(
            user_id, 'google', email,
            token_data['access_token'], token_data['refresh_token'],
            token_data.get('expires_in', 3600), db
        )
    except Exception:
        return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=google')

    return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_connected=google')

@app.get("/api/scheduling/calendar/connect/microsoft")
async def connect_microsoft_calendar(user_id: int = Depends(get_current_user_id)):
    if not (MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET and CALENDAR_TOKEN_ENCRYPTION_KEY):
        raise HTTPException(status_code=503, detail="Microsoft Calendar sync is not configured on this server yet.")
    state = create_oauth_state_token(user_id, 'microsoft')
    params = {
        'client_id': MICROSOFT_CLIENT_ID,
        'redirect_uri': MICROSOFT_REDIRECT_URI,
        'response_type': 'code',
        'response_mode': 'query',
        'scope': 'offline_access Calendars.Read User.Read',
        'state': state,
    }
    return {'authorization_url': f'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}'}

@app.get("/api/scheduling/calendar/callback/microsoft")
async def microsoft_calendar_callback(
    code: str | None = None, state: str | None = None, error: str | None = None,
    db: Session = Depends(get_db)
):
    if error or not code or not state:
        return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=microsoft')

    try:
        user_id = verify_oauth_state_token(state, 'microsoft')
    except HTTPException:
        return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=microsoft')

    try:
        token_resp = requests.post('https://login.microsoftonline.com/common/oauth2/v2.0/token', data={
            'client_id': MICROSOFT_CLIENT_ID,
            'client_secret': MICROSOFT_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': MICROSOFT_REDIRECT_URI,
            'scope': 'offline_access Calendars.Read User.Read',
        }, timeout=10)
        token_resp.raise_for_status()
        token_data = token_resp.json()

        if 'refresh_token' not in token_data:
            return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=microsoft')

        me_resp = requests.get(
            'https://graph.microsoft.com/v1.0/me',
            headers={'Authorization': f"Bearer {token_data['access_token']}"}, timeout=10
        )
        me_resp.raise_for_status()
        me = me_resp.json()
        email = me.get('mail') or me.get('userPrincipalName')

        upsert_calendar_connection(
            user_id, 'microsoft', email,
            token_data['access_token'], token_data['refresh_token'],
            token_data.get('expires_in', 3600), db
        )
    except Exception:
        return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_error=microsoft')

    return RedirectResponse(f'{FRONTEND_BASE_URL}/tools/scheduling?calendar_connected=microsoft')

# ============================================
# HEALTH
# ============================================

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/health/ready")
async def ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        count = db.query(DeductionRule).count()
        return {"ready": True, "deductions": count}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
