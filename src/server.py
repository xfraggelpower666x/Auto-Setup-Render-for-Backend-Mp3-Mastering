import os
import uuid
import shutil
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
DB_PATH = DATA_DIR / "jobs.db"

DATA_DIR.mkdir(exist_ok=True)
INPUT_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MASTER_ADMIN_PASSWORD = os.getenv("MASTER_ADMIN_PASSWORD", "")


# ======================
# DB
# ======================
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        filename TEXT,
        output TEXT,
        status TEXT
    )
    """)
    conn.commit()
    conn.close()


init_db()


# ======================
# AUTH
# ======================
def check_admin(request: Request):
    pw = request.headers.get("x-admin-password")
    if pw != MASTER_ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ======================
# ROUTES
# ======================
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/process")
async def process_audio(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form(default="process"),
):
    check_admin(request)

    job_id = str(uuid.uuid4())

    input_path = INPUT_DIR / f"{job_id}_{file.filename}"
    output_path = OUTPUT_DIR / f"{job_id}_out.mp3"

    with open(input_path, "wb") as f:
        f.write(await file.read())

    # fake mastering
    shutil.copy(input_path, output_path)

    conn = db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        (job_id, file.filename, str(output_path), "done"),
    )
    conn.commit()
    conn.close()

    return {"job_id": job_id}


@app.get("/jobs")
def jobs():
    conn = db()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM jobs").fetchall()
    conn.close()

    return rows


@app.get("/download/{job_id}")
def download(job_id: str):
    conn = db()
    c = conn.cursor()
    row = c.execute("SELECT output FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404)

    return FileResponse(row[0])
