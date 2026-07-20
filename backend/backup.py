import shutil, sqlite3
from pathlib import Path

# 主库旁路文件后缀：rollback 模式用 -journal，WAL 模式用 -wal/-shm。迁移时三件套
# 必须随主库一起搬，否则留在旧目录的旁路文件会让任一边的库读起来不一致。
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

def relocate_legacy_db(legacy_path: Path, db_path: Path) -> bool:
    """把旧位置（仓库内 data/，iCloud）的主库一次性搬到新位置（App Support，非 iCloud）。
    幂等：新库已存在 或 旧库不存在 → 不动，返回 False。真搬了 → 返回 True。
    连同 -journal/-wal/-shm 旁路文件一起搬，保证一致性（spec D7）。"""
    if db_path.exists() or not legacy_path.exists():
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy_path), str(db_path))
    for suffix in _SIDECAR_SUFFIXES:
        side = legacy_path.with_name(legacy_path.name + suffix)
        if side.exists():
            shutil.move(str(side), str(db_path.with_name(db_path.name + suffix)))
    return True

def snapshot(db_path: Path, backups_dir: Path, batch_id: str) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    out = backups_dir / f"trend-desk_{batch_id}.db"
    if out.exists():
        out.unlink()
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"VACUUM INTO '{out}'")
    return out

def rotate(backups_dir: Path, keep: int = 7) -> None:
    files = sorted(backups_dir.glob("trend-desk_*.db"), key=lambda p: (p.stat().st_mtime, p.name))
    for old in files[:-keep]:
        old.unlink()

def integrity_check(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]

def restore(snapshot_path: Path, db_path: Path) -> None:
    shutil.copy2(snapshot_path, db_path)
