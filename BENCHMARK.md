# Benchmark report

Evaluation date: 2026-07-16. Final public run: `gpt-5.6-luna`. Engine: `1.1.0`.
Metric version: `2`. The score is intentionally structure-weighted (70% structure,
30% exact color/pixels); it is not a claim of pixel perfection. GPT‑5.6 Luna is the
official cost-sensitive GPT‑5.6 tier and supports image input plus structured outputs.

## Reproducible public set

`benchmarks/manifest.json` defines six slides from four public presentations:

- World Bank, [Digital Transformation of the Agrifood System](https://thedocs.worldbank.org/en/doc/e5058a942964a3a9e65d55d845e694e0-0360012021/original/17June2021-Digital-Transformation-Japan-Public-Event-GE.pdf), pages 3 and 7;
- OECD, [Digital Government Outlook 2026 – key findings](https://www.oecd.org/content/dam/oecd/en/publications/support-materials/2026/06/digital-government-outlook_4585678e/Digital-government-outlook-key-findings.pdf), page 9;
- World Bank, [IDA in Focus: Financing Model](https://thedocs.worldbank.org/en/doc/9586d703f1fef4a864b6f5add9f75bf9-0410012024/original/IDA-in-Focus-Financing-Model-2-7-2024.pdf), pages 3 and 6;
- World Bank, [Beyond Unicorns](https://thedocs.worldbank.org/en/doc/314f6ce40311342e43840f037fb15d10-0070012021/original/World-Bank-Indonesia-Digital-Report-Presentation.pdf), page 17.

All six final PPTX files were inspected at full size. Each also passed the independent
PowerPoint canvas-overflow test. The initial pass used zero optional correction iterations;
one targeted correction was retained for the agrifood value chain, and two for the IDA
hybrid model. Corrections always started from the best accepted spec.

| Case | Similarity | Structure | Native shapes | Text runs | Pictures | Flattened | Overflow |
|---|---:|---:|---:|---:|---:|---|---:|
| Agrifood pyramid | 0.9535 | 0.9548 | 18 | 6 | 1 photo | No | 0 |
| Agrifood value chain | 0.8299 | 0.8340 | 79 | 21 | 1 photo | No | 0 |
| OECD DPI dashboard | 0.9108 | 0.9240 | 70 | 34 | 0 | No | 0 |
| IDA hybrid model | 0.9264 | 0.9284 | 58 | 42 | 0 | No | 0 |
| IDA balance sheet | 0.8719 | 0.8525 | 31 | 29 | 0 | No | 0 |
| Indonesia ride-hailing | 0.8794 | 0.8831 | 28 | 22 | 1 photo | No | 0 |

Mean final similarity: **0.8953**. Every non-photographic visual is a native PowerPoint
shape, line, text box, component, or custom vector path. The three picture objects are
only the genuine photographic regions in the source slides.

## Budget versus GPT‑5.5 reference

Three cases also have a prior GPT‑5.5 result under the same metric. Their mean was 0.9002;
the Luna final mean on those same three cases is 0.8981, a difference of 0.0021. Standard
token rates list Luna at $1 input / $6 output per million tokens versus $5 / $30 for
GPT‑5.5, so the token price is five times lower. Actual cost per successful slide still
depends on image size, response length, and correction count. See OpenAI's official
[pricing table](https://developers.openai.com/api/docs/pricing).

| Case | GPT‑5.5 | GPT‑5.6 Luna final |
|---|---:|---:|
| Agrifood pyramid | 0.9262 | 0.9535 |
| Agrifood value chain | 0.8557 | 0.8299 |
| OECD DPI dashboard | 0.9188 | 0.9108 |

## Additional real-world case studies

Two supplied J.P. Morgan references were also rendered and scored with the same current
metric. They are not part of the downloadable public set, so their source images and
generated files are intentionally absent from Git.

| Case | Similarity | Structure | Native shapes | Text runs | Pictures | Flattened | Overflow |
|---|---:|---:|---:|---:|---:|---|---:|
| Sport / entertainment economy | 0.9359 | 0.9550 | 179 | 33 | 0 | No | 0 |
| Digitalisation / key challenges | 0.7636 | 0.7560 | 121 | 23 | 0 | No | 0 |

The digitalisation slide improved from 0.7609 to 0.7636 after one GPT‑5.5 patch. The patch
corrected the right-column line wrapping without replacing the complete object graph.

## Improvements made from the evaluation

- Native line extents are always non-negative; reversed lines use PowerPoint flips.
- Responses run in background mode with polling and a shared deadline.
- Invalid structured responses are regenerated once with the local schema errors attached.
- Empty top-level slide graphs are invalid and cannot be cached as successful results.
- Refinement returns small stable-ID patches instead of regenerating the entire slide.
- The correction loop accepts only measured improvements and always refines from the best
  accepted candidate.
- Refinement explicitly compares shape topology and silhouettes, preventing metrics from
  treating circles and rounded rectangles as equivalent.
- Edge matching uses a noise-tolerant structural metric, so camera moiré does not dominate.
- Raster photo crops remove source text/logo pixels that are recreated as native overlays,
  eliminating duplicated screenshot text.
- `insufficient_quota` is not retried, and the benchmark runner skips queued paid cases
  after the first quota failure.
- Cached reports are reused only when engine, metric, model, target score, raster policy,
  and iteration count match the requested run.
- Offline rescoring never makes an API call, validates all six target dimensions first,
  and writes a separate summary instead of overwriting the canonical run.
- The OOXML audit now rejects objects outside the slide canvas, including rotated objects.
- Text auto-fit factors are materialized in OOXML, making text layout stable during external
  PowerPoint/LibreOffice inspection and page resizing.

## Re-run

```bash
npm run benchmark:fetch
npm run benchmark -- --model gpt-5.6-luna --iterations 1 --jobs 2
npm run benchmark -- --rescore-existing --jobs 2
```

The default result directory is ignored by Git. Use `--force` only when successful cached
cases should also be regenerated.
