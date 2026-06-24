"""
Job Searcher
Multi-platform job search using jobspy (Indeed, LinkedIn, ZipRecruiter, Glassdoor).
Falls back to individual scrapers if jobspy is not available.
"""
import uuid
import time
from typing import List, Optional
from datetime import datetime, timedelta

from job_agent.models import JobPosting, JobPlatform
from job_agent.config import SearchConfig


class JobSearcher:
    def __init__(self, config: SearchConfig):
        self.config = config
        self._check_dependencies()

    def _check_dependencies(self):
        try:
            import jobspy  # noqa
            self._use_jobspy = True
            print("[search] Using jobspy for multi-platform search")
        except ImportError:
            self._use_jobspy = False
            print("[search] jobspy not found - install with: pip install python-jobspy")
            print("[search] Falling back to basic Indeed search")

    def search_all(self, keywords: Optional[List[str]] = None, locations: Optional[List[str]] = None) -> List[JobPosting]:
        """
        Run all configured searches and return deduplicated results.
        """
        keywords = keywords or self.config.keywords
        locations = locations or self.config.locations
        platforms = self.config.platforms

        print(f"[search] Searching {len(keywords)} keywords × {len(locations)} locations "
              f"on {platforms}")

        all_jobs: List[JobPosting] = []
        seen_ids = set()

        for keyword in keywords:
            for location in locations:
                jobs = self._search(keyword, location, platforms)
                for job in jobs:
                    if job.id not in seen_ids:
                        seen_ids.add(job.id)
                        all_jobs.append(job)
                time.sleep(2)  # Polite delay between searches

        # Filter by minimum salary where listed
        if self.config.min_salary:
            filtered = []
            for job in all_jobs:
                # Keep jobs with no salary listed (can't filter out unknown)
                if job.salary_max is None and job.salary_min is None:
                    filtered.append(job)
                elif job.salary_max and job.salary_max >= self.config.min_salary:
                    filtered.append(job)
                elif job.salary_min and job.salary_min >= self.config.min_salary * 0.9:
                    filtered.append(job)
            print(f"[search] {len(filtered)}/{len(all_jobs)} jobs pass salary filter (>${self.config.min_salary:,})")
            all_jobs = filtered

        # Filter excluded companies
        if self.config.exclude_companies:
            all_jobs = [j for j in all_jobs
                        if j.company.lower() not in [c.lower() for c in self.config.exclude_companies]]

        print(f"[search] Total unique jobs found: {len(all_jobs)}")
        return all_jobs

    def _search(self, keyword: str, location: str, platforms: List[str]) -> List[JobPosting]:
        if self._use_jobspy:
            return self._search_jobspy(keyword, location, platforms)
        else:
            return self._search_indeed_basic(keyword, location)

    def _search_jobspy(self, keyword: str, location: str, platforms: List[str]) -> List[JobPosting]:
        """Use the jobspy library for robust multi-platform search."""
        from jobspy import scrape_jobs
        import pandas as pd
        import math

        site_map = {
            "indeed":       "indeed",
            "linkedin":     "linkedin",
            "ziprecruiter": "zip_recruiter",
            "glassdoor":    "glassdoor",
        }
        sites = [site_map[p] for p in platforms if p in site_map]

        def _s(val, default="") -> str:
            if val is None:
                return default
            try:
                if isinstance(val, float) and math.isnan(val):
                    return default
            except Exception:
                pass
            s = str(val).strip()
            return default if s.lower() in ("nan", "none", "") else s

        def _scrape(site_names: list):
            return scrape_jobs(
                site_name=site_names,
                search_term=keyword,
                location=location,
                results_wanted=self.config.max_results_per_search,
                hours_old=168,          # 7-day window — more inventory from all platforms
                country_indeed="USA",
                linkedin_fetch_description=True,
            )

        # Try all platforms together; if that fails, retry each one individually
        # so a broken Glassdoor/ZipRecruiter response doesn't kill LinkedIn/Indeed
        df = None
        try:
            df = _scrape(sites)
        except Exception as e:
            print(f"[search] Multi-platform scrape failed ({e}), retrying per-platform…")
            frames = []
            for site in sites:
                try:
                    frames.append(_scrape([site]))
                    print(f"[search]   {site}: ok")
                except Exception as site_err:
                    print(f"[search]   {site}: failed — {site_err}")
            if frames:
                df = pd.concat(frames, ignore_index=True)

        if df is None or df.empty:
            print(f"[search] '{keyword}' in '{location}': 0 jobs (all platforms failed)")
            return []

        jobs = []
        platform_counts: dict = {}
        for _, row in df.iterrows():
            try:
                site_name = _s(row.get("site"), "indeed")
                platform = self._map_platform(site_name)
                job = JobPosting(
                    id=_s(row.get("id")) or str(uuid.uuid4()),
                    title=_s(row.get("title")),
                    company=_s(row.get("company")),
                    location=_s(row.get("location"), location),
                    description=_s(row.get("description")),
                    url=_s(row.get("job_url")),
                    platform=platform,
                    salary_min=self._safe_int(row.get("min_amount")),
                    salary_max=self._safe_int(row.get("max_amount")),
                    salary_text=_s(row.get("interval")) if row.get("min_amount") else None,
                    job_type=_s(row.get("job_type"), "fulltime"),
                    remote="remote" in _s(row.get("location"), "").lower(),
                    posted_date=self._parse_date(row.get("date_posted")),
                    easy_apply=bool(row.get("is_easy_apply", False)),
                )
                if job.title and job.company:
                    jobs.append(job)
                    platform_counts[site_name] = platform_counts.get(site_name, 0) + 1
            except Exception as e:
                print(f"[search] Warning: could not parse job row: {e}")
                continue

        breakdown = "  ".join(f"{k}:{v}" for k, v in sorted(platform_counts.items()))
        print(f"[search] '{keyword}' in '{location}': {len(jobs)} jobs  [{breakdown}]")
        return jobs

    def _search_indeed_basic(self, keyword: str, location: str) -> List[JobPosting]:
        """Basic Indeed scraper fallback using requests+BeautifulSoup."""
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError:
            print("[search] Install requests + beautifulsoup4 for fallback search")
            return []

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        params = {
            "q": keyword,
            "l": location,
            "sort": "date",
            "fromage": "3",
        }

        try:
            resp = requests.get(
                "https://www.indeed.com/jobs",
                params=params,
                headers=headers,
                timeout=10
            )
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"[search] Indeed request failed: {e}")
            return []

        jobs = []
        for card in soup.select("[data-jk]")[:self.config.max_results_per_search]:
            try:
                job_id = card.get("data-jk", str(uuid.uuid4()))
                title_el = card.select_one(".jobTitle span")
                company_el = card.select_one("[data-testid='company-name']")
                location_el = card.select_one("[data-testid='text-location']")
                salary_el = card.select_one(".salary-snippet-container")

                job = JobPosting(
                    id=job_id,
                    title=title_el.text.strip() if title_el else "",
                    company=company_el.text.strip() if company_el else "",
                    location=location_el.text.strip() if location_el else location,
                    description="",  # Need detail page for full description
                    url=f"https://www.indeed.com/viewjob?jk={job_id}",
                    platform=JobPlatform.INDEED,
                    salary_text=salary_el.text.strip() if salary_el else None,
                )
                if job.title:
                    jobs.append(job)
            except Exception:
                continue

        print(f"[search] Basic Indeed '{keyword}' in '{location}': {len(jobs)} jobs")
        return jobs

    @staticmethod
    def _map_platform(site: str) -> JobPlatform:
        mapping = {
            "indeed": JobPlatform.INDEED,
            "linkedin": JobPlatform.LINKEDIN,
            "zip_recruiter": JobPlatform.ZIPRECRUITER,
            "glassdoor": JobPlatform.GLASSDOOR,
        }
        return mapping.get(site.lower(), JobPlatform.INDEED)

    @staticmethod
    def _safe_int(val) -> Optional[int]:
        try:
            return int(float(val)) if val and str(val) not in ("nan", "None", "") else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_date(val) -> Optional[datetime]:
        if val is None:
            return None
        try:
            if isinstance(val, datetime):
                return val
            return datetime.fromisoformat(str(val))
        except (ValueError, TypeError):
            return None
