from fastapi import FastAPI, Depends, HTTPException, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text, extract
from pydantic import BaseModel
from decimal import Decimal
import csv
import io
import json
from datetime import datetime
import os
import re

from app.database import get_db, engine
from app.models import Base, DeductionRule, CategorizationRule, Transaction, TaxSummary, User, ConsumerAccount, BusinessAccount
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

class TransactionInput(BaseModel):
    merchant_name: str
    amount: Decimal
    transaction_date: str
    deduction_code: str = None

class CalculationRequest(BaseModel):
    user_id: int
    tax_year: int
    entity_type: str
    transactions: list = []
    officer_wages: Decimal = Decimal(0)

class QuestionnaireAnswer(BaseModel):
    deduction_code: str
    amount: Decimal = Decimal(0)

class QuestionnaireSubmission(BaseModel):
    user_id: int
    tax_year: int
    entity_type: str
    answers: list[QuestionnaireAnswer]
    officer_wages: Decimal = Decimal(0)

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
        for i in range(1, ACCOUNTS_PER_BUREAU_SET + 1):
            db.add(BusinessAccount(
                user_id=user.id,
                business_name=business_name,
                current_balance=Decimal(0),
                reported_to_bureaus=False
            ))

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
        'user': {
            'id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'account_type': user.account_type
        }
    }


@app.get("/api/consumer-accounts")
async def get_consumer_accounts(user_id: int, db: Session = Depends(get_db)):
    accounts = db.query(ConsumerAccount).filter(ConsumerAccount.user_id == user_id).all()
    return {
        'accounts': [
            {
                'id': a.id,
                'account_name': a.account_name,
                'credit_limit': float(a.credit_limit) if a.credit_limit is not None else None,
                'current_balance': float(a.current_balance) if a.current_balance is not None else None,
                'payment_status': a.payment_status
            }
            for a in accounts
        ]
    }

@app.get("/api/business-accounts")
async def get_business_accounts(user_id: int, db: Session = Depends(get_db)):
    accounts = db.query(BusinessAccount).filter(BusinessAccount.user_id == user_id).all()
    return {
        'accounts': [
            {
                'id': a.id,
                'business_name': a.business_name,
                'ein': a.ein,
                'credit_limit': float(a.credit_limit) if a.credit_limit is not None else None,
                'current_balance': float(a.current_balance) if a.current_balance is not None else None
            }
            for a in accounts
        ]
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
async def submit_questionnaire(payload: QuestionnaireSubmission, db: Session = Depends(get_db)):
    """Save walkthrough answers as transactions, then calculate deductions from them."""
    try:
        year_start = datetime(payload.tax_year, 1, 1)
        year_end = datetime(payload.tax_year, 12, 31, 23, 59, 59)

        # Clear previous questionnaire answers for this user/year so re-submitting
        # (e.g. after changing an answer) doesn't double-count old ones.
        db.query(Transaction).filter(
            Transaction.user_id == payload.user_id,
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
                user_id=payload.user_id,
                transaction_date=datetime(payload.tax_year, 12, 31),
                merchant_name=rule.deduction_name,
                amount=answer.amount,
                deduction_code=answer.deduction_code,
                category='questionnaire',
                confidence_score=1.0
            ))

        db.commit()

        return compute_tax_summary(
            payload.user_id, payload.tax_year, payload.entity_type,
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
    user_id: int,
    file: UploadFile = File(...),
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
async def calculate(payload: CalculationRequest, db: Session = Depends(get_db)):
    try:
        return compute_tax_summary(
            payload.user_id, payload.tax_year, payload.entity_type,
            payload.officer_wages, db
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

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
