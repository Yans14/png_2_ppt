const test = require("node:test");
const assert = require("node:assert/strict");

const { __test } = require("../src/pptx-reader");

test("extractTextAndStyle preserves paragraph blocks", () => {
  const extracted = __test.extractTextAndStyle({
    "a:p": [
      {
        "a:r": [
          {
            "a:rPr": { "@_sz": "2400", "@_b": "1", "a:solidFill": { "a:srgbClr": { "@_val": "112233" } } },
            "a:t": "Title line",
          },
        ],
      },
      {
        "a:r": [
          {
            "a:rPr": { "@_sz": "1800" },
            "a:t": "Second paragraph",
          },
        ],
      },
    ],
  });

  assert.equal(extracted.text, "Title line\nSecond paragraph");
  assert.equal(extracted.blocks.length, 2);
  assert.equal(extracted.blocks[0].role, "paragraph");
  assert.equal(extracted.blocks[0].text, "Title line");
  assert.equal(extracted.blocks[1].text, "Second paragraph");
});

test("extractTableText preserves row blocks", () => {
  const extracted = __test.extractTableText({
    "a:tr": [
      {
        "a:tc": [
          { "a:txBody": { "a:p": [{ "a:r": [{ "a:t": "Q1" }] }] } },
          { "a:txBody": { "a:p": [{ "a:r": [{ "a:t": "120" }] }] } },
        ],
      },
      {
        "a:tc": [
          { "a:txBody": { "a:p": [{ "a:r": [{ "a:t": "Q2" }] }] } },
          { "a:txBody": { "a:p": [{ "a:r": [{ "a:t": "145" }] }] } },
        ],
      },
    ],
  });

  assert.equal(extracted.text, "Q1 | 120\nQ2 | 145");
  assert.equal(extracted.blocks.length, 2);
  assert.equal(extracted.blocks[0].role, "row");
  assert.equal(extracted.blocks[1].text, "Q2 | 145");
});

test("resolveColorNode maps scheme colors through the color map", () => {
  const color = __test.resolveColorNode(
    {
      "a:schemeClr": {
        "@_val": "bg1",
      },
    },
    {
      themeColors: {
        lt1: "F3EBDD",
      },
      colorMap: __test.withDefaultColorMap({
        bg1: "lt1",
      }),
    }
  );

  assert.equal(color, "F3EBDD");
});
