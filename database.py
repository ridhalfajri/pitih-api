import os
import re
import sqlite3
import hashlib
from typing import Optional, List, Dict, Any
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "/opt/data/keuangan/keuangan.db")

def get_db_connection():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def normalize_account_name(akun: str) -> str:
    raw = (akun or "").strip()
    low = raw.lower()
    if "bsi" in low or "byond" in low or "syariah indonesia" in low:
        return "BSI"
    if "bca" in low:
        return "BCA"
    if "mandiri" in low or "livin" in low:
        return "Mandiri"
    if "bri" in low or "brimo" in low:
        return "BRI"
    if "bni" in low or "wondr" in low:
        return "BNI"
    if "jago" in low:
        return "Jago"
    if "seabank" in low:
        return "Seabank"
    if "gopay" in low or "gojek" in low:
        return "GoPay"
    if "ovo" in low:
        return "OVO"
    return raw or "BCA"

def compute_tx_fingerprint(
    akun: str,
    jenis: str,
    nominal: float,
    tanggal: str,
    ref_id: Optional[str] = None,
    merchant: Optional[str] = None,
) -> str:
    akun_norm = (akun or "").strip().lower()
    jenis_norm = (jenis or "keluar").strip().lower()
    nom_int = int(round(float(nominal or 0)))
    clean_ref = (ref_id or "").strip()
    if clean_ref and clean_ref != "-":
        payload = f"ref|{akun_norm}|{jenis_norm}|{nom_int}|{clean_ref.upper()}"
    else:
        time_bucket = (tanggal or "")[:15]
        merch_norm = (merchant or "").strip().lower()[:24]
        payload = f"time|{akun_norm}|{jenis_norm}|{nom_int}|{time_bucket}|{merch_norm}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

def is_duplicate_transaction(
    email_message_id: Optional[str] = None,
    fingerprint: Optional[str] = None,
) -> Optional[int]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if email_message_id and email_message_id.strip():
            cursor.execute(
                "SELECT id FROM transaksi WHERE email_message_id = ? LIMIT 1",
                (email_message_id.strip(),),
            )
            row = cursor.fetchone()
            if row:
                return int(row["id"])
        if fingerprint and fingerprint.strip():
            cursor.execute(
                "SELECT id FROM transaksi WHERE fingerprint = ? LIMIT 1",
                (fingerprint.strip(),),
            )
            row = cursor.fetchone()
            if row:
                return int(row["id"])
        return None
    finally:
        conn.close()

def recompute_account_ledger(conn: sqlite3.Connection, akun: str) -> None:
    if not akun:
        return
    akun = normalize_account_name(akun)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO rekening (akun, nama, saldo_awal, saldo_berjalan) VALUES (?, ?, 0.0, 0.0)",
        (akun, akun),
    )
    cursor.execute(
        "SELECT saldo_awal, last_reconciled_at, updated_at FROM rekening WHERE akun = ?",
        (akun,),
    )
    r_row = cursor.fetchone()
    saldo_awal = float(r_row["saldo_awal"]) if r_row and r_row["saldo_awal"] is not None else 0.0
    running = saldo_awal

    cursor.execute("DELETE FROM jurnal_keuangan WHERE akun = ?", (akun,))
    if abs(saldo_awal) >= 1.0:
        recon_date = (
            (r_row["last_reconciled_at"] or r_row["updated_at"])
            if r_row
            else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        formatted_init = f"Rp {abs(saldo_awal):,.0f}".replace(",", ".")
        sign = "+" if saldo_awal > 0 else "-"
        cursor.execute(
            """
            INSERT INTO jurnal_keuangan (
                transaksi_id, tanggal, akun, jenis, debit, kredit,
                saldo_sebelum, saldo_sesudah, keterangan, ref_id, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                recon_date or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                akun,
                "penyesuaian",
                saldo_awal if saldo_awal > 0 else 0.0,
                abs(saldo_awal) if saldo_awal < 0 else 0.0,
                0.0,
                saldo_awal,
                f"Rekonsiliasi Bank — Saldo Awal / Penyesuaian Rekening (Selisih {sign}{formatted_init})",
                "RECON-SALDO",
                "reconcile",
            ),
        )

    cursor.execute(
        "SELECT * FROM transaksi WHERE akun = ? ORDER BY id ASC",
        (akun,),
    )
    tx_rows = cursor.fetchall()
    for row in tx_rows:
        row_dict = dict(row)
        nom = float(row_dict.get("nominal") or 0.0)
        admin_fee = float(row_dict.get("biaya_admin") or 0.0)
        jns = (row_dict.get("jenis") or "keluar").lower()
        debit = nom if jns == "masuk" else 0.0
        kredit = (nom + admin_fee) if jns != "masuk" else 0.0
        saldo_sebelum = running
        saldo_sesudah = saldo_sebelum + debit - kredit
        merchant = row_dict.get("merchant") or "-"
        catatan = row_dict.get("catatan") or row_dict.get("kategori") or "-"
        keterangan = f"{merchant} — {catatan}"
        source = row_dict.get("source") or "receipt"
        jurnal_jenis = "penyesuaian" if source == "reconcile" else jns
        cursor.execute(
            """
            INSERT INTO jurnal_keuangan (
                transaksi_id, tanggal, akun, jenis, debit, kredit,
                saldo_sebelum, saldo_sesudah, keterangan, ref_id, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_dict["id"],
                row_dict["tanggal"],
                akun,
                jurnal_jenis,
                debit,
                kredit,
                saldo_sebelum,
                saldo_sesudah,
                keterangan,
                row_dict.get("ref_id"),
                source,
            ),
        )
        running = saldo_sesudah

    cursor.execute(
        "UPDATE rekening SET saldo_berjalan = ?, saldo_bank_terakhir = NULL, updated_at = CURRENT_TIMESTAMP WHERE akun = ?",
        (running, akun),
    )

def recompute_all_ledgers(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT akun FROM transaksi WHERE akun IS NOT NULL AND akun != ''")
    accounts = [r["akun"] for r in cursor.fetchall()]
    for akun in accounts:
        recompute_account_ledger(conn, akun)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transaksi (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tanggal TEXT NOT NULL,
        jenis TEXT NOT NULL,          -- 'keluar' atau 'masuk'
        kategori TEXT NOT NULL,       -- 'Makanan & Minuman', 'Operasional', dll
        nominal REAL NOT NULL,
        akun TEXT NOT NULL,           -- 'BCA', 'Mandiri', 'BSI', dll
        merchant TEXT,
        catatan TEXT,
        ref_id TEXT,
        image_url TEXT,
        ai_confidence REAL DEFAULT 0.98,
        source TEXT DEFAULT 'receipt',
        email_message_id TEXT,
        fingerprint TEXT,
        saldo_akhir REAL,
        transfer_pair_id INTEGER,
        biaya_admin REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    migrations = [
        "ALTER TABLE transaksi ADD COLUMN source TEXT DEFAULT 'receipt'",
        "ALTER TABLE transaksi ADD COLUMN email_message_id TEXT",
        "ALTER TABLE transaksi ADD COLUMN fingerprint TEXT",
        "ALTER TABLE transaksi ADD COLUMN saldo_akhir REAL",
        "ALTER TABLE transaksi ADD COLUMN transfer_pair_id INTEGER",
        "ALTER TABLE transaksi ADD COLUMN biaya_admin REAL DEFAULT 0.0",
        "ALTER TABLE rekening ADD COLUMN no_rekening TEXT",
    ]
    for sql in migrations:
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_transaksi_email_msg
    ON transaksi(email_message_id)
    WHERE email_message_id IS NOT NULL AND email_message_id != ''
    """)

    cursor.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_transaksi_fingerprint
    ON transaksi(fingerprint)
    WHERE fingerprint IS NOT NULL AND fingerprint != ''
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_transaksi_transfer_pair
    ON transaksi(transfer_pair_id)
    """)


    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rekening (
        akun TEXT PRIMARY KEY,
        nama TEXT,
        no_rekening TEXT,
        saldo_awal REAL NOT NULL DEFAULT 0.0,
        saldo_berjalan REAL NOT NULL DEFAULT 0.0,
        saldo_bank_terakhir REAL,
        last_reconciled_at TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS jurnal_keuangan (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaksi_id INTEGER,
        tanggal TEXT NOT NULL,
        akun TEXT NOT NULL,
        jenis TEXT NOT NULL,          -- 'masuk' | 'keluar' | 'penyesuaian'
        debit REAL NOT NULL DEFAULT 0.0,
        kredit REAL NOT NULL DEFAULT 0.0,
        saldo_sebelum REAL NOT NULL DEFAULT 0.0,
        saldo_sesudah REAL NOT NULL DEFAULT 0.0,
        keterangan TEXT NOT NULL,
        ref_id TEXT,
        source TEXT NOT NULL DEFAULT 'receipt',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_inbox_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT UNIQUE NOT NULL,
        sender TEXT NOT NULL,
        subject TEXT NOT NULL,
        received_at TEXT NOT NULL,
        bank_detected TEXT,
        status TEXT NOT NULL,         -- 'processed' | 'duplicate' | 'verification_code' | 'ignored' | 'failed'
        transaksi_id INTEGER,
        verification_code TEXT,
        verification_url TEXT,
        masked_preview TEXT,
        error_message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_agent_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        agent_email TEXT DEFAULT '',
        imap_host TEXT DEFAULT 'imap.gmail.com',
        imap_port INTEGER DEFAULT 993,
        imap_user TEXT DEFAULT '',
        imap_password TEXT DEFAULT '',
        polling_interval_sec INTEGER DEFAULT 30,
        enabled INTEGER DEFAULT 1,
        allowed_forwarders TEXT DEFAULT 'ridhalfajri00@gmail.com,tuanridhal@gmail.com',
        telegram_notify_chat_id TEXT DEFAULT '',
        last_sync_at TIMESTAMP,
        last_error TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute(
        """
        INSERT OR IGNORE INTO email_agent_config (
            id, agent_email, imap_host, imap_port, imap_user, imap_password,
            polling_interval_sec, enabled, allowed_forwarders, telegram_notify_chat_id
        ) VALUES (1, ?, ?, ?, ?, ?, 30, 1, ?, ?)
        """,
        (
            os.getenv("AGENT_EMAIL_ADDRESS", "tuanridhal@gmail.com"),
            os.getenv("AGENT_IMAP_HOST", "imap.gmail.com"),
            int(os.getenv("AGENT_IMAP_PORT", "993") or 993),
            os.getenv("AGENT_IMAP_USER", "tuanridhal@gmail.com"),
            os.getenv("AGENT_IMAP_PASSWORD", ""),
            os.getenv("ALLOWED_EMAILS", "ridhalfajri00@gmail.com,tuanridhal@gmail.com"),
            os.getenv("TELEGRAM_NOTIFY_CHAT_ID", ""),
        ),
    )

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS anggaran (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kategori TEXT UNIQUE NOT NULL,
        target_nominal REAL NOT NULL,
        periode TEXT DEFAULT 'monthly',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    default_budgets = [
        ("Makanan & Minuman", 2000000.0),
        ("Belanja & Retail", 1500000.0),
        ("Transportasi", 500000.0),
        ("Tagihan & Utilitas", 46500.0),
        ("Langganan Digital", 286000.0),
        ("Kost & Tempat Tinggal", 900000.0),
        ("Lainnya", 500000.0),
    ]
    for kat, target in default_budgets:
        cursor.execute(
            "INSERT OR IGNORE INTO anggaran (kategori, target_nominal) VALUES (?, ?)",
            (kat, target),
        )

    # Migrasi kategori 'Server Subscriptions' ke 'Langganan Digital'
    try:
        cursor.execute("UPDATE OR IGNORE anggaran SET kategori = 'Langganan Digital' WHERE kategori = 'Server Subscriptions'")
        cursor.execute("DELETE FROM anggaran WHERE kategori = 'Server Subscriptions'")
    except Exception:
        pass
    try:
        cursor.execute("UPDATE transaksi SET kategori = 'Langganan Digital' WHERE kategori = 'Server Subscriptions'")
    except Exception:
        pass
    try:
        cursor.execute("UPDATE langganan_rutin SET kategori = 'Langganan Digital' WHERE kategori = 'Server Subscriptions'")
    except Exception:
        pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS langganan_rutin (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nama TEXT NOT NULL,
        kategori TEXT NOT NULL,
        estimasi_nominal REAL NOT NULL DEFAULT 0.0,
        tanggal_jatuh_tempo INTEGER DEFAULT 1,
        siklus TEXT DEFAULT 'bulanan',
        keywords TEXT DEFAULT '',
        catatan TEXT DEFAULT '',
        aktif INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("SELECT COUNT(*) as cnt FROM langganan_rutin")
    if cursor.fetchone()["cnt"] == 0:
        default_subs = [
            ("Kos", "Kost & Tempat Tinggal", 900000.0, 1, "kos,kost,sewa kamar,kamar kos", "Sewa kamar kost bulanan"),
            ("Wifi", "Tagihan & Utilitas", 46500.0, 1, "indihome,biznet,myrepublic,firstmedia,wifi,oxygen", "Internet wifi bulanan"),
            ("VPS", "Langganan Digital", 150000.0, 1, "vps,hostinger,digitalocean,linode,vultr,hetzner,ovh,niagahoster,cloud hosting", "Sewa server VPS bulanan"),
            ("Gemini Pro", "Langganan Digital", 77000.0, 5, "gemini,google workspace,google cloud", "Langganan Google Gemini Pro"),
            ("YouTube Premium", "Hiburan & Streaming", 179000.0, 10, "youtube,google youtube", "Langganan YouTube Premium"),
            ("Vidio Premium", "Hiburan & Streaming", 25000.0, 11, "vidio", "Streaming Vidio Premier"),
            ("iCloud+", "Langganan Digital", 59000.0, 29, "apple,icloud,itunes,apple.com", "Penyimpanan cloud Apple"),
        ]
        for nama, kat, est, tgl, kws, cat in default_subs:
            cursor.execute(
                """
                INSERT INTO langganan_rutin (nama, kategori, estimasi_nominal, tanggal_jatuh_tempo, keywords, catatan)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (nama, kat, est, tgl, kws, cat),
            )
    cursor.execute("SELECT COUNT(*) as cnt FROM jurnal_keuangan")
    jurnal_cnt = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM transaksi")
    tx_cnt = cursor.fetchone()["cnt"]
    if jurnal_cnt == 0 and tx_cnt > 0:
        recompute_all_ledgers(conn)

    conn.commit()
    conn.close()

def insert_transaksi(
    tanggal: str,
    jenis: str,
    kategori: str,
    nominal: float,
    akun: str,
    merchant: Optional[str] = None,
    catatan: Optional[str] = None,
    ref_id: Optional[str] = None,
    image_url: Optional[str] = None,
    ai_confidence: float = 0.98,
    source: str = "receipt",
    email_message_id: Optional[str] = None,
    fingerprint: Optional[str] = None,
    saldo_akhir: Optional[float] = None,
    biaya_admin: float = 0.0,
    transfer_pair_id: Optional[int] = None,
) -> int:
    akun = normalize_account_name(akun)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO rekening (akun, nama, saldo_awal, saldo_berjalan) VALUES (?, ?, 0.0, 0.0)",
            (akun, akun),
        )
        cursor.execute("SELECT saldo_berjalan FROM rekening WHERE akun = ?", (akun,))
        r_row = cursor.fetchone()
        saldo_sebelum = float(r_row["saldo_berjalan"]) if r_row and r_row["saldo_berjalan"] is not None else 0.0

        nom = float(nominal or 0.0)
        admin_fee = float(biaya_admin or 0.0)
        jns = (jenis or "keluar").lower()
        debit = nom if jns == "masuk" else 0.0
        kredit = (nom + admin_fee) if jns != "masuk" else 0.0
        saldo_sesudah = saldo_sebelum + debit - kredit
        final_saldo_akhir = float(saldo_akhir) if saldo_akhir is not None else saldo_sesudah

        cursor.execute(
            """
            INSERT INTO transaksi (
                tanggal, jenis, kategori, nominal, akun, merchant, catatan,
                ref_id, image_url, ai_confidence, source, email_message_id, fingerprint, saldo_akhir,
                biaya_admin, transfer_pair_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tanggal,
                jns,
                kategori,
                nom,
                akun,
                merchant,
                catatan,
                ref_id,
                image_url,
                ai_confidence,
                source,
                email_message_id,
                fingerprint,
                final_saldo_akhir,
                admin_fee,
                transfer_pair_id,
            ),
        )
        new_id = cursor.lastrowid

        keterangan = f"{merchant or '-'} — {catatan or kategori}"
        jurnal_jenis = "penyesuaian" if source == "reconcile" else jns
        cursor.execute(
            """
            INSERT INTO jurnal_keuangan (
                transaksi_id, tanggal, akun, jenis, debit, kredit,
                saldo_sebelum, saldo_sesudah, keterangan, ref_id, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                tanggal,
                akun,
                jurnal_jenis,
                debit,
                kredit,
                saldo_sebelum,
                saldo_sesudah,
                keterangan,
                ref_id,
                source,
            ),
        )

        if saldo_akhir is not None:
            cursor.execute(
                """
                UPDATE rekening
                SET saldo_berjalan = ?, saldo_bank_terakhir = ?, updated_at = CURRENT_TIMESTAMP
                WHERE akun = ?
                """,
                (saldo_sesudah, float(saldo_akhir), akun),
            )
        else:
            cursor.execute(
                """
                UPDATE rekening
                SET saldo_berjalan = ?, saldo_bank_terakhir = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE akun = ?
                """,
                (saldo_sesudah, akun),
            )

        conn.commit()
        return int(new_id)
    finally:
        conn.close()

def get_transaksi_by_id(tx_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM transaksi WHERE id = ?", (tx_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def detect_destination_account(
    source_akun: str,
    text: str,
    merchant: str = "",
    catatan: str = "",
) -> Optional[str]:
    src_norm = normalize_account_name(source_akun)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT akun, nama, no_rekening FROM rekening")
        rows = cursor.fetchall()
        accounts_in_db = [dict(r) for r in rows]
    finally:
        conn.close()

    registered_map: Dict[str, Dict[str, Any]] = {}
    for r in accounts_in_db:
        canon = normalize_account_name(r["akun"])
        registered_map[canon] = r

    if not registered_map:
        for acc in ["BSI", "BCA", "Seabank", "OVO", "Mandiri", "BRI", "BNI", "Jago", "GoPay", "DANA"]:
            registered_map[acc] = {"akun": acc, "nama": acc, "no_rekening": None}

    full_probe = f"{merchant} {catatan} {text}".lower()
    digits_found = re.findall(r"\b(\d{4,16})\b", full_probe)
    masked_digits = re.findall(r"\*{2,}(\d{2,4})", full_probe)

    account_keywords: Dict[str, List[str]] = {
        "OVO": ["ovo"],
        "Seabank": ["seabank", "bank seabank", "bank seabank indonesia"],
        "BSI": ["bsi", "byond", "bank syariah indonesia", "bsi mobile"],
        "BCA": ["bca", "klikbca", "mybca", "bank central asia"],
        "Mandiri": ["mandiri", "livin"],
        "BRI": ["bri", "brimo", "bank rakyat indonesia"],
        "BNI": ["bni", "wondr", "bank negara indonesia"],
        "Jago": ["jago", "bank jago"],
        "GoPay": ["gopay", "gojek"],
        "DANA": ["dana"],
        "ShopeePay": ["shopeepay", "shopee"],
    }

    # If the transaction explicitly mentions another external e-wallet brand not in registered_map,
    # prevent false matching to an internal account (e.g. ShopeePay top up must not match OVO)
    if any(k in full_probe for k in ("shopeepay", "shopee", "linkaja", "astrapay", "isaku")):
        if "ShopeePay" not in registered_map:
            return None

    # 1. Match 4-digit no_rekening / HP in text
    for canon_acc, r_data in registered_map.items():
        if canon_acc == src_norm:
            continue
        no_rek = str(r_data.get("no_rekening") or "").strip()
        if no_rek and len(no_rek) >= 4:
            last4 = no_rek[-4:]
            has_num_match = (
                any(d == no_rek or d.endswith(last4) or d == last4 for d in digits_found)
                or any(last4.endswith(m) for m in masked_digits if len(m) >= 2)
            )
            if has_num_match:
                # If canon_acc is an e-wallet (e.g. OVO, GoPay, DANA), DO NOT match purely on 4-digit phone number
                # unless the specific e-wallet brand keyword is ALSO present in the probe,
                # because the user's phone number is shared across multiple e-wallets!
                if canon_acc in ("OVO", "GoPay", "DANA", "ShopeePay", "LinkAja"):
                    kws = account_keywords.get(canon_acc, [canon_acc.lower()])
                    if any(kw in full_probe for kw in kws):
                        return canon_acc
                else:
                    return canon_acc
    merchant_low = (merchant or "").lower()
    catatan_low = (catatan or "").lower()

    # 2. Check merchant text
    for canon_acc in registered_map.keys():
        if canon_acc == src_norm:
            continue
        kws = account_keywords.get(canon_acc, [canon_acc.lower()])
        if any(kw in merchant_low for kw in kws):
            return canon_acc
    # 3. Check catatan text
    for canon_acc in registered_map.keys():
        if canon_acc == src_norm:
            continue
        kws = account_keywords.get(canon_acc, [canon_acc.lower()])
        if any(kw in catatan_low for kw in kws):
            return canon_acc

    # 4. Check destination headers in body text
    dest_matches = re.findall(
        r"(?:rekening penerima|tujuan|penerima|ke rekening|beneficiary bank|bank penerima)\s*[:\-=]\s*([^\n\r]+)",
        full_probe,
        re.IGNORECASE,
    )
    for line in dest_matches:
        line_low = line.lower()
        for canon_acc in registered_map.keys():
            if canon_acc == src_norm:
                continue
            kws = account_keywords.get(canon_acc, [canon_acc.lower()])
            if any(kw in line_low for kw in kws):
                return canon_acc

    return None

def create_internal_transfer(
    from_akun: str,
    to_akun: str,
    nominal: float,
    biaya_admin: float = 0.0,
    tanggal: Optional[str] = None,
    catatan: Optional[str] = None,
    ref_id: Optional[str] = None,
    source: str = "manual",
    email_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    from_akun = normalize_account_name(from_akun)
    to_akun = normalize_account_name(to_akun)
    if from_akun == to_akun:
        raise ValueError("Rekening asal dan tujuan tidak boleh sama.")

    tgl = tanggal or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nom = float(nominal)
    admin_fee = float(biaya_admin or 0.0)
    base_ref = ref_id or f"TRF-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        admin_suffix = f" (+ Biaya Admin Rp {int(admin_fee):,})".replace(",", ".") if admin_fee > 0 else ""
        out_catatan = f"{catatan or 'Transfer ke ' + to_akun}{admin_suffix}"
        out_merchant = f"Pindah Dana ke {to_akun}"

        out_fp = compute_tx_fingerprint(
            akun=from_akun,
            jenis="keluar",
            nominal=nom,
            tanggal=tgl,
            ref_id=base_ref,
            merchant=out_merchant,
        )

        cursor.execute(
            """
            INSERT INTO transaksi (
                tanggal, jenis, kategori, nominal, akun, merchant, catatan,
                ref_id, image_url, ai_confidence, source, email_message_id, fingerprint,
                biaya_admin, transfer_pair_id
            ) VALUES (?, ?, 'Transfer Antar Rekening', ?, ?, ?, ?, ?, '', 1.0, ?, ?, ?, ?, NULL)
            """,
            (
                tgl,
                "keluar",
                nom,
                from_akun,
                out_merchant,
                out_catatan,
                base_ref,
                source,
                email_message_id,
                out_fp,
                admin_fee,
            ),
        )
        tx_out_id = int(cursor.lastrowid)

        in_catatan = catatan or f"Transfer dari {from_akun}"
        in_merchant = f"Pindah Dana dari {from_akun}"
        in_ref = f"{base_ref}-IN"
        in_fp = compute_tx_fingerprint(
            akun=to_akun,
            jenis="masuk",
            nominal=nom,
            tanggal=tgl,
            ref_id=in_ref,
            merchant=in_merchant,
        )

        cursor.execute(
            """
            INSERT INTO transaksi (
                tanggal, jenis, kategori, nominal, akun, merchant, catatan,
                ref_id, image_url, ai_confidence, source, email_message_id, fingerprint,
                biaya_admin, transfer_pair_id
            ) VALUES (?, ?, 'Transfer Antar Rekening', ?, ?, ?, ?, ?, '', 1.0, ?, ?, ?, 0.0, ?)
            """,
            (
                tgl,
                "masuk",
                nom,
                to_akun,
                in_merchant,
                in_catatan,
                in_ref,
                source,
                f"{email_message_id}-in" if email_message_id else None,
                in_fp,
                tx_out_id,
            ),
        )
        tx_in_id = int(cursor.lastrowid)

        cursor.execute(
            "UPDATE transaksi SET transfer_pair_id = ? WHERE id = ?",
            (tx_in_id, tx_out_id),
        )
        conn.commit()

        recompute_account_ledger(conn, from_akun)
        recompute_account_ledger(conn, to_akun)
        conn.commit()

        return {
            "out_id": tx_out_id,
            "in_id": tx_in_id,
            "from_akun": from_akun,
            "to_akun": to_akun,
            "nominal": nom,
            "biaya_admin": admin_fee,
        }
    finally:
        conn.close()

def link_transaction_as_transfer(
    tx_id: int,
    to_akun: str,
    biaya_admin: Optional[float] = None,
) -> Dict[str, Any]:
    tx = get_transaksi_by_id(tx_id)
    if not tx:
        raise ValueError(f"Transaksi #{tx_id} tidak ditemukan.")

    if tx.get("transfer_pair_id"):
        partner_id = int(tx["transfer_pair_id"])
        partner = get_transaksi_by_id(partner_id) or {}
        return {
            "out_id": tx_id if tx["jenis"] == "keluar" else partner_id,
            "in_id": partner_id if tx["jenis"] == "keluar" else tx_id,
            "from_akun": tx["akun"] if tx["jenis"] == "keluar" else partner.get("akun", ""),
            "to_akun": partner.get("akun", "") if tx["jenis"] == "keluar" else tx["akun"],
            "nominal": tx["nominal"],
            "biaya_admin": tx.get("biaya_admin", 0.0),
        }

    target_akun = normalize_account_name(to_akun)
    source_akun = normalize_account_name(tx["akun"])
    if target_akun == source_akun:
        raise ValueError("Rekening tujuan tidak boleh sama dengan rekening transaksi.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        tgl = tx["tanggal"]
        nom = float(tx["nominal"])
        admin_fee = float(biaya_admin) if biaya_admin is not None else float(tx.get("biaya_admin") or 0.0)

        if tx["jenis"] == "keluar":
            merchant = f"Pindah Dana ke {target_akun}"
            cursor.execute(
                """
                UPDATE transaksi
                SET kategori = 'Transfer Antar Rekening',
                    merchant = ?,
                    biaya_admin = ?
                WHERE id = ?
                """,
                (merchant, admin_fee, tx_id),
            )
            partner_ref = f"{tx.get('ref_id') or tx_id}-IN"
            partner_merchant = f"Pindah Dana dari {source_akun}"
            partner_catatan = f"Transfer dari {source_akun}"
            partner_fp = compute_tx_fingerprint(
                akun=target_akun,
                jenis="masuk",
                nominal=nom,
                tanggal=tgl,
                ref_id=partner_ref,
                merchant=partner_merchant,
            )
            cursor.execute(
                """
                INSERT INTO transaksi (
                    tanggal, jenis, kategori, nominal, akun, merchant, catatan,
                    ref_id, image_url, ai_confidence, source, email_message_id, fingerprint,
                    biaya_admin, transfer_pair_id
                ) VALUES (?, ?, 'Transfer Antar Rekening', ?, ?, ?, ?, ?, '', 1.0, 'transfer_link', NULL, ?, 0.0, ?)
                """,
                (
                    tgl,
                    "masuk",
                    nom,
                    target_akun,
                    partner_merchant,
                    partner_catatan,
                    partner_ref,
                    partner_fp,
                    tx_id,
                ),
            )
            partner_id = int(cursor.lastrowid)
            cursor.execute("UPDATE transaksi SET transfer_pair_id = ? WHERE id = ?", (partner_id, tx_id))
            out_id, in_id = tx_id, partner_id
            from_ak, to_ak = source_akun, target_akun
        else:
            merchant = f"Pindah Dana dari {target_akun}"
            cursor.execute(
                """
                UPDATE transaksi
                SET kategori = 'Transfer Antar Rekening',
                    merchant = ?
                WHERE id = ?
                """,
                (merchant, tx_id),
            )
            partner_ref = f"{tx.get('ref_id') or tx_id}-OUT"
            partner_merchant = f"Pindah Dana ke {source_akun}"
            partner_catatan = f"Transfer ke {source_akun}"
            partner_fp = compute_tx_fingerprint(
                akun=target_akun,
                jenis="keluar",
                nominal=nom,
                tanggal=tgl,
                ref_id=partner_ref,
                merchant=partner_merchant,
            )
            cursor.execute(
                """
                INSERT INTO transaksi (
                    tanggal, jenis, kategori, nominal, akun, merchant, catatan,
                    ref_id, image_url, ai_confidence, source, email_message_id, fingerprint,
                    biaya_admin, transfer_pair_id
                ) VALUES (?, ?, 'Transfer Antar Rekening', ?, ?, ?, ?, ?, '', 1.0, 'transfer_link', NULL, ?, ?, ?)
                """,
                (
                    tgl,
                    "keluar",
                    nom,
                    target_akun,
                    partner_merchant,
                    partner_catatan,
                    partner_ref,
                    partner_fp,
                    admin_fee,
                    tx_id,
                ),
            )
            partner_id = int(cursor.lastrowid)
            cursor.execute("UPDATE transaksi SET transfer_pair_id = ? WHERE id = ?", (partner_id, tx_id))
            out_id, in_id = partner_id, tx_id
            from_ak, to_ak = target_akun, source_akun

        conn.commit()
        recompute_account_ledger(conn, source_akun)
        recompute_account_ledger(conn, target_akun)
        conn.commit()

        return {
            "out_id": out_id,
            "in_id": in_id,
            "from_akun": from_ak,
            "to_akun": to_ak,
            "nominal": nom,
            "biaya_admin": admin_fee,
        }
    finally:
        conn.close()
def get_transaksi_list(
    search: Optional[str] = None,
    bank: Optional[str] = None,
    jenis: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    limit: int = 20
) -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = "SELECT * FROM transaksi WHERE 1=1"
    params = []
    
    if search:
        query += " AND (merchant LIKE ? OR catatan LIKE ? OR ref_id LIKE ? OR akun LIKE ?)"
        s = f"%{search}%"
        params.extend([s, s, s, s])
        
    if bank and bank.lower() != "all" and bank.lower() != "semua":
        query += " AND akun LIKE ?"
        params.append(f"%{bank}%")
        
    if jenis and jenis.lower() != "all" and jenis.lower() != "semua":
        query += " AND jenis = ?"
        params.append(jenis.lower())
        
    if start_date:
        query += " AND tanggal >= ?"
        params.append(start_date)
        
    if end_date:
        query += " AND tanggal <= ?"
        params.append(f"{end_date} 23:59:59" if len(end_date) == 10 else end_date)
        
    # Count total
    count_query = f"SELECT COUNT(*) FROM ({query})"
    cursor.execute(count_query, params)
    total_count = cursor.fetchone()[0]
    
    # Pagination
    offset = (page - 1) * limit
    query += " ORDER BY tanggal DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    data = [dict(row) for row in rows]
    conn.close()
    
    return {
        "items": data,
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": (total_count + limit - 1) // limit if limit > 0 else 1
    }

def get_summary(
    range_filter: Optional[str] = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
    SELECT 
        COALESCE(SUM(CASE WHEN jenis = 'masuk' AND kategori != 'Transfer Antar Rekening' THEN nominal ELSE 0 END), 0) as total_masuk,
        COALESCE(SUM(CASE WHEN kategori = 'Transfer Antar Rekening' THEN COALESCE(biaya_admin, 0) WHEN jenis = 'keluar' THEN (nominal + COALESCE(biaya_admin, 0)) ELSE 0 END), 0) as total_keluar,
        COUNT(*) as total_transaksi
    FROM transaksi
    WHERE 1=1
    """
    params = []
    
    if start_date:
        query += " AND tanggal >= ?"
        params.append(start_date)
    if end_date:
        query += " AND tanggal <= ?"
        params.append(f"{end_date} 23:59:59" if len(end_date) == 10 else end_date)
        
    today = datetime.now().strftime("%Y-%m-%d")
    if not start_date and not end_date:
        if range_filter == "today":
            query += " AND tanggal >= ?"
            params.append(f"{today} 00:00:00")
        elif range_filter == "7d":
            query += " AND tanggal >= datetime('now', '-7 days')"
        elif range_filter == "30d":
            query += " AND tanggal >= datetime('now', '-30 days')"
        elif range_filter in ("month", "this_month"):
            query += " AND strftime('%Y-%m', tanggal) = strftime('%Y-%m', 'now')"
        elif range_filter == "quarter":
            query += " AND strftime('%Y', tanggal) = strftime('%Y', 'now')"
        
    cursor.execute(query, params)
    row = cursor.fetchone()
    total_masuk = float(row["total_masuk"])
    total_keluar = float(row["total_keluar"])
    saldo_bersih = total_masuk - total_keluar
    total_tx = int(row["total_transaksi"])
    
    conn.close()
    return {
        "total_masuk": total_masuk,
        "total_keluar": total_keluar,
        "saldo_bersih": saldo_bersih,
        "total_transaksi": total_tx,
        "perubahan_persen_masuk": 0.0,
        "perubahan_persen_keluar": 0.0
    }

def get_bank_breakdown() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT akun, COUNT(*) as count, SUM(nominal) as total
    FROM transaksi
    GROUP BY akun
    ORDER BY total DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_cashflow_chart(range_days: int = 30) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
    SELECT 
        strftime('%Y-%m-%d', tanggal) as date,
        COALESCE(SUM(CASE WHEN jenis = 'masuk' AND kategori != 'Transfer Antar Rekening' THEN nominal ELSE 0 END), 0) as masuk,
        COALESCE(SUM(CASE WHEN kategori = 'Transfer Antar Rekening' THEN COALESCE(biaya_admin, 0) WHEN jenis = 'keluar' THEN (nominal + COALESCE(biaya_admin, 0)) ELSE 0 END), 0) as keluar
    FROM transaksi
    WHERE tanggal >= datetime('now', '-{range_days} days')
    GROUP BY date
    ORDER BY date ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_account_balances() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT OR IGNORE INTO rekening (akun, nama, saldo_awal, saldo_berjalan)
            SELECT DISTINCT akun, akun, 0.0, 0.0
            FROM transaksi
            WHERE akun IS NOT NULL AND akun != ''
            """
        )
        conn.commit()

        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            """
            SELECT
                r.akun,
                r.nama,
                r.no_rekening,
                r.saldo_awal,
                r.saldo_berjalan,
                r.saldo_bank_terakhir,
                r.last_reconciled_at,
                COALESCE(SUM(CASE WHEN t.jenis = 'masuk' THEN t.nominal ELSE -(t.nominal + COALESCE(t.biaya_admin, 0)) END), 0.0) as tx_net,
                COALESCE(SUM(CASE WHEN substr(t.tanggal, 1, 10) = ? THEN (CASE WHEN t.jenis = 'masuk' THEN t.nominal ELSE -(t.nominal + COALESCE(t.biaya_admin, 0)) END) ELSE 0.0 END), 0.0) as mutasi_hari_ini,
                COUNT(t.id) as total_transaksi
            FROM rekening r
            LEFT JOIN transaksi t ON r.akun = t.akun
            GROUP BY r.akun
            ORDER BY (r.saldo_awal + COALESCE(SUM(CASE WHEN t.jenis = 'masuk' THEN t.nominal ELSE -t.nominal END), 0.0)) DESC
            """,
            (today,),
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            saldo_awal = float(d.get("saldo_awal") or 0.0)
            tx_net = float(d.get("tx_net") or 0.0)
            saldo_berjalan = saldo_awal + tx_net
            saldo_bank_terakhir = (
                float(d["saldo_bank_terakhir"])
                if d.get("saldo_bank_terakhir") is not None
                else None
            )
            selisih = (
                round(saldo_bank_terakhir - saldo_berjalan, 2)
                if saldo_bank_terakhir is not None
                else 0.0
            )
            result.append(
                {
                    "akun": d["akun"],
                    "nama": d.get("nama") or d["akun"],
                    "no_rekening": d.get("no_rekening") or "",
                    "saldo": saldo_berjalan,
                    "balance": saldo_berjalan,
                    "saldo_awal": saldo_awal,
                    "saldo_bank_terakhir": saldo_bank_terakhir,
                    "selisih_rekonsiliasi": selisih,
                    "mutasi_hari_ini": float(d.get("mutasi_hari_ini") or 0.0),
                    "total_transaksi": int(d.get("total_transaksi") or 0),
                    "last_reconciled_at": d.get("last_reconciled_at"),
                }
            )
        return result
    finally:
        conn.close()

def update_transaksi(tx_id: int, updates: Dict[str, Any]) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT akun FROM transaksi WHERE id = ?", (tx_id,))
        old_row = cursor.fetchone()
        if not old_row:
            return False
        old_akun = old_row["akun"]

        fields = []
        values = []
        allowed = ["tanggal", "jenis", "kategori", "nominal", "akun", "merchant", "catatan", "ref_id", "biaya_admin", "transfer_pair_id"]
        for k, v in updates.items():
            if k in allowed:
                fields.append(f"{k} = ?")
                values.append(v)

        if not fields:
            return False

        values.append(tx_id)
        cursor.execute(f"UPDATE transaksi SET {', '.join(fields)} WHERE id = ?", values)
        affected = cursor.rowcount > 0
        if affected:
            recompute_account_ledger(conn, old_akun)
            new_akun = updates.get("akun")
            if new_akun and new_akun != old_akun:
                recompute_account_ledger(conn, new_akun)
        conn.commit()
        return affected
    finally:
        conn.close()

def delete_transaksi(tx_id: int) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    old_akun = None
    try:
        # Clean up associated receipt file
        try:
            cursor.execute("SELECT akun, image_url FROM transaksi WHERE id = ?", (tx_id,))
            row = cursor.fetchone()
            if row:
                old_akun = row["akun"]
            if row and row["image_url"]:
                img_url = row["image_url"]
                if img_url.startswith("/api/receipts/drive/"):
                    file_id = img_url.replace("/api/receipts/drive/", "")
                    try:
                        from drive_manager import delete_drive_file
                        delete_drive_file(file_id)
                    except Exception as de:
                        print(f"Error deleting drive file {file_id}: {de}")
                elif img_url.startswith("/api/receipts/"):
                    fname = img_url.replace("/api/receipts/", "")
                    receipts_dir = os.getenv("RECEIPTS_DIR", "/data/receipts")
                    fpath = os.path.join(receipts_dir, fname)
                    if os.path.exists(fpath):
                        try:
                            os.remove(fpath)
                        except Exception as fe:
                            print(f"Error deleting local file {fpath}: {fe}")
        except Exception as e:
            print(f"Error querying image for deletion: {e}")

        cursor.execute("UPDATE transaksi SET transfer_pair_id = NULL WHERE transfer_pair_id = ?", (tx_id,))
        cursor.execute("DELETE FROM transaksi WHERE id = ?", (tx_id,))
        affected = cursor.rowcount > 0
        if affected and old_akun:
            recompute_account_ledger(conn, old_akun)
        conn.commit()
        return affected
    finally:
        conn.close()

def get_journals(
    akun: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    limit: int = 25,
) -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        query = "SELECT * FROM jurnal_keuangan WHERE 1=1"
        params: List[Any] = []
        if akun and akun.lower() not in ("all", "semua"):
            query += " AND akun LIKE ?"
            params.append(f"%{akun}%")
        if start_date:
            query += " AND tanggal >= ?"
            params.append(start_date)
        if end_date:
            query += " AND tanggal <= ?"
            params.append(f"{end_date} 23:59:59" if len(end_date) == 10 else end_date)

        count_query = f"SELECT COUNT(*) FROM ({query})"
        cursor.execute(count_query, params)
        total_count = int(cursor.fetchone()[0])

        offset = (page - 1) * limit
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor.execute(query, params)
        items = [dict(r) for r in cursor.fetchall()]
        return {
            "items": items,
            "total": total_count,
            "page": page,
            "limit": limit,
            "total_pages": (total_count + limit - 1) // limit if limit > 0 else 1,
        }
    finally:
        conn.close()

def reconcile_account_balance(
    akun: str,
    actual_balance: float,
    catatan: str = "Rekonsiliasi Saldo Rekening",
    nama: Optional[str] = None,
    no_rekening: Optional[str] = None,
) -> Dict[str, Any]:
    clean_akun = akun.strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO rekening (akun, nama, saldo_awal, saldo_berjalan) VALUES (?, ?, 0.0, 0.0)",
            (clean_akun, clean_akun),
        )
        cursor.execute(
            """
            SELECT
                r.saldo_awal,
                COALESCE(SUM(CASE WHEN t.jenis = 'masuk' THEN t.nominal ELSE -(t.nominal + COALESCE(t.biaya_admin, 0)) END), 0.0) as tx_net
            FROM rekening r
            LEFT JOIN transaksi t ON r.akun = t.akun
            WHERE r.akun = ?
            GROUP BY r.akun
            """,
            (clean_akun,),
        )
        row = cursor.fetchone()
        old_saldo_awal = float(row["saldo_awal"] or 0.0) if row else 0.0
        tx_net = float(row["tx_net"] or 0.0) if row else 0.0
        saldo_berjalan = old_saldo_awal + tx_net

        selisih = round(float(actual_balance) - saldo_berjalan, 2)
        new_saldo_awal = float(actual_balance) - tx_net
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        jurnal_id: Optional[int] = None
        if abs(selisih) >= 1.0:
            formatted_diff = f"Rp {abs(selisih):,.0f}".replace(",", ".")
            sign = "+" if selisih > 0 else "-"
            debit = abs(selisih) if selisih > 0 else 0.0
            kredit = abs(selisih) if selisih < 0 else 0.0
            keterangan = f"Rekonsiliasi Bank — {catatan or 'Rekonsiliasi Saldo Rekening'} (Selisih {sign}{formatted_diff})"
            ref_id = f"RECON-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            cursor.execute(
                """
                INSERT INTO jurnal_keuangan (
                    transaksi_id, tanggal, akun, jenis, debit, kredit,
                    saldo_sebelum, saldo_sesudah, keterangan, ref_id, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    None,
                    now_str,
                    clean_akun,
                    "penyesuaian",
                    debit,
                    kredit,
                    saldo_berjalan,
                    float(actual_balance),
                    keterangan,
                    ref_id,
                    "reconcile",
                ),
            )
            jurnal_id = int(cursor.lastrowid or 0)

        cursor.execute(
            """
            UPDATE rekening
            SET saldo_awal = ?,
                saldo_berjalan = ?,
                saldo_bank_terakhir = ?,
                last_reconciled_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE akun = ?
            """,
            (new_saldo_awal, float(actual_balance), float(actual_balance), now_str, clean_akun),
        )
        if nama is not None and nama.strip():
            cursor.execute("UPDATE rekening SET nama = ? WHERE akun = ?", (nama.strip(), clean_akun))
        if no_rekening is not None and no_rekening.strip():
            cursor.execute("UPDATE rekening SET no_rekening = ? WHERE akun = ?", (no_rekening.strip(), clean_akun))
        conn.commit()

        return {
            "akun": clean_akun,
            "saldo_sebelum": saldo_berjalan,
            "saldo_aktual": float(actual_balance),
            "selisih": selisih,
            "adjusted_tx_id": jurnal_id,
        }
    finally:
        conn.close()

def get_email_agent_config() -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM email_agent_config WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            init_db()
            conn2 = get_db_connection()
            c2 = conn2.cursor()
            c2.execute("SELECT * FROM email_agent_config WHERE id = 1")
            r2 = c2.fetchone()
            conn2.close()
            return dict(r2) if r2 else {}
        return dict(row)
    finally:
        conn.close()

def update_email_agent_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        allowed = [
            "agent_email",
            "imap_host",
            "imap_port",
            "imap_user",
            "imap_password",
            "polling_interval_sec",
            "enabled",
            "allowed_forwarders",
            "telegram_notify_chat_id",
            "last_sync_at",
            "last_error",
        ]
        fields = []
        values = []
        for k, v in updates.items():
            if k in allowed:
                fields.append(f"{k} = ?")
                if k == "enabled" and isinstance(v, bool):
                    values.append(1 if v else 0)
                else:
                    values.append(v)
        if fields:
            fields.append("updated_at = CURRENT_TIMESTAMP")
            cursor.execute(
                f"UPDATE email_agent_config SET {', '.join(fields)} WHERE id = 1",
                values,
            )
            conn.commit()
    finally:
        conn.close()
    return get_email_agent_config()

def log_email_event(
    message_id: str,
    sender: str,
    subject: str,
    received_at: str,
    status: str,
    bank_detected: Optional[str] = None,
    transaksi_id: Optional[int] = None,
    verification_code: Optional[str] = None,
    verification_url: Optional[str] = None,
    masked_preview: Optional[str] = None,
    error_message: Optional[str] = None,
) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO email_inbox_log (
                message_id, sender, subject, received_at, bank_detected, status,
                transaksi_id, verification_code, verification_url, masked_preview, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                bank_detected = COALESCE(excluded.bank_detected, email_inbox_log.bank_detected),
                transaksi_id = COALESCE(excluded.transaksi_id, email_inbox_log.transaksi_id),
                verification_code = COALESCE(excluded.verification_code, email_inbox_log.verification_code),
                verification_url = COALESCE(excluded.verification_url, email_inbox_log.verification_url),
                masked_preview = COALESCE(excluded.masked_preview, email_inbox_log.masked_preview),
                error_message = excluded.error_message
            """,
            (
                message_id,
                sender or "-",
                subject or "(Tanpa Subjek)",
                received_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                bank_detected,
                status,
                transaksi_id,
                verification_code,
                verification_url,
                masked_preview,
                error_message,
            ),
        )
        row_id = cursor.lastrowid
        conn.commit()
        return int(row_id or 0)
    finally:
        conn.close()

def get_email_logs(limit: int = 30) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT * FROM email_inbox_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

def get_budget_summary(month_str: Optional[str] = None) -> Dict[str, Any]:
    target_month = month_str or datetime.now().strftime("%Y-%m")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, kategori, target_nominal, periode, updated_at FROM anggaran ORDER BY target_nominal DESC")
        budget_rows = cursor.fetchall()
        budget_map = {r["kategori"]: float(r["target_nominal"]) for r in budget_rows}

        cursor.execute(
            """
            SELECT kategori, SUM(nominal + coalesce(biaya_admin, 0)) as total_spent, COUNT(*) as count
            FROM transaksi
            WHERE jenis = 'keluar'
              AND kategori != 'Transfer Antar Rekening'
              AND strftime('%Y-%m', tanggal) = ?
            GROUP BY kategori
            """,
            (target_month,),
        )
        spent_rows = cursor.fetchall()
        spent_map = {r["kategori"]: float(r["total_spent"]) for r in spent_rows}

        all_categories = sorted(list(set(list(budget_map.keys()) + list(spent_map.keys()))))

        items = []
        total_budget = 0.0
        total_spent = 0.0

        for cat in all_categories:
            target = budget_map.get(cat, 0.0)
            spent = spent_map.get(cat, 0.0)
            remaining = target - spent
            pct = round((spent / target) * 100, 1) if target > 0 else 0.0

            status = "safe"
            if target > 0:
                if pct >= 100:
                    status = "overbudget"
                elif pct >= 80:
                    status = "warning"

            if target > 0:
                total_budget += target
            total_spent += spent

            items.append({
                "kategori": cat,
                "target_nominal": target,
                "terpakai": spent,
                "sisa": remaining,
                "persentase": pct,
                "status": status,
                "has_budget": target > 0,
            })

        overall_pct = round((total_spent / total_budget) * 100, 1) if total_budget > 0 else 0.0
        overall_status = "safe"
        if total_budget > 0:
            if overall_pct >= 100:
                overall_status = "overbudget"
            elif overall_pct >= 80:
                overall_status = "warning"

        return {
            "month": target_month,
            "total_budget": total_budget,
            "total_spent": total_spent,
            "total_remaining": total_budget - total_spent,
            "overall_percentage": overall_pct,
            "overall_status": overall_status,
            "items": items,
        }
    finally:
        conn.close()


def upsert_budget(kategori: str, target_nominal: float) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO anggaran (kategori, target_nominal, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(kategori) DO UPDATE SET
                target_nominal = excluded.target_nominal,
                updated_at = CURRENT_TIMESTAMP
            """,
            (kategori.strip(), float(target_nominal)),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_budget(kategori: str) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM anggaran WHERE kategori = ?", (kategori.strip(),))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def check_category_budget_alert(kategori: str) -> Optional[Dict[str, Any]]:
    if not kategori or kategori == "Transfer Antar Rekening":
        return None
    try:
        summary = get_budget_summary()
        item = next((i for i in summary["items"] if i["kategori"] == kategori), None)
        if not item or not item.get("has_budget"):
            return None
        pct = item["persentase"]
        if pct >= 80:
            return {
                "kategori": kategori,
                "target": item["target_nominal"],
                "terpakai": item["terpakai"],
                "sisa": item["sisa"],
                "persentase": pct,
                "is_overbudget": pct >= 100,
            }
    except Exception as e:
        print(f"[Budget] Error checking budget alert: {e}")
    return None

def get_subscriptions_summary(month_str: Optional[str] = None) -> Dict[str, Any]:
    target_month = month_str or datetime.now().strftime("%Y-%m")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. Fetch active subscriptions
        cursor.execute("SELECT * FROM langganan_rutin WHERE aktif = 1 ORDER BY tanggal_jatuh_tempo ASC")
        subs = [dict(r) for r in cursor.fetchall()]

        # 2. Fetch outgoing transactions for target_month
        cursor.execute(
            """
            SELECT id, tanggal, nominal, akun, merchant, catatan, kategori, ref_id
            FROM transaksi
            WHERE jenis = 'keluar'
              AND kategori != 'Transfer Antar Rekening'
              AND strftime('%Y-%m', tanggal) = ?
            ORDER BY id DESC
            """,
            (target_month,),
        )
        txs = [dict(r) for r in cursor.fetchall()]

        total_komitmen = 0.0
        total_sudah_dibayar = 0.0
        count_lunas = 0
        count_pending = 0
        items = []

        matched_tx_ids = set()

        for sub in subs:
            est = float(sub.get("estimasi_nominal") or 0.0)
            total_komitmen += est

            # Build keyword probe list
            raw_kws = sub.get("keywords") or ""
            kw_list = [k.strip().lower() for k in raw_kws.split(",") if k.strip()]
            sub_name_low = sub["nama"].lower()
            if sub_name_low not in kw_list:
                kw_list.append(sub_name_low)

            # Check if any transaction matches
            matched_tx = None
            for t in txs:
                if t["id"] in matched_tx_ids:
                    continue
                # Probe ONLY merchant and catatan - do NOT probe kategori to avoid false-positive matches
                t_probe = f"{t.get('merchant', '')} {t.get('catatan', '')}".lower()
                if any(kw in t_probe for kw in kw_list):
                    matched_tx = t
                    matched_tx_ids.add(t["id"])
                    break

            if matched_tx:
                act_nom = float(matched_tx["nominal"])
                total_sudah_dibayar += act_nom
                count_lunas += 1
                items.append({
                    **sub,
                    "sudah_dibayar": True,
                    "nominal_aktual": act_nom,
                    "tanggal_bayar": matched_tx["tanggal"],
                    "matched_transaksi_id": matched_tx["id"],
                    "matched_merchant": matched_tx["merchant"],
                })
            else:
                count_pending += 1
                items.append({
                    **sub,
                    "sudah_dibayar": False,
                    "nominal_aktual": None,
                    "tanggal_bayar": None,
                    "matched_transaksi_id": None,
                    "matched_merchant": None,
                })

        total_belum_dibayar = max(0.0, total_komitmen - total_sudah_dibayar)
        total_subs = len(subs)
        pct_lunas = round((count_lunas / total_subs) * 100, 1) if total_subs > 0 else 0.0

        return {
            "month": target_month,
            "total_komitmen": total_komitmen,
            "total_sudah_dibayar": total_sudah_dibayar,
            "total_belum_dibayar": total_belum_dibayar,
            "count_lunas": count_lunas,
            "count_pending": count_pending,
            "total_count": total_subs,
            "persentase_lunas": pct_lunas,
            "items": items,
        }
    finally:
        conn.close()


def upsert_subscription(payload: Dict[str, Any]) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        sub_id = payload.get("id")
        nama = str(payload.get("nama", "")).strip()
        kategori = str(payload.get("kategori", "Lainnya")).strip()
        est = float(payload.get("estimasi_nominal") or 0.0)
        due_day = int(payload.get("tanggal_jatuh_tempo") or 1)
        cycle = str(payload.get("siklus") or "bulanan").strip()
        kws = str(payload.get("keywords") or "").strip()
        catatan = str(payload.get("catatan") or "").strip()
        aktif = int(payload.get("aktif", 1))

        if sub_id:
            cursor.execute(
                """
                UPDATE langganan_rutin
                SET nama = ?, kategori = ?, estimasi_nominal = ?, tanggal_jatuh_tempo = ?,
                    siklus = ?, keywords = ?, catatan = ?, aktif = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (nama, kategori, est, due_day, cycle, kws, catatan, aktif, sub_id),
            )
            conn.commit()
            return int(sub_id)
        else:
            cursor.execute(
                """
                INSERT INTO langganan_rutin (nama, kategori, estimasi_nominal, tanggal_jatuh_tempo, siklus, keywords, catatan, aktif)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (nama, kategori, est, due_day, cycle, kws, catatan, aktif),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def delete_subscription(sub_id: int) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM langganan_rutin WHERE id = ?", (sub_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
