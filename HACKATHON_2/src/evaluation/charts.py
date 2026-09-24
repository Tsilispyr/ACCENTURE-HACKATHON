"""Render the ledger as presentation images.

    uv run python -m evaluation.charts

Writes PNGs to evaluation-results/charts/. Static images on purpose: these go on
slides, where a hover tooltip is worth nothing and a 300 DPI file is worth a
lot.

Design rules followed here, so nobody has to re-derive them:
  one measure per axis, never two scales on one chart
  categorical hues assigned in fixed order, never cycled
  recessive grid, no chart border, no vertical gridlines on a bar chart
  values labelled directly on the marks, so the axis is a reference not a lookup
  a legend only when there are two or more series
  plain ASCII in every title and label
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display on this machine and none needed
import matplotlib.pyplot as plt  # noqa: E402

from evaluation.ledger import latest, load  # noqa: E402

CHARTS_DIR = Path(__file__).resolve().parents[2] / "evaluation-results" / "charts"

# Validated categorical palette, assigned in fixed order. Slot 1 is the
# subject of the chart; later slots are comparisons.
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e3e2de"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        # A FALLBACK CHAIN, not one name. Matplotlib walks it and takes the
        # first font present, so a light face is used where one exists and
        # DejaVu Sans - which matplotlib always ships - keeps CI and Linux
        # containers rendering identically shaped charts.
        "font.family": ["Segoe UI Light", "Segoe UI", "Corbel", "Candara",
                        "Gill Sans MT", "DejaVu Sans"],
        "font.size": 11,
        "axes.labelcolor": INK_SOFT,
        "text.color": INK,
        "xtick.color": INK_SOFT,
        "ytick.color": INK_SOFT,
        "axes.edgecolor": GRID,
    }
)


# Titles are set in ONE place so all seven charts agree. A title earns
# attention by sitting alone above the plot, not by being heavy: at projector
# size a 600 weight title competes with the bars it is labelling.
#
# The thinness comes from the FAMILY, not from a weight. "Segoe UI Light" is
# its own family; asking for fontweight="light" instead makes matplotlib hunt
# for a light face in families that have none and print a findfont warning per
# chart while falling back to normal anyway.
TITLE = {"fontsize": 15, "color": INK, "pad": 16, "loc": "left"}


def _title(ax, text: str) -> None:
    ax.set_title(text, **TITLE)


def _style(ax, *, title: str, ylabel: str = "", ymax: float | None = None) -> None:
    """The recessive frame every chart here shares."""
    _title(ax, title)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=10)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    # Horizontal only: a vertical gridline on a bar chart adds nothing and
    # crosses the marks.
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    if ymax:
        ax.set_ylim(0, ymax)


def _label_bars(ax, bars, fmt="{:.0%}") -> None:
    """Values on the marks. The axis becomes a reference, not a lookup."""
    for bar in bars:
        height = bar.get_height()
        ax.annotate(
            fmt.format(height),
            (bar.get_x() + bar.get_width() / 2, height),
            textcoords="offset points", xytext=(0, 4),
            ha="center", fontsize=10, color=INK,
        )


def _save(fig, name: str) -> Path:
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHARTS_DIR / name
    fig.tight_layout()
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    return path


# ------------------------------------------------------------------ charts --


def retrieval_arms(domain: str = "sample_policy") -> Path | None:
    """Grouped bars: each retrieval arm at three cutoffs.

    Grouped rather than stacked because these are independent measures of the
    same arm, not parts of a whole.
    """
    data = latest("retrieval", domain)
    if not data:
        return None

    # TWO NAMES FOR ONE ARM. retrieval_eval writes "hybrid" when forced with
    # --force-hybrid and "hybrid (v+bm25)" on the normal path, so which row
    # exists depends on how it was last invoked. Reading only one name made
    # the chart plot whichever stale row happened to match, or drop the bar
    # entirely. Prefer the normal-path row, fall back to the forced one.
    if "hybrid" not in data and "hybrid (v+bm25)" in data:
        data["hybrid"] = data["hybrid (v+bm25)"]
    order = [a for a in ("vector", "hybrid", "unfiltered") if a in data]
    metrics = ["recall@1", "recall@3", "recall@5"]
    colours = {"vector": BLUE, "hybrid": ORANGE, "unfiltered": AQUA}

    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    width = 0.8 / len(order)

    for i, arm in enumerate(order):
        values = [data[arm].get(m, 0) for m in metrics]
        offsets = [x + i * width - 0.4 + width / 2 for x in range(len(metrics))]
        bars = ax.bar(offsets, values, width * 0.88, label=arm.capitalize(),
                      color=colours.get(arm, BLUE), zorder=3)
        _label_bars(ax, bars)

    ax.set_xticks(range(len(metrics)))
    ax.set_xticklabels(["Top 1", "Top 3", "Top 5"])
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    _style(ax, title="Retrieval accuracy by method", ylabel="Correct section found", ymax=1.12)
    ax.legend(frameon=False, ncol=len(order), loc="upper left",
              bbox_to_anchor=(0, -0.12), fontsize=10)
    return _save(fig, "retrieval_arms.png")


def metadata_filter_effect(domain: str = "sample_policy") -> Path | None:
    """The headline result, as two bars. One measure, one comparison."""
    data = latest("retrieval", domain)
    if "vector" not in data or "unfiltered" not in data:
        return None

    labels = ["No filter", "Articles only"]
    values = [data["unfiltered"].get("recall@1", 0), data["vector"].get("recall@1", 0)]

    fig, ax = plt.subplots(figsize=(5.4, 4.2))
    bars = ax.bar(labels, values, 0.5, color=[AQUA, BLUE], zorder=3)
    _label_bars(ax, bars)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    _style(ax, title="Effect of one metadata filter", ylabel="Top 1 accuracy", ymax=1.12)
    # One series, so no legend: the title names what is plotted.
    return _save(fig, "metadata_filter.png")


def calibration(domain: str = "sample_policy") -> Path | None:
    """Two distributions on one number line, and the gap between them.

    A dot plot rather than bars: these are individual measurements, and their
    SPREAD is the point. The gap is where the threshold goes.
    """
    rows = [r for r in load("calibration", domain)]
    if not rows:
        return None
    detail = rows[-1]["detail"]
    real, junk = detail.get("real", []), detail.get("nonsense", [])
    if not real or not junk:
        return None

    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    ax.scatter(real, [1] * len(real), s=90, color=BLUE, zorder=3,
               label="Real questions", edgecolor=SURFACE, linewidth=1.5)
    ax.scatter(junk, [0.55] * len(junk), s=90, color=ORANGE, zorder=3,
               label="Nonsense questions", edgecolor=SURFACE, linewidth=1.5)

    # THE LIVE CEILING, not the recorded one. The ledger stores
    # `configured_ceiling` as it was WHEN CALIBRATE RAN, so the moment anyone
    # acts on the suggestion the recorded value is one behind - and the chart
    # drew a threshold nobody was using. It showed 0.7 while the system ran on
    # 0.64, which is worse than drawing no line at all, because the whole point
    # of this chart is where the line sits relative to the two clouds.
    try:
        from agentcore.registry import load_domain

        threshold = load_domain(domain).retrieval_policy().max_distance
    except Exception:  # noqa: BLE001 - a chart must not need a loadable domain
        threshold = rows[-1]["metrics"].get("configured_ceiling")
    if threshold:
        ax.axvline(threshold, color=INK_SOFT, linewidth=1.5, linestyle="--", zorder=2)
        ax.annotate(f"Threshold {threshold}", (threshold, 1.32), ha="center",
                    fontsize=10, color=INK)

    ax.set_ylim(0.2, 1.5)
    ax.set_yticks([])
    ax.set_xlabel("Distance from question, lower is closer")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    _title(ax, "Where the relevance threshold goes")
    ax.legend(frameon=False, ncol=2, loc="upper left",
              bbox_to_anchor=(0, -0.28), fontsize=10)
    return _save(fig, "calibration.png")


def pipeline_costs(domain: str = "sample_policy") -> Path | None:
    """Horizontal bars: which stage spends the time.

    Horizontal because the labels are stage names, and a rotated x label is
    harder to read than a left-aligned y one.
    """
    data = latest("stage_timing", domain)
    if not data:
        return None

    names = list(data)
    values = [data[n].get("seconds", 0) for n in names]
    pairs = sorted(zip(names, values), key=lambda p: p[1])

    fig, ax = plt.subplots(figsize=(7.0, 4.4))
    bars = ax.barh([p[0] for p in pairs], [p[1] for p in pairs], 0.6,
                   color=BLUE, zorder=3)
    for bar in bars:
        width = bar.get_width()
        ax.annotate(f"{width:.1f}s", (width, bar.get_y() + bar.get_height() / 2),
                    textcoords="offset points", xytext=(5, 0),
                    va="center", fontsize=10, color=INK)

    ax.set_xlabel("Seconds")
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    _title(ax, "Time spent per pipeline stage")
    return _save(fig, "pipeline_costs.png")


def chunking_shape(domain: str = "sample_policy") -> Path | None:
    """How the corpus was cut up. Counts, so bars."""
    data = latest("chunking", domain)
    if not data or "corpus" not in data:
        return None
    m = data["corpus"]

    labels = ["Pages", "Sections", "Chunks"]
    values = [m.get("pages", 0), m.get("sections", 0), m.get("chunks", 0)]

    fig, ax = plt.subplots(figsize=(5.8, 4.2))
    bars = ax.bar(labels, values, 0.5, color=BLUE, zorder=3)
    _label_bars(ax, bars, fmt="{:.0f}")
    _style(ax, title="Corpus after structure aware chunking", ylabel="Count",
           ymax=max(values) * 1.18)
    return _save(fig, "chunking_shape.png")


def store_backends(domain: str = "sample_policy") -> Path | None:
    """Two stores, same corpus, same questions. The point is that they match.

    A chart where the bars are equal is usually a bad chart. Here it is the
    finding: the store is swappable, so a laptop with no database can still run
    and measure the whole pipeline.
    """
    data = latest("backend", domain)
    if len(data) < 2:
        return None

    arms = list(data)
    metrics = ["recall@1", "recall@3", "mrr"]
    labels = ["Top 1", "Top 3", "Rank score"]
    colours = [BLUE, AQUA]

    fig, ax = plt.subplots(figsize=(6.6, 4.2))
    width = 0.8 / len(arms)
    for i, arm in enumerate(arms):
        values = [data[arm].get(m, 0) for m in metrics]
        offsets = [x + i * width - 0.4 + width / 2 for x in range(len(metrics))]
        bars = ax.bar(offsets, values, width * 0.88, label=arm.capitalize(),
                      color=colours[i % len(colours)], zorder=3)
        _label_bars(ax, bars, fmt="{:.2f}")

    ax.set_xticks(range(len(metrics)))
    ax.set_xticklabels(labels)
    _style(ax, title="Two vector stores, identical results", ylabel="Score", ymax=1.15)
    ax.legend(frameon=False, ncol=2, loc="upper left",
              bbox_to_anchor=(0, -0.12), fontsize=10)
    return _save(fig, "store_backends.png")


def run_cost(domain: str = "sample_policy") -> Path | None:
    """What one request costs, split into the tokens that were paid for.

    THE CHART THAT REPLACES SQUINTING AT A TRACE VIEWER. Token counts exist in
    the hosted trace UI, one run at a time, behind a login, at a font size no
    projector survives. Section 10 asks whether execution is operationally
    reasonable, and that question is answered by a number on a slide.

    Input and output are separated because they are priced differently and
    behave differently: input grows with the corpus and the prompt, output with
    how much the model chooses to write. A single "total tokens" bar hides
    which of those to go after when the bill is too high.
    """
    data = latest("usage", domain)
    if "per_run" not in data:
        return None
    row = data["per_run"]

    tokens_in = row.get("input_tokens", 0)
    tokens_out = row.get("output_tokens", 0)
    if not (tokens_in or tokens_out):
        return None

    # Wide and short: two bars in a tall frame read as slabs. A slide wants
    # the shape of the comparison, which is horizontal.
    fig, ax = plt.subplots(figsize=(6.6, 3.2))
    bars = ax.barh(["Output", "Input"], [tokens_out, tokens_in], 0.38,
                   color=[ORANGE, BLUE], zorder=3)
    for bar, value in zip(bars, (tokens_out, tokens_in)):
        ax.text(bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                f"{value:,.0f}", va="center", fontsize=10, color=INK)

    ax.xaxis.set_major_formatter(lambda v, _: f"{v / 1000:.0f}k" if v else "0")
    ax.set_xlim(0, max(tokens_in, tokens_out) * 1.18)
    _title(ax, "Tokens in one full assessment")

    # The euro figure is a SUBTITLE, not a bar. It is derived from the tokens
    # already plotted, so drawing it again would say the same thing twice, and
    # it is absent whenever no rate is configured rather than shown as zero.
    # Prefer a cost computed NOW over one frozen into the ledger. Token counts
    # are a measurement and never change; a rate is a fact about the world that
    # does. Recomputing at render time means updating .env re-prices every past
    # run, instead of leaving old rows quoting a rate that has since moved.
    from evaluation.usage import Usage

    cost = Usage(input_tokens=tokens_in, output_tokens=tokens_out).cost()
    if cost is None:
        cost = row.get("cost_eur")
    seconds = row.get("seconds")
    caption = f"{tokens_in + tokens_out:,.0f} tokens"
    if seconds:
        caption += f" in {seconds:.0f}s"
    if cost:
        caption += f"   EUR {cost:.4f} per run, EUR {cost * 1000:.2f} per 1,000"
    else:
        caption += "   not priced: set LLM_PRICE_INPUT_PER_M and LLM_PRICE_OUTPUT_PER_M"
    ax.text(0, -0.30, caption, transform=ax.transAxes, fontsize=10, color=INK_SOFT)

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.xaxis.grid(True, color=GRID, zorder=0)
    ax.set_axisbelow(True)
    return _save(fig, "run_cost.png")


def render_all(domain: str = "sample_policy") -> list[Path]:
    print(f"Rendering charts for '{domain}'")
    made = [
        chart(domain)
        for chart in (retrieval_arms, metadata_filter_effect, calibration,
                      chunking_shape, pipeline_costs, store_backends,
                      run_cost)
    ]
    made = [p for p in made if p]
    if not made:
        print("  nothing to plot yet. Run the evals first.")
    return made


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default="sample_policy")
    args = parser.parse_args()
    render_all(args.domain)
