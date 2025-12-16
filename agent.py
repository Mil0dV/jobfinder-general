"""Climate/Green Tech Job Vacancy Finder Agent using OpenAI.

This agent specializes in finding job vacancies in the climate and green technology sector.
It uses DuckDuckGo to find real job postings.
"""

import os
import json
import inspect
from openai import OpenAI
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()

class OpenAIAgent:
    def __init__(self, model="gpt-4o", tools=None, system_prompt=""):
        # self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.client = OpenAI(api_key="REDACTED_KEY")
        # print(os.getenv("OPENAI_API_KEY"))
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
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tool_schemas if tool_schemas else None,
            tool_choice="auto" if tool_schemas else None
        )
        
        response_message = response.choices[0].message
        
        if response_message.tool_calls:
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
            
            second_response = self.client.chat.completions.create(
                model=self.model,
                messages=messages
            )
            return second_response.choices[0].message.content
            
        return response_message.content


def google_search(query: str, num_results: int = 5) -> list:
    """Perform a web search using DuckDuckGo (replacing Google Search).
    
    Args:
        query: The search query
        num_results: Number of results to return
        
    Returns:
        List of search results
    """
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=num_results))
    return results


def search_climate_jobs(
    query: str,
    location: str = "",
    job_type: str = "",
    experience_level: str = ""
) -> dict:
    """Search for climate and green tech job vacancies.
    
    Args:
        query: Job title or keywords to search for (will be combined with climate/green tech focus)
        location: Location/city for the job search (optional)
        job_type: Type of job (e.g., 'full-time', 'part-time', 'contract', 'remote')
        experience_level: Experience level (e.g., 'entry', 'mid', 'senior')
    
    Returns:
        Dictionary containing job search results
    """
    # Build search query focused on climate/green tech sector
    search_terms = [query, "climate tech", "green technology", "sustainability", "jobs"]
    
    if location:
        search_terms.append(location)
    if job_type:
        search_terms.append(job_type)
    if experience_level:
        search_terms.append(experience_level)
    
    # Add job board sites to search
    search_query = " ".join(search_terms) + " site:linkedin.com OR site:indeed.com OR site:climatebase.org OR site:terra.do"
    
    try:
        # Use DuckDuckGo search to find job postings
        search_results = google_search(search_query, num_results=10)
        
        return {
            "status": "success",
            "query": query,
            "location": location,
            "job_type": job_type,
            "experience_level": experience_level,
            "sector": "Climate & Green Technology",
            "search_query": search_query,
            "results": search_results
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to search for jobs."
        }


def get_job_details(job_title: str, company: str) -> dict:
    """Get detailed information about a specific job posting.
    
    Args:
        job_title: The title of the job
        company: The company name
    
    Returns:
        Dictionary containing detailed job information
    """
    # Mock implementation
    return {
        "status": "success",
        "job_title": job_title,
        "company": company,
        "full_description": f"Detailed description for {job_title} at {company}...",
        "requirements": [
            "3+ years of experience",
            "Bachelor's degree or equivalent",
            "Strong communication skills"
        ],
        "benefits": [
            "Health insurance",
            "401k matching",
            "Remote work options",
            "Professional development budget"
        ],
        "application_url": f"https://example.com/jobs/{company.lower().replace(' ', '-')}/{job_title.lower().replace(' ', '-')}"
    }


def filter_jobs_by_salary(min_salary: int, max_salary: int = None) -> dict:
    """Filter job listings by salary range.
    
    Args:
        min_salary: Minimum salary requirement
        max_salary: Maximum salary (optional)
    
    Returns:
        Dictionary containing filtered jobs
    """
    # Mock implementation
    return {
        "status": "success",
        "min_salary": min_salary,
        "max_salary": max_salary,
        "message": f"Filtering jobs with salary between {min_salary} and {max_salary if max_salary else 'unlimited'}"
    }


# Initialize the agent
root_agent = OpenAIAgent(
    model="gpt-5.1",
    system_prompt="""You are a specialized Climate and Green Tech Job Finder Agent.
    
    Your mission is to help users find job vacancies specifically in the climate, sustainability, and green technology sectors.
    
    You have access to tools to search for jobs and filter them.
    
    Follow these guidelines:
    1. ALWAYS prioritize jobs in climate tech, renewable energy, sustainability, and environmental sectors.
    2. If a user asks for a generic job (e.g., "software engineer"), assume they want it in a climate tech company.
    3. Use the  tool to find real vacancies.
    4. Provide detailed information about climate tech job postings
    5. Filter jobs by location, type, and experience level
    6. Offer advice specific to careers in the climate and sustainability sector
    
    Key sectors to focus on:
    - Renewable energy (solar, wind, hydro, geothermal)
    - Electric vehicles and sustainable transportation
    - Carbon capture and climate solutions
    - Sustainable agriculture and food systems
    - Green building and energy efficiency
    - Climate data and analytics
    - Environmental policy and advocacy
    - Circular economy and waste reduction
    
    Use the available tools to search for jobs.
    Be enthusiastic about climate solutions and help users find meaningful work in this critical sector.
    Always emphasize the impact and mission-driven nature of climate tech roles.
    """,
    tools=[search_climate_jobs, get_job_details, filter_jobs_by_salary, google_search],
)
