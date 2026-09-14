"""三层记忆插件 — 与 agent 解耦的适配器接口 (PRD module 12, AC-49/50).

接口抽象：agent 引擎/routers 只依赖 MemoryBackend 抽象 + get_memory_backend() 工厂，
绝不 import 具体后端实现（LocalBackend/RedisStubBackend/...）。
可插拔：MEMORY_BACKEND=local|redis|milvus|neo4j|hermes（默认 local，DECISION-002）。
redis/milvus/neo4j 为适配位存根（接口一致，部署期接入）；hermes 为真实对接
Hermes Agent 记忆策略的适配器（JSONL 事实行写入 HERMES_MEMORY_DIR）。
"""
import abc
import asyncio
import json
import os
from core import db
from core.config import S
from llm import embedding


class MemoryBackend(abc.ABC):
    """三层记忆统一接口。L0 原始 / L1 短期(语义缓存) / L2 长期(立体图+B+树)。"""

    name: str = "abstract"
    stub: bool = False  # True = 适配位存根（部署期接入）

    # ---- L0 raw (不降噪行为记录) ----
    @abc.abstractmethod
    async def l0_append(self, agent_id, input_text: str, output_text: str): ...
    @abc.abstractmethod
    async def l0_recent(self, limit: int = 20) -> list[dict]: ...

    # ---- L1 short-term (semantic cache) ----
    @abc.abstractmethod
    async def l1_write(self, agent_key: str, text: str, answer: str): ...
    @abc.abstractmethod
    async def l1_search(self, agent_key: str, text: str, threshold: float) -> dict | None:
        """返回 {"text","answer","similarity"} 或 None."""
    @abc.abstractmethod
    async def l1_image_lookup(self, md5: str) -> dict | None: ...
    @abc.abstractmethod
    async def l1_image_store(self, md5: str, content_type: str, parsed: dict): ...
    @abc.abstractmethod
    async def l1_list(self, agent_key: str | None = None, limit: int = 50) -> list[dict]: ...

    # ---- L2 long-term (立体图 + B+树) ----
    @abc.abstractmethod
    async def l2_node_or_create(self, id: str, label: str, props: dict | None = None): ...
    @abc.abstractmethod
    async def l2_merge_edge(self, src: str, dst: str, type_: str, props: dict | None = None): ...
    @abc.abstractmethod
    async def l2_query(self, q: dict) -> dict: ...
    @abc.abstractmethod
    async def l2_bucket_write(self, node_id: str, dim: str, key: str, record: dict): ...
    @abc.abstractmethod
    async def l2_bucket_read(self, node_id: str, dim: str, key: str | None = None) -> list[dict]: ...

    # ---- meta ----
    @abc.abstractmethod
    def info(self) -> dict: ...


# ============================ LOCAL (SQLite + in-memory B+ tree/graph) ============================

class LocalBackend(MemoryBackend):
    name = "local"
    stub = False

    def __init__(self):
        # 进程内立体图 + B+ 树；启动时从 SQLite 重建（持久事实在 L2 表）
        from memory.graph import PropertyGraph
        from memory.btree import BPlusTree
        self.graph = PropertyGraph()
        self.entity_trees: dict[str, dict[str, BPlusTree]] = {}  # node_id -> dim -> tree

    def _conn(self):
        return get_backend_conn()

    # ---- L0 ----
    async def l0_append(self, agent_id, input_text, output_text):
        from core import db
        conn = self._conn()
        await db.execute(conn,
                         "INSERT INTO memory_l0_raw (agent_id, input, output) VALUES (?,?,?)",
                         (agent_id, input_text, output_text))

    async def l0_recent(self, limit=20):
        from core import db
        return await db.fetchall(self._conn(),
                                 "SELECT * FROM memory_l0_raw ORDER BY id DESC LIMIT ?", (limit,))

    # ---- L1 ----
    async def l1_write(self, agent_key, text, answer):
        from core import db
        vec = await embedding.embed(text)
        conn = self._conn()
        # 语义去重：同一 agent 下已有 >0.999 的相同问句 -> 更新答案
        existing = await db.fetchall(conn, "SELECT * FROM memory_l1_cache WHERE agent_key=?", (agent_key,))
        for e in existing:
            try:
                if embedding.cosine(json.loads(e["embedding"]), vec) > 0.999:
                    await db.execute(conn, "UPDATE memory_l1_cache SET answer=?, embedding=?, "
                                           "ts=datetime('now') WHERE id=?", (answer, json.dumps(vec), e["id"]))
                    return
            except Exception:
                continue
        await db.execute(conn,
                         "INSERT INTO memory_l1_cache (agent_key, text, answer, embedding) VALUES (?,?,?,?)",
                         (agent_key, text, answer, json.dumps(vec)))

    async def l1_search(self, agent_key, text, threshold):
        from core import db
        vec = await embedding.embed(text)
        rows = await db.fetchall(self._conn(),
                                 "SELECT * FROM memory_l1_cache WHERE agent_key=?", (agent_key,))
        best = None
        for r in rows:
            try:
                sim = embedding.cosine(json.loads(r["embedding"]), vec)
            except Exception:
                continue
            if sim >= threshold and (best is None or sim > best["similarity"]):
                best = {"text": r["text"], "answer": r["answer"], "similarity": round(sim, 4)}
        return best

    async def l1_image_lookup(self, md5):
        from core import db
        r = await db.fetchone(self._conn(),
                              "SELECT * FROM memory_l1_image WHERE md5=?", (md5,))
        if r:
            r["parsed"] = json.loads(r["parsed"])
        return r

    async def l1_image_store(self, md5, content_type, parsed):
        from core import db
        conn = self._conn()
        r = await db.fetchone(conn, "SELECT id FROM memory_l1_image WHERE md5=?", (md5,))
        if r:
            await db.execute(conn, "UPDATE memory_l1_image SET parsed=?, ts=datetime('now') WHERE md5=?",
                             (json.dumps(parsed, ensure_ascii=False), md5))
        else:
            await db.execute(conn,
                             "INSERT INTO memory_l1_image (md5, content_type, parsed) VALUES (?,?,?)",
                             (md5, content_type, json.dumps(parsed, ensure_ascii=False)))

    async def l1_list(self, agent_key=None, limit=50):
        from core import db
        if agent_key:
            return await db.fetchall(self._conn(),
                                     "SELECT * FROM memory_l1_cache WHERE agent_key=? ORDER BY id DESC LIMIT ?",
                                     (agent_key, limit))
        return await db.fetchall(self._conn(),
                                 "SELECT * FROM memory_l1_cache ORDER BY id DESC LIMIT ?", (limit,))

    # ---- L2 ----
    async def _ensure_loaded(self):
        if self.graph.node_count():
            return
        from core import db
        conn = self._conn()
        nodes = await db.fetchall(conn, "SELECT * FROM memory_l2_nodes")
        for n in nodes:
            try:
                self.graph.nodes[n["id"]] = _mk_node(n["id"], n["label"], n["props"])
            except Exception:
                continue
        edges = await db.fetchall(conn, "SELECT * FROM memory_l2_edges")
        for e in edges:
            try:
                self.graph.merge_edge(e["src"], e["dst"], e["type"], json.loads(e["props"] or "{}"))
            except Exception:
                continue
        buckets = await db.fetchall(conn, "SELECT * FROM memory_l2_buckets")
        for b in buckets:
            self._tree_for(b["node_id"], b["dim"]).insert(b["key"], json.loads(b["record"]))

    def _tree_for(self, node_id: str, dim: str):
        from memory.btree import BPlusTree
        trees = self.entity_trees.setdefault(node_id, {})
        if dim not in trees:
            node = self.graph.nodes.get(node_id)
            label = node.label if node else node_id
            trees[dim] = BPlusTree(f"{label}.{dim}")
        return trees[dim]

    async def l2_node_or_create(self, id, label, props=None):
        from core import db
        n = self.graph.get_or_create_node(id, label, props or {})
        conn = self._conn()
        r = await db.fetchone(conn, "SELECT id FROM memory_l2_nodes WHERE id=?", (id,))
        if r:
            await db.execute(conn, "UPDATE memory_l2_nodes SET label=?, props=? WHERE id=?",
                             (label, json.dumps(n.props, ensure_ascii=False), id))
        else:
            await db.execute(conn, "INSERT INTO memory_l2_nodes (id, label, props) VALUES (?,?,?)",
                             (id, label, json.dumps(n.props, ensure_ascii=False)))
        return n

    async def l2_merge_edge(self, src, dst, type_, props=None):
        from core import db
        e = self.graph.merge_edge(src, dst, type_, props or {})
        conn = self._conn()
        await db.execute(
            conn,
            """INSERT INTO memory_l2_edges (src, dst, type, props) VALUES (?,?,?,?)
               ON CONFLICT(src,dst,type) DO UPDATE SET props=excluded.props""",
            (src, dst, type_, json.dumps(e.props, ensure_ascii=False)))
        return e

    async def l2_query(self, q: dict) -> dict:
        await self._ensure_loaded()
        out = {}
        if "from" in q:
            out["bfs"] = [e.to_dict() for e in self.graph.bfs(q["from"], hops=q.get("hops", 1),
                                                              types=q.get("types"))]
            out["path"] = self.graph.path(q["from"], hops=q.get("hops", 1), types=q.get("types"))
        if "nodes" in q:
            out["nodes"] = [n.to_dict() for n in self.graph.nodes.values()]
        if "edges" in q:
            out["edges"] = [e.to_dict() for e in self.graph.edges.values()]
        if "neighbors" in q:
            out["neighbors"] = self.graph.neighbors(q["neighbors"])
        if "labels" in q:
            out["labels"] = sorted(self.graph.labels())
        if "subgraph" in q:
            sub = self.graph.subgraph(q["subgraph"], hops=q.get("hops", 2))
            out["subgraph_nodes"] = [n.to_dict() for n in sub["nodes"]]
            out["subgraph_edges"] = [e.to_dict() for e in sub["edges"]]
            out["subgraph"] = self.graph.subgraph_summary(q["subgraph"], hops=q.get("hops", 2))
        out["counts"] = {"nodes": self.graph.node_count(), "edges": self.graph.edge_count()}
        return out

    async def l2_bucket_write(self, node_id, dim, key, record):
        from core import db
        await self._ensure_loaded()
        self._tree_for(node_id, dim).insert(key, record)
        conn = self._conn()
        rec = json.dumps(record, ensure_ascii=False)
        existing = await db.fetchone(
            conn, "SELECT id FROM memory_l2_buckets WHERE node_id=? AND dim=? AND key=?",
            (node_id, dim, key))
        if existing:
            await db.execute(conn, "UPDATE memory_l2_buckets SET record=? WHERE id=?", (rec, existing["id"]))
        else:
            await db.execute(conn,
                             "INSERT INTO memory_l2_buckets (node_id, dim, key, record) VALUES (?,?,?,?)",
                             (node_id, dim, key, rec))

    async def l2_bucket_read(self, node_id, dim, key=None):
        await self._ensure_loaded()
        tree = self._tree_for(node_id, dim)
        if key is not None:
            v = tree.get(key)
            return [v] if v is not None else []
        return [v for _, v in tree.in_order()]

    def info(self):
        from core import db as dbmod
        d = dbmod.backend_info()
        store = (f"{'PostgreSQL' if d['backend'] == 'postgres' else 'SQLite'}"
                 f"({d.get('schema', '')} schema, {d.get('host', '')}:{d.get('port', '')})"
                 if d["backend"] == "postgres"
                 else f"SQLite({d['path']})")
        return {
            "name": "local",
            "stub": False,
            "store": f"{store} + in-memory B+ tree/property graph",
            "graph_nodes": self.graph.node_count(),
            "graph_edges": self.graph.edge_count(),
        }


def _mk_node(id, label, props_json):
    from memory.graph import GNode
    try:
        props = json.loads(props_json or "{}")
    except Exception:
        props = {}
    return GNode(id, label, props)


# ============================ 适配位存根 (redis / milvus / neo4j, DECISION-002) ============================

class StubBackend(MemoryBackend):
    """适配位：接口与 LocalBackend 一致，但依赖的实体服务在本期环境不存在。
    调用时抛受控错误（不崩溃）；部署期将真实客户端注入即可切换（PD-006）。"""
    name = "stub"

    def __init__(self, backend_name: str, note: str):
        self.name = backend_name
        self.stub = True
        self.note = note

    async def l0_append(self, agent_id, input_text, output_text):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根，未接入真实服务（{self.note}）")

    async def l0_recent(self, limit=20):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l1_write(self, agent_key, text, answer):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l1_search(self, agent_key, text, threshold):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l1_image_lookup(self, md5):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l1_image_store(self, md5, content_type, parsed):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l1_list(self, agent_key=None, limit=50):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l2_node_or_create(self, id, label, props=None):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l2_merge_edge(self, src, dst, type_, props=None):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l2_query(self, q):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l2_bucket_write(self, node_id, dim, key, record):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    async def l2_bucket_read(self, node_id, dim, key=None):
        raise RuntimeError(f"记忆后端 {self.name} 为适配位存根（{self.note}）")

    def info(self):
        return {"name": self.name, "stub": True, "note": self.note}


# ============================ Hermes 适配器 (真实对接 Hermes 记忆策略) ============================

class HermesBackend(LocalBackend):
    """对接 Hermes Agent 记忆策略：在本地 backend 之外，把每条 L0/L1 事实以
    JSONL 行（{ts, agent, kind, text}）追加写入 HERMES_MEMORY_DIR（Hermes 风格的
    可追加事实流，供 Hermes 记忆/检索消费）。接口位 + 真实可写。"""
    name = "hermes"
    stub = False

    def __init__(self):
        super().__init__()
        self.path = S.HERMES_MEMORY_DIR

    async def l0_append(self, agent_id, input_text, output_text):
        await super().l0_append(agent_id, input_text, output_text)
        await self._hermes_line(agent_id, "l0", f"IN: {input_text} | OUT: {output_text}")

    async def l1_write(self, agent_key, text, answer):
        await super().l1_write(agent_key, text, answer)
        await self._hermes_line(agent_key, "l1_cache", f"Q: {text} | A: {answer}")

    async def l2_merge_edge(self, src, dst, type_, props=None):
        e = await super().l2_merge_edge(src, dst, type_, props)
        await self._hermes_line("graph", "l2_edge",
                                f"({src})-[:{type_} {json.dumps(e.props, ensure_ascii=False)}]->({dst})")

    async def _hermes_line(self, agent: str, kind: str, text: str):
        import datetime
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            line = json.dumps({
                "ts": datetime.datetime.now().isoformat(),
                "agent": agent, "kind": kind, "text": text,
            }, ensure_ascii=False)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: open(self.path, "a", encoding="utf-8").write(line + "\n"))
        except Exception:
            pass  # Hermes 侧写入失败不阻塞主链路

    def info(self):
        d = super().info()
        d.update({"hermes": {"jsonl": self.path,
                            "exists": os.path.exists(self.path)}})
        return d


# ============================ 工厂 / 全局 ============================

_registry = {
    "local": lambda: LocalBackend(),
    "redis": lambda: StubBackend("redis", "需 Redis 实体服务；部署期注入 redis.asyncio 客户端，默认 local（DECISION-002）"),
    "milvus": lambda: StubBackend("milvus", "需 Milvus 实体服务；部署期注入 pymilvus 集合，默认 local（DECISION-002）"),
    "neo4j": lambda: StubBackend("neo4j", "需 Neo4j 实体服务；部署期注入 neo4j 驱动（MERGE 语义），默认 local（DECISION-002）"),
    "hermes": lambda: HermesBackend(),
}

_backend: MemoryBackend | None = None
_conn = None


def get_backend_conn():
    return _conn


async def init_memory():
    global _backend, _conn
    from core import db
    _conn = await db.connect()
    name = S.MEMORY_BACKEND
    if name not in _registry:
        name = "local"
    _backend = _registry[name]()
    return _backend


def get_memory_backend() -> MemoryBackend:
    """agent 引擎唯一记忆入口（只依赖此抽象，AC-50 解耦断言点）。"""
    if _backend is None:
        raise RuntimeError("memory backend 未初始化")
    return _backend


def list_adapters() -> dict:
    return {
        "local": {"enabled": True, "stub": False, "note": "SQLite + 内存 B+ 树/双向属性图（默认）"},
        "redis": {"enabled": True, "stub": True, "note": "适配位：接口一致，部署期注入真实 Redis"},
        "milvus": {"enabled": True, "stub": True, "note": "适配位：接口一致，部署期注入真实 Milvus"},
        "neo4j": {"enabled": True, "stub": True, "note": "适配位：接口一致，部署期注入真实 Neo4j"},
        "hermes": {"enabled": True, "stub": False, "note": "对接 Hermes 记忆策略：JSONL 事实流写入"},
        "current": S.MEMORY_BACKEND,
    }
