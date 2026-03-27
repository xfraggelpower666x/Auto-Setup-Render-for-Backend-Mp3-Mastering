import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)
PORT = int(os.getenv("PORT", "10000"))
MASTERING_BACKEND_TOKEN = os.getenv("MASTERING_BACKEND_TOKEN", "").strip()
ENABLE_TOKEN_CHECK = os.getenv("ENABLE_TOKEN_CHECK", "true").lower() == "true"
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
DEFAULT_PRESET = os.getenv("DEFAULT_PRESET", "studio")
DEFAULT_TARGET_LUFS = os.getenv("DEFAULT_TARGET_LUFS", "-8")
DEFAULT_CEILING_DB = os.getenv("DEFAULT_CEILING_DB", "-0.9")
DEFAULT_OUTPUT_FORMAT = os.getenv("DEFAULT_OUTPUT_FORMAT", "mp3")
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_BYTES", str(250 * 1024 * 1024)))
DEFAULT_WIDEN = float(os.getenv("DEFAULT_WIDEN", "1.05"))
DEFAULT_SATURATION = os.getenv("DEFAULT_SATURATION", "soft")

@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "service": "v51-render-mastering-backend",
        "ffmpeg_found": shutil.which(FFMPEG_BIN) is not None,
        "ffprobe_found": shutil.which(FFPROBE_BIN) is not None
    })

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "v51-render-mastering-backend",
        "ffmpeg_found": shutil.which(FFMPEG_BIN) is not None,
        "ffprobe_found": shutil.which(FFPROBE_BIN) is not None
    })

@app.post("/situate")
def situate():
    denied = _auth()
    if denied:
        return denied
    return jsonify({"ok": True, "action": "situate", "received": _safe_json()})

@app.post("/analyze")
def analyze():
    denied = _auth()
    if denied:
        return denied
    if request.content_type and "multipart/form-data" in request.content_type:
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"ok": False, "error": 'missing multipart field "file"'}), 400
        temp_dir = Path(tempfile.mkdtemp(prefix="v51_render_analyze_"))
        try:
            input_path = temp_dir / _safe_filename(file.filename)
            file.save(str(input_path))
            return jsonify({"ok": True, "action": "analyze", "probe": _probe_audio(input_path)})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return jsonify({"ok": True, "action": "analyze", "received": _safe_json()})

@app.post("/master")
def master():
    denied = _auth()
    if denied:
        return denied
    if not request.content_type or "multipart/form-data" not in request.content_type:
        return jsonify({"ok": False, "error": "Content-Type must be multipart/form-data"}), 400
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": 'missing multipart field "file"'}), 400

    preset = (request.form.get("preset") or DEFAULT_PRESET).strip().lower()
    target_lufs = _safe_float(request.form.get("target_lufs"), _safe_float(DEFAULT_TARGET_LUFS, -8.0))
    ceiling_db = _safe_float(request.form.get("ceiling_db"), _safe_float(DEFAULT_CEILING_DB, -0.9))
    output_format = (request.form.get("output_format") or DEFAULT_OUTPUT_FORMAT).strip().lower()
    widen = _safe_float(request.form.get("widen"), DEFAULT_WIDEN)
    saturation = (request.form.get("saturation") or DEFAULT_SATURATION).strip().lower()

    temp_dir = Path(tempfile.mkdtemp(prefix="v51_render_master_"))
    try:
        input_name = _safe_filename(file.filename)
        input_path = temp_dir / input_name
        out_ext = "mp3" if output_format not in {"wav", "flac"} else output_format
        output_name = f"{Path(input_name).stem}_mastered.{out_ext}"
        output_path = temp_dir / output_name
        file.save(str(input_path))

        analysis = _measure_loudnorm(input_path)
        if not analysis.get("ok"):
            return jsonify({"ok": False, "error": "first pass loudnorm failed", "analysis": analysis}), 500

        cmd = _build_master_command(input_path, output_path, preset, target_lufs, ceiling_db, out_ext, widen, saturation, analysis)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            return jsonify({"ok": False, "error": "ffmpeg mastering failed", "stderr": proc.stderr[-5000:], "command": cmd}), 500

        response = send_file(output_path, mimetype=_mime_for_format(out_ext), as_attachment=True, download_name=output_name, max_age=0)
        response.headers["X-Output-Filename"] = output_name
        response.headers["X-Mastering-Preset"] = preset
        response.headers["X-Target-LUFS"] = str(target_lufs)
        response.headers["X-Ceiling-DB"] = str(ceiling_db)
        return response
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def _build_master_command(input_path, output_path, preset, target_lufs, ceiling_db, output_format, widen, saturation, analysis):
    limit_linear = 10 ** (ceiling_db / 20)
    if preset == "club":
        hp, lp = 28, 18000
        comp = "mcompand=0.005,0.1 6:-90/-70,-70/-32,-15/-9,0/-3 6:6:-90:0.2"
        eq = "equalizer=f=65:t=q:w=1.0:g=1.4,equalizer=f=9500:t=q:w=1.0:g=1.1"
        widen = max(widen, 1.08)
    elif preset == "clean":
        hp, lp = 24, 17500
        comp = "mcompand=0.01,0.12 6:-90/-70,-70/-38,-15/-8,0/-3 4:4:-90:0.3"
        eq = "equalizer=f=85:t=q:w=1.0:g=0.8,equalizer=f=2600:t=q:w=1.0:g=0.5"
        widen = min(widen, 1.03)
    else:
        hp, lp = 26, 18000
        comp = "mcompand=0.006,0.10 6:-90/-70,-70/-35,-16/-8,0/-3 5:5:-90:0.25"
        eq = "equalizer=f=72:t=q:w=1.0:g=1.0,equalizer=f=8200:t=q:w=1.0:g=0.8"
    sat = {"soft":"asoftclip=type=tanh:oversample=4","hard":"asoftclip=type=cubic:oversample=4","none":"anull"}.get(saturation, "asoftclip=type=tanh:oversample=4")
    loud = f"loudnorm=I={target_lufs}:TP={ceiling_db}:LRA=7:measured_I={analysis['input_i']}:measured_TP={analysis['input_tp']}:measured_LRA={analysis['input_lra']}:measured_thresh={analysis['input_thresh']}:offset={analysis['target_offset']}:linear=true:print_format=summary"
    filters = ",".join([f"highpass=f={hp}", f"lowpass=f={lp}", comp, eq, f"extrastereo=m={widen}", sat, loud, f"alimiter=limit={limit_linear}:level=disabled"])
    cmd = [FFMPEG_BIN, "-y", "-i", str(input_path), "-vn", "-af", filters]
    if output_format == "wav":
        cmd += ["-c:a", "pcm_s24le", "-ar", "48000", str(output_path)]
    elif output_format == "flac":
        cmd += ["-c:a", "flac", str(output_path)]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", "320k", str(output_path)]
    return cmd

def _measure_loudnorm(input_path):
    cmd = [FFMPEG_BIN, "-hide_banner", "-i", str(input_path), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    text = proc.stderr or ""
    m = re.search(r"\{\s*\"input_i\".*?\}", text, flags=re.S)
    if proc.returncode != 0 or not m:
        return {"ok": False, "stderr": text[-5000:]}
    payload = json.loads(m.group(0))
    payload["ok"] = True
    return payload

def _probe_audio(input_path):
    cmd = [FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(input_path)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return {"ok": False, "error": "ffprobe failed", "stderr": proc.stderr[-2000:]}
    return json.loads(proc.stdout)

def _safe_filename(value):
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value) or "upload.bin"

def _safe_float(value, fallback):
    try:
        return float(value)
    except Exception:
        return fallback

def _safe_json():
    try:
        return request.get_json(force=True, silent=True) or {}
    except Exception:
        return {}

def _uploaded_size(file_storage):
    pos = file_storage.stream.tell()
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(pos)
    return size

def _mime_for_format(fmt):
    return {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac"}.get(fmt, "application/octet-stream")

def _auth():
    if not ENABLE_TOKEN_CHECK:
        return None
    auth = (request.headers.get("Authorization") or "").strip()
    expected = f"Bearer {MASTERING_BACKEND_TOKEN}" if MASTERING_BACKEND_TOKEN else ""
    if not MASTERING_BACKEND_TOKEN or auth != expected:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)