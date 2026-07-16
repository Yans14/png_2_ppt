const https = require("https");

const { getTemplateCatalog } = require("../templates");

function buildPlannerResponseSchema(maxOutputSlides) {
  return {
    type: "object",
    additionalProperties: false,
    required: ["strategy", "confidence", "reason", "outputSlides"],
    properties: {
      strategy: {
        type: "string",
        enum: ["preserve", "reflow", "split"],
      },
      confidence: {
        type: "number",
        minimum: 0,
        maximum: 1,
      },
      reason: {
        type: "string",
        minLength: 1,
      },
      outputSlides: {
        type: "array",
        minItems: 1,
        maxItems: maxOutputSlides,
        items: {
          type: "object",
          additionalProperties: false,
          required: ["templateId", "assignments"],
          properties: {
            templateId: {
              type: "string",
            },
            assignments: {
              type: "array",
              items: {
                type: "object",
                additionalProperties: false,
                required: ["sourceElementId", "slotId", "repeat", "blockIds"],
                properties: {
                  sourceElementId: {
                    type: "string",
                  },
                  blockIds: {
                    type: "array",
                    items: { type: "string" },
                  },
                  slotId: {
                    type: "string",
                  },
                  repeat: {
                    type: "boolean",
                  },
                },
              },
            },
          },
        },
      },
    },
  };
}

const SYSTEM_PROMPT = [
  "You are a slide-layout planner.",
  "Return JSON only.",
  "Choose one strategy for the current source slide: preserve, reflow, or split.",
  "Use preserve when a normal resize/reposition is enough.",
  "Use reflow when one templated output slide is better.",
  "Use split when the content should become multiple output slides within the same source slide boundary.",
  "For preserve, return exactly one output slide with templateId 'preserve' and an empty assignments array.",
  "Only assign movable content elements. Background, master, decorative, and unresolved placeholder visuals stay anchored automatically.",
  "Never invent text.",
  "Only use sourceElementId values and blockIds that exist in the input.",
  "Only use the provided templateIds and slotIds.",
  "If you reuse an element or block across multiple output slides, set repeat=true.",
  "Keep the plan readable for the chosen target size and layout goal.",
].join(" ");

function extractResponseText(responseJson) {
  if (typeof responseJson.output_text === "string" && responseJson.output_text.trim()) {
    return responseJson.output_text;
  }

  const output = Array.isArray(responseJson.output) ? responseJson.output : [];
  for (const item of output) {
    const content = Array.isArray(item?.content) ? item.content : [];
    for (const part of content) {
      if (typeof part?.text === "string" && part.text.trim()) {
        return part.text;
      }
    }
  }

  return "";
}

function requestJson({ apiKey, body, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request(
      {
        method: "POST",
        hostname: "api.openai.com",
        path: "/v1/responses",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
        timeout: timeoutMs,
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf8");
          let json;
          try {
            json = raw ? JSON.parse(raw) : {};
          } catch (error) {
            reject(new Error(`OpenAI returned invalid JSON: ${error.message}`));
            return;
          }

          if (res.statusCode < 200 || res.statusCode >= 300) {
            const message = json?.error?.message || `OpenAI request failed with status ${res.statusCode}.`;
            reject(new Error(message));
            return;
          }

          resolve(json);
        });
      }
    );

    req.on("timeout", () => {
      req.destroy(new Error(`OpenAI request timed out after ${timeoutMs}ms.`));
    });
    req.on("error", (error) => reject(error));
    req.write(payload);
    req.end();
  });
}

function buildDeckContext(model, slideIndex) {
  const current = model.slides[slideIndex];
  const previous = model.slides[slideIndex - 1];
  const next = model.slides[slideIndex + 1];

  function preview(slide) {
    if (!slide) return null;
    const textElement = slide.elements.find((element) => typeof element.text === "string" && element.text.trim());
    return {
      slideIndex: slide.index,
      preview: textElement ? textElement.text.slice(0, 140) : "",
    };
  }

  return {
    totalSlides: model.slides.length,
    previous: preview(previous),
    current: preview(current),
    next: preview(next),
  };
}

function buildPlannerInput({
  model,
  sourceSlide,
  slideIndex,
  targetLayout,
  policy,
  layoutGoal,
  modelName,
  preserveAssessment,
}) {
  const movableElements = sourceSlide.elements.filter((element) => element.role === "content");
  const anchoredVisuals = [
    ...(Array.isArray(sourceSlide.masterElements) ? sourceSlide.masterElements : []),
    ...sourceSlide.elements.filter((element) => element.role !== "content"),
  ];

  return {
    plannerModel: modelName,
    layoutGoal,
    slideIndex: sourceSlide.index,
    totalSlides: model.slides.length,
    sourceLayout: model.sourceLayout,
    targetLayout: {
      id: targetLayout.id,
      name: targetLayout.name,
      width: targetLayout.width,
      height: targetLayout.height,
      sizeClass: targetLayout.sizeClass,
      safeMargins: targetLayout.safeMargins,
    },
    deckContext: buildDeckContext(model, slideIndex),
    preserveAssessment: preserveAssessment || null,
    hardConstraints: {
      maxOutputSlidesPerSource: policy.maxOutputSlidesPerSource,
      noCrossSlideBoundaryMoves: true,
      allowInventedText: false,
      requireRepeatFlagOnReuse: true,
      moveContentOnlyByDefault: true,
      placeholderRule: "chart/diagram/ole placeholders must remain placeholders unless source extraction already recovered content",
    },
    templates: getTemplateCatalog(),
    sourceSlide: {
      index: sourceSlide.index,
      unsupportedCount: sourceSlide.unsupportedCount,
      placeholderCount: sourceSlide.placeholderCount,
      anchoredVisuals: {
        background: sourceSlide.background || null,
        elementCount: anchoredVisuals.length,
        masterElementCount: Array.isArray(sourceSlide.masterElements) ? sourceSlide.masterElements.length : 0,
      },
      elements: movableElements.map((element) => ({
        sourceElementId: element.sourceElementId,
        type: element.type,
        name: element.name,
        x: element.x,
        y: element.y,
        w: element.w,
        h: element.h,
        text: element.text || "",
        placeholderKind: element.placeholderKind || null,
        blocks: Array.isArray(element.blocks)
          ? element.blocks.map((block) => ({
              blockId: block.blockId,
              role: block.role,
              text: block.text,
            }))
          : [],
      })),
    },
  };
}

function createOpenAIPlanner(options = {}) {
  const apiKey = options.apiKey || process.env.OPENAI_API_KEY || "";
  const model = options.model || "gpt-5.5";
  const timeoutMs = Number(options.timeoutMs) || 45000;
  const layoutGoal =
    options.layoutGoal || "Preserve source content while maximizing readability for the chosen target size.";

  return {
    type: "openai",
    model,
    async planSlide({ model: sourceModel, sourceSlide, slideIndex, targetLayout, policy, preserveAssessment }) {
      if (!apiKey) {
        throw new Error("Missing OPENAI_API_KEY.");
      }

      const inputPayload = buildPlannerInput({
        model: sourceModel,
        sourceSlide,
        slideIndex,
        targetLayout,
        policy,
        layoutGoal,
        modelName: model,
        preserveAssessment,
      });

      const responseJson = await requestJson({
        apiKey,
        timeoutMs,
        body: {
          model,
          input: [
            {
              role: "system",
              content: [{ type: "input_text", text: SYSTEM_PROMPT }],
            },
            {
              role: "user",
              content: [{ type: "input_text", text: JSON.stringify(inputPayload) }],
            },
          ],
          text: {
            format: {
              type: "json_schema",
              name: "slide_plan",
              strict: true,
              schema: buildPlannerResponseSchema(policy.maxOutputSlidesPerSource || 3),
            },
          },
          max_output_tokens: 2200,
        },
      });

      const outputText = extractResponseText(responseJson);
      if (!outputText) {
        throw new Error("OpenAI planner response did not contain output text.");
      }

      try {
        return JSON.parse(outputText);
      } catch (error) {
        throw new Error(`OpenAI planner returned invalid JSON text: ${error.message}`);
      }
    },
  };
}

module.exports = {
  createOpenAIPlanner,
  buildPlannerResponseSchema,
};
