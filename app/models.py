from sqlalchemy import Column, Integer, String, DateTime, Numeric, Boolean, Text, ForeignKey, Float, JSON
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()

class DeductionRule(Base):
    __tablename__ = "deduction_rules"
    
    id = Column(Integer, primary_key=True, index=True)
    deduction_code = Column(String(100), unique=True, index=True)
    deduction_name = Column(String(255))
    description = Column(Text, nullable=True)
    irc_section = Column(String(100), nullable=True)
    
    meals_50_percent = Column(Boolean, default=False)
    depreciation = Column(Boolean, default=False)
    home_office_allocation = Column(Boolean, default=False)
    
    form_mapping_sole_prop = Column(String(255), nullable=True)
    form_mapping_s_corp = Column(String(255), nullable=True)
    form_mapping_c_corp = Column(String(255), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)

class CategorizationRule(Base):
    __tablename__ = "categorization_rules"
    
    id = Column(Integer, primary_key=True, index=True)
    rule_type = Column(String(50))
    rule_value = Column(String(500))
    deduction_code = Column(String(100), ForeignKey("deduction_rules.deduction_code"))
    priority = Column(Integer, default=100, index=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = "transactions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    transaction_date = Column(DateTime)
    merchant_name = Column(String(255))
    amount = Column(Numeric(12, 2))
    description = Column(Text, nullable=True)
    
    deduction_code = Column(String(100), ForeignKey("deduction_rules.deduction_code"), nullable=True)
    category = Column(String(255), nullable=True)
    confidence_score = Column(Float, nullable=True)
    
    bank_csv_source = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class TaxSummary(Base):
    __tablename__ = "tax_summaries"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    tax_year = Column(Integer)
    entity_type = Column(String(50))
    
    total_deductions = Column(Numeric(14, 2))
    officer_wages = Column(Numeric(14, 2), default=0)
    
    form_line_breakdown = Column(JSON, nullable=True)
    status = Column(String(50), default='draft')
    created_at = Column(DateTime, default=datetime.utcnow)

class CreditAccount(Base):
    __tablename__ = "credit_accounts"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    account_type = Column(String(50))
    
    stripe_account_id = Column(String(255), nullable=True)
    stripe_charge_id = Column(String(255), nullable=True)
    
    deposit_amount = Column(Numeric(12, 2))
    monthly_payment = Column(Numeric(12, 2))
    term_months = Column(Integer)
    
    status = Column(String(50), default='active')
    current_balance = Column(Numeric(12, 2))
    payments_made = Column(Integer, default=0)
    
    reported_to_equifax = Column(Boolean, default=False)
    reported_to_experian = Column(Boolean, default=False)
    reported_to_transunion = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)

class PaymentHistory(Base):
    __tablename__ = "payment_history"
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("credit_accounts.id"))
    
    payment_date = Column(DateTime)
    payment_amount = Column(Numeric(12, 2))
    stripe_charge_id = Column(String(255), nullable=True)
    status = Column(String(50))
    
    reported_to_bureaus = Column(Boolean, default=False)
    reported_to_equifax = Column(Boolean, default=False)
    reported_to_experian = Column(Boolean, default=False)
    reported_to_transunion = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)

class Subscription(Base):
    __tablename__ = "subscriptions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    
    subscription_type = Column(String(50))
    monthly_price = Column(Numeric(10, 2))
    
    status = Column(String(50))
    stripe_subscription_id = Column(String(255), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    cancelled_at = Column(DateTime, nullable=True)
