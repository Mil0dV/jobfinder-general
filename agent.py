"""
Climate/Green Tech Job Finder Agent
An advanced, agentic pipeline for searching, scoring, and tailoring applications.
"""

import os
import re
import json
import csv
import asyncio
import inspect
import requests
import traceback
from datetime import datetime
from typing import List, Dict, Any, Optional
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- CONFIGURATION & CONSTANTS ---

CLIMATE_KEYWORDS = [
    'climate', 'sustainability', 'renewable', 'solar', 'wind', 
    'green', 'cleantech', 'carbon', 'energy', 'environmental',
    'ev', 'electric', 'hydrogen', 'battery', 'nachhaltig',
    'umwelt', 'energie', 'klima', 'net zero', 'decarbonization'
]

PRODUCT_KEYWORDS = [
    'product manager', 'product owner', 'product lead', 
    'head of product', 'director of product', 'chief product',
    'vp product', 'product director'
]

SENIORITY_KEYWORDS = ['senior', 'sr', 'lead', 'principal', 'director', 'head']

TECH_KEYWORDS = ['saas', 'b2b', 'platform', 'api', 'data', 'ai', 'ml', 'devops', 'aws']

STARTUP_KEYWORDS = ['startup', 'scale-up', 'scaleup', 'early stage', 'series a', 'series b', 'seed funded']

# --- CORE LOGIC: SCORING ENGINE ---

class ScoringEngine:
    """Handles high-fidelity job matching based on the candidate's profile."""
    
    @staticmethod
    def calculate_score(job: Dict[str, Any]) -> Dict[str, Any]:
        score = 0
        title = job.get('title', '').lower()
        description = job.get('description', '').lower() or job.get('snippet', '').lower()
        location = job.get('location', '').lower()
        company = job.get('company', '').lower()
        
        # 1. Product Role Detection (+30)
        is_product = any(kw in title for kw in PRODUCT_KEYWORDS)
        if is_product:
            score += 30
            
        # 2. Climate/Sustainability Relevance (+25)
        is_climate = any(kw in title or kw in description or kw in company for kw in CLIMATE_KEYWORDS)
        if is_climate:
            score += 25
            
        # 3. Seniority Level (+20)
        is_senior = any(kw in title for kw in SENIORITY_KEYWORDS)
        if is_senior:
            score += 20
            
        # 4. Location / Remote Affinity (+15)
        # Prioritize Switzerland/Zurich/Basel or Remote
        if any(loc in location for loc in ['switzerland', 'zurich', 'basel', 'geneva', 'ch']):
            score += 15
        elif 'remote' in location or 'anywhere' in location:
            score += 10
            
        # 5. Startup/Scale-up Bonus (+10)
        if any(kw in description or kw in company for kw in STARTUP_KEYWORDS):
            score += 10
            
        # 6. Tech/SaaS Expertise (+5)
        if any(kw in description for kw in TECH_KEYWORDS):
            score += 5
            
        # Clamp score
        final_score = min(max(score, 0), 100)
        
        return {
            "match_score": final_score,
            "is_product_role": is_product,
            "is_climate_related": is_climate,
            "seniority_level": 'senior' if is_senior else 'mid'
        }

# --- AGENTIC CORE ---

class OpenAIAgent:
    def __init__(self, model="gpt-4o", tools=None, system_prompt=""):
        # Use environment variable, fallback to a safe empty string if not found
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("⚠️ WARNING: OPENAI_API_KEY not found in environment.")
        
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.tools = tools or []
        self.system_prompt = system_prompt
        self.tool_map = {tool.__name__: tool for tool in self.tools}

    def _get_tool_schemas(self):
        schemas = []
        for tool in self.tools:
            signature = inspect.signature(tool)
            parameters = {"type": "object", "properties": {}, "required": []}
            for name, param in signature.parameters.items():
                p_type = "string"
                if param.annotation == int: p_type = "integer"
                elif param.annotation == bool: p_type = "boolean"
                parameters["properties"][name] = {"type": p_type, "description": f"Parameter {name}"}
                if param.default == inspect.Parameter.empty:
                    parameters["required"].append(name)
            schemas.append({
                "type": "function",
                "function": {"name": tool.__name__, "description": tool.__doc__ or "", "parameters": parameters}
            })
        return schemas

    def run(self, prompt: str):
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        tool_schemas = self._get_tool_schemas()
        
        for _ in range(10):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tool_schemas if tool_schemas else None,
                tool_choice="auto" if tool_schemas else None
            )
            
            msg = response.choices[0].message
            if not msg.tool_calls:
                return msg.content
            
            messages.append(msg)
            for tool_call in msg.tool_calls:
                func_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                if func_name in self.tool_map:
                    res = self.tool_map[func_name](**args)
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": func_name, "content": json.dumps(res, default=str)})
        return "Max iterations reached."

# --- TOOLS (SCRAPERS & ENRICHMENT) ---

def search_arbeitnow(query: str, location: str = "") -> List[Dict]:
    """Search Arbeitnow API for EU/DACCH jobs."""
    import requests
    try:
        url = "https://www.arbeitnow.com/api/job-board-api"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        q = query.lower()
        loc = location.lower()
        
        for job in data.get("data", []):
            title = job.get("title", "").lower()
            desc = job.get("description", "").lower()
            j_loc = job.get("location", "").lower()
            
            if q in title or q in desc:
                if loc and loc not in j_loc and 'remote' not in j_loc:
                    continue
                jobs.append({
                    "title": job.get("title"),
                    "company": job.get("company_name"),
                    "url": job.get("url"),
                    "location": job.get("location"),
                    "description": job.get("description", ""),
                    "salary": "Not listed",
                    "posted_date": job.get("created_at", ""),
                    "source": "arbeitnow"
                })
        return jobs
    except Exception as e:
        print(f"Arbeitnow error: {e}")
        return []

def search_remoteok(query: str, climate_only: bool = True) -> List[Dict]:
    """Search RemoteOK API for remote jobs."""
    import requests
    try:
        url = "https://remoteok.com/api"
        resp = requests.get(url, headers={"User-Agent": "JobFinderBot/1.0"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        q = query.lower()
        tags = ["climate", "cleantech", "green", "energy", "sustainability"]
        
        for job in data[1:]: # Skip metadata
            title = job.get("position", "").lower()
            tags_list = [t.lower() for t in job.get("tags", [])]
            
            if q in title or any(t in title for t in tags_list):
                is_climate = any(ct in tags_list or ct in title for ct in tags)
                if climate_only and not is_climate:
                    continue
                    
                jobs.append({
                    "title": job.get("position"),
                    "company": job.get("company"),
                    "url": job.get("url"),
                    "location": "Remote",
                    "description": job.get("description", ""),
                    "salary": "Not listed",
                    "posted_date": job.get("date", ""),
                    "source": "remoteok"
                })
        return jobs
    except Exception as e:
        print(f"RemoteOK error: {e}")
        return []

def search_greenhouse(query: str) -> List[Dict]:
    """Scrape Greenhouse boards for climate companies."""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    COMPANIES = ["climeworks", "watershed", "pachama", "northvolt", "form-energy"]
    q = query.lower()
    jobs = []

    def fetch(slug):
        try:
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
            r = requests.get(url, timeout=10)
            if r.status_code != 200: return []
            found = []
            for j in r.json().get("jobs", []):
                title = j.get("title", "").lower()
                if q in title:
                    found.append({
                        "title": j.get("title"),
                        "company": slug.replace("-", " ").title(),
                        "url": j.get("absolute_url"),
                        "location": j.get("location", {}).get("name", "Unknown"),
                        "description": "",
                        "salary": "Not listed",
                        "posted_date": j.get("updated_at", "")[:10],
                        "source": "greenhouse"
                    })
            return found
        except: return []

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(fetch, c) for c in COMPANIES]
        for f in as_completed(futures):
            jobs.extend(f.result())
    return jobs

def search_climatetechlist(query: str) -> List[Dict]:
    """Scrapes the ClimateTechList Airtable view."""
    import requests
    import re
    try:
        url = "https://airtable.com/embed/appo9WyVgYvpap792/shrbAhhxKqvWjaJMs"
        resp = requests.get(url, timeout=15)
        html = resp.text
        
        # Extract API path
        match = re.search(r'urlWithParams\s*:\s*"(.*)"', html)
        if not match: return []
        api_path = match.group(1).replace("\\u002F", "/").replace("\\/", "/")
        api_url = "https://airtable.com" + api_path
        
        # Extract App ID
        app_match = re.search(r'"x-airtable-application-id"\s*:\s*"([^"]*)"', html)
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if app_match: headers["x-airtable-application-id"] = app_match.group(1)
        
        data_resp = requests.get(api_url, headers=headers, timeout=20)
        data = data_resp.json()
        
        rows = data.get("data", {}).get("table", {}).get("rows", [])
        cols = {c['name'].lower().strip(): c['id'] for c in data.get("data", {}).get("table", {}).get("columns", [])}
        
        q = query.lower()
        jobs = []
        for row in rows:
            cells = row.get("cellValuesByColumnId", {})
            # Basic mapping based on common Airtable structures
            title = ""
            if "position title" in cols and cols["position title"] in cells:
                title = str(cells[cols["position title"]])
            
            if q in title.lower():
                jobs.append({
                    "title": title,
                    "company": "ClimateTechList", # Placeholder
                    "url": "https://climatetechlist.com", # Placeholder
                    "location": "Various",
                    "description": "",
                    "salary": "Not listed",
                    "posted_date": "",
                    "source": "climatetechlist"
                })
        return jobs
    except Exception as e:
        print(f"ClimateTechList error: {e}")
        return []

def search_jobs(query: str, location: str = "") -> List[Dict]:
    """Unified entry point for all scrapers."""
    print(f"🔍 Searching all sources for: {query}...")
    results = []
    results.extend(search_arbeitnow(query, location))
    results.extend(search_remoteok(query))
    results.extend(search_greenhouse(query))
    results.extend(search_climatetechlist(query))
    
    # Apply Scoring Engine
    for job in results:
        scores = ScoringEngine.calculate_score(job)
        job.update(scores)
        
    return results

def save_jobs_to_csv(jobs: List[Dict], filename="jobs.csv"):
    """Saves jobs to CSV, deduplicating by URL."""
    if not jobs: return
    
    file_exists = os.path.isfile(filename)
    fieldnames = ["title", "company", "url", "location", "salary", "posted_date", "source", "match_score", "is_product_role", "is_climate_related"]
    
    existing_urls = set()
    if file_exists:
        with open(filename, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_urls.add(row['url'])
                
    with open(filename, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        if not file_exists:
            writer.writeheader()
        
        for job in jobs:
            if job['url'] not in existing_urls:
                writer.writerow(job)

# --- MAIN CLI ---

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Climate Tech Job Finder")
    parser.add_argument("command", choices=["search", "list"])
    parser.add_argument("--query", default="Product Manager")
    parser.add_argument("--location", default="")
    
    args = parser.parse_args()
    
    if args.command == "search":
        jobs = search_jobs(args.query, args.location)
        save_jobs_to_csv(jobs)
        print(f"✅ Found and saved {len(jobs)} jobs.")
    elif args.command == "list":
        if os.path.exists("jobs.csv"):
            with open("jobs.csv", 'r') as f:
                print(f.read())
        else:
            print("No jobs found. Run 'search' first.")

def save_csv(jobs_data: str) -> dict:
    """Append CSV-formatted job rows to jobs.csv, deduping by URL and handling new columns dynamically.

    Args:
        jobs_data: CSV-formatted string starting with a header row (e.g. title,company,location,salary,posted_date,wfh_policy,climate_score,source,url).
    """
    from io import StringIO

    filepath = "jobs.csv"
    try:
        new_jobs = list(csv.DictReader(StringIO(jobs_data)))
        if not new_jobs:
            return {"status": "success", "message": "No jobs to save"}

        existing_jobs = []
        try:
            with open(filepath, "r", newline="", encoding="utf-8") as f:
                existing_jobs = list(csv.DictReader(f))
        except FileNotFoundError:
            pass

        def norm_url(u): return (u or "").split('?')[0].strip()

        existing_urls = {norm_url(j.get("url", "")) for j in existing_jobs}
        unique_new, skipped = [], 0
        for job in new_jobs:
            u = norm_url(job.get("url", ""))
            if u and u not in existing_urls:
                unique_new.append(job)
                existing_urls.add(u)
            else:
                skipped += 1

        if not unique_new:
            return {"status": "success", "added": 0, "skipped": skipped}

        all_keys = set()
        for j in existing_jobs + unique_new:
            all_keys.update(j.keys())
        preferred = ["title", "company", "location", "salary", "posted_date", "wfh_policy", "climate_score", "source", "url"]
        fieldnames = [k for k in preferred if k in all_keys] + [k for k in all_keys if k not in preferred]

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(existing_jobs + unique_new)

        return {"status": "success", "added": len(unique_new), "skipped": skipped, "total_in_file": len(existing_jobs) + len(unique_new)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


root_agent = OpenAIAgent(
    model="gpt-4o-2024-08-06",
    system_prompt="""You are a job search automation agent that finds climate/green tech jobs.

WORKFLOW:
1. Call search_jobs with job title and (optional) location. Returns a list of validated jobs.
2. Convert the results into CSV and call save_csv. Header row:
   title,company,location,salary,posted_date,wfh_policy,climate_score,source,url
3. Reply with a brief summary, grouping job links by source.

RULES:
- Use structured data straight from search_jobs.
- Jobs with climate_score >= 3 are highly relevant.
""",
    tools=[search_jobs, save_csv],
)


if __name__ == "__main__":
    main()
