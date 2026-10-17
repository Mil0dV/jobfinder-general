"""Manual tool-use agent loop against the Anthropic Messages API."""

import inspect
import json

from jobfinder.agent.tools import get_company_info
from jobfinder.config.profile import CANDIDATE_PROFILE
from jobfinder.pipeline.runs import run_profile_search
from jobfinder.pipeline.search import search_jobs
from jobfinder.pipeline.store import save_csv
from jobfinder.pipeline.validate import _get_anthropic_client


class ClaudeAgent:
    """Manual tool-use loop against the Anthropic Messages API."""

    def __init__(self, model="claude-sonnet-5", tools=None, system_prompt=""):
        import jobfinder.llm.client as llm

        if not llm.api_key():
            print(f"⚠️ WARNING: {llm.api_key_env()} not found in environment.")
        self.model = model
        self.tools = tools or []
        self.system_prompt = system_prompt
        self.tool_map = {tool.__name__: tool for tool in self.tools}

    def _get_tool_schemas(self):
        schemas = []
        for tool in self.tools:
            signature = inspect.signature(tool)
            input_schema = {"type": "object", "properties": {}, "required": []}
            for name, param in signature.parameters.items():
                if name.startswith("_"):  # internal-only params (e.g. progress callbacks)
                    continue
                p_type = "string"
                if param.annotation is int:
                    p_type = "integer"
                elif param.annotation is bool:
                    p_type = "boolean"
                input_schema["properties"][name] = {
                    "type": p_type,
                    "description": f"Parameter {name}",
                }
                if param.default == inspect.Parameter.empty:
                    input_schema["required"].append(name)
            schemas.append(
                {
                    "name": tool.__name__,
                    "description": tool.__doc__ or "",
                    "input_schema": input_schema,
                }
            )
        return schemas

    def run(self, prompt: str):
        client = _get_anthropic_client()
        messages = [{"role": "user", "content": prompt}]
        tool_schemas = self._get_tool_schemas()

        for _ in range(10):
            response = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=self.system_prompt,
                thinking={"type": "enabled", "budget_tokens": 2048},  # haiku: no adaptive mode
                tools=tool_schemas,
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                return next((b.text for b in response.content if b.type == "text"), "")

            # Echo the assistant turn (thinking + tool_use blocks) back unchanged,
            # then answer every tool call in a single user message.
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    result = self.tool_map[block.name](**block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str)[:60000],
                        }
                    )
                except Exception as e:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {e}",
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
        return "Max iterations reached."


root_agent = ClaudeAgent(
    model="claude-sonnet-5",
    system_prompt=f"""You are a job search agent finding climate/green-tech jobs for this candidate:

{CANDIDATE_PROFILE}

WORKFLOW:
1. Call search_jobs with the job title (or "profile" to search all the candidate's target
   roles) and optional location. It queries 9 sources in parallel, scores each job for
   role fit (0-5), startup-ness (0-5), and climate relevance (0-5), and returns only
   good matches.
2. Convert results to CSV and call save_csv. Header row:
   title,company,location,salary,posted_date,wfh_policy,fit_score,startup_score,climate_score,reason,source,url
3. Reply with a brief summary: top matches first (highest fit+climate), grouped by source,
   with links.

RULES:
- Pass structured data straight through; do not invent or drop fields.
- Optionally call get_company_info for promising companies the candidate may not know.
""",
    tools=[search_jobs, get_company_info, save_csv],
)


if __name__ == "__main__":
    import sys

    run_profile_search(location=sys.argv[1] if len(sys.argv) > 1 else "")
