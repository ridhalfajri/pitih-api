import asyncio
import email
import hashlib
import imaplib
from datetime import datetime
from email.header import decode_header
from typing import Dict, Any, Optional, List

from database import (
    compute_tx_fingerprint,
    is_duplicate_transaction,
    insert_transaksi,
    get_account_balances,
    get_email_agent_config,
    update_email_agent_config,
    log_email_event,
    get_db_connection,
    detect_destination_account,
    create_internal_transfer,
)
from email_parser import parse_incoming_email, mask_sensitive_text, strip_html_to_text
from bot import notify_email_transaction, notify_gmail_verification, notify_internal_transfer


def _decode_mime_words(raw_val: Optional[str]) -> str:
    if not raw_val:
        return ""
    decoded_parts: List[str] = []
    for part, enc in decode_header(raw_val):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(enc or "utf-8", errors="replace"))
            except Exception:
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(str(part))
    return "".join(decoded_parts).strip()


def parse_mime_bytes(raw_bytes: bytes) -> Dict[str, Any]:
    msg = email.message_from_bytes(raw_bytes)
    message_id = (msg.get("Message-ID") or "").strip()
    sender = _decode_mime_words(msg.get("From"))
    subject = _decode_mime_words(msg.get("Subject"))
    received_at = _decode_mime_words(msg.get("Date"))

    body_text_parts: List[str] = []
    body_html_parts: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = (part.get_content_type() or "").lower()
            content_disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in content_disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")
            if content_type == "text/plain":
                body_text_parts.append(decoded)
            elif content_type == "text/html":
                body_html_parts.append(decoded)
    else:
        content_type = (msg.get_content_type() or "").lower()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")
            if content_type == "text/html":
                body_html_parts.append(decoded)
            else:
                body_text_parts.append(decoded)

    return {
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "received_at": received_at,
        "body_text": "\n".join(body_text_parts).strip(),
        "body_html": "\n".join(body_html_parts).strip() or None,
    }


def _is_message_id_already_logged(message_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, status, transaksi_id
            FROM email_inbox_log
            WHERE message_id = ? AND status IN ('processed', 'duplicate', 'verification_code')
            LIMIT 1
            """,
            (message_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def process_raw_email_payload(
    message_id: Optional[str],
    sender: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    received_at: Optional[str] = None,
) -> Dict[str, Any]:
    effective_received_at = (received_at or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_msg_id = (message_id or "").strip()
    if not clean_msg_id:
        raw_seed = f"{sender}|{subject}|{effective_received_at}|{(body_text or body_html or '')[:200]}"
        clean_msg_id = "msg-" + hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:24]

    existing_log = _is_message_id_already_logged(clean_msg_id)
    if existing_log:
        return {
            "status": "duplicate",
            "message_id": clean_msg_id,
            "transaksi_id": existing_log.get("transaksi_id"),
            "reason": "Message-ID sudah pernah diproses",
        }

    preview_source = body_text or (strip_html_to_text(body_html) if body_html else "") or subject
    masked_preview = mask_sensitive_text(preview_source)

    try:
        parsed = await parse_incoming_email(
            sender=sender,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            email_date=effective_received_at,
        )
    except Exception as e:
        log_email_event(
            message_id=clean_msg_id,
            sender=sender,
            subject=subject,
            received_at=effective_received_at,
            status="failed",
            masked_preview=masked_preview,
            error_message=str(e),
        )
        return {
            "status": "failed",
            "message_id": clean_msg_id,
            "error": str(e),
        }

    kind = parsed.get("kind")

    if kind == "verification":
        code = parsed.get("code")
        url = parsed.get("url")
        log_email_event(
            message_id=clean_msg_id,
            sender=sender,
            subject=subject,
            received_at=effective_received_at,
            status="verification_code",
            bank_detected="Gmail Auto-Forward",
            verification_code=code,
            verification_url=url,
            masked_preview=masked_preview,
        )
        await notify_gmail_verification(code, url, sender)
        return {
            "status": "verification_code",
            "message_id": clean_msg_id,
            "verification_code": code,
            "verification_url": url,
        }

    if kind == "ignored":
        reason = parsed.get("reason", "Bukan email mutasi transaksi finansial")
        log_email_event(
            message_id=clean_msg_id,
            sender=sender,
            subject=subject,
            received_at=effective_received_at,
            status="ignored",
            masked_preview=masked_preview,
            error_message=reason,
        )
        return {
            "status": "ignored",
            "message_id": clean_msg_id,
            "reason": reason,
        }

    # kind == "transaction"
    d = parsed["data"]
    fp = compute_tx_fingerprint(
        akun=d["akun"],
        jenis=d["jenis"],
        nominal=d["nominal"],
        tanggal=d["tanggal"],
        ref_id=d.get("ref_id"),
        merchant=d.get("merchant"),
    )

    dup_tx_id = is_duplicate_transaction(clean_msg_id, fp)
    if dup_tx_id is not None:
        log_email_event(
            message_id=clean_msg_id,
            sender=sender,
            subject=subject,
            received_at=effective_received_at,
            status="duplicate",
            bank_detected=d["akun"],
            transaksi_id=dup_tx_id,
            masked_preview=masked_preview,
        )
        return {
            "status": "duplicate",
            "message_id": clean_msg_id,
            "transaksi_id": dup_tx_id,
            "fingerprint": fp,
        }
    dest_akun = None
    if d["jenis"] == "keluar":
        dest_akun = detect_destination_account(
            source_akun=d["akun"],
            text=preview_source,
            merchant=d.get("merchant", ""),
            catatan=d.get("catatan", ""),
        )

    if dest_akun:
        transfer_res = create_internal_transfer(
            from_akun=d["akun"],
            to_akun=dest_akun,
            nominal=float(d["nominal"]),
            biaya_admin=float(d.get("biaya_admin") or 0.0),
            tanggal=d["tanggal"],
            catatan=d.get("catatan"),
            ref_id=d.get("ref_id"),
            source="email",
            email_message_id=clean_msg_id,
        )
        tx_out_id = transfer_res["out_id"]
        tx_in_id = transfer_res["in_id"]

        log_email_event(
            message_id=clean_msg_id,
            sender=sender,
            subject=subject,
            received_at=effective_received_at,
            status="processed",
            bank_detected=f"{d['akun']} -> {dest_akun}",
            transaksi_id=tx_out_id,
            masked_preview=masked_preview,
        )

        balances = get_account_balances()
        from_info = next((b for b in balances if b["akun"] == d["akun"]), None)
        to_info = next((b for b in balances if b["akun"] == dest_akun), None)
        saldo_from = float(from_info["saldo"]) if from_info else 0.0
        saldo_to = float(to_info["saldo"]) if to_info else 0.0

        await notify_internal_transfer(
            from_akun=d["akun"],
            to_akun=dest_akun,
            nominal=float(d["nominal"]),
            biaya_admin=float(d.get("biaya_admin") or 0.0),
            saldo_from=saldo_from,
            saldo_to=saldo_to,
            out_id=tx_out_id,
            in_id=tx_in_id,
        )

        return {
            "status": "processed",
            "message_id": clean_msg_id,
            "transaksi_id": tx_out_id,
            "in_transaksi_id": tx_in_id,
            "transfer": transfer_res,
            "parser": parsed.get("parser", "deterministic"),
            "saldo_from": saldo_from,
            "saldo_to": saldo_to,
        }

    tx_id = insert_transaksi(
        tanggal=d["tanggal"],
        jenis=d["jenis"],
        kategori=d["kategori"],
        nominal=float(d["nominal"]),
        akun=d["akun"],
        merchant=d.get("merchant") or "-",
        catatan=d.get("catatan") or f"Notifikasi Email {d['akun']}",
        ref_id=d.get("ref_id") or "-",
        image_url="",
        ai_confidence=float(d.get("ai_confidence") or 0.98),
        source="email",
        email_message_id=clean_msg_id,
        fingerprint=fp,
        saldo_akhir=d.get("saldo_akhir"),
        biaya_admin=float(d.get("biaya_admin") or 0.0),
    )

    log_email_event(
        message_id=clean_msg_id,
        sender=sender,
        subject=subject,
        received_at=effective_received_at,
        status="processed",
        bank_detected=d["akun"],
        transaksi_id=tx_id,
        masked_preview=masked_preview,
    )

    balances = get_account_balances()
    acc_info = next((b for b in balances if b["akun"] == d["akun"]), None)
    saldo_sesudah = float(acc_info["saldo"]) if acc_info else float(d["nominal"])
    selisih_recon = float(acc_info["selisih_rekonsiliasi"]) if acc_info else 0.0

    tx_obj = {
        **d,
        "id": tx_id,
        "source": "email",
        "email_message_id": clean_msg_id,
    }
    await notify_email_transaction(tx_obj, saldo_sesudah, selisih_recon)

    return {
        "status": "processed",
        "message_id": clean_msg_id,
        "transaksi_id": tx_id,
        "parser": parsed.get("parser", "deterministic"),
        "transaction": tx_obj,
        "saldo_sesudah": saldo_sesudah,
    }


def _fetch_unseen_imap_messages(
    host: str, port: int, user: str, password: str, max_messages: int = 25
) -> List[bytes]:
    mail = imaplib.IMAP4_SSL(host, port)
    try:
        mail.login(user, password)
        mail.select("INBOX")
        since_str = datetime.now().strftime("%d-%b-%Y")
        typ, data = mail.search(None, f'(UNSEEN SINCE "{since_str}")')
        if typ != "OK" or not data or not data[0]:
            return []
        msg_nums = data[0].split()[-max_messages:]
        raw_messages: List[bytes] = []
        for num in msg_nums:
            f_typ, f_data = mail.fetch(num, "(RFC822)")
            if f_typ == "OK" and f_data:
                for item in f_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                        raw_messages.append(item[1])
        return raw_messages
    finally:
        try:
            mail.logout()
        except Exception:
            pass


async def trigger_imap_sync_once() -> Dict[str, Any]:
    cfg = get_email_agent_config()
    imap_user = (cfg.get("imap_user") or cfg.get("agent_email") or "").strip()
    imap_password = (cfg.get("imap_password") or "").strip()
    imap_host = (cfg.get("imap_host") or "imap.gmail.com").strip()
    imap_port = int(cfg.get("imap_port") or 993)

    if not imap_user or not imap_password:
        return {
            "status": "not_configured",
            "synced": 0,
            "processed": 0,
            "duplicates": 0,
            "message": "Kredensial IMAP (email & App Password) belum dikonfigurasi.",
        }

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        raw_list = await asyncio.to_thread(
            _fetch_unseen_imap_messages, imap_host, imap_port, imap_user, imap_password, 25
        )
        processed = 0
        duplicates = 0
        verifications = 0
        for raw_bytes in raw_list:
            mime_parsed = parse_mime_bytes(raw_bytes)
            res = await process_raw_email_payload(
                message_id=mime_parsed["message_id"],
                sender=mime_parsed["sender"],
                subject=mime_parsed["subject"],
                body_text=mime_parsed["body_text"],
                body_html=mime_parsed["body_html"],
                received_at=mime_parsed["received_at"],
            )
            st = res.get("status")
            if st == "processed":
                processed += 1
            elif st == "duplicate":
                duplicates += 1
            elif st == "verification_code":
                verifications += 1

        update_email_agent_config({"last_sync_at": now_str, "last_error": None})
        return {
            "status": "ok",
            "synced": len(raw_list),
            "processed": processed,
            "duplicates": duplicates,
            "verifications": verifications,
            "last_sync_at": now_str,
        }
    except Exception as e:
        err_msg = str(e)
        update_email_agent_config({"last_sync_at": now_str, "last_error": err_msg})
        return {
            "status": "error",
            "synced": 0,
            "processed": 0,
            "error": err_msg,
        }


async def start_email_agent_worker() -> None:
    print("[EmailAgent] Background IMAP polling worker started.")
    while True:
        try:
            cfg = get_email_agent_config()
            enabled = bool(cfg.get("enabled", 1))
            interval = max(15, int(cfg.get("polling_interval_sec") or 30))
            if enabled and (cfg.get("imap_user") or cfg.get("agent_email")) and cfg.get("imap_password"):
                await trigger_imap_sync_once()
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[EmailAgent] Worker loop error: {e}")
            await asyncio.sleep(30)
