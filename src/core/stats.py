"""Global observability counters (PRD 9. 可观测): LLM calls / cache hits."""

COUNTERS = {
    "llm_calls": 0,
    "cache_md5_hits": 0,
    "cache_semantic_hits": 0,
    "split_mixed": 0,
    "split_complex": 0,
    "llm_errors": 0,
}


def bump(name: str, n: int = 1):
    COUNTERS[name] = COUNTERS.get(name, 0) + n


def snapshot():
    return dict(COUNTERS)
