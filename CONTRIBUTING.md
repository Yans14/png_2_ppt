# Contributing

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
npm ci
npm run test:all
```

Node.js renders the native PowerPoint objects. LibreOffice and Poppler are also required
for end-to-end visual evaluation.

## Renderer changes

The Python package ships a compiled copy of the JavaScript renderer. After changing
`src/render-image-spec.js`, rebuild it and run both test suites:

```bash
npm run build:renderer
npm run test:all
```

Commit both the source and the rebuilt `editable_pptx/js/index.js` bundle.

## Benchmark changes

The benchmark manifest contains links and page numbers, not copyrighted deck binaries.
Run `npm run benchmark:fetch` to populate the ignored local cache, then use
`npm run benchmark` with a configured `OPENAI_API_KEY`. Do not commit cache, result,
secret, or generated presentation files.
