"""SRT 时间轴重定时：按「原时间 → 目标时间」校准点做分段线性映射。

规则：

- 校准点两列时间都必须严格递增；
- 相邻校准点之间，对每块字幕的入点/出点做线性插值；
- 首点之前、末点之后沿用最近区段的斜率外推；
- 只有一个校准点时整体平移（斜率 1）；
- 映射结果统一四舍五入到毫秒。

映射后若出现负时间、入点不早于出点、或相邻字幕块入点顺序倒退，
抛出带具体块号的 ValueError，由调用方拒绝导出。
"""
from __future__ import annotations

import bisect
import math

from .parsers import SrtItem, format_timecode


def normalize_points(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """校验校准点：时间非负，原时间列与目标时间列均严格递增。"""
    norm: list[tuple[int, int]] = []
    for i, (src, dst) in enumerate(points, start=1):
        if src < 0 or dst < 0:
            raise ValueError(f"第 {i} 个校准点含负时间，时间不能为负")
        if norm:
            prev_src, prev_dst = norm[-1]
            if src <= prev_src:
                raise ValueError(
                    f"第 {i} 个校准点的原时间 {format_timecode(src)} 不晚于前一点 "
                    f"{format_timecode(prev_src)}：两列时间都必须严格递增"
                )
            if dst <= prev_dst:
                raise ValueError(
                    f"第 {i} 个校准点的目标时间 {format_timecode(dst)} 不晚于前一点 "
                    f"{format_timecode(prev_dst)}：两列时间都必须严格递增"
                )
        norm.append((src, dst))
    return norm


def _round_half_away_from_zero(x: float) -> int:
    """四舍五入到整数毫秒（负数对称，-0.5 → -1）。"""
    if x >= 0:
        return int(math.floor(x + 0.5))
    return -int(math.floor(-x + 0.5))


def retime_srt(
    subs: list[SrtItem], raw_points: list[tuple[int, int]]
) -> list[dict]:
    """对解析后的字幕块执行重定时，返回供预览/导出的行数据。

    校验不通过时抛出 ValueError，消息中列出具体块号。
    """
    if not raw_points:
        raise ValueError("至少需要一个校准点")
    points = normalize_points(raw_points)
    xs = [src for src, _ in points]

    def mapped(t: int) -> float:
        if len(points) == 1:
            # 单点：整体平移
            a0, b0 = points[0]
            return float(b0 + (t - a0))
        if t <= xs[0]:
            # 首点之前：沿用第一段斜率外推
            (a0, b0), (a1, b1) = points[0], points[1]
        elif t >= xs[-1]:
            # 末点之后：沿用最后一段斜率外推
            (a0, b0), (a1, b1) = points[-2], points[-1]
        else:
            i = bisect.bisect_right(xs, t) - 1
            a0, b0 = points[i]
            a1, b1 = points[i + 1]
        slope = (b1 - b0) / (a1 - a0)
        return b0 + (t - a0) * slope

    rows: list[dict] = []
    for block_no, sub in enumerate(subs, start=1):
        new_start = _round_half_away_from_zero(mapped(sub.start_ms))
        new_end = _round_half_away_from_zero(mapped(sub.end_ms))
        rows.append(
            {
                "index": block_no,
                "text": sub.raw_text,
                "start_tc": sub.start_tc,
                "end_tc": sub.end_tc,
                "new_start_tc": format_timecode(new_start),
                "new_end_tc": format_timecode(new_end),
                # 供导出与校验使用，不参与页面展示
                "new_start_ms": new_start,
                "new_end_ms": new_end,
            }
        )

    # 正斜率下映射严格保序，四舍五入后不会严格变小，但极端压缩可能把不同
    # 入点舍入到同一毫秒（顺序不再严格递增），同样按倒退处理。
    negatives = [r for r in rows if r["new_start_ms"] < 0 or r["new_end_ms"] < 0]
    inverted = [r for r in rows if r["new_start_ms"] >= r["new_end_ms"]]
    backwards = [
        rows[i]
        for i in range(1, len(rows))
        if rows[i]["new_start_ms"] <= rows[i - 1]["new_start_ms"]
    ]

    problems: list[str] = []
    if negatives:
        nums = "、".join(str(r["index"]) for r in negatives)
        problems.append(
            f"块 {nums} 映射后出现负时间（如块 {negatives[0]['index']} "
            f"原入点 {negatives[0]['start_tc']}）"
        )
    if inverted:
        detail = "；".join(
            f"块 {r['index']} 入点 {r['new_start_tc']} 不早于出点 "
            f"{r['new_end_tc']}"
            for r in inverted[:5]
        )
        if len(inverted) > 5:
            detail += f" 等共 {len(inverted)} 块"
        problems.append(detail)
    if backwards:
        detail = "；".join(
            f"块 {r['index']} 入点 {r['new_start_tc']} 未严格晚于前一块 "
            f"{rows[r['index'] - 2]['new_start_tc']}"
            for r in backwards[:5]
        )
        if len(backwards) > 5:
            detail += f" 等共 {len(backwards)} 块"
        problems.append(f"字幕入点顺序倒退：{detail}")
    if problems:
        raise ValueError("；".join(problems) + "，已拒绝导出，请调整校准点")
    return rows


def render_srt(subs: list[SrtItem], rows: list[dict]) -> str:
    """用重定时后的时间码重新生成 SRT：保留正文原换行，序号从头重排。"""
    blocks: list[str] = []
    for new_index, (sub, row) in enumerate(zip(subs, rows), start=1):
        lines = [
            str(new_index),
            f"{row['new_start_tc']} --> {row['new_end_tc']}",
            *sub.body_lines,
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
