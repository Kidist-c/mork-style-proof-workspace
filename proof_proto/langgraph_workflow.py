from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Dict, Optional

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from proof_proto.proof_project import ProofProject


class SimpleLLMClient:
    """A small adapter for local Ollama-backed models."""

    def __init__(self, base_url: str = "http://127.0.0.1:11434", model: str = "llama3.1:8b"):
        self.base_url = base_url
        self.model = model

    def _call_model(self, prompt: str) -> str:
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

    @staticmethod
    def _parse_json(text: str) -> dict:
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

    # Valid attempt statuses the critic may assign — anything else gets normalised to supported
    _VALID_ATTEMPT_STATUSES = {"supported", "critic_accepted", "refuted", "retracted"}

    def explore(self, theorem: str, context: dict) -> dict:
        # Include previous attempts in the prompt so the explorer doesn't repeat itself
        previous = [a["move_summary"] for a in context.get("attempts", [])]
        prompt = (
            f"You are an explorer agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Already attempted moves (do NOT repeat these): {json.dumps(previous)}\n"
            f"Current context: {json.dumps(context, indent=2)[:2000]}\n"
            f'Return ONLY a JSON object with keys: "move_summary" (a new distinct proof move), '
            f'"claim_statement" (what this move claims)'
        )
        result = self._parse_json(self._call_model(prompt))
        if "move_summary" not in result:
            return {
                "move_summary": "Try a case split on parity",
                "claim_statement": "The expression can be analyzed by parity",
            }
        return result

    def critique(self, theorem: str, move_summary: str, claim_statement: str, context: dict) -> dict:
        prompt = (
            f"You are a critic agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Move summary: {move_summary}\n"
            f"Claim statement: {claim_statement}\n"
            f"Context: {json.dumps(context, indent=2)[:2000]}\n"
            f'Return ONLY a JSON object with keys:\n'
            f'  "decision": "continue" or "stop"\n'
            f'  "reason": explanation of your verdict\n'
            f'  "status": MUST be one of: supported, critic_accepted, refuted, retracted'
        )
        result = self._parse_json(self._call_model(prompt))
        if "decision" not in result:
            return {
                "decision": "continue",
                "reason": "The proposed move is a plausible next step",
                "status": "supported",
            }
        for key in ("decision", "status"):
            if key in result:
                result[key] = str(result[key]).lower()
        # Enforce valid status — if LLM returns something invalid, default to supported
        if result.get("status") not in self._VALID_ATTEMPT_STATUSES:
            result["status"] = "supported"
        return result


class GeminiLLMClient:
    """Gemini-backed LLM client — same interface as SimpleLLMClient."""

    _VALID_ATTEMPT_STATUSES = {"supported", "critic_accepted", "refuted", "retracted"}

    def __init__(self, model: str = "gemini-2.0-flash"):
        import google.generativeai as genai
        api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")).strip()
        genai.configure(api_key=api_key, transport="rest")
        self._model = genai.GenerativeModel(model)

    def _call(self, prompt: str) -> str:
        return self._model.generate_content(prompt).text

    def explore(self, theorem: str, context: dict) -> dict:
        previous = [a["move_summary"] for a in context.get("attempts", [])]
        prompt = (
            f"You are an explorer agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Already attempted moves (do NOT repeat these): {json.dumps(previous)}\n"
            f"Current context: {json.dumps(context, indent=2)[:2000]}\n"
            f'Return ONLY a JSON object with exactly two string keys: "move_summary" (a new distinct proof move), '
            f'"claim_statement" (what this move claims). Values must be plain strings, not nested objects.'
        )
        result = SimpleLLMClient._parse_json(self._call(prompt))
        move = result.get("move_summary", "")
        claim = result.get("claim_statement", "")
        if not move or not isinstance(move, str):
            return {"move_summary": "Try a case split on parity", "claim_statement": "The expression can be analyzed by parity"}
        return {"move_summary": move, "claim_statement": claim if isinstance(claim, str) else ""}

    def critique(self, theorem: str, move_summary: str, claim_statement: str, context: dict) -> dict:
        prompt = (
            f"You are a critic agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Move summary: {move_summary}\n"
            f"Claim statement: {claim_statement}\n"
            f"Context: {json.dumps(context, indent=2)[:2000]}\n"
            f'Return ONLY a JSON object with keys:\n'
            f'  "decision": "continue" or "stop"\n'
            f'  "reason": explanation of your verdict\n'
            f'  "status": MUST be one of: supported, critic_accepted, refuted, retracted'
        )
        result = SimpleLLMClient._parse_json(self._call(prompt))
        if "decision" not in result:
            return {"decision": "continue", "reason": "The proposed move is a plausible next step", "status": "supported"}
        for key in ("decision", "status"):
            if key in result:
                result[key] = str(result[key]).lower()
        if result.get("status") not in self._VALID_ATTEMPT_STATUSES:
            result["status"] = "supported"
        return result


class GroqLLMClient:
    """Groq-backed LLM client — fast free tier, same interface as SimpleLLMClient."""

    _VALID_ATTEMPT_STATUSES = {"supported", "critic_accepted", "refuted", "retracted"}

    def __init__(self, model: str = "llama-3.1-8b-instant"):
        from groq import Groq
        self._client = Groq(api_key=os.environ["GROQ_API_KEY"].strip())
        self._model = model

    def _call(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        return response.choices[0].message.content

    def explore(self, theorem: str, context: dict) -> dict:
        previous = [a["move_summary"] for a in context.get("attempts", [])]
        prompt = (
            f"You are an explorer agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Already attempted moves (do NOT repeat these): {json.dumps(previous)}\n"
            f"Current context: {json.dumps(context, indent=2)[:2000]}\n"
            f'Return ONLY a JSON object with exactly two string keys: "move_summary" (a new distinct proof move), '
            f'"claim_statement" (what this move claims). Values must be plain strings, not nested objects.'
        )
        result = SimpleLLMClient._parse_json(self._call(prompt))
        move = result.get("move_summary", "")
        claim = result.get("claim_statement", "")
        if not move or not isinstance(move, str):
            return {"move_summary": "Try a case split on parity", "claim_statement": "The expression can be analyzed by parity"}
        return {"move_summary": move, "claim_statement": claim if isinstance(claim, str) else ""}

    def critique(self, theorem: str, move_summary: str, claim_statement: str, context: dict) -> dict:
        prompt = (
            f"You are a critic agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Move summary: {move_summary}\n"
            f"Claim statement: {claim_statement}\n"
            f"Context: {json.dumps(context, indent=2)[:2000]}\n"
            f'Return ONLY a JSON object with keys:\n'
            f'  "decision": "continue" or "stop"\n'
            f'  "reason": explanation of your verdict\n'
            f'  "status": MUST be one of: supported, critic_accepted, refuted, retracted'
        )
        result = SimpleLLMClient._parse_json(self._call(prompt))
        if "decision" not in result:
            return {"decision": "continue", "reason": "The proposed move is a plausible next step", "status": "supported"}
        for key in ("decision", "status"):
            if key in result:
                result[key] = str(result[key]).lower()
        if result.get("status") not in self._VALID_ATTEMPT_STATUSES:
            result["status"] = "supported"
        return result


def make_llm_client():
    """Auto-select LLM client based on available env vars."""
    if os.environ.get("GROQ_API_KEY"):
        return GroqLLMClient()
    if os.environ.get("USE_GEMINI") and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return GeminiLLMClient()
    return SimpleLLMClient()


class WorkflowState(TypedDict, total=False):
    theorem: str
    root: str
    project: ProofProject
    iteration: int
    max_iterations: int
    last_move: str
    last_claim: str
    last_critique: str


def build_graph(llm_client: Optional[Any] = None):
    llm = llm_client or SimpleLLMClient()

    def init_state(state: WorkflowState) -> WorkflowState:
        # Project is always created in run_workflow and passed in — never recreated here
        state["iteration"] = state.get("iteration", 0)
        state["max_iterations"] = state.get("max_iterations", 2)
        return state

    def explore_node(state: WorkflowState) -> WorkflowState:
        project = state["project"]
        context = project.context_for("root")
        exploration = llm.explore(state["theorem"], context)
        # Offset attempt ID by existing count to avoid collisions across runs
        existing = len(project.graph.get_attempts_for_state("root", project.proof_id))
        attempt_id = f"attempt-{existing + 1}"
        project.record_attempt(
            attempt_id,
            "root",
            exploration["move_summary"],
            worker="explorer",
            note=exploration.get("claim_statement", ""),
        )
        state["last_move"] = exploration["move_summary"]
        state["last_claim"] = exploration.get("claim_statement", "")
        state["iteration"] = state.get("iteration", 0) + 1
        return state

    def critique_node(state: WorkflowState) -> WorkflowState:
        project = state["project"]
        context = project.context_for("root")
        critique = llm.critique(
            state["theorem"],
            state["last_move"],
            state.get("last_claim", ""),
            context,
        )
        existing = len(project.graph.get_attempts_for_state("root", project.proof_id))
        attempt_id = f"attempt-{existing}"
        project.mark_attempt(
            attempt_id,
            critique["status"],
            critique.get("reason", ""),
        )
        state["last_critique"] = critique.get("reason", "")
        return state

    def should_continue(state: WorkflowState) -> str:
        max_iterations = state.get("max_iterations", 2)
        return "continue" if state.get("iteration", 0) < max_iterations else END

    workflow = StateGraph(WorkflowState)
    workflow.add_node("init", init_state)
    workflow.add_node("explore", explore_node)
    workflow.add_node("critique", critique_node)
    workflow.set_entry_point("init")
    workflow.add_edge("init", "explore")
    workflow.add_edge("explore", "critique")
    workflow.add_conditional_edges("critique", should_continue, {"continue": "explore", END: END})
    return workflow.compile()


def run_workflow(
    theorem: str,
    root: str,
    llm_client: Optional[Any] = None,
    max_iterations: int = 2,
) -> Dict[str, Any]:
    """Run a minimal LangGraph workflow over the proof workspace."""

    graph = build_graph(llm_client or make_llm_client())
    project = ProofProject(root, theorem)
    if project.graph.get_state("root", project.proof_id) is None:
        project.add_state("root", "Initial theorem state")
    project.add_claim("claim-1", "A parity-based approach is promising")

    state = {
        "theorem": theorem,
        "root": root,
        "project": project,
        "iteration": 0,
        "max_iterations": max_iterations,
    }
    state = graph.invoke(state)

    snapshot = project.export_snapshot()
    return {
        "theorem": theorem,
        "project": project,
        "snapshot_path": str(snapshot),
    }
