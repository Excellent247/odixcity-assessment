import os
import time
import random
import string
import threading
import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError
import redis as redis_lib

# ── Config ─────────────────────────────────────────────────────────────────────
MONGO_URI       = os.getenv("MONGO_URI",  "mongodb://mongo:27017/assessmentdb")
REDIS_HOST      = os.getenv("REDIS_HOST", "redis")
REDIS_PORT      = int(os.getenv("REDIS_PORT", "6379"))
CACHE_TTL       = int(os.getenv("CACHE_TTL", "30"))
APP_PORT        = int(os.getenv("APP_PORT", "8000"))
WRITE_QUEUE_KEY = "api:write:queue"
WRITE_QUEUE_MAX = 500
CACHE_KEY       = "api:data:reads"
CACHE_LOCK      = "api:data:lock"
LOCK_TTL        = 5

# ── FastAPI ────────────────────────────────────────────────────────────────────
app = FastAPI(title="DevOps Assessment API", version="3.0.0")

# ── MongoDB ────────────────────────────────────────────────────────────────────
_mongo_client = None
_col          = None
_mongo_lock   = threading.Lock()


def get_col():
    global _mongo_client, _col
    if _col is not None:
        return _col
    with _mongo_lock:
        if _col is not None:
            return _col
        try:
            _mongo_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=3000,
                maxPoolSize=3,
                minPoolSize=1,
                connectTimeoutMS=3000,
                socketTimeoutMS=10000,
            )
            _mongo_client.admin.command("ping")
            _col = _mongo_client["assessmentdb"]["records"]
            return _col
        except PyMongoError:
            return None


# ── Redis ──────────────────────────────────────────────────────────────────────
_redis: redis_lib.Redis | None = None
_redis_lock = threading.Lock()


def get_redis_cache() -> redis_lib.Redis | None:
    """DB 0 — cache only. Small, protected from write queue pressure."""
    global _redis
    if _redis is not None:
        return _redis
    with _redis_lock:
        if _redis is not None:
            return _redis
        try:
            pool = redis_lib.ConnectionPool(
                host=REDIS_HOST, port=REDIS_PORT, db=0,
                max_connections=50, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
            )
            r = redis_lib.Redis(connection_pool=pool)
            r.ping()
            _redis = r
            return _redis
        except Exception:
            return None


_redis_queue: redis_lib.Redis | None = None
_redis_queue_lock = threading.Lock()


def get_redis_queue() -> redis_lib.Redis | None:
    """DB 1 — write queue only. Isolated from cache eviction."""
    global _redis_queue
    if _redis_queue is not None:
        return _redis_queue
    with _redis_queue_lock:
        if _redis_queue is not None:
            return _redis_queue
        try:
            pool = redis_lib.ConnectionPool(
                host=REDIS_HOST, port=REDIS_PORT, db=1,
                max_connections=50, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
            )
            r = redis_lib.Redis(connection_pool=pool)
            r.ping()
            _redis_queue = r
            return _redis_queue
        except Exception:
            return None


# ── Write queue — push to Redis list, drained by writer pod ───────────────────
def enqueue_writes(docs: list[dict]) -> list[str]:
    r = get_redis_queue()
    ids = []
    for doc in docs:
        if r is not None:
            try:
                if r.llen(WRITE_QUEUE_KEY) < WRITE_QUEUE_MAX:
                    r.rpush(WRITE_QUEUE_KEY, json.dumps(doc, default=str))
                    ids.append("queued")
                else:
                    ids.append("dropped")
            except Exception:
                ids.append("dropped")
        else:
            ids.append("dropped")
    return ids


# ── Cache ──────────────────────────────────────────────────────────────────────
def cache_get() -> list | None:
    r = get_redis_cache()
    if r is None:
        return None
    try:
        val = r.get(CACHE_KEY)
        return json.loads(val) if val else None
    except Exception:
        return None


def cache_set(reads: list) -> None:
    r = get_redis_cache()
    if r is None:
        return
    try:
        r.setex(CACHE_KEY, CACHE_TTL, json.dumps(reads))
    except Exception:
        pass


def cache_lock_acquire() -> bool:
    r = get_redis_cache()
    if r is None:
        return True
    try:
        return r.set(CACHE_LOCK, "1", nx=True, ex=LOCK_TTL) is not None
    except Exception:
        return True


def cache_lock_release() -> None:
    r = get_redis_cache()
    if r is None:
        return
    try:
        r.delete(CACHE_LOCK)
    except Exception:
        pass


# ── Helpers ────────────────────────────────────────────────────────────────────
def random_payload(size: int = 512) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=size))


def build_write_doc(i: int) -> dict:
    return {
        "type":      "write",
        "index":     i,
        "payload":   random_payload(32),   # small in Redis queue; writer can pad if needed
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    for attempt in range(1, 11):
        if get_col() is not None:
            print(f"[mongo] connected on attempt {attempt}")
            break
        print(f"[mongo] attempt {attempt}/10 failed, retrying in 5s...")
        time.sleep(5)
    else:
        print("[mongo] could not connect on startup — will retry on first request")

    if get_redis_cache() is not None:
        print("[redis] cache DB connected")
    else:
        print("[redis] cache DB not available")

    if get_redis_queue() is not None:
        print("[redis] queue DB connected")
    else:
        print("[redis] queue DB not available")


# ── Liveness ───────────────────────────────────────────────────────────────────
@app.get("/healthz")
def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Readiness ──────────────────────────────────────────────────────────────────
@app.get("/readyz")
def readiness_check():
    return {"status": "ready", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Core endpoint ──────────────────────────────────────────────────────────────
@app.get("/api/data")
def process_data():
    ts = datetime.now(timezone.utc).isoformat()

    # Always enqueue 5 writes — go to Redis list, not MongoDB directly
    write_ids = enqueue_writes([build_write_doc(i) for i in range(5)])

    # Serve reads from cache
    cached_reads = cache_get()
    if cached_reads is not None:
        return JSONResponse(content={
            "status":    "success",
            "reads":     cached_reads,
            "writes":    write_ids,
            "timestamp": ts,
            "cached":    True,
        })

    # Cache miss — only one process hits MongoDB to rebuild, others return empty
    if not cache_lock_acquire():
        return JSONResponse(content={
            "status":    "success",
            "reads":     [],
            "writes":    write_ids,
            "timestamp": ts,
            "cached":    False,
        })

    try:
        col = get_col()
        if col is None:
            cache_lock_release()
            raise HTTPException(status_code=503, detail="MongoDB not reachable")

        reads = []
        for _ in range(5):
            doc = col.find_one({"type": "write"}, sort=[("_id", DESCENDING)])
            reads.append(str(doc["_id"]) if doc else None)

        cache_set(reads)
        cache_lock_release()

        return JSONResponse(content={
            "status":    "success",
            "reads":     reads,
            "writes":    write_ids,
            "timestamp": ts,
            "cached":    False,
        })

    except PyMongoError as exc:
        cache_lock_release()
        raise HTTPException(status_code=500, detail=str(exc))


# ── Stats ──────────────────────────────────────────────────────────────────────
@app.get("/api/stats")
def get_stats():
    col = get_col()
    if col is None:
        raise HTTPException(status_code=503, detail="MongoDB not reachable")
    try:
        rq = get_redis_queue()
        queue_depth = rq.llen(WRITE_QUEUE_KEY) if rq else -1
        return {
            "total_documents":   col.count_documents({}),
            "write_queue_depth": queue_depth,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=str(exc))