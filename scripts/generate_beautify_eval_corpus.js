#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const PptxGenJS = require('pptxgenjs');
const JSZip = require('jszip');
const pptxApi = new PptxGenJS();
const ShapeType = pptxApi.ShapeType;
const ChartType = pptxApi.ChartType;

const ROOT = path.resolve(__dirname, '..');
const manifestPath = process.argv[2] || path.join(ROOT, 'benchmarks', 'beautify_v1', 'cases.json');
const outputDir = process.argv[3] || path.join(ROOT, 'benchmarks', 'cache', 'beautify-v1');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
fs.mkdirSync(outputDir, { recursive: true });

const colors = {
  navy: '002D72', blue: '167B9F', orange: 'E65C00', green: '159447',
  gray: '69777D', light: 'E4E8EA', ink: '181A1B', white: 'FFFFFF', red: 'D71920'
};

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  const output = Buffer.alloc(4);
  output.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
  return output;
}

function pngChunk(type, data) {
  const name = Buffer.from(type, 'ascii');
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  return Buffer.concat([length, name, data, crc32(Buffer.concat([name, data]))]);
}

function syntheticPhotoData(width = 640, height = 360) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const row = y * (width * 3 + 1);
    raw[row] = 0;
    for (let x = 0; x < width; x += 1) {
      const offset = row + 1 + x * 3;
      const horizon = y < height * 0.48;
      raw[offset] = horizon ? 55 + Math.floor((x / width) * 70) : 35 + ((x * 7 + y * 3) % 35);
      raw[offset + 1] = horizon ? 120 + Math.floor((y / height) * 55) : 95 + ((x + y) % 65);
      raw[offset + 2] = horizon ? 165 + Math.floor((x / width) * 45) : 75 + ((x * 3 + y) % 50);
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  const png = Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    pngChunk('IEND', Buffer.alloc(0))
  ]);
  return `data:image/png;base64,${png.toString('base64')}`;
}

function baseSlide(pptx, item) {
  const slide = pptx.addSlide();
  slide.background = { color: item.variant % 2 ? 'FFFFFF' : 'F4F4F4' };
  slide.addText(item.title, {
    x: 0.45, y: 0.25, w: 8.8, h: 0.48,
    fontFace: 'Arial', fontSize: item.category === 'title' ? 25 : 22,
    bold: true, color: colors.ink, margin: 0
  });
  slide.addText(`Sources: synthetic public fixture · ${item.id}`, {
    x: 0.47, y: 7.12, w: 4.2, h: 0.18, fontFace: 'Arial', fontSize: 7,
    color: colors.gray, margin: 0
  });
  slide.addText('1', { x: 12.55, y: 7.08, w: 0.25, h: 0.2, fontSize: 8, color: colors.gray, margin: 0 });
  return slide;
}

function addContent(slide, item) {
  const bullets = [
    'Demand remains supported by long-term structural trends',
    'Execution discipline protects downside resilience',
    'Selective investment accelerates sustainable growth'
  ];
  bullets.forEach((text, index) => {
    const y = 1.25 + index * 1.65;
    slide.addShape(ShapeType.rect, { x: 0.6 + index * 0.1, y, w: 3.0, h: 1.05, fill: { color: index === 1 ? colors.orange : colors.blue }, line: { color: colors.white } });
    slide.addText(`${index + 1}`, { x: 0.75, y: y + 0.18, w: 0.45, h: 0.4, fontSize: 22, bold: true, color: colors.white, margin: 0 });
    slide.addText(text, { x: 1.35, y: y + 0.12, w: 5.3, h: 0.65, fontSize: 14 - (item.variant % 2), color: colors.ink, margin: 0.02, breakLine: false });
  });
}

function addDense(slide) {
  const columns = ['Commercial', 'Operational', 'Financial'];
  columns.forEach((heading, column) => {
    const x = 0.5 + column * 4.15;
    slide.addShape(ShapeType.rect, { x, y: 1.0, w: 3.75, h: 0.55, fill: { color: column === 1 ? colors.orange : colors.navy }, line: { color: colors.white } });
    slide.addText(heading, { x: x + 0.15, y: 1.14, w: 3.3, h: 0.25, fontSize: 14, bold: true, color: colors.white, margin: 0 });
    for (let row = 0; row < 5; row += 1) {
      slide.addText(`Finding ${row + 1}: quantified evidence supports the key conclusion`, { x: x + 0.12, y: 1.8 + row * 0.9, w: 3.3, h: 0.55, fontSize: 10, color: colors.ink, margin: 0.02, bullet: { indent: 10 } });
    }
  });
}

function addTable(slide, item) {
  const rows = [
    [{ text: 'Metric', options: { bold: true } }, { text: 'Base', options: { bold: true } }, { text: 'Upside', options: { bold: true } }, { text: 'Downside', options: { bold: true } }],
    ['Revenue 2026', 'US$500m', 'US$560m', 'US$440m'],
    ['EBITDA margin', '18.0%', '21.5%', '14.5%'],
    ['Growth', '8.0%', '12.0%', '3.0%'],
    ['Enterprise value', 'US$1.2bn', 'US$1.4bn', 'US$0.9bn']
  ];
  slide.addTable(rows, {
    x: 0.65, y: 1.2, w: 11.9, h: 4.7, border: { color: colors.gray, pt: 0.5 },
    fill: item.variant % 2 ? colors.light : 'FFFFFF', color: colors.ink,
    fontFace: 'Arial', fontSize: 11, margin: 0.08, rowH: 0.65,
    bold: false, valign: 'mid'
  });
}

function addChart(slide, item) {
  const type = item.variant === 2 ? ChartType.line : ChartType.bar;
  slide.addChart(type, [
    { name: 'Core', labels: ['2023', '2024', '2025', '2026'], values: [220, 245, 280, 315] },
    { name: 'Growth', labels: ['2023', '2024', '2025', '2026'], values: [80, 105, 135, 185] }
  ], {
    x: 0.75, y: 1.05, w: 8.2, h: 5.35, catAxisLabelFontSize: 9,
    valAxisLabelFontSize: 9, showLegend: true, legendPos: 'b', showTitle: false,
    chartColors: [colors.blue, colors.orange], showValue: false,
    valGridLine: { color: colors.light, pt: 0.7 }, border: { color: colors.gray, pt: 0.5 }
  });
  slide.addText('Key message', { x: 9.35, y: 1.25, w: 2.8, h: 0.3, fontSize: 13, bold: true, color: colors.navy, margin: 0 });
  slide.addText('Growth accelerates to 12.0% while the core business remains resilient.', { x: 9.35, y: 1.75, w: 2.7, h: 1.4, fontSize: 14, color: colors.ink, margin: 0 });
}

function addValuation(slide) {
  const labels = ['DCF', 'Trading comps', 'Transactions', 'Target range'];
  labels.forEach((label, index) => {
    const y = 1.15 + index * 1.25;
    slide.addText(label, { x: 0.7, y: y + 0.08, w: 2.1, h: 0.3, fontSize: 12, bold: index === 3, color: colors.ink, margin: 0 });
    slide.addShape(ShapeType.rect, { x: 3.2 + index * 0.35, y, w: 3.2 + index * 0.25, h: 0.55, fill: { color: index === 3 ? colors.orange : colors.blue, transparency: index * 8 }, line: { color: colors.white } });
    slide.addText(`US$${900 + index * 150}m – US$${1200 + index * 180}m`, { x: 7.4, y: y + 0.08, w: 2.9, h: 0.3, fontSize: 11, color: colors.navy, margin: 0 });
  });
}

function addTimeline(slide) {
  slide.addShape(ShapeType.line, { x: 1.0, y: 3.4, w: 10.8, h: 0, line: { color: colors.navy, pt: 2 } });
  ['Signing', 'Planning', 'Execution', 'Review', 'Completion'].forEach((label, index) => {
    const x = 1.0 + index * 2.7;
    slide.addShape(ShapeType.ellipse, { x, y: 3.12, w: 0.55, h: 0.55, fill: { color: index % 2 ? colors.orange : colors.blue }, line: { color: colors.white, pt: 1 } });
    slide.addText(label, { x: x - 0.35, y: index % 2 ? 3.9 : 2.35, w: 1.3, h: 0.45, fontSize: 11, bold: true, align: 'center', color: colors.ink, margin: 0 });
    slide.addText(`Q${index + 1} 2026`, { x: x - 0.35, y: index % 2 ? 4.35 : 2.75, w: 1.3, h: 0.3, fontSize: 9, align: 'center', color: colors.gray, margin: 0 });
  });
}

function addVisual(slide) {
  const logo = `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="90"><rect width="320" height="90" rx="10" fill="#002D72"/><text x="160" y="58" text-anchor="middle" font-family="Arial" font-size="36" font-weight="700" fill="#FFFFFF">SYNTHETIC</text></svg>`;
  const icon = `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><circle cx="48" cy="48" r="44" fill="#E65C00"/><path d="M25 52 L42 68 L72 30" fill="none" stroke="#FFFFFF" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  slide.addImage({ data: syntheticPhotoData(), x: 0.75, y: 1.15, w: 5.6, h: 3.75, objectName: 'Synthetic Photo' });
  slide.addImage({ data: `data:image/svg+xml;base64,${Buffer.from(logo).toString('base64')}`, x: 0.75, y: 5.2, w: 2.0, h: 0.55, objectName: 'Synthetic Logo', altText: 'Synthetic benchmark logo' });
  slide.addImage({ data: `data:image/svg+xml;base64,${Buffer.from(icon).toString('base64')}`, x: 5.35, y: 5.05, w: 0.75, h: 0.75, objectName: 'Synthetic Icon', altText: 'Synthetic native-source SVG icon' });
  slide.addText('Participants', { x: 7.0, y: 1.3, w: 3.0, h: 0.35, fontSize: 15, bold: true, color: colors.navy, margin: 0 });
  ['Customers', 'Partners', 'Suppliers', 'Investors'].forEach((label, index) => {
    slide.addShape(ShapeType.roundRect, { x: 7.0, y: 1.95 + index * 0.9, w: 3.4, h: 0.55, rectRadius: 0.05, fill: { color: index % 2 ? colors.light : 'FFFFFF' }, line: { color: colors.blue, pt: 1 } });
    slide.addText(label, { x: 7.2, y: 2.08 + index * 0.9, w: 2.8, h: 0.25, fontSize: 12, color: colors.ink, margin: 0 });
  });
}

async function normalizePackage(file, injectDiagram) {
  const zip = await JSZip.loadAsync(fs.readFileSync(file));
  for (const name of Object.keys(zip.files).filter((item) => /^ppt\/slides\/slide\d+\.xml$/.test(item))) {
    const xml = await zip.file(name).async('string');
    const identifiers = [...xml.matchAll(/<p:cNvPr\b[^>]*\bid="(\d+)"[^>]*>/g)].map((match) => Number(match[1]));
    let nextId = Math.max(0, ...identifiers) + 1;
    const seen = new Set();
    const normalized = xml.replace(/<p:cNvPr\b[^>]*\bid="(\d+)"[^>]*>/g, (tag, rawId) => {
      const id = Number(rawId);
      if (!seen.has(id)) {
        seen.add(id);
        return tag;
      }
      const replacement = nextId;
      nextId += 1;
      seen.add(replacement);
      return tag.replace(`id="${rawId}"`, `id="${replacement}"`);
    });
    zip.file(name, normalized);
  }
  if (injectDiagram) {
    zip.file('ppt/diagrams/data1.xml', '<?xml version="1.0" encoding="UTF-8"?><dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"/>');
  }
  fs.writeFileSync(file, await zip.generateAsync({ type: 'nodebuffer' }));
}

async function generate(item) {
  const pptx = new PptxGenJS();
  pptx.layout = 'LAYOUT_WIDE';
  pptx.author = 'Editable PPTX synthetic benchmark';
  pptx.subject = item.category;
  pptx.title = item.title;
  pptx.company = 'Synthetic public fixture';
  pptx.lang = 'en-GB';
  const slide = baseSlide(pptx, item);
  if (item.category === 'title') {
    slide.addShape(ShapeType.rect, { x: 0.5, y: 1.5, w: 11.7, h: 4.6, fill: { color: item.variant === 1 ? colors.navy : colors.blue }, line: { color: colors.white } });
    slide.addText('A concise synthetic subtitle for editable-slide evaluation', { x: 1.0, y: 3.1, w: 8.5, h: 0.8, fontSize: 24, color: colors.white, margin: 0 });
  } else if (item.category === 'content') addContent(slide, item);
  else if (item.category === 'dense') addDense(slide);
  else if (item.category === 'table') addTable(slide, item);
  else if (item.category === 'chart') addChart(slide, item);
  else if (item.category === 'valuation') addValuation(slide);
  else if (item.category === 'timeline') addTimeline(slide);
  else if (item.category === 'visual') addVisual(slide);
  else {
    addContent(slide, item);
    slide.addText('Protected complex object', { x: 8.6, y: 5.8, w: 3.0, h: 0.35, fontSize: 10, color: colors.red, margin: 0 });
  }
  const output = path.join(outputDir, `${item.id}.pptx`);
  await pptx.writeFile({ fileName: output });
  await normalizePackage(output, Boolean(item.inject_diagram_part));
  return output;
}

(async () => {
  const outputs = [];
  for (const item of manifest.cases) outputs.push(await generate(item));
  process.stdout.write(`${JSON.stringify({ count: outputs.length, outputDir, outputs }, null, 2)}\n`);
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
