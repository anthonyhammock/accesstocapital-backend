from fastapi import FastAPI, Depends, HTTPException, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, extract
from pydantic import BaseModel, Field
from decimal import Decimal
import csv
import io
import json
from datetime import datetime
import os
import re
import uuid

from app.database import get_db, engine
from app.models import Base, DeductionRule, CategorizationRule, Transaction, TaxSummary, User, ConsumerAccount, BusinessAccount
from app.auth import create_access_token, get_current_user_id
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
