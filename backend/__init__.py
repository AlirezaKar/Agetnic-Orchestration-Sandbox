"""
backend — the orchestration core of the Agetnic Orchestration Sandbox.

Package layout
--------------
config      Organised .env-driven settings (single source of truth)
schemas     Shared Pydantic models (plans, results, traces, HTTP contract)
llm         LLM client abstraction: Groq (real) + deterministic Mock
agents      Worker agents: document, web_search, general, number
orchestration
            Manager (root brain) · Planner · parallel Executor · Router · Traces
rag         Local document store + lexical retrieval for the Document agent
search      Pluggable web-search providers (simulated / tavily / serper)
api         FastAPI test-harness server + REST endpoints
"""

__version__ = "1.0.0"
