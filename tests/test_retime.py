"""SRT 时间轴重定时算法测试。

运行：python -m pytest tests/ -q
也可直接运行：python -m tests.test_retime
"""
from app.parsers import parse_srt
from app.retime import normalize_points, render_srt, retime_srt

SRT = "\n".join(
    [
        "1",
        "00:00:01,000 --> 00:00:02,500",
        "你好世界",
        "",
        "2",
        "00:00:03,000 --> 00:00:06,000",
        "今天我们来聊聊字幕校对",
        "",
        "3",
        "00:00:07,000 --> 00:00:10,000",
        "编辑距离会忽略标点和空格",
        "",
    ]
)


def _subs():
    return parse_srt(SRT)


def test_single_point_is_uniform_shift():
    rows = retime_srt(_subs(), [(1000, 5000)])
    # 整体 +4000ms
    assert rows[0]["new_start_tc"] == "00:00:05,000"
    assert rows[0]["new_end_tc"] == "00:00:06,500"
    assert rows[2]["new_start_tc"] == "00:00:11,000"
    assert rows[2]["new_end_tc"] == "00:00:14,000"


def test_two_points_piecewise_linear_and_extrapolate():
    # (1s->2s, 10s->20s)：段内斜率 2，区间外沿用斜率 2 外推
    rows = retime_srt(_subs(), [(1000, 2000), (10000, 20000)])
    # 第 2 块入点 3s 在段内：2000 + (3000-1000)*2 = 6000
    assert rows[1]["new_start_tc"] == "00:00:06,000"
    # 第 3 块出点 10s 恰好等于末点
    assert rows[2]["new_end_tc"] == "00:00:20,000"
    # 第 1 块入点 1s 恰好等于首点
    assert rows[0]["new_start_tc"] == "00:00:02,000"


def test_extrapolation_before_first_and_after_last():
    subs = [
        # 在首点之前
        ("00:00:00,000", "00:00:00,500"),
        # 在末点之后
        ("00:01:10,000", "00:01:11,000"),
    ]
    text = _srt(subs)
    # 斜率 2：0 -> 0（首段 0,0→10s,20s 外推），70s -> 20+(70-10)*2 = 140s
    rows = retime_srt(parse_srt(text), [(0, 0), (10000, 20000)])
    assert rows[0]["new_start_tc"] == "00:00:00,000"
    assert rows[0]["new_end_tc"] == "00:00:01,000"
    assert rows[1]["new_start_tc"] == "00:02:20,000"
    assert rows[1]["new_end_tc"] == "00:02:22,000"


def test_rounding_to_nearest_millisecond():
    # 斜率 1/3：3000ms -> 1000ms；2500 -> 833.33 -> 833
    rows = retime_srt(
        parse_srt(_srt([("00:00:02,500", "00:00:03,500")])),
        [(0, 0), (3000, 1000)],
    )
    assert rows[0]["new_start_tc"] == "00:00:00,833"
    # 3500/3 = 1166.67 -> 1167
    assert rows[0]["new_end_tc"] == "00:00:01,167"


def test_points_must_be_strictly_increasing():
    import pytest

    with pytest.raises(ValueError, match="严格递增"):
        normalize_points([(1000, 2000), (1000, 5000)])
    with pytest.raises(ValueError, match="严格递增"):
        normalize_points([(1000, 2000), (5000, 2000)])


def test_negative_time_rejected_with_block_no():
    import pytest

    # 单点平移 -5000ms：校准点 5s->0，第 1 块（1s）变成 -4s
    with pytest.raises(ValueError) as exc:
        retime_srt(_subs(), [(5000, 0)])
    msg = str(exc.value)
    assert "块 1" in msg
    assert "负时间" in msg
    assert "拒绝导出" in msg


def test_start_not_before_end_rejected_with_detail():
    import pytest

    # 极端压缩：0~60s 压到 0~1ms（斜率 1/60000），块时长被取整抹平时，
    # 入点与出点会舍入到同一毫秒
    subs = parse_srt(_srt([
        ("00:00:03,000", "00:00:05,500"),  # 入 0.05ms->0，出 0.0917ms->0
    ]))
    with pytest.raises(ValueError) as exc:
        retime_srt(subs, [(0, 0), (60000, 1)])
    msg = str(exc.value)
    assert "块 1" in msg
    assert "不早于出点" in msg


def test_backward_start_order_rejected_with_block_no():
    import pytest

    # 极端压缩（0~10s 压到 0~1ms）：两块入点相差 1ms，舍入后同为 0，
    # 入点不再严格递增
    subs = parse_srt(_srt([
        ("00:00:04,000", "00:00:06,000"),
        ("00:00:04,001", "00:00:06,001"),
    ]))
    with pytest.raises(ValueError) as exc:
        retime_srt(subs, [(0, 0), (10000, 1)])
    assert "块 2" in str(exc.value)
    assert "倒退" in str(exc.value)


def test_render_srt_renumbers_and_keeps_body_lines():
    subs = parse_srt(
        "\n".join(
            [
                "7",
                "00:00:01,000 --> 00:00:02,000",
                "第一行正文",
                "第二行正文",
                "",
                "9",
                "00:00:03,000 --> 00:00:04,000",
                "另一块",
                "",
            ]
        )
    )
    rows = retime_srt(subs, [(1000, 5000)])
    out = render_srt(subs, rows)
    lines = out.splitlines()
    assert lines[0] == "1"                      # 重新编号
    assert lines[1] == "00:00:05,000 --> 00:00:06,000"
    assert lines[2] == "第一行正文"
    assert lines[3] == "第二行正文"            # 保留正文换行
    assert lines[5] == "2"
    assert "00:00:07,000 --> 00:00:08,000" in out
    # 输出仍可被解析器解析，块数一致
    assert len(parse_srt(out)) == 2


def test_identity_points_keep_timecodes():
    rows = retime_srt(_subs(), [(0, 0), (60000, 60000)])
    assert rows[0]["new_start_tc"] == rows[0]["start_tc"]
    assert rows[2]["new_end_tc"] == rows[2]["end_tc"]


def _srt(pairs):
    blocks = []
    for i, (start, end) in enumerate(pairs, start=1):
        blocks.append(f"{i}\n{start} --> {end}\n字幕{i}")
    return "\n\n".join(blocks) + "\n"


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
