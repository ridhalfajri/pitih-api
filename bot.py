import os
import re
import asyncio
import uuid
import httpx
from datetime import datetime
from typing import Optional, Dict, Any
from database import (
    insert_transaksi,
    get_email_agent_config,
    get_db_connection,
    detect_destination_account,
    create_internal_transfer,
    link_transaction_as_transfer,
    get_transaksi_by_id,
    update_transaksi,
    check_category_budget_alert,
    get_account_balances,
    get_summary,
    get_budget_summary,
    get_subscriptions_summary,
    get_transaksi_list,
)
from email_parser import infer_category
from vision_extractor import extract_receipt_data
from drive_manager import upload_receipt_to_drive, is_drive_connected
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_PITIH", "")
RECEIPTS_DIR = os.getenv("RECEIPTS_DIR", "/opt/data/keuangan/receipts")
LAST_CHAT_ID_FILE = os.path.join(
    os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "/data/keuangan.db"))),
    "last_telegram_chat_id.txt",
)

os.makedirs(RECEIPTS_DIR, exist_ok=True)

def _save_last_chat_id(chat_id: int) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_CHAT_ID_FILE), exist_ok=True)
        with open(LAST_CHAT_ID_FILE, "w", encoding="utf-8") as f:
            f.write(str(chat_id))
    except Exception as e:
        print(f"[TelegramBot] Error saving last_chat_id: {e}")

def get_notify_chat_id() -> Optional[int]:
    try:
        cfg = get_email_agent_config()
        cid = (cfg.get("telegram_notify_chat_id") or "").strip()
        if cid:
            return int(cid)
    except Exception:
        pass
    env_cid = (os.getenv("TELEGRAM_NOTIFY_CHAT_ID") or "").strip()
    if env_cid:
        try:
            return int(env_cid)
        except ValueError:
            pass
    if os.path.exists(LAST_CHAT_ID_FILE):
        try:
            with open(LAST_CHAT_ID_FILE, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val:
                    return int(val)
        except Exception:
            pass
    return None

async def notify_email_transaction(
    tx: Dict[str, Any],
    saldo_sesudah: float,
    selisih_rekonsiliasi: Optional[float] = None,
) -> None:
    if not TELEGRAM_TOKEN:
        return
    chat_id = get_notify_chat_id()
    if not chat_id:
        return
    jenis = tx.get("jenis", "keluar")
    badge = "🟢 Uang Masuk" if jenis == "masuk" else "🔴 Uang Keluar"
    nominal = float(tx.get("nominal") or 0.0)
    fmt_nominal = f"Rp {nominal:,.0f}".replace(",", ".")
    fmt_saldo = f"Rp {saldo_sesudah:,.0f}".replace(",", ".")
    recon_line = ""
    if selisih_rekonsiliasi is not None and abs(selisih_rekonsiliasi) >= 1.0:
        fmt_diff = f"Rp {abs(selisih_rekonsiliasi):,.0f}".replace(",", ".")
        recon_line = f"\n⚠️ *Selisih Rekonsiliasi:* {fmt_diff}"

    catatan = tx.get("catatan", "")
    merchant = tx.get("merchant", "-")
    kategori = tx.get("kategori", "-")
    is_qris = "qris" in (catatan + " " + merchant).lower()

    budget_line = ""
    if jenis == "keluar":
        b_alert = check_category_budget_alert(kategori)
        if b_alert:
            tag_label = "MELEBIHI TARGET" if b_alert["is_overbudget"] else "Mendekati Batas (80%+)"
            fmt_sp = f"Rp {int(b_alert['terpakai']):,}".replace(",", ".")
            fmt_tg = f"Rp {int(b_alert['target']):,}".replace(",", ".")
            fmt_rem = f"Rp {int(abs(b_alert['sisa'])):,}".replace(",", ".")
            rem_txt = f"Sisa jatah: *{fmt_rem}*" if not b_alert["is_overbudget"] else f"Overbudget: *+{fmt_rem}*"
            budget_line = f"\n⚠️ *Anggaran ({tag_label})!*\nKategori *{b_alert['kategori']}* terpakai *{b_alert['persentase']}%* ({fmt_sp} dari target {fmt_tg}). {rem_txt}"

    if is_qris:
        msg = (
            f"📱 *Pembayaran QRIS Berhasil!* (#{tx.get('id', '-')})\n\n"
            f"• *Merchant/Toko:* *{merchant}*\n"
            f"• *Nominal:* *{fmt_nominal}*\n"
            f"• *Kategori:* {kategori}\n"
            f"• *Bank/Sumber:* {tx.get('akun', '-')}\n"
            f"• *No. Ref:* `{tx.get('ref_id', '-')}`\n"
            f"• *Saldo Berjalan:* *{fmt_saldo}*"
            f"{recon_line}"
            f"{budget_line}\n\n"
            f"💡 _Balas (reply) pesan ini untuk menambahkan rincian barang/keperluan belanja (contoh: \"Cuci sepatu & helm\")._"
        )
    else:
        msg = (
            f"📧 *Mutasi Email Bank Tercatat (#{tx.get('id', '-')})*\n\n"
            f"• *Tipe:* {badge}\n"
            f"• *Nominal:* *{fmt_nominal}*\n"
            f"• *Bank/Akun:* {tx.get('akun', '-')}\n"
            f"• *Merchant/Tujuan:* {merchant}\n"
            f"• *Kategori:* {kategori}\n"
            f"• *No Ref:* `{tx.get('ref_id', '-')}`\n"
            f"• *Saldo Berjalan:* *{fmt_saldo}*"
            f"{recon_line}"
            f"{budget_line}\n\n"
            f"📊 _Jurnal keuangan otomatis telah diperbarui di Pitih._"
        )
    await send_message(chat_id, msg)

async def notify_gmail_verification(
    code: Optional[str],
    url: Optional[str],
    sender: str,
) -> None:
    if not TELEGRAM_TOKEN:
        return
    chat_id = get_notify_chat_id()
    if not chat_id:
        return
    msg = (
        f"🔐 *Konfirmasi Auto-Forward Gmail Diterima!*\n\n"
        f"• *Pengirim:* `{sender}`\n"
        f"• *Kode Konfirmasi:* `{code or '-'}`\n"
        f"• *Link Verifikasi:* {url or '-'}\n\n"
        f"_Gunakan kode atau buka link di atas untuk mengaktifkan penerusan email bank otomatis ke Pitih._"
    )
    await send_message(chat_id, msg)

async def notify_internal_transfer(
    from_akun: str,
    to_akun: str,
    nominal: float,
    biaya_admin: float,
    saldo_from: float,
    saldo_to: float,
    out_id: int,
    in_id: int,
) -> None:
    if not TELEGRAM_TOKEN:
        return
    chat_id = get_notify_chat_id()
    if not chat_id:
        return
    fmt_nominal = f"Rp {nominal:,.0f}".replace(",", ".")
    fmt_from = f"Rp {saldo_from:,.0f}".replace(",", ".")
    fmt_to = f"Rp {saldo_to:,.0f}".replace(",", ".")
    admin_line = f"\n• *Biaya Admin:* Rp {int(biaya_admin):,}".replace(",", ".") if biaya_admin > 0 else ""

    msg = (
        f"🔄 *Transfer Antar Rekening Tercatat!*\n\n"
        f"• *Dari:* {from_akun} (−{fmt_nominal}){admin_line}\n"
        f"• *Ke:* {to_akun} (+{fmt_nominal})\n"
        f"• *Saldo {from_akun}:* *{fmt_from}*\n"
        f"• *Saldo {to_akun}:* *{fmt_to}*\n\n"
        f"📊 _Jurnal keuangan otomatis diperbarui (KPI kas riil tetap bersih)._"
    )
    await send_message(chat_id, msg)
async def download_file(file_path: str, dest_path: str):
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)

async def send_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(url, json=payload)
        except Exception as e:
            print(f"[TelegramBot] Error sending message: {e}")


def parse_quick_expense_text(text: str) -> Optional[Dict[str, Any]]:
    clean = text.strip()
    if not clean or clean.startswith("/"):
        return None

    # 1. Detect Jenis (default 'keluar')
    jenis = "keluar"
    masuk_kws = ("dapat", "terima", "gaji", "uang masuk", "pemasukan", "transfer masuk")
    if any(k in clean.lower() for k in masuk_kws):
        jenis = "masuk"

    # 2. Detect Nominal
    nom = None
    nom_match = None

    # Pattern: 1.5jt, 2jt, 1.5 juta
    m_jt = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*(?:jt|juta)\b", clean, re.IGNORECASE)
    if m_jt:
        nom = float(m_jt.group(1).replace(",", ".")) * 1_000_000
        nom_match = m_jt.group(0)

    if not nom:
        # Pattern: 25rb, 25k, 50 ribu
        m_rb = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*(?:rb|ribu|k)\b", clean, re.IGNORECASE)
        if m_rb:
            nom = float(m_rb.group(1).replace(",", ".")) * 1_000
            nom_match = m_rb.group(0)

    if not nom:
        # Pattern: 25.000, 25000, Rp 25.000
        m_std = re.search(r"(?:rp\.?|idr)?\s*([0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{2})?|[0-9]{4,})", clean, re.IGNORECASE)
        if m_std:
            s_val = m_std.group(1).replace(".", "").replace(",", ".")
            try:
                nom = float(s_val)
                nom_match = m_std.group(0)
            except Exception:
                pass

    if not nom or nom <= 0:
        return None

    # 3. Detect Akun
    account_map = {
        "bca": "BCA",
        "bsi": "BSI",
        "seabank": "Seabank",
        "ovo": "OVO",
        "mandiri": "Mandiri",
        "bri": "BRI",
        "bni": "BNI",
        "jago": "Jago",
        "gopay": "GoPay",
        "dana": "DANA",
        "cash": "Cash",
        "tunai": "Cash",
    }
    akun = "Cash"
    bank_matched_word = None
    for kw, acc in account_map.items():
        if re.search(rf"\b{kw}\b", clean, re.IGNORECASE):
            akun = acc
            bank_matched_word = kw
            break

    # 4. Extract Description / Catatan
    desc = clean
    if nom_match:
        desc = re.sub(re.escape(nom_match), "", desc, flags=re.IGNORECASE)
    if bank_matched_word:
        desc = re.sub(rf"\b{bank_matched_word}\b", "", desc, flags=re.IGNORECASE)

    desc = re.sub(r"^(?:bayar|beli|uang|pengeluaran|pemasukan)\s+", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"\s+", " ", desc).strip(" :.-")
    if not desc or len(desc) < 2:
        desc = "Pengeluaran Tunai" if jenis == "keluar" else "Pemasukan Tunai"

    return {
        "nominal": nom,
        "akun": akun,
        "catatan": desc.capitalize(),
        "merchant": desc.capitalize(),
        "jenis": jenis,
    }
async def handle_text_message(message: dict):
    chat_id = message["chat"]["id"]
    _save_last_chat_id(chat_id)
    text = message.get("text", "").strip()
    first_name = message.get("from", {}).get("first_name", "Kawan")
    print(f"[TelegramBot] Text received from {chat_id} ({first_name}): {text}")
    
    if text.startswith("/start") or text.startswith("/help") or text.startswith("/bantuan"):
        reply = (
            f"👋 *Halo {first_name}! Saya Bot Pitih (Pencatat Keuangan Otomatis)*\n\n"
            "📱 *Fitur & Perintah Cepat:*\n"
            "• /saldo — Cek saldo semua rekening & kas\n"
            "• /hari_ini — Rekap transaksi & pengeluaran hari ini\n"
            "• /bulan_ini — Rekap bulanan & status target anggaran\n"
            "• /tagihan — Pantau tagihan rutin (Kos, Wifi, VPS, dll)\n"
            "• /status — Cek koneksi server bot\n\n"
            "✍️ *Catat Cepat via Chat:*\n"
            "Ketik santai tanpa struk, misal:\n"
            "• `Makan siang 25rb bca`\n"
            "• `Bensin motor 30k`\n"
            "• `Parkir 2000 cash`\n\n"
            "📸 *Kirim Foto Struk:*\n"
            "Kirimkan foto / screenshot struk transfer untuk dipindai otomatis via AI.\n\n"
            "📊 *Dashboard Web:* https://pitih.haluih.id"
        )
        await send_message(chat_id, reply)
        return
    elif text.startswith("/status") or text.startswith("/ping"):
        await send_message(chat_id, "🟢 *Bot Pitih Online & Terhubung ke Server!*")
        return
    elif text.startswith("/saldo"):
        balances = get_account_balances()
        total_kas = sum(b.get("saldo", 0) for b in balances)
        lines = []
        for b in sorted(balances, key=lambda x: x["akun"]):
            s_fmt = f"Rp {int(b.get('saldo', 0)):,}".replace(",", ".")
            diff = b.get("selisih_rekonsiliasi") or 0.0
            recon_note = f" (⚠️ Selisih: Rp {int(abs(diff)):,})" if abs(diff) >= 1 else ""
            lines.append(f"• *{b['akun']}:* {s_fmt}{recon_note}")
        
        fmt_total = f"Rp {int(total_kas):,}".replace(",", ".")
        reply = (
            f"🏦 *Saldo Rekening & E-Wallet Pitih*\n\n"
            + "\n".join(lines)
            + f"\n\n💰 *Total Kas Tersedia:* *{fmt_total}*\n"
            f"📊 _Dashboard: https://pitih.haluih.id_"
        )
        await send_message(chat_id, reply)
        return
    elif text.startswith("/hari_ini") or text.startswith("/today"):
        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        now_f = now_dt.strftime("%d %b %Y")
        txs_res = get_transaksi_list(start_date=today_str, end_date=today_str, limit=100)
        today_txs = txs_res.get("items", [])
        
        masuk = sum(t["nominal"] for t in today_txs if t["jenis"] == "masuk" and t.get("kategori") != "Transfer Antar Rekening")
        keluar = sum(t["nominal"] for t in today_txs if t["jenis"] == "keluar" and t.get("kategori") != "Transfer Antar Rekening")
        net = masuk - keluar
        
        fmt_masuk = f"Rp {int(masuk):,}".replace(",", ".")
        fmt_keluar = f"Rp {int(keluar):,}".replace(",", ".")
        fmt_net = f"Rp {int(abs(net)):,}".replace(",", ".")
        net_sign = "+" if net >= 0 else "-"
        
        tx_lines = []
        for t in today_txs[:10]:
            j_icon = "🟢" if t["jenis"] == "masuk" else "🔴"
            nom_fmt = f"Rp {int(t['nominal']):,}".replace(",", ".")
            merch = t.get("merchant") or t.get("catatan") or "-"
            tx_lines.append(f"• {j_icon} [{t['akun']}] {merch} — *{nom_fmt}*")

        details = "\n" + "\n".join(tx_lines) if tx_lines else "\n_Belum ada transaksi tercatat hari ini._"
        reply = (
            f"📅 *Rekap Transaksi Hari Ini ({now_f})*\n\n"
            f"🟢 *Pemasukan:* {fmt_masuk}\n"
            f"🔴 *Pengeluaran:* {fmt_keluar}\n"
            f"⚖️ *Arus Kas Bersih:* *{net_sign}{fmt_net}*\n\n"
            f"*Daftar Transaksi:* {details}\n\n"
            f"📊 _Dashboard: https://pitih.haluih.id_"
        )
        await send_message(chat_id, reply)
        return
    elif text.startswith("/bulan_ini") or text.startswith("/rekap"):
        now_dt = datetime.now()
        month_str = now_dt.strftime("%Y-%m")
        month_label = now_dt.strftime("%B %Y")
        summary = get_summary("month")
        b_summary = get_budget_summary(month_str)

        masuk = summary.get("total_masuk") or 0.0
        keluar = summary.get("total_keluar") or 0.0
        net = summary.get("saldo_bersih") or 0.0
        fmt_masuk = f"Rp {int(masuk):,}".replace(",", ".")
        fmt_keluar = f"Rp {int(keluar):,}".replace(",", ".")
        fmt_net = f"Rp {int(abs(net)):,}".replace(",", ".")
        net_sign = "+" if net >= 0 else "-"

        budget_lines = []
        for b in b_summary.get("items", []):
            if b.get("has_budget"):
                b_spent = f"Rp {int(b['terpakai']):,}".replace(",", ".")
                b_tgt = f"Rp {int(b['target_nominal']):,}".replace(",", ".")
                st_icon = "⚠️" if b["status"] == "overbudget" else ("⚡" if b["status"] == "warning" else "🟢")
                budget_lines.append(f"• {st_icon} *{b['kategori']}:* {b_spent} / {b_tgt} ({b['persentase']}%)")

        b_section = "\n\n🎯 *Status Target Anggaran:*\n" + "\n".join(budget_lines) if budget_lines else ""
        reply = (
            f"📊 *Rekap Keuangan Bulan Ini ({month_label})*\n\n"
            f"🟢 *Total Pemasukan:* *{fmt_masuk}*\n"
            f"🔴 *Total Pengeluaran:* *{fmt_keluar}*\n"
            f"⚖️ *Arus Kas Bersih:* *{net_sign}{fmt_net}*"
            f"{b_section}\n\n"
            f"📊 _Dashboard: https://pitih.haluih.id_"
        )
        await send_message(chat_id, reply)
        return
    elif text.startswith("/tagihan") or text.startswith("/subs"):
        now_dt = datetime.now()
        month_label = now_dt.strftime("%B %Y")
        sub_data = get_subscriptions_summary()

        tot_kom = f"Rp {int(sub_data.get('total_komitmen', 0)):,}".replace(",", ".")
        tot_lunas = f"Rp {int(sub_data.get('total_sudah_dibayar', 0)):,}".replace(",", ".")
        tot_pending = f"Rp {int(sub_data.get('total_belum_dibayar', 0)):,}".replace(",", ".")

        sub_lines = []
        for it in sub_data.get("items", []):
            nom_str = f"Rp {int(it['estimasi_nominal']):,}".replace(",", ".")
            if it["sudah_dibayar"]:
                sub_lines.append(f"• 🟢 *{it['nama']}* — {nom_str} (Lunas)")
            else:
                sub_lines.append(f"• ⏳ *{it['nama']}* — {nom_str} (Tgl {it['tanggal_jatuh_tempo']})")

        reply = (
            f"📅 *Status Tagihan & Langganan ({month_label})*\n\n"
            f"💰 *Total Komitmen:* *{tot_kom}*\n"
            f"🟢 *Lunas:* {sub_data.get('count_lunas', 0)} dari {sub_data.get('total_count', 0)} tagihan ({tot_lunas})\n"
            f"⏳ *Menunggu:* {sub_data.get('count_pending', 0)} tagihan ({tot_pending})\n\n"
            f"*Daftar Pos Tagihan:*\n"
            + "\n".join(sub_lines)
            + f"\n\n📊 _Kelola di: https://pitih.haluih.id_"
        )
        await send_message(chat_id, reply)
        return

    # Check if this message is a reply to a transaction notification
    reply_to = message.get("reply_to_message", {})
    reply_text = reply_to.get("text", "") if reply_to else ""
    tx_id_match = re.search(r"#(\d+)", reply_text) if reply_text else None

    if tx_id_match:
        tx_id = int(tx_id_match.group(1))
        tx = get_transaksi_by_id(tx_id)
        if tx:
            current_merchant = tx.get("merchant", "")
            updated_category = infer_category(current_merchant, text, tx.get("jenis", "keluar"))
            if not updated_category or updated_category == "Lainnya":
                updated_category = tx.get("kategori", "Belanja & Retail")

            updates = {
                "catatan": text,
                "kategori": updated_category,
            }
            success = update_transaksi(tx_id, updates)
            if success:
                reply = (
                    f"✅ *Rincian Transaksi #{tx_id} Diperbarui!*\n\n"
                    f"• *Catatan/Barang:* {text}\n"
                    f"• *Kategori:* {updated_category}\n\n"
                    f"📊 _Dashboard Pitih telah diperbarui._"
                )
                await send_message(chat_id, reply)
                return
            else:
                await send_message(chat_id, f"❌ Gagal memperbarui catatan transaksi #{tx_id}.")
                return
        else:
            await send_message(chat_id, f"⚠️ Transaksi #{tx_id} tidak ditemukan di database.")
            return

    # If not an explicit reply, check if there's a recent QRIS transaction within 15 minutes
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM transaksi WHERE (LOWER(catatan) LIKE '%qris%' OR LOWER(merchant) LIKE '%qris%') ORDER BY id DESC LIMIT 1"
        )
        recent_qris = cursor.fetchone()
        conn.close()

        if recent_qris:
            created_str = recent_qris["created_at"] or recent_qris["tanggal"]
            if created_str:
                created_dt = datetime.strptime(str(created_str)[:19], "%Y-%m-%d %H:%M:%S")
                diff_sec = (datetime.now() - created_dt).total_seconds()
                if 0 <= diff_sec <= 900:  # 15 minutes
                    tx_id = int(recent_qris["id"])
                    current_merchant = recent_qris["merchant"]
                    updated_category = infer_category(current_merchant, text, recent_qris["jenis"] or "keluar")
                    if not updated_category or updated_category == "Lainnya":
                        updated_category = recent_qris["kategori"] or "Belanja & Retail"
                    update_transaksi(tx_id, {"catatan": text, "kategori": updated_category})
                    reply = (
                        f"✅ *Rincian Transaksi QRIS #{tx_id} Diperbarui!*\n\n"
                        f"• *Merchant:* {current_merchant}\n"
                        f"• *Catatan/Barang:* {text}\n"
                        f"• *Kategori:* {updated_category}\n\n"
                        f"📊 _Dashboard Pitih telah diperbarui._"
                    )
                    await send_message(chat_id, reply)
                    return
    except Exception as e:
        print(f"[TelegramBot] Error matching recent QRIS transaction: {e}")

    # Check if text is a quick expense/income note (e.g. "Makan padang 25rb bca" or "Parkir 2000 cash")
    quick = parse_quick_expense_text(text)
    if quick:
        now_dt = datetime.now()
        tanggal = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        jenis = quick["jenis"]
        merchant = quick["merchant"]
        catatan = quick["catatan"]
        nominal = quick["nominal"]
        akun = quick["akun"]
        kategori = infer_category(merchant, catatan, jenis)

        tx_id = insert_transaksi(
            tanggal=tanggal,
            jenis=jenis,
            kategori=kategori,
            nominal=nominal,
            akun=akun,
            merchant=merchant,
            catatan=catatan,
            ref_id="-",
            image_url="",
            source="telegram_text",
            ai_confidence=0.99,
        )

        fmt_nom = f"Rp {nominal:,.0f}".replace(",", ".")
        badge = "🟢 Uang Masuk" if jenis == "masuk" else "🔴 Uang Keluar"
        budget_line = ""
        if jenis == "keluar":
            b_alert = check_category_budget_alert(kategori)
            if b_alert:
                tag_label = "MELEBIHI TARGET" if b_alert["is_overbudget"] else "Mendekati Batas (80%+)"
                fmt_sp = f"Rp {int(b_alert['terpakai']):,}".replace(",", ".")
                fmt_tg = f"Rp {int(b_alert['target']):,}".replace(",", ".")
                fmt_rem = f"Rp {int(abs(b_alert['sisa'])):,}".replace(",", ".")
                rem_txt = f"Sisa jatah: *{fmt_rem}*" if not b_alert["is_overbudget"] else f"Overbudget: *+{fmt_rem}*"
                budget_line = f"\n\n⚠️ *Peringatan Anggaran ({tag_label})!*\nKategori *{b_alert['kategori']}* terpakai *{b_alert['persentase']}%* ({fmt_sp} dari target {fmt_tg}). {rem_txt}"

        reply = (
            f"✅ *Transaksi Berhasil Dicatat!* (#{tx_id})\n\n"
            f"• *Tipe:* {badge}\n"
            f"• *Nominal:* *{fmt_nom}*\n"
            f"• *Catatan/Item:* {catatan}\n"
            f"• *Kategori:* {kategori}\n"
            f"• *Akun:* {akun}\n"
            f"• *Waktu:* {now_dt.strftime('%d %b %Y, %H:%M')} WIB"
            f"{budget_line}\n\n"
            f"📊 _Buku jurnal dan saldo {akun} telah diperbarui._"
        )
        await send_message(chat_id, reply)
        return

    reply = (
        "📸 *Kirimkan foto struk pembayaran atau ketik pengeluaran santai!*\n\n"
        "💡 *Contoh Catat Cepat via Chat:*\n"
        "• `Makan siang 25rb bca`\n"
        "• `Bensin motor 30k`\n"
        "• `Parkir 2000 cash`\n"
        "• `Kopi tuku 28rb ovo`\n\n"
        "Atau kirimkan foto struk belanja untuk dipindai otomatis via AI. 🚀"
    )
    await send_message(chat_id, reply)
async def process_photo_message(message: dict):
    chat_id = message["chat"]["id"]
    _save_last_chat_id(chat_id)
    photos = message.get("photo", [])
    doc = message.get("document", {})
    
    file_id = None
    if photos:
        best_photo = photos[-1]
        file_id = best_photo["file_id"]
    elif doc and (doc.get("mime_type", "").startswith("image/") or doc.get("file_name", "").lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
        file_id = doc["file_id"]
        
    if not file_id:
        return
    
    print(f"[TelegramBot] Photo received from {chat_id}, file_id: {file_id}")
    await send_message(chat_id, "⏳ *Sedang memindai struk transfer via AI...*")
    
    try:
        # Get file path from Telegram
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}")
            resp.raise_for_status()
            file_info = resp.json()["result"]
            file_path = file_info["file_path"]
            
        ext = os.path.splitext(file_path)[1] or ".jpg"
        local_filename = f"receipt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
        local_filepath = os.path.join(RECEIPTS_DIR, local_filename)
        
        await download_file(file_path, local_filepath)
        
        # Scan with AI
        extracted = await extract_receipt_data(local_filepath)

        if not extracted.get("is_receipt", True) or float(extracted.get("nominal", 0)) <= 0:
            try:
                if os.path.exists(local_filepath):
                    os.remove(local_filepath)
            except Exception:
                pass
            await send_message(
                chat_id,
                "⚠️ *Gambar Tidak Terdeteksi Sebagai Struk Keuangan*\n\n"
                "Foto yang dikirim tidak terdeteksi sebagai bukti transfer atau pembayaran yang sah.\n\n"
                "ℹ️ *Tidak ada transaksi yang dicatat ke sistem Pitih.*",
            )
            return

        now_dt = datetime.now()
        tanggal = extracted.get("tanggal", now_dt.strftime("%Y-%m-%d %H:%M:%S"))
        # If time is 12:00:00 (default fallback when receipt only showed date), use current time
        if tanggal.endswith("12:00:00"):
            tanggal = f"{tanggal[:10]} {now_dt.strftime('%H:%M:%S')}"
        jenis = extracted.get("jenis", "keluar")
        kategori = extracted.get("kategori", "Lainnya")
        if jenis == "masuk" and kategori in ("Lainnya", "Gaji / Pemasukan"):
            kategori = infer_category(merchant, catatan, jenis)
        nominal = float(extracted.get("nominal", 0))
        akun = extracted.get("akun", "Bank")
        merchant = extracted.get("merchant", "-")
        catatan = extracted.get("catatan", "Scan Struk")
        ref_id = extracted.get("ref_id", "-")
        confidence = float(extracted.get("ai_confidence", 0.98))
        
        drive_res = upload_receipt_to_drive(local_filepath, local_filename)
        if drive_res and drive_res.get("image_url"):
            image_url = drive_res["image_url"]
            storage_label = "☁️ Google Drive"
        else:
            image_url = f"/api/receipts/{local_filename}"
            storage_label = "💾 Lokal Server"

        
        tx_id = None
        linked_pair_id = None
        is_internal_pair = False

        if jenis == "masuk":
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT id, akun, nominal, transfer_pair_id, image_url
                    FROM transaksi
                    WHERE jenis = 'keluar'
                      AND nominal = ?
                      AND tanggal >= datetime(?, '-60 minutes')
                      AND tanggal <= datetime(?, '+60 minutes')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (nominal, tanggal, tanggal),
                )
                matching_out = cursor.fetchone()
            finally:
                conn.close()

            if matching_out:
                out_id = matching_out["id"]
                existing_in_id = matching_out["transfer_pair_id"]
                if existing_in_id:
                    tx_id = existing_in_id
                    linked_pair_id = out_id
                    is_internal_pair = True
                    conn = get_db_connection()
                    try:
                        conn.execute("UPDATE transaksi SET image_url = ? WHERE id = ?", (image_url, existing_in_id))
                        conn.commit()
                    finally:
                        conn.close()
                else:
                    link_res = link_transaction_as_transfer(out_id, akun)
                    tx_id = link_res["in_id"]
                    linked_pair_id = out_id
                    is_internal_pair = True
                    conn = get_db_connection()
                    try:
                        conn.execute("UPDATE transaksi SET image_url = ? WHERE id = ?", (image_url, tx_id))
                        conn.commit()
                    finally:
                        conn.close()
            else:
                dest_test = detect_destination_account(akun, f"{catatan} {merchant}", merchant, catatan)
                if dest_test:
                    kategori = "Transfer Antar Rekening"
        elif jenis == "keluar":
            dest_akun = detect_destination_account(akun, f"{catatan} {merchant}", merchant, catatan)
            if dest_akun:
                transfer_res = create_internal_transfer(
                    from_akun=akun,
                    to_akun=dest_akun,
                    nominal=nominal,
                    biaya_admin=0.0,
                    tanggal=tanggal,
                    catatan=catatan,
                    ref_id=ref_id,
                    source="telegram_bot",
                )
                tx_id = transfer_res["out_id"]
                linked_pair_id = transfer_res["in_id"]
                is_internal_pair = True
                conn = get_db_connection()
                try:
                    conn.execute("UPDATE transaksi SET image_url = ? WHERE id = ?", (image_url, tx_id))
                    conn.commit()
                finally:
                    conn.close()

        if not tx_id:
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

        formatted_nominal = f"Rp {nominal:,.0f}".replace(",", ".")
        if is_internal_pair:
            reply = (
                f"🔄 *Transfer Antar Rekening Terdeteksi (#{tx_id} ↔ #{linked_pair_id})!*\n\n"
                f"• *Nominal:* *{formatted_nominal}*\n"
                f"• *Rekening:* {akun}\n"
                f"• *Status:* Bukti transfer berhasil ditautkan ke pasangan transaksi.\n"
                f"• *Penyimpanan:* {storage_label}\n\n"
                f"📊 _Kategori: Transfer Antar Rekening (tidak menggandakan arus kas)._"
            )
        else:
            badge = "🟢 Uang Masuk" if jenis == "masuk" else "🔴 Uang Keluar"
            reply = (
                f"✅ *Transaksi #{tx_id} Berhasil Dicatat!*\n\n"
                f"• *Tipe:* {badge}\n"
                f"• *Nominal:* *{formatted_nominal}*\n"
                f"• *Bank/Akun:* {akun}\n"
                f"• *Tujuan/Pengirim:* {merchant}\n"
                f"• *Kategori:* {kategori}\n"
                f"• *Tanggal:* {tanggal}\n"
                f"• *No Ref:* `{ref_id}`\n\n"
                f"• *Penyimpanan:* {storage_label}\n\n"
                f"📊 _Data sudah otomatis masuk ke dashboard Pitih._"
            )
        await send_message(chat_id, reply)
        
    except Exception as e:
        print(f"[TelegramBot] Error processing receipt: {e}")
        await send_message(chat_id, f"❌ Gagal memproses struk: {str(e)}")

async def start_telegram_bot():
    if not TELEGRAM_TOKEN:
        print("[TelegramBot] TELEGRAM_BOT_TOKEN_PITIH is not set. Bot worker disabled.")
        return
        
    print("[TelegramBot] Starting Telegram long polling...")
    offset = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        if "message" in update:
                            msg = update["message"]
                            if "photo" in msg or ("document" in msg and msg.get("document", {}).get("mime_type", "").startswith("image/")):
                                asyncio.create_task(process_photo_message(msg))
                            elif "text" in msg:
                                asyncio.create_task(handle_text_message(msg))
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[TelegramBot] Polling loop error: {e}")
            await asyncio.sleep(5)
