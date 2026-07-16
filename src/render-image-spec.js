#!/usr/bin/env node

const fs = require('node:fs');
const path = require('node:path');
const JSZip = require('jszip');
const PptxGenJS = require('pptxgenjs');

const PX_PER_INCH = 96;

function px(value) {
  return Number(value) / PX_PER_INCH;
}

function color(value, fallback = '000000') {
  return typeof value === 'string' ? value.replace(/^#/, '').toUpperCase() : fallback;
}

function transparency(opacity) {
  return Math.max(0, Math.min(100, Math.round((1 - Number(opacity)) * 100)));
}

function xmlEscape(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function fillOptions(fill, opacityMultiplier = 1) {
  if (!fill || fill.kind === 'none') return { color: 'FFFFFF', transparency: 100 };
  if (fill.kind === 'linear_gradient') {
    const first = fill.stops[0] || { color: '#000000', opacity: 1 };
    return {
      color: color(first.color),
      transparency: transparency(first.opacity * fill.opacity * opacityMultiplier),
    };
  }
  return {
    color: color(fill.color),
    transparency: transparency(fill.opacity * opacityMultiplier),
  };
}

function lineOptions(stroke, opacityMultiplier = 1) {
  if (!stroke || stroke.width_px <= 0 || stroke.opacity <= 0) {
    return { color: 'FFFFFF', transparency: 100, width: 0 };
  }
  const dashMap = { solid: 'solid', dash: 'dash', dot: 'sysDot', dash_dot: 'dashDot' };
  return {
    color: color(stroke.color),
    transparency: transparency(stroke.opacity * opacityMultiplier),
    width: Math.max(0.05, Number(stroke.width_px) * 0.75),
    dashType: dashMap[stroke.dash] || 'solid',
  };
}

function shapeBounds(bounds) {
  return { x: px(bounds.x), y: px(bounds.y), w: px(bounds.width), h: px(bounds.height) };
}

function lineShapeOptions(element) {
  const dx = Number(element.x2) - Number(element.x1);
  const dy = Number(element.y2) - Number(element.y1);
  return {
    x: px(Math.min(Number(element.x1), Number(element.x2))),
    y: px(Math.min(Number(element.y1), Number(element.y2))),
    w: px(Math.abs(dx)),
    h: px(Math.abs(dy)),
    flipH: dx < 0,
    flipV: dy < 0,
  };
}

function objectName(element) {
  const id = String(element.id || element.name || 'object').replace(/[\r\n]/g, ' ');
  return `editable:${id}`;
}

function copyWithOpacity(element, multiplier) {
  const result = structuredClone(element);
  result._opacityMultiplier = Number(multiplier ?? 1);
  return result;
}

function transformComponentPrimitive(primitive, instance, index) {
  const item = copyWithOpacity(primitive, instance.opacity);
  item.id = `${instance.id}/${primitive.id}/${index}`;
  item.name = `${instance.name}: ${primitive.name}`;
  item.layer = Number(instance.layer) + Number(primitive.layer) / 1000;
  item.group_id = instance.id;
  item.rotation_deg = Number(instance.rotation_deg || 0) + Number(primitive.rotation_deg || 0);
  const box = instance.bounds;

  if ('bounds' in primitive) {
    item.bounds = {
      x: box.x + primitive.bounds.x * box.width,
      y: box.y + primitive.bounds.y * box.height,
      width: primitive.bounds.width * box.width,
      height: primitive.bounds.height * box.height,
    };
    if (instance.rotation_deg) {
      const angle = Number(instance.rotation_deg) * Math.PI / 180;
      const instanceCx = box.x + box.width / 2;
      const instanceCy = box.y + box.height / 2;
      const itemCx = item.bounds.x + item.bounds.width / 2;
      const itemCy = item.bounds.y + item.bounds.height / 2;
      const dx = itemCx - instanceCx;
      const dy = itemCy - instanceCy;
      const rotatedCx = instanceCx + dx * Math.cos(angle) - dy * Math.sin(angle);
      const rotatedCy = instanceCy + dx * Math.sin(angle) + dy * Math.cos(angle);
      item.bounds.x = rotatedCx - item.bounds.width / 2;
      item.bounds.y = rotatedCy - item.bounds.height / 2;
    }
  }
  if (primitive.kind === 'line') {
    item.x1 = box.x + primitive.x1 * box.width;
    item.y1 = box.y + primitive.y1 * box.height;
    item.x2 = box.x + primitive.x2 * box.width;
    item.y2 = box.y + primitive.y2 * box.height;
    if (instance.rotation_deg) {
      const angle = Number(instance.rotation_deg) * Math.PI / 180;
      const cx = box.x + box.width / 2;
      const cy = box.y + box.height / 2;
      for (const suffix of ['1', '2']) {
        const dx = item[`x${suffix}`] - cx;
        const dy = item[`y${suffix}`] - cy;
        item[`x${suffix}`] = cx + dx * Math.cos(angle) - dy * Math.sin(angle);
        item[`y${suffix}`] = cy + dx * Math.sin(angle) + dy * Math.cos(angle);
      }
    }
  }
  if (primitive.kind === 'text') {
    // Component fonts scale with the instance's height, using 96 px as the unit box.
    item.font_size_pt = primitive.font_size_pt * (box.height / 96);
    item.margin_px = primitive.margin_px * (box.height / 96);
  }
  return item;
}

function flattenElements(spec) {
  const components = new Map(spec.components.map((component) => [component.id, component]));
  const result = [];
  for (const element of spec.elements) {
    if (element.kind !== 'component') {
      result.push(copyWithOpacity(element, 1));
      continue;
    }
    const component = components.get(element.component_id);
    if (!component) throw new Error(`Unknown component ${element.component_id}`);
    component.elements.forEach((primitive, index) => {
      result.push(transformComponentPrimitive(primitive, element, index));
    });
  }
  return result.sort((left, right) => Number(left.layer) - Number(right.layer));
}

function pathPoints(element) {
  const w = px(element.bounds.width);
  const h = px(element.bounds.height);
  return element.commands.map((command, index) => {
    if (command.op === 'Z') return { close: true };
    const point = { x: Number(command.x) * w, y: Number(command.y) * h };
    if (command.op === 'M' || index === 0) point.moveTo = true;
    if (command.op === 'C') {
      point.curve = {
        type: 'cubic',
        x1: Number(command.x1) * w,
        y1: Number(command.y1) * h,
        x2: Number(command.x2) * w,
        y2: Number(command.y2) * h,
      };
    }
    if (command.op === 'Q') {
      point.curve = {
        type: 'quadratic',
        x1: Number(command.x1) * w,
        y1: Number(command.y1) * h,
      };
    }
    return point;
  });
}

function addElement(slide, pptx, element, assets, gradientJobs) {
  const opacityMultiplier = Number(element._opacityMultiplier ?? 1);
  const name = objectName(element);

  if (element.kind === 'text') {
    const box = shapeBounds(element.bounds);
    slide.addText(element.text, {
      ...box,
      objectName: name,
      fontFace: element.font_family,
      fontSize: Number(element.font_size_pt),
      bold: Boolean(element.bold),
      italic: Boolean(element.italic),
      color: color(element.color),
      transparency: transparency(element.opacity * opacityMultiplier),
      align: element.alignment,
      valign: element.vertical_alignment,
      lineSpacingMultiple: Number(element.line_spacing),
      margin: Number(element.margin_px) * 0.75,
      rotate: Number(element.rotation_deg || 0),
      breakLine: false,
      fit: 'shrink',
      isTextBox: true,
    });
    return;
  }

  if (element.kind === 'shape') {
    const box = shapeBounds(element.bounds);
    slide.addShape(pptx.ShapeType[element.preset] || element.preset, {
      ...box,
      objectName: name,
      rotate: Number(element.rotation_deg || 0),
      rectRadius: element.corner_radius == null ? undefined : Number(element.corner_radius),
      fill: fillOptions(element.fill, opacityMultiplier),
      line: lineOptions(element.stroke, opacityMultiplier),
    });
    if (element.fill.kind === 'linear_gradient') {
      gradientJobs.set(name, { ...element.fill, opacity: element.fill.opacity * opacityMultiplier });
    }
    return;
  }

  if (element.kind === 'line') {
    const arrows = { none: 'none', triangle: 'triangle', stealth: 'stealth', diamond: 'diamond', oval: 'oval' };
    slide.addShape(pptx.ShapeType.line, {
      ...lineShapeOptions(element),
      objectName: name,
      line: {
        ...lineOptions(element.stroke, opacityMultiplier),
        beginArrowType: arrows[element.arrow_start] || 'none',
        endArrowType: arrows[element.arrow_end] || 'none',
      },
    });
    return;
  }

  if (element.kind === 'path') {
    const box = shapeBounds(element.bounds);
    slide.addShape(pptx.ShapeType.custGeom, {
      ...box,
      objectName: name,
      rotate: Number(element.rotation_deg || 0),
      points: pathPoints(element),
      fill: fillOptions(element.fill, opacityMultiplier),
      line: lineOptions(element.stroke, opacityMultiplier),
    });
    if (element.fill.kind === 'linear_gradient') {
      gradientJobs.set(name, { ...element.fill, opacity: element.fill.opacity * opacityMultiplier });
    }
    return;
  }

  if (element.kind === 'image') {
    const asset = assets[element.id];
    if (!asset) throw new Error(`Missing cropped asset for image element ${element.id}`);
    let box = shapeBounds(element.bounds);
    if (element.preserve_aspect) {
      const sourceRatio = element.source_region.width / element.source_region.height;
      const targetRatio = element.bounds.width / element.bounds.height;
      if (sourceRatio > targetRatio) {
        const newHeight = box.w / sourceRatio;
        box = { ...box, y: box.y + (box.h - newHeight) / 2, h: newHeight };
      } else {
        const newWidth = box.h * sourceRatio;
        box = { ...box, x: box.x + (box.w - newWidth) / 2, w: newWidth };
      }
    }
    slide.addImage({
      path: asset,
      ...box,
      objectName: name,
      altText: element.alt_text,
      transparency: transparency(element.opacity),
      rotate: Number(element.rotation_deg || 0),
    });
    return;
  }

  throw new Error(`Unsupported element kind ${element.kind}`);
}

function gradientXml(fill) {
  const stops = [...fill.stops].sort((a, b) => a.position - b.position).map((stop) => {
    const alpha = Math.round(Number(stop.opacity) * Number(fill.opacity) * 100000);
    const alphaXml = alpha < 100000 ? `<a:alpha val="${alpha}"/>` : '';
    return `<a:gs pos="${Math.round(Number(stop.position) * 100000)}"><a:srgbClr val="${color(stop.color)}">${alphaXml}</a:srgbClr></a:gs>`;
  }).join('');
  const angle = Math.round((((Number(fill.angle_deg) % 360) + 360) % 360) * 60000);
  return `<a:gradFill rotWithShape="1"><a:gsLst>${stops}</a:gsLst><a:lin ang="${angle}" scaled="1"/></a:gradFill>`;
}

async function injectGradients(pptxPath, gradientJobs) {
  if (!gradientJobs.size) return;
  const zip = await JSZip.loadAsync(fs.readFileSync(pptxPath));
  const slideFiles = Object.keys(zip.files).filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name));
  for (const slideFile of slideFiles) {
    let xml = await zip.file(slideFile).async('string');
    xml = xml.replace(/<p:sp>[\s\S]*?<\/p:sp>/g, (shapeXml) => {
      for (const [name, fill] of gradientJobs) {
        if (!shapeXml.includes(`name="${xmlEscape(name)}"`)) continue;
        const replacement = gradientXml(fill);
        if (/<a:(?:solidFill|noFill)(?:\s*\/|>)[\s\S]*?<\/a:solidFill>/.test(shapeXml)) {
          return shapeXml.replace(/<a:solidFill>[\s\S]*?<\/a:solidFill>|<a:noFill\s*\/>/, replacement);
        }
        const geometryEnd = shapeXml.includes('</a:custGeom>') ? '</a:custGeom>' : '</a:prstGeom>';
        return shapeXml.replace(geometryEnd, `${geometryEnd}${replacement}`);
      }
      return shapeXml;
    });
    zip.file(slideFile, xml);
  }
  const buffer = await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
  fs.writeFileSync(pptxPath, buffer);
}

async function render(specPath, outputPath) {
  const envelope = JSON.parse(fs.readFileSync(specPath, 'utf8'));
  const spec = envelope.spec || envelope;
  const assets = envelope.assets || {};
  const pptx = new PptxGenJS();
  const layoutName = 'EDITABLE_IMAGE_LAYOUT';
  pptx.defineLayout({ name: layoutName, width: px(spec.source_width), height: px(spec.source_height) });
  pptx.layout = layoutName;
  pptx.author = 'Editable PPTX Reconstruction';
  pptx.subject = 'Editable reconstruction from a reference image';
  pptx.title = 'Editable reconstructed slide';
  pptx.company = 'OpenAI Codex';
  pptx.lang = 'fr-FR';
  pptx.theme = {
    headFontFace: 'Arial',
    bodyFontFace: 'Arial',
    lang: 'fr-FR',
  };

  const slide = pptx.addSlide();
  const gradientJobs = new Map();
  if (spec.background.kind === 'solid') {
    slide.background = { color: color(spec.background.color, 'FFFFFF'), transparency: transparency(spec.background.opacity) };
  } else if (spec.background.kind === 'linear_gradient') {
    const backgroundShape = {
      kind: 'shape', id: '__background_gradient__', name: 'Background gradient', layer: -100000,
      bounds: { x: 0, y: 0, width: spec.source_width, height: spec.source_height },
      rotation_deg: 0, preset: 'rect', fill: spec.background,
      stroke: { color: '#FFFFFF', opacity: 0, width_px: 0, dash: 'solid' }, corner_radius: null,
      _opacityMultiplier: 1,
    };
    addElement(slide, pptx, backgroundShape, assets, gradientJobs);
  }

  for (const element of flattenElements(spec)) addElement(slide, pptx, element, assets, gradientJobs);

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  await pptx.writeFile({ fileName: outputPath, compression: true });
  await injectGradients(outputPath, gradientJobs);
}

if (require.main === module) {
  const [, , specPath, outputPath] = process.argv;
  if (!specPath || !outputPath) {
    console.error('Usage: node src/render-image-spec.js SPEC.json OUTPUT.pptx');
    process.exit(2);
  }
  render(path.resolve(specPath), path.resolve(outputPath)).catch((error) => {
    console.error(error.stack || String(error));
    process.exit(1);
  });
}

module.exports = { render, flattenElements, gradientXml, lineShapeOptions, pathPoints };
