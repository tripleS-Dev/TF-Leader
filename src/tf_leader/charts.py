from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Literal, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .models import PlayerHistoryPoint


ChartKind = Literal["score", "rank"]


def plot_history(
    history: Sequence[PlayerHistoryPoint],
    *,
    kind: ChartKind,
    output: str | Path,
    title: str | None = None,
) -> Path:
    image = render_history_png(history, kind=kind, title=title)
    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image)
    return output_path


def render_history_png(
    history: Sequence[PlayerHistoryPoint],
    *,
    kind: ChartKind,
    title: str | None = None,
) -> bytes:
    if not history:
        raise ValueError("그래프로 만들 유저 이력이 없습니다.")
    if kind not in ("score", "rank"):
        raise ValueError("kind는 'score' 또는 'rank'여야 합니다.")

    x = [point.source_updated_at for point in history]
    y = [point.score if kind == "score" else point.rank for point in history]
    player_name = history[-1].display_name
    ylabel = "Score" if kind == "score" else "Rank"

    fig, ax = plt.subplots(figsize=(10, 5.5), constrained_layout=True)
    ax.plot(x, y, color="#d84b35", marker="o", linewidth=2, markersize=5)
    ax.set_title(title or f"{player_name} - {ylabel} History")
    ax.set_xlabel("Leaderboard updated at (UTC)")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(_format_timestamp))
    ax.margins(x=0.05)

    if kind == "rank":
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        if min(y) == max(y):
            pad = max(1.0, abs(y[0]) * 0.02)
            ax.set_ylim(max(0.5, y[0] - pad), y[0] + pad)
        ax.invert_yaxis()
    else:
        if min(y) == max(y):
            pad = max(100.0, abs(y[0]) * 0.01)
            ax.set_ylim(y[0] - pad, y[0] + pad)
        ax.ticklabel_format(axis="y", style="plain", useOffset=False)

    for point_x, point_y in zip(x, y, strict=True):
        ax.annotate(
            f"{point_y:,}",
            (point_x, point_y),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=8,
        )

    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=160)
    plt.close(fig)
    return buffer.getvalue()


def _format_timestamp(value: float, _position: int | None = None) -> str:
    timestamp = mdates.num2date(value)
    return (
        f"{timestamp:%m-%d}\n{timestamp:%H:%M:%S}.{timestamp.microsecond // 1000:03d}"
    )
