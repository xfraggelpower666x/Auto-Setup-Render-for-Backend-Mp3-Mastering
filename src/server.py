import os
import json
import uuid
import shutil
import sqlite3
import datetime
import subprocess
from pathlib import Path
from typing import Optional, Any, Dict

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

APP_NAME = "666 audio processing backend"
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
DB_PATH = DATA_DIR / "audio_system.sqlite3"

DATA_DIR.mkdir(parents=True, exist_ok=True)
INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MASTER_ADMIN_PASSWORD = os.getenv("MASTER_ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MASTERING_BACKEND_TOKEN = os.getenv("MASTERING_BACKEND_TOKEN", "")

app = FastAPI(title=APP_NAME)

# ============================================================
# DATABASE
# ============================================================

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_uuid TEXT UNIQUE,
        source_filename TEXT,
        source_path TEXT,
        output_filename TEXT,
        output_path TEXT,
        status TEXT,
        mode TEXT,
        created_at TEXT,
        updated_at TEXT,
        error TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS audio_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        duration_seconds REAL,
        sample_rate INTEGER,
        channels INTEGER,
        bit_rate INTEGER,
        peak_db REAL,
        input_i REAL,
        input_tp REAL,
        input_lra REAL,
        input_thresh REAL,
        normalization_type TEXT,
        target_i REAL,
        target_tp REAL,
        notes TEXT,
        created_at TEXT,
        FOREIGN KEY(job_id) REFERENCES jobs(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS metadata (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        title TEXT,
        artist TEXT,
        album TEXT,
        year TEXT,
        genre TEXT,
        comment TEXT,
        album_artist TEXT,
        composer TEXT,
        track TEXT,
        disc TEXT,
        cover_used TEXT,
        created_at TEXT,
        FOREIGN KEY(job_id) REFERENCES jobs(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        transcript_text TEXT,
        transcript_json TEXT,
        created_at TEXT,
        FOREIGN KEY(job_id) REFERENCES jobs(id)
    )
    """)

    conn.commit()
    conn.close()

def now_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"

# ============================================================
# AUTH
# ============================================================

def check_admin(request: Request):
    """
    Central architecture:
      Client -> system-pw-worker -> validation -> allow/deny
      system-pw-worker -> mp3-mastering-worker -> backend

    For direct Postman/backend testing, this backend also accepts the same
    x-admin-password header against MASTER_ADMIN_PASSWORD.
    """
    header_pw = request.headers.get("x-admin-password", "")
    if not MASTER_ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="MASTER_ADMIN_PASSWORD / ADMIN_PASSWORD not configured")
    if header_pw != MASTER_ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="unauthorized")

# ============================================================
# MODELS
# ============================================================

class ProcessRequest(BaseModel):
    filename: str = "test.mp3"
    mode: str = "process"
    title: Optional[str] = None
    artist: Optional[str] = "xXXx_FRAGGLE_xXXx"
    album: Optional[str] = None
    year: Optional[str] = None
    genre: Optional[str] = "PSYTRANCE - TECHNO"
    comment: Optional[str] = None
    album_artist: Optional[str] = "xXXx_FRAGGLE_xXXx"
    composer: Optional[str] = "FRAGGLEPOWER666"
    track: Optional[str] = "666"
    disc: Optional[str] = "666"

# ============================================================
# UTILS
# ============================================================

def ffprobe_json(path: Path) -> Dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,bit_rate:stream=index,codec_type,sample_rate,channels",
        "-of", "json", str(path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}

def analyze_loudnorm(path: Path) -> Dict[str, Any]:
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(path),
        "-af", "loudnorm=I=-9:TP=-1.0:LRA=7:print_format=json",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    start = text.rfind("{")
    end = text.rfind("}")
    payload = {}
    if start != -1 and end != -1 and end > start:
        try:
            payload = json.loads(text[start:end+1])
        except Exception:
            payload = {}

    probe = ffprobe_json(path)
    format_info = probe.get("format", {}) if isinstance(probe, dict) else {}
    streams = probe.get("streams", []) if isinstance(probe, dict) else []

    audio_stream = None
    for s in streams:
        if s.get("codec_type") == "audio":
            audio_stream = s
            break

    def num(v):
        try:
            return float(v)
        except Exception:
            return None

    return {
        "duration_seconds": num(format_info.get("duration")),
        "bit_rate": int(format_info.get("bit_rate", 0) or 0),
        "sample_rate": int(audio_stream.get("sample_rate", 0) or 0) if audio_stream else 0,
        "channels": int(audio_stream.get("channels", 0) or 0) if audio_stream else 0,
        "peak_db": None,
        "input_i": num(payload.get("input_i")),
        "input_tp": num(payload.get("input_tp")),
        "input_lra": num(payload.get("input_lra")),
        "input_thresh": num(payload.get("input_thresh")),
        "target_i": -9.0,
        "target_tp": -1.0,
        "normalization_type": "adaptive_club_48k_320k",
        "notes": "analyze-first pipeline; protect tails; no blind maximize"
    }

def choose_target_i(analysis: Dict[str, Any], mode: str) -> float:
    if mode == "master":
        return -9.0
    if mode == "transcribe":
        return -14.0
    input_i = analysis.get("input_i")
    try:
        input_i = float(input_i) if input_i is not None else None
    except Exception:
        input_i = None
    if input_i is None:
        return -9.0
    if input_i >= -8.5:
        return -8.5
    if input_i <= -18:
        return -10.0
    return -9.0

def year_now():
    return str(datetime.datetime.now().year)

def default_metadata(source_filename: str, payload: Optional[ProcessRequest] = None) -> Dict[str, str]:
    title = Path(source_filename).stem
    y = year_now()
    meta = {
        "title": title,
        "artist": "xXXx_FRAGGLE_xXXx",
        "album": f"666SOUNDsDESIGn {y}",
        "year": y,
        "genre": "PSYTRANCE - TECHNO",
        "comment": f"xXXx_FRAGGLE_xXXx - FRAGGLEPOWER666 - 666SOUNDsDESIGn {y}",
        "album_artist": "xXXx_FRAGGLE_xXXx",
        "composer": "FRAGGLEPOWER666",
        "track": "666",
        "disc": "666",
    }
    if payload:
        for k in list(meta.keys()):
            v = getattr(payload, k, None)
            if v not in [None, ""]:
                meta[k] = str(v)
    return meta

def transcribe_stub(path: Path) -> Dict[str, Any]:
    return {
        "text": f"[stub transcript] transcription not wired yet for {path.name}. Add OpenAI call with OPENAI_API_KEY.",
        "json": {"stub": True, "filename": path.name}
    }

def write_metadata_row(job_id: int, metadata: Dict[str, str], cover_used: Optional[str] = None):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO metadata
        (job_id, title, artist, album, year, genre, comment, album_artist, composer, track, disc, cover_used, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_id,
        metadata.get("title"),
        metadata.get("artist"),
        metadata.get("album"),
        metadata.get("year"),
        metadata.get("genre"),
        metadata.get("comment"),
        metadata.get("album_artist"),
        metadata.get("composer"),
        metadata.get("track"),
        metadata.get("disc"),
        cover_used,
        now_iso()
    ))
    conn.commit()
    conn.close()

def write_analysis(job_id: int, analysis: Dict[str, Any]):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO audio_analysis
        (job_id, duration_seconds, sample_rate, channels, bit_rate, peak_db, input_i, input_tp, input_lra, input_thresh,
         normalization_type, target_i, target_tp, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_id,
        analysis.get("duration_seconds"),
        analysis.get("sample_rate"),
        analysis.get("channels"),
        analysis.get("bit_rate"),
        analysis.get("peak_db"),
        analysis.get("input_i"),
        analysis.get("input_tp"),
        analysis.get("input_lra"),
        analysis.get("input_thresh"),
        analysis.get("normalization_type"),
        analysis.get("target_i"),
        analysis.get("target_tp"),
        analysis.get("notes"),
        now_iso()
    ))
    conn.commit()
    conn.close()

def write_transcript(job_id: int, transcript: Dict[str, Any]):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO transcripts (job_id, transcript_text, transcript_json, created_at)
        VALUES (?, ?, ?, ?)
    """, (job_id, transcript.get("text"), json.dumps(transcript.get("json", {})), now_iso()))
    conn.commit()
    conn.close()

def create_job(source_filename: str, source_path: str, mode: str) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO jobs (job_uuid, source_filename, source_path, status, mode, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), source_filename, source_path, "processing", mode, now_iso(), now_iso()))
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id

def update_job(job_id: int, **kwargs):
    if not kwargs:
        return
    kwargs["updated_at"] = now_iso()
    keys = list(kwargs.keys())
    assignments = ", ".join(f"{k}=?" for k in keys)
    values = [kwargs[k] for k in keys] + [job_id]
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE jobs SET {assignments} WHERE id=?", values)
    conn.commit()
    conn.close()

def master_audio(input_path: Path, output_path: Path, metadata: Dict[str, str]):
    analysis = analyze_loudnorm(input_path)
    target_i = choose_target_i(analysis, "process")
    analysis["target_i"] = target_i

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-i", str(input_path),
        "-af", f"loudnorm=I={target_i}:TP=-1.0:LRA=7",
        "-ar", "48000",
        "-b:a", "320k",
        "-id3v2_version", "3",
        "-metadata", f"title={metadata.get('title','')}",
        "-metadata", f"artist={metadata.get('artist','')}",
        "-metadata", f"album={metadata.get('album','')}",
        "-metadata", f"genre={metadata.get('genre','')}",
        "-metadata", f"date={metadata.get('year','')}",
        "-metadata", f"comment={metadata.get('comment','')}",
        "-metadata", f"album_artist={metadata.get('album_artist','')}",
        "-metadata", f"composer={metadata.get('composer','')}",
        "-metadata", f"track={metadata.get('track','')}",
        "-metadata", f"disc={metadata.get('disc','')}",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "ffmpeg mastering failed")
    return analysis

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    return {
        "ok": True,
        "service": "audio-only-mp3-mastering-backend",
        "ffmpeg_bin": ffmpeg_bin,
        "ffmpeg_found": bool(ffmpeg_bin),
        "ffprobe_bin": ffprobe_bin,
        "ffprobe_found": bool(ffprobe_bin),
    }

@app.post("/process")
async def process_audio(
    request: Request,
    file: Optional[UploadFile] = File(default=None),
    mode: Optional[str] = Form(default="process"),
):
    check_admin(request)

    payload = None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        payload = ProcessRequest(**data)
        source_filename = payload.filename
        dummy_path = INPUT_DIR / source_filename
        if not dummy_path.exists():
            dummy_path.write_bytes(b"")
        source_path = dummy_path
    else:
        if file is None:
            raise HTTPException(status_code=400, detail="file upload or JSON payload required")
        source_filename = file.filename or "upload.mp3"
        source_path = INPUT_DIR / source_filename
        with open(source_path, "wb") as f:
            f.write(await file.read())
        payload = ProcessRequest(filename=source_filename, mode=mode or "process")

    metadata = default_metadata(source_filename, payload)
    job_id = create_job(source_filename=source_filename, source_path=str(source_path), mode=payload.mode)

    try:
        output_filename = f"{Path(source_filename).stem}_mastered.mp3"
        output_path = OUTPUT_DIR / output_filename

        analysis = {}
        if payload.mode in ("master", "process") and source_path.exists() and source_path.stat().st_size > 0:
            analysis = master_audio(source_path, output_path, metadata)
        else:
            output_path = source_path
            analysis = {
                "duration_seconds": None,
                "sample_rate": 48000,
                "channels": None,
                "bit_rate": 320000,
                "peak_db": None,
                "input_i": None,
                "input_tp": None,
                "input_lra": None,
                "input_thresh": None,
                "normalization_type": "queue_or_json_test_mode",
                "target_i": -9.0,
                "target_tp": -1.0,
                "notes": "No binary audio supplied; API test mode only."
            }

        write_analysis(job_id, analysis)
        write_metadata_row(job_id, metadata, cover_used=None)

        transcript = None
        if payload.mode in ("transcribe", "process"):
            transcript = transcribe_stub(output_path if output_path.exists() else source_path)
            write_transcript(job_id, transcript)

        update_job(
            job_id,
            status="done",
            output_filename=output_filename if output_path else None,
            output_path=str(output_path) if output_path else None,
            error=None
        )

        return {
            "ok": True,
            "job_id": job_id,
            "status": "done",
            "mode": payload.mode,
            "source_filename": source_filename,
            "mastered_file_url": f"/download/{output_filename}" if output_path and output_path.exists() else None,
            "analysis": analysis,
            "metadata": metadata,
            "transcript_text": (transcript or {}).get("text"),
            "transcript_json": (transcript or {}).get("json"),
            "note": "When called with JSON only, this route performs queue/db testing without real audio mastering."
        }
    except Exception as e:
        update_job(job_id, status="error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/jobs")
def list_jobs(request: Request):
    check_admin(request)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"ok": True, "jobs": rows}

@app.get("/job/{job_id}")
def get_job(job_id: int, request: Request):
    check_admin(request)
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    job = cur.fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="job not found")

    cur.execute("SELECT * FROM audio_analysis WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
    analysis = cur.fetchone()

    cur.execute("SELECT * FROM metadata WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
    meta = cur.fetchone()

    cur.execute("SELECT * FROM transcripts WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
    transcript = cur.fetchone()

    conn.close()

    return {
        "ok": True,
        "job": dict(job),
        "analysis": dict(analysis) if analysis else None,
        "metadata": dict(meta) if meta else None,
        "transcript": dict(transcript) if transcript else None,
    }

@app.delete("/job/{job_id}")
def delete_job(job_id: int, request: Request):
    check_admin(request)
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    job = cur.fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="job not found")

    output_path = job["output_path"]
    source_path = job["source_path"]
    for p in [output_path, source_path]:
        try:
            if p and Path(p).exists() and Path(p).is_file():
                Path(p).unlink()
        except Exception:
            pass

    cur.execute("DELETE FROM transcripts WHERE job_id=?", (job_id,))
    cur.execute("DELETE FROM metadata WHERE job_id=?", (job_id,))
    cur.execute("DELETE FROM audio_analysis WHERE job_id=?", (job_id,))
    cur.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()
    conn.close()

    return {"ok": True, "deleted_job_id": job_id}

@app.get("/search")
def search(request: Request, q: str = ""):
    check_admin(request)
    like = f"%{q}%"
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT j.*
        FROM jobs j
        LEFT JOIN metadata m ON j.id = m.job_id
        LEFT JOIN transcripts t ON j.id = t.job_id
        WHERE j.source_filename LIKE ?
           OR j.status LIKE ?
           OR j.mode LIKE ?
           OR IFNULL(m.title,'') LIKE ?
           OR IFNULL(m.artist,'') LIKE ?
           OR IFNULL(m.album,'') LIKE ?
           OR IFNULL(m.genre,'') LIKE ?
           OR IFNULL(m.comment,'') LIKE ?
           OR IFNULL(t.transcript_text,'') LIKE ?
        ORDER BY j.id DESC
    """, (like, like, like, like, like, like, like, like, like))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"ok": True, "query": q, "results": rows}

@app.get("/download/{filename}")
def download(filename: str, request: Request):
    check_admin(request)
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, filename=filename, media_type="audio/mpeg")
