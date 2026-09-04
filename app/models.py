from sqlalchemy import Column, Integer, String, DateTime, Numeric, Boolean, Text, ForeignKey, Float, JSON
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    first_name = Column(String(100))
    last_name = Column(String(100))
    account_type = Column(String(50))
    stripe_customer_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

class ConsumerAccount(Base):
    __tablename__ = "consumer_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_name = Column(String(255), nullable=False)
    account_number = Column(String(255), nullable=True)
    credit_limit = Column(Numeric(12, 2), nullable=True)
    current_balance = Column(Numeric(12, 2), nullable=True)
    payment_status = Column(String(50), nullable=True)
    reported_to_bureaus = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class BusinessAccount(Base):
    __tablename__ = "business_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    business_group_id = Column(String(36), nullable=True, index=True)
    business_name = Column(String(255), nullable=False)
    ein = Column(String(50), nullable=True)
    business_type = Column(String(100), nullable=True)
    annual_revenue = Column(Numeric(14, 2), nullable=True)
    credit_limit = Column(Numeric(12, 2), nullable=True)
    current_balance = Column(Numeric(12, 2), nullable=True)
    reported_to_bureaus = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    user_id = Column(Integer)
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
    user_id = Column(Integer)
    tax_year = Column(Integer)
    entity_type = Column(String(50))

    total_deductions = Column(Numeric(14, 2))
    officer_wages = Column(Numeric(14, 2), default=0)

    form_line_breakdown = Column(JSON, nullable=True)
    status = Column(String(50), default='draft')
    created_at = Column(DateTime, default=datetime.utcnow)
