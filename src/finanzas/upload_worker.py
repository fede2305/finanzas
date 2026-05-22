"""Background worker para procesar uploads secuencialmente."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from finanzas import db, ingest


def process_upload_job(conn, job_id: int) -> None:
    """Procesa un job de upload: itera archivos guardados y llama a ingest."""
    import shutil
    from finanzas import db

    job = conn.execute(
        "SELECT id, user_id, results FROM upload_jobs WHERE id = %s",
        (job_id,),
    ).fetchone()

    if not job:
        print(f"[UPLOAD] Job {job_id} not found")
        return

    user_id = job["user_id"]
    files_data = json.loads(job["results"] or "[]")
    upload_dir = Path(tempfile.gettempdir()) / "finanzas-uploads" / str(job_id)

    print(f"[UPLOAD] Starting job {job_id}, {len(files_data)} files")
    conn.execute(
        "UPDATE upload_jobs SET status = %s, stage = %s, started_at = NOW() WHERE id = %s",
        ("processing", "processing", job_id),
    )

    results = []
    try:
        for idx, file_info in enumerate(files_data):
            tmp_path = Path(file_info["tmp_path"])
            filename = file_info["filename"]
            print(f"[UPLOAD] Processing {idx+1}/{len(files_data)}: {filename}")

            # Update progress BEFORE processing (so user sees it's being processed)
            with db.connect() as progress_conn:
                progress_conn.execute(
                    "UPDATE upload_jobs SET progress_current = %s WHERE id = %s",
                    (idx + 1, job_id),
                )

            try:
                if not tmp_path.exists():
                    results.append({"filename": filename, "status": "error", "message": "File not found"})
                    continue

                result = ingest.ingest_file(conn, tmp_path, user_id=user_id)
                results.append({
                    "filename": filename,
                    "status": "success",
                    "new_transactions": result.new_transactions,
                    "duplicate_transactions": result.duplicate_transactions,
                    "auto_categorized": result.auto_categorized,
                    "uncategorized": result.uncategorized,
                })
            except Exception as e:
                print(f"[UPLOAD] Error processing {filename}: {e}")
                results.append({"filename": filename, "status": "error", "message": str(e)})

            print(f"[UPLOAD] Progress: {idx+1}/{len(files_data)}")

    finally:
        # Cleanup upload directory
        try:
            shutil.rmtree(upload_dir, ignore_errors=True)
        except Exception:
            pass

    # Mark as completed
    conn.execute(
        """UPDATE upload_jobs SET status = %s, results = %s, completed_at = NOW()
           WHERE id = %s""",
        ("completed", json.dumps(results), job_id),
    )


def start_processing_pending() -> None:
    """Inicia procesamiento de jobs pendientes (llamar periódicamente o en un scheduler)."""
    with db.connect() as conn:
        jobs = conn.execute(
            "SELECT id FROM upload_jobs WHERE status = %s ORDER BY created_at ASC",
            ("pending",),
        ).fetchall()

        for job in jobs:
            process_upload_job(conn, job["id"])
