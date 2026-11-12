"""Answer-cache evidence: isolation, revocation, expiry and bounded capacity.

Only a temporary local SQLite database and synthetic answers are used. No
model, network or provider call is made.
"""

import concurrent.futures

import pytest

from app.answer_cache import AnswerCacheService, cache_key, normalize_prompt
from app.config import Settings
from app.service import Service


PASSWORD = "Correct-test-password-123"


@pytest.fixture(scope="module")
def cache_service(tmp_path_factory):
    # Service construction prepares the SQLite schema and is the dominant cost
    # on the shared test host, so one local service is shared by this module.
    root = tmp_path_factory.mktemp("answer-cache")
    service = Service(Settings(data_dir=root, provider_settings="", origins=("http://testserver",)))
    admin = service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
    user = service.create_user({"username": "member", "display_name": "员工", "password": PASSWORD, "role": "member"})
    other = service.create_user({"username": "other", "display_name": "另一员工", "password": PASSWORD, "role": "member"})
    assistant = service.save_assistant({"name": "缓存测试", "instructions": "", "scope": "team", "connections": []}, user=admin)
    second = service.save_assistant({"name": "另一个助手", "instructions": "", "scope": "team", "connections": []}, user=admin)
    try:
        yield service, admin, user, other, assistant, second
    finally:
        service.db.close()


@pytest.fixture
def cache_env(cache_service):
    service = cache_service[0]
    # Each test starts from an empty cache and fresh counters; the shared
    # service is otherwise read-only for this suite.
    service.answer_cache.invalidate_all()
    service.answer_cache.max_entries = 256
    with service.answer_cache._lock:
        service.answer_cache._stats = {key: 0 for key in service.answer_cache._stats}
    service.db.execute("UPDATE users SET enabled=1")
    service.db.execute("UPDATE assistants SET enabled=1")
    return cache_service


def _remember(service, user, assistant, prompt, answer="合成答案", **kwargs):
    return service.remember_answer(user["id"], assistant["id"], prompt, answer, **kwargs)


def _lookup(service, user, assistant, prompt, **kwargs):
    return service.cached_answer(user["id"], assistant["id"], prompt, **kwargs)


def test_store_hit_and_observability(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    stored = _remember(service, user, assistant, "设备温度是多少？", "68 摄氏度", citations=[{"id": "s1"}])
    assert stored is not None
    hit = _lookup(service, user, assistant, "设备温度是多少？")
    assert hit is not None
    assert hit.answer == "68 摄氏度"
    assert hit.citations == ({"id": "s1"},)
    assert _lookup(service, user, assistant, "未缓存的问题") is None
    stats = service.answer_cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["stores"] == 1
    assert stats["entries"] == 1


def test_isolation_by_user_assistant_prompt_knowledge_and_model(cache_env):
    service, _admin, user, other, assistant, second = cache_env
    prompt = "同样的业务问题"
    assert _remember(service, user, assistant, prompt, knowledge_version="kv1", model_version="m1") is not None
    # Same principal and boundary hits.
    assert _lookup(service, user, assistant, prompt, knowledge_version="kv1", model_version="m1") is not None
    # Every boundary dimension is a distinct cache namespace.
    assert _lookup(service, other, assistant, prompt, knowledge_version="kv1", model_version="m1") is None
    assert _lookup(service, user, second, prompt, knowledge_version="kv1", model_version="m1") is None
    assert _lookup(service, user, assistant, prompt + "（补充）", knowledge_version="kv1", model_version="m1") is None
    assert _lookup(service, user, assistant, prompt, knowledge_version="kv2", model_version="m1") is None
    assert _lookup(service, user, assistant, prompt, knowledge_version="kv1", model_version="m2") is None


def test_expiry_and_purge(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    assert _remember(service, user, assistant, "立刻过期", ttl_seconds=0) is not None
    assert _lookup(service, user, assistant, "立刻过期") is None
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 0

    assert _remember(service, user, assistant, "一小时后过期", ttl_seconds=3600) is not None
    assert service.answer_cache.purge_expired() == 0
    assert _lookup(service, user, assistant, "一小时后过期") is not None


def test_hit_time_revalidation_revokes_disabled_user(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    assert _remember(service, user, assistant, "需要重新验权") is not None
    # Simulate a grant change that did not go through the normal invalidation
    # path; the hit-time check must still refuse and delete the entry.
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    assert _lookup(service, user, assistant, "需要重新验权") is None
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 0
    service.db.execute("UPDATE users SET enabled=1 WHERE id=?", (user["id"],))


def test_hit_time_revalidation_revokes_archived_assistant(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    assert _remember(service, user, assistant, "助手停用后失效") is not None
    service.db.execute("UPDATE assistants SET enabled=0 WHERE id=?", (assistant["id"],))
    assert _lookup(service, user, assistant, "助手停用后失效") is None
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 0


def test_invalidation_by_scope(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    kb = service.knowledge.save({"name": "知识库", "scope": "team"}, _admin)
    base = dict(knowledge_version="kv", model_version="m", knowledge_bases=[kb["id"]])
    assert _remember(service, user, assistant, "用户范围", **base) is not None
    assert _remember(service, user, assistant, "助手范围", **base) is not None
    assert _remember(service, user, assistant, "会话范围", conversation_id="conversation-1", **base) is not None
    assert _remember(service, user, assistant, "模型范围", **base) is not None
    assert _remember(service, user, assistant, "知识范围", **base) is not None

    service.answer_cache.invalidate_user(user["id"])
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 0
    assert _remember(service, user, assistant, "助手范围", **base) is not None
    service.answer_cache.invalidate_assistant(assistant["id"])
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 0
    assert _remember(service, user, assistant, "会话范围", conversation_id="conversation-1", **base) is not None
    assert service.answer_cache.invalidate_conversation("conversation-1") == 1
    assert _remember(service, user, assistant, "模型范围", **base) is not None
    assert service.answer_cache.invalidate_model("m") == 1
    assert _remember(service, user, assistant, "知识范围", **base) is not None
    assert service.answer_cache.invalidate_knowledge(kb["id"]) == 1
    assert service.answer_cache.stats()["invalidations"] >= 5


def test_side_effects_approvals_and_sensitive_answers_are_never_cached(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    prompt = "写操作结果"
    assert _remember(service, user, assistant, prompt, side_effects=True) is None
    assert _remember(service, user, assistant, prompt, approval_involved=True) is None
    assert _remember(service, user, assistant, prompt, sensitive=True) is None
    assert _remember(service, user, assistant, prompt, answer="   ") is None
    assert _remember(service, user, assistant, prompt, answer="x" * 50000) is None
    assert _remember(service, user, assistant, "p" * 5000) is None
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 0
    assert service.answer_cache.stats()["rejected"] == 6


def test_capacity_is_bounded_and_evicts_oldest(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    service.answer_cache.max_entries = 3
    for index in range(5):
        assert _remember(service, user, assistant, f"问题 {index}") is not None
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 3
    assert service.answer_cache.stats()["evictions"] == 2
    assert _lookup(service, user, assistant, "问题 0") is None
    assert _lookup(service, user, assistant, "问题 4") is not None


def test_concurrent_stores_keep_one_row(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    key = dict(user_id=user["id"], assistant_id=assistant["id"], prompt="并发问题")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_remember, service, user, assistant, "并发问题", f"答案 {index}") for index in range(12)]
        assert all(future.result() is not None for future in futures)
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 1
    hit = _lookup(service, user, assistant, "并发问题")
    assert hit is not None
    assert hit.answer.startswith("答案 ")
    boundary, _digest = cache_key(**key)
    assert service.db.one("SELECT cache_key FROM answer_cache")["cache_key"] == boundary


def test_prompt_normalization_shares_one_entry(cache_env):
    service, _admin, user, _other, assistant, _second = cache_env
    assert normalize_prompt("  What   is the TEMP? ") == "what is the temp?"
    assert _remember(service, user, assistant, "设备温度 是多少？") is not None
    assert _lookup(service, user, assistant, "　设备温度   是多少？") is not None
    assert service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"] == 1


def test_assistant_and_knowledge_updates_invalidate_through_service(cache_env):
    service, admin, user, _other, assistant, _second = cache_env
    kb = service.knowledge.save({"name": "待更新知识库", "scope": "team"}, admin)
    assert _remember(service, user, assistant, "助手更新后失效", knowledge_bases=[kb["id"]]) is not None
    service.save_assistant({"name": "缓存测试改名"}, assistant["id"], admin)
    assert _lookup(service, user, assistant, "助手更新后失效") is None

    assert _remember(service, user, assistant, "知识更新后失效", knowledge_bases=[kb["id"]]) is not None
    service.knowledge.save({"name": "待更新知识库改名"}, admin, kb["id"])
    assert _lookup(service, user, assistant, "知识更新后失效") is None


def test_stats_expose_limits(cache_env):
    service, _admin, _user, _other, _assistant, _second = cache_env
    stats = service.answer_cache.stats()
    assert stats["max_entries"] == 256
    assert stats["ttl_seconds"] == 3600
    assert stats["entries"] == 0
    assert set(stats) >= {"hits", "misses", "stores", "rejected", "evictions", "invalidations"}
