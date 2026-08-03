"""每行所属板块的解析（共享给初筛 / 校对显示 / 聚合）。

OCR 只在板块组的组首行采到 sector，续行（尤其跨截图）为空，导致筛选误判「板块温度未知」。
两步恢复：① 按代码跨行回填（同代码任何一行采到板块就用它）；② 按采集顺序
(image_index, row_id) 位置继承——同 category 内把上一只个股的板块带给空板块的续行，
跨 category 重置防止板块串到别的清单/页。仅个股行(row_type=='instrument')参与继承，
板块温度行(row_type=='sector')既不被填也不向下传。
"""
from backend.db import OcrRow, OcrJob


def resolve_sectors(pairs: list[tuple[OcrRow, OcrJob]]) -> dict[int, str | None]:
    """返回 row_id -> 解析后的板块。pairs = [(OcrRow, OcrJob), ...]，传入顺序无所谓。

    已知局限（评审记录在案，有意取舍）：位置继承的边界是 category（非板块组），
    若某板块组的「首行」也丢了板块，它会误继承上一组的板块，而非留 None。
    趋势动物每组通常组头带板块，故实践可接受；好过满屏「板块未知」全拒。
    """
    ordered = sorted(pairs, key=lambda p: (p[1].image_index, p[0].row_id or 0))
    code_map: dict[str, str] = {}
    for row, _job in ordered:
        if row.code and row.sector:
            code_map.setdefault(row.code, row.sector)
    resolved: dict[int, str | None] = {}
    last_sector: str | None = None
    last_cat: str | None = None
    for row, job in ordered:
        cat = job.category or row.market
        if cat != last_cat:          # 跨类别重置，防板块串清单
            last_sector = None
            last_cat = cat
        if row.row_type != "instrument":
            resolved[row.row_id] = row.sector   # 板块温度行/大盘行原样，不参与继承
            continue
        sec = row.sector or (code_map.get(row.code) if row.code else None) or last_sector
        if sec:
            last_sector = sec
        resolved[row.row_id] = sec
    return resolved
