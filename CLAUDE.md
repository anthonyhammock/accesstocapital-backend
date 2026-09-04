# BlissPoint / Access to Capital — Project Rules

- 100% cloud. Never assume local development, local files, or local installs.
- Backend: Python 3.13, FastAPI, SQLAlchemy 2.0.51+ (NOT 2.1.x — still beta,
  and 2.0.23 has a known Python 3.13 crash, fixed in 2.0.36+).
- psycopg2-binary must be UNPINNED in requirements.txt (a pinned old version
  tries to build from source and fails).
- Auth uses the `bcrypt` package directly (hashpw/checkpw) — NOT passlib,
  which breaks against bcrypt >=4.1.0.
- Deploys via Cloud Build trigger (inline YAML) → Cloud Run, using a
  Dockerfile-based Docker build step, NOT Google Buildpacks.
- Cloud SQL instance: accesstocapital-primary (blisspoint-metro-2:us-central1).
  Connects via Unix socket, DATABASE_URL set as a Cloud Run env var.
- Existing tables: users, consumer_accounts, business_accounts,
  deduction_rules, categorization_rules, transactions, tax_summaries,
  payment_history. Check what exists before assuming a schema.
- When editing code, make targeted edits to the specific broken section.
  Never replace whole files — it has silently deleted working code
  (auth endpoints, model classes) in this project before.
- Always actually run/test code changes before calling a task complete.
