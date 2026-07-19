# Editable PPTX Reconstructor

Reconstruct a slide screenshot or geometric figure as native, editable PowerPoint
objects. Text stays text; bars, icons, diagrams, arrows, lines, gradients, and freeform
curves become PowerPoint shapes. A genuine photo may remain as a separate replaceable
picture, but the tool rejects using the complete screenshot as a hidden background.

The vision pipeline uses a balanced quality profile by default. It starts with
`gpt-5.6-luna`, applies deterministic local corrections, and escalates only persistent
structural failures. The measured correction loop is:

1. local image analysis extracts dimensions, colors, and repeated horizontal geometry;
2. the selected vision model returns a strict object graph with stable IDs;
3. JavaScript renders a real `.pptx` with PptxGenJS;
4. LibreOffice renders the PPTX back to an image;
5. Python scores structure (70%) and color/pixels (30%), including the five worst regions
   and every editable object region;
6. local coordinate/color proposals target the lowest-scoring stable object IDs;
7. if the target is not reached, the model returns only a minimal patch;
8. a candidate becomes the new base only under the global/local regression guards;
9. the final package is checked for PowerPoint-sensitive OOXML errors and can be round-tripped
   through Microsoft PowerPoint itself.

Invalid structured responses are regenerated once with the exact local validation errors.
Refinement requests include enlarged source/rendered crops for the highest-impact editable
objects, and the structured patch schema permits at most four slide elements per pass.
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
- Microsoft PowerPoint M365 is optional for `auto` validation and required for
  `--powerpoint-validation required`

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
  --quality-profile balanced \
  --iterations 1 \
  --powerpoint-validation auto \
  --raster-policy photos-only
```

Quality profiles:

| Profile | Initial model | Local proposals | Escalation |
|---|---|---:|---|
| `budget` | GPT‑5.6 Luna | 0 | none |
| `balanced` | GPT‑5.6 Luna | up to 9 | Terra after a persistent second-pass structural failure |
| `max` | GPT‑5.5 | up to 18 | GPT‑5.5 throughout |

An explicit `--model` overrides routing for every LLM pass. See the official
[model guidance](https://developers.openai.com/api/docs/guides/latest-model) and
[pricing](https://developers.openai.com/api/docs/pricing).

Outputs:

- `slide-editable.pptx`: the editable presentation;
- `slide-editable.spec.json`: the reusable object graph;
- `slide-editable.report.json`: metrics, iteration decisions, and OOXML audit.

`--raster-policy none` forbids every picture object. `photos-only`, the default, allows
only genuine photographs or textures as independent picture objects. `allow` also permits
small raster illustrations, while full-slide flattening remains forbidden.

## Execute production notes visible on a slide

The note-modification service reads authoring instructions already transcribed into the
editable `SlideSpec`, applies the requested structural change, removes the note callout,
renders a new PPTX, and runs semantic plus OOXML validation.

Offline mode is the privacy-first default. It currently executes repeated-row additions
without sending the slide or its text to an external service:

```bash
apply-slide-notes \
  --input "/absolute/path/to/source-slide.png" \
  --spec-in "./out/slide-editable.spec.json" \
  --output "./out/slide-notes-applied.pptx" \
  --engine offline \
  --raster-policy none
```

For broader natural-language edits on sanitized or explicitly authorized material, use
the structured OpenAI engine. It returns a bounded stable-ID patch rather than regenerating
the complete slide, then a second vision pass verifies instruction completion, note removal,
and preservation of unrelated layout:

```bash
apply-slide-notes \
  --input "/absolute/path/to/sanitized-slide.png" \
  --spec-in "./out/slide-editable.spec.json" \
  --output "./out/slide-notes-applied.pptx" \
  --engine openai \
  --model gpt-5.5 \
  --review-iterations 1 \
  --powerpoint-validation auto
```

Do not use `--engine openai` for confidential material unless sending it to the configured
provider is explicitly permitted. The report records detected notes, touched stable IDs,
semantic review, native-object audit, and PowerPoint compatibility status.

## PowerPoint compatibility

Every output receives a package-level Open XML validation covering relationships, content
types, XML integrity, non-visual object IDs, extents, gradient stops, transparency values,
custom geometry, text auto-fit values, and fonts.

```bash
validate-editable-pptx ./out/slide-editable.pptx --mode auto
validate-editable-pptx ./out/slide-editable.pptx --mode required
```

- `off`: OOXML validation only;
- `auto`: real PowerPoint round-trip when a supported local installation exists;
- `required`: fail unless PowerPoint opens, saves, reopens, and exports the presentation.

The real adapter uses PowerPoint COM on Windows and AppleScript on macOS. It exports the
first slide before and after saving, compares their structure, and verifies that native
shape, picture, text, and custom-geometry counts survive. Microsoft documents the native
[Open XML save type](https://learn.microsoft.com/en-us/office/vba/api/powerpoint.ppsaveasfiletype),
[Presentation.SaveCopyAs](https://learn.microsoft.com/en-us/office/vba/api/powerpoint.presentation.savecopyas),
and [Slide.Export](https://learn.microsoft.com/en-us/office/vba/api/powerpoint.slide.export)
operations used by the validation contract.

`--font-policy portable`, the default, replaces unknown brand fonts with an Office-safe
fallback and reports every substitution. `--font-policy exact` preserves the requested
font names; the PowerPoint validation report then records any non-portable fonts for
cross-machine review.

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

To test the correction loop from already accepted specs instead of paying for another full
reconstruction:

```bash
npm run benchmark -- --reuse-existing-spec --quality-profile balanced --iterations 1 --jobs 2
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

- `editable_pptx/`: Python schemas, image analysis, API client, correction loop, object-level
  metrics, raster extraction, visible-note modification, deterministic figure conversion,
  OOXML audits, and native PowerPoint validation adapters.
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
stable text auto-fit, object-region scoring, local stable-ID optimization, model escalation,
OOXML relationships, portable fonts, absence of hidden full-slide rasters, background API
polling, and structure-weighted scoring. The generated report also counts native shapes,
text runs, picture objects, media files, gradients, and Bézier segments. Blank, malformed,
or OOXML-incompatible PPTX files never count as successful benchmark cases.

The normal GitHub workflow builds the wheel and validates a deterministic compatibility
fixture without an API call. `.github/workflows/powerpoint-compatibility.yml` is a manual
workflow for licensed self-hosted M365 Windows and macOS runners.

## Limitations

- Fonts unavailable on the rendering machine are substituted by PowerPoint or LibreOffice.
- A photograph stays a raster picture; it cannot become meaningful editable vector objects.
- Dense illustrations can require many native shapes and a longer model response.
- Camera perspective, glare, and moiré reduce source certainty. A clean exported slide image
  produces a better reconstruction than a phone photograph.
- The benchmark invokes a paid API and can take several minutes per complex slide.
- Real PowerPoint round-trip validation cannot run on a machine without a licensed local
  Microsoft PowerPoint installation. `auto` reports this explicitly; it never claims that
  LibreOffice is PowerPoint.

The optional legacy `npm run convert:layout` command resizes and reflows an existing PPTX;
it is separate from image reconstruction.

## License

MIT. Benchmark source documents retain their original publishers' terms and are downloaded
only when the benchmark is run.
