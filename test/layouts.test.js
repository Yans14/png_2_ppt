const test = require("node:test");
const assert = require("node:assert/strict");

const { getTargetLayout, inferSizeClass, normalizeLayout } = require("../src/layouts");

test("normalizes preset and custom layouts with size classes and safe margins", () => {
  const wide = getTargetLayout({ target: "wide" });
  const standard = getTargetLayout({ target: "standard" });
  const portrait = getTargetLayout({ target: "a4-portrait" });
  const square = normalizeLayout({ id: "custom", name: "CUSTOM", width: 9, height: 9 });

  assert.equal(wide.sizeClass, "landscape");
  assert.equal(standard.sizeClass, "landscape");
  assert.equal(portrait.sizeClass, "portrait");
  assert.equal(square.sizeClass, "square");
  assert.ok(wide.safeMargins.left > 0);
  assert.ok(portrait.safeMargins.top > portrait.safeMargins.left);
  assert.equal(inferSizeClass(9, 9), "square");
});
