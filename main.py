import os
import uuid
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Header, Request, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    init_db,
    insert_transaksi,
    get_transaksi_list,
    get_transaksi_by_id,
    get_summary,
    get_bank_breakdown,
    get_cashflow_chart,
    get_account_balances,
    update_transaksi,
    delete_transaksi,
    get_journals,
    reconcile_account_balance,
    get_email_agent_config,
    update_email_agent_config,
    get_email_logs,
    get_db_connection,
    create_internal_transfer,
    link_transaction_as_transfer,
)
from vision_extractor import extract_receipt_data
from bot import start_telegram_bot, RECEIPTS_DIR, get_notify_chat_id, send_message
from email_worker import (
    start_email_agent_worker,
    trigger_imap_sync_once,
    process_raw_email_payload,
    parse_mime_bytes,
)
from drive_manager import (
    get_auth_url,
    exchange_code_for_token,
    is_drive_connected,
    download_drive_file_stream,
    upload_receipt_to_drive
)
from backup_manager import (
    perform_database_backup,
    get_backup_status,
    get_latest_backup_filepath,
    start_database_backup_worker,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure tables exist
    init_db()
    # Start bot polling and email agent polling in background tasks
    bot_task = asyncio.create_task(start_telegram_bot())
    email_task = asyncio.create_task(start_email_agent_worker())
    backup_task = asyncio.create_task(start_database_backup_worker())
    yield
    # Shutdown
    bot_task.cancel()
    email_task.cancel()
    backup_task.cancel()
app = FastAPI(title="Pitih API", version="1.0.0", lifespan=lifespan)

# Allow CORS for pitih-web frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



class TransactionUpdate(BaseModel):
    tanggal: Optional[str] = None
    jenis: Optional[str] = None
    kategori: Optional[str] = None
    nominal: Optional[float] = None
    akun: Optional[str] = None
    merchant: Optional[str] = None
    catatan: Optional[str] = None
    ref_id: Optional[str] = None
    biaya_admin: Optional[float] = None
    transfer_pair_id: Optional[int] = None

class BatchDelete(BaseModel):
    ids: List[int]

class BatchUpdate(BaseModel):
    ids: List[int]
    kategori: Optional[str] = None


class InternalTransferPayload(BaseModel):
    from_akun: str
    to_akun: str
    nominal: float
    biaya_admin: Optional[float] = 0.0
    tanggal: Optional[str] = None
    catatan: Optional[str] = None

class LinkTransferPayload(BaseModel):
    to_akun: str
    biaya_admin: Optional[float] = 0.0

class EmailWebhookPayload(BaseModel):
    message_id: Optional[str] = None
    sender: str = ""
    subject: str = ""
    body_text: str = ""
    body_html: Optional[str] = None
    received_at: Optional[str] = None
    raw_mime: Optional[str] = None

class EmailConfigUpdatePayload(BaseModel):
    agent_email: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_user: Optional[str] = None
    imap_password: Optional[str] = None
    polling_interval_sec: Optional[int] = None
    enabled: Optional[bool] = None
    allowed_forwarders: Optional[str] = None
    telegram_notify_chat_id: Optional[str] = None

class ReconcilePayload(BaseModel):
    akun: str
    actual_balance: float
    catatan: Optional[str] = "Rekonsiliasi Saldo Rekening"
    nama: Optional[str] = None
    no_rekening: Optional[str] = None
@app.get("/api/health")
def health():
    return {"status": "ok", "service": "pitih-api", "timestamp": datetime.now().isoformat()}
@app.get("/api/drive/status")
def drive_status():
    return {
        "connected": is_drive_connected(),
        "folder_id": os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
    }

@app.get("/api/auth/login")
def auth_login():
    import urllib.parse
    client_id = os.getenv("GMAIL_CLIENT_ID", "")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "https://pitih.haluih.id/api/drive/callback")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "state": "login"
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)

@app.get("/api/drive/auth")
def drive_auth():
    try:
        url = get_auth_url()
        return RedirectResponse(url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal membuat URL otorisasi: {e}")

@app.get("/api/drive/callback", response_class=HTMLResponse)
def drive_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    import httpx
    import json
    
    if error:
        return f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"><title>Error Login — Pitih</title></head>
        <body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#09090b;color:#fafafa;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;">
            <div style="background:#18181b;border:1px solid #27272a;border-radius:16px;padding:32px;max-width:400px;text-align:center;">
                <h2 style="color:#e11d48;margin-bottom:12px;">❌ Otorisasi Dibatalkan</h2>
                <p style="color:#a1a1aa;font-size:14px;margin-bottom:24px;">{error}</p>
                <a href="/" style="display:inline-block;background:#27272a;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:14px;">Kembali ke Halaman Masuk</a>
            </div>
        </body>
        </html>
        """
    if not code:
        return "<h3>Parameter 'code' tidak ditemukan</h3>"

    # Alur 1: Login Dashboard Google Sign-In
    if state == "login":
        client_id = os.getenv("GMAIL_CLIENT_ID", "")
        client_secret = os.getenv("GMAIL_CLIENT_SECRET", "")
        redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "https://pitih.haluih.id/api/drive/callback")
        
        token_url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri
        }
        try:
            resp = httpx.post(token_url, data=data, timeout=20.0)
            if resp.status_code != 200:
                return f"<h3>Gagal verifikasi Google: {resp.text}</h3>"
            tok_data = resp.json()
            access_token = tok_data.get("access_token")
            
            u_resp = httpx.get("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=15.0)
            if u_resp.status_code != 200:
                return "<h3>Gagal mengambil profil akun Google</h3>"
                
            prof = u_resp.json()
            user_email = prof.get("email", "").lower().strip()
            user_name = prof.get("name") or prof.get("given_name") or "Pengguna"
            user_picture = prof.get("picture", "")
            
            allowed_raw = os.getenv("ALLOWED_EMAILS", "ridhalfajri00@gmail.com,tuanridhal@gmail.com")
            allowed = [e.strip().lower() for e in allowed_raw.split(",") if e.strip()]
            allowed_prefixes = ["ridhalfajri00", "tuanridhal"]
            user_prefix = user_email.split("@")[0] if "@" in user_email else user_email

            is_allowed = (user_email in allowed) or (user_prefix in allowed_prefixes)
            
            if not is_allowed:
                return f"""
                <!DOCTYPE html>
                <html>
                <head><meta charset="utf-8"><title>Akses Ditolak — Pitih</title></head>
                <body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#09090b;color:#fafafa;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;">
                    <div style="background:#18181b;border:1px solid #ef4444;border-radius:16px;padding:32px;max-width:440px;text-align:center;">
                        <h2 style="color:#ef4444;margin-bottom:12px;">❌ Akses Ditolak</h2>
                        <p style="color:#d4d4d8;font-size:14px;line-height:1.6;margin-bottom:16px;">
                            Akun Google <b>{user_email}</b> tidak memiliki izin akses ke dashboard Pitih ini.
                        </p>
                        <p style="color:#71717a;font-size:12px;margin-bottom:24px;">
                            Akses dibatasi khusus untuk email terdaftar: <code>ridhalfajri00</code> & <code>tuanridhal</code>.
                        </p>
                        <a href="/api/auth/login" style="display:inline-block;background:#3b82f6;color:#fff;font-weight:600;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:14px;">Ganti Akun Google</a>
                    </div>
                </body>
                </html>
                """
                
            user_payload = json.dumps({
                "name": user_name,
                "email": user_email,
                "role": "Owner",
                "picture": user_picture
            })
            return f"""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Masuk Berhasil — Pitih</title></head>
            <body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#09090b;color:#fafafa;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;">
                <div style="text-align:center;">
                    <div style="width:40px;height:40px;border:3px solid #22c55e;border-top-color:transparent;border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 16px;"></div>
                    <p style="color:#a1a1aa;font-size:14px;">Selamat datang, <b>{user_name}</b>! Mengalihkan ke dashboard...</p>
                </div>
                <style>@keyframes spin {{ to {{ transform: rotate(360deg); }} }}</style>
                <script>
                    const user = {user_payload};
                    localStorage.setItem("finscan-auth", JSON.stringify(user));
                    sessionStorage.setItem("finscan-auth", JSON.stringify(user));
                    window.location.href = "/dashboard";
                </script>
            </body>
            </html>
            """
        except Exception as e:
            return f"<h3>Error login Google: {e}</h3>"

    # Alur 2: Otorisasi Google Drive Storage
    success = exchange_code_for_token(code)
    if success:
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Google Drive Terhubung — Pitih</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #09090b; color: #fafafa; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
                .card { background: #18181b; border: 1px solid #27272a; border-radius: 16px; padding: 32px; max-width: 420px; text-align: center; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
                h1 { font-size: 20px; margin-bottom: 12px; color: #22c55e; }
                p { font-size: 14px; color: #a1a1aa; line-height: 1.5; margin-bottom: 24px; }
                a { display: inline-block; background: #22c55e; color: #000; font-weight: 600; text-decoration: none; padding: 10px 20px; border-radius: 8px; font-size: 14px; }
                a:hover { background: #16a34a; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>🎉 Google Drive Berhasil Terhubung!</h1>
                <p>Bot Pitih sekarang sudah terhubung ke Google Drive pribadi Anda. Setiap foto struk yang dikirim ke bot Telegram akan otomatis tersimpan langsung ke folder Drive dan tidak memakan memori VPS.</p>
                <a href="/dashboard">Kembali ke Dashboard</a>
            </div>
        </body>
        </html>
        """
    else:
        return """
        <html>
        <body style="font-family:sans-serif;text-align:center;padding:50px;">
            <h1 style="color:#e11d48;">❌ Gagal Menyimpan Token</h1>
            <p>Terjadi kesalahan saat menukar kode otorisasi Google.</p>
            <p><a href="/api/drive/auth">Coba lagi</a></p>
        </body>
        </html>
        """


@app.get("/api/receipts/drive/{file_id}")
def get_drive_receipt(file_id: str):
    fh = download_drive_file_stream(file_id)
    if not fh:
        raise HTTPException(status_code=404, detail="File struk tidak ditemukan di Google Drive")
    return StreamingResponse(
        fh,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"}
    )

@app.get("/api/summary")
def summary(
    range: Optional[str] = Query("all", description="today, 7d, 30d, this_month, month, all"),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    return get_summary(range, start_date=start_date, end_date=end_date)

@app.get("/api/transactions")
def transactions(
    search: Optional[str] = None,
    bank: Optional[str] = None,
    jenis: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100)
):
    return get_transaksi_list(
        search=search,
        bank=bank,
        jenis=jenis,
        start_date=start_date,
        end_date=end_date,
        page=page,
        limit=limit
    )

@app.get("/api/charts/cashflow")
def chart_cashflow(range: Optional[str] = "30d"):
    days = 30
    if range == "7d":
        days = 7
    elif range == "90d":
        days = 90
    elif range == "365d":
        days = 365
    return get_cashflow_chart(days)

@app.get("/api/charts/breakdown-bank")
def chart_breakdown_bank():
    return get_bank_breakdown()

@app.get("/api/accounts/balances")
def accounts_balances():
    return get_account_balances()

async def process_uploaded_receipt_background(filepath: str, filename: str) -> None:
    try:
        extracted = await extract_receipt_data(filepath)
    except Exception as e:
        print(f"[UploadReceipt] Error extracting receipt data: {e}")
        return

    if not extracted.get("is_receipt", True) or float(extracted.get("nominal", 0)) <= 0:
        print(f"[UploadReceipt] Gambar {filename} bukan struk transaksi finansial, diabaikan.")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        try:
            chat_id = get_notify_chat_id()
            if chat_id:
                msg = (
                    "⚠️ *Gambar Tidak Terdeteksi Sebagai Struk Transaksi*\n\n"
                    "Screenshot yang dikirimkan tidak mengandung bukti transfer atau pembayaran yang sah.\n\n"
                    "ℹ️ *Tidak ada transaksi yang dicatat ke sistem Pitih.*"
                )
                await send_message(chat_id, msg)
        except Exception:
            pass
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tanggal = extracted.get("tanggal", now_str)
    if tanggal.endswith("12:00:00"):
        tanggal = f"{tanggal[:10]} {now_str[11:]}"
    jenis = extracted.get("jenis", "keluar")
    kategori = extracted.get("kategori", "Lainnya")
    nominal = float(extracted.get("nominal", 0))
    akun = extracted.get("akun", "Bank")
    merchant = extracted.get("merchant", "-")
    catatan = extracted.get("catatan", "Upload Struk iPhone")
    ref_id = extracted.get("ref_id", "-")
    confidence = float(extracted.get("ai_confidence", 0.98))

    drive_res = upload_receipt_to_drive(filepath, filename)
    image_url = drive_res.get("image_url") if (drive_res and drive_res.get("image_url")) else f"/api/receipts/{filename}"

    tx_id = insert_transaksi(
        tanggal=tanggal,
        jenis=jenis,
        kategori=kategori,
        nominal=nominal,
        akun=akun,
        merchant=merchant,
        catatan=catatan,
        ref_id=ref_id,
        image_url=image_url,
        ai_confidence=confidence,
    )

    try:
        chat_id = get_notify_chat_id()
        if chat_id:
            fmt_nom = f"Rp {nominal:,.0f}".replace(",", ".")
            badge = "🟢 Uang Masuk" if jenis == "masuk" else "🔴 Uang Keluar"
            msg = (
                f"📸 *Struk Berhasil Dicatat dari iPhone!* (#{tx_id})\n\n"
                f"• *Tipe:* {badge}\n"
                f"• *Nominal:* *{fmt_nom}*\n"
                f"• *Merchant/Pengirim:* *{merchant}*\n"
                f"• *Kategori:* {kategori}\n"
                f"• *Akun:* {akun}\n"
                f"• *Catatan:* {catatan}\n\n"
                f"💡 _Balas (reply) pesan ini jika ingin menambahkan rincian keperluan belanja._"
            )
            await send_message(chat_id, msg)
    except Exception as alert_err:
        print(f"[UploadReceipt] Telegram alert error: {alert_err}")


@app.post("/api/upload-receipt")
async def upload_receipt(
    request: Request,
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
):
    content_type = (request.headers.get("content-type") or "").lower()
    file_bytes = b""
    ext = ".jpg"

    # 1. Standard multipart form file (from Web Dashboard)
    if file and file.filename:
        file_bytes = await file.read()
        ext = os.path.splitext(file.filename)[1] or ".jpg"

    # 2. Form data (any field name or urlencoded base64 from iOS Shortcuts)
    elif "multipart/form-data" in content_type:
        try:
            form = await request.form()
            for key, val in form.items():
                if hasattr(val, "read"):
                    file_bytes = await val.read()
                    ext = os.path.splitext(getattr(val, "filename", "receipt.jpg"))[1] or ".jpg"
                    break
                elif isinstance(val, str) and len(val) > 100:
                    try:
                        import base64
                        clean_b64 = re.sub(r"^data:image/[a-zA-Z]+;base64,", "", val)
                        file_bytes = base64.b64decode(clean_b64)
                    except Exception:
                        pass
        except Exception:
            pass

    # 3. Raw binary body (iOS Shortcuts Request Body: File or octet-stream or image/*)
    if not file_bytes:
        raw_body = await request.body()
        if raw_body and len(raw_body) > 50:
            file_bytes = raw_body
            if "png" in content_type or file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = ".png"
            elif "webp" in content_type or file_bytes.startswith(b"RIFF"):
                ext = ".webp"
            else:
                ext = ".jpg"

    if not file_bytes:
        raise HTTPException(status_code=400, detail="File gambar struk tidak ditemukan dalam request")

    filename = f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    filepath = os.path.join(RECEIPTS_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(file_bytes)

    # Check if request is from iOS Shortcuts or requested async
    is_mobile_shortcut = (
        not (file and file.filename)
        or request.query_params.get("async") == "true"
        or "shortcuts" in (request.headers.get("user-agent") or "").lower()
        or request.headers.get("x-client") == "shortcuts"
    )

    if is_mobile_shortcut:
        background_tasks.add_task(process_uploaded_receipt_background, filepath, filename)
        return {
            "status": "processing",
            "message": "Struk berhasil diterima dan sedang diproses AI di latar belakang",
            "filename": filename,
        }

    # Synchronous processing for Web Dashboard
    extracted = await extract_receipt_data(filepath)
    if not extracted.get("is_receipt", True) or float(extracted.get("nominal", 0)) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Gambar tidak terdeteksi sebagai bukti transfer atau struk pembayaran valid",
        )
    tanggal = extracted.get("tanggal", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    jenis = extracted.get("jenis", "keluar")
    kategori = extracted.get("kategori", "Lainnya")
    nominal = float(extracted.get("nominal", 0))
    akun = extracted.get("akun", "Bank")
    merchant = extracted.get("merchant", "-")
    catatan = extracted.get("catatan", "Upload Struk Manual")
    ref_id = extracted.get("ref_id", "-")
    confidence = float(extracted.get("ai_confidence", 0.98))

    drive_res = upload_receipt_to_drive(filepath, filename)
    image_url = drive_res.get("image_url") if (drive_res and drive_res.get("image_url")) else f"/api/receipts/{filename}"

    tx_id = insert_transaksi(
        tanggal=tanggal,
        jenis=jenis,
        kategori=kategori,
        nominal=nominal,
        akun=akun,
        merchant=merchant,
        catatan=catatan,
        ref_id=ref_id,
        image_url=image_url,
        ai_confidence=confidence,
    )

    return {
        "id": tx_id,
        "tanggal": tanggal,
        "jenis": jenis,
        "kategori": kategori,
        "nominal": nominal,
        "akun": akun,
        "merchant": merchant,
        "catatan": catatan,
        "ref_id": ref_id,
        "image_url": image_url,
        "ai_confidence": confidence,
    }

@app.put("/api/transactions/{tx_id}")
def update_transaction(tx_id: int, payload: TransactionUpdate):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    success = update_transaksi(tx_id, updates)
    if not success:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    return {"success": True, "id": tx_id}

@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int):
    success = delete_transaksi(tx_id)
    if not success:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan")
    return {"success": True, "id": tx_id}

@app.post("/api/transactions/batch-delete")
def batch_delete(payload: BatchDelete):
    deleted = 0
    for tx_id in payload.ids:
        if delete_transaksi(tx_id):
            deleted += 1
    return {"success": True, "deleted_count": deleted}

@app.post("/api/transactions/batch-update")
def batch_update(payload: BatchUpdate):
    if not payload.kategori:
        return {"success": False, "message": "Tidak ada perubahan"}
    updated = 0
    for tx_id in payload.ids:
        if update_transaksi(tx_id, {"kategori": payload.kategori}):
            updated += 1
    return {"success": True, "updated_count": updated}


def _build_email_status_response() -> Dict[str, Any]:
    cfg = get_email_agent_config()
    has_pwd = bool((cfg.get("imap_password") or "").strip())
    safe_cfg = {
        **cfg,
        "enabled": bool(cfg.get("enabled", 1)),
        "has_imap_password": has_pwd,
        "imap_password": "••••••••" if has_pwd else "",
    }

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END), 0) as processed,
                COALESCE(SUM(CASE WHEN status = 'duplicate' THEN 1 ELSE 0 END), 0) as duplicate,
                COALESCE(SUM(CASE WHEN status = 'verification_code' THEN 1 ELSE 0 END), 0) as verification_code,
                COALESCE(SUM(CASE WHEN status = 'ignored' THEN 1 ELSE 0 END), 0) as ignored,
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) as failed
            FROM email_inbox_log
            """
        )
        stats_row = dict(cursor.fetchone() or {})
        cursor.execute(
            """
            SELECT id, sender, subject, received_at, verification_code, verification_url
            FROM email_inbox_log
            WHERE status = 'verification_code'
            ORDER BY id DESC
            LIMIT 1
            """
        )
        verif_row = cursor.fetchone()
        latest_verification = dict(verif_row) if verif_row else None
    finally:
        conn.close()

    logs = get_email_logs(30)
    return {
        "config": safe_cfg,
        "webhook_url": "https://pitih.haluih.id/api/email/webhook",
        "stats": stats_row,
        "latest_verification": latest_verification,
        "logs": logs,
    }

@app.get("/api/journals")
def journals(
    akun: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
):
    return get_journals(
        akun=akun,
        start_date=start_date,
        end_date=end_date,
        page=page,
        limit=limit,
    )

@app.post("/api/accounts/reconcile")
def reconcile_account(payload: ReconcilePayload):
    if not payload.akun or not payload.akun.strip():
        raise HTTPException(status_code=400, detail="Nama akun tidak boleh kosong")
    return reconcile_account_balance(
        akun=payload.akun.strip(),
        actual_balance=payload.actual_balance,
        catatan=payload.catatan or "Rekonsiliasi Saldo Rekening",
        nama=payload.nama,
        no_rekening=payload.no_rekening,
    )

@app.post("/api/accounts/transfer")
def accounts_transfer(payload: InternalTransferPayload):
    if not payload.from_akun or not payload.to_akun:
        raise HTTPException(status_code=400, detail="Rekening asal dan tujuan harus diisi")
    if payload.nominal <= 0:
        raise HTTPException(status_code=400, detail="Nominal transfer harus lebih besar dari 0")
    try:
        res = create_internal_transfer(
            from_akun=payload.from_akun,
            to_akun=payload.to_akun,
            nominal=payload.nominal,
            biaya_admin=payload.biaya_admin or 0.0,
            tanggal=payload.tanggal,
            catatan=payload.catatan,
            source="manual",
        )
        balances = get_account_balances()
        from_b = next((b for b in balances if b["akun"] == res["from_akun"]), None)
        to_b = next((b for b in balances if b["akun"] == res["to_akun"]), None)
        return {
            "success": True,
            "transfer": res,
            "saldo_from": from_b["saldo"] if from_b else None,
            "saldo_to": to_b["saldo"] if to_b else None,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/transactions/{tx_id}/link-transfer")
def link_transaction_transfer(tx_id: int, payload: LinkTransferPayload):
    if not payload.to_akun:
        raise HTTPException(status_code=400, detail="Rekening tujuan harus dipilih")
    try:
        res = link_transaction_as_transfer(
            tx_id=tx_id,
            to_akun=payload.to_akun,
            biaya_admin=payload.biaya_admin,
        )
        return {"success": True, "transfer": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/email/status")
def email_status():
    return _build_email_status_response()

@app.put("/api/email/config")
def update_email_config(payload: EmailConfigUpdatePayload):
    raw = payload.model_dump()
    updates: Dict[str, Any] = {}
    for k, v in raw.items():
        if v is None:
            continue
        if k == "imap_password" and v in ("••••••••", ""):
            continue
        updates[k] = v
    update_email_agent_config(updates)
    return _build_email_status_response()

@app.post("/api/email/sync")
async def sync_email_inbox():
    return await trigger_imap_sync_once()

@app.post("/api/email/webhook")
async def email_webhook(
    payload: EmailWebhookPayload,
    secret: Optional[str] = Query(None),
    x_webhook_secret: Optional[str] = Header(None),
):
    expected_secret = (os.getenv("EMAIL_WEBHOOK_SECRET") or "").strip()
    if expected_secret:
        provided = (x_webhook_secret or secret or "").strip()
        if provided != expected_secret:
            raise HTTPException(status_code=401, detail="Webhook secret tidak valid")

    if payload.raw_mime:
        parsed = parse_mime_bytes(payload.raw_mime.encode("utf-8", errors="replace"))
        return await process_raw_email_payload(
            message_id=parsed["message_id"] or payload.message_id,
            sender=parsed["sender"] or payload.sender,
            subject=parsed["subject"] or payload.subject,
            body_text=parsed["body_text"] or payload.body_text,
            body_html=parsed["body_html"] or payload.body_html,
            received_at=parsed["received_at"] or payload.received_at,
        )

    return await process_raw_email_payload(
        message_id=payload.message_id,
        sender=payload.sender,
        subject=payload.subject,
        body_text=payload.body_text,
        body_html=payload.body_html,
        received_at=payload.received_at,
    )

@app.post("/api/email/simulate")
async def email_simulate(payload: EmailWebhookPayload):
    return await process_raw_email_payload(
        message_id=payload.message_id,
        sender=payload.sender,
        subject=payload.subject,
        body_text=payload.body_text,
        body_html=payload.body_html,
        received_at=payload.received_at,
    )

@app.get("/api/backup/status")
def backup_status():
    return get_backup_status()

@app.post("/api/backup/now")
def backup_now():
    return perform_database_backup(upload_drive=True)

@app.get("/api/backup/download")
def backup_download():
    filepath = get_latest_backup_filepath()
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File backup database tidak ditemukan")
    filename = os.path.basename(filepath)
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type="application/x-sqlite3",
    )
# Serve receipts static files (mounted after specific route handlers)
os.makedirs(RECEIPTS_DIR, exist_ok=True)
app.mount("/api/receipts", StaticFiles(directory=RECEIPTS_DIR), name="receipts")
