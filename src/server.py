from flask import Flask, request, jsonify, send_file
import os
import tempfile
import subprocess
import shutil
from pathlib import Path

app = Flask(__name__)

@app.route("/")
def home():
    return {"ok": True, "service": "v51-backend"}

@app.route("/health")
def health():
    return {"ok": True}

def check_auth():
    token = os.getenv("MASTERING_BACKEND_TOKEN", "")
    header = request.headers.get("Authorization", "")
    if not token:
        return True
    return header == f"Bearer {token}"

@app.route("/master", methods=["POST"])
def master():
    if not check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400

    file = request.files["file"]

    temp_dir = Path(tempfile.mkdtemp())
    try:
        input_path = temp_dir / "input.mp3"
        output_path = temp_dir / "output.mp3"

        file.save(input_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-af", "loudnorm=I=-8:TP=-0.9:LRA=7",
            "-b:a", "320k",
            str(output_path)
        ]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if result.returncode != 0:
            return jsonify({
                "ok": False,
                "error": "ffmpeg failed",
                "stderr": result.stderr.decode(errors="ignore")
            }), 500

        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="mastered.mp3"
        )

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.route("/analyze", methods=["POST"])
def analyze():
    if not check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    return jsonify({
        "ok": True,
        "message": "analyze placeholder"
    })

@app.route("/situate", methods=["POST"])
def situate():
    if not check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    return jsonify({
        "ok": True,
        "message": "situate placeholder"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)