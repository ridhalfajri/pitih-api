import os
import glob
import sqlite3
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, List

from database import DB_PATH, get_db_connection
from drive_manager import (
    is_drive_connected,
    upload_backup_to_drive,
    list_drive_backups,
)
from bot import get_notify_chat_id, send_message

BACKUPS_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
os.makedirs(BACKUPS_DIR, exist_ok=True)


def create_local_sqlite_snapshot() -> Dict[str, Any]:
    """Creates a point-in-time consistent SQLite snapshot using the SQLite Online Backup API."""
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pitih_backup_{now_str}.db"
    dest_path = os.path.join(BACKUPS_DIR, filename)

    src_conn = get_db_connection()
    dest_conn = sqlite3.connect(dest_path)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()

    size_bytes = os.path.getsize(dest_path)
    print(f"[BackupManager] Created local SQLite snapshot: {dest_path} ({size_bytes} bytes)")

    # Prune local backups: keep latest 3 to prevent local disk buildup
    _prune_local_backups(keep_last=3)

    return {
        "filename": filename,
        "filepath": dest_path,
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 1),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _prune_local_backups(keep_last: int = 3) -> None:
    try:
        files = sorted(glob.glob(os.path.join(BACKUPS_DIR, "pitih_backup_*.db")), reverse=True)
        if len(files) > keep_last:
            for old_path in files[keep_last:]:
                try:
                    os.remove(old_path)
                    print(f"[BackupManager] Pruned old local backup: {old_path}")
                except Exception as e:
                    print(f"[BackupManager] Error pruning {old_path}: {e}")
    except Exception as e:
        print(f"[BackupManager] Error scanning local backups: {e}")


def perform_database_backup(upload_drive: bool = True) -> Dict[str, Any]:
    """Creates a local snapshot and optionally uploads it to Google Drive."""
    snapshot = create_local_sqlite_snapshot()
    drive_result: Optional[Dict[str, Any]] = None

    if upload_drive and is_drive_connected():
        drive_result = upload_backup_to_drive(snapshot["filepath"], snapshot["filename"])

    return {
        "status": "success",
        "filename": snapshot["filename"],
        "size_bytes": snapshot["size_bytes"],
        "size_kb": snapshot["size_kb"],
        "created_at": snapshot["created_at"],
        "drive_connected": is_drive_connected(),
        "drive_uploaded": drive_result is not None,
        "drive_file_id": drive_result.get("file_id") if drive_result else None,
        "drive_link": drive_result.get("web_view_link") if drive_result else None,
    }


def get_backup_status() -> Dict[str, Any]:
    """Returns the current backup status, local files, and Google Drive backups."""
    local_files = sorted(glob.glob(os.path.join(BACKUPS_DIR, "pitih_backup_*.db")), reverse=True)
    latest_local = None
    if local_files:
        p = local_files[0]
        latest_local = {
            "filename": os.path.basename(p),
            "size_kb": round(os.path.getsize(p) / 1024, 1),
            "modified_at": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S"),
        }

    drive_active = is_drive_connected()
    drive_list: List[Dict[str, Any]] = []
    if drive_active:
        drive_list = list_drive_backups()

    return {
        "drive_connected": drive_active,
        "latest_local_backup": latest_local,
        "total_local_backups": len(local_files),
        "drive_backups_count": len(drive_list),
        "latest_drive_backup": drive_list[0] if drive_list else None,
        "drive_backups": drive_list[:10],
    }


def get_latest_backup_filepath() -> Optional[str]:
    local_files = sorted(glob.glob(os.path.join(BACKUPS_DIR, "pitih_backup_*.db")), reverse=True)
    if local_files:
        return local_files[0]
    # If no snapshot yet, create one on the fly
    snap = create_local_sqlite_snapshot()
    return snap["filepath"]


async def start_database_backup_worker() -> None:
    """Automated daily backup worker running in background."""
    print("[BackupManager] Automated database backup worker started.")
    # Wait 30 seconds on startup before initial backup check
    await asyncio.sleep(30)
    
    while True:
        try:
            status = get_backup_status()
            needs_backup = True
            
            # If a backup was already made in the last 20 hours, skip
            latest_time_str = None
            if status.get("latest_drive_backup"):
                latest_time_str = status["latest_drive_backup"].get("createdTime")
            elif status.get("latest_local_backup"):
                latest_time_str = status["latest_local_backup"].get("modified_at")

            if latest_time_str:
                try:
                    # Parse ISO or standard datetime
                    clean_t = latest_time_str[:19].replace("T", " ")
                    last_dt = datetime.strptime(clean_t, "%Y-%m-%d %H:%M:%S")
                    diff_hours = (datetime.now() - last_dt).total_seconds() / 3600
                    if diff_hours < 20:
                        needs_backup = False
                except Exception:
                    pass

            if needs_backup:
                print("[BackupManager] Executing scheduled daily database backup...")
                res = await asyncio.to_thread(perform_database_backup, True)
                print(f"[BackupManager] Scheduled backup completed: {res['filename']} ({res['size_kb']} KB)")
                
                # Send quiet notification to Telegram
                try:
                    chat_id = get_notify_chat_id()
                    if chat_id:
                        storage_info = "☁️ Google Drive" if res["drive_uploaded"] else "💾 Penyimpanan Lokal"
                        msg = (
                            f"💾 *Backup Database Otomatis Selesai!*\n\n"
                            f"• *File:* `{res['filename']}`\n"
                            f"• *Ukuran:* {res['size_kb']} KB\n"
                            f"• *Lokasi:* {storage_info}\n"
                            f"• *Waktu:* {res['created_at']}\n\n"
                            f"🛡️ _Histori data keuangan Anda terlindungi dengan aman._"
                        )
                        await send_message(chat_id, msg)
                except Exception as e:
                    print(f"[BackupManager] Error sending backup telegram alert: {e}")

            # Wait 6 hours before next check
            await asyncio.sleep(6 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[BackupManager] Error in backup worker: {e}")
            await asyncio.sleep(3600)
