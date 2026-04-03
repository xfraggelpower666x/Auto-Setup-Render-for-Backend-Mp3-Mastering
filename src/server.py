@app.post("/process")
async def process_audio(
    request: Request,
    file: Optional[UploadFile] = File(default=None),
    mode: Optional[str] = Form(default="process"),
):
    check_admin(request)

    if file is None:
        raise HTTPException(status_code=400, detail="file upload required")

    source_filename = file.filename or "upload.mp3"
    source_path = INPUT_DIR / source_filename

    with open(source_path, "wb") as f:
        f.write(await file.read())

    payload = ProcessRequest(filename=source_filename, mode=mode or "process")

    metadata = default_metadata(source_filename, payload)
    job_id = create_job(
        source_filename=source_filename,
        source_path=str(source_path),
        mode=payload.mode
    )

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
        }

    except Exception as e:
        update_job(job_id, status="error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
