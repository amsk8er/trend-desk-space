"""把识别成功的 iCloud 原图移进归档目录，按批次号分子目录。

与 nodes/ocr.py 里「移动 app 内部副本」是两码事：本模块动的是用户 iCloud
inbox 里的原图，目的是 OCR 成功后让 inbox 变干净；失败的图调用方不传进来，留原地重试。
"""
import shutil
from pathlib import Path


def archive_source_images(*, source_dir: Path, archive_root: Path,
                          batch_id: str, filenames: list[str]) -> int:
    """把 source_dir 下指定文件名移到 archive_root/<batch_id>/。

    缺失的文件（用户已手动删/移）跳过。返回实际移动数。
    """
    dest_dir = archive_root / batch_id
    moved = 0
    for name in filenames:
        src = source_dir / name
        if not src.exists():
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_dir / name))
        moved += 1
    return moved
