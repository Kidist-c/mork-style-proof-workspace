from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import matplotlib.pyplot as plt
import networkx as nx
from neo4j import GraphDatabase

LABEL_COLORS = {
    "Proof": "#222222",
    "State": "#4C72B0",
    "Move": "#DD8452",
    "Claim": "#55A868",
    "Attempt": "#C44E52",
    "Route": "#8172B3",
    "Artifact": "#937860",
    "Context": "#64B5CD",
    "Hypothesis": "#CCB974",
    "Concept": "#AA8FD8",
    "Critique": "#9B59B6",
    "Experiment": "#2E86C1",
    "Verification": "#148F77",
}

DAG_GROUPS = {
    "search": {"State", "Move"},
    "justification": {"Claim"},
    "provenance": {"Attempt", "Route", "Artifact", "Context", "Critique", "Experiment", "Verification"},
    "speculative": {"Hypothesis", "Concept"},
}


def load_graph(proof_id: str, uri: str = "bolt://localhost:7687", user: str = "neo4j", password: str = "proofagent123"):
    g = nx.DiGraph()
    with GraphDatabase.driver(uri, auth=(user, password)) as driver, driver.session() as s:
        nodes = s.run(
            "MATCH (n) WHERE n.proof_id = $pid "
            "RETURN n.id AS id, labels(n) AS labels, n.status AS status",
            pid=proof_id,
        ).data()
        for rec in nodes:
            g.add_node(
                rec["id"],
                label=(rec["labels"][0] if rec["labels"] else "?"),
                proof_id=proof_id,
                status=rec.get("status", ""),
            )
        rels = s.run(
            "MATCH (a)-[r]->(b) "
            "WHERE a.proof_id = $pid AND b.proof_id = $pid "
            "RETURN a.id AS src, type(r) AS rel, b.id AS dst",
            pid=proof_id,
        ).data()
        for rec in rels:
            g.add_edge(rec["src"], rec["dst"], rel=rec["rel"])
    return g


def dag_group(label: str) -> str:
    for group, labels in DAG_GROUPS.items():
        if label in labels:
            return group
    return "other"


def draw(g: nx.DiGraph, proof_id: str) -> str:
    groups: Dict[str, List[str]] = defaultdict(list)
    for node, data in g.nodes(data=True):
        groups[dag_group(data["label"])].append(node)

    pos = {}
    layers = ["search", "justification", "provenance", "speculative", "other"]
    x_by_group = {grp: i * 8 for i, grp in enumerate(layers)}
    for grp, nodes in groups.items():
        if not nodes:
            continue
        sub = g.subgraph(nodes)
        sub_pos = nx.spring_layout(sub, seed=7, scale=3)
        for node, p in sub_pos.items():
            pos[node] = (x_by_group[grp] + p[0], p[1])

    fig, ax = plt.subplots(figsize=(16, 10))
    plt.title(f"Proof metagraph: {proof_id} (grouped: search | justification | provenance | speculative)")

    nx.draw_networkx_nodes(
        g, pos, ax=ax, node_size=900,
        node_color=[LABEL_COLORS.get(d["label"], "#999999") for _, d in g.nodes(data=True)],
    )
    nx.draw_networkx_labels(
        g, pos, ax=ax, font_size=7,
        labels={n: f"{d['label'][0]}·{n}" for n, d in g.nodes(data=True)},
    )
    edge_labels = {(u, v): d["rel"] for u, v, d in g.edges(data=True)}
    nx.draw_networkx_edges(g, pos, ax=ax, arrows=True, arrowsize=12, edge_color="#999999", alpha=0.5, connectionstyle="arc3,rad=0.1")
    nx.draw_networkx_edge_labels(g, pos, ax=ax, edge_labels=edge_labels, font_size=5, rotate=False)

    ax.axis("off")
    out_dir = Path(__file__).resolve().parent.parent / "visualizations"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"visual_{proof_id}.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return str(out)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m proof_proto.visualize <proof_id> [proof_id ...]")
        sys.exit(1)
    for pid in sys.argv[1:]:
        g = load_graph(pid)
        if g.number_of_nodes() == 0:
            print(f"{pid}: no nodes with proof_id='{pid}' — pick one from: "
                  + ", ".join(sorted(available_proofs())))
            continue
        print(f"{pid}: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges")
        print("    " + str(sorted(nx.get_node_attributes(g, "label").values())))
        path = draw(g, pid)
        print(f"    saved -> {path}")


def available_proofs() -> List[str]:
    from neo4j import GraphDatabase
    with GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "proofagent123")) as driver, driver.session() as s:
        return [r["id"] for r in s.run("MATCH (p:Proof) RETURN p.id AS id ORDER BY id")]


if __name__ == "__main__":
    main()
