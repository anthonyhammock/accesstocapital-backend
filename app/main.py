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

# Import database and models
from app.database import get_db, engine
from app.models import Base, DeductionRule, CategorizationRule, Transaction, TaxSummary

# Create tables on startup
Base.metadata.create_all(bind=engine)

# Initialize app
app = FastAPI(title="BlissPoint Tax & Credit", version="1.0.0")

# CORS
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
    transactions: list = []
    officer_wages: Decimal = Decimal(0)

# ============================================
# HELPER FUNCTIONS
# ============================================

def categorize_merchant(merchant: str, db: Session) -> dict:
    """Match merchant name to deduction code"""
    
    # Try exact match
    exact = db.query(CategorizationRule).filter(
        CategorizationRule.rule_type == 'exact_vendor'
    ).order_by(CategorizationRule.priority).all()
    
    for rule in exact:
        if rule.rule_value.upper() in merchant.upper():
            return {
                'code': rule.deduction_code,
                'confidence': 0.95,
                'method': 'exact'
            }
    
    # Try regex
    regex_rules = db.query(CategorizationRule).filter(
        CategorizationRule.rule_type == 'regex_pattern'
    ).order_by(CategorizationRule.priority).all()
    
    for rule in regex_rules:
        try:
            if re.search(rule.rule_value, merchant, re.IGNORECASE):
                return {
                    'code': rule.deduction_code,
                    'confidence': 0.80,
                    'method': 'regex'
                }
        except:
            pass
    
    return {'code': None, 'confidence': 0, 'method': 'none'}

# ============================================
# TAX ENDPOINTS
# ============================================

@app.post("/api/tax/upload-csv")
async def upload_csv(
    user_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Upload and categorize bank CSV"""
    
    try:
        contents = await file.read()
        text_io = io.StringIO(contents.decode('utf-8'))
        reader = csv.DictReader(text_io)
        
        if not reader.fieldnames:
            raise ValueError("CSV is empty")
        
        transactions = []
        for row_num, row in enumerate(reader, start=2):
            try:
                # Parse CSV
                merchant = row.get('Description') or row.get('Merchant') or 'Unknown'
                amount_str = row.get('Amount') or row.get('amount') or '0'
                date_str = row.get('Date') or row.get('date') or ''
                
                # Amount
                try:
                    amount = Decimal(str(amount_str).replace('$', '').replace(',', ''))
                except:
                    amount = Decimal('0')
                
                if amount == 0:
                    continue
                
                # Date
                try:
                    tx_date = datetime.strptime(date_str, '%m/%d/%Y').date()
                except:
                    try:
                        tx_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    except:
                        tx_date = datetime.now().date()
                
                # Categorize
                cat = categorize_merchant(merchant, db)
                
                # Save
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

@app.post("/api/tax/calculate-deductions")
async def calculate(payload: CalculationRequest, db: Session = Depends(get_db)):
    """Calculate deductions for user"""
    
    try:
        # Get user's transactions
        txs = db.query(Transaction).filter(
            Transaction.user_id == payload.user_id
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
            
            # Calculate
            amount = tx.amount
            limit = None
            
            if rule.meals_50_percent:
                amount = tx.amount * Decimal('0.50')
                limit = '50% meals'
            
            total += amount
            
            # Form line
            if payload.entity_type == 'SOLE_PROP':
                line = rule.form_mapping_sole_prop
            elif payload.entity_type == 'S_CORP':
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
        
        # Officer wages
        if payload.entity_type != 'SOLE_PROP' and payload.officer_wages > 0:
            total += payload.officer_wages
            line = 'Form 1120-S Line 7' if payload.entity_type == 'S_CORP' else 'Form 1120 Line 12'
            lines[line] = {'total': payload.officer_wages, 'count': 1}
        
        # Save
        summary = TaxSummary(
            user_id=payload.user_id,
            tax_year=payload.tax_year,
            entity_type=payload.entity_type,
            total_deductions=total,
            form_line_breakdown=json.dumps({k: float(v['total']) for k, v in lines.items()}),
            status='draft'
        )
        db.add(summary)
        db.commit()
        
        return {
            'status': 'success',
            'total': float(total),
            'lines': {k: {'total': float(v['total']), 'count': v['count']} for k, v in lines.items()},
            'details': details[:20]
        }
    
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
