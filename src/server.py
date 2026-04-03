import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse

APP_NAME = "audio-only-mp3-mastering-backend"

MASTER_ADMIN_PASSWORD = os.getenv("MASTER_ADMIN_PASSWORD", "")

app = FastAPI(title=APP_NAME)


# ======================
# AUTH
# ======================
def check_admin(request: Request):
    pw = request.headers.get("x-admin-password")
    if not MASTER_ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="MASTER_ADMIN_PASSWORD not configured")
    if pw != MASTER_ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ======================
# HEALTH
# ======================
@app.get("/health")
def health():
    ffmpeg_found = shutil_which("ffmpeg") is not None
    ffprobe_found = shutil_which("ffprobe") is not None
    return {
        "ok": True,
        "service": APP_NAME,
        "ffmpeg_found": ffmpeg_found,
        "ffprobe_found": ffprobe_found,
    }


def shutil_which(binary_name: str) -> Optional[str]:
    from shutil import which
    return which(binary_name)


# ======================
# MASTERING
# ======================
@app.post("/process")
async def process_audio(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form(default="process"),
):
    check_admin(request)

    # mode bleibt drin für spätere Kompatibilität
    # aktuell wird hier nur Audio-Processing / Mastering behandelt
    original_name = file.filename or "upload.mp3"
    safe_stem = Path(original_name).stem
    output_name = f"{safe_stem}_mastered.mp3"

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_path = tmpdir_path / original_name
            output_path = tmpdir_path / output_name

            # Upload speichern
            content = await file.read()
            with open(input_path, "wb") as f:
                f.write(content)

            # Einfaches Mastering / Loudness-Normalisierung
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(input_path),
                "-af", "loudnorm=I=-9:TP=-1.0:LRA=7",
                "-ar", "48000",
                "-b:a", "320k",
                str(output_path),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"ffmpeg failed: {result.stderr}"
                )

            if not output_path.exists():
                raise HTTPException(
                    status_code=500,
                    detail="Mastered output file was not created"
                )

            return FileResponse(
                path=output_path,
                media_type="audio/mpeg",
                filename=output_name,
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
