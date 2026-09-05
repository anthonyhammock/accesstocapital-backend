from sqlalchemy import Column, Integer, String, DateTime, Numeric, Boolean, Text, ForeignKey, Float, JSON, LargeBinary, Time
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

    # 'income' or 'expense' — needed to total a ledger correctly; deduction_code
    # alone can't tell the two apart since every deduction_rules row is an
    # expense category. Defaults to 'expense' since that's what every existing
    # row (CSV upload, questionnaire) already is.
    transaction_type = Column(String(20), nullable=False, default='expense')

    # 'operating', 'investing', or 'financing' — the Cash Flow Statement's
    # three activity sections. Defaults to 'operating' since that's what
    # every existing row (day-to-day income/expenses) actually is; only
    # asset purchases/sales (investing) and loan/equity/draw activity
    # (financing) need reclassifying, which happens on the transaction
    # itself rather than via a second parallel ledger.
    cash_flow_category = Column(String(20), nullable=False, default='operating')

    deduction_code = Column(String(100), ForeignKey("deduction_rules.deduction_code"), nullable=True)
    category = Column(String(255), nullable=True)
    confidence_score = Column(Float, nullable=True)

    bank_csv_source = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PortalClient(Base):
    __tablename__ = "portal_clients"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)

    # Unguessable magic-link token (secrets.token_urlsafe(32)) — this is the
    # only thing that gates the public /api/portal/public/* endpoints, so it
    # must never be derivable from the client id or any other public value.
    portal_token = Column(String(64), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

class PortalDocument(Base):
    __tablename__ = "portal_documents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("portal_clients.id"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=False)
    file_data = Column(LargeBinary, nullable=False)

    # 'owner' or 'client' — who uploaded it, shown in the UI so each side can
    # tell a document they sent apart from one they received.
    uploaded_by = Column(String(20), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

class PortalComment(Base):
    __tablename__ = "portal_comments"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("portal_documents.id"), nullable=False, index=True)
    author = Column(String(20), nullable=False)  # 'owner' or 'client'
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class SchedulingSettings(Base):
    __tablename__ = "scheduling_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)

    # IANA name (e.g. "America/New_York") — every weekly AvailabilityRule
    # time and every computed public slot is interpreted in this zone, then
    # converted to/from UTC for storage and for the visitor's own browser.
    timezone = Column(String(64), nullable=False, default="UTC")

    meeting_duration_minutes = Column(Integer, nullable=False, default=30)
    buffer_minutes = Column(Integer, nullable=False, default=0)

    # No one can book a slot starting sooner than this many hours from now —
    # keeps a booking from landing on the owner's calendar with zero warning.
    min_notice_hours = Column(Integer, nullable=False, default=2)

    # Unguessable-enough public slug (not a secret like the portal token —
    # meant to be shared openly, like a Calendly link).
    booking_slug = Column(String(64), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AvailabilityRule(Base):
    __tablename__ = "availability_rules"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Python's date.weekday() convention: 0 = Monday ... 6 = Sunday.
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    guest_name = Column(String(255), nullable=False)
    guest_email = Column(String(255), nullable=False)
    notes = Column(Text, nullable=True)

    # Always UTC — the one unambiguous instant both sides agree on. The
    # owner's and guest's local displays are computed from this, not stored.
    start_at = Column(DateTime, nullable=False, index=True)
    end_at = Column(DateTime, nullable=False)

    status = Column(String(20), nullable=False, default='confirmed')  # 'confirmed' or 'cancelled'
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
