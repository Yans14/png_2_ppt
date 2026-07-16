const fs = require("fs/promises");
const path = require("path");
const PptxGenJS = require("pptxgenjs");

function normalizeColor(color, fallback = "000000") {
  if (!color) return fallback;
  const value = String(color).replace(/^#/, "").toUpperCase();
  if (/^[0-9A-F]{6}$/.test(value)) return value;
  return fallback;
}

async function ensureOutputDir(filePath) {
  const dirPath = path.dirname(filePath);
  await fs.mkdir(dirPath, { recursive: true });
}

function renderTextLikeElement(slide, element, options = {}) {
  const text = element.text || "";

  if (element.type === "placeholder" && options.renderPlaceholders) {
    slide.addShape("rect", {
      x: element.x,
      y: element.y,
      w: element.w,
      h: element.h,
      line: { color: "888888", pt: 1, dash: "dash" },
      fill: { color: "F5F5F5", transparency: 15 },
      radius: 0.03,
    });
  }

  if (element.type === "table-text" && options.renderTableBoxes) {
    slide.addShape("rect", {
      x: element.x,
      y: element.y,
      w: element.w,
      h: element.h,
      line: { color: "BBBBBB", pt: 0.5 },
      fill: { color: "FFFFFF", transparency: 0 },
      radius: 0,
    });
  }

  slide.addText(text, {
    x: element.x,
    y: element.y,
    w: element.w,
    h: element.h,
    fontSize: element.fontSizePt || 14,
    bold: Boolean(element.bold),
    italic: Boolean(element.italic) || element.type === "placeholder",
    color: normalizeColor(element.color),
    valign: "top",
    fit: "shrink",
    margin: 2,
  });
}

function renderShapeElement(slide, element) {
  if (!element.fillColor && !element.lineColor) {
    return;
  }

  const shapeOptions = {
    x: element.x,
    y: element.y,
    w: element.w,
    h: element.h,
  };

  if (element.lineColor) {
    shapeOptions.line = {
      color: normalizeColor(element.lineColor, "666666"),
      pt: element.lineWidthPt || 0.75,
    };
  } else {
    shapeOptions.line = { color: "FFFFFF", transparency: 100, pt: 0 };
  }

  if (element.fillColor) {
    shapeOptions.fill = {
      color: normalizeColor(element.fillColor, "FFFFFF"),
      transparency: 0,
    };
  } else {
    shapeOptions.fill = { color: "FFFFFF", transparency: 100 };
  }

  slide.addShape(element.shapeType || "rect", {
    ...shapeOptions,
  });
}

async function writePlanToPptx(plan, outputPath, rawOptions = {}) {
  const options = {
    renderPlaceholders: rawOptions.renderPlaceholders !== false,
    renderTableBoxes: rawOptions.renderTableBoxes !== false,
  };

  const pptx = new PptxGenJS();
  const layoutName = "CONVERTED_LAYOUT";

  pptx.defineLayout({
    name: layoutName,
    width: plan.targetLayout.width,
    height: plan.targetLayout.height,
  });
  pptx.layout = layoutName;
  pptx.author = "PptxGenJS Slide Converter Agent";
  pptx.subject = "Converted deck";
  pptx.title = "Slide format conversion";

  for (const plannedSlide of plan.slides) {
    const slide = pptx.addSlide();
    if (plannedSlide.background?.color) {
      slide.background = { color: normalizeColor(plannedSlide.background.color, "FFFFFF") };
    }
    if (plannedSlide.background?.dataUri) {
      slide.background = { data: plannedSlide.background.dataUri };
    }

    const elements = [...plannedSlide.elements].sort((a, b) => {
      const aLayer = Number(a.layer) || 0;
      const bLayer = Number(b.layer) || 0;
      if (aLayer !== bLayer) return aLayer - bLayer;
      return (a.y - b.y) || (a.x - b.x);
    });

    for (const element of elements) {
      if (
        element.type === "text" ||
        element.type === "table-text" ||
        element.type === "placeholder"
      ) {
        renderTextLikeElement(slide, element, options);
        continue;
      }

      if (element.type === "image" && element.dataUri) {
        slide.addImage({
          data: element.dataUri,
          x: element.x,
          y: element.y,
          w: element.w,
          h: element.h,
        });
        continue;
      }

      if (element.type === "shape") {
        renderShapeElement(slide, element);
      }
    }
  }

  await ensureOutputDir(outputPath);
  await pptx.writeFile({ fileName: outputPath });
}

module.exports = {
  writePlanToPptx,
};
