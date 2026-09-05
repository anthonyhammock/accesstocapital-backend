from sqlalchemy import Column, Integer, String, DateTime, Numeric, Boolean, Text, ForeignKey, Float, JSON, LargeBinary, Time, UniqueConstraint
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

    # No self-service path to either of these — set directly in the DB by
    # the platform owner. is_admin gates every /api/admin/* endpoint;
    # is_comped just marks an account as free/test access for the owner's
    # own tracking (and, once real billing exists, to exclude it from
    # charges) — the two are independent (an admin isn't automatically
    # comped, a comped user isn't automatically an admin).
    #
    # default=False here is Python-side only (SQLAlchemy applies it on
    # INSERT, not to existing rows) — any manual ALTER TABLE against the
    # already-populated production `users` table MUST include an explicit
    # DEFAULT FALSE, or Postgres will reject a NOT NULL column add outright.
    is_admin = Column(Boolean, nullable=False, default=False)
    is_comped = Column(Boolean, nullable=False, default=False)

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
    # Generically useful beyond Client Portal now that this doubles as the
    # platform-wide Contact record (also used by Invoicing and CRM) —
    # nullable so nothing that only ever set name/email/notes breaks.
    phone = Column(String(50), nullable=True)
    company = Column(String(255), nullable=True)
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

class CalendarConnection(Base):
    __tablename__ = "calendar_connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # 'google' or 'microsoft'. A user can connect at most one of each
    # (enforced by the app, not a DB constraint, so a future third provider
    # doesn't need a schema change).
    provider = Column(String(20), nullable=False)
    provider_email = Column(String(255), nullable=True)

    # Fernet-encrypted at rest (see encrypt_token/decrypt_token in main.py) —
    # these are live credentials that can read someone's calendar, not
    # ordinary app data, so they never touch the database in plaintext.
    access_token_encrypted = Column(Text, nullable=False)
    refresh_token_encrypted = Column(Text, nullable=False)
    token_expires_at = Column(DateTime, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False, default='1h')  # '5min','15min','1h','1day'
    strategy_type = Column(String(20), nullable=False, default='hybrid')  # momentum/mean_reversion/breakout/hybrid
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Signal(Base):
    __tablename__ = "signals"

    # Signals are generated once per (symbol, timeframe) — NOT per user, even
    # though many users may watch the same symbol. A user's "current signals"
    # is computed by joining their active WatchlistEntry rows against recent
    # rows here, so 500 users watching AAPL still costs one market-data fetch
    # and one signal row, not 500.
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    signal_type = Column(String(10), nullable=False)  # 'buy' or 'sell'
    confidence = Column(Integer, nullable=False)  # 0-100
    entry_price = Column(Numeric(14, 4), nullable=False)
    target_price = Column(Numeric(14, 4), nullable=True)
    stop_loss = Column(Numeric(14, 4), nullable=True)
    risk_reward_ratio = Column(Numeric(6, 2), nullable=True)
    reason = Column(Text, nullable=True)
    explanation = Column(JSON, nullable=True)  # list of plain-language strings, beginner-friendly version of `reason`
    market_condition = Column(String(20), nullable=True)  # uptrend/downtrend/ranging

    # Historical outcome tracking, resolved after the fact by checking real
    # price action against target_price/stop_loss — this is what powers an
    # honest, empirically-computed success rate (see /api/trading/success-rate)
    # instead of confusing indicator-agreement confidence with a probability
    # of profit, which are not the same thing.
    outcome = Column(String(20), nullable=False, default='pending')  # pending/target_hit/stop_hit/expired
    outcome_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class AlertPreference(Base):
    __tablename__ = "alert_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    email_enabled = Column(Boolean, default=True)
    sms_enabled = Column(Boolean, default=False)
    sms_phone = Column(String(20), nullable=True)
    min_confidence = Column(Integer, nullable=False, default=75)
    quiet_hours_start = Column(Time, nullable=True)
    quiet_hours_end = Column(Time, nullable=True)
    digest_mode = Column(Boolean, default=False)
    last_viewed_signals_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SignalAlert(Base):
    __tablename__ = "signal_alerts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False, index=True)
    delivery_method = Column(String(10), nullable=False)  # email/sms/in_app
    status = Column(String(10), nullable=False, default='sent')  # sent/failed/skipped
    sent_at = Column(DateTime, default=datetime.utcnow)

class TradeLog(Base):
    __tablename__ = "trade_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False, default='long')  # long/short
    shares = Column(Numeric(14, 4), nullable=False)
    entry_price = Column(Numeric(14, 4), nullable=False)
    entry_at = Column(DateTime, nullable=False)
    exit_price = Column(Numeric(14, 4), nullable=True)
    exit_at = Column(DateTime, nullable=True)
    status = Column(String(10), nullable=False, default='open')  # open/closed
    pnl = Column(Numeric(14, 2), nullable=True)
    roi_pct = Column(Numeric(8, 2), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    address = Column(Text, nullable=True)
    payment_terms = Column(String(20), nullable=False, default='net_30')  # due_on_receipt/net_15/net_30/net_45/net_60
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Bill(Base):
    __tablename__ = "bills"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False, index=True)
    bill_number = Column(String(100), nullable=True)
    bill_date = Column(DateTime, nullable=False)
    due_date = Column(DateTime, nullable=False, index=True)
    amount = Column(Numeric(14, 2), nullable=False)
    category = Column(String(100), nullable=True)
    memo = Column(Text, nullable=True)

    # 'paid'/'overdue'/'partial'/'unpaid' is never stored — always derived
    # from amount vs. the live sum of this bill's BillPayment rows plus
    # due_date vs. today, so it can never drift out of sync with the actual
    # payment history the way a cached status column could.

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class BillPayment(Base):
    __tablename__ = "bill_payments"

    id = Column(Integer, primary_key=True, index=True)
    bill_id = Column(Integer, ForeignKey("bills.id"), nullable=False, index=True)
    amount = Column(Numeric(14, 2), nullable=False)
    payment_date = Column(DateTime, nullable=False)
    payment_method = Column(String(20), nullable=False, default='other')  # check/ach/card/wire/cash/other
    reference_number = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Invoice(Base):
    __tablename__ = "invoices"
    # Backstop against next_invoice_number() ever handing out a number a
    # user already has (e.g. a race between two concurrent creates) — a
    # duplicate, customer-facing invoice number is a real data-integrity
    # problem, not just a cosmetic one, so this fails loudly instead of
    # silently allowing it.
    __table_args__ = (UniqueConstraint('user_id', 'invoice_number', name='uq_invoices_user_invoice_number'),)

    # Reuses PortalClient as the customer record rather than a separate
    # Customer model — one client list shared across Client Portal and
    # Invoicing instead of two parallel, easily-out-of-sync ones.
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("portal_clients.id"), nullable=False, index=True)

    # Assigned once at creation (e.g. "INV-1001") and never recomputed —
    # unlike a bill's status, this is an identity, not a derived value.
    invoice_number = Column(String(20), nullable=False)

    issue_date = Column(DateTime, nullable=False)
    due_date = Column(DateTime, nullable=False, index=True)
    tax_rate = Column(Numeric(5, 2), nullable=False, default=0)  # percent, applied to the line-item subtotal
    notes = Column(Text, nullable=True)

    # NULL = draft (not yet sent to the client); set = sent. Status beyond
    # that (paid/overdue/partial) is always derived from payments + due_date,
    # same "derive, don't cache" discipline as Bill.status in Vendor & AP.
    sent_at = Column(DateTime, nullable=True)

    # Unguessable public link so the client can view (not edit) this one
    # invoice without an account — same pattern as PortalClient.portal_token.
    public_token = Column(String(64), unique=True, index=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    description = Column(Text, nullable=False)
    quantity = Column(Numeric(10, 2), nullable=False, default=1)
    unit_price = Column(Numeric(12, 2), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)

class InvoicePayment(Base):
    __tablename__ = "invoice_payments"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    amount = Column(Numeric(14, 2), nullable=False)
    payment_date = Column(DateTime, nullable=False)
    payment_method = Column(String(20), nullable=False, default='other')  # check/ach/card/wire/cash/other
    reference_number = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Deal(Base):
    __tablename__ = "deals"

    # Reuses PortalClient as the contact, same as Invoicing — one contact
    # list across Client Portal, Invoicing, and the CRM pipeline.
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("portal_clients.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    # Numeric(12, 2) to match InvoiceLineItem.unit_price — a deal converts
    # straight into a line item, so the two must share the same precision
    # or a large deal value overflows the invoice line item column.
    value = Column(Numeric(12, 2), nullable=False, default=0)
    stage = Column(String(20), nullable=False, default='lead', index=True)  # lead/qualified/proposal/negotiation/won/lost
    expected_close_date = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    # Set when stage moves to won/lost, cleared if reopened to an active
    # stage — lets the pipeline distinguish "just created" from "closed
    # a while ago" without a second parallel status field.
    closed_at = Column(DateTime, nullable=True)

    # Set once a won deal is converted to an invoice — blocks converting
    # the same deal twice into two separate invoices.
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class DealNote(Base):
    __tablename__ = "deal_notes"

    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class BalanceSheetItem(Base):
    __tablename__ = "balance_sheet_items"

    # Manually-tracked assets/liabilities the platform has no other feed
    # for (equipment, inventory, loans, credit lines) — a current balance
    # the owner updates directly, same pattern as a Vendor's running
    # balance rather than a dated transaction history.
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    side = Column(String(20), nullable=False, index=True)  # 'asset' or 'liability'
    name = Column(String(255), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class OwnerEquityEntry(Base):
    __tablename__ = "owner_equity_entries"

    # Feeds both the Balance Sheet's equity section and the Statement of
    # Owner's Equity roll-forward — one entry, two reports, rather than
    # two parallel places to record the same contribution or draw.
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    entry_type = Column(String(20), nullable=False, index=True)  # 'contribution' or 'draw'
    amount = Column(Numeric(14, 2), nullable=False)
    entry_date = Column(DateTime, nullable=False)
    description = Column(Text, nullable=True)
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
