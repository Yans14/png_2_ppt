# Editable PPTX Reconstructor

Reconstruct a slide screenshot or geometric figure as native, editable PowerPoint
objects. Text stays text; bars, icons, diagrams, arrows, lines, gradients, and freeform
curves become PowerPoint shapes. A genuine photo may remain as a separate replaceable
picture, but the tool rejects using the complete screenshot as a hidden background.

The vision pipeline uses GPT‑5.5 by default, accepts a cheaper model through `--model`,
and follows a measured correction loop:

1. local image analysis extracts dimensions, colors, and repeated horizontal geometry;
2. the selected vision model returns a strict object graph with stable IDs;
3. JavaScript renders a real `.pptx` with PptxGenJS;
4. LibreOffice renders the PPTX back to an image;
5. Python scores structure (70%) and color/pixels (30%), including the five worst regions;
6. if the target is not reached, the model returns only a minimal patch;
7. a candidate becomes the new base only when its measured score improves.

Invalid structured responses are regenerated once with the exact local validation errors.
The corrector also treats object topology—such as circle versus rounded rectangle or a
faceted versus smooth curve—as a hard structural requirement. The renderer materializes
PowerPoint text auto-fit scales so text remains stable when another tool resizes or inspects
the slide.

This is shape reconstruction, not screenshot tracing. The objective is an editable slide
with the same visual structure, rather than a pixel-perfect raster copy.

## Requirements

- Python 3.10+
- Node.js 18+
- LibreOffice for PPTX-to-PDF rendering
- Poppler (`pdftoppm`) for PDF-to-PNG rendering
- an OpenAI API key for vision reconstruction or LLM-assisted refinement

On macOS with Homebrew:

```bash
brew install libreoffice poppler node python
```

## Installation

```bash
git clone <repository-url>
cd editable-pptx-reconstructor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
npm ci
cp .env.example .env
```

Add `OPENAI_API_KEY` to `.env`, or export it in the shell. The project never logs the key,
and `.env` is ignored by Git.

## Reconstruct a slide image

```bash
image-to-editable-pptx \
  --input "/absolute/path/to/slide.png" \
  --output "./out/slide-editable.pptx" \
  --model gpt-5.6-luna \
  --iterations 1 \
  --raster-policy photos-only
```

`gpt-5.6-luna` is the validated budget profile. OpenAI describes it as the GPT‑5.6 model
for cost-sensitive, high-volume workloads; it supports image input and structured outputs.
Use `gpt-5.6-terra` for a higher-quality/cost balance, or omit `--model` to retain the
GPT‑5.5 quality default. See the official [model guidance](https://developers.openai.com/api/docs/guides/latest-model)
and [pricing](https://developers.openai.com/api/docs/pricing).

Outputs:

- `slide-editable.pptx`: the editable presentation;
- `slide-editable.spec.json`: the reusable object graph;
- `slide-editable.report.json`: metrics, iteration decisions, and OOXML audit.

`--raster-policy none` forbids every picture object. `photos-only`, the default, allows
only genuine photographs or textures as independent picture objects. `allow` also permits
small raster illustrations, while full-slide flattening remains forbidden.

An existing spec can be adjusted manually and rendered without another initial model call:

```bash
image-to-editable-pptx \
  --input "/absolute/path/to/slide.png" \
  --spec-in "./out/slide-editable.spec.json" \
  --output "./out/slide-editable-v2.pptx" \
  --iterations 0
```

## Convert a single figure

The deterministic figure pipeline converts SVG or high-contrast raster artwork into
PowerPoint custom geometry. SVG cubic curves and linear gradients remain native.

```bash
figure-to-editable-pptx \
  --input "./arrow.svg" \
  --output "./out/arrow-editable.pptx" \
  --canvas 1280x720 \
  --refine optimize \
  --iterations 2
```

`--refine optimize` combines GPT‑5.5 proposals with numerical loss acceptance and local
coordinate/gradient descent. The measured loss—not the model—decides whether a proposal
is retained.

## Reproducible six-slide benchmark

The repository includes a manifest with six varied public slides: an illustrated process,
a pyramid, a chart dashboard, two financial diagrams, and a photo-led layout. Source PDFs
are downloaded into an ignored cache; third-party PDFs are not committed.

```bash
npm run benchmark:fetch
npm run benchmark -- --model gpt-5.6-luna --iterations 1 --jobs 2
```

After changing only the renderer or metrics, rescore existing object graphs without any API
call:

```bash
npm run benchmark -- --rescore-existing --jobs 2
```

Results are written to `benchmarks/results/summary.json` and `summary.csv`. Each case also
keeps its PPTX, strict spec, render intermediates, stdout/stderr, and audit report. Use
`--case ida-hybrid-model` to run one case and `--force` to replace cached results.
Subset summaries use their own filename, and offline rescoring writes
`summary-rescore.json`, so neither operation can overwrite the canonical full-run summary.

See [BENCHMARK.md](BENCHMARK.md) for the measured evaluation and the changes it drove.

The metric deliberately weights geometry and layout more heavily than exact color pixels.
It is still only a regression signal: final releases should also inspect the rendered
montage and open representative PPTX files in PowerPoint.

## Architecture

- `editable_pptx/`: Python schemas, image analysis, API client, correction loop, metrics,
  raster extraction, deterministic figure conversion, and OOXML audits.
- `src/render-image-spec.js`: native PptxGenJS renderer for the strict object graph.
- `editable_pptx/js/`: compiled renderer shipped with the Python package.
- `benchmarks/manifest.json`: public benchmark sources, pages, and categories.
- `scripts/`: benchmark download and execution tools.
- `test/`: Node and Python unit/integration tests.

After editing the renderer source, rebuild the packaged bundle:

```bash
npm run build:renderer
```

## Verification

```bash
npm run test:all
```

Tests validate schemas, non-empty slide graphs, semantic-response regeneration, correction
patches, component transforms, reversed-line geometry, native cubic curves, native gradients,
stable text auto-fit, absence of hidden full-slide rasters, background API polling, and
structure-weighted scoring. The generated report also counts native shapes, text runs,
picture objects, media files, gradients, and Bézier segments. Blank or otherwise unusable
PPTX files never count as successful benchmark cases.

## Limitations

- Fonts unavailable on the rendering machine are substituted by PowerPoint or LibreOffice.
- A photograph stays a raster picture; it cannot become meaningful editable vector objects.
- Dense illustrations can require many native shapes and a longer model response.
- Camera perspective, glare, and moiré reduce source certainty. A clean exported slide image
  produces a better reconstruction than a phone photograph.
- The benchmark invokes a paid API and can take several minutes per complex slide.

The optional legacy `npm run convert:layout` command resizes and reflows an existing PPTX;
it is separate from image reconstruction.

## License

MIT. Benchmark source documents retain their original publishers' terms and are downloaded
only when the benchmark is run.
