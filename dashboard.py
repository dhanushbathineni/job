"""
Streamlit Dashboard — Job Application Automation Hub
Orchestrates all four agents from a single UI.
Run: streamlit run dashboard.py
"""
import asyncio
import sys

# Windows requires ProactorEventLoop for subprocess support (Playwright)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import ollama
import pandas as pd
import streamlit as st

import config
from config import setup_logging
from db import DatabaseManager
from agents.scraper import JobScraper
from agents.assessor import ResumeAssessor
from agents.composer import ResumeComposer
from agents.form_filler import FormFiller
try:
    from agents.composer_v2 import ResumeComposerV2
    COMPOSER_V2_AVAILABLE = True
except ImportError:
    COMPOSER_V2_AVAILABLE = False
    ResumeComposerV2 = None

logger = setup_logging("jobbot.dashboard")


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Job Application Dashboard",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .main > div { padding-top: 1rem; }
    .stButton > button { width: 100%; }
    .status-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .badge-not-applied { background: #fee2e2; color: #991b1b; }
    .badge-in-progress { background: #fef9c3; color: #854d0e; }
    .badge-applied { background: #dcfce7; color: #166534; }
    div[data-testid="metric-container"] {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 12px;
    }
</style>
""", unsafe_allow_html=True)


# ── Cached resource initialization ───────────────────────────────────────────

@st.cache_resource
def init_app():
    config.ensure_dirs()
    db = DatabaseManager()
    db.initialize_schema()

    # Verify Ollama is reachable
    ollama_ok = True
    try:
        ollama.list()
    except Exception as e:
        logger.error("Ollama not reachable at %s: %s", config.OLLAMA_HOST, e)
        ollama_ok = False

    scraper = JobScraper(db)
    assessor = ResumeAssessor(db)
    composer = ResumeComposer(db)
    form_filler = FormFiller(db)
    composer_v2 = ResumeComposerV2(db) if COMPOSER_V2_AVAILABLE else None

    warnings = []
    if not config.MASTER_RESUME_PATH.exists():
        warnings.append(f"Master resume not found. Place it at: {config.MASTER_RESUME_PATH}")
    if not ollama_ok:
        warnings.append(
            f"Ollama is not running. Start it with: ollama serve  "
            f"(expected at {config.OLLAMA_HOST})"
        )
    if not COMPOSER_V2_AVAILABLE:
        warnings.append(
            "Composer V2 (LangGraph) not available - install with: pip install langgraph langchain-core"
        )

    return db, ollama_ok, scraper, assessor, composer, form_filler, composer_v2, warnings


result = init_app()
db: DatabaseManager = result[0]
ollama_ok: bool = result[1]
scraper: JobScraper = result[2]
assessor: ResumeAssessor = result[3]
composer: ResumeComposer = result[4]
form_filler: FormFiller = result[5]
composer_v2: Optional[ResumeComposerV2] = result[6]
startup_warnings: list = result[7]


# ── Startup warnings ──────────────────────────────────────────────────────────

if not ollama_ok:
    st.error(
        f"Ollama is not running. Start it with `ollama serve` "
        f"and ensure model `{config.OLLAMA_MODEL}` is pulled: "
        f"`ollama pull {config.OLLAMA_MODEL}`"
    )
    st.stop()

for warning in startup_warnings:
    st.warning(warning)


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("💼 Job Bot")
    st.divider()

    # Stats
    stats = db.get_stats()
    by_status = stats.get("by_status", {})
    st.metric("Total Jobs", stats.get("total", 0))
    col1, col2 = st.columns(2)
    col1.metric("Applied", by_status.get("Applied", 0))
    col2.metric("In Progress", by_status.get("In Progress", 0))

    st.divider()

    # Filters
    st.subheader("Filters")
    all_platforms = config.SUPPORTED_PLATFORMS
    selected_platforms = st.multiselect(
        "Platforms", all_platforms,
        default=all_platforms,
        key="platform_filter",
    )
    status_options = ["Not Applied", "In Progress", "Applied"]
    selected_statuses = st.multiselect(
        "Status", status_options,
        default=status_options,
        key="status_filter",
    )

    st.divider()

    # Scraper trigger
    st.subheader("Run Scraper")
    scrape_query = st.text_input("Job Title", value="Software Engineer", key="scrape_query")
    scrape_location = st.text_input("Location", value="United States", key="scrape_location")
    scrape_platforms = st.multiselect(
        "Platforms to Scrape", all_platforms,
        default=["google", "indeed"],
        key="scrape_platforms",
    )
    max_jobs = st.slider("Max jobs per platform", 5, 50, 20, key="max_jobs")

    if st.button("Start Scraping", type="primary", use_container_width=True):
        if not scrape_platforms:
            st.error("Select at least one platform.")
        else:
            progress_placeholder = st.empty()
            status_text = st.empty()

            def run_scraper():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(
                    scraper.scrape_all(
                        query=scrape_query,
                        location=scrape_location,
                        platforms=scrape_platforms,
                        max_per_platform=max_jobs,
                    )
                )
                return result

            with st.spinner("Scraping jobs... (browser may open)"):
                try:
                    result = run_scraper()
                    st.success(f"Done! {result['total_new']} new jobs added.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Scraper error: {e}")


# ── Main tabs ─────────────────────────────────────────────────────────────────

tab_jobs, tab_analytics, tab_settings = st.tabs(["Jobs", "Analytics", "Settings"])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: JOBS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_jobs:
    col_search, col_refresh = st.columns([5, 1])
    with col_search:
        search_term = st.text_input("Search jobs", placeholder="Filter by title, company, keyword...",
                                    label_visibility="collapsed", key="search_term")
    with col_refresh:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    # Load jobs with filters
    filters = {
        "platforms": selected_platforms,
        "statuses": selected_statuses,
        "search": search_term if search_term else None,
    }
    jobs = db.get_all_jobs(filters)

    if not jobs:
        st.info("No jobs found. Run the scraper to get started.")
    else:
        st.caption(f"Showing {len(jobs)} jobs")

        # ── Jobs table ─────────────────────────────────────────────────────────

        STATUS_EMOJI = {
            "Not Applied": "🔴",
            "In Progress": "🟡",
            "Applied": "🟢",
        }

        # Column headers
        header_cols = st.columns([0.4, 2.5, 2.0, 1.0, 0.8, 0.8, 3.5])
        header_cols[0].caption("#")
        header_cols[1].caption("Job Title")
        header_cols[2].caption("Company")
        header_cols[3].caption("Platform")
        header_cols[4].caption("Match")
        header_cols[5].caption("Status")
        header_cols[6].caption("Actions")
        st.divider()

        for job in jobs:
            job_id = job["id"]
            has_gap = bool(job.get("gap_analysis"))
            has_resume = bool(job.get("resume_path")) and Path(job["resume_path"]).exists()
            status = job.get("status", "Not Applied")
            match_score = job.get("match_score")

            row_cols = st.columns([0.4, 2.5, 2.0, 1.0, 0.8, 0.8, 3.5])
            row_cols[0].caption(str(job_id))
            row_cols[1].write(job.get("job_title") or "—")
            row_cols[2].write(job.get("company") or "—")
            row_cols[3].caption((job.get("platform") or "").title())

            if match_score is not None:
                color = "green" if match_score >= 70 else ("orange" if match_score >= 50 else "red")
                row_cols[4].markdown(f":{color}[**{match_score}%**]")
            else:
                row_cols[4].caption("—")

            row_cols[5].write(f"{STATUS_EMOJI.get(status, '')} {status}")

            # Action buttons
            btn_cols = row_cols[6].columns(4)

            # Assess button
            assess_label = "Re-Assess" if has_gap else "Assess"
            if btn_cols[0].button(assess_label, key=f"assess_{job_id}", use_container_width=True):
                with st.spinner(f"Assessing {job.get('job_title')}..."):
                    try:
                        assessor.assess(job_id)
                        st.success("Assessment complete!")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

            # Compose button (only enabled after assessment)
            compose_disabled = not has_gap

            # Composer version selection
            use_v2 = COMPOSER_V2_AVAILABLE and composer_v2 is not None
            if use_v2:
                composer_version = st.radio(
                    "Composer",
                    ["Standard", "V2 (Iterative)"],
                    key=f"composer_ver_{job_id}",
                    horizontal=True,
                    label_visibility="collapsed",
                )
            else:
                composer_version = "Standard"

            if btn_cols[1].button(
                "Compose", key=f"compose_{job_id}",
                disabled=compose_disabled,
                use_container_width=True,
            ):
                with st.spinner(f"Composing tailored resume ({composer_version})..."):
                    try:
                        if composer_version == "V2 (Iterative)" and composer_v2:
                            path = composer_v2.compose(job_id)
                            st.success(f"Saved: {Path(path).name} (Iterative workflow)")
                        else:
                            path = composer.compose(job_id)
                            st.success(f"Saved: {Path(path).name}")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

            # Download button
            if has_resume:
                with open(job["resume_path"], "rb") as f:
                    btn_cols[2].download_button(
                        "DOCX",
                        f,
                        file_name=Path(job["resume_path"]).name,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=f"dl_{job_id}",
                        use_container_width=True,
                    )
            else:
                btn_cols[2].button("DOCX", key=f"dl_{job_id}", disabled=True, use_container_width=True)

            # Apply button (only enabled after resume composed)
            if btn_cols[3].button(
                "Apply",
                key=f"apply_{job_id}",
                disabled=not has_resume,
                use_container_width=True,
                type="primary" if has_resume else "secondary",
            ):
                db.update_job_status(job_id, "In Progress")
                with st.spinner("Opening browser for form filling..."):
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(form_filler.fill(job_id))
                    except Exception as e:
                        st.error(f"Form filler error: {e}")

                # After form filler closes, prompt user
                did_submit = st.radio(
                    f"Did you submit the application for **{job.get('job_title')} @ {job.get('company')}**?",
                    ["Not yet", "Yes, I submitted"],
                    key=f"submitted_{job_id}",
                )
                if did_submit == "Yes, I submitted":
                    db.update_job_status(job_id, "Applied", datetime.utcnow().date().isoformat())
                    st.success("Marked as Applied!")
                    st.rerun()

            # ── Expandable detail panel ─────────────────────────────────────
            with st.expander(f"Details — {job.get('company', '')} · {job.get('job_title', '')}", expanded=False):
                detail_col1, detail_col2 = st.columns([3, 1])

                with detail_col1:
                    # Keywords
                    keywords_raw = job.get("keywords") or "[]"
                    if isinstance(keywords_raw, str):
                        try:
                            keywords = json.loads(keywords_raw)
                        except Exception:
                            keywords = []
                    else:
                        keywords = keywords_raw
                    if keywords:
                        st.markdown("**Keywords:** " + " · ".join(f"`{k}`" for k in keywords[:15]))

                    # Job description
                    st.markdown("**Job Description**")
                    st.text_area(
                        "", job.get("job_description") or "No description available.",
                        height=250, disabled=True, key=f"jd_{job_id}",
                    )

                    # Application link
                    if job.get("application_url"):
                        st.markdown(f"[Open Job Posting]({job['application_url']})", unsafe_allow_html=True)

                with detail_col2:
                    st.markdown("**Job Info**")
                    if job.get("date_posted"):
                        st.caption(f"Posted: {job['date_posted']}")
                    if job.get("salary_range"):
                        st.caption(f"Salary: {job['salary_range']}")
                    if job.get("job_type"):
                        st.caption(f"Type: {job['job_type']}")

                    # Gap analysis
                    if has_gap:
                        st.divider()
                        st.markdown("**Gap Analysis**")
                        gap = json.loads(job["gap_analysis"]) if isinstance(job["gap_analysis"], str) else job["gap_analysis"]

                        score = gap.get("match_score", 0)
                        score_color = "green" if score >= 70 else ("orange" if score >= 50 else "red")
                        st.markdown(f"Match Score: :{score_color}[**{score}%**]")

                        if gap.get("recommended_title"):
                            st.caption(f"Use title: *{gap['recommended_title']}*")

                        if gap.get("missing_keywords"):
                            st.markdown("**Missing Keywords:**")
                            st.warning(", ".join(gap["missing_keywords"][:10]))

                        if gap.get("critical_gaps"):
                            st.markdown("**Critical Gaps:**")
                            for g in gap["critical_gaps"][:3]:
                                st.error(g)

                        if gap.get("strengths"):
                            st.markdown("**Strengths:**")
                            for s in gap["strengths"][:3]:
                                st.success(s)

                    # Status update
                    st.divider()
                    new_status = st.selectbox(
                        "Update Status",
                        ["Not Applied", "In Progress", "Applied"],
                        index=["Not Applied", "In Progress", "Applied"].index(status),
                        key=f"status_sel_{job_id}",
                    )
                    if new_status != status:
                        applied_date = datetime.utcnow().date().isoformat() if new_status == "Applied" else None
                        db.update_job_status(job_id, new_status, applied_date)
                        st.rerun()

            st.divider()


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_analytics:
    stats = db.get_stats()
    by_status = stats.get("by_status", {})
    by_platform = stats.get("by_platform", {})
    total = stats.get("total", 0)
    applied = by_status.get("Applied", 0)

    st.subheader("Overview")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Jobs Tracked", total)
    m2.metric("Applied", applied)
    m3.metric("In Progress", by_status.get("In Progress", 0))
    m4.metric("Not Applied", by_status.get("Not Applied", 0))

    st.divider()

    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        if by_platform:
            st.subheader("Jobs by Platform")
            platform_df = (
                pd.DataFrame(list(by_platform.items()), columns=["Platform", "Count"])
                .set_index("Platform")
                .sort_values("Count", ascending=False)
            )
            st.bar_chart(platform_df)

    with chart_col2:
        if by_status:
            st.subheader("Application Status")
            status_icons = {"Not Applied": "🔴", "In Progress": "🟡", "Applied": "🟢"}
            for s, count in by_status.items():
                icon = status_icons.get(s, "")
                pct = round(count / total * 100) if total else 0
                st.markdown(f"{icon} **{s}** — {count} ({pct}%)")
                st.progress(pct / 100)

    # Timeline
    recent = stats.get("recent_by_day", [])
    if recent:
        st.subheader("Jobs Scraped Over Time")
        timeline_df = (
            pd.DataFrame(recent)
            .rename(columns={"day": "Date", "cnt": "New Jobs"})
            .set_index("Date")
        )
        st.area_chart(timeline_df)

    # Match score distribution
    all_jobs = db.get_all_jobs()
    scored_jobs = [j for j in all_jobs if j.get("match_score") is not None]
    if scored_jobs:
        st.subheader("Top Jobs by Match Score")
        scores_df = (
            pd.DataFrame(scored_jobs)[["company", "job_title", "match_score", "status"]]
            .sort_values("match_score", ascending=False)
            .head(20)
            .rename(columns={"company": "Company", "job_title": "Title",
                              "match_score": "Score %", "status": "Status"})
        )
        st.dataframe(scores_df, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_settings:
    st.subheader("Configuration")

    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("**Search Defaults**")
        default_query = st.text_input("Default Job Query",
                                       value=", ".join(config.JOB_ROLES),
                                       help="Comma-separated job titles")
        default_location = st.text_input("Default Location", value="United States")

        st.markdown("**Files**")
        st.caption(f"Database: `{config.DB_PATH}`")
        st.caption(f"Excel Export: `{config.EXCEL_PATH}`")

        # Master resume upload section
        st.markdown("**Master Resume**")
        resume_exists = config.MASTER_RESUME_PATH.exists()
        if resume_exists:
            mtime = datetime.fromtimestamp(config.MASTER_RESUME_PATH.stat().st_mtime)
            st.success(f"master_resume.docx found (last modified: {mtime.strftime('%Y-%m-%d %H:%M')})")
        else:
            st.error("master_resume.docx NOT found")

        uploaded_resume = st.file_uploader(
            "Upload new master resume (DOCX)",
            type=["docx"],
            key="resume_uploader",
        )
        if uploaded_resume is not None:
            if st.button("Replace Master Resume", type="primary"):
                try:
                    config.MASTER_RESUME_PATH.write_bytes(uploaded_resume.getvalue())
                    st.success("Master resume updated!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to save resume: {e}")

    with col_right:
        st.markdown("**Database Actions**")

        if st.button("Export to Excel", use_container_width=True):
            with st.spinner("Exporting..."):
                try:
                    path = db.export_to_excel()
                    with open(path, "rb") as f:
                        st.download_button(
                            "Download jobs.xlsx",
                            f,
                            file_name="jobs.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    st.success("Excel file ready!")
                except Exception as e:
                    st.error(f"Export failed: {e}")

        if st.button("Test DB Connection", use_container_width=True):
            try:
                stats = db.get_stats()
                st.success(f"DB OK — {stats['total']} jobs in database")
            except Exception as e:
                st.error(f"DB error: {e}")

        st.divider()
        st.markdown("**Batch Actions**")
        st.caption("Re-run gap analysis on all jobs with current master resume")

        if st.button("Re-assess All Jobs", use_container_width=True):
            all_jobs = db.get_all_jobs()
            unscored = [j for j in all_jobs if j.get("match_score") is None]
            st.info(f"Found {len(all_jobs)} jobs ({len(unscored)} unscored). Re-assessing all...")

            progress_bar = st.progress(0)
            status_text = st.empty()

            for i, job in enumerate(all_jobs):
                job_id = job["id"]
                status_text.text(f"Assessing job {i+1}/{len(all_jobs)}: {job.get('job_title', 'Unknown')} @ {job.get('company', 'Unknown')}")
                try:
                    # Clear existing gap analysis first
                    db.update_job_gap_analysis(job_id, {}, 0)
                    # Re-run assessment
                    assessor.assess(job_id)
                except Exception as e:
                    logger.error("Re-assessment failed for job %d: %s", job_id, e)
                progress_bar.progress((i + 1) / len(all_jobs))

            status_text.text("Re-assessment complete!")
            st.success(f"Re-assessed {len(all_jobs)} jobs with updated master resume.")
            st.rerun()

        st.divider()
        st.markdown("**Danger Zone**")
        confirm_delete = st.checkbox("I confirm I want to delete applied jobs")
        if st.button("Delete Applied Jobs", disabled=not confirm_delete,
                     type="secondary", use_container_width=True):
            with db.get_connection() as conn:
                conn.execute("DELETE FROM jobs WHERE status='Applied'")
            st.success("Applied jobs deleted.")
            st.rerun()

    st.divider()
    st.subheader("Environment Variables")
    st.markdown("Set these in your `.env` file:")
    st.code("""
ANTHROPIC_API_KEY=sk-ant-...
LINKEDIN_EMAIL=you@email.com
LINKEDIN_PASSWORD=yourpassword
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
MAX_JOBS_PER_PLATFORM=30
HEADLESS_BROWSER=false
""", language="bash")
