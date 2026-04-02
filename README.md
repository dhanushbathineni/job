# Job Application Automation Bot

A multi-agent system that automates the full job search pipeline — from scraping listings to tailoring your resume to pre-filling application forms.

## What It Does

| Step | Agent | What happens |
|------|-------|-------------|
| 1 | **Scraper** | Searches LinkedIn, Indeed, Glassdoor, Wellfound, and Google Jobs for your target roles and saves results to a local database |
| 2 | **Assessor** | Reads your resume and acts as a recruiter — scores it against the job description and identifies every gap |
| 3 | **Composer** | Rewrites your resume, tailored to that specific job, ATS-optimised, saved as a DOCX |
| 4 | **Form Filler** | Opens the real application page in a browser, pre-fills every detectable field, then pauses for you to review before you manually submit |

All four agents are orchestrated from a **Streamlit dashboard**.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | `python --version` |
| [Ollama](https://ollama.com) | Local LLM server — must be running |
| qwen3 model | `ollama pull qwen3` |
| Chromium | Installed automatically by Playwright |

---

## Installation

```bash
# 1. Clone / download the project
cd E:\Projects\job

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install Playwright's Chromium browser
python -m playwright install chromium

# 4. Copy and fill in your .env
cp .env.example .env
```

---

## Configuration

Edit `.env` with your details:

```bash
# Local LLM
OLLAMA_MODEL=qwen3
OLLAMA_HOST=http://localhost:11434

# LinkedIn (needed for LinkedIn scraping)
LINKEDIN_EMAIL=you@email.com
LINKEDIN_PASSWORD=yourpassword

# Your personal info — used by the form filler
APPLICANT_NAME=Your Full Name
APPLICANT_EMAIL=you@email.com
APPLICANT_PHONE=+1-555-000-0000
APPLICANT_LINKEDIN=linkedin.com/in/yourprofile
APPLICANT_GITHUB=github.com/yourusername
APPLICANT_LOCATION=San Francisco, CA
APPLICANT_COUNTRY=United States
APPLICANT_STATE=California
APPLICANT_SALARY_EXPECTATION=150000
APPLICANT_START_DATE=2 weeks
YEARS_EXPERIENCE=5

# Scraper limits
MAX_JOBS_PER_PLATFORM=30
HEADLESS_BROWSER=false   # keep false — you need to solve CAPTCHAs manually
```

Place your existing resume at:
```
resumes/master_resume.docx
```

---

## Running

```bash
# Start Ollama (if not already running)
ollama serve

# Pull the model (first time only)
ollama pull qwen3

# Launch the dashboard
python -m streamlit run dashboard.py
```

Open `http://localhost:8501` in your browser.

---

## Usage Walkthrough

### 1. Scrape Jobs
In the sidebar, set a job title (e.g. `Software Engineer`), location, select platforms, and click **Start Scraping**. The browser opens and scrapes in real time. Results populate the Jobs tab.

### 2. Assess Resume
Click **Assess** on any job row. The assessor reads your `master_resume.docx` and the job description, then scores your match (0–100) and lists:
- Missing keywords
- Weak bullet points
- ATS formatting issues
- Top 3 critical gaps

### 3. Compose Tailored Resume
Click **Compose** (enabled after assessment). The composer rewrites your resume for that specific role, incorporates missing keywords, and saves a DOCX to `resumes/`.

### 4. Apply
Click **Apply** on a job that has a composed resume. Playwright opens the application page. Filled fields are highlighted green; unfilled required fields are highlighted red. Review everything, edit any field you want, then **manually click Submit**. The bot never auto-submits. Afterwards, mark the job as Applied in the dashboard.

---

## Project Structure

```
job/
├── dashboard.py          # Streamlit UI — run this to start
├── config.py             # All settings: paths, env vars, logging
├── db.py                 # DatabaseManager — all SQLite + Excel operations
├── agents/
│   ├── scraper.py        # Playwright scrapers for 5 platforms
│   ├── assessor.py       # Resume gap analysis via Ollama
│   ├── composer.py       # ATS DOCX resume builder
│   └── form_filler.py    # Form automation + human review gate
├── data/
│   ├── jobs.db           # SQLite database (source of truth)
│   ├── jobs.xlsx         # Excel export (auto-synced after each scrape)
│   └── linkedin_cookies.json  # LinkedIn session (auto-created)
├── resumes/
│   ├── master_resume.docx     # YOUR resume — place here
│   └── *.docx                 # Tailored resumes — auto-generated
├── logs/
│   └── jobbot.log        # Rotating log file (5 MB × 3 backups)
├── .env                  # Your secrets — never commit this
├── .env.example          # Template
└── requirements.txt
```

---

## Database Tables

| Table | Purpose |
|-------|---------|
| `jobs` | Every scraped job — description, keywords, match score, resume path, status |
| `scrape_sessions` | Audit log of every scrape run |
| `resume_versions` | History of every generated resume DOCX |
| `application_log` | Action history per job (assess, compose, fill, submit) |

---

## Logs

Logs are written to `logs/jobbot.log` and the console. To increase verbosity, the file handler captures `DEBUG`; the console handler shows `INFO` and above.

```
2026-03-31 16:06:03  INFO      jobbot.scraper:45   Starting scrape: google — query='Software Engineer'
2026-03-31 16:06:08  INFO      jobbot.scraper:62   Finished google: 10 found, 8 new
2026-03-31 16:06:09  INFO      jobbot.assessor:71  Assessment complete — job 3 match score: 72%
```

---

## Supported Platforms

| Platform | Login required | Notes |
|----------|---------------|-------|
| Google Jobs | No | Most reliable, good starting point |
| Indeed | No | May show CAPTCHA — solve manually in browser |
| LinkedIn | Yes | Credentials in `.env`; session cookie cached |
| Glassdoor | Optional | Modal dismissed automatically; add credentials if needed |
| Wellfound | No | Scroll-based pagination |

---

## Anti-Bot Handling

- Random 2–5 second delays between page interactions
- Stealth mode: `navigator.webdriver` hidden from page scripts
- LinkedIn session cookies cached in `data/linkedin_cookies.json` to avoid repeated logins
- CAPTCHAs: browser pauses with `page.pause()` so you can solve manually, then resumes
- Exponential backoff on HTTP 429 / 503

---

## Troubleshooting

**Ollama not reachable**
```bash
ollama serve          # start the server
ollama pull qwen3     # pull model if missing
```

**`master_resume.docx` not found**
Place your resume at `resumes/master_resume.docx`. A warning banner appears in the dashboard if it is missing.

**LinkedIn keeps asking for login**
Delete `data/linkedin_cookies.json` and let the scraper log in fresh.

**Claude / LLM returns non-JSON**
The system retries up to 3 times, each time appending stricter instructions. If it still fails, check `logs/jobbot.log` for the raw model output.

**Playwright browser not found**
```bash
python -m playwright install chromium
```

---

## Notes

- The form filler **never auto-submits**. It calls `page.pause()` which freezes the browser in a review state. You click Submit manually.
- Tailored resumes are ATS-safe by design: no tables for layout, no headers/footers, bullet points use `•` characters instead of list styles, and all content is in a single column.
- The LLM is instructed never to fabricate experience — it only reframes and reorders what is already in your master resume.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
