"""
v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick win'):
Redis-backed sheet cache layer.

Provides:
- get/set Sheet rows with TTL (5 min default)
- get/set CoachComment with TTL
- get/set JPG card bytes with TTL
- Rate limiting helpers (future use)

Falls back to in-memory dict if Redis is unreachable, so gymbro
stays functional even if Redis goes down.
"""
import sys, time, threading, json
from typing import Optional, Any

# Add system dist-packages to path for redis module
sys.path.insert(0, '/usr/lib/python3/dist-packages')
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class _RedisCache:
    """Thin wrapper around redis with TTL + in-memory fallback."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6379, namespace: str = "gymbro"):
        self.namespace = namespace
        self._mem: dict[str, tuple[float, Any]] = {}
        self._mem_lock = threading.Lock()
        self._client = None
        if REDIS_AVAILABLE:
            try:
                self._client = redis.Redis(
                    host=host, port=port,
                    decode_responses=False,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                )
                # Ping to verify connection
                self._client.ping()
            except Exception as e:
                print(f"[redis_cache] Redis unreachable ({e}); using in-memory fallback")
                self._client = None

    def _key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def get(self, key: str) -> Optional[bytes]:
        """Get value, None if missing or expired. Falls back to memory if Redis down."""
        if self._client:
            try:
                return self._client.get(self._key(key))
            except Exception:
                pass
        # Memory fallback
        with self._mem_lock:
            entry = self._mem.get(key)
            if entry is None:
                return None
            expiry, value = entry
            if expiry and time.time() > expiry:
                del self._mem[key]
                return None
            return value

    def set(self, key: str, value: bytes, ttl_s: int = 300) -> None:
        """Set value with TTL (seconds)."""
        if self._client:
            try:
                self._client.setex(self._key(key), ttl_s, value)
                return
            except Exception:
                pass
        # Memory fallback
        with self._mem_lock:
            expiry = time.time() + ttl_s if ttl_s > 0 else 0
            self._mem[key] = (expiry, value)

    def delete(self, key: str) -> None:
        if self._client:
            try:
                self._client.delete(self._key(key))
            except Exception:
                pass
        with self._mem_lock:
            self._mem.pop(key, None)

    def stats(self) -> dict:
        return {
            "redis_available": REDIS_AVAILABLE,
            "redis_connected": self._client is not None,
            "mem_entries": len(self._mem),
        }


# Singleton
_instance: Optional[_RedisCache] = None
_instance_lock = threading.Lock()


def get_redis_cache() -> _RedisCache:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = _RedisCache()
    return _instance
