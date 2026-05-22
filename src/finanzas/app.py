"""FastAPI app: rutas, templates, upload, review inbox, dashboard."""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

import plotly.graph_objects as go
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from finanzas import auth, db, ingest, insights, queries, upload_worker
from finanzas.categorizer import add_learned_rule
from finanzas.parsers.base import normalize_description

ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ejecuta migraciones al arrancar (seed es per-user, se hace en /auth/callback)."""
    db.migrate()

    # Start background task to process pending uploads
    import asyncio
    async def process_uploads_loop():
        while True:
            try:
                with db.connect() as conn:
                    upload_worker.start_processing_pending()
            except Exception as e:
                print(f"Error processing uploads: {e}")
            await asyncio.sleep(5)  # Check every 5 seconds

    task = asyncio.create_task(process_uploads_loop())
    yield
    task.cancel()


app = FastAPI(title="Finanzas", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
)

templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["currency_ars"] = lambda x: _fmt_ars(x)
templates.env.globals["currency_usd"] = lambda x: _fmt_usd(x)
templates.env.globals["fmt_pct"] = lambda x: f"{x:+.0f}%" if x is not None else ""
templates.env.globals["abs"] = abs

static_dir = ROOT / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# --------------------- Helpers ---------------------

def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _redirect_back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(
        url=request.headers.get("Referer") or fallback, status_code=303
    )


def _fmt_ars(v: float | None) -> str:
    if v is None:
        return "$0"
    sign = "-" if v < 0 else ""
    v = abs(v)
    s = f"{v:,.0f}".replace(",", ".")
    return f"{sign}${s}"


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "US$0"
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}US${v:,.2f}"


def _delta(curr: float, avg: float) -> float | None:
    if avg <= 0 or curr <= 0:
        return None
    return (curr - avg) / avg * 100


def _parse_month(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m").date().replace(day=1)
    except ValueError:
        return None


def _month_options(months_back: int = 12) -> list[tuple[str, str]]:
    today = date.today().replace(day=1)
    out = []
    for i in range(months_back):
        m = queries.add_months(today, -i)
        out.append((m.strftime("%Y-%m"), m.strftime("%B %Y").capitalize()))
    return out


def _load_tx_with_participants(conn, user_id: str, tx_id: int) -> dict:
    row = conn.execute(
        """SELECT t.id, t.posted_at, t.description_raw, t.amount, t.currency,
                  t.my_share_pct, t.share_with, t.notes,
                  t.installment_current, t.installment_total,
                  a.bank, a.card_last4, s.name AS subcategory
           FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           LEFT JOIN categories s ON s.id = t.subcategory_id
           WHERE t.id = %s AND t.user_id = %s""",
        (tx_id, user_id),
    ).fetchone()
    item = dict(row)
    parts = conn.execute(
        """SELECT id, person_name, amount_owed, paid_back, paid_back_at
           FROM tx_participants WHERE transaction_id = %s
           ORDER BY sort_order, id""",
        (tx_id,),
    ).fetchall()
    item["participants"] = [dict(p) for p in parts]
    return item


# --------------------- Auth routes ---------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if auth.require_user(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/auth/google")
async def auth_google(request: Request):
    redirect_uri = auth.callback_url(request)
    return await auth.oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await auth.oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")
    if not user_info:
        user_info = await auth.oauth.google.userinfo(token=token)
    user = {
        "sub": user_info["sub"],
        "email": user_info["email"],
        "name": user_info.get("name", user_info["email"]),
    }
    request.session["user"] = user
    with db.connect() as conn:
        db.seed_for_user(conn, user["sub"])
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/me")
def me(request: Request):
    from fastapi.responses import JSONResponse
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return JSONResponse(user)


# --------------------- Dashboard ---------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, month: str | None = None, account: str | None = None):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    account_id: int | None = None
    if account and account != "all":
        try:
            account_id = int(account)
        except ValueError:
            account_id = None

    with db.connect() as conn:
        anchor = _parse_month(month) or queries.latest_data_anchor(conn, user_id)

        ars = queries.total_month(conn, user_id, anchor, "ARS", account_id=account_id)
        usd = queries.total_month(conn, user_id, anchor, "USD", account_id=account_id)
        ars_facturado = queries.total_month(conn, user_id, anchor, "ARS", account_id=account_id, use_my_share=False)
        usd_facturado = queries.total_month(conn, user_id, anchor, "USD", account_id=account_id, use_my_share=False)
        ars_te_deben = queries.te_deben_month(conn, user_id, anchor, "ARS", account_id=account_id)
        usd_te_deben = queries.te_deben_month(conn, user_id, anchor, "USD", account_id=account_id)
        ars_te_pagado = queries.te_pagado_month(conn, user_id, anchor, "ARS", account_id=account_id)
        usd_te_pagado = queries.te_pagado_month(conn, user_id, anchor, "USD", account_id=account_id)
        ars_avg = queries.avg_last_3_months(conn, user_id, anchor, "ARS", account_id=account_id)
        usd_avg = queries.avg_last_3_months(conn, user_id, anchor, "USD", account_id=account_id)

        cat_dist = queries.category_distribution(conn, user_id, anchor, "ARS", account_id=account_id)
        merchants = queries.top_categories_compare(conn, user_id, anchor, "ARS", account_id=account_id)
        trend = queries.monthly_trend(conn, user_id, anchor, 6, "ARS", account_id=account_id)
        cuotas = queries.cuotas_forecast(conn, user_id, anchor, 6, "ARS", account_id=account_id)
        fixed = queries.confirmed_recurring(conn, user_id)
        suggested = queries.suggested_recurring(conn, user_id)
        ins = insights.generate(conn, anchor, user_id)
        uncat_count = queries.uncategorized_count(conn, user_id)
        accounts = queries.all_accounts(conn, user_id)

        delta_ars = _delta(ars, ars_avg)
        delta_usd = _delta(usd, usd_avg)

        treemap_html = _plot_treemap(cat_dist)
        trend_html = _plot_trend(trend)
        cuotas_html = _plot_cuotas(cuotas)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "anchor": anchor,
            "month_label": anchor.strftime("%B %Y").capitalize(),
            "month_iso": anchor.strftime("%Y-%m"),
            "month_options": _month_options(),
            "account_id": account_id,
            "account_param": str(account_id) if account_id else "all",
            "ars": ars,
            "usd": usd,
            "ars_facturado": ars_facturado,
            "usd_facturado": usd_facturado,
            "ars_te_deben": ars_te_deben,
            "usd_te_deben": usd_te_deben,
            "ars_te_pagado": ars_te_pagado,
            "usd_te_pagado": usd_te_pagado,
            "delta_ars": delta_ars,
            "delta_usd": delta_usd,
            "merchants": merchants,
            "fixed": fixed,
            "suggested": suggested,
            "insights": ins,
            "uncat_count": uncat_count,
            "accounts": accounts,
            "treemap_html": treemap_html,
            "trend_html": trend_html,
            "cuotas_html": cuotas_html,
        },
    )


# --------------------- Upload ---------------------

@app.get("/upload", response_class=HTMLResponse)
def upload_get(request: Request):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        request, "upload.html", {"result": None, "user": user},
    )


@app.post("/api/upload")
async def upload_api(request: Request, file: UploadFile = File(...), job_id: int | None = None, total_files: int | None = None):
    from fastapi.responses import JSONResponse
    user = auth.require_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id: str = user["sub"]

    if not file.filename:
        raise HTTPException(400, "No file uploaded")

    import json

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".pdf", ".xlsx"}:
        raise HTTPException(400, f"Archivo inválido: {file.filename}")

    if file.size and file.size > 50 * 1024 * 1024:
        raise HTTPException(400, "Archivo muy grande (máx 50MB)")

    # First file: create new job
    if not job_id:
        with db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO upload_jobs (user_id, status, stage, progress_total, results)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (user_id, "pending", "uploading", total_files or 1, json.dumps([])),
            )
            job_id = cur.lastrowid

    # Create temp directory for this job
    upload_dir = Path(tempfile.gettempdir()) / "finanzas-uploads" / str(job_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    try:
        tmp_path = upload_dir / file.filename

        # Write to disk in 64KB chunks
        with open(tmp_path, "wb", buffering=8192) as out:
            while chunk := await file.read(65536):
                out.write(chunk)
                out.flush()

        # Get current files list and add this one
        with db.connect() as conn:
            job = conn.execute(
                "SELECT results FROM upload_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
            files_data = json.loads(job["results"] or "[]")

            # Add new file
            files_data.append({"filename": file.filename, "tmp_path": str(tmp_path)})

            # Update job
            conn.execute(
                """UPDATE upload_jobs SET results = %s WHERE id = %s""",
                (json.dumps(files_data), job_id),
            )

        # If this is the last file, start processing
        if total_files and len(files_data) >= total_files:
            import asyncio
            asyncio.create_task(_process_upload_async(job_id))

        return JSONResponse({"job_id": job_id, "files_uploaded": len(files_data)})

    except Exception as e:
        print(f"[UPLOAD] Error: {e}")
        raise


async def _process_upload_async(job_id: int) -> None:
    """Background task to process upload job."""
    with db.connect() as conn:
        upload_worker.process_upload_job(conn, job_id)


@app.get("/api/upload-status/{job_id}")
async def upload_status(request: Request, job_id: int):
    from fastapi.responses import JSONResponse
    user = auth.require_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user_id: str = user["sub"]

    with db.connect() as conn:
        job = conn.execute(
            """SELECT id, status, stage, progress_current, progress_total, results, error
               FROM upload_jobs WHERE id = %s AND user_id = %s""",
            (job_id, user_id),
        ).fetchone()

    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    import json
    results = json.loads(job["results"]) if job["results"] else []

    return JSONResponse({
        "job_id": job["id"],
        "status": job["status"],
        "stage": job["stage"] or "uploading",
        "progress": {"current": job["progress_current"], "total": job["progress_total"]},
        "results": results,
        "error": job["error"],
    })


# --------------------- Review inbox ---------------------

@app.get("/review", response_class=HTMLResponse)
def review_get(request: Request):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        items = queries.uncategorized(conn, user_id, limit=200)
        tree = queries.category_tree(conn, user_id)
    return templates.TemplateResponse(
        request, "review.html", {"items": items, "tree": tree, "user": user},
    )


@app.post("/review/{tx_id}", response_class=HTMLResponse)
def review_post(
    request: Request,
    tx_id: int,
    category_id: int = Form(...),
    subcategory_id: int | None = Form(None),
    remember: bool = Form(False),
):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        tx = conn.execute(
            "SELECT description_raw, description_normalized FROM transactions WHERE id = %s AND user_id = %s",
            (tx_id, user_id),
        ).fetchone()
        if not tx:
            raise HTTPException(404, "Transaction not found")
        conn.execute(
            """UPDATE transactions SET category_id = %s, subcategory_id = %s,
               is_manually_categorized = 1 WHERE id = %s AND user_id = %s""",
            (category_id, subcategory_id, tx_id, user_id),
        )
        if remember:
            pattern = tx["description_normalized"] or normalize_description(tx["description_raw"])
            add_learned_rule(conn, user_id, pattern, category_id, subcategory_id, match_type="exact")
            conn.execute(
                """UPDATE transactions
                   SET category_id = %s, subcategory_id = %s, is_manually_categorized = 0
                   WHERE category_id IS NULL AND description_normalized = %s AND user_id = %s""",
                (category_id, subcategory_id, pattern, user_id),
            )
    return HTMLResponse(f'<tr id="tx-{tx_id}" hx-swap-oob="outerHTML"></tr>')


# --------------------- Categorías ---------------------

@app.get("/categories", response_class=HTMLResponse)
def categories_get(request: Request):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        tree = queries.category_tree(conn, user_id)
    return templates.TemplateResponse(
        request, "categories.html", {"tree": tree, "user": user},
    )


@app.post("/categories", response_class=HTMLResponse)
def categories_post(
    request: Request,
    name: str = Form(...),
    parent_id: int | None = Form(None),
    color: str = Form("#94a3b8"),
):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre es requerido")
    with db.connect() as conn:
        try:
            conn.execute(
                """INSERT INTO categories (user_id, parent_id, name, color, is_user_created, sort_order)
                   VALUES (%s, %s, %s, %s, 1, 999)""",
                (user_id, parent_id if parent_id else None, name, color),
            )
        except Exception as e:
            raise HTTPException(400, f"Error: {e}")
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/inline", response_class=HTMLResponse)
def categories_inline_create(
    request: Request,
    name: str = Form(...),
    parent_id: int | None = Form(None),
):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre es requerido")
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO categories (user_id, parent_id, name, color, is_user_created, sort_order)
               VALUES (%s, %s, %s, '#94a3b8', 1, 999) RETURNING id""",
            (user_id, parent_id if parent_id else None, name),
        )
        new_id = cur.lastrowid
    return HTMLResponse(f'<option value="{new_id}" selected>{name}</option>')


# --------------------- Manual entries ---------------------

@app.get("/manual", response_class=HTMLResponse)
def manual_get(request: Request):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        rows = conn.execute(
            """SELECT m.id, m.posted_at, m.description, m.amount, m.currency,
                      c.name AS cat, s.name AS sub, m.is_fixed, m.recurrence_rule
               FROM manual_expenses m
               LEFT JOIN categories c ON c.id = m.category_id
               LEFT JOIN categories s ON s.id = m.subcategory_id
               WHERE m.user_id = %s
               ORDER BY m.posted_at DESC""",
            (user_id,),
        ).fetchall()
        items = [dict(r) for r in rows]
        tree = queries.category_tree(conn, user_id)
    return templates.TemplateResponse(
        request, "manual.html", {"items": items, "tree": tree, "user": user},
    )


@app.post("/manual", response_class=HTMLResponse)
def manual_post(
    request: Request,
    posted_at: str = Form(...),
    description: str = Form(...),
    amount: float = Form(...),
    currency: str = Form("ARS"),
    category_id: int = Form(...),
    subcategory_id: int | None = Form(None),
    is_fixed: bool = Form(False),
    recurrence_rule: str | None = Form(None),
):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        conn.execute(
            """INSERT INTO manual_expenses
               (user_id, posted_at, description, amount, currency,
                category_id, subcategory_id, is_fixed, recurrence_rule)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, posted_at, description, amount, currency,
             category_id, subcategory_id, 1 if is_fixed else 0,
             recurrence_rule if is_fixed else None),
        )
    return RedirectResponse(url="/manual", status_code=303)


@app.post("/manual/{mid}/delete", response_class=HTMLResponse)
def manual_delete(request: Request, mid: int):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        conn.execute("DELETE FROM manual_expenses WHERE id = %s AND user_id = %s", (mid, user_id))
    return RedirectResponse(url="/manual", status_code=303)


# --------------------- All transactions of month ---------------------

@app.get("/transactions/month", response_class=HTMLResponse)
def transactions_month(request: Request, month: str | None = None, account: str | None = None):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    account_id: int | None = None
    if account and account != "all":
        try:
            account_id = int(account)
        except ValueError:
            account_id = None
    with db.connect() as conn:
        anchor = _parse_month(month) or queries.latest_data_anchor(conn, user_id)
        items = queries.transactions_in_month(conn, user_id, anchor, "ARS", account_id=account_id)
        items_usd = queries.transactions_in_month(conn, user_id, anchor, "USD", account_id=account_id)
        tree = queries.category_tree(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)
    return templates.TemplateResponse(
        request, "partials/category_detail.html",
        {
            "user": user,
            "category_id": -1,
            "category_name": "Todos los gastos del mes",
            "category_color": "#64748b",
            "show_close": False,
            "items": items,
            "items_usd": items_usd,
            "total_ars_pagado": sum(i["amount"] for i in items),
            "total_ars_mio":    sum(i["amount"] * (i["my_share_pct"] or 1.0) for i in items),
            "total_usd_pagado": sum(i["amount"] for i in items_usd),
            "total_usd_mio":    sum(i["amount"] * (i["my_share_pct"] or 1.0) for i in items_usd),
            "month_iso": anchor.strftime("%Y-%m"),
            "account_param": str(account_id) if account_id else "all",
            "tree": tree,
            "frequent_people": frequent_people,
        },
    )


# --------------------- Category drill-down ---------------------

@app.get("/category/{category_id}/transactions", response_class=HTMLResponse)
def category_txs(request: Request, category_id: int,
                 month: str | None = None, account: str | None = None):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    account_id: int | None = None
    if account and account != "all":
        try:
            account_id = int(account)
        except ValueError:
            account_id = None
    with db.connect() as conn:
        anchor = _parse_month(month) or queries.latest_data_anchor(conn, user_id)
        cid = None if category_id == 0 else category_id
        items = queries.transactions_in_category(conn, user_id, cid, anchor, "ARS", account_id=account_id)
        items_usd = queries.transactions_in_category(conn, user_id, cid, anchor, "USD", account_id=account_id)
        if cid is None:
            name = "Sin categoría"
            color = "#94a3b8"
        else:
            row = conn.execute(
                "SELECT name, color FROM categories WHERE id = %s AND user_id = %s", (cid, user_id)
            ).fetchone()
            name = row["name"] if row else "?"
            color = row["color"] if row else "#94a3b8"
        tree = queries.category_tree(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)
    return templates.TemplateResponse(
        request,
        "partials/category_detail.html",
        {
            "user": user,
            "category_id": category_id,
            "category_name": name,
            "category_color": color,
            "show_close": True,
            "items": items,
            "items_usd": items_usd,
            "total_ars_pagado": sum(i["amount"] for i in items),
            "total_ars_mio":    sum(i["amount"] * (i["my_share_pct"] or 1.0) for i in items),
            "total_usd_pagado": sum(i["amount"] for i in items_usd),
            "total_usd_mio":    sum(i["amount"] * (i["my_share_pct"] or 1.0) for i in items_usd),
            "month_iso": anchor.strftime("%Y-%m"),
            "account_param": str(account_id) if account_id else "all",
            "tree": tree,
            "frequent_people": frequent_people,
        },
    )


# --------------------- Shared expense (participants) ---------------------

@app.post("/tx/{tx_id}/share", response_class=HTMLResponse)
async def tx_share(request: Request, tx_id: int):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    form = await request.form()
    names = form.getlist("participant_name")
    amounts = form.getlist("participant_amount")
    paids = form.getlist("participant_paid")

    with db.connect() as conn:
        tx = conn.execute(
            "SELECT id, amount FROM transactions WHERE id = %s AND user_id = %s", (tx_id, user_id)
        ).fetchone()
        if not tx:
            raise HTTPException(404, "Transacción no existe")
        conn.execute("DELETE FROM tx_participants WHERE transaction_id = %s", (tx_id,))
        total_owed = 0.0
        for i, (n, a) in enumerate(zip(names, amounts)):
            n = (n or "").strip()
            if not n:
                continue
            try:
                amt = float((a or "0").replace(",", "."))
            except ValueError:
                amt = 0.0
            if amt <= 0:
                continue
            paid = 0
            if i < len(paids) and paids[i] in ("1", "on", "true"):
                paid = 1
            paid_at = datetime.utcnow().isoformat(timespec="seconds") if paid else None
            conn.execute(
                """INSERT INTO tx_participants
                   (transaction_id, person_name, amount_owed, paid_back, paid_back_at, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (tx_id, n, amt, paid, paid_at, i),
            )
            total_owed += amt

        tx_amount = tx["amount"] or 0.0
        my_share = (
            max(0.0, min(1.0, 1.0 - total_owed / tx_amount)) if tx_amount > 0 else 1.0
        )
        names_str = ", ".join(n.strip() for n in names if n.strip()) or None
        conn.execute(
            "UPDATE transactions SET my_share_pct = %s, share_with = %s WHERE id = %s AND user_id = %s",
            (my_share, names_str, tx_id, user_id),
        )

        item = _load_tx_with_participants(conn, user_id, tx_id)
        tree = queries.category_tree(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)
    if not _is_htmx(request):
        return _redirect_back(request)
    return templates.TemplateResponse(
        request, "partials/category_row.html",
        {"it": item, "currency": item["currency"], "tree": tree,
         "frequent_people": frequent_people, "user": user},
    )


@app.post("/tx/{tx_id}/category", response_class=HTMLResponse)
def tx_change_category(
    request: Request,
    tx_id: int,
    category_id: int = Form(...),
    subcategory_id: int | None = Form(None),
    remember: bool = Form(False),
):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        tx = conn.execute(
            "SELECT description_raw, description_normalized FROM transactions WHERE id = %s AND user_id = %s",
            (tx_id, user_id),
        ).fetchone()
        if not tx:
            raise HTTPException(404, "Transacción no existe")
        sub_id = subcategory_id if (subcategory_id and subcategory_id > 0) else None
        conn.execute(
            """UPDATE transactions SET category_id = %s, subcategory_id = %s,
               is_manually_categorized = 1 WHERE id = %s AND user_id = %s""",
            (category_id, sub_id, tx_id, user_id),
        )
        if remember:
            pattern = tx["description_normalized"] or normalize_description(tx["description_raw"])
            add_learned_rule(conn, user_id, pattern, category_id, sub_id, match_type="exact")
            conn.execute(
                """UPDATE transactions SET category_id = %s, subcategory_id = %s, is_manually_categorized = 1
                   WHERE description_normalized = %s AND id != %s AND user_id = %s""",
                (category_id, sub_id, pattern, tx_id, user_id),
            )
        item = _load_tx_with_participants(conn, user_id, tx_id)
        tree = queries.category_tree(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)
    if not _is_htmx(request):
        return _redirect_back(request)
    return templates.TemplateResponse(
        request, "partials/category_row.html",
        {"it": item, "currency": item["currency"], "tree": tree,
         "frequent_people": frequent_people, "moved_to_category_id": category_id, "user": user},
    )


@app.post("/tx/{tx_id}/note", response_class=HTMLResponse)
def tx_set_note(request: Request, tx_id: int, note: str = Form("")):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    cleaned = (note or "").strip() or None
    with db.connect() as conn:
        conn.execute(
            "UPDATE transactions SET notes = %s WHERE id = %s AND user_id = %s",
            (cleaned, tx_id, user_id),
        )
        item = _load_tx_with_participants(conn, user_id, tx_id)
        tree = queries.category_tree(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)
    if not _is_htmx(request):
        return _redirect_back(request)
    return templates.TemplateResponse(
        request, "partials/category_row.html",
        {"it": item, "currency": item["currency"], "tree": tree,
         "frequent_people": frequent_people, "user": user},
    )


@app.post("/participant/{pid}/toggle-paid", response_class=HTMLResponse)
def participant_toggle(request: Request, pid: int):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        row = conn.execute(
            """SELECT p.transaction_id, p.paid_back
               FROM tx_participants p
               JOIN transactions t ON t.id = p.transaction_id
               WHERE p.id = %s AND t.user_id = %s""",
            (pid, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Participante no existe")
        new_paid = 0 if row["paid_back"] else 1
        paid_at = datetime.utcnow().isoformat(timespec="seconds") if new_paid else None
        conn.execute(
            "UPDATE tx_participants SET paid_back = %s, paid_back_at = %s WHERE id = %s",
            (new_paid, paid_at, pid),
        )
        item = _load_tx_with_participants(conn, user_id, row["transaction_id"])
        tree = queries.category_tree(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)
    if not _is_htmx(request):
        return _redirect_back(request)
    return templates.TemplateResponse(
        request, "partials/category_row.html",
        {"it": item, "currency": item["currency"], "tree": tree,
         "frequent_people": frequent_people, "user": user},
    )


# --------------------- Pendientes ---------------------

@app.get("/pendientes", response_class=HTMLResponse)
def pendientes(request: Request, include_paid: bool = False):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        only_pending = not include_paid
        groups = queries.participants_owed_by_person(conn, user_id, only_pending=only_pending)
        groups.sort(key=lambda g: g["total_owed_pending"], reverse=True)
        total_pending = sum(g["total_owed_pending"] for g in groups)
        uncat_count = queries.uncategorized_count(conn, user_id)
    return templates.TemplateResponse(
        request, "pendientes.html",
        {
            "user": user,
            "groups": groups,
            "total_pending": total_pending,
            "include_paid": include_paid,
            "uncat_count": uncat_count,
        },
    )


@app.get("/pendientes.csv")
def pendientes_csv(request: Request, include_paid: bool = False, person: str | None = None):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        groups = queries.participants_owed_by_person(conn, user_id, only_pending=not include_paid)
        if person:
            person_lower = person.strip().lower()
            groups = [g for g in groups if g["person_name"].lower() == person_lower]
        groups.sort(key=lambda g: g["total_owed_pending"], reverse=True)

    buf = io.StringIO()
    writer = csv.writer(buf)
    if person and len(groups) == 1:
        g = groups[0]
        writer.writerow([f"Detalle de lo que {g['person_name']} te debe"])
        writer.writerow([f"Total pendiente: ${g['total_owed_pending']:,.2f}"])
        if g["total_owed_paid"] > 0:
            writer.writerow([f"Ya pagado: ${g['total_owed_paid']:,.2f}"])
        writer.writerow([])
    writer.writerow([
        "fecha", "descripcion", "cuenta", "monto_total_tx",
        "monto_que_te_debe", "moneda", "pagado", "pagado_el",
    ])
    for g in groups:
        for tx in g["txs"]:
            writer.writerow([
                tx["posted_at"][:10],
                tx["description_raw"],
                f"{tx['bank']} ···{tx['card_last4']}",
                f"{tx['amount']:.2f}",
                f"{tx['amount_owed']:.2f}",
                tx["currency"],
                "sí" if tx["paid_back"] else "no",
                (tx["paid_back_at"] or "")[:10] if tx["paid_back_at"] else "",
            ])
        if not person:
            writer.writerow([
                "", f"  Subtotal {g['person_name']}", "", "",
                f"{g['total_owed_pending']:.2f}", "ARS", "pendiente", "",
            ])
    buf.seek(0)
    today = date.today().isoformat()
    base = f"pendientes_{today}"
    if person:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", person.strip())[:40]
        base = f"pendientes_{safe}_{today}"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{base}.csv"'},
    )


# --------------------- Búsqueda ---------------------

@app.get("/buscar", response_class=HTMLResponse)
def buscar(
    request: Request,
    q: str | None = None,
    amount: str | None = None,
    tolerance: int = 10,
    currency: str = "",
    account: str | None = None,
):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    account_id: int | None = None
    if account and account != "all":
        try:
            account_id = int(account)
        except ValueError:
            account_id = None

    amt: float | None = None
    if amount:
        try:
            s = amount.strip().replace("$", "").replace(" ", "")
            if "," in s and "." in s:
                s = s.replace(".", "").replace(",", ".")
            elif "," in s:
                s = s.replace(",", ".")
            amt = float(s)
        except (ValueError, TypeError):
            amt = None

    has_query = bool(q) or amt is not None
    with db.connect() as conn:
        if has_query:
            results = queries.search_transactions(
                conn, user_id, text=q, amount=amt, tolerance_pct=tolerance,
                currency=(currency or None), account_id=account_id, limit=300,
            )
        else:
            results = []
        tree = queries.category_tree(conn, user_id)
        accounts = queries.all_accounts(conn, user_id)
        uncat_count = queries.uncategorized_count(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)

    total_ars = sum(r["amount"] for r in results if r["currency"] == "ARS")
    total_usd = sum(r["amount"] for r in results if r["currency"] == "USD")

    return templates.TemplateResponse(
        request, "buscar.html",
        {
            "user": user,
            "q": q or "",
            "amount": amount or "",
            "tolerance": tolerance,
            "currency": currency,
            "account_param": str(account_id) if account_id else "all",
            "results": results,
            "tree": tree,
            "accounts": accounts,
            "uncat_count": uncat_count,
            "frequent_people": frequent_people,
            "total_ars": total_ars,
            "total_usd": total_usd,
            "has_query": has_query,
        },
    )


# --------------------- Cuotas ---------------------

@app.get("/cuotas", response_class=HTMLResponse)
def cuotas_page(request: Request, account: str | None = None):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    account_id: int | None = None
    if account and account != "all":
        try:
            account_id = int(account)
        except ValueError:
            account_id = None

    with db.connect() as conn:
        anchor = queries.latest_data_anchor(conn, user_id)
        items = queries.cuotas_pending_detail(conn, user_id, account_id=account_id)
        total_pending_ars, n_purchases_ars = queries.cuotas_pending_total(conn, user_id, "ARS", account_id)
        total_pending_usd, n_purchases_usd = queries.cuotas_pending_total(conn, user_id, "USD", account_id)
        month_paid_ars, n_txs_ars = queries.cuotas_this_month(conn, user_id, anchor, "ARS", account_id)
        month_paid_usd, n_txs_usd = queries.cuotas_this_month(conn, user_id, anchor, "USD", account_id)
        forecast = queries.cuotas_forecast(conn, user_id, anchor, 6, "ARS", account_id)
        accounts = queries.all_accounts(conn, user_id)
        uncat_count = queries.uncategorized_count(conn, user_id)
        frequent_people = queries.frequent_participants(conn, user_id)
        tree = queries.category_tree(conn, user_id)
        forecast_html = _plot_cuotas(forecast)
    return templates.TemplateResponse(
        request, "cuotas.html",
        {
            "user": user,
            "items": items,
            "anchor_label": anchor.strftime("%B %Y").capitalize(),
            "total_pending_ars": total_pending_ars,
            "total_pending_usd": total_pending_usd,
            "n_purchases_ars": n_purchases_ars,
            "n_purchases_usd": n_purchases_usd,
            "month_paid_ars": month_paid_ars,
            "month_paid_usd": month_paid_usd,
            "n_txs_ars": n_txs_ars,
            "n_txs_usd": n_txs_usd,
            "forecast_html": forecast_html,
            "accounts": accounts,
            "account_param": str(account_id) if account_id else "all",
            "uncat_count": uncat_count,
            "frequent_people": frequent_people,
            "tree": tree,
        },
    )


@app.get("/cuotas.csv")
def cuotas_csv(request: Request, account: str | None = None):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    account_id: int | None = None
    if account and account != "all":
        try:
            account_id = int(account)
        except ValueError:
            account_id = None
    with db.connect() as conn:
        items = queries.cuotas_pending_detail(conn, user_id, account_id=account_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "descripcion", "cuenta", "categoria", "subcategoria",
        "monto_cuota", "cuota_actual", "cuotas_totales", "cuotas_restantes",
        "monto_pendiente", "total_compra_estimado", "moneda", "fecha_origen",
    ])
    for it in items:
        writer.writerow([
            it["description_raw"],
            f"{it['bank']} ···{it['card_last4']}",
            it.get("category") or "",
            it.get("subcategory") or "",
            f"{it['amount']:.2f}",
            it["installment_current"],
            it["installment_total"],
            it["remaining_count"],
            f"{it['remaining_amount']:.2f}",
            f"{it['total_purchase']:.2f}",
            it["currency"],
            it["posted_at"][:10],
        ])
    buf.seek(0)
    today = date.today().isoformat()
    filename = f"cuotas_{today}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------- Recurring ---------------------

@app.post("/recurring/{gid}/confirm")
def recurring_confirm(request: Request, gid: int):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        conn.execute(
            "UPDATE recurring_groups SET status = 'confirmed' WHERE id = %s AND user_id = %s",
            (gid, user_id),
        )
        from finanzas.recurring import link_transactions
        link_transactions(conn, user_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/recurring/{gid}/reject")
def recurring_reject(request: Request, gid: int):
    user = auth.require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user_id: str = user["sub"]

    with db.connect() as conn:
        conn.execute(
            "UPDATE recurring_groups SET status = 'rejected' WHERE id = %s AND user_id = %s",
            (gid, user_id),
        )
    return RedirectResponse(url="/", status_code=303)


# --------------------- Charts (Plotly server-side) ---------------------

def _plot_treemap(cat_dist: list[tuple[int | None, str, str, float]],
                  div_id: str = "treemap-chart") -> str:
    if not cat_dist:
        return "<p class='text-slate-400 text-sm'>Sin datos para mostrar.</p>"
    ids = [c[0] if c[0] is not None else 0 for c in cat_dist]
    names = [c[1] for c in cat_dist]
    colors = [c[2] for c in cat_dist]
    values = [c[3] for c in cat_dist]
    fig = go.Figure(go.Treemap(
        labels=names,
        parents=[""] * len(names),
        values=values,
        marker={"colors": colors},
        customdata=ids,
        textinfo="label+percent root",
        hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0), height=320,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig.to_html(
        include_plotlyjs="cdn", full_html=False,
        config={"displayModeBar": False},
        div_id=div_id,
    )


def _plot_trend(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "<p class='text-slate-400 text-sm'>Sin datos.</p>"
    months = [m for m, _ in trend]
    vals = [v for _, v in trend]
    fig = go.Figure(go.Scatter(
        x=months, y=vals, mode="lines+markers",
        line=dict(color="#3b82f6", width=3),
        marker=dict(size=8),
        hovertemplate="<b>%{x}</b><br>$%{y:,.0f}<extra></extra>",
        fill="tozeroy", fillcolor="rgba(59,130,246,0.1)",
    ))
    fig.update_layout(
        margin=dict(l=40, r=10, t=10, b=40), height=240,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#94a3b8"),
        xaxis=dict(showgrid=False),
        yaxis=dict(tickformat=",.0f", gridcolor="rgba(148,163,184,0.2)"),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, config={"displayModeBar": False})


def _plot_cuotas(cuotas: list[tuple[str, float]]) -> str:
    if not cuotas or all(v == 0 for _, v in cuotas):
        return "<p class='text-slate-400 text-sm'>No tenés cuotas pendientes 🎉</p>"
    months = [m for m, _ in cuotas]
    vals = [v for _, v in cuotas]
    fig = go.Figure(go.Bar(
        x=months, y=vals,
        marker_color="#a855f7",
        text=[f"${v:,.0f}" if v > 0 else "" for v in vals],
        textposition="outside",
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>$%{y:,.0f}<extra></extra>",
    ))
    max_val = max(vals) if vals else 0
    fig.update_layout(
        margin=dict(l=40, r=20, t=40, b=40),
        height=260,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#94a3b8"),
        xaxis=dict(showgrid=False),
        yaxis=dict(tickformat=",.0f", showgrid=True, gridcolor="rgba(148,163,184,0.2)",
                   range=[0, max_val * 1.2] if max_val > 0 else None),
        showlegend=False,
        uniformtext=dict(minsize=10, mode="show"),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, config={"displayModeBar": False})
