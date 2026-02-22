"""
writer.py — standalone MongoDB write worker.

Runs as a single-replica Deployment. Drains the Redis write queue (DB 1)
and bulk-inserts into MongoDB at a controlled, predictable rate.

Reconnects to both Redis and MongoDB after failures — never caches a dead
connection.
"""

import os
import json
import time

from pymongo import MongoClient
from pymongo.errors import PyMongoError
import redis

MONGO_URI       = os.getenv("MONGO_URI",             "mongodb://mongo:27017/assessmentdb")
REDIS_HOST      = os.getenv("REDIS_HOST",            "redis")
REDIS_PORT      = int(os.getenv("REDIS_PORT",        "6379"))
BATCH_SIZE      = int(os.getenv("WRITE_BATCH_SIZE",  "25"))
FLUSH_INTERVAL  = float(os.getenv("WRITE_FLUSH_INTERVAL", "3.0"))
WRITE_QUEUE_KEY = "api:write:queue"
REDIS_DB        = 1   # queue DB — isolated from cache (DB 0)


def make_mongo():
    """Create a fresh MongoClient. Returns (client, col) or (None, None)."""
    try:
        client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=2,
            minPoolSize=1,
        )
        client.admin.command("ping")
        col = client["assessmentdb"]["records"]
        print("[writer] MongoDB connected")
        return client, col
    except Exception as e:
        print(f"[writer] MongoDB connection failed: {e}")
        return None, None


def make_redis():
    """Create a fresh Redis connection. Returns client or None."""
    try:
        r = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        r.ping()
        print("[writer] Redis connected (db=1)")
        return r
    except Exception as e:
        print(f"[writer] Redis connection failed: {e}")
        return None


def main():
    mongo_client, col = None, None
    r = None

    # Initial connections with retry
    while col is None:
        mongo_client, col = make_mongo()
        if col is None:
            time.sleep(5)

    while r is None:
        r = make_redis()
        if r is None:
            time.sleep(5)

    print(f"[writer] running — batch={BATCH_SIZE} interval={FLUSH_INTERVAL}s")

    while True:
        # ── Reconnect Redis if needed ──────────────────────────────────────
        if r is None:
            r = make_redis()
            if r is None:
                time.sleep(5)
                continue

        # ── Pop batch from queue ───────────────────────────────────────────
        try:
            pipe = r.pipeline()
            for _ in range(BATCH_SIZE):
                pipe.lpop(WRITE_QUEUE_KEY)
            results = pipe.execute()
            batch = [json.loads(item) for item in results if item is not None]
        except Exception as e:
            print(f"[writer] Redis error: {e} — reconnecting")
            r = None
            time.sleep(2)
            continue

        if not batch:
            time.sleep(FLUSH_INTERVAL)
            continue

        # ── Reconnect MongoDB if needed ────────────────────────────────────
        if col is None:
            mongo_client, col = make_mongo()
            if col is None:
                # Re-queue the batch so docs aren't lost
                try:
                    for doc in batch:
                        r.lpush(WRITE_QUEUE_KEY, json.dumps(doc))
                except Exception:
                    pass
                time.sleep(5)
                continue

        # ── Write to MongoDB ───────────────────────────────────────────────
        try:
            col.insert_many(batch, ordered=False)
            depth = r.llen(WRITE_QUEUE_KEY) if r else -1
            print(f"[writer] flushed {len(batch)} docs — queue depth: {depth}")
        except PyMongoError as e:
            print(f"[writer] insert_many failed: {e} — backing off 10s")
            try:
                mongo_client.close()
            except Exception:
                pass
            mongo_client, col = None, None
            # Re-queue the batch
            try:
                for doc in batch:
                    r.lpush(WRITE_QUEUE_KEY, json.dumps(doc))
            except Exception:
                pass
            time.sleep(10)  # longer backoff — give mongo time to recover fully
            continue

        time.sleep(FLUSH_INTERVAL)


if __name__ == "__main__":
    main()