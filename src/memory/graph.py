"""L2 双向属性图（立体图）— index-free adjacency (PRD module 12/13, BRIEF 五-1).

Design principles enforced (module 13):
- property graph 4 elements: start node / end node / relationship type / relationship props
- relationship type MUST be UPPER_SNAKE_CASE -> else rejected (GraphError)
- universal edges RELATED_TO / LINKED_TO rejected (拒绝万能边)
- dynamic context (dates/confidence/values) lives on EDGE PROPS, not nodes
- event nodes (Visit) reduce many-to-many to 1:N
- dates are properties, never Date nodes
- index-free adjacency: nodes hold direct pointers to adjacent edges ->
  multi-hop traversal cost depends only on hop count, not graph size
"""
import json
import re
from core.errors import GraphError

TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
UNIVERSAL_EDGES = {"RELATED_TO", "LINKED_TO"}


class GNode:
    __slots__ = ("id", "label", "props", "out_edges", "in_edges", "trees")

    def __init__(self, id: str, label: str, props: dict | None = None):
        self.id = id
        self.label = label
        self.props = props or {}
        # index-free adjacency: direct pointers (type -> list[Edge])
        self.out_edges: dict[str, list] = {}
        self.in_edges: dict[str, list] = {}
        # 立体图: each entity node is the root of a B+ tree per dimension
        self.trees: dict = {}  # dim -> BPlusTree

    def to_dict(self):
        return {"id": self.id, "label": self.label, "props": self.props}


class GEdge:
    __slots__ = ("src", "dst", "type", "props")

    def __init__(self, src: str, dst: str, type_: str, props: dict | None = None):
        self.src = src
        self.dst = dst
        self.type = type_
        self.props = props or {}

    def to_dict(self):
        return {"src": self.src, "dst": self.dst, "type": self.type, "props": self.props}


def validate_edge_type(type_: str):
    if type_ in UNIVERSAL_EDGES:
        raise GraphError(f"拒绝万能边 {type_}：关系类型必须有明确语义（需求②原则1）")
    if not TYPE_RE.match(type_):
        raise GraphError(f"关系类型 {type_!r} 非 UPPER_SNAKE_CASE，拒绝写入")


class PropertyGraph:
    def __init__(self):
        self.nodes: dict[str, GNode] = {}
        self.edges: dict[tuple, GEdge] = {}  # (src,dst,type) -> edge, MERGE idempotency

    # ---------- node ops ----------
    def get_or_create_node(self, id: str, label: str, props: dict | None = None) -> GNode:
        if id in self.nodes:
            n = self.nodes[id]
            n.props.update(props or {})
            return n
        n = GNode(id, label, props or {})
        self.nodes[id] = n
        return n

    def node(self, id: str) -> GNode | None:
        return self.nodes.get(id)

    def labels(self):
        return {n.label for n in self.nodes.values()}

    def node_count(self):
        return len(self.nodes)

    def edge_count(self):
        return len(self.edges)

    # ---------- edge ops (MERGE semantics) ----------
    def merge_edge(self, src: str, dst: str, type_: str, props: dict | None = None) -> GEdge:
        """MERGE: existing (src,dst,type) -> update props; else create. Idempotent."""
        validate_edge_type(type_)
        key = (src, dst, type_)
        if key in self.edges:
            e = self.edges[key]
            e.props.update(props or {})
            return e
        s, d = self.nodes.get(src), self.nodes.get(dst)
        if s is None or d is None:
            raise GraphError(f"边端点不存在: {src} -> {dst}")
        e = GEdge(src, dst, type_, props or {})
        self.edges[key] = e
        s.out_edges.setdefault(type_, []).append(e)
        d.in_edges.setdefault(type_, []).append(e)
        return e

    def edge(self, src: str, dst: str, type_: str) -> GEdge | None:
        return self.edges.get((src, dst, type_))

    def edges_from(self, node_id: str, type_: str | None = None) -> list[GEdge]:
        n = self.nodes.get(node_id)
        if n is None:
            return []
        if type_ is not None:
            return list(n.out_edges.get(type_, []))
        out = []
        for lst in n.out_edges.values():
            out.extend(lst)
        return out

    def edges_to(self, node_id: str, type_: str | None = None) -> list[GEdge]:
        n = self.nodes.get(node_id)
        if n is None:
            return []
        if type_ is not None:
            return list(n.in_edges.get(type_, []))
        out = []
        for lst in n.in_edges.values():
            out.extend(lst)
        return out

    # ---------- traversal (index-free adjacency, cost ~ hops) ----------
    def neighbors(self, node_id: str) -> dict:
        n = self.nodes.get(node_id)
        if n is None:
            return {"out": {}, "in": {}}
        return {
            "out": {t: [e.to_dict() for e in lst] for t, lst in n.out_edges.items()},
            "in": {t: [e.to_dict() for e in lst] for t, lst in n.in_edges.items()},
        }

    def bfs(self, start_id: str, hops: int = 1, types: list[str] | None = None) -> list[GEdge]:
        """Multi-hop follow-the-pointer walk. Only hops matter (AC-45)."""
        seen = {start_id}
        frontier = [start_id]
        out: list[GEdge] = []
        for _ in range(max(1, hops)):
            nxt = []
            for nid in frontier:
                for e in self.edges_from(nid, None):
                    if types is not None and e.type not in types:
                        continue
                    out.append(e)
                    if e.dst not in seen:
                        seen.add(e.dst)
                        nxt.append(e.dst)
            frontier = nxt
            if not frontier:
                break
        return out

    def path(self, start_id: str, hops: int = 2, types: list[str] | None = None) -> list[str]:
        """Node id path following first-match adjacency (deterministic)."""
        path = [start_id]
        cur = start_id
        for _ in range(max(1, hops)):
            cands = [e for e in self.edges_from(cur) if types is None or e.type in types]
            if not cands:
                break
            e = cands[0]
            path.append(e.dst)
            cur = e.dst
        return path

    # ---------- serialization (persistence / subgraph summary) ----------
    def export(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
        }

    def load(self, data: dict):
        for nd in data.get("nodes", []):
            self.nodes[nd["id"]] = GNode(nd["id"], nd["label"], nd.get("props", {}))
        for ed in data.get("edges", []):
            try:
                self.merge_edge(ed["src"], ed["dst"], ed["type"], ed.get("props", {}))
            except GraphError:
                continue  # stale/invalid legacy edge — skip, do not crash

    def subgraph(self, center_id: str, hops: int = 2) -> dict:
        """Undirected BFS subgraph (双向属性图：出边+入边都算邻接).

        Returns {"nodes": [GNode], "edges": [GEdge]} within `hops` hops of
        center. Unknown center -> empty (no error, no fabricated node).
        """
        if center_id not in self.nodes:
            return {"nodes": [], "edges": []}
        seen = {center_id}
        frontier = [center_id]
        out_edges: list[GEdge] = []
        for _ in range(max(1, hops)):
            nxt = []
            for nid in frontier:
                for e in self.edges_from(nid, None) + self.edges_to(nid, None):
                    if e not in out_edges:
                        out_edges.append(e)
                    other = e.dst if e.src == nid else e.src
                    if other not in seen:
                        seen.add(other)
                        nxt.append(other)
            frontier = nxt
            if not frontier:
                break
        return {"nodes": [self.nodes[nid] for nid in seen], "edges": out_edges}

    def subgraph_summary(self, center_id: str, hops: int = 2, max_edges: int = 40) -> str:
        """Structured subgraph summary for final QA without re-reading source (策略2).

        Uses undirected adjacency (出边+入边) so a center node's full 2-hop
        neighborhood is captured, not just its out-edges.
        """
        edges = self.subgraph(center_id, hops=hops)["edges"]
        lines = [f"[子图摘要 中心={center_id} hops={hops} 边数={len(edges)}]"]
        for e in edges[:max_edges]:
            p = json.dumps(e.props, ensure_ascii=False)
            lines.append(f"({e.src})-[:{e.type} {p}]->({e.dst})")
        return "\n".join(lines)
