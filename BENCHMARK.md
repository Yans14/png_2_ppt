# Benchmark report

Evaluation date: 2026-07-16. Model: `gpt-5.5`. The score is intentionally
structure-weighted (70% structure, 30% exact color/pixels); it is not a claim of pixel
perfection.

## Reproducible public set

`benchmarks/manifest.json` defines six slides from four public presentations:

- World Bank, [Digital Transformation of the Agrifood System](https://thedocs.worldbank.org/en/doc/e5058a942964a3a9e65d55d845e694e0-0360012021/original/17June2021-Digital-Transformation-Japan-Public-Event-GE.pdf), pages 3 and 7;
- OECD, [Digital Government Outlook 2026 – key findings](https://www.oecd.org/content/dam/oecd/en/publications/support-materials/2026/06/digital-government-outlook_4585678e/Digital-government-outlook-key-findings.pdf), page 9;
- World Bank, [IDA in Focus: Financing Model](https://thedocs.worldbank.org/en/doc/9586d703f1fef4a864b6f5add9f75bf9-0410012024/original/IDA-in-Focus-Financing-Model-2-7-2024.pdf), pages 3 and 6;
- World Bank, [Beyond Unicorns](https://thedocs.worldbank.org/en/doc/314f6ce40311342e43840f037fb15d10-0070012021/original/World-Bank-Indonesia-Digital-Report-Presentation.pdf), page 17.

The first run completed three cases before the configured API project returned
`insufficient_quota`. The runner records that condition, stops launching additional paid
work, and resumes incomplete cases without regenerating successful cases after quota is
restored.

| Case | Similarity | Structure | Native shapes | Text runs | Pictures | Flattened |
|---|---:|---:|---:|---:|---:|---|
| Agrifood pyramid | 0.9262 | 0.9171 | 22 | 6 | 1 photo | No |
| Agrifood value chain | 0.8562 | 0.8634 | 119 | 21 | 1 photo | No |
| OECD DPI dashboard | 0.9188 | 0.9311 | 96 | 34 | 0 | No |
| IDA hybrid model | blocked by API quota | — | — | — | — | — |
| IDA balance sheet | blocked by API quota | — | — | — | — | — |
| Indonesia ride-hailing | blocked by API quota | — | — | — | — | — |

## Additional real-world case studies

Two supplied J.P. Morgan references were also rendered and scored with the same current
metric. They are not part of the downloadable public set, so their source images and
generated files are intentionally absent from Git.

| Case | Similarity | Structure | Native shapes | Text runs | Pictures | Flattened |
|---|---:|---:|---:|---:|---:|---|
| Sport / entertainment economy | 0.9359 | 0.9550 | 179 | 33 | 0 | No |
| Digitalisation / key challenges | 0.7636 | 0.7560 | 121 | 23 | 0 | No |

The digitalisation slide improved from 0.7609 to 0.7636 after one GPT‑5.5 patch. The patch
corrected the right-column line wrapping without replacing the complete object graph.

## Improvements made from the evaluation

- Native line extents are always non-negative; reversed lines use PowerPoint flips.
- Responses run in background mode with polling and a shared deadline.
- Refinement returns small stable-ID patches instead of regenerating the entire slide.
- The correction loop accepts only measured improvements and always refines from the best
  accepted candidate.
- Edge matching uses a noise-tolerant structural metric, so camera moiré does not dominate.
- Raster photo crops remove source text/logo pixels that are recreated as native overlays,
  eliminating duplicated screenshot text.
- `insufficient_quota` is not retried, and the benchmark runner skips queued paid cases
  after the first quota failure.

## Re-run

```bash
npm run benchmark:fetch
npm run benchmark -- --model gpt-5.5 --iterations 1 --jobs 2
```

The default result directory is ignored by Git. Use `--force` only when successful cached
cases should also be regenerated.
