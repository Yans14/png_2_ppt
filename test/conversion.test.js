const test = require("node:test");
const assert = require("node:assert/strict");

const { getTargetLayout } = require("../src/layouts");
const { buildConversionPlan } = require("../src/transform");
const { parseArgs } = require("../src/convert");
const { createBrandedSourceModel, createSourceModel } = require("./helpers");

test("wide to portrait can split into two planned output slides", async () => {
  const model = createSourceModel();
  const targetLayout = getTargetLayout({ target: "a4-portrait" });
  const splitPlanner = {
    type: "openai",
    model: "gpt-5.5",
    async planSlide({ sourceSlide }) {
      return {
        strategy: "split",
        confidence: 0.88,
        reason: "Portrait layout benefits from separating title/media and body.",
        outputSlides: [
          {
            templateId: "image_left_text_right",
            assignments: [
              {
                sourceElementId: sourceSlide.elements[0].sourceElementId,
                blockIds: sourceSlide.elements[0].blocks.map((block) => block.blockId),
                slotId: "title",
                repeat: false,
              },
              {
                sourceElementId: sourceSlide.elements[2].sourceElementId,
                blockIds: [],
                slotId: "media_primary",
                repeat: false,
              },
            ],
          },
          {
            templateId: "continuation_list",
            assignments: [
              {
                sourceElementId: sourceSlide.elements[0].sourceElementId,
                blockIds: sourceSlide.elements[0].blocks.map((block) => block.blockId),
                slotId: "title",
                repeat: true,
              },
              {
                sourceElementId: sourceSlide.elements[1].sourceElementId,
                blockIds: sourceSlide.elements[1].blocks.map((block) => block.blockId),
                slotId: "body",
                repeat: false,
              },
            ],
          },
        ],
      };
    },
  };

  const plan = await buildConversionPlan(model, targetLayout, {
    planner: "openai",
    plannerOverride: splitPlanner,
    maxOutputSlidesPerSource: 3,
  });

  assert.equal(plan.slides.length, 2);
  assert.equal(plan.report.slideReports[0].chosenStrategy, "split");
  assert.deepEqual(plan.report.slideReports[0].chosenTemplates, [
    "image_left_text_right",
    "continuation_list",
  ]);
  assert.equal(plan.report.slideReports[0].outputSlideCount, 2);
});

test("simple heuristic conversion preserves content within bounds", async () => {
  const model = createSourceModel();
  const targetLayout = getTargetLayout({ target: "wide" });
  const plan = await buildConversionPlan(model, targetLayout, { planner: "heuristic" });

  assert.equal(plan.report.slideReports[0].chosenStrategy, "preserve");
  assert.equal(plan.slides.length, 1);
  assert.equal(plan.slides[0].background.color, "F7F4EF");
  assert.ok(plan.slides[0].elements.some((element) => element.sourceElementId === "s1:m1"));
  assert.ok(plan.slides[0].elements.some((element) => element.sourceElementId === "s1:m2"));
  for (const element of plan.slides[0].elements) {
    assert.ok(element.x >= 0);
    assert.ok(element.y >= 0);
    assert.ok(element.x + element.w <= targetLayout.width + 0.001);
    assert.ok(element.y + element.h <= targetLayout.height + 0.001);
  }
});

test("planner errors fall back to heuristic and queue manual review", async () => {
  const model = createSourceModel();
  const targetLayout = getTargetLayout({ target: "a4-portrait" });
  const failingPlanner = {
    type: "openai",
    model: "gpt-5.5",
    async planSlide() {
      throw new Error("simulated api failure");
    },
  };

  const plan = await buildConversionPlan(model, targetLayout, {
    planner: "openai",
    plannerOverride: failingPlanner,
  });

  assert.equal(plan.report.slideReports[0].chosenStrategy, "preserve");
  assert.match(plan.report.slideReports[0].fallbackReason, /planner-error/);
  assert.ok(plan.report.slideReports[0].plannerLatencyMs >= 0);
  assert.equal(plan.report.manualReviewCount, 1);
});

test("invalid planner output falls back to heuristic and records warnings", async () => {
  const model = createSourceModel();
  const targetLayout = getTargetLayout({ target: "a4-portrait" });
  const invalidPlanner = {
    type: "openai",
    model: "gpt-5.5",
    async planSlide() {
      return {
        strategy: "reflow",
        confidence: 0.7,
        reason: "bad plan",
        outputSlides: [
          {
            templateId: "title_body",
            assignments: [
              {
                sourceElementId: "unknown",
                blockIds: [],
                slotId: "title",
                repeat: false,
              },
            ],
          },
        ],
      };
    },
  };

  const plan = await buildConversionPlan(model, targetLayout, {
    planner: "openai",
    plannerOverride: invalidPlanner,
    reflowPolicy: "auto",
  });

  assert.equal(plan.report.slideReports[0].chosenStrategy, "preserve");
  assert.match(plan.report.slideReports[0].fallbackReason, /planner-validation/);
  assert.ok(plan.report.slideReports[0].plannerValidationWarnings.length > 0);
});

test("strict reflow policy keeps high-fidelity branded slide on preserve path", async () => {
  const model = createBrandedSourceModel();
  const targetLayout = getTargetLayout({ target: "wide" });
  let plannerCalled = false;
  const aggressivePlanner = {
    type: "openai",
    model: "gpt-5.5",
    async planSlide() {
      plannerCalled = true;
      return {
        strategy: "reflow",
        confidence: 0.8,
        reason: "should not be used",
        outputSlides: [],
      };
    },
  };

  const plan = await buildConversionPlan(model, targetLayout, {
    planner: "openai",
    plannerOverride: aggressivePlanner,
    reflowPolicy: "strict",
  });

  assert.equal(plannerCalled, false);
  assert.equal(plan.report.slideReports[0].chosenStrategy, "preserve");
  assert.equal(plan.report.slideReports[0].reflowAllowed, false);
  assert.equal(plan.report.slideReports[0].fallbackReason, null);
});

test("planner timeout surfaces timeout fallback in report", async () => {
  const model = createSourceModel();
  const targetLayout = getTargetLayout({ target: "a4-portrait" });
  const timeoutPlanner = {
    type: "openai",
    model: "gpt-5.5",
    async planSlide() {
      throw new Error("OpenAI request timed out after 45000ms.");
    },
  };

  const plan = await buildConversionPlan(model, targetLayout, {
    planner: "openai",
    plannerOverride: timeoutPlanner,
    reflowPolicy: "auto",
  });

  assert.equal(plan.report.slideReports[0].chosenStrategy, "preserve");
  assert.equal(plan.report.slideReports[0].timeoutFallback, true);
  assert.ok(plan.report.slideReports[0].plannerLatencyMs >= 0);
});

test("cli parsing keeps heuristic planner explicit", () => {
  const args = parseArgs([
    "node",
    "src/convert.js",
    "--input",
    "/tmp/source.pptx",
    "--planner",
    "heuristic",
  ]);

  assert.equal(args.planner, "heuristic");
});

test("cli parsing accepts reflow policy and OpenAI timeout", () => {
  const args = parseArgs([
    "node",
    "src/convert.js",
    "--input",
    "/tmp/source.pptx",
    "--reflow-policy",
    "auto",
    "--openai-timeout-ms",
    "60000",
  ]);

  assert.equal(args.reflowPolicy, "auto");
  assert.equal(args.openaiTimeoutMs, 60000);
});
