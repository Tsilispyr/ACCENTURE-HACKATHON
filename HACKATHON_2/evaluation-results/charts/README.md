# Charts

Generated, not drawn. Every image here is rendered from `evaluation-results/results.json`,
so a number on a slide is a number something measured.

```bash
uv run python -m evaluation.charts --domain sample_policy
```

| Image | Shows | Source experiment |
|---|---|---|
| `retrieval_arms.png` | accuracy of each retrieval method at three cutoffs | `retrieval` |
| `metadata_filter.png` | the single largest win, as two bars | `retrieval` |
| `calibration.png` | real and nonsense question distances, and where the threshold sits | `calibration` |
| `chunking_shape.png` | pages, sections and chunks after structure aware chunking | `chunking` |
| `pipeline_costs.png` | seconds per pipeline stage | `stage_timing` |

Re-run the evals first, then re-render. A chart is only as current as the last measurement:

```bash
DOMAIN=sample_policy uv run python -m evaluation.retrieval_eval --force-hybrid
DOMAIN=sample_policy uv run python -m evaluation.calibrate
uv run python -m evaluation.charts
```

Conventions, so a new chart matches the rest: one measure per axis and never two scales,
categorical colours assigned in a fixed order, values printed on the marks, a recessive grid with
no chart border, a legend only when there are two or more series, and plain ASCII in every title.
