"""Background worker para procesar uploads secuencialmente."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from finanzas import db, ingest


def process_upload_job(job_id: int) -> None:
    """Procesa un job de upload: itera archivos guardados y llama a ingest.

    Usa una conn separada por archivo para que un fallo en uno no aborte
    la transacción del resto.
    """
    # Atomic claim: only one worker can transition pending -> processing
    with db.connect() as conn:
        claim = conn.execute(
            """UPDATE upload_jobs SET status = 'processing', stage = 'processing', started_at = NOW()
               WHERE id = %s AND status = 'pending'
               RETURNING user_id, results""",
            (job_id,),
        ).fetchone()

    if not claim:
        print(f"[UPLOAD] Job {job_id} not claimable (already processing or missing)")
        return

    user_id = claim["user_id"]
    files_data = json.loads(claim["results"] or "[]")
    upload_dir = Path(tempfile.gettempdir()) / "finanzas-uploads" / str(job_id)

    print(f"[UPLOAD] Starting job {job_id}, {len(files_data)} files")

    with db.connect() as conn:
        conn.execute(
            "UPDATE upload_jobs SET progress_current = 0, progress_total = %s WHERE id = %s",
            (len(files_data), job_id),
        )

    results: list[dict] = []
    try:
        for idx, file_info in enumerate(files_data):
            tmp_path = Path(file_info["tmp_path"])
            filename = file_info["filename"]
            print(f"[UPLOAD] Processing {idx+1}/{len(files_data)}: {filename}")

            # Progress update in its own conn (committed immediately)
            with db.connect() as progress_conn:
                progress_conn.execute(
                    "UPDATE upload_jobs SET progress_current = %s WHERE id = %s",
                    (idx + 1, job_id),
                )

            # Persist partial results so frontend sees them as they complete
            with db.connect() as partial_conn:
                partial_conn.execute(
                    "UPDATE upload_jobs SET results = %s WHERE id = %s",
                    (json.dumps(results), job_id),
                )

            if not tmp_path.exists():
                results.append({"filename": filename, "status": "error", "message": "File not found"})
                continue

            # New conn per file: rollback on exception keeps other files unaffected
            try:
                with db.connect() as file_conn:
                    result = ingest.ingest_file(file_conn, tmp_path, user_id=user_id)
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

    finally:
        try:
            shutil.rmtree(upload_dir, ignore_errors=True)
        except Exception:
            pass

    # Mark as completed in its own conn
    with db.connect() as conn:
        conn.execute(
            """UPDATE upload_jobs SET status = %s, results = %s, completed_at = NOW()
               WHERE id = %s""",
            ("completed", json.dumps(results), job_id),
        )


def start_processing_pending() -> None:
    """Procesa jobs pendientes uno por uno. Cada uno maneja sus propias conns."""
    with db.connect() as conn:
        jobs = conn.execute(
            "SELECT id FROM upload_jobs WHERE status = %s ORDER BY created_at ASC",
            ("pending",),
        ).fetchall()

    for job in jobs:
        try:
            process_upload_job(job["id"])
        except Exception as e:
            print(f"[UPLOAD] Worker error on job {job['id']}: {e}")
