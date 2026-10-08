"""Tests for Redis Semantic Cache and /api/v1/cache/evaluate."""

import math

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.redis import get_redis_client
from app.main import app
from app.services.cache import (
    RedisSemanticCache,
    compute_cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)


class FakeRedis:
    """In-memory async Redis simulator for unit and integration testing."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        if key not in self.hashes:
            self.hashes[key] = {}
        self.hashes[key].update(mapping)
        return len(mapping)

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def sadd(self, key: str, *members: str) -> int:
        if key not in self.sets:
            self.sets[key] = set()
        count = 0
        for m in members:
            if m not in self.sets[key]:
                self.sets[key].add(m)
                count += 1
        return count

    async def srem(self, key: str, *members: str) -> int:
        if key not in self.sets:
            return 0
        count = 0
        for m in members:
            if m in self.sets[key]:
                self.sets[key].remove(m)
                count += 1
        return count

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self.hashes:
                del self.hashes[k]
                count += 1
            if k in self.sets:
                del self.sets[k]
                count += 1
            if k in self.ttls:
                del self.ttls[k]
        return count

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, fake_redis: FakeRedis) -> None:
        self.fake = fake_redis
        self.commands: list[tuple[str, tuple, dict]] = []

    def hgetall(self, key: str) -> "FakePipeline":
        self.commands.append(("hgetall", (key,), {}))
        return self

    def hset(self, key: str, mapping: dict[str, str]) -> "FakePipeline":
        self.commands.append(("hset", (key, mapping), {}))
        return self

    def expire(self, key: str, ttl: int) -> "FakePipeline":
        self.commands.append(("expire", (key, ttl), {}))
        return self

    def sadd(self, key: str, member: str) -> "FakePipeline":
        self.commands.append(("sadd", (key, member), {}))
        return self

    def delete(self, key: str) -> "FakePipeline":
        self.commands.append(("delete", (key,), {}))
        return self

    async def execute(self) -> list:
        results = []
        for cmd, args, kwargs in self.commands:
            method = getattr(self.fake, cmd)
            res = await method(*args, **kwargs)
            results.append(res)
        return results


def test_embedding_serialization_roundtrip() -> None:
    """Verifies that vector serialization retains exact float32 fidelity."""
    original = [0.012345, -0.987654, 0.5, 0.0] * 384  # 1536 floats
    serialized = serialize_embedding(original)
    assert isinstance(serialized, str)
    deserialized = deserialize_embedding(serialized)
    assert len(deserialized) == 1536
    for o, d in zip(original, deserialized, strict=True):
        assert math.isclose(o, d, rel_tol=1e-5, abs_tol=1e-6)


def test_cosine_similarity_edge_cases() -> None:
    """Verifies cosine similarity computation across orthogonal, identical, and mismatched vectors."""
    vec_a = [1.0, 0.0, 0.0]
    vec_b = [1.0, 0.0, 0.0]
    assert math.isclose(compute_cosine_similarity(vec_a, vec_b), 1.0, rel_tol=1e-5)

    vec_c = [0.0, 1.0, 0.0]
    assert math.isclose(compute_cosine_similarity(vec_a, vec_c), 0.0, abs_tol=1e-5)

    vec_d = [-1.0, 0.0, 0.0]
    assert math.isclose(compute_cosine_similarity(vec_a, vec_d), -1.0, rel_tol=1e-5)

    # Dimension mismatch
    assert compute_cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


@pytest.mark.asyncio
async def test_semantic_cache_exact_hit_and_miss() -> None:
    """Verifies exact-match fast path and threshold-based semantic cache hits."""
    fake_redis = FakeRedis()
    cache = RedisSemanticCache(fake_redis)  # type: ignore[arg-type]

    tenant_id = "tenant_test"
    query_1 = "What is SentinelRAG?"
    vec_1 = [0.1] * 1536
    norm = math.sqrt(sum(x * x for x in vec_1))
    vec_1 = [x / norm for x in vec_1]

    # Initial lookup -> CACHE_MISS
    res = await cache.get_or_lookup(tenant_id, query_1, vec_1)
    assert res.status == "CACHE_MISS"
    assert res.cached_response is None

    # Store entry
    await cache.set(
        tenant_id=tenant_id,
        query=query_1,
        query_vector=vec_1,
        response="SentinelRAG is an enterprise AI gateway.",
        metadata={"model": "gpt-4o"},
    )

    # Exact-match fast path lookup
    exact_res = await cache.get_or_lookup(tenant_id, query_1, vec_1)
    assert exact_res.status == "CACHE_HIT"
    assert exact_res.similarity == 1.0
    assert exact_res.distance == 0.0
    assert exact_res.cached_response == "SentinelRAG is an enterprise AI gateway."

    # Semantic match query with very high similarity (sim >= 0.92, dist <= 0.08)
    query_similar = "Tell me about SentinelRAG"
    vec_similar = list(vec_1)
    vec_similar[0] += 0.005  # slight perturbation
    norm_sim = math.sqrt(sum(x * x for x in vec_similar))
    vec_similar = [x / norm_sim for x in vec_similar]

    sem_res = await cache.get_or_lookup(tenant_id, query_similar, vec_similar)
    assert sem_res.status == "CACHE_HIT"
    assert sem_res.similarity is not None and sem_res.similarity >= 0.92
    assert sem_res.distance is not None and sem_res.distance <= 0.08
    assert sem_res.cached_response == "SentinelRAG is an enterprise AI gateway."

    # Dissimilar query (orthogonal vector -> sim ~0.0, dist ~1.0)
    query_diff = "How do you bake bread?"
    vec_diff = [-x for x in vec_1]
    diff_res = await cache.get_or_lookup(tenant_id, query_diff, vec_diff)
    assert diff_res.status == "CACHE_MISS"
    assert diff_res.cached_response is None


@pytest.mark.asyncio
async def test_tenant_invalidation_isolation() -> None:
    """Verifies that invalidating tenant A does not purge tenant B."""
    fake_redis = FakeRedis()
    cache = RedisSemanticCache(fake_redis)  # type: ignore[arg-type]

    vec = [0.05] * 1536
    await cache.set("tenant_A", "query A", vec, "response A")
    await cache.set("tenant_B", "query B", vec, "response B")

    # Invalidate tenant_A
    evicted = await cache.invalidate_tenant("tenant_A")
    assert evicted >= 1

    # tenant_A is gone
    res_a = await cache.get_or_lookup("tenant_A", "query A", vec)
    assert res_a.status == "CACHE_MISS"

    # tenant_B remains untouched
    res_b = await cache.get_or_lookup("tenant_B", "query B", vec)
    assert res_b.status == "CACHE_HIT"
    assert res_b.cached_response == "response B"


@pytest.mark.asyncio
async def test_api_cache_evaluate_endpoint() -> None:
    """Integration test for POST /api/v1/cache/evaluate endpoint."""
    fake_redis = FakeRedis()

    async def override_get_redis():
        return fake_redis

    app.dependency_overrides[get_redis_client] = override_get_redis

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            vec = [0.02] * 1536
            norm = math.sqrt(sum(x * x for x in vec))
            norm_vec = [x / norm for x in vec]

            # 1. First evaluation: CACHE_MISS with store_if_miss=True
            payload = {
                "query": "What is the hybrid retrieval latency?",
                "query_vector": norm_vec,
                "tenant_id": "tenant_eval",
                "threshold": 0.08,
                "prompt_response": "Hybrid retrieval latency is under 15ms.",
                "store_if_miss": True,
            }
            resp = await client.post("/api/v1/cache/evaluate", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "CACHE_MISS"
            assert data["stored"] is True
            assert "lookup_latency_ms" in data

            # 2. Second evaluation: CACHE_HIT on exact query
            resp2 = await client.post("/api/v1/cache/evaluate", json=payload)
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["status"] == "CACHE_HIT"
            assert data2["cached_response"] == "Hybrid retrieval latency is under 15ms."
            assert data2["distance"] == 0.0
            assert data2["similarity"] == 1.0

            # 3. Third evaluation: CACHE_HIT on semantic perturbation
            perturbed_vec = list(norm_vec)
            perturbed_vec[0] += 0.001
            p_norm = math.sqrt(sum(x * x for x in perturbed_vec))
            perturbed_vec = [x / p_norm for x in perturbed_vec]

            payload3 = {
                "query": "What is the latency of hybrid search?",
                "query_vector": perturbed_vec,
                "tenant_id": "tenant_eval",
                "threshold": 0.08,
                "store_if_miss": False,
            }
            resp3 = await client.post("/api/v1/cache/evaluate", json=payload3)
            assert resp3.status_code == 200
            data3 = resp3.json()
            assert data3["status"] == "CACHE_HIT"
            assert data3["similarity"] >= 0.92
            assert data3["distance"] <= 0.08
            assert data3["cached_response"] == "Hybrid retrieval latency is under 15ms."

    finally:
        app.dependency_overrides.pop(get_redis_client, None)
