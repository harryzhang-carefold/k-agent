"""B+ tree (in-memory) — L2 "立体图" ordered access component (PRD module 12, AC-44).

Each entity node in the property graph is the root of a B+ tree, bucketed by
dimension (date / entity / process / topic per PD-008). Fan-out 4.
Operations: insert, range_scan, in-order traversal (assertable sorted), by_key.
"""
from typing import Any, Optional


class BPlusNode:
    def __init__(self, leaf: bool):
        self.keys: list = []
        self.children: list = []      # internal: child nodes
        self.values: list = []        # leaf: record dicts
        self.leaf = leaf
        self.prev: Optional["BPlusNode"] = None
        self.next: Optional["BPlusNode"] = None


class BPlusTree:
    FANOUT = 4  # max keys per node

    def __init__(self, name: str = ""):
        self.root = BPlusNode(leaf=True)
        self.name = name

    # ---------- insert ----------
    def insert(self, key: Any, value: Any):
        leaf = self._find_leaf(key)
        idx = self._bisect(leaf.keys, key)
        leaf.keys.insert(idx, key)
        leaf.values.insert(idx, value)
        if len(leaf.keys) > self.FANOUT:
            self._split_leaf(leaf)
        self._rebalance_up(leaf)

    def _find_leaf(self, key: Any) -> BPlusNode:
        node = self.root
        while not node.leaf:
            idx = self._bisect(node.keys, key)
            node = node.children[idx]
        return node

    @staticmethod
    def _bisect(keys: list, key: Any) -> int:
        lo, hi = 0, len(keys)
        while lo < hi:
            mid = (lo + hi) // 2
            if key < keys[mid]:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def _split_leaf(self, leaf: BPlusNode):
        mid = len(leaf.keys) // 2
        right = BPlusNode(leaf=True)
        right.keys = leaf.keys[mid:]
        right.values = leaf.values[mid:]
        promote = right.keys[0]
        leaf.keys = leaf.keys[:mid]
        leaf.values = leaf.values[:mid]
        right.prev = leaf
        right.next = leaf.next
        if leaf.next:
            leaf.next.prev = right
        leaf.next = right
        self._insert_into_parent(leaf, promote, right)

    def _split_internal(self, node: BPlusNode):
        mid = len(node.keys) // 2
        promote = node.keys[mid]
        right = BPlusNode(leaf=False)
        right.keys = node.keys[mid + 1:]
        right.children = node.children[mid + 1:]
        node.keys = node.keys[:mid]
        node.children = node.children[:mid + 1]
        self._insert_into_parent(node, promote, right)

    def _insert_into_parent(self, left: BPlusNode, key: Any, right: BPlusNode):
        if left is self.root:
            new_root = BPlusNode(leaf=False)
            new_root.keys = [key]
            new_root.children = [left, right]
            self.root = new_root
            return
        parent = self._find_parent(self.root, left)
        idx = parent.children.index(left)
        parent.keys.insert(idx, key)
        parent.children.insert(idx + 1, right)
        if len(parent.keys) > self.FANOUT:
            self._split_internal(parent)
            self._rebalance_up(parent)

    def _find_parent(self, node: BPlusNode, target: BPlusNode) -> BPlusNode:
        if node.leaf:
            return None
        for c in node.children:
            if c is target:
                return node
            if not c.leaf:
                p = self._find_parent(c, target)
                if p:
                    return p
        return None

    def _rebalance_up(self, node: BPlusNode):
        # handled inline in splits; this is a no-op safety walk
        return

    # ---------- queries ----------
    def get(self, key: Any):
        leaf = self._find_leaf(key)
        if key in leaf.keys:
            return leaf.values[leaf.keys.index(key)]
        return None

    def range_scan(self, lo, hi):
        """All keys k with lo <= k <= hi, in order."""
        out = []
        leaf = self._find_leaf(lo)
        node = leaf
        while node is not None:
            for i, k in enumerate(node.keys):
                if k > hi:
                    return out
                if k >= lo:
                    out.append(node.values[i])
            node = node.next
        return out

    def in_order(self):
        """Full in-order traversal (assertable: sorted)."""
        out = []
        node = self.root
        while not node.leaf:
            node = node.children[0]
        while node is not None:
            for i, k in enumerate(node.keys):
                out.append((k, node.values[i]))
            node = node.next
        return out

    def keys_sorted(self):
        return [k for k, _ in self.in_order()]

    def __len__(self):
        return len(self.in_order())
