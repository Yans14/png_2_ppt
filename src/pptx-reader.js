const fs = require("fs/promises");
const path = require("path");
const JSZip = require("jszip");
const { XMLParser } = require("fast-xml-parser");

const { normalizeLayout } = require("./layouts");

const EMU_PER_INCH = 914400;
const XML_OPTIONS = {
  ignoreAttributes: false,
  attributeNamePrefix: "@_",
  parseTagValue: false,
  trimValues: false,
};

const RELATIONSHIP_TYPES = {
  image: "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
  slide: "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
  slideLayout: "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout",
  slideMaster: "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster",
  theme: "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
};

const MIME_TYPES = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".bmp": "image/bmp",
  ".tif": "image/tiff",
  ".tiff": "image/tiff",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
  ".emf": "image/emf",
  ".wmf": "image/wmf",
};

const PRESET_COLORS = {
  black: "000000",
  white: "FFFFFF",
  red: "FF0000",
  green: "00AA00",
  blue: "0000FF",
  yellow: "FFFF00",
  orange: "FFA500",
  gray: "808080",
  grey: "808080",
  lightGray: "D3D3D3",
  darkGray: "666666",
};

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function round(value, precision = 4) {
  const factor = 10 ** precision;
  return Math.round(value * factor) / factor;
}

function toInches(emu) {
  const numeric = Number(emu);
  if (!Number.isFinite(numeric)) return 0;
  return round(numeric / EMU_PER_INCH);
}

function normalizeHexColor(value) {
  if (!value) return null;
  const hex = String(value).replace(/^#/, "").toUpperCase();
  if (/^[0-9A-F]{6}$/.test(hex)) return hex;
  if (/^[0-9A-F]{3}$/.test(hex)) {
    return hex
      .split("")
      .map((char) => `${char}${char}`)
      .join("");
  }
  return PRESET_COLORS[hex] || PRESET_COLORS[String(value)] || null;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function withDefaultColorMap(colorMap) {
  return {
    bg1: "lt1",
    tx1: "dk1",
    bg2: "lt2",
    tx2: "dk2",
    accent1: "accent1",
    accent2: "accent2",
    accent3: "accent3",
    accent4: "accent4",
    accent5: "accent5",
    accent6: "accent6",
    hlink: "hlink",
    folHlink: "folHlink",
    ...(colorMap || {}),
  };
}

function getAttr(node, key) {
  if (!node || typeof node !== "object") return undefined;
  return node[`@_${key}`];
}

function normalizeTextValue(value) {
  if (value === undefined || value === null) return "";
  return String(value).replace(/\r/g, "");
}

function joinTextParts(parts) {
  return parts
    .map((part) => normalizeTextValue(part))
    .join("")
    .replace(/\u000b/g, "\n");
}

function parsePercent(value, divisor = 100000) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  return numeric / divisor;
}

function channelToLinear(value) {
  const normalized = value / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function linearToChannel(value) {
  const normalized =
    value <= 0.0031308 ? value * 12.92 : 1.055 * value ** (1 / 2.4) - 0.055;
  return clamp(Math.round(normalized * 255), 0, 255);
}

function applyLuminanceTransforms(hex, node) {
  const color = normalizeHexColor(hex);
  if (!color) return null;

  const tint = parsePercent(node?.["a:tint"]?.["@_val"]);
  const shade = parsePercent(node?.["a:shade"]?.["@_val"]);
  const lumMod = parsePercent(node?.["a:lumMod"]?.["@_val"]);
  const lumOff = parsePercent(node?.["a:lumOff"]?.["@_val"]);

  let r = parseInt(color.slice(0, 2), 16);
  let g = parseInt(color.slice(2, 4), 16);
  let b = parseInt(color.slice(4, 6), 16);

  if (tint !== null) {
    r = Math.round(r + (255 - r) * tint);
    g = Math.round(g + (255 - g) * tint);
    b = Math.round(b + (255 - b) * tint);
  }

  if (shade !== null) {
    r = Math.round(r * (1 - shade));
    g = Math.round(g * (1 - shade));
    b = Math.round(b * (1 - shade));
  }

  if (lumMod !== null || lumOff !== null) {
    const rLin = channelToLinear(r);
    const gLin = channelToLinear(g);
    const bLin = channelToLinear(b);
    const mod = lumMod === null ? 1 : lumMod;
    const off = lumOff === null ? 0 : lumOff;
    r = linearToChannel(clamp(rLin * mod + off, 0, 1));
    g = linearToChannel(clamp(gLin * mod + off, 0, 1));
    b = linearToChannel(clamp(bLin * mod + off, 0, 1));
  }

  return [r, g, b]
    .map((channel) => channel.toString(16).padStart(2, "0").toUpperCase())
    .join("");
}

function resolveColorNode(node, colorContext = {}) {
  if (!node || typeof node !== "object") return null;

  if (node["a:srgbClr"]) {
    return applyLuminanceTransforms(node["a:srgbClr"]["@_val"], node["a:srgbClr"]);
  }

  if (node["a:sysClr"]) {
    const sys = node["a:sysClr"];
    return applyLuminanceTransforms(sys?.["@_lastClr"] || sys?.["@_val"], sys);
  }

  if (node["a:schemeClr"]) {
    const scheme = node["a:schemeClr"];
    const mappedName = colorContext.colorMap?.[scheme?.["@_val"]] || scheme?.["@_val"];
    const themeColor = colorContext.themeColors?.[mappedName] || colorContext.themeColors?.[scheme?.["@_val"]];
    return applyLuminanceTransforms(themeColor, scheme);
  }

  if (node["a:prstClr"]) {
    return normalizeHexColor(node["a:prstClr"]["@_val"]);
  }

  return null;
}

function resolveFillColor(fillNode, colorContext = {}) {
  if (!fillNode || typeof fillNode !== "object") return null;
  return resolveColorNode(fillNode, colorContext);
}

function resolveStyleColor(styleNode, styleKey, colorContext = {}) {
  const styleRef = styleNode?.[styleKey];
  if (!styleRef) return null;
  return resolveColorNode(styleRef, colorContext);
}

function resolveShapeFill(spPr, styleNode, colorContext = {}) {
  if (!spPr) {
    return {
      fillColor: resolveStyleColor(styleNode, "a:fillRef", colorContext),
      noFill: false,
      fillImageRelId: null,
    };
  }

  if (spPr["a:noFill"]) {
    return { fillColor: null, noFill: true, fillImageRelId: null };
  }

  if (spPr["a:blipFill"]) {
    return {
      fillColor: null,
      noFill: false,
      fillImageRelId: spPr["a:blipFill"]?.["a:blip"]?.["@_r:embed"] || null,
    };
  }

  if (spPr["a:solidFill"]) {
    return {
      fillColor: resolveFillColor(spPr["a:solidFill"], colorContext),
      noFill: false,
      fillImageRelId: null,
    };
  }

  if (spPr["a:gradFill"]) {
    const stops = asArray(spPr["a:gradFill"]?.["a:gsLst"]?.["a:gs"]);
    const firstStop = stops[0] || null;
    return {
      fillColor: resolveColorNode(firstStop || {}, colorContext),
      noFill: false,
      fillImageRelId: null,
    };
  }

  return {
    fillColor: resolveStyleColor(styleNode, "a:fillRef", colorContext),
    noFill: false,
    fillImageRelId: null,
  };
}

function resolveShapeLine(spPr, styleNode, colorContext = {}) {
  const lineNode = spPr?.["a:ln"];
  if (lineNode?.["a:noFill"]) {
    return { lineColor: null, lineWidthPt: 0, noLine: true };
  }

  const widthEmu = Number(lineNode?.["@_w"]);
  const lineWidthPt = Number.isFinite(widthEmu) ? round(widthEmu / 12700, 2) : 0;
  const lineColor =
    resolveFillColor(lineNode?.["a:solidFill"], colorContext) ||
    resolveStyleColor(styleNode, "a:lnRef", colorContext);

  return {
    lineColor,
    lineWidthPt: lineColor ? Math.max(lineWidthPt || 0.5, 0.25) : 0,
    noLine: !lineColor,
  };
}

function parseThemeColorValue(colorNode) {
  if (!colorNode) return null;
  if (colorNode["a:srgbClr"]) return normalizeHexColor(colorNode["a:srgbClr"]["@_val"]);
  if (colorNode["a:sysClr"]) return normalizeHexColor(colorNode["a:sysClr"]["@_lastClr"]);
  return null;
}

function parseThemeColors(themeDoc) {
  const colorScheme =
    themeDoc?.["a:theme"]?.["a:themeElements"]?.["a:clrScheme"] || {};
  const colors = {};

  for (const [key, value] of Object.entries(colorScheme)) {
    if (!key.startsWith("a:")) continue;
    colors[key.slice(2)] = parseThemeColorValue(value);
  }

  return colors;
}

function parseColorMap(doc) {
  const mapNode =
    doc?.["p:sldMaster"]?.["p:clrMap"] ||
    doc?.["p:sldLayout"]?.["p:clrMapOvr"]?.["a:overrideClrMapping"] ||
    doc?.["p:sld"]?.["p:clrMapOvr"]?.["a:overrideClrMapping"] ||
    doc?.["p:sld"]?.["p:clrMapOvr"]?.["p:overrideClrMapping"] ||
    null;

  if (!mapNode) return {};

  const colorMap = {};
  for (const [key, value] of Object.entries(mapNode)) {
    if (!key.startsWith("@_")) continue;
    colorMap[key.slice(2)] = value;
  }
  return colorMap;
}

function makeSourceElementId(slideIndex, prefix, localIndex) {
  return `s${slideIndex}:${prefix}${localIndex}`;
}

function readTransform(xfrm, groupContext = { offsetX: 0, offsetY: 0, scaleX: 1, scaleY: 1 }) {
  const off = xfrm?.["a:off"] || {};
  const ext = xfrm?.["a:ext"] || {};
  const x = Number(off?.["@_x"]) || 0;
  const y = Number(off?.["@_y"]) || 0;
  const w = Number(ext?.["@_cx"]) || 0;
  const h = Number(ext?.["@_cy"]) || 0;

  return {
    x: round(toInches(groupContext.offsetX + x * groupContext.scaleX)),
    y: round(toInches(groupContext.offsetY + y * groupContext.scaleY)),
    w: round(toInches(w * groupContext.scaleX)),
    h: round(toInches(h * groupContext.scaleY)),
  };
}

function deriveGroupContext(xfrm, parent = { offsetX: 0, offsetY: 0, scaleX: 1, scaleY: 1 }) {
  if (!xfrm) return parent;

  const offX = Number(xfrm?.["a:off"]?.["@_x"]) || 0;
  const offY = Number(xfrm?.["a:off"]?.["@_y"]) || 0;
  const extX = Number(xfrm?.["a:ext"]?.["@_cx"]) || 0;
  const extY = Number(xfrm?.["a:ext"]?.["@_cy"]) || 0;
  const chOffX = Number(xfrm?.["a:chOff"]?.["@_x"]) || 0;
  const chOffY = Number(xfrm?.["a:chOff"]?.["@_y"]) || 0;
  const chExtX = Number(xfrm?.["a:chExt"]?.["@_cx"]) || extX || 1;
  const chExtY = Number(xfrm?.["a:chExt"]?.["@_cy"]) || extY || 1;

  const localScaleX = extX > 0 && chExtX > 0 ? extX / chExtX : 1;
  const localScaleY = extY > 0 && chExtY > 0 ? extY / chExtY : 1;

  return {
    offsetX: parent.offsetX + (offX - chOffX * localScaleX) * parent.scaleX,
    offsetY: parent.offsetY + (offY - chOffY * localScaleY) * parent.scaleY,
    scaleX: parent.scaleX * localScaleX,
    scaleY: parent.scaleY * localScaleY,
  };
}

function getShapeIdentity(node) {
  const cNvPr =
    node?.["p:nvSpPr"]?.["p:cNvPr"] ||
    node?.["p:nvPicPr"]?.["p:cNvPr"] ||
    node?.["p:nvGraphicFramePr"]?.["p:cNvPr"] ||
    node?.["p:nvCxnSpPr"]?.["p:cNvPr"] ||
    node?.["p:nvGrpSpPr"]?.["p:cNvPr"] ||
    {};

  return {
    id: Number(cNvPr?.["@_id"]) || null,
    name: cNvPr?.["@_name"] || "Unnamed element",
  };
}

function getPlaceholderInfo(node) {
  const ph =
    node?.["p:nvSpPr"]?.["p:nvPr"]?.["p:ph"] ||
    node?.["p:nvGraphicFramePr"]?.["p:nvPr"]?.["p:ph"] ||
    node?.["p:nvPicPr"]?.["p:nvPr"]?.["p:ph"] ||
    null;

  if (!ph) return null;

  return {
    kind: ph?.["@_type"] || ph?.["@_idx"] || "placeholder",
    idx: ph?.["@_idx"] || null,
  };
}

function inferVisualRole(element, slideLayout, options = {}) {
  if (element.role) return element.role;
  if (options.isMaster) return "decorative";
  if (element.type === "placeholder") return "placeholder";
  if (element.type === "text" || element.type === "table-text") return "content";

  const slideArea = Math.max(slideLayout.width * slideLayout.height, 0.0001);
  const area = Math.max(element.w * element.h, 0);
  const areaRatio = area / slideArea;
  const edgeThresholdX = Math.max(0.2, slideLayout.width * 0.03);
  const edgeThresholdY = Math.max(0.2, slideLayout.height * 0.04);
  const touchesEdge =
    element.x <= edgeThresholdX ||
    element.y <= edgeThresholdY ||
    element.x + element.w >= slideLayout.width - edgeThresholdX ||
    element.y + element.h >= slideLayout.height - edgeThresholdY;

  if (element.type === "shape") {
    if (areaRatio >= 0.18 && touchesEdge) return "decorative";
    if (areaRatio <= 0.05 && touchesEdge) return "decorative";
    return "content";
  }

  if (element.type === "image") {
    if (areaRatio >= 0.5 && touchesEdge) return "decorative";
    if (areaRatio <= 0.04 && touchesEdge) return "decorative";
    return "content";
  }

  return "content";
}

function buildPlaceholderText(placeholderInfo, name) {
  const kind = placeholderInfo?.kind || "placeholder";
  const normalized = kind
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim();
  return `[${normalized || name || "Placeholder"}]`;
}

function collectRunNodes(paragraph) {
  const runs = [];

  for (const run of asArray(paragraph?.["a:r"])) {
    runs.push({
      text: run?.["a:t"],
      style: run?.["a:rPr"] || null,
    });
  }

  for (const run of asArray(paragraph?.["a:fld"])) {
    runs.push({
      text: run?.["a:t"],
      style: run?.["a:rPr"] || null,
    });
  }

  if (runs.length === 0 && paragraph?.["a:t"]) {
    runs.push({ text: paragraph["a:t"], style: null });
  }

  return runs;
}

function extractTextAndStyle(textBody, colorContext = {}, sourceElementId = "element") {
  const paragraphs = asArray(textBody?.["a:p"]);
  const blocks = [];

  for (let index = 0; index < paragraphs.length; index += 1) {
    const paragraph = paragraphs[index];
    const runs = collectRunNodes(paragraph);
    const text = joinTextParts(runs.map((run) => run.text)).trim();
    if (!text) continue;

    const firstStyle =
      runs.find((run) => run.style)?.style ||
      paragraph?.["a:pPr"]?.["a:defRPr"] ||
      paragraph?.["a:endParaRPr"] ||
      null;

    const fontSizePt = Number.isFinite(Number(firstStyle?.["@_sz"]))
      ? round(Number(firstStyle["@_sz"]) / 100)
      : null;

    blocks.push({
      blockId: `${sourceElementId}:b${blocks.length + 1}`,
      role: "paragraph",
      localIndex: blocks.length,
      text,
      fontSizePt: fontSizePt || undefined,
      bold: firstStyle?.["@_b"] === "1",
      italic: firstStyle?.["@_i"] === "1",
      color:
        resolveFillColor(firstStyle?.["a:solidFill"], colorContext) ||
        resolveStyleColor(paragraph?.["a:pPr"], "a:fontRef", colorContext) ||
        null,
    });
  }

  const firstBlock = blocks[0] || {};
  return {
    text: blocks.map((block) => block.text).join("\n"),
    blocks,
    fontSizePt: firstBlock.fontSizePt || undefined,
    bold: Boolean(firstBlock.bold),
    italic: Boolean(firstBlock.italic),
    color: firstBlock.color || null,
  };
}

function extractTableText(tableNode, colorContext = {}, sourceElementId = "element") {
  const rows = asArray(tableNode?.["a:tr"]);
  const blocks = [];

  for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
    const row = rows[rowIndex];
    const cells = asArray(row?.["a:tc"]);
    const cellTexts = [];
    let rowStyle = null;

    for (const cell of cells) {
      const extracted = extractTextAndStyle(
        cell?.["a:txBody"],
        colorContext,
        `${sourceElementId}:row${rowIndex + 1}:cell${cellTexts.length + 1}`
      );
      if (extracted.text) {
        cellTexts.push(extracted.text.replace(/\n+/g, " ").trim());
        rowStyle = rowStyle || extracted.blocks[0] || null;
      }
    }

    const rowText = cellTexts.join(" | ").trim();
    if (!rowText) continue;

    blocks.push({
      blockId: `${sourceElementId}:b${blocks.length + 1}`,
      role: "row",
      localIndex: blocks.length,
      text: rowText,
      fontSizePt: rowStyle?.fontSizePt || undefined,
      bold: Boolean(rowStyle?.bold),
      italic: Boolean(rowStyle?.italic),
      color: rowStyle?.color || null,
    });
  }

  const firstBlock = blocks[0] || {};
  return {
    text: blocks.map((block) => block.text).join("\n"),
    blocks,
    fontSizePt: firstBlock.fontSizePt || undefined,
    bold: Boolean(firstBlock.bold),
    italic: Boolean(firstBlock.italic),
    color: firstBlock.color || null,
  };
}

function parseBackgroundNode(bgNode, relationships, colorContext = {}) {
  if (!bgNode) return null;

  const bgPr = bgNode["p:bgPr"] || bgNode["a:bgPr"] || bgNode;
  const bgRef = bgNode["p:bgRef"] || bgNode["a:bgRef"] || null;

  if (bgPr?.["a:blipFill"]?.["a:blip"]?.["@_r:embed"]) {
    return {
      color: null,
      imageRelId: bgPr["a:blipFill"]["a:blip"]["@_r:embed"],
      source: relationships?.get(bgPr["a:blipFill"]["a:blip"]["@_r:embed"])?.target || null,
    };
  }

  const solidColor =
    resolveFillColor(bgPr?.["a:solidFill"], colorContext) ||
    resolveColorNode(bgRef || {}, colorContext);

  if (solidColor) {
    return {
      color: solidColor,
      imageRelId: null,
      source: null,
    };
  }

  return null;
}

async function readDataUri(zip, mediaPath, cache) {
  if (!mediaPath) return null;
  if (cache.has(mediaPath)) return cache.get(mediaPath);

  const entry = zip.file(mediaPath);
  if (!entry) {
    cache.set(mediaPath, null);
    return null;
  }

  const ext = path.posix.extname(mediaPath).toLowerCase();
  const mimeType = MIME_TYPES[ext] || "application/octet-stream";
  const base64 = await entry.async("base64");
  const dataUri = `data:${mimeType};base64,${base64}`;
  cache.set(mediaPath, dataUri);
  return dataUri;
}

function resolveZipPath(basePath, target) {
  if (!target) return null;
  if (target.startsWith("/")) return target.replace(/^\/+/, "");
  return path.posix.normalize(path.posix.join(path.posix.dirname(basePath), target));
}

async function readXml(zip, xmlPath, cache) {
  if (cache.has(xmlPath)) return cache.get(xmlPath);
  const entry = zip.file(xmlPath);
  if (!entry) {
    cache.set(xmlPath, null);
    return null;
  }

  const xml = await entry.async("string");
  const parser = new XMLParser(XML_OPTIONS);
  const parsed = parser.parse(xml);
  cache.set(xmlPath, parsed);
  return parsed;
}

async function readRelationships(zip, xmlPath, cache) {
  if (cache.has(xmlPath)) return cache.get(xmlPath);

  const relsPath = path.posix.join(
    path.posix.dirname(xmlPath),
    "_rels",
    `${path.posix.basename(xmlPath)}.rels`
  );
  const relsDoc = await readXml(zip, relsPath, cache);
  const rels = new Map();

  for (const rel of asArray(relsDoc?.Relationships?.Relationship)) {
    rels.set(rel?.["@_Id"], {
      id: rel?.["@_Id"],
      type: rel?.["@_Type"] || "",
      target: resolveZipPath(xmlPath, rel?.["@_Target"]),
    });
  }

  cache.set(xmlPath, rels);
  return rels;
}

function getRootSpTree(doc) {
  return (
    doc?.["p:sld"]?.["p:cSld"]?.["p:spTree"] ||
    doc?.["p:sldLayout"]?.["p:cSld"]?.["p:spTree"] ||
    doc?.["p:sldMaster"]?.["p:cSld"]?.["p:spTree"] ||
    null
  );
}

function getRootBackground(doc) {
  return (
    doc?.["p:sld"]?.["p:cSld"]?.["p:bg"] ||
    doc?.["p:sldLayout"]?.["p:cSld"]?.["p:bg"] ||
    doc?.["p:sldMaster"]?.["p:cSld"]?.["p:bg"] ||
    null
  );
}

function sortNodesByIdentity(nodes) {
  return [...nodes].sort((a, b) => {
    const aId = getShapeIdentity(a)?.id;
    const bId = getShapeIdentity(b)?.id;
    if (Number.isFinite(aId) && Number.isFinite(bId) && aId !== bId) return aId - bId;
    return 0;
  });
}

async function extractShapeNode(node, extractionContext) {
  const {
    slideIndex,
    slideLayout,
    colorContext,
    relationships,
    zip,
    mediaCache,
    nextElementId,
    groupContext,
    isMaster,
  } = extractionContext;

  const identity = getShapeIdentity(node);
  const placeholderInfo = getPlaceholderInfo(node);
  const spPr = node?.["p:spPr"] || node?.["p:cxnSpPr"] || {};
  const styleNode = node?.["p:style"] || {};
  const box = readTransform(spPr?.["a:xfrm"], groupContext);
  const textResult = extractTextAndStyle(
    node?.["p:txBody"],
    colorContext,
    `${makeSourceElementId(slideIndex, isMaster ? "m" : "e", nextElementId())}:text`
  );
  const fill = resolveShapeFill(spPr, styleNode, colorContext);
  const line = resolveShapeLine(spPr, styleNode, colorContext);
  const shapeType = spPr?.["a:prstGeom"]?.["@_prst"] || "rect";

  if (fill.fillImageRelId) {
    const mediaPath = relationships.get(fill.fillImageRelId)?.target || null;
    const dataUri = await readDataUri(zip, mediaPath, mediaCache);
    if (dataUri) {
      const sourceElementId = makeSourceElementId(slideIndex, isMaster ? "m" : "e", nextElementId());
      const element = {
        type: "image",
        id: identity.id,
        name: identity.name,
        sourceElementId,
        x: box.x,
        y: box.y,
        w: box.w,
        h: box.h,
        dataUri,
        layer: sourceElementId,
      };
      element.role = inferVisualRole(element, slideLayout, { isMaster });
      return element;
    }
  }

  if (textResult.text) {
    const sourceElementId = makeSourceElementId(slideIndex, isMaster ? "m" : "e", nextElementId());
    const element = {
      type: "text",
      id: identity.id,
      name: identity.name,
      sourceElementId,
      x: box.x,
      y: box.y,
      w: box.w,
      h: box.h,
      text: textResult.text,
      blocks: textResult.blocks.map((block, index) => ({
        ...block,
        blockId: `${sourceElementId}:b${index + 1}`,
      })),
      fontSizePt: textResult.fontSizePt || undefined,
      bold: Boolean(textResult.bold),
      italic: Boolean(textResult.italic),
      color: textResult.color || resolveStyleColor(styleNode, "a:fontRef", colorContext) || null,
      placeholderKind: placeholderInfo?.kind || null,
      shapeType,
    };
    element.role = inferVisualRole(element, slideLayout, { isMaster });
    return element;
  }

  if (placeholderInfo) {
    if (isMaster) return null;
    const sourceElementId = makeSourceElementId(slideIndex, "e", nextElementId());
    return {
      type: "placeholder",
      id: identity.id,
      name: identity.name,
      sourceElementId,
      x: box.x,
      y: box.y,
      w: box.w,
      h: box.h,
      text: buildPlaceholderText(placeholderInfo, identity.name),
      placeholderKind: placeholderInfo.kind,
      role: "placeholder",
      layer: sourceElementId,
    };
  }

  if (!fill.fillColor && line.noLine) {
    return null;
  }

  const sourceElementId = makeSourceElementId(slideIndex, isMaster ? "m" : "e", nextElementId());
  const element = {
    type: "shape",
    id: identity.id,
    name: identity.name,
    sourceElementId,
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    fillColor: fill.fillColor || null,
    lineColor: line.lineColor || null,
    lineWidthPt: line.lineWidthPt || 0,
    noLine: Boolean(line.noLine),
    noFill: Boolean(fill.noFill),
    shapeType,
    layer: sourceElementId,
  };
  element.role = inferVisualRole(element, slideLayout, { isMaster });
  return element;
}

async function extractPictureNode(node, extractionContext) {
  const {
    slideIndex,
    slideLayout,
    relationships,
    zip,
    mediaCache,
    nextElementId,
    groupContext,
    isMaster,
  } = extractionContext;

  const identity = getShapeIdentity(node);
  const spPr = node?.["p:spPr"] || {};
  const placeholderInfo = getPlaceholderInfo(node);
  const relId = node?.["p:blipFill"]?.["a:blip"]?.["@_r:embed"] || null;
  const mediaPath = relationships.get(relId)?.target || null;
  const dataUri = await readDataUri(zip, mediaPath, mediaCache);
  const box = readTransform(spPr?.["a:xfrm"], groupContext);

  if (!dataUri && placeholderInfo) {
    if (isMaster) return null;
    const sourceElementId = makeSourceElementId(slideIndex, "e", nextElementId());
    return {
      type: "placeholder",
      id: identity.id,
      name: identity.name,
      sourceElementId,
      x: box.x,
      y: box.y,
      w: box.w,
      h: box.h,
      text: buildPlaceholderText(placeholderInfo, identity.name),
      placeholderKind: placeholderInfo.kind,
      role: "placeholder",
      layer: sourceElementId,
    };
  }

  if (!dataUri) return null;

  const sourceElementId = makeSourceElementId(slideIndex, isMaster ? "m" : "e", nextElementId());
  const element = {
    type: "image",
    id: identity.id,
    name: identity.name,
    sourceElementId,
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    dataUri,
    layer: sourceElementId,
  };
  element.role = inferVisualRole(element, slideLayout, { isMaster });
  return element;
}

async function extractGraphicFrameNode(node, extractionContext) {
  const {
    slideIndex,
    slideLayout,
    colorContext,
    nextElementId,
    groupContext,
    isMaster,
  } = extractionContext;

  const identity = getShapeIdentity(node);
  const placeholderInfo = getPlaceholderInfo(node);
  const graphicData = node?.["a:graphic"]?.["a:graphicData"] || {};
  const tableNode = graphicData?.["a:tbl"] || null;
  const box = readTransform(node?.["p:xfrm"], groupContext);

  if (tableNode) {
    const sourceElementId = makeSourceElementId(slideIndex, isMaster ? "m" : "e", nextElementId());
    const extracted = extractTableText(tableNode, colorContext, sourceElementId);
    return {
      type: "table-text",
      id: identity.id,
      name: identity.name,
      sourceElementId,
      x: box.x,
      y: box.y,
      w: box.w,
      h: box.h,
      text: extracted.text,
      blocks: extracted.blocks.map((block, index) => ({
        ...block,
        blockId: `${sourceElementId}:b${index + 1}`,
      })),
      fontSizePt: extracted.fontSizePt || undefined,
      bold: Boolean(extracted.bold),
      italic: Boolean(extracted.italic),
      color: extracted.color || null,
      role: inferVisualRole({ type: "table-text", x: box.x, y: box.y, w: box.w, h: box.h }, slideLayout, {
        isMaster,
      }),
      layer: sourceElementId,
    };
  }

  if (isMaster) return null;

  const uri = graphicData?.["@_uri"] || "";
  const placeholderKind =
    placeholderInfo?.kind ||
    (uri.includes("chart") && "chart") ||
    (uri.includes("diagram") && "diagram") ||
    (uri.includes("ole") && "ole") ||
    "graphic";

  const sourceElementId = makeSourceElementId(slideIndex, "e", nextElementId());
  return {
    type: "placeholder",
    id: identity.id,
    name: identity.name,
    sourceElementId,
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    text: buildPlaceholderText({ kind: placeholderKind }, identity.name),
    placeholderKind,
    role: "placeholder",
    layer: sourceElementId,
  };
}

async function extractContainerElements(containerNode, extractionContext) {
  if (!containerNode) return [];

  const elements = [];
  const localNextElementId = extractionContext.nextElementId;

  for (const shape of sortNodesByIdentity(asArray(containerNode["p:sp"]))) {
    const element = await extractShapeNode(shape, extractionContext);
    if (element) elements.push({ ...element, layer: localNextElementId.currentLayer() });
  }

  for (const picture of sortNodesByIdentity(asArray(containerNode["p:pic"]))) {
    const element = await extractPictureNode(picture, extractionContext);
    if (element) elements.push({ ...element, layer: localNextElementId.currentLayer() });
  }

  for (const graphicFrame of sortNodesByIdentity(asArray(containerNode["p:graphicFrame"]))) {
    const element = await extractGraphicFrameNode(graphicFrame, extractionContext);
    if (element) elements.push({ ...element, layer: localNextElementId.currentLayer() });
  }

  for (const connector of sortNodesByIdentity(asArray(containerNode["p:cxnSp"]))) {
    const element = await extractShapeNode(connector, extractionContext);
    if (element) elements.push({ ...element, layer: localNextElementId.currentLayer() });
  }

  for (const group of sortNodesByIdentity(asArray(containerNode["p:grpSp"]))) {
    const groupContext = deriveGroupContext(group?.["p:grpSpPr"]?.["a:xfrm"], extractionContext.groupContext);
    const nested = await extractContainerElements(group, {
      ...extractionContext,
      groupContext,
    });
    elements.push(...nested);
  }

  return elements;
}

function countByType(elements, type) {
  return elements.filter((element) => element.type === type).length;
}

function countDecorativeElements(elements) {
  return elements.filter((element) => element.role === "decorative").length;
}

async function resolveBackground({ slideDoc, slideRels, layoutDoc, layoutRels, masterDoc, masterRels, zip, mediaCache, colorContext }) {
  const slideBg = parseBackgroundNode(getRootBackground(slideDoc), slideRels, colorContext);
  const layoutBg = parseBackgroundNode(getRootBackground(layoutDoc), layoutRels, colorContext);
  const masterBg = parseBackgroundNode(getRootBackground(masterDoc), masterRels, colorContext);
  const chosen = slideBg || layoutBg || masterBg;
  if (!chosen) return null;

  let dataUri = null;
  if (chosen.imageRelId) {
    const relSource = slideBg === chosen ? slideRels : layoutBg === chosen ? layoutRels : masterRels;
    const mediaPath = relSource?.get(chosen.imageRelId)?.target || chosen.source || null;
    dataUri = await readDataUri(zip, mediaPath, mediaCache);
  }

  return {
    color: chosen.color || null,
    dataUri,
  };
}

async function readSlideModel({
  zip,
  slidePath,
  slideIndex,
  sourceLayout,
  xmlCache,
  relCache,
  mediaCache,
  themeCache,
}) {
  const slideDoc = await readXml(zip, slidePath, xmlCache);
  const slideRels = await readRelationships(zip, slidePath, relCache);
  const layoutPath = Array.from(slideRels.values()).find(
    (rel) => rel.type === RELATIONSHIP_TYPES.slideLayout
  )?.target;
  const layoutDoc = layoutPath ? await readXml(zip, layoutPath, xmlCache) : null;
  const layoutRels = layoutPath ? await readRelationships(zip, layoutPath, relCache) : new Map();
  const masterPath = Array.from(layoutRels.values()).find(
    (rel) => rel.type === RELATIONSHIP_TYPES.slideMaster
  )?.target;
  const masterDoc = masterPath ? await readXml(zip, masterPath, xmlCache) : null;
  const masterRels = masterPath ? await readRelationships(zip, masterPath, relCache) : new Map();
  const themePath = Array.from(masterRels.values()).find(
    (rel) => rel.type === RELATIONSHIP_TYPES.theme
  )?.target;

  let themeColors = {};
  if (themePath) {
    if (themeCache.has(themePath)) {
      themeColors = themeCache.get(themePath);
    } else {
      themeColors = parseThemeColors(await readXml(zip, themePath, xmlCache));
      themeCache.set(themePath, themeColors);
    }
  }

  const colorContext = {
    themeColors,
    colorMap: withDefaultColorMap({
      ...parseColorMap(masterDoc),
      ...parseColorMap(layoutDoc),
      ...parseColorMap(slideDoc),
    }),
  };

  let elementCounter = 0;
  let layerCounter = 0;
  const nextElementId = () => {
    elementCounter += 1;
    return elementCounter;
  };
  nextElementId.currentLayer = () => {
    layerCounter += 1;
    return layerCounter;
  };

  const background = await resolveBackground({
    slideDoc,
    slideRels,
    layoutDoc,
    layoutRels,
    masterDoc,
    masterRels,
    zip,
    mediaCache,
    colorContext,
  });

  const masterElementsRaw = [
    ...(await extractContainerElements(getRootSpTree(masterDoc), {
      slideIndex,
      slideLayout: sourceLayout,
      colorContext,
      relationships: masterRels,
      zip,
      mediaCache,
      nextElementId,
      groupContext: { offsetX: 0, offsetY: 0, scaleX: 1, scaleY: 1 },
      isMaster: true,
    })),
    ...(await extractContainerElements(getRootSpTree(layoutDoc), {
      slideIndex,
      slideLayout: sourceLayout,
      colorContext,
      relationships: layoutRels,
      zip,
      mediaCache,
      nextElementId,
      groupContext: { offsetX: 0, offsetY: 0, scaleX: 1, scaleY: 1 },
      isMaster: true,
    })),
  ]
    .filter((element) => element.role === "decorative")
    .map((element, index) => ({ ...element, layer: index + 1 }));

  const slideElementsRaw = await extractContainerElements(getRootSpTree(slideDoc), {
    slideIndex,
    slideLayout: sourceLayout,
    colorContext,
    relationships: slideRels,
    zip,
    mediaCache,
    nextElementId,
    groupContext: { offsetX: 0, offsetY: 0, scaleX: 1, scaleY: 1 },
    isMaster: false,
  });

  const slideElements = slideElementsRaw.map((element, index) => ({
    ...element,
    layer: masterElementsRaw.length + index + 1,
  }));

  const placeholderCount = countByType(slideElements, "placeholder");
  const recoveredGraphicCount = countByType(slideElements, "table-text");
  const recoveredShapeCount = countByType(slideElements, "shape");
  const unsupportedCount = placeholderCount;

  return {
    index: slideIndex,
    sourcePath: slidePath,
    background,
    masterElements: masterElementsRaw,
    unsupportedCount,
    recoveredGraphicCount,
    placeholderCount,
    recoveredShapeCount,
    decorativeCount: countDecorativeElements(slideElements) + masterElementsRaw.length,
    elements: slideElements,
  };
}

async function getOrderedSlidePaths(zip, xmlCache, relCache) {
  const presentationDoc = await readXml(zip, "ppt/presentation.xml", xmlCache);
  const presentationRels = await readRelationships(zip, "ppt/presentation.xml", relCache);
  const slideIds = asArray(presentationDoc?.["p:presentation"]?.["p:sldIdLst"]?.["p:sldId"]);
  const orderedPaths = [];

  for (const slideId of slideIds) {
    const rel = presentationRels.get(slideId?.["@_r:id"]);
    if (rel?.target) orderedPaths.push(rel.target);
  }

  if (orderedPaths.length > 0) return orderedPaths;

  return zip
    .file(/^ppt\/slides\/slide\d+\.xml$/)
    .map((entry) => entry.name)
    .sort((a, b) => {
      const aNum = Number(a.match(/slide(\d+)\.xml$/)?.[1]);
      const bNum = Number(b.match(/slide(\d+)\.xml$/)?.[1]);
      return aNum - bNum;
    });
}

async function readPptxToModel(sourcePath) {
  const fileBuffer = await fs.readFile(sourcePath);
  const zip = await JSZip.loadAsync(fileBuffer);
  const xmlCache = new Map();
  const relCache = new Map();
  const mediaCache = new Map();
  const themeCache = new Map();
  const presentationDoc = await readXml(zip, "ppt/presentation.xml", xmlCache);
  const slideSize = presentationDoc?.["p:presentation"]?.["p:sldSz"] || {};
  const sourceLayout = normalizeLayout({
    id: "source",
    name: "SOURCE",
    width: toInches(slideSize?.["@_cx"]) || 13.333,
    height: toInches(slideSize?.["@_cy"]) || 7.5,
  });

  const slidePaths = await getOrderedSlidePaths(zip, xmlCache, relCache);
  const slides = [];
  const parsingStats = {
    extractedGraphicFrames: 0,
    placeholderGraphicFrames: 0,
    unsupportedGraphicFrames: 0,
    extractedShapeFallbacks: 0,
    extractedMasterElements: 0,
  };

  for (let index = 0; index < slidePaths.length; index += 1) {
    const slide = await readSlideModel({
      zip,
      slidePath: slidePaths[index],
      slideIndex: index + 1,
      sourceLayout,
      xmlCache,
      relCache,
      mediaCache,
      themeCache,
    });

    parsingStats.extractedGraphicFrames += slide.recoveredGraphicCount;
    parsingStats.placeholderGraphicFrames += slide.placeholderCount;
    parsingStats.unsupportedGraphicFrames += slide.unsupportedCount;
    parsingStats.extractedShapeFallbacks += slide.recoveredShapeCount;
    parsingStats.extractedMasterElements += slide.masterElements.length;
    slides.push(slide);
  }

  return {
    sourcePath: path.resolve(sourcePath),
    sourceLayout,
    slides,
    parsingStats,
  };
}

module.exports = {
  readPptxToModel,
  __test: {
    applyLuminanceTransforms,
    extractTableText,
    extractTextAndStyle,
    parseThemeColors,
    parseColorMap,
    resolveColorNode,
    withDefaultColorMap,
  },
};
