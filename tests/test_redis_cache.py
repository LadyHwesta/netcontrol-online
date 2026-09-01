"""
Tests for the optional Redis-backed cache layer (routers/helpers.py) that
makes the DMR/APRS relay-push caches correct across multiple uvicorn
worker processes (TECH_DEBT.md: "In-memory cache and rate limiter won't
survive multiple workers" -- resolved).

Uses a fake in-process Redis stand-in (monkeypatched onto
helpers._get_redis_client) rather than a real Redis server -- Redis stays
entirely optional in this app (unset by default; see REDIS_URL), and
deploy.sh's test run has no Redis server to stand one up. The actual
redis-py wiring (REDIS_URL -> redis.asyncio.from_url, and the rate
limiter's storage_uri resolving to `limits`' RedisStorage vs. MemoryStorage
when unset) was verified manually against a real disposable Redis instance
instead, not re-proven here -- that's third-party library behavior, not
app logic.
"""

from routers import aprs, digital_voice, helpers


class _FakeRedis:
    """Minimal async stand-in for redis.asyncio.Redis -- get/set only,
    backed by a plain dict."""
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _RaisingRedis:
    """Simulates a Redis outage -- every call raises, so callers must
    degrade gracefully rather than 500ing the request that triggered it."""
    async def get(self, key):
        raise ConnectionError("simulated Redis outage")

    async def set(self, key, value, ex=None):
        raise ConnectionError("simulated Redis outage")


def _clear_push_caches():
    digital_voice._dmr_push_cache.clear()
    aprs._aprs_push_cache.clear()


class TestRedisUrlWithDb:
    """_redis_url_with_db (issue follow-up) -- REDIS_DB in .env is always
    the final word on which logical Redis database an instance uses, so
    several instances (main/testing/demo) on one server can share a single
    Redis server without their cache/rate-limit data colliding."""

    def test_overrides_existing_db_in_path(self):
        assert helpers._redis_url_with_db("redis://localhost:6379/0", 3) == "redis://localhost:6379/3"

    def test_adds_db_when_url_has_none(self):
        assert helpers._redis_url_with_db("redis://localhost:6379", 2) == "redis://localhost:6379/2"

    def test_preserves_auth_and_query_string(self):
        assert helpers._redis_url_with_db("redis://user:pass@host:6379/0?ssl_cert_reqs=none", 5) \
            == "redis://user:pass@host:6379/5?ssl_cert_reqs=none"

    def test_rediss_scheme_supported(self):
        assert helpers._redis_url_with_db("rediss://host:6379", 1) == "rediss://host:6379/1"

    def test_unsupported_scheme_left_unchanged(self):
        """Unix sockets, sentinel, and cluster URLs don't address the db
        index via the URL path the same way -- left alone rather than
        corrupted by blindly overwriting the path."""
        original = "redis+sentinel://host:26379/mymaster"
        assert helpers._redis_url_with_db(original, 4) == original


class TestInstanceKeyPrefixAvoidsCollisions:
    """Two 'instances' sharing the same Redis database (misconfigured
    REDIS_DB, or none set) still can't collide -- INSTANCE_KEY_PREFIX
    namespaces every key this app writes, independent of REDIS_DB."""

    async def test_different_prefixes_isolate_same_underlying_store(self, monkeypatch):
        shared_backing_store = {}

        class _SharedFakeRedis:
            async def get(self, key):
                return shared_backing_store.get(key)

            async def set(self, key, value, ex=None):
                shared_backing_store[key] = value

        fake = _SharedFakeRedis()
        monkeypatch.setattr(helpers, "_get_redis_client", lambda: fake)

        monkeypatch.setattr(helpers, "INSTANCE_KEY_PREFIX", "nettracker-main")
        await helpers._redis_cache_write("dmr_cache_42", "data-from-main", 60)

        monkeypatch.setattr(helpers, "INSTANCE_KEY_PREFIX", "nettracker-testing")
        await helpers._redis_cache_write("dmr_cache_42", "data-from-testing", 60)

        # Both land in the same underlying store (simulating one shared
        # Redis database) under different keys, and each instance reads
        # back only its own.
        assert shared_backing_store == {
            "nettracker-main:dmr_cache_42": "data-from-main",
            "nettracker-testing:dmr_cache_42": "data-from-testing",
        }
        assert await helpers._redis_cache_read("dmr_cache_42") == "data-from-testing"
        monkeypatch.setattr(helpers, "INSTANCE_KEY_PREFIX", "nettracker-main")
        assert await helpers._redis_cache_read("dmr_cache_42") == "data-from-main"


class TestRedisCacheHelpers:
    def test_no_redis_client_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(helpers, "REDIS_URL", None)
        monkeypatch.setattr(helpers, "_redis_client", None)
        assert helpers._get_redis_client() is None

    async def test_write_read_round_trip(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(helpers, "_get_redis_client", lambda: fake)
        await helpers._redis_cache_write("k", "v", 60)
        assert await helpers._redis_cache_read("k") == "v"

    async def test_read_missing_key_returns_none(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(helpers, "_get_redis_client", lambda: fake)
        assert await helpers._redis_cache_read("nope") is None

    async def test_write_failure_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(helpers, "_get_redis_client", lambda: _RaisingRedis())
        await helpers._redis_cache_write("k", "v", 60)  # must not raise

    async def test_read_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(helpers, "_get_redis_client", lambda: _RaisingRedis())
        assert await helpers._redis_cache_read("k") is None

    async def test_unconfigured_is_a_pure_noop(self, monkeypatch):
        monkeypatch.setattr(helpers, "REDIS_URL", None)
        monkeypatch.setattr(helpers, "_redis_client", None)
        await helpers._redis_cache_write("k", "v", 60)  # no-op, no error
        assert await helpers._redis_cache_read("k") is None


class TestDmrCacheUsesRedisAcrossWorkers:
    async def test_read_prefers_redis_over_absent_local_dict(self, monkeypatch, db):
        """The actual correctness property this feature exists for: a
        'different worker' (no local in-memory entry for this net) still
        sees a fresh push via Redis, rather than only via the slower
        SystemSetting fallback."""
        _clear_push_caches()
        fake = _FakeRedis()
        monkeypatch.setattr(helpers, "_get_redis_client", lambda: fake)
        try:
            await digital_voice._dmr_cache_write(999, [{"callsign": "W1AW"}], db)
            # Simulate a different worker process -- this one never saw the push.
            digital_voice._dmr_push_cache.clear()
            result = await digital_voice._dmr_cache_read(999, db)
            assert result is not None
            assert result["entries"][0]["callsign"] == "W1AW"
            # Came from Redis specifically, not a DB-fallback repopulate --
            # the dict should still be empty for this net_id.
            assert 999 not in digital_voice._dmr_push_cache
        finally:
            _clear_push_caches()

    async def test_read_falls_back_when_redis_unset(self, monkeypatch, db):
        """Unchanged single-worker behavior when REDIS_URL isn't set --
        still served from the in-memory dict directly."""
        _clear_push_caches()
        monkeypatch.setattr(helpers, "REDIS_URL", None)
        monkeypatch.setattr(helpers, "_redis_client", None)
        try:
            await digital_voice._dmr_cache_write(998, [{"callsign": "W2XYZ"}], db)
            result = await digital_voice._dmr_cache_read(998, db)
            assert result is not None
            assert result["entries"][0]["callsign"] == "W2XYZ"
        finally:
            _clear_push_caches()


class TestAprsCacheUsesRedisAcrossWorkers:
    async def test_read_prefers_redis_over_absent_local_dict(self, monkeypatch, db):
        _clear_push_caches()
        fake = _FakeRedis()
        monkeypatch.setattr(helpers, "_get_redis_client", lambda: fake)
        try:
            await aprs._aprs_cache_write(999, [{"callsign": "W1AW", "lat": 47.6, "lon": -122.3}], db)
            aprs._aprs_push_cache.clear()
            result = await aprs._aprs_cache_read(999, db)
            assert result is not None
            assert result["entries"][0]["callsign"] == "W1AW"
            assert 999 not in aprs._aprs_push_cache
        finally:
            _clear_push_caches()
