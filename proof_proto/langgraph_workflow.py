"""LangGraph workflow that drives an explore/critique loop over a proof workspace.

Agents:
  - explorer: proposes proof moves (search DAG: Move from an OR state, with AND subgoals)
  - critic:   verdicts each move and decides whether to keep exploring or stop

LLM clients differ only in *how* they call the model; prompting and response
normalisation live on the shared ``LLMClient`` base. The formal verifier is a
pluggable socket (heuristic now, Lean 4 later).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from proof_proto.proof_project import ProofProject

ROOT_STATE_ID = "root"
MAX_SUBGOALS_PER_MOVE = 5
DEFAULT_ITERATIONS = 2
OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# Single source of truth for attempt statuses and the strings sent to the model.
ATTEMPT_STATUSES = ("conjectural","supported", "critic_accepted", "lean_verified", "refuted", "retracted")
VALID_ATTEMPT_STATUSES = set(ATTEMPT_STATUSES)

FALLBACK_EXPLORATION: dict[str, Any] = {
    "move_summary": "Begin by reformulating the goal into a more tractable equivalent form.",
    "claim_statement": "A useful reformulation or reduction makes the target easier to analyze.",
    "required_subgoals": [
        "Identify a meaningful reformulation or reduction of the goal.",
        "Establish the transformed statement or reduced case.",
    ],
}

DEFAULT_CRITIQUE: dict[str, Any] = {
    "decision": "continue",
    "reason": "The proposed move is plausible and worth testing in the current proof state.",
    "status": "supported",
}


# ---------------------------------------------------------------------------
# Prompt building and response parsing
# ---------------------------------------------------------------------------

def _explore_prompt(theorem: str, context: dict) -> str:
    previous = [a["move_summary"] for a in context.get("attempts", [])]
    return (
        f"You are an explorer agent for a mathematical proof project.\n"
        f"Theorem: {theorem}\n"
        f"Already attempted moves (do NOT repeat these): {json.dumps(previous)}\n"
        f"Current context: {json.dumps(context, indent=2)[:2000]}\n"
        f"Return ONLY a JSON object with exactly three keys:\n"
        f'  "move_summary" — a new distinct proof move\n'
        f'  "claim_statement" — what this move claims\n'
        f'  "required_subgoals" — a JSON array of strings naming subgoals this move'
        f" must establish before it succeeds (empty array if it closes the state directly)"
    )


def _critique_prompt(theorem: str, move_summary: str, claim_statement: str, context: dict) -> str:
    statuses = ", ".join(ATTEMPT_STATUSES)
    return (
        f"You are a critic agent for a mathematical proof project.\n"
        f"Theorem: {theorem}\n"
        f"Move summary: {move_summary}\n"
        f"Claim statement: {claim_statement}\n"
        f"Context: {json.dumps(context, indent=2)[:2000]}\n"
        f"Return ONLY a JSON object with keys:\n"
        f'  "decision": "continue" or "stop"\n'
        f'  "reason": explanation of your verdict\n'
        f'  "status": MUST be one of: {statuses}'
    )


def _load_local_env() -> None:
    """Populate os.environ from a repo-local .env file if present.

    This lets the app read GROQ_API_KEY without requiring the user to export it
    in every terminal session. The project root is chosen as the directory above
    this file, which makes the setup convenient in normal repo usage.
    """
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        for raw_line in resolved.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _parse_json(text: str) -> dict:
    """Best-effort JSON parsing that tolerates code fences and prose."""
    if text:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
    stripped = text.strip()
    stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {}


def _normalize_exploration(result: dict) -> dict:
    """Coerce an LLM exploration response into a well-formed move proposal."""
    move = result.get("move_summary")
    if not isinstance(move, str) or not move:
        return dict(FALLBACK_EXPLORATION)
    claim = result.get("claim_statement", "")
    subgoals = result.get("required_subgoals", [])
    if not isinstance(subgoals, list):
        subgoals = []
    return {
        "move_summary": move,
        "claim_statement": claim if isinstance(claim, str) else "",
        "required_subgoals": [s for s in subgoals if isinstance(s, str)],
    }


def _normalize_critique(result: dict) -> dict:
    """Coerce an LLM critique response into a well-formed verdict."""
    if "decision" not in result:
        return dict(DEFAULT_CRITIQUE)
    for key in ("decision", "status"):
        if key in result:
            result[key] = str(result[key]).lower()
    if result.get("status") not in VALID_ATTEMPT_STATUSES:
        result["status"] = "supported"
    return result


# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------

class LLMClient(ABC):
    """Base interface for LLM-backed explorer/critic agents.

    Subclasses only implement ``_call(prompt) -> str``; prompting and
    response normalisation are shared here.
    """

    def explore(self, theorem: str, context: dict) -> dict:
        return _normalize_exploration(self._respond(_explore_prompt(theorem, context)))

    def critique(self, theorem: str, move_summary: str, claim_statement: str, context: dict) -> dict:
        return _normalize_critique(
            self._respond(_critique_prompt(theorem, move_summary, claim_statement, context))
        )

    def _respond(self, prompt: str) -> dict:
        return _parse_json(self._call(prompt))

    @abstractmethod
    def _call(self, prompt: str) -> str:
        raise NotImplementedError


class OllamaLLMClient(LLMClient):
    """Adapter for a local Ollama server."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL, model: Optional[str] = None):
        self.base_url = base_url
        self.model = model or "llama3.1:8b"

    def _call(self, prompt: str) -> str:
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False, "format": "json"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data.get("response", "")
        except Exception:
            return ""


# Backward-compatible alias for the previous name of the Ollama client.
SimpleLLMClient = OllamaLLMClient


class GeminiLLMClient(LLMClient):
    """Gemini-backed LLM client."""

    def __init__(self, model: Optional[str] = None):
        import google.generativeai as genai

        api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")).strip()
        genai.configure(api_key=api_key, transport="rest")
        self._model = genai.GenerativeModel(model or "gemini-2.0-flash")

    def _call(self, prompt: str) -> str:
        return self._model.generate_content(prompt).text


class GroqLLMClient(LLMClient):
    """Groq-backed LLM client — fast free tier."""

    def __init__(self, model: Optional[str] = None):
        _load_local_env()
        from groq import Groq

        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "GROQ_API_KEY is missing. Put it in .env at the repo root or export it in your shell."
            )
        self._client = Groq(api_key=key)
        self._model = model or "llama-3.1-8b-instant"

    def _call(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        return response.choices[0].message.content


def make_llm_client(
    provider: str = "auto",
    model: Optional[str] = None,
    ollama_url: str = OLLAMA_BASE_URL,
) -> LLMClient:
    """Select an LLM client by provider preference, falling back to env vars.

    "auto" prefers Groq when GROQ_API_KEY is set, Gemini when opted in via
    USE_GEMINI, otherwise the local Ollama server.
    """
    _load_local_env()
    provider = (provider or "auto").lower()
    if provider == "auto":
        if os.environ.get("GROQ_API_KEY"):
            return GroqLLMClient(model=model)
        if os.environ.get("USE_GEMINI") and (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        ):
            return GeminiLLMClient(model=model)
        return OllamaLLMClient(base_url=ollama_url, model=model)
    if provider == "groq":
        return GroqLLMClient(model=model)
    if provider == "ollama":
        return OllamaLLMClient(base_url=ollama_url, model=model)
    raise ValueError(f"Unknown LLM provider {provider!r} (choose from: auto, groq, ollama)")


# ---------------------------------------------------------------------------
# Formal verifier socket
# ---------------------------------------------------------------------------

class FormalVerifier:
    """Socket for a formal verifier — LLM/heuristic based now, Lean 4 later.

    To plug in Lean 4: subclass this, override verify(), return
    verdict='verified' only when `lake build` passes with no errors.
    """

    def verify(self, theorem: str, attempts: list) -> dict:
        """Given the full attempt chain, decide if the proof is complete.

        The default implementation is intentionally generic: it does not assume a
        specific theorem family or algebraic pattern. A proof is considered closed
        only when the attempt history includes an explicit accepted or verified
        status, leaving the actual certification to a stronger backend later.

        Returns:
            closed: bool — True if proof is complete
            reason: str  — explanation
        """
        accepted_statuses = {"critic_accepted", "lean_verified"}
        if any(a.get("status") in accepted_statuses for a in attempts):
            return {
                "closed": True,
                "reason": "The proof chain contains an explicit accepted or verified result.",
            }
        return {
            "closed": False,
            "reason": "The proof chain is not yet explicitly accepted or verified.",
        }


class Lean4Verifier(FormalVerifier):
    """Placeholder for future Lean 4 integration.

    When implemented: translate attempt chain to Lean 4 syntax,
    run `lake build`, parse output, return verified only on success.
    """

    def verify(self, theorem: str, attempts: list) -> dict:
        raise NotImplementedError("Lean 4 verifier not yet implemented — use FormalVerifier for now")


# ---------------------------------------------------------------------------
# LangGraph workflow
# ---------------------------------------------------------------------------

class WorkflowState(TypedDict, total=False):
    theorem: str
    project: ProofProject
    iteration: int
    max_iterations: int
    last_move: str
    last_claim: str
    last_move_id: str
    current_attempt_id: str
    last_critique: str
    proof_closed: bool


class ProofWorkflow:
    """The explore/critique loop as a compiled LangGraph.

    init -> explore -> critique, looping back to explore until the critic stops
    or the max iteration budget is exhausted.
    """

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        verifier: Optional[FormalVerifier] = None,
    ):
        self.llm = llm or OllamaLLMClient()
        self.verifier = verifier or FormalVerifier()

    def build(self):
        workflow = StateGraph(WorkflowState)
        workflow.add_node("init", self._init_state)
        workflow.add_node("explore", self._explore)
        workflow.add_node("critique", self._critique)
        workflow.set_entry_point("init")
        workflow.add_edge("init", "explore")
        workflow.add_edge("explore", "critique")
        workflow.add_conditional_edges(
            "critique", self._should_continue, {"continue": "explore", END: END}
        )
        return workflow.compile()

    # --- nodes ------------------------------------------------------------

    @staticmethod
    def _init_state(state: WorkflowState) -> WorkflowState:
        state["iteration"] = state.get("iteration", 0)
        state["max_iterations"] = state.get("max_iterations", DEFAULT_ITERATIONS)
        return state

    def _explore(self, state: WorkflowState) -> WorkflowState:
        project: ProofProject = state["project"]
        context = project.context_for(ROOT_STATE_ID)
        exploration = self.llm.explore(state["theorem"], context)

        # Search DAG: the explorer proposes a Move from the root state (OR point),
        # optionally requiring subgoal States (AND points).
        move = project.add_move(
            ROOT_STATE_ID,
            exploration["move_summary"],
            kind="reduction",
            note=exploration.get("claim_statement", ""),
        )
        for subgoal_desc in exploration.get("required_subgoals", [])[:MAX_SUBGOALS_PER_MOVE]:
            project.add_subgoal(move["id"], subgoal_desc, ROOT_STATE_ID)

        # Attempt id is derived from the current count and carried in state so
        # the critic verdict lands on the exact attempt just explored.
        attempt_id = self._next_attempt_id(project)
        project.record_attempt(
            attempt_id,
            ROOT_STATE_ID,
            exploration["move_summary"],
            worker="explorer",
            note=exploration.get("claim_statement", ""),
            move_id=move["id"],
        )

        state["last_move"] = exploration["move_summary"]
        state["last_claim"] = exploration.get("claim_statement", "")
        state["last_move_id"] = move["id"]
        state["current_attempt_id"] = attempt_id
        state["iteration"] = state.get("iteration", 0) + 1
        return state

    def _critique(self, state: WorkflowState) -> WorkflowState:
        project: ProofProject = state["project"]
        context = project.context_for(ROOT_STATE_ID)
        critique = self.llm.critique(
            state["theorem"],
            state["last_move"],
            state.get("last_claim", ""),
            context,
        )
        project.mark_attempt(
            state["current_attempt_id"],
            critique["status"],
            critique.get("reason", ""),
        )
        state["last_critique"] = critique.get("reason", "")

        # Run the formal verifier against the full attempt chain.
        all_attempts = project.graph.get_attempts_for_state(ROOT_STATE_ID, project.proof_id)
        verdict = self.verifier.verify(state["theorem"], all_attempts)
        if verdict["closed"] or critique.get("decision") == "stop":
            project.close_state(ROOT_STATE_ID, verdict["reason"] or critique.get("reason", ""))
            state["proof_closed"] = True
        return state

    # --- routing ----------------------------------------------------------

    @staticmethod
    def _next_attempt_id(project: ProofProject) -> str:
        existing = len(project.graph.get_attempts_for_state(ROOT_STATE_ID, project.proof_id))
        return f"attempt-{existing + 1}"

    @staticmethod
    def _should_continue(state: WorkflowState) -> str:
        if state.get("proof_closed"):
            return END
        max_iterations = state.get("max_iterations", DEFAULT_ITERATIONS)
        return "continue" if state.get("iteration", 0) < max_iterations else END


def build_graph(
    llm_client: Optional[LLMClient] = None,
    verifier: Optional[FormalVerifier] = None,
):
    return ProofWorkflow(llm_client, verifier).build()


def run_workflow(
    theorem: str,
    root: str,
    llm_client: Optional[LLMClient] = None,
    max_iterations: int = DEFAULT_ITERATIONS,
) -> Dict[str, Any]:
    """Run the LangGraph explore/critique workflow over the proof workspace."""
    graph = build_graph(llm_client or make_llm_client())
    project = ProofProject(root, theorem)
    if project.graph.get_state(ROOT_STATE_ID, project.proof_id) is None:
        project.add_state(ROOT_STATE_ID, "Initial theorem state")
    project.add_claim("claim-1", "Initial proof strategy is under exploration")
    state: WorkflowState = {
        "theorem": theorem,
        "project": project,
        "iteration": 0,
        "max_iterations": max_iterations,
    }
    graph.invoke(state)

    snapshot = project.export_snapshot()
    return {
        "theorem": theorem,
        "project": project,
        "snapshot_path": str(snapshot),
    }
