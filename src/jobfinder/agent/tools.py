"""Company research tool for the agent."""

import json

CACHE_FILE = "company_cache.json"


def get_company_info(company_name: str) -> dict:
    """Search for company info (greentech focus, WFH policy) with caching."""
    if not company_name:
        return {"status": "error", "message": "No company name provided"}

    try:
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    if company_name in cache:
        return cache[company_name]

    print(f"   Researching {company_name}...")
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            about = list(
                ddgs.text(f"{company_name} company climate sustainability mission", max_results=3)
            )
            wfh = list(ddgs.text(f"{company_name} remote work policy", max_results=2))
        info = {
            "status": "success",
            "company": company_name,
            "about_snippets": " ".join(r.get("body", "") for r in about)[:800],
            "wfh_snippets": " ".join(r.get("body", "") for r in wfh)[:500],
        }
        cache[company_name] = info
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        return info
    except Exception as e:
        return {"status": "error", "error": str(e)}
