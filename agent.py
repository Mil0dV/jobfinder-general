"""Climate/Green Tech Job Vacancy Finder Agent using OpenAI.

This agent specializes in finding job vacancies in the climate and green technology sector.
It uses DuckDuckGo to find real job postings and outputs CSV with job details.
"""

import os
import csv
import io
import json
import inspect
import re
from openai import OpenAI
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()


class OpenAIAgent:
    def __init__(self, model="gpt-4o", tools=None, system_prompt=""):
        # Hardcoded for testing - same as jobfinder-general
        self.client = OpenAI(api_key="REDACTED_KEY")
        self.model = model
        self.tools = tools or []
        self.system_prompt = system_prompt
        self.tool_map = {tool.__name__: tool for tool in self.tools}

    def _get_tool_schemas(self):
        schemas = []
        for tool in self.tools:
            signature = inspect.signature(tool)
            parameters = {
                "type": "object",
                "properties": {},
                "required": []
            }
            for name, param in signature.parameters.items():
                param_type = "string"
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == bool:
                    param_type = "boolean"
                
                parameters["properties"][name] = {
                    "type": param_type,
                    "description": f"Parameter {name}"
                }
                if param.default == inspect.Parameter.empty:
                    parameters["required"].append(name)
            
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.__name__,
                    "description": tool.__doc__ or "",
                    "parameters": parameters
                }
            })
        return schemas

    def run(self, prompt):
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        tool_schemas = self._get_tool_schemas()
        
        # Agentic loop - keep running until we have a final response
        max_iterations = 10  # Increased for more tool calls
        for _ in range(max_iterations):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tool_schemas if tool_schemas else None,
                tool_choice="auto" if tool_schemas else None
            )
            
            response_message = response.choices[0].message
            
            if not response_message.tool_calls:
                # No more tool calls, return final response
                return response_message.content
            
            # Process tool calls
            messages.append(response_message)
            
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)
                
                if function_name in self.tool_map:
                    tool_function = self.tool_map[function_name]
                    function_response = tool_function(**function_args)
                    
                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": json.dumps(function_response)
                    })
        
        return "Max iterations reached without completing the search."


def search_arbeitnow(query: str, location: str = "") -> list:
    """Search Arbeitnow API for EU/DACH jobs. Free, no API key required.
    
    Args:
        query: Job search query (job title)
        location: Location for the job search (optional)
        
    Returns:
        List of job dictionaries with standardized format
    """
    import requests
    
    try:
        # Arbeitnow API endpoint - free, no auth
        url = "https://www.arbeitnow.com/api/job-board-api"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        jobs = []
        query_words = query.lower().split()
        location_lower = location.lower() if location else ""
        
        # Climate-related keywords for filtering
        climate_keywords = ['climate', 'sustainability', 'renewable', 'solar', 'wind', 
                           'green', 'cleantech', 'carbon', 'energy', 'environmental',
                           'ev', 'electric', 'hydrogen', 'battery', 'nachhaltig',
                           'umwelt', 'energie', 'klima']
        
        # Common job title translations (EN -> DE)
        title_translations = {
            'engineer': ['engineer', 'ingenieur', 'entwickler'],
            'software': ['software', 'it', 'developer', 'entwickler'],
            'manager': ['manager', 'leiter', 'lead'],
            'data': ['data', 'daten', 'analytics'],
            'product': ['product', 'produkt'],
            'senior': ['senior', 'sr', 'lead'],
        }
        
        # Country to cities/regions mapping for DACH
        country_cities = {
            'germany': ['berlin', 'munich', 'münchen', 'hamburg', 'frankfurt', 'cologne', 'köln', 
                       'stuttgart', 'düsseldorf', 'dresden', 'leipzig', 'nuremberg', 'nürnberg',
                       'hannover', 'bremen', 'essen', 'dortmund', 'germany', 'deutschland', 'de'],
            'switzerland': ['zurich', 'zürich', 'geneva', 'genf', 'basel', 'bern', 'lausanne',
                           'winterthur', 'lucerne', 'luzern', 'switzerland', 'schweiz', 'ch'],
            'austria': ['vienna', 'wien', 'graz', 'linz', 'salzburg', 'innsbruck', 
                       'austria', 'österreich', 'at'],
            'eu': ['remote', 'europe', 'eu', 'emea'],
        }
        
        # Expand query words to include translations
        expanded_query_words = set(query_words)
        for word in query_words:
            if word in title_translations:
                expanded_query_words.update(title_translations[word])
        
        # Expand location to include cities
        location_terms = set()
        if location_lower:
            location_terms.add(location_lower)
            for country, cities in country_cities.items():
                if location_lower in [country] + cities:
                    location_terms.update(cities)
        
        for job in data.get("data", []):
            title = job.get("title", "").lower()
            description = job.get("description", "").lower()
            job_location = job.get("location", "").lower()
            tags = [t.lower() for t in job.get("tags", [])]
            
            # Fuzzy match: check if ANY query word appears in title/description/tags
            title_words = title.split() + tags
            matches_query = any(
                any(qw in tw for tw in title_words) 
                for qw in expanded_query_words
            )
            
            if not matches_query:
                continue
                
            # Check location if specified
            if location_terms:
                location_match = (
                    any(term in job_location for term in location_terms) or
                    "remote" in job_location or 
                    "anywhere" in job_location
                )
                if not location_match:
                    continue
            
            # Check for climate relevance
            is_climate = any(kw in title or kw in description or kw in tags 
                           for kw in climate_keywords)
            
            jobs.append({
                "title": job.get("title", ""),
                "company": job.get("company_name", "Unknown"),
                "url": job.get("url", ""),
                "location": job.get("location", ""),
                "salary": "Not listed",  # Arbeitnow doesn't always have salary
                "posted_date": job.get("created_at", "Recently"),
                "wfh_policy": "Remote" if job.get("remote", False) else "On-site",
                "snippet": job.get("description", "")[:500] if job.get("description") else "",
                "source": "arbeitnow",
                "is_climate_relevant": is_climate
            })
        
        print(f"   Arbeitnow: Found {len(jobs)} matching jobs")
        return jobs
        
    except Exception as e:
        print(f"   Arbeitnow API error: {e}")
        return []


def search_remoteok(query: str, climate_only: bool = True) -> list:
    """Search RemoteOK API for remote jobs. Free JSON feed.
    
    Args:
        query: Job search query (job title)
        climate_only: If True, filter for climate/cleantech tagged jobs
        
    Returns:
        List of job dictionaries with standardized format
    """
    import requests
    
    try:
        # RemoteOK JSON API - can filter by tags
        # Climate-related tags to search for
        climate_tags = ["climate", "cleantech", "sustainability", "green", "energy"]
        
        url = "https://remoteok.com/api"
        headers = {"User-Agent": "JobFinderBot/1.0"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        jobs = []
        query_lower = query.lower()
        
        for job in data:
            # Skip the first item (it's metadata in RemoteOK API)
            if not isinstance(job, dict) or "position" not in job:
                continue
                
            title = job.get("position", "").lower()
            company = job.get("company", "").lower()
            tags = [t.lower() for t in job.get("tags", [])]
            description = job.get("description", "").lower()
            
            # Check if job matches query
            if query_lower not in title and query_lower not in " ".join(tags):
                continue
            
            # Check for climate relevance
            is_climate = any(ct in tags or ct in title or ct in description 
                           for ct in climate_tags)
            
            if climate_only and not is_climate:
                continue
            
            # Parse salary if available
            salary = "Not listed"
            if job.get("salary_min") and job.get("salary_max"):
                salary = f"${job['salary_min']:,} - ${job['salary_max']:,}"
            
            jobs.append({
                "title": job.get("position", ""),
                "company": job.get("company", "Unknown"),
                "url": job.get("url", ""),
                "location": "Remote",
                "salary": salary,
                "posted_date": job.get("date", "Recently"),
                "wfh_policy": "Remote",
                "snippet": job.get("description", "")[:500] if job.get("description") else "",
                "source": "remoteok",
                "tags": job.get("tags", []),
                "is_climate_relevant": is_climate
            })
        
        print(f"   RemoteOK: Found {len(jobs)} matching jobs")
        return jobs
        
    except Exception as e:
        print(f"   RemoteOK API error: {e}")
        return []





# Shared OpenAI client for validation (reuses existing key)
_validation_client = None

def _get_validation_client():
    """Get or create a shared OpenAI client for validation."""
    global _validation_client
    if _validation_client is None:
        # Use same key as the agent
        _validation_client = OpenAI(api_key="REDACTED_KEY")
    return _validation_client





def search_adzuna(query: str, location: str = "", country: str = "ch") -> list:
    """Search Adzuna API for jobs. Requires ADZUNA_APP_ID and ADZUNA_API_KEY in .env.
    
    Args:
        query: Job search query
        location: City/region filter
        country: Country code (gb, us, de, at, ch, fr, etc.)
        
    Returns:
        List of job dictionaries with standardized format
    """
    import requests
    
    app_id = os.getenv("ADZUNA_APP_ID")
    api_key = os.getenv("ADZUNA_API_KEY")
    
    if not app_id or not api_key:
        print("   Adzuna: Skipped (no API key configured)")
        return []
    
    try:
        # Country code mapping
        country_map = {
            "switzerland": "ch", "germany": "de", "austria": "at",
            "uk": "gb", "united kingdom": "gb", "netherlands": "nl",
            "france": "fr", "usa": "us", "united states": "us"
        }
        country_code = country_map.get(country.lower(), country.lower())
        
        url = f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/1"
        params = {
            "app_id": app_id,
            "app_key": api_key,
            "what": query,
            "results_per_page": 50,
            "content-type": "application/json"
        }
        # If location matches the country name, omit 'where' param (country is already selected by endpoint)
        redundant_locations = {
            "ch": ["switzerland", "schweiz", "suisse", "ch"],
            "de": ["germany", "deutschland", "de"],
            "at": ["austria", "österreich", "at"],
            "gb": ["uk", "united kingdom", "britain", "gb"],
            "us": ["usa", "united states", "us"]
        }
        
        include_location = True
        if location and country_code in redundant_locations:
            if location.lower() in redundant_locations[country_code]:
                include_location = False
        
        if location and include_location:
            params["where"] = location

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        jobs = []
        for job in data.get("results", []):
            jobs.append({
                "title": job.get("title", ""),
                "company": job.get("company", {}).get("display_name", "Unknown"),
                "url": job.get("redirect_url", ""),
                "location": job.get("location", {}).get("display_name", ""),
                "salary": f"{job.get('salary_min', 'N/A')} - {job.get('salary_max', 'N/A')}" if job.get("salary_min") else "Not listed",
                "posted_date": job.get("created", "Recently"),
                "wfh_policy": "Remote" if "remote" in job.get("title", "").lower() or "remote" in job.get("description", "").lower() else "On-site",
                "snippet": job.get("description", "")[:500],
                "source": "adzuna",
                "is_climate_relevant": False  # Will be set by LLM validation
            })
        
        print(f"   Adzuna: Found {len(jobs)} jobs")
        return jobs
        
    except Exception as e:
        print(f"   Adzuna API error: {e}")
        return []


# List of climate-focused companies with public Greenhouse job boards
GREENHOUSE_CLIMATE_COMPANIES = [
    "climeworks", "watershed", "pachama", "charm-industrial", "rondo-energy",
    "antora-energy", "redwood-materials", "form-energy", "terra-do", "arcadia",
    "weavegrid", "span", "samsara", "terra", "northvolt", "fluence"
]



def search_greenhouse(query: str, companies: list = None) -> list:
    """Search Greenhouse job boards for climate companies. No API key needed.
    
    Args:
        query: Job search query
        companies: List of company slugs to search (uses climate defaults if None)
        
    Returns:
        List of job dictionaries with standardized format
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    companies = companies or GREENHOUSE_CLIMATE_COMPANIES
    query_lower = query.lower()
    
    # Check if query logic should be OR (comma separated) or AND (space separated)
    if "," in query_lower:
        query_parts = [p.strip() for p in query_lower.split(",")]
        match_mode = "OR"
    else:
        query_parts = query_lower.split()
        match_mode = "AND"
        
    jobs = []
    
    def fetch_company_jobs(company_slug):
        """Fetch jobs from a single company's Greenhouse board."""
        company_jobs = []
        try:
            url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return []
            
            data = resp.json()
            for job in data.get("jobs", []):
                title = job.get("title", "").lower()
                
                # Srict matching logic
                if match_mode == "OR":
                    # Match ANY of the comma-separated parts (substring check is okay for full phrases)
                    is_match = any(part in title for part in query_parts)
                else:
                    # Match ALL words for space-separated queries (e.g. "software engineer")
                    is_match = all(part in title for part in query_parts)
                
                if is_match:
                    company_jobs.append({
                        "title": job.get("title", ""),
                        "company": company_slug.replace("-", " ").title(),
                        "url": job.get("absolute_url", ""),
                        "location": job.get("location", {}).get("name", "Unknown"),
                        "salary": "Not listed",
                        "posted_date": job.get("updated_at", "Recently")[:10] if job.get("updated_at") else "Recently",
                        "wfh_policy": "Remote" if "remote" in job.get("location", {}).get("name", "").lower() else "On-site",
                        "snippet": "",  # Greenhouse doesn't include description in list
                        "source": "greenhouse",
                        "is_climate_relevant": True  # These are curated climate companies
                    })
        except Exception:
            pass
        return company_jobs
    
    # Fetch jobs from all companies in parallel
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_company_jobs, c): c for c in companies}
        for future in as_completed(futures):
            jobs.extend(future.result())
    
    print(f"   Greenhouse: Found {len(jobs)} jobs from {len(companies)} climate companies")
    return jobs


CLIMATETECHLIST_AIRTABLE_URL = "https://airtable.com/embed/appo9WyVgYvpap792/shrbAhhxKqvWjaJMs"


def search_climatetechlist(query: str, location: str = "") -> list:
    """Search ClimateTechList (Airtable-backed) for climate tech jobs. No API key needed.

    Scrapes the shared Airtable view that powers climatetechlist.com (~11k jobs, ~580 companies).

    Args:
        query: Job search query (job title)
        location: Location filter (optional)

    Returns:
        List of job dictionaries with standardized format
    """
    import requests

    try:
        # 1. Fetch the shared view embed page
        resp = requests.get(CLIMATETECHLIST_AIRTABLE_URL, timeout=15)
        resp.raise_for_status()
        html = resp.text
        cookies = resp.cookies.get_dict()

        # 2. Extract the internal API URL from urlWithParams
        url_match = re.search(r'urlWithParams:\s*"(.*?)"', html)
        if not url_match:
            url_match = re.search(r'"urlWithParams"\s*:\s*"(.*?)"', html)
        if not url_match:
            print("   ClimateTechList: Could not extract API URL from embed page")
            return []

        api_path = url_match.group(1).replace("\\u002F", "/").replace("\\/", "/")
        api_url = "https://airtable.com" + api_path

        # 3. Build required headers (X-Requested-With and x-time-zone are mandatory)
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "x-airtable-inter-service-client": "webClient",
            "x-time-zone": "Europe/Zurich",
        }

        # Extract x-airtable-application-id from page
        app_id_match = re.search(r'"x-airtable-application-id"\s*:\s*"([^"]*)"', html)
        if app_id_match:
            headers["x-airtable-application-id"] = app_id_match.group(1)

        # 4. Fetch the actual data
        data_resp = requests.get(api_url, headers=headers, cookies=cookies, timeout=30)
        data_resp.raise_for_status()
        data = data_resp.json()

        # 5. Extract column definitions and rows
        table_data = data.get("data", {}).get("table", {})
        columns = table_data.get("columns", [])
        rows = table_data.get("rows", [])

        # Build column name -> ID mapping (case-insensitive)
        col_by_name = {}
        for col in columns:
            col_by_name[col.get("name", "").lower().strip()] = col["id"]

        # Map known columns by name (fuzzy substring match as fallback)
        def find_col(exact_names, substring_fallbacks=None):
            for name in exact_names:
                if name.lower() in col_by_name:
                    return col_by_name[name.lower()]
            if substring_fallbacks:
                for col_name, col_id in col_by_name.items():
                    if any(s in col_name for s in substring_fallbacks):
                        return col_id
            return None

        col_title = find_col(["position title"], ["title", "position"])
        col_company = find_col(["company"], ["company", "employer"])
        col_apply = find_col(["apply to job"], ["apply"])
        col_job_location = find_col(["job location"], ["job location"])
        col_location = find_col(["location"], ["location"])
        col_salary = find_col(
            ["\U0001f4b0  salary range (beta)", "salary range (beta)"],
            ["salary", "compensation"],
        )
        col_date = find_col(["date first listed"], ["date", "listed"])
        col_remote = find_col(["remote"], ["remote"])

        # 6. Parse query matching logic (same OR/AND as greenhouse)
        query_lower = query.lower()
        if "," in query_lower:
            query_parts = [p.strip() for p in query_lower.split(",")]
            match_mode = "OR"
        else:
            query_parts = query_lower.split()
            match_mode = "AND"

        location_lower = location.lower() if location else ""

        # 7. Iterate rows and filter
        jobs = []
        for row in rows:
            cells = row.get("cellValuesByColumnId", {})

            # Extract title (formula type, plain string, may have emoji prefix)
            title = ""
            if col_title and col_title in cells:
                title = str(cells[col_title] or "")
            # Strip leading emoji/sparkle markers (e.g. "✨Software Engineer")
            clean_title = re.sub(r"^[\u2728\U0001f31f\u2b50\U0001f525]+", "", title).strip()

            # Extract company (formula type, plain string)
            company = "Unknown"
            if col_company and col_company in cells:
                company = str(cells[col_company] or "Unknown")

            # Match query against title and company
            match_text = f"{clean_title} {company}".lower()
            if match_mode == "OR":
                is_match = any(part in match_text for part in query_parts)
            else:
                is_match = all(part in match_text for part in query_parts)

            if not is_match:
                continue

            # Extract location (prefer "Job Location" formula, fallback to "Location")
            job_location = ""
            loc_col = col_job_location or col_location
            if loc_col and loc_col in cells:
                val = cells[loc_col]
                if isinstance(val, str):
                    # Formula fields may have multiline duplicates; take first line
                    job_location = val.split("\n")[0].strip()
                elif val:
                    job_location = str(val)

            # Filter by location if specified
            if location_lower:
                loc_text = job_location.lower()
                if location_lower not in loc_text and "remote" not in loc_text:
                    continue

            # Extract URL from "Apply to Job" button field
            job_url = ""
            if col_apply and col_apply in cells:
                val = cells[col_apply]
                if isinstance(val, dict):
                    job_url = val.get("url", "")
                elif isinstance(val, str):
                    job_url = val

            # Extract salary (formula type, often empty)
            salary = "Not listed"
            if col_salary and col_salary in cells:
                val = cells[col_salary]
                if val:
                    salary = str(val).strip()
                if not salary:
                    salary = "Not listed"

            # Extract date (formula type, ISO date string)
            posted_date = "Recently"
            if col_date and col_date in cells:
                val = cells[col_date]
                if val:
                    posted_date = str(val)[:10]

            # Extract remote/WFH (formula type, text like "🌴Remote allowed")
            wfh_policy = "Unknown"
            if col_remote and col_remote in cells:
                val = cells[col_remote]
                if val:
                    wfh_policy = str(val).strip()

            jobs.append({
                "title": clean_title or title,
                "company": company,
                "url": job_url,
                "location": job_location,
                "salary": salary,
                "posted_date": posted_date,
                "wfh_policy": wfh_policy,
                "snippet": "",
                "source": "climatetechlist",
                "is_climate_relevant": True,
            })

        print(f"   ClimateTechList: Found {len(jobs)} matching jobs")
        return jobs

    except Exception as e:
        print(f"   ClimateTechList error: {e}")
        return []


def validate_jobs_batch(jobs: list, search_query: str) -> list:
    """Validate a batch of jobs using LLM to save token costs and time.
    
    Args:
        jobs: List of job dictionaries (max 20 recommended)
        search_query: Search query string
        
    Returns:
        List of validation dictionaries (same order)
    """
    if not jobs:
        return []
        
    try:
        client = _get_validation_client()
        
        # Prepare batch prompt
        jobs_text = ""
        for i, job in enumerate(jobs):
            desc = job.get("snippet", "") or job.get("description", "")
            jobs_text += f"ID {i}: {job.get('title')} at {job.get('company')} - {desc[:200]}\n"
            
        prompt = f"""Evaluate these job postings.
        
SEARCH QUERY: "{search_query}"

JOBS:
{jobs_text}

Respond with a JSON object where keys are job IDs (0, 1, etc.) and values are objects with:
- relevant: true/false. Does the ROLE match the search query? (Ignore climate matching here).
- score: 0-5. Climate relevance score based on the COMPANY and its mission.
- reason: brief explanation.

SCORING GUIDE (Climate Relevance):
5 = Core Climate Mission. The COMPANY is 100% focused on climate/environment (e.g., Climeworks, Tesla, Vestas).
4 = Strong Climate Focus. Company has major green product lines or strong tangible impact.
3 = Moderate. Company has some sustainability focus or this is a sustainability role at a normal company.
2 = Indirect. Standard company with "green" initiatives or ESG reporting.
1 = Low. No obvious climate connection.
0 = None. Oil & Gas, heavy polluters, or completely irrelevant.

IMPORTANT: 
- A "Software Engineer" at a Solar company gets a HIGH score (5).
- A "Sustainability Manager" at a Bank gets a MODERATE score (3).
- An "Accountant" at an Oil company gets a LOW score (0).
- The 'score' depends on the COMPANY/MISSION.
- The 'relevant' flag depends on the JOB TITLE matching the query.

Example:
{{
  "0": {{"relevant": true, "score": 5, "reason": "Role matches, company is a leader in Carbon Capture"}},
  "1": {{"relevant": false, "score": 0, "reason": "Role mismatch (Sales vs Engineer)"}}
}}"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1000
        )
        
        results_map = json.loads(response.choices[0].message.content)
        
        validations = []
        for i in range(len(jobs)):
            res = results_map.get(str(i)) or results_map.get(i)
            if res:
                validations.append({
                    "is_relevant": res.get("relevant", True),
                    "climate_score": res.get("score", 0),
                    "reason": res.get("reason", "")
                })
            else:
                validations.append({"is_relevant": True, "climate_score": 0, "reason": "Validation missing"})
                
        return validations
        
    except Exception as e:
        print(f"   Batch validation error: {e}")
        # Return fallback for all
        return [{"is_relevant": True, "climate_score": 0, "reason": "Batch error"} for _ in jobs]




def search_jobs(query: str, location: str = "", max_results: int = 50, climate_focus: bool = True) -> dict:
    """Search for job vacancies across multiple free APIs (Arbeitnow + RemoteOK + Greenhouse + Adzuna + ClimateTechList).

    This is a unified search function that:
    1. Queries Arbeitnow API (EU/DACH focus, free, no auth)
    2. Queries RemoteOK API (global remote, free JSON feed)
    3. Queries ClimateTechList (Airtable-backed climate tech job board)
    4. Applies SDG-based climate relevance scoring
    5. Deduplicates and ranks results
    
    Args:
        query: Job search query (job title)
        location: Location for the job search (optional, mainly for Arbeitnow)
        max_results: Maximum number of results to return (default 50)
        climate_focus: If True, prioritize climate-relevant jobs
        
    Returns:
        Dictionary containing aggregated job search results
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    # Clean the query
    clean_query = query.lower()
    prefixes = ["find ", "search for ", "looking for ", "jobs in ", "jobs for ", 
                "in the climate and green tech sector"]
    for prefix in prefixes:
        clean_query = clean_query.replace(prefix, "")
    clean_query = clean_query.strip()
    
    print(f"   Searching for '{clean_query}' jobs" + (f" in {location}" if location else ""))
    
    # Determine country code for Adzuna based on location
    adzuna_country = "ch"  # default
    if location:
        loc_lower = location.lower()
        if any(x in loc_lower for x in ["germany", "deutschland", "berlin", "munich", "münchen"]):
            adzuna_country = "de"
        elif any(x in loc_lower for x in ["austria", "österreich", "vienna", "wien"]):
            adzuna_country = "at"
        elif any(x in loc_lower for x in ["switzerland", "schweiz", "zurich", "zürich", "geneva"]):
            adzuna_country = "ch"
    
    # 1. Search all APIs in parallel
    all_jobs = []
    
    # Adzuna query expansion: Search generic + specific keywords to find relevant roles buried in 1500+ results
    adzuna_queries = [clean_query]
    if len(clean_query.split()) < 4:
        # Append specific terms to find deep matches
        adzuna_queries.extend([f"{clean_query} {kw}" for kw in ["climate", "sustainability", "renewable", "energy"]])
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(search_arbeitnow, clean_query, location): "arbeitnow",
            executor.submit(search_remoteok, clean_query, climate_only=False): "remoteok",
            executor.submit(search_climatetechlist, clean_query, location): "climatetechlist",
            # executor.submit(search_greenhouse, clean_query): "greenhouse",  # Disabled
        }
        
        # Add Adzuna queries
        for q in set(adzuna_queries):
             futures[executor.submit(search_adzuna, q, location, adzuna_country)] = f"adzuna_{q}"
        
        for future in as_completed(futures):
            source = futures[future]
            try:
                jobs = future.result()
                all_jobs.extend(jobs)
            except Exception as e:
                print(f"   Error from {source}: {e}")
    
    # 2. Deduplicate by URL
    seen_urls = set()
    unique_jobs = []
    for job in all_jobs:
        url = job.get("url", "").split("?")[0].strip()
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_jobs.append(job)
    
    print(f"   Found {len(unique_jobs)} unique jobs, validating with LLM (in batches)...")
    
    # 3. Validate jobs in batches
    validated_jobs = []
    batch_size = 15
    
    for i in range(0, len(unique_jobs), batch_size):
        batch = unique_jobs[i:i+batch_size]
        print(f"   Validating batch {i//batch_size + 1}/{(len(unique_jobs)-1)//batch_size + 1}...")
        
        validations = validate_jobs_batch(batch, clean_query)
        
        for job, val in zip(batch, validations):
            if val.get("is_relevant", True):
                job["climate_score"] = val.get("climate_score", 0)
                job["validation_reason"] = val.get("reason", "")
                validated_jobs.append(job)

    # 4. Sort: climate-relevant jobs first
    if climate_focus:
        validated_jobs.sort(key=lambda x: -x.get("climate_score", 0))
    
    # 5. Limit results
    final_jobs = validated_jobs[:max_results]
    
    # Stats - Lowered threshold to 2 as per user request
    climate_jobs = sum(1 for j in final_jobs if j.get("climate_score", 0) >= 2)
    
    print(f"   Final: {len(final_jobs)} relevant jobs ({climate_jobs} with climate score >= 2)")
    
    return {
        "status": "success",
        "query": query,
        "location": location,
        "total_found": len(final_jobs),
        "climate_relevant_count": climate_jobs,
        "sources": ["arbeitnow", "remoteok", "greenhouse", "adzuna", "climatetechlist"],
        "jobs": final_jobs
    }


# Simple JSON cache for company info
CACHE_FILE = "company_cache.json"

def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_to_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except:
        pass

def get_company_info(company_name: str) -> dict:
    """Search for company info (greentech focus, WFH policy) with caching."""
    if not company_name:
        return {"status": "error", "message": "No company name provided"}
    
    cache = load_cache()
    if company_name in cache:
        print(f"   (Using cached info for {company_name})")
        return cache[company_name]
    
    print(f"   Researching {company_name}...")
    try:
        with DDGS() as ddgs:
            # Combined query to save requests
            about_q = f"{company_name} company about sustainability climate cleantech"
            wfh_q = f"{company_name} remote work policy hybrid office"
            
            about_results = list(ddgs.text(about_q, max_results=3))
            wfh_results = list(ddgs.text(wfh_q, max_results=2))
        
        info = {
            "status": "success",
            "company": company_name,
            "about_snippets": " ".join([r.get("body", "") for r in about_results])[:800],
            "greentech_snippets": " ".join([r.get("body", "") for r in about_results])[:600],
            "wfh_snippets": " ".join([r.get("body", "") for r in wfh_results])[:500]
        }
        
        cache[company_name] = info
        save_to_cache(cache)
        return info
    except Exception as e:
        return {"status": "error", "error": str(e)}


def save_csv(jobs_data: str) -> dict:
    """Append job data to the jobs.csv file, handling new columns dynamically.
    
    Args:
        jobs_data: CSV-formatted string with headers: title,company,location,wfh_policy,salary,posted_date,company_description,url
        
    Returns:
        Dictionary with save status and stats
    """
    import csv
    from io import StringIO
    
    filepath = "jobs.csv"
    
    try:
        # Parse the new jobs data
        new_reader = csv.DictReader(StringIO(jobs_data))
        new_jobs = list(new_reader)
        if not new_jobs:
            return {"status": "success", "message": "No jobs to save"}

        # Load existing jobs
        existing_jobs = []
        try:
            with open(filepath, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_jobs = list(reader)
        except FileNotFoundError:
            pass

        # Helper to normalize URL for deduplication
        def norm_url(u): return u.split('?')[0].strip()

        existing_urls = {norm_url(j.get("url", "")) for j in existing_jobs}
        
        # Filter duplicates
        unique_new_jobs = []
        skipped_count = 0
        for job in new_jobs:
            url = norm_url(job.get("url", ""))
            if url and url not in existing_urls:
                unique_new_jobs.append(job)
                existing_urls.add(url)
            else:
                skipped_count += 1
        
        if not unique_new_jobs:
            return {"status": "success", "added": 0, "skipped": skipped_count}

        # Determine all fieldnames (old + new)
        all_keys = set()
        for j in existing_jobs + unique_new_jobs:
            all_keys.update(j.keys())
        
        # Define standard order
        preferred_order = ["title", "company", "location", "salary", "posted_date", "wfh_policy", "company_description", "url"]
        fieldnames = [k for k in preferred_order if k in all_keys] + [k for k in all_keys if k not in preferred_order]

        # Write everything back (overwrite to update headers)
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(existing_jobs + unique_new_jobs)
            
        return {
            "status": "success",
            "added": len(unique_new_jobs),
            "skipped": skipped_count,
            "total_in_file": len(existing_jobs) + len(unique_new_jobs)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Initialize the agent with a focused prompt for CSV output
root_agent = OpenAIAgent(
    model="gpt-4o-2024-08-06",
    system_prompt="""You are a job search automation agent that finds climate/green tech jobs using multiple APIs.

DATA SOURCES (search_jobs queries all automatically):
- Arbeitnow API: EU/DACH focus, Germany/Austria/Switzerland
- RemoteOK API: Global remote jobs
- Greenhouse API: Curated climate tech companies (Climeworks, Watershed, etc.)
- Adzuna API: 16+ countries (if configured)

LLM VALIDATION: Each job is validated for:
- Title relevance (filters out "Art Director" when searching "CTO")
- Climate score 0-5 (based on job description, not just keywords)

WORKFLOW:
1. Call search_jobs with job title and location. Returns structured JSON with validated jobs.
2. Call save_csv with CSV-formatted job data.
3. Show a summary with job links grouped by source.

CSV FORMAT for save_csv:
title,company,location,salary,posted_date,wfh_policy,climate_score,source,url
"Title","Company","Location","Salary","Date","Remote","3","arbeitnow","https://..."

FIELD MAPPING from search_jobs results:
- title, company, url, location, salary, posted_date, wfh_policy: Direct from job object
- climate_score: LLM-generated (0-5, higher = more climate relevant)
- source: arbeitnow, remoteok, greenhouse, or adzuna

RULES:
- Use structured data from search_jobs
- Jobs with climate_score >= 3 are highly relevant
- Show job links grouped by source
""",
    tools=[search_jobs, get_company_info, save_csv],
)
