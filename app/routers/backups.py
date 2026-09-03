from fastapi import APIRouter, HTTPException

from .. import backups

router = APIRouter()


@router.get("/api/backups")
def api_backups_list():
    dest = backups.backup_dir()
    if dest is None:
        raise HTTPException(400, "Scheduled backups are not configured — set ND_BACKUP_DIR")
    return {"backups": backups.list_snapshots(dest)}


@router.post("/api/backups/run")
def api_backups_run():
    """Take a snapshot right now (GM-only like everything unlisted in
    _is_player_safe). Same VACUUM INTO primitive the scheduler thread uses;
    ignores ND_BACKUP_INTERVAL_SECONDS but honors ND_BACKUP_KEEP retention."""
    try:
        path = backups.run_backup_once()
    except RuntimeError:
        raise HTTPException(400, "Scheduled backups are not configured — set ND_BACKUP_DIR")
    except Exception as exc:
        raise HTTPException(500, f"Snapshot failed: {exc}")
    return {"name": path.name, "size_bytes": path.stat().st_size}
