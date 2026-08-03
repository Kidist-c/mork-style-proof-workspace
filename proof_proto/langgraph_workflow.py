from __future__ import annotations

import json
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

    def explore(self, theorem: str, context: dict) -> dict:
        prompt = (
            f"You are an explorer agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Current context: {json.dumps(context, indent=2)[:3000]}\n"
            f'Return ONLY a JSON object with lowercase keys: "move_summary", "claim_statement", "status"'
        )
        result = self._parse_json(self._call_model(prompt))
        if "move_summary" not in result:
            return {
                "move_summary": "Try a case split on parity",
                "claim_statement": "The expression can be analyzed by parity",
                "status": "conjectural",
            }
        if "status" in result:
            result["status"] = str(result["status"]).lower()
        return result

    def critique(self, theorem: str, move_summary: str, claim_statement: str, context: dict) -> dict:
        prompt = (
            f"You are a critic agent for a mathematical proof project.\n"
            f"Theorem: {theorem}\n"
            f"Move summary: {move_summary}\n"
            f"Claim statement: {claim_statement}\n"
            f"Context: {json.dumps(context, indent=2)[:3000]}\n"
            f'Return ONLY a JSON object with lowercase keys: "decision", "reason", "status"'
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
        return result


class WorkflowState(TypedDict, total=False):
    theorem: str
    root: str
    project: ProofProject
    iteration: int
    max_iterations: int
    last_move: str
    last_critique: str


def build_graph(llm_client: Optional[Any] = None):
    llm = llm_client or SimpleLLMClient()

    def init_state(state: WorkflowState) -> WorkflowState:
        if "project" not in state or state["project"] is None:
            project = ProofProject(state["root"], state["theorem"])
            if "root" not in project.states:
                project.add_state("root", "Initial theorem state")
            project.add_claim("claim-1", "A parity-based approach is promising")
            state["project"] = project
        state["iteration"] = state.get("iteration", 0)
        state["max_iterations"] = state.get("max_iterations", 2)
        return state

    def explore_node(state: WorkflowState) -> WorkflowState:
        project = state["project"]
        context = project.context_for("root")
        exploration = llm.explore(state["theorem"], context)
        attempt_id = f"attempt-{state['iteration'] + 1}"
        project.record_attempt(
            attempt_id,
            "root",
            exploration["move_summary"],
            worker="explorer",
            note=exploration.get("claim_statement", ""),
        )
        state["last_move"] = exploration["move_summary"]
        state["iteration"] = state.get("iteration", 0) + 1
        return state

    def critique_node(state: WorkflowState) -> WorkflowState:
        project = state["project"]
        context = project.context_for("root")
        exploration = {
            "move_summary": state["last_move"],
            "claim_statement": "A parity-based approach is promising",
        }
        critique = llm.critique(
            state["theorem"],
            exploration["move_summary"],
            exploration.get("claim_statement", ""),
            context,
        )
        attempt_id = f"attempt-{state['iteration']}"
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

    graph = build_graph(llm_client)
    project = ProofProject(root, theorem)
    if "root" not in project.states:
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
