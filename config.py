import logging
import logging.handlers
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RESUMES_DIR = BASE_DIR / "resumes"
LOGS_DIR = BASE_DIR / "logs"

DB_PATH = DATA_DIR / "jobs.db"
EXCEL_PATH = DATA_DIR / "jobs.xlsx"
MASTER_RESUME_PATH = RESUMES_DIR / "master_resume.docx"
LINKEDIN_COOKIES_PATH = DATA_DIR / "linkedin_cookies.json"

LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")
GLASSDOOR_EMAIL = os.getenv("GLASSDOOR_EMAIL", "")
GLASSDOOR_PASSWORD = os.getenv("GLASSDOOR_PASSWORD", "")

# RapidAPI JSearch credentials
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY") or None  # Must be set in .env; no hardcoded default
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "jsearch.p.rapidapi.com")

# Ollama settings
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

JOB_ROLES = [
    "Software Engineer",
    "Data Engineer",
    "ML Engineer",
    "AI Engineer",
    "Backend Engineer",
    "Full Stack Engineer",
]

SUPPORTED_PLATFORMS = ["linkedin", "indeed", "glassdoor", "wellfound", "google"]

REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 5.0
MAX_RETRIES = 3
try:
    _max_jobs_raw = int(os.getenv("MAX_JOBS_PER_PLATFORM", "30"))
except (TypeError, ValueError):
    _max_jobs_raw = 30
MAX_JOBS_PER_PLATFORM = max(1, _max_jobs_raw)  # Must be a positive integer (minimum 1)
HEADLESS_BROWSER = os.getenv("HEADLESS_BROWSER", "false").lower() == "true"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    RESUMES_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)


def setup_logging(name: str = "jobbot") -> logging.Logger:
    """Configure and return a logger with console + rotating file handlers."""
    ensure_dirs()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s:%(lineno)d  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler — DEBUG and above, 5 MB × 3 backups
    log_file = LOGS_DIR / "jobbot.log"
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger
