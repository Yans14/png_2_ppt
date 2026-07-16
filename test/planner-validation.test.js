const test = require("node:test");
const assert = require("node:assert/strict");

const { validateSlidePlan } = require("../src/planners/validate");
const { createSourceModel } = require("./helpers");

function getSourceSlide() {
  return createSourceModel().slides[0];
}

test("planner validation rejects unknown source element ids", () => {
  const validation = validateSlidePlan(
    {
      strategy: "reflow",
      confidence: 0.9,
      reason: "test",
      outputSlides: [
        {
          templateId: "title_body",
          assignments: [
            {
              sourceElementId: "missing",
              blockIds: [],
              slotId: "title",
              repeat: false,
            },
          ],
        },
      ],
    },
    getSourceSlide(),
    { maxOutputSlidesPerSource: 3 }
  );

  assert.equal(validation.valid, false);
  assert.match(validation.errors.join(" "), /Unknown source element/);
});

test("planner validation rejects unknown slots", () => {
  const slide = getSourceSlide();
  const validation = validateSlidePlan(
    {
      strategy: "reflow",
      confidence: 0.9,
      reason: "test",
      outputSlides: [
        {
          templateId: "title_body",
          assignments: [
            {
              sourceElementId: slide.elements[0].sourceElementId,
              blockIds: slide.elements[0].blocks.map((block) => block.blockId),
              slotId: "unknown_slot",
              repeat: false,
            },
            {
              sourceElementId: slide.elements[1].sourceElementId,
              blockIds: slide.elements[1].blocks.map((block) => block.blockId),
              slotId: "body",
              repeat: false,
            },
          ],
        },
      ],
    },
    slide,
    { maxOutputSlidesPerSource: 3 }
  );

  assert.equal(validation.valid, false);
  assert.match(validation.errors.join(" "), /Unknown slot/);
});

test("planner validation rejects excessive output slide counts", () => {
  const slide = getSourceSlide();
  const validation = validateSlidePlan(
    {
      strategy: "split",
      confidence: 0.9,
      reason: "test",
      outputSlides: new Array(4).fill(null).map(() => ({
        templateId: "continuation_list",
        assignments: [
          {
            sourceElementId: slide.elements[1].sourceElementId,
            blockIds: [slide.elements[1].blocks[0].blockId],
            slotId: "body",
            repeat: false,
          },
        ],
      })),
    },
    slide,
    { maxOutputSlidesPerSource: 3 }
  );

  assert.equal(validation.valid, false);
  assert.match(validation.errors.join(" "), /exceeds max output slides/);
});
