from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from datetime import datetime

# Initialize FastAPI app
app = FastAPI(
    title="Access to Capital API",
    version="1.0.0",
    description="Credit reporting platform API"
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://accesstocapital-web.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Password hashing setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Database connection
def get_db_connection():
    """Connect to Google Cloud SQL"""
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            database=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            port=os.getenv("DB_PORT", "5432")
        )
        return conn
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

# ===== DATA MODELS =====

class UserRegister(BaseModel):
    """Data model for user registration"""
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    account_type: str  # 'consumer' or 'business'

class UserLogin(BaseModel):
    """Data model for user login"""
    email: str
    password: str

class UserResponse(BaseModel):
    """User data to return (no password)"""
    id: int
    email: str
    first_name: str
    last_name: str
    account_type: str
    created_at: str

# ===== AUTHENTICATION FUNCTIONS =====

def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

# ===== HEALTH CHECKS =====

@app.get("/health")
async def health():
    """Simple health check"""
    return {
        "status": "healthy",
        "service": "access-to-capital-api",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health/ready")
async def ready():
    """Ready check - verifies database connection"""
    conn = get_db_connection()
    if conn:
        conn.close()
        db_status = "connected"
    else:
        db_status = "disconnected"
    
    return {
        "status": "ready",
        "database": db_status,
        "version": "1.0.0"
    }

# ===== USER REGISTRATION =====

@app.post("/api/auth/register")
async def register(user: UserRegister):
    """
    Register a new user
    
    Expects:
    {
      "email": "user@example.com",
      "password": "securepass123",
      "first_name": "John",
      "last_name": "Doe",
      "account_type": "consumer"
    }
    """
    
    # Validate inputs
    if len(user.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters"
        )
    
    if user.account_type not in ["consumer", "business"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account type must be 'consumer' or 'business'"
        )
    
    # Connect to database
    conn = get_db_connection()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database connection failed"
        )
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Check if email already exists
        cur.execute("SELECT id FROM users WHERE email = %s", (user.email,))
        if cur.fetchone():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Hash password
        password_hash = hash_password(user.password)
        
        # Insert user
        cur.execute("""
            INSERT INTO users (email, password_hash, first_name, last_name, account_type)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, email, first_name, last_name, account_type, created_at
        """, (user.email, password_hash, user.first_name, user.last_name, user.account_type))
        
        new_user = cur.fetchone()
        conn.commit()
        
        return {
            "message": "User registered successfully",
            "user": {
                "id": new_user['id'],
                "email": new_user['email'],
                "first_name": new_user['first_name'],
                "last_name": new_user['last_name'],
                "account_type": new_user['account_type']
            },
            "status": "success"
        }
    
    except Exception as e:
        conn.rollback()
        print(f"Registration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration failed: {str(e)}"
        )
    finally:
        cur.close()
        conn.close()

# ===== USER LOGIN =====

@app.post("/api/auth/login")
async def login(user: UserLogin):
    """
    Login a user
    
    Expects:
    {
      "email": "user@example.com",
      "password": "securepass123"
    }
    """
    
    conn = get_db_connection()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database connection failed"
        )
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Find user by email
        cur.execute(
            "SELECT id, password_hash, email, first_name, last_name, account_type FROM users WHERE email = %s",
            (user.email,)
        )
        
        db_user = cur.fetchone()
        
        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        # Verify password
        if not verify_password(user.password, db_user['password_hash']):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        return {
            "message": "Login successful",
            "user": {
                "id": db_user['id'],
                "email": db_user['email'],
                "first_name": db_user['first_name'],
                "last_name": db_user['last_name'],
                "account_type": db_user['account_type']
            },
            "access_token": f"token_{db_user['id']}_{datetime.now().timestamp()}",
            "token_type": "bearer",
            "status": "success"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Login error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Login failed"
        )
    finally:
        cur.close()
        conn.close()

# ===== GET USER PROFILE =====

@app.get("/api/users/{user_id}")
async def get_user(user_id: int):
    """Get user profile by ID"""
    
    conn = get_db_connection()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database connection failed"
        )
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute(
            "SELECT id, email, first_name, last_name, account_type, created_at FROM users WHERE id = %s",
            (user_id,)
        )
        
        user = cur.fetchone()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        return {
            "user": user,
            "status": "success"
        }
    
    except Exception as e:
        print(f"Get user error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user"
        )
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
