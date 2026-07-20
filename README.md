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

For the local HTTP service and the GPT-5.5 quality loop, install both optional extras:

```bash
pip install -e '.[api,agents]'
editable-pptx-api --with-worker
editable-pptx-doctor
```

Swagger is available at `http://127.0.0.1:8765/docs`. The service reuses
`OPENAI_API_KEY` from the process environment or the project `.env`; the value is read
into memory and is never copied into SQLite, job artifacts, reports, or logs.

## Asynchronous editing API

Version 2.1 exposes six independent editing job endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /v1/image-to-editable` | one or more slide images to a native editable deck |
| `POST /v1/figure-to-editable` | SVG/raster figure to PowerPoint custom geometry |
| `POST /v1/notes` | execute API text, comments, speaker notes, and visible callouts |
| `POST /v1/beautify` | improve a PPTX while preserving business content |
| `POST /v1/render` | render all slides to PDF and PNG previews |
| `POST /v1/validate` | OOXML, portability, and optional real-PowerPoint checks |

Each mutation endpoint accepts `mode=plan` or `mode=apply`. Planning produces a
checksummed JSON patch without changing the source. Apply the reviewed plan with
`POST /v1/plans/{plan_id}/apply`; a changed source checksum invalidates the plan.
Inputs can be uploads or `source_artifact_id` values from an earlier job, so a deck can
flow through reconstruction, notes, beautification, render, and validation without being
uploaded again.

Jobs are polled at `/v1/jobs/{job_id}` or streamed as server-sent events at
`/v1/jobs/{job_id}/events`. Cancellation, retry, artifact download, and a 30-day local
artifact TTL are supported. SQLite stores metadata; immutable files live in the service
home (`~/.local/share/editable-pptx-service` by default).
`POST /v1/jobs/{job_id}/bundle` creates an optional ZIP containing `manifest.json` and
all individual artifacts.

Notes and beautification use GPT‑5.5 for planning and visual review. Beautification runs
an Analyst → Designer → deterministic executor → Reviewer pipeline. It creates up to three
independent candidates from the immutable source, runs two Designer calls concurrently for
multi-slide decks, rejects hard-gate failures before visual review, and selects valid
candidates only by the Reviewer score. Scores between 7.5 and 8.5 receive an independent
second vote. If any selected slide remains below 8, the whole deck receives the terminal
`failed_quality` status; the best invariant-safe diagnostic candidate remains downloadable.

The default job artifact TTL is 30 days. Template decks live in a separate persistent
catalog and remain there until an explicit soft delete. Metadata traces contain only IDs,
hashes, model/tool names, scores, costs, and latency unless `trace_level=full` is requested.

The note-source priority is API instruction, PowerPoint comments, speaker notes, then
visible authoring callouts. Unsupported native objects such as charts, SmartArt, OLE,
media, relationships, and animations are preserved in place because edits are surgical
OOXML mutations, not a full deck regeneration. PPTM input is intentionally rejected.

Example:

```bash
curl -F file=@deck.pptx \
  -F mode=apply \
  -F slides=all \
  -F max_attempts=3 \
  http://127.0.0.1:8765/v1/notes

curl -F source_artifact_id="$PPTX_ARTIFACT_ID" \
  -F template=@template.pptx \
  -F instruction='Tighten hierarchy and spacing; preserve all business content' \
  -F restyle_mode=auto \
  -F max_candidates=3 \
  -F max_cost_usd=5 \
  -F trace_level=metadata \
  http://127.0.0.1:8765/v1/beautify
```

The API is localhost-only by default. Binding to another interface is refused unless
`EDITABLE_PPTX_BEARER_TOKEN` is configured; when set, all non-health API calls require
`Authorization: Bearer …`.

### GPT-5.5 beautification contract

`POST /v1/beautify` keeps all historical fields and adds:

| Field | Default | Contract |
|---|---:|---|
| `restyle_mode` | `auto` | `auto`, `conservative`, `structural`, or `rebuild` |
| `catalog_enabled` | `true` | enable structural template matching |
| `max_candidates` | `3` | one to three independent candidates |
| `body_min_font_size_pt` | `8` | hard minimum for body text |
| `source_min_font_size_pt` | `6` | hard minimum for sources and footnotes |
| `max_cost_usd` | `5` | per-deck GPT-5.5 budget |
| `timeout_seconds` | `900` | timeout applied to external and rendering phases |
| `trace_level` | `metadata` | `none`, `metadata`, or `full` |
| `powerpoint_validation` | `auto` | `off`, `auto`, or `required` |

Legacy `max_attempts` remains accepted and is capped at three with a
`request.warning` SSE event. `mode=plan` returns the analysis, selected template, typed
operation plan, scorecard, and previews without publishing a final PPTX.

Every candidate must keep the slide count, case-insensitive word multiset, numbers, table
cells, native chart series, original image/logo hashes, protected relationships, animation
XML, and transitions. Tables and charts remain native; photos may be cropped or repositioned;
logos may only be moved or proportionally resized. SmartArt, OLE, embedded workbooks, and
animation timelines block rebuild and force structural OOXML editing.

### Local template catalog

Template import is asynchronous and stores immutable full decks on disk with a SQLite
index, SHA-256 and perceptual deduplication, inferred families, per-slide archetypes, and
structure-dominant features. GPT-5.5 reranks only the deterministic top five, and automatic
templates are used only above 0.75 confidence. An uploaded job template forces its family.

| Endpoint | Purpose |
|---|---|
| `POST /v1/templates/import` | render, deduplicate and index a PPTX |
| `GET /v1/templates` / `GET /v1/templates/{id}` | inspect catalog entries |
| `DELETE /v1/templates/{id}` | soft delete |
| `POST /v1/templates/{id}/restore` | restore a soft-deleted template |
| `POST /v1/templates/reindex` | incremental reindex job |
| `/v1/template-families/*` | rename, enable/disable, merge and split families |
| `GET /v1/jobs/{id}/candidates` | candidate PPTX, previews and scorecards |
| `GET /v1/jobs/{id}/trace` | trace allowed by the job trace level |

The same operations are available locally:

```bash
editable-pptx-templates import ./templates/public-template.pptx
editable-pptx-templates list
editable-pptx-templates families
editable-pptx-templates reindex
```

The provider boundary is intentionally isolated in `editable_pptx/agents_runtime.py`.
Version 2.1 ships only the OpenAI provider; there is no silent model downgrade. A future
Azure adapter can implement the same factory without changing the deterministic executor.

### Run the 20-slide beautification qualification

The committed manifest is synthetic and public; generated decks are ignored by Git.

```bash
npm run benchmark:beautify:fixtures

# Start the API/worker in another shell, then run the paid GPT-5.5 qualification.
npm run benchmark:beautify -- --base-url http://127.0.0.1:8765
```

The runner enforces at least 18 accepted slides, Reviewer score ≥8, average improvement
≥1 point, and 100% hard-gate success. CI always generates and OOXML-validates all 20 decks;
the paid visual qualification remains an explicit run because it requires API quota.
The manual `powerpoint-compatibility` workflow has a `run_paid_beautify` option. On a
licensed self-hosted macOS runner it stores the 20 best outputs and requires a native
PowerPoint open/save/reopen validation for every published candidate.

### Run the 30-case endpoint quality suite

`benchmarks/api_examples/manifest.json` defines five examples for each of the six API
endpoints. The runner submits the real multipart requests through FastAPI, lets the real
worker execute them, downloads the artifacts, renders every PPTX, and writes endpoint-
specific quality checks rather than treating HTTP success as visual success.

```bash
# Prepare public SVGs and deterministic note fixtures.
python scripts/prepare_api_examples.py

# API-free endpoints: figure conversion, rendering, and validation.
python scripts/run_api_examples.py \
  --endpoints figure_to_editable,render,validate --offline

# GPT-backed endpoints; uses OPENAI_API_KEY already present in .env.
python scripts/run_api_examples.py \
  --endpoints image_to_editable,notes,beautify \
  --model gpt-5.5 --max-attempts 1 --paid --offline --resume
```

Results are written to `out/api-endpoint-benchmark/summary.json`, `summary.csv`, and
`montages/`. Quota/rate-limit failures are reported as `blocked_external`, never as a
quality failure. `--resume` keeps successful cases and retries only incomplete ones.
See [BENCHMARK.md](BENCHMARK.md#six-endpoint-api-quality-suite) for the measured snapshot.

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
renders a new PPTX, and runs semantic, layout, and OOXML validation.

The structured OpenAI engine classifies and executes these note families:

- add or remove repeated rows;
- replace text, values, placeholders, comments, chart data, or table data;
- delete, move, resize, recolor, restyle, add, or duplicate objects;
- add or remove sections;
- local or global layout reflow.

Content-density changes trigger adaptive layout. The service can reclaim gaps, resize
sections, and move dependent cards, labels, bars, values, rules, comments, and backgrounds
together. Local checks reject text below the configured minimum, likely clipped text, and
global reflows that do not touch neighboring existing objects.

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
  --review-iterations 2 \
  --layout-mode global \
  --minimum-body-font-size 7.5 \
  --powerpoint-validation auto
```

Do not use `--engine openai` for confidential material unless sending it to the configured
provider is explicitly permitted. The report records typed actions, layout strategy,
detected notes, touched stable IDs, font and text-fit checks, dependent-object reflow,
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
