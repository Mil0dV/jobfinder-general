import re

from jobfinder.config.profile import TIMEZONE
from jobfinder.sources.base import (
    _HTTP_HEADERS,
    _location_terms,
    _make_job,
    _matches_location,
    _title_matches,
)

CLIMATETECHLIST_AIRTABLE_URL = "https://airtable.com/embed/appo9WyVgYvpap792/shrbAhhxKqvWjaJMs"


def search_climatetechlist(query: str, location: str = "") -> list:
    """Search ClimateTechList's Airtable shared view (~10k climate jobs). No API key."""
    import requests

    try:
        resp = requests.get(CLIMATETECHLIST_AIRTABLE_URL, headers=_HTTP_HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text

        url_match = re.search(r'urlWithParams["\s:]*"(.*?)"', html)
        if not url_match:
            print("   ClimateTechList: could not extract API URL")
            return []
        api_url = "https://airtable.com" + url_match.group(1).replace("\\u002F", "/").replace(
            "\\/", "/"
        )

        headers = {
            **_HTTP_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "x-airtable-inter-service-client": "webClient",
            "x-time-zone": TIMEZONE,
        }
        app_id_match = re.search(r'"x-airtable-application-id"\s*:\s*"([^"]*)"', html)
        if app_id_match:
            headers["x-airtable-application-id"] = app_id_match.group(1)

        data = requests.get(
            api_url, headers=headers, cookies=resp.cookies.get_dict(), timeout=30
        ).json()

        table = data.get("data", {}).get("table", {})
        col_by_name = {c.get("name", "").lower().strip(): c["id"] for c in table.get("columns", [])}

        def find_col(*candidates):
            for name in candidates:
                if name in col_by_name:
                    return col_by_name[name]
            for cand in candidates:
                for col_name, col_id in col_by_name.items():
                    if cand in col_name:
                        return col_id
            return None

        col_title = find_col("position title", "title", "position")
        col_company = find_col("company", "employer")
        col_apply = find_col("apply to job", "apply")
        col_location = find_col("job location", "location")
        col_salary = find_col("salary range (beta)", "salary", "compensation")
        col_date = find_col("date first listed", "date")
        col_remote = find_col("remote")

        terms = _location_terms(location)
        jobs = []
        for row in table.get("rows", []):
            cells = row.get("cellValuesByColumnId", {})

            def cell(col_id, default=""):
                val = cells.get(col_id) if col_id else None
                if isinstance(val, dict):
                    return val.get("url", "")
                return str(val).strip() if val else default

            title = re.sub(r"^[✨\U0001f31f⭐\U0001f525]+", "", cell(col_title)).strip()
            if not _title_matches(title, query):
                continue
            job_location = cell(col_location).split("\n")[0].strip()
            if not _matches_location(job_location, terms):
                continue
            jobs.append(
                _make_job(
                    title=title,
                    company=cell(col_company, "Unknown"),
                    url=cell(col_apply),
                    location=job_location,
                    salary=cell(col_salary, "Not listed"),
                    posted_date=cell(col_date)[:10],
                    wfh_policy=cell(col_remote, "Unknown"),
                    source="climatetechlist",
                )
            )
        print(f"   ClimateTechList: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   ClimateTechList error: {e}")
        return []
