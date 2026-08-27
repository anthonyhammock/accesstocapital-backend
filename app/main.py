from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Access to Capital API", version="1.0.0")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/health/ready")
async def ready():
    return {"status": "ready"}

@app.post("/api/auth/register")
async def register(email: str, password: str, first_name: str, last_name: str):
    # Placeholder - will implement properly
    return {
        "message": "User registered",
        "email": email,
        "first_name": first_name,
        "last_name": last_name
    }

@app.post("/api/auth/login")
async def login(email: str, password: str):
    # Placeholder
    return {
        "message": "Login successful",
        "access_token": "token_here"
    }
