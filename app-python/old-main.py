import os
import time
import random
import string
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/assessmentdb")
APP_PORT  = int(os.getenv("APP_PORT", "8000"))

app = FastAPI(
    title="DevOps Assessment API",
    version="1.0.0",
)

# MongoDB connection state
client = None
db     = None
col    = None


def get_col():
    """Return the records collection, connecting if not already connected."""
    global client, db, col
    if col is not None:
        return col
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        db  = client["assessmentdb"]
        col = db["records"]
        return col
    except PyMongoError:
        return None


@app.on_event("startup")
async def startup_event():
    """Attempt MongoDB connection on startup with retries."""
    for attempt in range(1, 11):
        if get_col() is not None:
            print(f"[mongo] connected on attempt {attempt}")
            return
        print(f"[mongo] attempt {attempt}/10 failed, retrying in 5s...")
        time.sleep(5)
    print("[mongo] could not connect on startup — will retry on first request")


def random_payload(size: int = 512) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=size))


# Liveness probe
@app.get("/healthz")
def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# Readiness probe
@app.get("/readyz")
def readiness_check():
    c = get_col()
    if c is None:
        raise HTTPException(status_code=503, detail="MongoDB not reachable")
    try:
        client.admin.command("ping")
        return {"status": "ready", "timestamp": datetime.utcnow().isoformat()}
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# Core endpoint — must perform exactly 5 reads and 5 writes per request
@app.get("/api/data")
def process_data():
    c = get_col()
    if c is None:
        raise HTTPException(status_code=503, detail="MongoDB not reachable")

    reads  = []
    writes = []

    try:
        # 5 writes
        for i in range(5):
            result = c.insert_one({
                "type":      "write",
                "index":     i,
                "payload":   random_payload(),
                "timestamp": datetime.utcnow(),
            })
            writes.append(str(result.inserted_id))

        # 5 reads
        for i in range(5):
            doc = c.find_one({"type": "write"})
            reads.append(str(doc["_id"]) if doc else None)

        return JSONResponse(content={
            "status":    "success",
            "reads":     reads,
            "writes":    writes,
            "timestamp": datetime.utcnow().isoformat(),
        })

    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# Collection stats
@app.get("/api/stats")
def get_stats():
    c = get_col()
    if c is None:
        raise HTTPException(status_code=503, detail="MongoDB not reachable")
    try:
        return {"total_documents": c.count_documents({}), "timestamp": datetime.utcnow().isoformat()}
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
