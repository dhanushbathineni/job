"""
Agent 1: Job Scraper
Scrapes LinkedIn, Indeed, Glassdoor, Wellfound, and Google Jobs using Playwright.
Extracts job details and uses Ollama (qwen3) to pull ATS keywords from descriptions.
"""
import asyncio
import sys

# Windows requires ProactorEventLoop for subprocess support (Playwright)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import ollama
import requests
from playwright.async_api import async_playwright, Page, BrowserContext

import config
from config import setup_logging
from db import DatabaseManager

logger = setup_logging("jobbot.scraper")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    """Strip tracking params for deduplication."""
    if not url:
        return url
    url = re.sub(r"[?&](utm_[^&=]+=[^&]*|trackingId=[^&]*|refId=[^&]*)", "", url)
    return url.rstrip("/?&")


async def _random_delay(page: Page, min_s: float = config.REQUEST_DELAY_MIN,
                        max_s: float = config.REQUEST_DELAY_MAX):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _human_scroll(page: Page, steps: int = 3):
    for _ in range(steps):
        await page.evaluate("window.scrollBy(0, Math.floor(Math.random()*400+200))")
        await asyncio.sleep(random.uniform(0.3, 0.8))


# ── Keyword extractor (Ollama) ────────────────────────────────────────────────

class KeywordExtractor:
    def extract(self, job_description: str) -> list[str]:
        if not job_description:
            return []
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a technical recruiter parsing job descriptions to extract ATS keywords. "
                    "Be precise and concise."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Extract the top 20 technical and professional keywords from this job description "
                    "that a resume must contain to pass ATS screening. "
                    "Return ONLY a JSON array of strings, no explanation.\n\n"
                    f"Job Description:\n{job_description[:4000]}"
                ),
            },
        ]
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = ollama.chat(
                    model=config.OLLAMA_MODEL,
                    messages=messages,
                    options={"temperature": 0.1},
                )
                raw = resp["message"]["content"].strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1].lstrip("json").strip()
                # Strip <think>...</think> tags that qwen3 may emit
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                return json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("Keyword extraction JSON parse failed (attempt %d/%d): %s", attempt + 1, config.MAX_RETRIES, e)
                if attempt == config.MAX_RETRIES - 1:
                    logger.warning("Keyword extraction failed after retries, using regex fallback: %s", e)
            except Exception as e:
                logger.warning("Keyword extraction unexpected error (attempt %d/%d): %s", attempt + 1, config.MAX_RETRIES, e)
                if attempt == config.MAX_RETRIES - 1:
                    logger.warning("Keyword extraction failed after retries, using regex fallback: %s", e)
                    return list({
                        w for w in re.findall(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b", job_description)
                        if len(w) > 3
                    })[:20]
                logger.debug("Keyword extraction attempt %d failed: %s", attempt + 1, e)
                time.sleep(2)
        return []


# ── Platform scrapers ──────────────────────────────────────────────────────────

class LinkedInScraper:
    """LinkedIn scraper using RapidAPI JSearch instead of Playwright."""

    def __init__(self):
        self.api_key = config.RAPIDAPI_KEY
        self.api_host = config.RAPIDAPI_HOST
        self.base_url = f"https://{self.api_host}"
        self.headers = {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": self.api_host,
            "Content-Type": "application/json",
        }

    async def scrape(self, context: BrowserContext, query: str, location: str,
                     max_jobs: int) -> list[dict]:
        """Scrape LinkedIn jobs via RapidAPI JSearch."""
        jobs: list[dict] = []
        if not self.api_key:
            logger.error("RAPIDAPI_KEY is not set. Set it in .env to use the LinkedIn/JSearch scraper.")
            return jobs
        try:
            # Calculate number of pages needed (10 jobs per page)
            num_pages = (max_jobs // 10) + (1 if max_jobs % 10 > 0 else 0)
            num_pages = min(num_pages, 10)  # Limit to 10 pages max

            for page_num in range(1, num_pages + 1):
                if len(jobs) >= max_jobs:
                    break

                # Build query targeting LinkedIn jobs
                querystring = {
                    "query": f"{query} in {location}",
                    "page": str(page_num),
                    "num_pages": "1",
                    "country": "us",
                    "date_posted": "all",
                }

                logger.debug("LinkedIn/RapidAPI: fetching page %d for '%s' in '%s'",
                             page_num, query, location)

                # Run synchronous requests in executor to not block async loop
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: requests.get(
                        f"{self.base_url}/search",
                        headers=self.headers,
                        params=querystring,
                        timeout=30,
                    )
                )

                if response.status_code != 200:
                    logger.warning("LinkedIn/RapidAPI: HTTP %d - %s",
                                   response.status_code, response.text[:200])
                    continue

                data = response.json()
                api_jobs = data.get("data", [])

                if not api_jobs:
                    logger.debug("LinkedIn/RapidAPI: no jobs returned on page %d", page_num)
                    break

                for api_job in api_jobs:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._transform_job(api_job)
                    if job:
                        jobs.append(job)
                        logger.debug("LinkedIn/RapidAPI: extracted '%s' @ '%s'",
                                     job.get("job_title"), job.get("company"))

                # Rate limit: RapidAPI typically allows 1 request per second on free tier
                await asyncio.sleep(1.0)

            logger.info("LinkedIn/RapidAPI: fetched %d jobs", len(jobs))

        except Exception as e:
            logger.error("LinkedIn/RapidAPI scraper error: %s", e, exc_info=True)

        return jobs

    def _transform_job(self, api_job: dict) -> dict | None:
        """Transform RapidAPI job format to our standard format."""
        try:
            # Extract job details from the API response
            job_title = api_job.get("job_title")
            employer_name = api_job.get("employer_name")

            if not job_title or not employer_name:
                return None

            # Get application URL - prefer direct apply link, fallback to LinkedIn job page
            apply_link = (api_job.get("job_apply_link") or
                         api_job.get("job_google_link") or
                         api_job.get("job_posted_at_url") or
                         api_job.get("job_apply_links", [{}])[0].get("link"))

            job_description = api_job.get("job_description") or api_job.get("job_snippet")

            # Determine if this came from LinkedIn (RapidAPI aggregates multiple sources)
            # We'll still mark it as linkedin platform for consistency
            return {
                "platform": "linkedin",
                "job_title": job_title,
                "company": employer_name,
                "location": api_job.get("job_location"),
                "job_description": job_description,
                "application_url": _normalize_url(apply_link),
                "salary_range": self._format_salary(api_job),
                "job_type": api_job.get("job_employment_type"),
                "date_posted": api_job.get("job_posted_at_datetime_utc"),
            }
        except Exception as e:
            logger.debug("LinkedIn/RapidAPI: job transform error: %s", e)
            return None

    def _format_salary(self, api_job: dict) -> str | None:
        """Format salary information from API fields."""
        min_salary = api_job.get("job_min_salary")
        max_salary = api_job.get("job_max_salary")
        salary_period = api_job.get("job_salary_period")
        salary_currency = api_job.get("job_salary_currency", "USD")

        if min_salary and max_salary:
            period = f"/{salary_period}" if salary_period else ""
            return f"{salary_currency} {min_salary:,.0f} - {max_salary:,.0f}{period}"
        elif min_salary:
            period = f"/{salary_period}" if salary_period else ""
            return f"{salary_currency} {min_salary:,.0f}{period}"
        elif max_salary:
            period = f"/{salary_period}" if salary_period else ""
            return f"{salary_currency} {max_salary:,.0f}{period}"

        return api_job.get("job_salary") or api_job.get("estimated_salary")


class IndeedScraper:
    async def scrape(self, context: BrowserContext, query: str, location: str,
                     max_jobs: int) -> list[dict]:
        page = await context.new_page()
        jobs: list[dict] = []
        try:
            offset = 0
            while len(jobs) < max_jobs:
                url = (
                    f"https://www.indeed.com/jobs?q={quote_plus(query)}"
                    f"&l={quote_plus(location)}&start={offset}&fromage=7"
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await _human_scroll(page)

                if "challenge" in page.url or await page.query_selector("#captcha-box"):
                    logger.warning("Indeed: CAPTCHA detected — please solve it in the browser")
                    await page.pause()

                cards = await page.query_selector_all(".job_seen_beacon")
                if not cards:
                    break

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break
                    job = await self._extract_card(page, card)
                    if job:
                        jobs.append(job)
                        logger.debug("Indeed: extracted '%s' @ '%s'", job.get("job_title"), job.get("company"))
                    await _random_delay(page, 1.0, 2.5)

                offset += 10
        except Exception as e:
            logger.error("Indeed scraper error: %s", e, exc_info=True)
        finally:
            await page.close()
        return jobs

    async def _extract_card(self, page: Page, card) -> dict | None:
        try:
            title_el = await card.query_selector("h2.jobTitle a")
            company_el = await card.query_selector('[data-testid="company-name"]')
            location_el = await card.query_selector('[data-testid="text-location"]')
            salary_el = await card.query_selector('[data-testid="attribute_snippet_testid"]')

            title = (await title_el.inner_text()).strip() if title_el else None
            company = (await company_el.inner_text()).strip() if company_el else None
            location = (await location_el.inner_text()).strip() if location_el else None
            salary = (await salary_el.inner_text()).strip() if salary_el else None
            href = await title_el.get_attribute("href") if title_el else None
            job_url = f"https://www.indeed.com{href}" if href and href.startswith("/") else href

            description = None
            if job_url:
                detail_page = await page.context.new_page()
                try:
                    await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
                    desc_el = await detail_page.query_selector("#jobDescriptionText")
                    if desc_el:
                        description = (await desc_el.inner_text()).strip()
                except Exception as e:
                    logger.debug("Indeed: failed to fetch description: %s", e)
                finally:
                    await detail_page.close()

            return {
                "platform": "indeed",
                "job_title": title,
                "company": company,
                "location": location,
                "job_description": description,
                "salary_range": salary,
                "application_url": _normalize_url(job_url),
            }
        except Exception as e:
            logger.debug("Indeed: card extract error: %s", e)
            return None


class GlassdoorScraper:
    async def scrape(self, context: BrowserContext, query: str, location: str,
                     max_jobs: int) -> list[dict]:
        page = await context.new_page()
        jobs: list[dict] = []
        try:
            url = (
                f"https://www.glassdoor.com/Job/jobs.htm"
                f"?sc.keyword={quote_plus(query)}&locT=N&locId=1"
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(page)
            await self._dismiss_modal(page)

            while len(jobs) < max_jobs:
                cards = await page.query_selector_all("li[data-test='jobListing']")
                if not cards:
                    cards = await page.query_selector_all(".react-job-listing")

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break
                    job = await self._extract_card(page, card)
                    if job:
                        jobs.append(job)
                        logger.debug("Glassdoor: extracted '%s' @ '%s'", job.get("job_title"), job.get("company"))
                    await _random_delay(page, 0.8, 2.0)
                    await self._dismiss_modal(page)

                next_btn = await page.query_selector('[data-test="pagination-next"]')
                if not next_btn:
                    break
                await next_btn.click()
                await _random_delay(page)
                await page.wait_for_load_state("networkidle", timeout=15000)
                await self._dismiss_modal(page)

        except Exception as e:
            logger.error("Glassdoor scraper error: %s", e, exc_info=True)
        finally:
            await page.close()
        return jobs

    async def _dismiss_modal(self, page: Page):
        try:
            close = await page.query_selector('[alt="Close"], button[class*="modal_closeIcon"]')
            if close:
                await close.click()
                await asyncio.sleep(0.5)
            else:
                await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _extract_card(self, page: Page, card) -> dict | None:
        try:
            await card.click()
            await asyncio.sleep(1.5)
            await self._dismiss_modal(page)

            title_el = await page.query_selector('[data-test="job-title"]')
            company_el = await page.query_selector('[data-test="employer-name"]')
            location_el = await page.query_selector('[data-test="emp-location"]')
            desc_el = await page.query_selector('[class*="JobDetails_jobDescription"]')
            salary_el = await page.query_selector('[data-test="detailSalary"]')

            return {
                "platform": "glassdoor",
                "job_title": (await title_el.inner_text()).strip() if title_el else None,
                "company": (await company_el.inner_text()).strip() if company_el else None,
                "location": (await location_el.inner_text()).strip() if location_el else None,
                "job_description": (await desc_el.inner_text()).strip() if desc_el else None,
                "salary_range": (await salary_el.inner_text()).strip() if salary_el else None,
                "application_url": _normalize_url(page.url),
            }
        except Exception as e:
            logger.debug("Glassdoor: card extract error: %s", e)
            return None


class WellfoundScraper:
    async def scrape(self, context: BrowserContext, query: str, location: str,
                     max_jobs: int) -> list[dict]:
        page = await context.new_page()
        jobs: list[dict] = []
        try:
            role_slug = query.lower().replace(" ", "-")
            url = f"https://wellfound.com/jobs?role={role_slug}&remote=true"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(page)

            prev_count = 0
            stall = 0
            while len(jobs) < max_jobs:
                cards = await page.query_selector_all('[data-test="StartupResult"]')
                new_cards = cards[prev_count:]
                prev_count = len(cards)

                for card in new_cards:
                    if len(jobs) >= max_jobs:
                        break
                    job = await self._extract_card(page, card)
                    if job:
                        jobs.append(job)
                        logger.debug("Wellfound: extracted '%s' @ '%s'", job.get("job_title"), job.get("company"))

                if not new_cards:
                    stall += 1
                    if stall >= 3:
                        break

                await _human_scroll(page, steps=5)
                await _random_delay(page)

        except Exception as e:
            logger.error("Wellfound scraper error: %s", e, exc_info=True)
        finally:
            await page.close()
        return jobs

    async def _extract_card(self, page: Page, card) -> dict | None:
        try:
            title_el = await card.query_selector('[data-test="job-title"]')
            company_el = await card.query_selector('[data-test="startup-name"]')
            location_el = await card.query_selector('[data-test="location"]')
            salary_el = await card.query_selector('[data-test="salary"]')
            link_el = await card.query_selector("a[href*='/jobs/']")

            href = await link_el.get_attribute("href") if link_el else None
            job_url = f"https://wellfound.com{href}" if href and href.startswith("/") else href

            description = None
            if job_url:
                detail_page = await page.context.new_page()
                try:
                    await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
                    desc_el = await detail_page.query_selector('[data-test="job-description"], .styles_description__y4YFF')
                    if desc_el:
                        description = (await desc_el.inner_text()).strip()
                except Exception as e:
                    logger.debug("Wellfound: failed to fetch description: %s", e)
                finally:
                    await detail_page.close()

            return {
                "platform": "wellfound",
                "job_title": (await title_el.inner_text()).strip() if title_el else None,
                "company": (await company_el.inner_text()).strip() if company_el else None,
                "location": (await location_el.inner_text()).strip() if location_el else None,
                "job_description": description,
                "salary_range": (await salary_el.inner_text()).strip() if salary_el else None,
                "application_url": _normalize_url(job_url),
            }
        except Exception as e:
            logger.debug("Wellfound: card extract error: %s", e)
            return None


class GoogleJobsScraper:
    async def scrape(self, context: BrowserContext, query: str, location: str,
                     max_jobs: int) -> list[dict]:
        page = await context.new_page()
        jobs: list[dict] = []
        try:
            search_url = (
                f"https://www.google.com/search"
                f"?q={quote_plus(query)}+jobs+{quote_plus(location)}&ibp=htl;jobs"
            )
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(page)

            first_card = await page.query_selector('[data-ved][jsname]')
            if first_card:
                await first_card.click()
                await asyncio.sleep(1.5)

            cards = await page.query_selector_all('[role="treeitem"], [jsname="uj1hJb"]')
            logger.debug("Google Jobs: found %d cards", len(cards))

            for card in cards[:max_jobs]:
                try:
                    await card.click()
                    await asyncio.sleep(1.0)
                    job = await self._extract_panel(page)
                    if job:
                        jobs.append(job)
                        logger.debug("Google Jobs: extracted '%s' @ '%s'", job.get("job_title"), job.get("company"))
                except Exception as e:
                    logger.debug("Google Jobs: card error: %s", e)
                    continue

        except Exception as e:
            logger.error("Google Jobs scraper error: %s", e, exc_info=True)
        finally:
            await page.close()
        return jobs

    async def _extract_panel(self, page: Page) -> dict | None:
        try:
            title_el = await page.query_selector(".KLsYvd, [data-hveid] h2")
            company_el = await page.query_selector(".nJlQNd, [data-hveid] .vNEEBe")
            location_el = await page.query_selector(".Qk80Jf")
            desc_el = await page.query_selector(".HBvzbc, [data-hveid] .NgUYpe")
            apply_els = await page.query_selector_all("a.pMhGee, a[href*='apply']")

            apply_url = None
            for el in apply_els:
                href = await el.get_attribute("href")
                if href and "google.com" not in href:
                    apply_url = href
                    break

            return {
                "platform": "google",
                "job_title": (await title_el.inner_text()).strip() if title_el else None,
                "company": (await company_el.inner_text()).strip() if company_el else None,
                "location": (await location_el.inner_text()).strip() if location_el else None,
                "job_description": (await desc_el.inner_text()).strip() if desc_el else None,
                "application_url": _normalize_url(apply_url or page.url),
            }
        except Exception as e:
            logger.debug("Google Jobs: panel extract error: %s", e)
            return None


# ── Main orchestrator ──────────────────────────────────────────────────────────

class JobScraper:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.keyword_extractor = KeywordExtractor()
        self._scrapers: dict[str, Any] = {
            "linkedin": LinkedInScraper(),
            "indeed": IndeedScraper(),
            "glassdoor": GlassdoorScraper(),
            "wellfound": WellfoundScraper(),
            "google": GoogleJobsScraper(),
        }

    async def scrape_all(
        self,
        query: str,
        location: str = "United States",
        platforms: list[str] | None = None,
        max_per_platform: int = config.MAX_JOBS_PER_PLATFORM,
        progress_callback=None,
    ) -> dict:
        if platforms is None:
            platforms = config.SUPPORTED_PLATFORMS

        total_new = 0
        results = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=config.HEADLESS_BROWSER,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=config.USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            for platform in platforms:
                scraper_cls = self._scrapers.get(platform)
                if not scraper_cls:
                    logger.warning("Unknown platform: %s", platform)
                    continue

                logger.info("Starting scrape: %s — query='%s' location='%s'", platform, query, location)
                session_id = self.db.start_scrape_session(platform, query, location)

                try:
                    raw_jobs = await scraper_cls.scrape(context, query, location, max_per_platform)
                    new_count = 0

                    for job in raw_jobs:
                        if not job or not job.get("application_url"):
                            continue
                        if job.get("job_description"):
                            job["keywords"] = self.keyword_extractor.extract(job["job_description"])
                        else:
                            job["keywords"] = []

                        job_id = self.db.upsert_job(job)
                        if job_id > 0:
                            new_count += 1

                    self.db.finish_scrape_session(session_id, len(raw_jobs), new_count)
                    total_new += new_count
                    results[platform] = {"found": len(raw_jobs), "new": new_count}
                    logger.info("Finished %s: %d found, %d new", platform, len(raw_jobs), new_count)

                    if progress_callback:
                        progress_callback(platform, new_count)

                except Exception as e:
                    self.db.fail_scrape_session(session_id, str(e))
                    results[platform] = {"error": str(e)}
                    logger.error("Platform %s failed: %s", platform, e, exc_info=True)

            await context.close()
            await browser.close()

        try:
            self.db.export_to_excel()
            logger.info("Excel export updated")
        except Exception as e:
            logger.warning("Excel export failed: %s", e)

        return {"total_new": total_new, "by_platform": results}


async def main():
    """CLI entry point for testing."""
    config.ensure_dirs()
    db = DatabaseManager()
    db.initialize_schema()

    scraper = JobScraper(db)
    result = await scraper.scrape_all(
        query="Software Engineer",
        location="United States",
        platforms=["google"],
        max_per_platform=10,
    )
    logger.info("Scrape complete: %s", result)


if __name__ == "__main__":
    asyncio.run(main())
