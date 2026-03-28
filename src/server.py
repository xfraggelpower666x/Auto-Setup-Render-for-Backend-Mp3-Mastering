from __future__ import annotations

from flask import Flask, request, jsonify, send_file
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

app = Flask(__name__)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def get_cfg() -> dict[str, Any]:
    return {
        "service": os.getenv("SERVICE_NAME", "audio-only-mp3-mastering-backend"),
        "enable_token_check": env_bool("ENABLE_TOKEN_CHECK", True),
        "token": os.getenv("MASTERING_BACKEND_TOKEN", "").strip(),
        "default_preset": os.getenv("DEFAULT_PRESET", "club").strip().lower(),
        "default_target_lufs": safe_float(os.getenv("DEFAULT_TARGET_LUFS", "-8"), -8.0),
        "default_ceiling_db": safe_float(os.getenv("DEFAULT_CEILING_DB", "-0.8"), -0.8),
        "default_output_format": os.getenv("DEFAULT_OUTPUT_FORMAT", "mp3").strip().lower(),
        "default_widen": safe_float(os.getenv("DEFAULT_WIDEN", "1.05"), 1.05),
        "default_saturation": os.getenv("DEFAULT_SATURATION", "soft").strip().lower(),
        "ffmpeg_bin": os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg",
        "ffprobe_bin": os.getenv("FFPROBE_BIN", "ffprobe").strip() or "ffprobe",
        "max_file_size_bytes": safe_int(os.getenv("MAX_FILE_SIZE_BYTES", str(250 * 1024 * 1024)), 250 * 1024 * 1024),
    }


def ffmpeg_exists(cfg: dict[str, Any]) -> bool:
    return shutil.which(cfg["ffmpeg_bin"]) is not None


def ffprobe_exists(cfg: dict[str, Any]) -> bool:
    return shutil.which(cfg["ffprobe_bin"]) is not None


def check_auth() -> bool:
    cfg = get_cfg()
    if not cfg["enable_token_check"]:
        return True
    token = cfg["token"]
    if not token:
        return False
    header = request.headers.get("Authorization", "")
    return header == f"Bearer {token}"


def file_ext_from_format(fmt: str) -> str:
    return "wav" if str(fmt).lower() == "wav" else "mp3"


def mime_from_format(fmt: str) -> str:
    return "audio/wav" if str(fmt).lower() == "wav" else "audio/mpeg"


def sanitize_name(value: str) -> str:
    keep = []
    for ch in (value or "mastered"):
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    out = "".join(keep).strip("._-")
    return (out or "mastered")[:120]


def preset_config(input_data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    preset = str(input_data.get("preset") or cfg["default_preset"] or "club").lower()
    mode = str(input_data.get("mode") or "standard").lower()
    genre = str(input_data.get("genre") or "").lower()

    presets = {
        "club": {"target_lufs": -8.0, "ceiling_db": -0.8, "gain_db": 1.4, "lra": 7.0},
        "streaming": {"target_lufs": -10.0, "ceiling_db": -1.0, "gain_db": 0.7, "lra": 8.0},
        "loud": {"target_lufs": -7.0, "ceiling_db": -0.6, "gain_db": 1.8, "lra": 6.0},
        "transparent": {"target_lufs": -9.0, "ceiling_db": -1.0, "gain_db": 0.4, "lra": 9.0},
        "studio": {"target_lufs": cfg["default_target_lufs"], "ceiling_db": cfg["default_ceiling_db"], "gain_db": 1.0, "lra": 7.0},
    }
    genre_adjustments = {
        "techno": {"gain_db": 1.2},
        "edm": {"gain_db": 1.0},
        "house": {"gain_db": 0.8},
        "hiphop": {"gain_db": 0.6},
        "rap": {"gain_db": 0.6},
        "pop": {"gain_db": 0.4},
        "rock": {"gain_db": 0.2},
    }

    chosen = dict(presets.get(preset, presets["club"]))
    if genre and genre in genre_adjustments:
        chosen.update(genre_adjustments[genre])
    if mode == "safe":
        chosen["target_lufs"] -= 0.5
        chosen["ceiling_db"] = min(chosen["ceiling_db"], -1.0)
    if mode == "aggressive":
        chosen["target_lufs"] += 0.4
        chosen["gain_db"] += 0.4

    chosen["target_lufs"] = safe_float(input_data.get("target_lufs"), chosen["target_lufs"])
    chosen["ceiling_db"] = safe_float(input_data.get("ceiling_db"), chosen["ceiling_db"])
    chosen["output_format"] = file_ext_from_format(str(input_data.get("output_format") or cfg["default_output_format"]))
    chosen["widen"] = safe_float(input_data.get("widen"), cfg["default_widen"])
    chosen["saturation"] = str(input_data.get("saturation") or cfg["default_saturation"]).lower()
    chosen["preset"] = preset
    chosen["mode"] = mode
    chosen["genre"] = genre
    return chosen


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ffprobe_json(path: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    result = run_cmd([cfg["ffprobe_bin"], "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "ffprobe failed")
    return json.loads(result.stdout or "{}")


def parse_loudnorm_json(stderr_text: str) -> dict[str, Any] | None:
    import re
    match = re.search(r'\{\s*"input_i"[\s\S]*?\}', stderr_text, re.M)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def parse_volumedetect(stderr_text: str) -> dict[str, Any]:
    import re
    mean_m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", stderr_text, re.I)
    max_m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", stderr_text, re.I)
    return {
        "rms_db": float(mean_m.group(1)) if mean_m else None,
        "peak_db": float(max_m.group(1)) if max_m else None,
    }


def measure_loudnorm(path: Path, cfg: dict[str, Any], pcfg: dict[str, Any]) -> dict[str, Any] | None:
    result = run_cmd([
        cfg["ffmpeg_bin"], "-y", "-i", str(path),
        "-af", f'loudnorm=I={pcfg["target_lufs"]}:TP={pcfg["ceiling_db"]}:LRA={pcfg["lra"]}:print_format=json',
        "-f", "null", "-",
    ])
    if result.returncode != 0:
        return None
    return parse_loudnorm_json(result.stderr)


def measure_volumedetect(path: Path, cfg: dict[str, Any]) -> dict[str, Any] | None:
    result = run_cmd([cfg["ffmpeg_bin"], "-y", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    if result.returncode != 0:
        return None
    return parse_volumedetect(result.stderr)


def build_master_filter(pcfg: dict[str, Any], measured: dict[str, Any] | None) -> str:
    stages = []
    widen = pcfg.get("widen", 1.0)
    if widen and widen > 1.0:
        mix = min(1.0, max(0.0, (widen - 1.0) * 0.5))
        stages.append(f"extrastereo=m={mix:.3f}")

    sat = str(pcfg.get("saturation") or "soft")
    if sat == "soft":
        stages.append("acompressor=threshold=-16dB:ratio=2:attack=20:release=180:makeup=1")
    elif sat == "medium":
        stages.append("acompressor=threshold=-18dB:ratio=2.5:attack=15:release=150:makeup=1.5")
    elif sat == "hard":
        stages.append("acompressor=threshold=-20dB:ratio=3:attack=10:release=120:makeup=2")

    if measured:
        stages.append(
            "loudnorm="
            f'I={pcfg["target_lufs"]}:TP={pcfg["ceiling_db"]}:LRA={pcfg["lra"]}:'
            f'measured_I={measured.get("input_i", "-23.0")}:measured_LRA={measured.get("input_lra", "7.0")}:'
            f'measured_TP={measured.get("input_tp", "-2.0")}:measured_thresh={measured.get("input_thresh", "-34.0")}:'
            f'offset={measured.get("target_offset", "0.0")}:linear=true:print_format=summary'
        )
    else:
        stages.append(f'loudnorm=I={pcfg["target_lufs"]}:TP={pcfg["ceiling_db"]}:LRA={pcfg["lra"]}')

    stages.append("alimiter=limit=-0.1dB")
    return ",".join(stages)


def download_to_path(url: str, destination: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "audio-only-backend/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(destination, "wb") as f:
        shutil.copyfileobj(resp, f)


def save_input_file(temp_dir: Path) -> tuple[Path, str]:
    if "file" in request.files and request.files["file"] and request.files["file"].filename:
        file = request.files["file"]
        suffix = Path(file.filename).suffix or ".bin"
        input_path = temp_dir / f"input{suffix}"
        file.save(input_path)
        return input_path, file.filename

    file_url = request.form.get("file_url") or request.json.get("file_url") if request.is_json else None
    if file_url:
        parsed = urllib.parse.urlparse(file_url)
        suffix = Path(parsed.path).suffix or ".bin"
        input_path = temp_dir / f"input{suffix}"
        download_to_path(file_url, input_path)
        return input_path, Path(parsed.path).name or "input.bin"

    input_path_raw = request.form.get("input_path") or request.json.get("input_path") if request.is_json else None
    if input_path_raw:
        source = Path(input_path_raw)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError("input_path not found")
        suffix = source.suffix or ".bin"
        input_path = temp_dir / f"input{suffix}"
        shutil.copy2(source, input_path)
        return input_path, source.name

    raise ValueError("no file")


def summarize_recommendations(loudness: dict[str, Any], volume: dict[str, Any], pcfg: dict[str, Any]) -> list[str]:
    recs: list[str] = []
    integrated = loudness.get("integrated_lufs")
    true_peak = loudness.get("true_peak_db")
    if integrated is not None:
        delta = pcfg["target_lufs"] - integrated
        if delta > 1.0:
            recs.append("Track is quieter than target. More loudness headroom is available.")
        elif delta < -1.0:
            recs.append("Track is already hotter than target. Use safe or transparent mode.")
    if true_peak is not None and true_peak > -0.5:
        recs.append("True peak is high. Keep ceiling at or below -0.8 dBTP.")
    if volume.get("peak_db") is not None and volume["peak_db"] > -0.2:
        recs.append("Peak level is very close to 0 dBFS. Limiting will be required.")
    if not recs:
        recs.append("Material looks workable for club-style mastering with current defaults.")
    return recs


@app.route("/")
def home():
    cfg = get_cfg()
    return {
        "ok": True,
        "service": cfg["service"],
        "auth_enabled": cfg["enable_token_check"],
        "default_output_format": cfg["default_output_format"],
    }


@app.route("/health")
def health():
    cfg = get_cfg()
    return {
        "ok": True,
        "service": cfg["service"],
        "ffmpeg_found": ffmpeg_exists(cfg),
        "ffprobe_found": ffprobe_exists(cfg),
        "ffmpeg_bin": cfg["ffmpeg_bin"],
        "ffprobe_bin": cfg["ffprobe_bin"],
    }


@app.route("/master", methods=["POST"])
def master():
    cfg = get_cfg()
    if not check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not ffmpeg_exists(cfg):
        return jsonify({"ok": False, "error": "ffmpeg not found", "ffmpeg_bin": cfg["ffmpeg_bin"]}), 500

    temp_dir = Path(tempfile.mkdtemp(prefix="mastering_"))
    try:
        try:
            input_path, original_name = save_input_file(temp_dir)
        except ValueError:
            return jsonify({"ok": False, "error": "no file"}), 400
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if input_path.stat().st_size > cfg["max_file_size_bytes"]:
            return jsonify({"ok": False, "error": "file too large", "max_file_size_bytes": cfg["max_file_size_bytes"]}), 413

        params = {}
        params.update(request.form.to_dict(flat=True))
        if request.is_json and isinstance(request.json, dict):
            params.update(request.json)
        pcfg = preset_config(params, cfg)
        output_ext = file_ext_from_format(pcfg["output_format"])
        output_path = temp_dir / f"output.{output_ext}"

        measured = measure_loudnorm(input_path, cfg, pcfg)
        filter_chain = build_master_filter(pcfg, measured)
        cmd = [cfg["ffmpeg_bin"], "-y", "-i", str(input_path), "-af", filter_chain]
        if output_ext == "mp3":
            cmd += ["-b:a", "320k"]
        else:
            cmd += ["-c:a", "pcm_s16le"]
        cmd.append(str(output_path))

        result = run_cmd(cmd)
        if result.returncode != 0:
            return jsonify({"ok": False, "error": "ffmpeg failed", "stderr": result.stderr, "cmd": cmd}), 500

        download_name = f'{sanitize_name(Path(original_name).stem)}_mastered.{output_ext}'
        return send_file(output_path, mimetype=mime_from_format(output_ext), as_attachment=True, download_name=download_name)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route("/analyze", methods=["POST"])
def analyze():
    cfg = get_cfg()
    if not check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not ffmpeg_exists(cfg) or not ffprobe_exists(cfg):
        return jsonify({
            "ok": False,
            "error": "ffmpeg or ffprobe not found",
            "ffmpeg_found": ffmpeg_exists(cfg),
            "ffprobe_found": ffprobe_exists(cfg),
        }), 500

    temp_dir = Path(tempfile.mkdtemp(prefix="analyze_"))
    try:
        try:
            input_path, original_name = save_input_file(temp_dir)
        except ValueError:
            return jsonify({"ok": False, "error": "no file"}), 400
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if input_path.stat().st_size > cfg["max_file_size_bytes"]:
            return jsonify({"ok": False, "error": "file too large", "max_file_size_bytes": cfg["max_file_size_bytes"]}), 413

        params = {}
        params.update(request.form.to_dict(flat=True))
        if request.is_json and isinstance(request.json, dict):
            params.update(request.json)
        pcfg = preset_config(params, cfg)

        probe = ffprobe_json(input_path, cfg)
        fmt = probe.get("format", {})
        streams = probe.get("streams", [])
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
        loudnorm = measure_loudnorm(input_path, cfg, pcfg) or {}
        vol = measure_volumedetect(input_path, cfg) or {}

        return jsonify({
            "ok": True,
            "file": {
                "original_name": original_name,
                "size_bytes": input_path.stat().st_size,
            },
            "preset": pcfg,
            "analysis": {
                "format": {
                    "duration_seconds": float(fmt["duration"]) if fmt.get("duration") else None,
                    "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
                    "format_name": fmt.get("format_name"),
                },
                "audio": {
                    "codec_name": audio_stream.get("codec_name"),
                    "sample_rate": int(audio_stream["sample_rate"]) if audio_stream.get("sample_rate") else None,
                    "channels": int(audio_stream["channels"]) if audio_stream.get("channels") else None,
                    "channel_layout": audio_stream.get("channel_layout"),
                },
                "loudness": {
                    "integrated_lufs": float(loudnorm["input_i"]) if loudnorm.get("input_i") else None,
                    "loudness_range": float(loudnorm["input_lra"]) if loudnorm.get("input_lra") else None,
                    "true_peak_db": float(loudnorm["input_tp"]) if loudnorm.get("input_tp") else None,
                    "threshold_db": float(loudnorm["input_thresh"]) if loudnorm.get("input_thresh") else None,
                    "target_offset": float(loudnorm["target_offset"]) if loudnorm.get("target_offset") else None,
                },
                "volume": vol,
                "recommendations": summarize_recommendations({
                    "integrated_lufs": float(loudnorm["input_i"]) if loudnorm.get("input_i") else None,
                    "true_peak_db": float(loudnorm["input_tp"]) if loudnorm.get("input_tp") else None,
                }, vol, pcfg),
            }
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route("/situate", methods=["POST"])
def situate():
    cfg = get_cfg()
    if not check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    analysis = payload.get("analysis") or {}
    preset = preset_config(payload, cfg)
    loudness = analysis.get("loudness") or {}
    volume = analysis.get("volume") or {}

    recommendations = summarize_recommendations(loudness, volume, preset)
    strategy = {
        "preset": preset,
        "notes": recommendations,
        "suggested_mode": "safe" if (loudness.get("true_peak_db") is not None and loudness.get("true_peak_db") > -0.5) else preset["mode"],
        "suggested_output_format": preset["output_format"],
    }
    return jsonify({"ok": True, "strategy": strategy})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
