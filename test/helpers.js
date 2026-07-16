const { normalizeLayout } = require("../src/layouts");

function createTextBlocks(sourceElementId, texts, role = "paragraph") {
  return texts.map((text, index) => ({
    blockId: `${sourceElementId}:b${index + 1}`,
    role,
    localIndex: index,
    text,
    fontSizePt: 20 - index * 2,
    bold: index === 0,
    color: "111111",
  }));
}

function createSourceModel() {
  const titleId = "s1:e1";
  const bodyId = "s1:e2";
  const imageId = "s1:e3";

  const titleBlocks = createTextBlocks(titleId, ["Quarterly Business Review"]);
  const bodyBlocks = createTextBlocks(bodyId, [
    "Revenue grew 18% year over year across the enterprise segment.",
    "Retention improved after the onboarding refresh and support changes.",
    "Pipeline remains strongest in EMEA and mid-market accounts.",
  ]);

  return {
    sourcePath: "/tmp/source.pptx",
    sourceLayout: normalizeLayout({ id: "source", name: "SOURCE", width: 13.333, height: 7.5 }),
    slides: [
      {
        index: 1,
        sourcePath: "ppt/slides/slide1.xml",
        background: { color: "F7F4EF", dataUri: null },
        masterElements: [
          {
            type: "shape",
            id: 901,
            name: "Footer Bar",
            sourceElementId: "s1:m1",
            x: 0,
            y: 7.05,
            w: 13.333,
            h: 0.45,
            fillColor: "8C1515",
            lineColor: null,
            lineWidthPt: 0,
            shapeType: "rect",
            role: "decorative",
            layer: 1,
          },
          {
            type: "image",
            id: 902,
            name: "Logo",
            sourceElementId: "s1:m2",
            x: 11.9,
            y: 0.3,
            w: 1,
            h: 0.45,
            dataUri: "data:image/png;base64,AA==",
            role: "decorative",
            layer: 2,
          },
        ],
        unsupportedCount: 0,
        recoveredGraphicCount: 0,
        placeholderCount: 0,
        recoveredShapeCount: 0,
        elements: [
          {
            type: "text",
            id: 1,
            name: "Title",
            sourceElementId: titleId,
            x: 0.8,
            y: 0.7,
            w: 7,
            h: 0.9,
            text: titleBlocks[0].text,
            blocks: titleBlocks,
            fontSizePt: 24,
            bold: true,
            color: "111111",
            role: "content",
            layer: 3,
          },
          {
            type: "text",
            id: 2,
            name: "Body",
            sourceElementId: bodyId,
            x: 0.9,
            y: 1.8,
            w: 6.5,
            h: 3.6,
            text: bodyBlocks.map((block) => block.text).join("\n"),
            blocks: bodyBlocks,
            fontSizePt: 18,
            bold: false,
            color: "222222",
            role: "content",
            layer: 4,
          },
          {
            type: "image",
            id: 3,
            name: "Hero Image",
            sourceElementId: imageId,
            x: 8.2,
            y: 1.5,
            w: 4.2,
            h: 3,
            dataUri: "data:image/png;base64,AA==",
            role: "content",
            layer: 5,
          },
        ],
      },
    ],
    parsingStats: {
      extractedGraphicFrames: 0,
      placeholderGraphicFrames: 0,
      unsupportedGraphicFrames: 0,
      extractedShapeFallbacks: 0,
      extractedMasterElements: 2,
    },
  };
}

function createBrandedSourceModel() {
  const model = createSourceModel();
  model.slides[0].elements.push({
    type: "shape",
    id: 4,
    name: "Accent Panel",
    sourceElementId: "s1:e4",
    x: 0,
    y: 0,
    w: 2.1,
    h: 7.5,
    fillColor: "DDD2C6",
    lineColor: null,
    lineWidthPt: 0,
    shapeType: "rect",
    role: "decorative",
    layer: 6,
  });
  return model;
}

module.exports = {
  createBrandedSourceModel,
  createSourceModel,
};
