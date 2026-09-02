from fastapi import FastAPI, Depends, HTTPException, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from decimal import Decimal
import csv
import io
import json
from datetime import datetime
import os
import re

from app.database import get_db, engine
from app.models import Base, DeductionRule, CategorizationRule, Transaction, TaxSummary, CreditAccount, PaymentHistory

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="BlissPoint - Tax & Credit Builder", version="1.0.0")

# CORS Configuration
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

class TransactionInput(BaseModel):
    merchant_name: str
    amount: Decimal
    transaction_date: str
    deduction_code: str = None

class CalculationRequest(BaseModel):
    user_id: int
    tax_year: int
    entity_type: str
    transactions: list[TransactionInput]
    officer_wages: Decimal = Decimal(0)

# ============================================
# TAX ENGINE FUNCTIONS
# ============================================

def categorize_transaction(merchant: str, db: Session) -> dict:
    """Match merchant to deduction using rules"""
    
    # Try exact vendor match first
    exact_rule = db.query(CategorizationRule).filter(
        CategorizationRule.rule_type == 'exact_vendor',
        CategorizationRule.rule_value.ilike(f"%{merchant}%")
    ).order_by(CategorizationRule.priority).first()
    
    if exact_rule:
        return {
            'deduction_code': exact_rule.deduction_code,
            'confidence': 0.95,
            'matched_by': 'exact_vendor'
        }
    
    # Try regex patterns
    regex_rules = db.query(CategorizationRule).filter(
        CategorizationRule.rule_type == 'regex_pattern'
    ).order_by(CategorizationRule.priority).all()
    
    for rule in regex_rules:
        try:
            if re.search(rule.rule_value, merchant, re.IGNORECASE):
                return {
                    'deduction_code': rule.deduction_code,
                    'confidence': 0.80,
                    'matched_by': 'regex'
                }
        except:
            pass
    
    # No match
    return {
        'deduction_code': None,
        'confidence': 0,
        'matched_by': 'none'
    }

# ============================================
# TAX ENDPOINTS
# ============================================

@app.post("/api/tax/upload-csv")
async def upload_csv(
    user_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Upload bank CSV and categorize transactions"""
    
    try:
        contents = await file.read()
        csv_reader = csv.DictReader(io.StringIO(contents.decode('utf-8')))
        
        transactions_list = []
        for row in csv_reader:
            try:
                # Generic CSV parsing (Date, Description, Amount)
                merchant = row.get('Description') or row.get('Merchant') or 'Unknown'
                amount_str = row.get('Amount') or row.get('amount') or '0'
                date_str = row.get('Date') or row.get('date') or '2026-01-01'
                
                # Parse amount
                try:
                    amount = Decimal(amount_str.replace('$', '').replace(',', ''))
                except:
                    amount = Decimal(0)
                
                # Parse date (multiple formats)
                try:
                    tx_date = datetime.strptime(date_str, '%m/%d/%Y').date()
                except:
                    try:
                        tx_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    except:
                        tx_date = datetime.now().date()
                
                # Categorize
                category_result = categorize_transaction(merchant, db)
                
                # Create transaction
                tx = Transaction(
                    user_id=user_id,
                    merchant_name=merchant,
                    amount=amount,
                    transaction_date=tx_date,
                    deduction_code=category_result.get('deduction_code'),
                    category=category_result.get('matched_by'),
                    confidence_score=category_result.get('confidence')
                )
                db.add(tx)
                transactions_list.append(tx)
            except Exception as e:
                print(f"Error parsing row: {e}")
                continue
        
        db.commit()
        
        return {
            'status': 'success',
            'transactions_uploaded': len(transactions_list),
            'transactions': [
                {
                    'id': t.id,
                    'date': t.transaction_date.strftime('%Y-%m-%d'),
                    'merchant': t.merchant_name,
                    'amount': float(t.amount),
                    'category': t.category,
                    'confidence': t.confidence_score
                }
                for t in transactions_list
            ]
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tax/calculate-deductions")
async def calculate_deductions(payload: CalculationRequest, db: Session = Depends(get_db)):
    """Calculate deductions and map to form lines"""
    
    try:
        # Get all transactions for user
        transactions = db.query(Transaction).filter(
            Transaction.user_id == payload.user_id
        ).all()
        
        total_deductions = Decimal(0)
        form_breakdown = {}
        deduction_details = []
        
        for tx in transactions:
            if not tx.deduction_code:
                continue
            
            # Get deduction rule
            rule = db.query(DeductionRule).filter(
                DeductionRule.deduction_code == tx.deduction_code
            ).first()
            
            if not rule:
                continue
            
            # Apply IRC rules
            deductible = tx.amount
            limitation = None
            
            # 50% meals limitation
            if rule.meals_50_percent:
                deductible = tx.amount * Decimal('0.50')
                limitation = "50% meals limitation"
            
            total_deductions += deductible
            
            # Map to form line
            if payload.entity_type == 'SOLE_PROP':
                form_line = rule.form_mapping_sole_prop
            elif payload.entity_type == 'S_CORP':
                form_line = rule.form_mapping_s_corp
            else:
                form_line = rule.form_mapping_c_corp
            
            if form_line not in form_breakdown:
                form_breakdown[form_line] = {
                    'total': Decimal(0),
                    'transactions': []
                }
            
            form_breakdown[form_line]['total'] += deductible
            form_breakdown[form_line]['transactions'].append({
                'merchant': tx.merchant_name,
                'amount': float(tx.amount),
                'deductible': float(deductible),
                'limitation': limitation
            })
            
            deduction_details.append({
                'merchant': tx.merchant_name,
                'code': tx.deduction_code,
                'amount': float(tx.amount),
                'deductible': float(deductible),
                'form_line': form_line,
                'limitation': limitation
            })
        
        # Add officer wages (S/C corp)
        if payload.entity_type in ['S_CORP', 'C_CORP'] and payload.officer_wages > 0:
            officer_form = 'Form 1120-S Line 7' if payload.entity_type == 'S_CORP' else 'Form 1120 Line 12'
            total_deductions += payload.officer_wages
            form_breakdown[officer_form] = {
                'total': payload.officer_wages,
                'transactions': [{'description': 'Officer Wages'}]
            }
        
        # Save summary
        summary = TaxSummary(
            user_id=payload.user_id,
            tax_year=payload.tax_year,
            entity_type=payload.entity_type,
            total_deductions=total_deductions,
            form_line_breakdown=json.dumps({k: {'total': float(v['total'])} for k, v in form_breakdown.items()}),
            status='draft'
        )
        db.add(summary)
        db.commit()
        
        return {
            'status': 'success',
            'tax_year': payload.tax_year,
            'entity_type': payload.entity_type,
            'total_deductions': float(total_deductions),
            'transaction_count': len(transactions),
            'form_line_breakdown': {
                k: {'total': float(v['total']), 'count': len(v['transactions'])}
                for k, v in form_breakdown.items()
            },
            'deduction_details': deduction_details
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

# ============================================
# CREDIT-BUILDER ENDPOINTS
# ============================================

@app.post("/api/credit-builder/accounts/create")
async def create_credit_account(
    user_id: int,
    account_type: str,
    deposit_amount: Decimal,
    monthly_payment: Decimal,
    term_months: int,
    db: Session = Depends(get_db)
):
    """Create credit-builder account"""
    
    try:
        account = CreditAccount(
            user_id=user_id,
            account_type=account_type,
            deposit_amount=deposit_amount,
            monthly_payment=monthly_payment,
            term_months=term_months,
            current_balance=deposit_amount,
            status='active'
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        
        return {
            'status': 'success',
            'account_id': account.id,
            'deposit_amount': float(deposit_amount),
            'monthly_payment': float(monthly_payment),
            'term_months': term_months,
            'account_type': account_type
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

# ============================================
# HEALTH ENDPOINTS
# ============================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "blisspoint-tax-credit"}

@app.get("/health/ready")
async def ready(db: Session = Depends(get_db)):
    try:
        # Test database
        db.execute(text("SELECT 1"))
        
        # Count deductions
        deduction_count = db.query(DeductionRule).count()
        
        return {
            "status": "ready",
            "database": "connected",
            "deductions_loaded": deduction_count
        }
    except:
        raise HTTPException(status_code=503, detail="Database not connected")
