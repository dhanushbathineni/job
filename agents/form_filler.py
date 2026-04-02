"""
Agent 4: Form Filler Bot
Semi-automated job application form filler using Playwright.
Bot pre-fills all detectable fields; human reviews and manually clicks Submit.
"""
import asyncio
import sys

# Windows requires ProactorEventLoop for subprocess support (Playwright)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import re
from datetime import datetime
from pathlib import Path

import ollama
from playwright.async_api import async_playwright, Page, BrowserContext

import config
from config import setup_logging
from db import DatabaseManager

logger = setup_logging("jobbot.form_filler")


# ── Platform detection ────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "greenhouse.io" in url_lower or "boards.greenhouse" in url_lower:
        return "greenhouse"
    if "lever.co" in url_lower:
        return "lever"
    if "myworkdayjobs.com" in url_lower or "workday.com" in url_lower:
        return "workday"
    if "smartrecruiters.com" in url_lower:
        return "smartrecruiters"
    if "jobvite.com" in url_lower:
        return "jobvite"
    if "ashbyhq.com" in url_lower:
        return "ashby"
    return "custom"


# ── Field mapping heuristics ──────────────────────────────────────────────────

FIELD_PATTERNS = {
    "first_name": re.compile(r"(first.?name|fname|given.?name)", re.I),
    "last_name": re.compile(r"(last.?name|lname|surname|family.?name)", re.I),
    "full_name": re.compile(r"(full.?name|your.?name|^name$)", re.I),
    "email": re.compile(r"(e.?mail)", re.I),
    "phone": re.compile(r"(phone|mobile|cell|tel)", re.I),
    "linkedin": re.compile(r"(linkedin)", re.I),
    "github": re.compile(r"(github|git.?hub)", re.I),
    "portfolio": re.compile(r"(portfolio|website|personal.?site)", re.I),
    "location": re.compile(r"(city|location|address|where.?are.?you)", re.I),
    "country": re.compile(r"(country)", re.I),
    "state": re.compile(r"(state|province)", re.I),
    "zip": re.compile(r"(zip|postal)", re.I),
    "years_experience": re.compile(r"(years?.?of?.?exp|experience.?years?)", re.I),
    "cover_letter": re.compile(r"(cover.?letter|motivation|why.?do.?you)", re.I),
    "salary": re.compile(r"(salary|compensation|expected.?pay)", re.I),
    "start_date": re.compile(r"(start.?date|available|when.?can.?you.?start)", re.I),
    "resume_upload": re.compile(r"(resume|cv|curriculum)", re.I),
}


def _map_field(label: str, field_type: str, accept: str = "") -> str | None:
    if field_type == "file":
        if re.search(FIELD_PATTERNS["resume_upload"], label or accept or ""):
            return "resume_upload"
        return None
    for key, pattern in FIELD_PATTERNS.items():
        if key == "resume_upload":
            continue
        if pattern.search(label or ""):
            return key
    return None


async def _get_field_label(page: Page, element) -> str:
    try:
        aria = await element.get_attribute("aria-label")
        if aria:
            return aria
        labelledby = await element.get_attribute("aria-labelledby")
        if labelledby:
            label_el = await page.query_selector(f"#{labelledby}")
            if label_el:
                return (await label_el.inner_text()).strip()
        el_id = await element.get_attribute("id")
        if el_id:
            label_el = await page.query_selector(f"label[for='{el_id}']")
            if label_el:
                return (await label_el.inner_text()).strip()
        placeholder = await element.get_attribute("placeholder")
        if placeholder:
            return placeholder
        name = await element.get_attribute("name")
        return name or ""
    except Exception:
        return ""


# ── Cover letter generator (Ollama) ──────────────────────────────────────────

def _generate_cover_letter(name: str, job_title: str, company: str, summary: str) -> str:
    try:
        resp = ollama.chat(
            model=config.OLLAMA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional cover letter writer. Be concise and authentic. No <think> blocks.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Write a 3-sentence cover letter opening for {name} applying for "
                        f"{job_title} at {company}. "
                        f"Context about the candidate: {summary[:300]}. "
                        "Be specific, enthusiastic, and professional. No salutation."
                    ),
                },
            ],
            options={"temperature": 0.5, "num_predict": 200},
        )
        text = resp["message"]["content"].strip()
        # Strip any <think> blocks
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        logger.debug("Cover letter generated (%d chars)", len(text))
        return text
    except Exception as e:
        logger.warning("Cover letter generation failed: %s", e)
        return f"I am excited to apply for the {job_title} position at {company}."


# ── Main class ────────────────────────────────────────────────────────────────

class FormFiller:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def fill(self, job_id: int):
        """Open browser, pre-fill application form, pause for human review."""
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        application_url = job.get("application_url")
        if not application_url:
            raise ValueError(f"Job {job_id} has no application URL")

        resume_path_str = job.get("resume_path")
        resume_path = Path(resume_path_str) if resume_path_str else None

        resume_data = self._build_resume_data(job)
        platform = detect_platform(application_url)

        logger.info("Form filler starting — job %d, platform: %s, url: %s",
                    job_id, platform, application_url)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False, slow_mo=100)
            context = await browser.new_context(
                user_agent=config.USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            try:
                await page.goto(application_url, wait_until="networkidle", timeout=45000)
                await asyncio.sleep(2)

                filled_count = 0
                page_num = 1

                while True:
                    logger.debug("Filling page %d of form", page_num)
                    filled_count += await self._fill_page(page, resume_data, resume_path, platform)
                    await self._highlight_fields(page)

                    next_btn = await self._find_next_button(page)
                    if next_btn and page_num < 10:
                        logger.info("Multi-page form detected, navigating to page %d", page_num + 1)
                        await next_btn.click()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        await asyncio.sleep(1.5)
                        page_num += 1
                    else:
                        break

                logger.info("Pre-filled %d fields across %d page(s)", filled_count, page_num)
                logger.info("REVIEW: Inspect the browser, edit any fields, then close when done. DO NOT auto-submit.")

                self.db.log_action(job_id, "fill_form", "paused_for_review",
                                   {"fields_filled": filled_count, "platform": platform})

                await page.pause()

            except Exception as e:
                logger.error("Form filler error for job %d: %s", job_id, e, exc_info=True)
                self.db.log_action(job_id, "fill_form", "error", {"error": str(e)})
            finally:
                await context.close()
                await browser.close()

        logger.info("Browser closed — mark job as Applied in the dashboard if you submitted")

    async def _fill_page(self, page: Page, resume_data: dict,
                         resume_path: Path | None, platform: str) -> int:
        filled = 0
        elements = await page.query_selector_all("input, textarea, select")

        for element in elements:
            try:
                el_type = (await element.get_attribute("type") or "text").lower()
                if not (await element.is_visible()) or not (await element.is_enabled()):
                    continue
                if el_type in ("submit", "button", "reset", "hidden", "checkbox", "radio"):
                    continue

                accept = await element.get_attribute("accept") or ""
                label = await _get_field_label(page, element)
                field_key = _map_field(label, el_type, accept)

                if not field_key:
                    continue

                value = resume_data.get(field_key)
                if not value:
                    continue

                if el_type == "file" and field_key == "resume_upload":
                    if resume_path and resume_path.exists():
                        await element.set_input_files(str(resume_path))
                        await element.evaluate("el => el.setAttribute('data-autofilled','true')")
                        filled += 1
                        logger.debug("Uploaded resume to file field (label: '%s')", label)
                elif await element.evaluate("el => el.tagName.toLowerCase() === 'select'"):
                    try:
                        el_id = await element.get_attribute("id")
                        await page.select_option(f"#{el_id}", value=value)
                        filled += 1
                    except Exception as e:
                        logger.debug("Select field fill failed (label: '%s'): %s", label, e)
                else:
                    await element.fill(value)
                    await element.evaluate("el => el.setAttribute('data-autofilled','true')")
                    filled += 1
                    logger.debug("Filled field '%s' with key '%s'", label, field_key)
                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.warning("Field fill error: %s", e)
                continue

        return filled

    async def _highlight_fields(self, page: Page):
        await page.add_style_tag(content="""
            input[data-autofilled="true"],
            textarea[data-autofilled="true"] {
                border: 2px solid #22c55e !important;
                background-color: #f0fdf4 !important;
            }
            input:required:not([data-autofilled="true"]):not([type="file"]):not([type="hidden"]),
            textarea:required:not([data-autofilled="true"]) {
                border: 2px solid #ef4444 !important;
                background-color: #fff5f5 !important;
            }
        """)

    async def _find_next_button(self, page: Page):
        selectors = [
            "button[data-automation-id='bottom-navigation-next-button']",
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "[type='button']:has-text('Next')",
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible() and await btn.is_enabled():
                    return btn
            except Exception as e:
                logger.warning("Next button detection failed for selector '%s': %s", sel, e)
                continue
        return None

    def _build_resume_data(self, job: dict) -> dict:
        import os
        name = os.getenv("APPLICANT_NAME", "")
        email = os.getenv("APPLICANT_EMAIL", "")
        phone = os.getenv("APPLICANT_PHONE", "")
        location = os.getenv("APPLICANT_LOCATION", "")

        # Validate required applicant env vars and warn (not error) for missing ones
        _required_fields = {
            "APPLICANT_NAME": name,
            "APPLICANT_EMAIL": email,
            "APPLICANT_PHONE": phone,
            "APPLICANT_LOCATION": location,
        }
        for var_name, var_value in _required_fields.items():
            if not var_value:
                logger.warning(
                    "Environment variable %s is not set or empty; "
                    "form filling may be incomplete for this field", var_name
                )

        name_parts = name.split() if name else ["", ""]

        gap = {}
        if job.get("gap_analysis"):
            try:
                raw = job["gap_analysis"]
                gap = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Failed to parse gap_analysis JSON: %s", e)

        cover_letter = _generate_cover_letter(
            name=name,
            job_title=job.get("job_title", ""),
            company=job.get("company", ""),
            summary=(gap.get("strengths") or [""])[0],
        )

        return {
            "first_name": name_parts[0],
            "last_name": name_parts[-1] if len(name_parts) > 1 else "",
            "full_name": name,
            "email": email,
            "phone": phone,
            "linkedin": os.getenv("APPLICANT_LINKEDIN", ""),
            "github": os.getenv("APPLICANT_GITHUB", ""),
            "portfolio": os.getenv("APPLICANT_PORTFOLIO", ""),
            "location": location,
            "country": os.getenv("APPLICANT_COUNTRY", "United States"),
            "state": os.getenv("APPLICANT_STATE", ""),
            "zip": os.getenv("APPLICANT_ZIP", ""),
            "years_experience": os.getenv("YEARS_EXPERIENCE", ""),
            "cover_letter": cover_letter,
            "salary": os.getenv("APPLICANT_SALARY_EXPECTATION", ""),
            "start_date": os.getenv("APPLICANT_START_DATE", "2 weeks"),
            "resume_upload": "file_upload_handled_separately",
        }
