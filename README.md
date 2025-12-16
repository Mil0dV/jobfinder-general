# Climate & Green Tech Job Finder Agent

A specialized job search agent using Google Agent Development Kit (ADK) to find opportunities in the climate and green technology sector.

## Overview

This project demonstrates how to create an AI agent using Google ADK that specializes in finding job vacancies in the **climate and green technology sector**. The agent uses Google search to find real job postings from major job boards and climate-focused platforms.

## Features

- 🌱 **Climate Tech Focused**: Specialized in sustainability, renewable energy, and green technology jobs
- 🔍 **Real Job Search**: Uses Google search to find actual job postings
- 📍 Filter by location, job type, and experience level
- 💼 Searches major job boards (LinkedIn, Indeed) and climate-specific platforms (Climatebase, Terra.do)
- 🎯 **CLI Interface**: Easy command-line interface with job title as argument
- 💰 Filter by salary range
- 📝 Get detailed job information

## Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) - Fast Python package installer
- Google API key (get one from [Google AI Studio](https://aistudio.google.com/apikey))

## Installation

1. **Install uv (if not already installed):**
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Clone or navigate to this directory:**
   ```bash
   cd job_vacancy_agent
   ```

3. **Install dependencies with uv:**
   ```bash
   uv sync
   ```
   
   This will automatically create a virtual environment and install all dependencies.

4. **Set up environment variables:**
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env` and add your Google API key:
   ```
   GOOGLE_API_KEY=your_actual_api_key_here
   ```

## Usage

### Command-Line Interface (Recommended)

The easiest way to search for jobs is using the CLI:

```bash
# Basic search
uv run python cli.py "Software Engineer"

# With location
uv run python cli.py "Data Scientist" --location "San Francisco"

# Remote jobs
uv run python cli.py "Renewable Energy Engineer" --job-type remote

# Senior-level positions
uv run python cli.py "Climate Policy Analyst" --experience senior --location "Washington DC"

# Interactive mode
uv run python cli.py "Sustainability Manager" --interactive
```

**CLI Options:**
- `job_title` (required): The job title or role to search for
- `-l, --location`: Location or city for the job search
- `-t, --job-type`: Type of employment (full-time, part-time, contract, remote)
- `-e, --experience`: Experience level (entry, mid, senior)
- `--interactive`: Start interactive mode after initial search

### Run with ADK Web Interface

```bash
uv run adk run agent.py --web
```

This will start a web server (usually at `http://localhost:8000`) where you can interact with the agent through a browser.

### Run Programmatically

You can also use the agent in your own Python scripts:

```bash
uv run python example.py
```

Or in your own code:

```python
from agent import root_agent

# Send a message to the agent
response = root_agent.run("Find me climate data scientist jobs in Europe")
print(response)
```

## Project Structure

```
job_vacancy_agent/
├── agent.py           # Main agent code with climate tech focus
├── cli.py             # Command-line interface
├── example.py         # Example usage script
├── __init__.py        # Package initialization
├── pyproject.toml     # Project dependencies (uv)
├── .env.example       # Environment variables template
├── .env              # Your actual environment variables (not in git)
├── setup.sh          # Quick setup script
└── README.md         # This file
```

## How It Works

The agent specializes in climate and green technology jobs using:

1. **search_climate_jobs**: Searches Google for climate tech jobs on major job boards and specialized platforms
2. **get_job_details**: Retrieves detailed information about specific job postings
3. **filter_jobs_by_salary**: Filters jobs by salary range
4. **google_search**: Direct Google search capability for flexible queries

The agent automatically focuses searches on:
- Climate-specific job boards (Climatebase, Terra.do)
- General job boards (LinkedIn, Indeed) with climate tech filters
- Key sectors: renewable energy, EVs, carbon capture, sustainable agriculture, etc.

## Customization

### Adding Real Job APIs

To integrate real job search APIs, modify the tool functions in `agent.py`:

```python
def search_jobs(query: str, location: str = "", ...):
    # Replace mock data with actual API calls
    import requests
    
    response = requests.get(
        "https://api.example-job-board.com/search",
        params={"q": query, "location": location}
    )
    return response.json()
```

### Changing the Model

You can use different Gemini models by changing the `model` parameter in `agent.py`:

```python
root_agent = Agent(
    model='gemini-2.0-flash-exp',  # or 'gemini-1.5-pro', etc.
    ...
)
```

### Adding More Tools

Add new tools by defining functions and adding them to the `tools` list:

```python
def save_job(job_id: str) -> dict:
    """Save a job to favorites."""
    return {"status": "saved", "job_id": job_id}

root_agent = Agent(
    ...
    tools=[search_jobs, get_job_details, filter_jobs_by_salary, save_job],
)
```

## Example Interactions

**CLI:**
```bash
$ uv run python cli.py "Solar Engineer" --location "California" --job-type remote
```

**Interactive:**

**User:** "I'm looking for remote software engineer jobs in climate tech"

**Agent:** *Uses search_climate_jobs and google_search to find real job postings*

**User:** "Tell me more about the position at Tesla"

**Agent:** *Uses get_job_details to provide comprehensive information*

**User:** "Show me only jobs paying over $120k"

**Agent:** *Uses filter_jobs_by_salary tool*

## Climate Tech Job Sectors

The agent specializes in these climate and sustainability sectors:

- ☀️ **Renewable Energy**: Solar, wind, hydro, geothermal
- 🚗 **Sustainable Transportation**: EVs, batteries, charging infrastructure
- 🌍 **Carbon Solutions**: Carbon capture, climate tech, emissions reduction
- 🌾 **Sustainable Agriculture**: AgTech, food systems, regenerative farming
- 🏢 **Green Building**: Energy efficiency, sustainable construction
- 📊 **Climate Data**: Analytics, modeling, climate science
- 🏛️ **Policy & Advocacy**: Environmental policy, climate justice
- ♻️ **Circular Economy**: Waste reduction, recycling, sustainable materials

## Next Steps

- Explore different climate tech roles using the CLI
- Add more specialized job boards to the search
- Integrate with job application tracking
- Set up job alerts for new postings

## Resources

- [Google ADK Documentation](https://google.github.io/adk-docs/)
- [Google ADK Python Repository](https://github.com/google/adk-python)
- [Google ADK Samples](https://github.com/google/adk-samples)
- [Gemini API Documentation](https://ai.google.dev/docs)

## License

This is a demonstration project. Modify and use as needed for your purposes.
