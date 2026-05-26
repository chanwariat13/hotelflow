import os
from dotenv import load_dotenv
load_dotenv()

DB_HOST  = os.getenv("DB_HOST", "localhost")
DB_PORT  = int(os.getenv("DB_PORT", 5432))
DB_NAME  = os.getenv("DB_NAME", "hoteldb")
DB_USER  = os.getenv("DB_USER", "postgres")
DB_PASS  = os.getenv("DB_PASS", "")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB   = int(os.getenv("REDIS_DB", 0))
REDIS_PASS = os.getenv("REDIS_PASS", None)

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")

HOST       = os.getenv("HOST", "0.0.0.0")
PORT       = int(os.getenv("PORT", 8000))
# SECRET_KEY: REQUIRED in production. Boot-time validation in main.py refuses
# to start if this is empty or still the default placeholder. Generate one
# with:  python -c "import secrets; print(secrets.token_urlsafe(48))"
SECRET_KEY = os.getenv("SECRET_KEY", "")
BASE_URL   = os.getenv("BASE_URL", "http://localhost:8000")
GOTENBERG_URL = os.getenv("GOTENBERG_URL", "http://localhost:3000")
TZ         = "Asia/Kolkata"
