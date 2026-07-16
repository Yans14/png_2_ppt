const { createPlanner } = require("./planners");
const { validateSlidePlan } = require("./planners/validate");
const { resolveTemplate } = require("./templates");

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function round(value, precision = 4) {
  const factor = 10 ** precision;
  return Math.round(value * factor) / factor;
}

function computeArea(item) {
  return Math.max(0, (item.w || 0) * (item.h || 0));
}

function overlapArea(a, b) {
  const xOverlap = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
  const yOverlap = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  return xOverlap * yOverlap;
}

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function isFlowElement(item) {
  return item.type === "text" || item.type === "table-text" || item.type === "placeholder";
}

function isPlannableElement(item) {
  return item.role === "content";
}

function sortElementsByLayer(items) {
  return [...items].sort((a, b) => {
    const aLayer = Number(a.layer) || 0;
    const bLayer = Number(b.layer) || 0;
    if (aLayer !== bLayer) return aLayer - bLayer;
    return (a.y - b.y) || (a.x - b.x);
  });
}

function isLowPriority(item, slideArea) {
  const areaRatio = slideArea > 0 ? computeArea(item) / slideArea : 0;

  if (item.type === "text" || item.type === "table-text") {
    const textLength = item.text ? item.text.replace(/\s+/g, "").length : 0;
    return textLength <= 3 || areaRatio < 0.02;
  }

  if (item.type === "image") {
    return areaRatio < 0.03;
  }

  return false;
}

function resolveOverlaps(items, slideHeight) {
  const minGap = 0.05;
  const sorted = [...items].sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const actions = [];

  for (let i = 0; i < sorted.length; i += 1) {
    const current = sorted[i];
    if (!isFlowElement(current)) continue;

    for (let j = 0; j < i; j += 1) {
      const previous = sorted[j];
      if (!isFlowElement(previous)) continue;
      const overlap = overlapArea(current, previous);
      if (overlap <= 0.001) continue;

      const newY = previous.y + previous.h + minGap;
      if (newY > current.y) {
        actions.push({
          type: "reposition",
          reason: "overlap",
          sourceElementId: current.sourceElementId || null,
          elementName: current.name,
          from: { x: round(current.x), y: round(current.y) },
          to: { x: round(current.x), y: round(newY) },
        });
        current.y = round(newY);
      }
    }

    if (current.y + current.h > slideHeight) {
      current.overflow = true;
    }
  }

  return actions;
}

function transformElement(item, scale, offsetX, offsetY, policy) {
  const transformed = {
    ...item,
    x: round(item.x * scale + offsetX),
    y: round(item.y * scale + offsetY),
    w: round(item.w * scale),
    h: round(item.h * scale),
  };

  if (item.type === "text" || item.type === "table-text" || item.type === "placeholder") {
    const sourceFontSize = Number.isFinite(item.fontSizePt) ? item.fontSizePt : 14;
    transformed.fontSizePt = round(
      clamp(
        sourceFontSize * scale,
        policy.readabilityMinFontPt,
        Math.max(sourceFontSize, policy.readabilityMinFontPt)
      )
    );
  }

  return transformed;
}

function splitByHeight(items, dstLayout, policy) {
  const slides = [];
  const actions = [];
  const dropped = [];
  const topPadding = 0.1;
  const bottomLimit = dstLayout.height - 0.1;
  const usableHeight = bottomLimit - topPadding;
  const slideArea = dstLayout.width * dstLayout.height;
  const sorted = [...items].sort((a, b) => (a.y - b.y) || (a.x - b.x));

  function ensureSlide(slideIndex) {
    while (slides.length <= slideIndex) slides.push([]);
  }

  for (const item of sorted) {
    const itemCopy = { ...item };

    if (itemCopy.h > dstLayout.height - 0.2) {
      if (policy.allowElementDeletion && isLowPriority(itemCopy, slideArea)) {
        dropped.push({
          sourceElementId: itemCopy.sourceElementId || null,
          elementName: itemCopy.name,
          reason: "oversized-low-priority",
        });
        continue;
      }

      const originalHeight = itemCopy.h;
      itemCopy.h = round(dstLayout.height - 0.2);
      itemCopy.y = topPadding;
      actions.push({
        type: "resize",
        reason: "oversized",
        sourceElementId: itemCopy.sourceElementId || null,
        elementName: itemCopy.name,
        from: { h: round(originalHeight) },
        to: { h: round(itemCopy.h) },
      });
    }

    if (itemCopy.y < topPadding) {
      itemCopy.y = topPadding;
    }

    const overflows = itemCopy.y + itemCopy.h > bottomLimit;
    if (!overflows) {
      ensureSlide(0);
      slides[0].push(itemCopy);
      continue;
    }

    if (!policy.allowSlideSplit) {
      if (policy.allowElementDeletion && isLowPriority(itemCopy, slideArea)) {
        dropped.push({
          sourceElementId: itemCopy.sourceElementId || null,
          elementName: itemCopy.name,
          reason: "overflow-low-priority",
        });
        continue;
      }

      itemCopy.y = round(Math.max(topPadding, bottomLimit - itemCopy.h));
      actions.push({
        type: "reposition",
        reason: "overflow-clamped",
        sourceElementId: itemCopy.sourceElementId || null,
        elementName: itemCopy.name,
        to: { y: itemCopy.y },
      });
      ensureSlide(0);
      slides[0].push(itemCopy);
      continue;
    }

    let slideIndex = Math.max(1, Math.floor((itemCopy.y - topPadding) / usableHeight));
    let rebasedY = itemCopy.y - slideIndex * usableHeight;
    while (rebasedY + itemCopy.h > bottomLimit) {
      slideIndex += 1;
      rebasedY = itemCopy.y - slideIndex * usableHeight;
      if (slideIndex > 200) break;
    }

    itemCopy.y = round(clamp(rebasedY, topPadding, bottomLimit - itemCopy.h));
    ensureSlide(slideIndex);
    slides[slideIndex].push(itemCopy);
    actions.push({
      type: "split",
      reason: "overflow",
      sourceElementId: itemCopy.sourceElementId || null,
      elementName: itemCopy.name,
    });
  }

  const compactSlides = slides.filter((segment) => segment.length > 0);
  if (compactSlides.length === 0) {
    compactSlides.push([]);
  }

  return {
    slides: compactSlides,
    actions,
    dropped,
  };
}

function buildDefaultPolicy(policy = {}) {
  return {
    allowSlideSplit: policy.allowSlideSplit !== false,
    allowElementDeletion: policy.allowElementDeletion === true,
    maxSlidesGrowthPct: Number.isFinite(policy.maxSlidesGrowthPct)
      ? Number(policy.maxSlidesGrowthPct)
      : 200,
    readabilityMinFontPt: Number.isFinite(policy.readabilityMinFontPt)
      ? Number(policy.readabilityMinFontPt)
      : 12,
    reviewThreshold: Number.isFinite(policy.reviewThreshold)
      ? Number(policy.reviewThreshold)
      : 0.78,
    strictReview: policy.strictReview !== false,
    maxOutputSlidesPerSource: Number.isFinite(policy.maxOutputSlidesPerSource)
      ? Number(policy.maxOutputSlidesPerSource)
      : 3,
    reflowPolicy: ["strict", "auto", "disabled"].includes(policy.reflowPolicy)
      ? policy.reflowPolicy
      : "strict",
    openaiTimeoutMs: Number.isFinite(policy.openaiTimeoutMs)
      ? Number(policy.openaiTimeoutMs)
      : 45000,
  };
}

function scaleToFitBox(sourceBox, targetBox) {
  const sourceAspect = sourceBox.w / Math.max(sourceBox.h, 0.0001);
  const targetAspect = targetBox.w / Math.max(targetBox.h, 0.0001);

  if (sourceAspect >= targetAspect) {
    const width = targetBox.w;
    const height = width / Math.max(sourceAspect, 0.0001);
    return {
      x: round(targetBox.x),
      y: round(targetBox.y + (targetBox.h - height) / 2),
      w: round(width),
      h: round(height),
    };
  }

  const height = targetBox.h;
  const width = height * sourceAspect;
  return {
    x: round(targetBox.x + (targetBox.w - width) / 2),
    y: round(targetBox.y),
    w: round(width),
    h: round(height),
  };
}

function getAssignmentText(sourceElement, assignment) {
  const blocks = asArray(sourceElement.blocks);
  const blockIds = asArray(assignment.blockIds);

  if (blockIds.length === 0 || blocks.length === 0) {
    return {
      text: sourceElement.text || "",
      blocks,
    };
  }

  const blockById = new Map(blocks.map((block) => [block.blockId, block]));
  const selectedBlocks = blockIds.map((blockId) => blockById.get(blockId)).filter(Boolean);
  return {
    text: selectedBlocks.map((block) => block.text).join("\n").trim(),
    blocks: selectedBlocks,
  };
}

function renderAssignmentToElement(assignment, sourceElement, slot, policy) {
  const placement =
    sourceElement.type === "image" || sourceElement.type === "shape"
      ? scaleToFitBox(sourceElement, slot)
      : { x: slot.x, y: slot.y, w: slot.w, h: slot.h };

  if (sourceElement.type === "text" || sourceElement.type === "table-text" || sourceElement.type === "placeholder") {
    const extracted = getAssignmentText(sourceElement, assignment);
    const blocks = extracted.blocks;
    const baseFontSize =
      blocks[0]?.fontSizePt || sourceElement.fontSizePt || policy.readabilityMinFontPt || 14;
    const widthScale = slot.w / Math.max(sourceElement.w, 0.0001);
    const heightScale = slot.h / Math.max(sourceElement.h, 0.0001);
    const scale = Math.min(1.25, Math.max(0.7, Math.min(widthScale, heightScale)));

    return {
      ...sourceElement,
      x: placement.x,
      y: placement.y,
      w: placement.w,
      h: placement.h,
      text: extracted.text || sourceElement.text || "",
      fontSizePt: round(
        clamp(
          baseFontSize * scale,
          policy.readabilityMinFontPt,
          Math.max(baseFontSize * 1.15, policy.readabilityMinFontPt)
        )
      ),
    };
  }

  return {
    ...sourceElement,
    x: placement.x,
    y: placement.y,
    w: placement.w,
    h: placement.h,
  };
}

function summarizeDeckContext(model) {
  return model.slides.map((slide) => {
    const textElement = slide.elements.find((element) => typeof element.text === "string" && element.text.trim());
    return {
      index: slide.index,
      preview: textElement ? textElement.text.slice(0, 120) : "",
    };
  });
}

function createSourceSlideReportBase(sourceSlide) {
  return {
    sourceSlideIndex: sourceSlide.index,
    sourceElementCount: sourceSlide.elements.length,
    masterElementCount: sourceSlide.masterElements?.length || 0,
    unsupportedCount: sourceSlide.unsupportedCount,
    recoveredGraphicCount: sourceSlide.recoveredGraphicCount || 0,
    recoveredShapeCount: sourceSlide.recoveredShapeCount || 0,
    placeholderCount: sourceSlide.placeholderCount || 0,
    outputSlideIndexes: [],
    outputSlideCount: 0,
    chosenStrategy: "preserve",
    chosenTemplates: [],
    plannerConfidence: 1,
    plannerLatencyMs: null,
    timeoutFallback: false,
    actions: [],
    repeatedElements: [],
    dropped: [],
    fallbackReason: null,
    plannerValidationWarnings: [],
    preserveScore: 1,
    reflowAllowed: false,
    reflowReason: null,
    visualRiskReasons: [],
    confidenceScore: 0,
    needsManualReview: false,
    reviewReasons: [],
  };
}

function createBackgroundElement(sourceSlide, sourceLayout, scale, offsetX, offsetY) {
  if (!sourceSlide.background?.dataUri) return null;

  return {
    type: "image",
    name: "Slide Background",
    sourceElementId: `s${sourceSlide.index}:bg`,
    x: round(offsetX),
    y: round(offsetY),
    w: round(sourceLayout.width * scale),
    h: round(sourceLayout.height * scale),
    dataUri: sourceSlide.background.dataUri,
    role: "decorative",
    layer: 0,
  };
}

function transformAnchoredElements(sourceSlide, sourceLayout, targetLayout, policy) {
  const scale = Math.min(targetLayout.width / sourceLayout.width, targetLayout.height / sourceLayout.height);
  const offsetX = (targetLayout.width - sourceLayout.width * scale) / 2;
  const offsetY = (targetLayout.height - sourceLayout.height * scale) / 2;

  const backgroundImage = createBackgroundElement(sourceSlide, sourceLayout, scale, offsetX, offsetY);
  const anchored = [
    ...(backgroundImage ? [backgroundImage] : []),
    ...asArray(sourceSlide.masterElements).map((item) => transformElement(item, scale, offsetX, offsetY, policy)),
    ...sourceSlide.elements
      .filter((item) => !isPlannableElement(item))
      .map((item) => transformElement(item, scale, offsetX, offsetY, policy)),
  ].map((item, index) => ({
    ...item,
    layer: index + 1,
  }));

  return {
    scale,
    offsetX,
    offsetY,
    background: sourceSlide.background?.color ? { color: sourceSlide.background.color } : null,
    anchored,
  };
}

function executePreservePlan(sourceSlide, sourceLayout, targetLayout, policy) {
  const anchored = transformAnchoredElements(sourceSlide, sourceLayout, targetLayout, policy);
  const plannable = sourceSlide.elements
    .filter((item) => isPlannableElement(item))
    .map((item) => transformElement(item, anchored.scale, anchored.offsetX, anchored.offsetY, policy))
    .map((item, index) => ({
      ...item,
      layer: anchored.anchored.length + index + 1,
    }));

  const overlapActions = resolveOverlaps(plannable, targetLayout.height);
  const splitResult = splitByHeight(plannable, targetLayout, policy);
  const segments = splitResult.slides.length > 0 ? splitResult.slides : [[]];

  return {
    slides: segments.map((segment) => ({
      templateId: "preserve",
      background: anchored.background,
      elements: sortElementsByLayer([...anchored.anchored, ...segment]),
    })),
    actions: [...overlapActions, ...splitResult.actions],
    dropped: splitResult.dropped,
    chosenTemplates: [],
    repeatedElements: [],
    anchoredElementsCount: anchored.anchored.length,
    backgroundPreserved: Boolean(sourceSlide.background?.color || sourceSlide.background?.dataUri),
  };
}

function executeTemplatePlan(slidePlan, sourceSlide, sourceLayout, targetLayout, policy, validation) {
  const elementById = new Map(sourceSlide.elements.map((element) => [element.sourceElementId, element]));
  const anchored = transformAnchoredElements(sourceSlide, sourceLayout, targetLayout, policy);
  const slides = [];
  const actions = [];

  for (const outputSlide of slidePlan.outputSlides) {
    const template = resolveTemplate(outputSlide.templateId, targetLayout);
    const slotById = new Map(template.slots.map((slot) => [slot.id, slot]));
    const renderedElements = [];

    for (const assignment of outputSlide.assignments) {
      const sourceElement = elementById.get(assignment.sourceElementId);
      const slot = slotById.get(assignment.slotId);
      if (!sourceElement || !slot) continue;
      renderedElements.push(renderAssignmentToElement(assignment, sourceElement, slot, policy));
    }

    const layeredContent = renderedElements.map((element, index) => ({
      ...element,
      layer: anchored.anchored.length + index + 1,
    }));
    const overlapActions = resolveOverlaps(layeredContent, targetLayout.height);
    const fitResult = splitByHeight(layeredContent, targetLayout, {
      ...policy,
      allowSlideSplit: false,
      allowElementDeletion: false,
    });

    actions.push({
      type: "template",
      reason: slidePlan.strategy,
      templateId: template.id,
    });
    actions.push(...overlapActions, ...fitResult.actions);

    slides.push({
      templateId: template.id,
      background: anchored.background,
      elements: sortElementsByLayer([...anchored.anchored, ...(fitResult.slides[0] || [])]),
    });
  }

  const dropped = validation.droppedElementIds.map((sourceElementId) => {
    const sourceElement = elementById.get(sourceElementId);
    return {
      sourceElementId,
      elementName: sourceElement?.name || sourceElementId,
      reason: "not-assigned-by-planner",
    };
  });

  return {
    slides,
    actions,
    dropped,
    chosenTemplates: slidePlan.outputSlides.map((outputSlide) => outputSlide.templateId),
    repeatedElements: validation.repeatedElementIds,
    anchoredElementsCount: anchored.anchored.length,
    backgroundPreserved: Boolean(sourceSlide.background?.color || sourceSlide.background?.dataUri),
  };
}

function assessPreserveExecution({
  sourceSlide,
  sourceLayout,
  targetLayout,
  policy,
  preserveExecution,
}) {
  const reasons = [];
  let score = 1;
  const scale = Math.min(targetLayout.width / sourceLayout.width, targetLayout.height / sourceLayout.height);
  const splitCount = preserveExecution.actions.filter((action) => action.type === "split").length;
  const overlapFixCount = preserveExecution.actions.filter(
    (action) => action.type === "reposition" && action.reason === "overlap"
  ).length;

  const lowFontCount = sourceSlide.elements
    .filter((element) => isPlannableElement(element) && (element.type === "text" || element.type === "table-text"))
    .filter((element) => (Number(element.fontSizePt) || 14) * scale < policy.readabilityMinFontPt).length;

  if (scale < 0.82) {
    score -= 0.06;
    reasons.push("major-size-change");
  }

  if (scale < 0.68) {
    score -= 0.12;
    reasons.push("aggressive-downscale");
  }

  if (lowFontCount > 0) {
    score -= Math.min(0.28, lowFontCount * 0.12);
    reasons.push("text-below-min-font");
  }

  if (splitCount > 0) {
    score -= Math.min(0.3, splitCount * 0.18);
    reasons.push("preserve-overflow-risk");
  }

  if (overlapFixCount > 0) {
    score -= Math.min(0.18, overlapFixCount * 0.05);
    reasons.push("preserve-overlap-risk");
  }

  if ((sourceSlide.unsupportedCount || 0) > 0) {
    score -= 0.08;
    reasons.push("unsupported-source-content");
  }

  if ((sourceSlide.placeholderCount || 0) > 0) {
    score -= 0.05;
    reasons.push("placeholder-source-content");
  }

  const sourceVisualStructure =
    (sourceSlide.background ? 1 : 0) +
    (sourceSlide.masterElements?.length || 0) +
    sourceSlide.elements.filter((element) => element.role === "decorative").length;
  const preserveVisualStructure =
    (preserveExecution.backgroundPreserved ? 1 : 0) + (preserveExecution.anchoredElementsCount || 0);

  if (sourceVisualStructure > 0 && preserveVisualStructure === 0) {
    score -= 0.24;
    reasons.push("decorative-structure-loss");
  }

  const preserveScore = round(clamp(score, 0, 1), 3);
  const severeRisk = reasons.some((reason) =>
    ["text-below-min-font", "preserve-overflow-risk", "preserve-overlap-risk", "decorative-structure-loss"].includes(reason)
  );

  let reflowAllowed = false;
  if (policy.reflowPolicy === "auto") {
    reflowAllowed = severeRisk || preserveScore < 0.84;
  } else if (policy.reflowPolicy === "strict") {
    reflowAllowed = severeRisk || preserveScore < 0.68;
  }

  return {
    preserveScore,
    reflowAllowed,
    reflowReason: reflowAllowed ? reasons[0] || "preserve-quality-risk" : null,
    visualRiskReasons: Array.from(new Set(reasons)),
  };
}

function computeVisualRiskReasons({
  sourceSlide,
  execution,
  chosenStrategy,
  dropped,
  preserveAssessment,
}) {
  const risks = [...asArray(preserveAssessment?.visualRiskReasons)];
  const decorativeOmissions = dropped.filter((item) => {
    const element = sourceSlide.elements.find((candidate) => candidate.sourceElementId === item.sourceElementId);
    return element?.type === "image" || element?.type === "shape";
  });

  if (chosenStrategy !== "preserve" && decorativeOmissions.length > 0) {
    risks.push("major-visual-omission");
  }

  const hasVisualChrome =
    Boolean(sourceSlide.background) ||
    (sourceSlide.masterElements?.length || 0) > 0 ||
    sourceSlide.elements.some((element) => element.role === "decorative");
  const outputHasVisualChrome =
    execution.slides.some((slide) => slide.background?.color) ||
    execution.slides.some((slide) =>
      slide.elements.some((element) => element.role === "decorative" || element.sourceElementId === `s${sourceSlide.index}:bg`)
    );

  if (hasVisualChrome && !outputHasVisualChrome) {
    risks.push("unstyled-output-risk");
  }

  return Array.from(new Set(risks));
}

function scoreSlideQuality({
  sourceSlide,
  actions,
  dropped,
  outputSlideCount,
  plannerConfidence,
  plannerWarnings,
  fallbackReason,
  preserveAssessment,
  visualRiskReasons,
  timeoutFallback,
}) {
  let score = 1;

  const splitCount = actions.filter((item) => item.type === "split").length;
  const overlapFixCount = actions.filter(
    (item) => item.type === "reposition" && item.reason === "overlap"
  ).length;
  const resizeCount = actions.filter((item) => item.type === "resize").length;

  score -= (sourceSlide.unsupportedCount || 0) * 0.12;
  score -= (sourceSlide.placeholderCount || 0) * 0.04;
  score -= dropped.length * 0.15;
  score -= splitCount * 0.18;
  score -= overlapFixCount * 0.03;
  score -= resizeCount * 0.05;
  score -= Math.max(0, outputSlideCount - 1) * 0.08;
  score -= (plannerWarnings || []).length * 0.04;
  score -= (visualRiskReasons || []).length * 0.08;
  if (fallbackReason) {
    score -= 0.08;
  }
  if (timeoutFallback) {
    score -= 0.04;
  }

  const preserveScore = Number.isFinite(preserveAssessment?.preserveScore)
    ? preserveAssessment.preserveScore
    : 1;
  const blended = score * 0.45 + clamp(Number(plannerConfidence) || 0, 0, 1) * 0.25 + preserveScore * 0.3;
  return round(clamp(blended, 0, 1), 3);
}

function buildReviewDecision({
  sourceSlide,
  actions,
  dropped,
  outputSlideIndexes,
  confidenceScore,
  policy,
  chosenStrategy,
  fallbackReason,
  plannerValidationWarnings,
  visualRiskReasons,
}) {
  const reasons = [];
  const splitCount = actions.filter((item) => item.type === "split").length;

  if (confidenceScore < policy.reviewThreshold) {
    reasons.push("low-confidence");
  }

  if (sourceSlide.unsupportedCount > 0) {
    reasons.push("unsupported-elements");
  }

  if (dropped.length > 0) {
    reasons.push("deleted-elements");
  }

  if (splitCount > 0 || chosenStrategy === "split") {
    reasons.push("slide-split");
  }

  if (outputSlideIndexes.length > 1) {
    reasons.push("multi-output-slide");
  }

  if (sourceSlide.placeholderCount > 0) {
    reasons.push("placeholder-content");
  }

  if (plannerValidationWarnings.length > 0) {
    reasons.push("planner-validation");
  }

  if (fallbackReason) {
    reasons.push("planner-fallback");
  }

  if ((visualRiskReasons || []).length > 0) {
    reasons.push("visual-risk");
  }

  const strictTrigger =
    sourceSlide.unsupportedCount > 0 ||
    dropped.length > 0 ||
    splitCount > 0 ||
    sourceSlide.placeholderCount > 0 ||
    plannerValidationWarnings.length > 0 ||
    Boolean(fallbackReason) ||
    (visualRiskReasons || []).length > 0;

  const needsManualReview = policy.strictReview
    ? confidenceScore < policy.reviewThreshold || strictTrigger
    : confidenceScore < policy.reviewThreshold;

  return {
    needsManualReview,
    reasons: Array.from(new Set(reasons)),
  };
}

async function buildConversionPlan(model, targetLayout, rawPolicy = {}) {
  const policy = buildDefaultPolicy(rawPolicy);
  const planner = rawPolicy.plannerOverride || createPlanner({
    planner: rawPolicy.planner,
    apiKey: rawPolicy.apiKey,
    model: rawPolicy.openaiModel,
    layoutGoal: rawPolicy.layoutGoal,
    timeoutMs: policy.openaiTimeoutMs,
  });
  const heuristicFallbackPlanner = createPlanner({ planner: "heuristic" });

  const plannedSlides = [];
  const sourceReports = [];
  const manualReviewQueue = [];
  const deckContext = summarizeDeckContext(model);

  for (let index = 0; index < model.slides.length; index += 1) {
    const sourceSlide = model.slides[index];
    const slideReport = createSourceSlideReportBase(sourceSlide);

    const preserveExecution = executePreservePlan(sourceSlide, model.sourceLayout, targetLayout, policy);
    const preserveAssessment = assessPreserveExecution({
      sourceSlide,
      sourceLayout: model.sourceLayout,
      targetLayout,
      policy,
      preserveExecution,
    });

    slideReport.preserveScore = preserveAssessment.preserveScore;
    slideReport.reflowAllowed = preserveAssessment.reflowAllowed;
    slideReport.reflowReason = preserveAssessment.reflowReason;

    let slidePlan = null;
    let validation = {
      valid: true,
      warnings: [],
      errors: [],
      droppedElementIds: [],
      repeatedElementIds: [],
    };
    let fallbackReason = null;
    let plannerLatencyMs = null;

    const shouldInvokePlanner =
      planner.type !== "heuristic" &&
      policy.reflowPolicy !== "disabled" &&
      preserveAssessment.reflowAllowed;

    if (shouldInvokePlanner) {
      const startedAt = Date.now();
      try {
        slidePlan = await planner.planSlide({
          model,
          sourceSlide,
          slideIndex: index,
          targetLayout,
          deckContext,
          policy,
          preserveAssessment,
        });
      } catch (error) {
        fallbackReason = `planner-error: ${error.message}`;
      } finally {
        plannerLatencyMs = Date.now() - startedAt;
      }
    }

    if (slidePlan && planner.type !== "heuristic") {
      validation = validateSlidePlan(slidePlan, sourceSlide, {
        maxOutputSlidesPerSource: policy.maxOutputSlidesPerSource,
        allowElementDeletion: policy.allowElementDeletion,
      });

      if (!validation.valid) {
        fallbackReason = `planner-validation: ${validation.errors.join(" ")}`;
        slideReport.plannerValidationWarnings = [...validation.warnings, ...validation.errors];
        slidePlan = null;
      } else {
        slideReport.plannerValidationWarnings = [...validation.warnings];
      }
    }

    if (!slidePlan) {
      if (fallbackReason) {
        slidePlan = await heuristicFallbackPlanner.planSlide({
          model,
          sourceSlide,
          slideIndex: index,
          targetLayout,
          deckContext,
          policy,
        });
      } else {
        slidePlan = {
          strategy: "preserve",
          confidence: preserveAssessment.reflowAllowed ? 0.9 : 0.96,
          reason: preserveAssessment.reflowAllowed
            ? "Planner skipped after preserve-first gate retained preserve path."
            : "Preserve-first gate kept the slide on the preserve path.",
          outputSlides: [{ templateId: "preserve", assignments: [] }],
        };
      }
    }

    const execution =
      slidePlan.strategy === "preserve"
        ? preserveExecution
        : executeTemplatePlan(
            slidePlan,
            sourceSlide,
            model.sourceLayout,
            targetLayout,
            policy,
            validation
          );

    const timeoutFallback = Boolean(fallbackReason && /timed out/i.test(fallbackReason));
    const visualRiskReasons = computeVisualRiskReasons({
      sourceSlide,
      execution,
      chosenStrategy: slidePlan.strategy,
      dropped: execution.dropped,
      preserveAssessment,
    });

    slideReport.chosenStrategy = slidePlan.strategy;
    slideReport.chosenTemplates = execution.chosenTemplates;
    slideReport.plannerConfidence = clamp(Number(slidePlan.confidence) || 0, 0, 1);
    slideReport.plannerLatencyMs = plannerLatencyMs;
    slideReport.timeoutFallback = timeoutFallback;
    slideReport.actions = execution.actions;
    slideReport.repeatedElements = execution.repeatedElements;
    slideReport.dropped = execution.dropped;
    slideReport.fallbackReason = fallbackReason;
    slideReport.visualRiskReasons = visualRiskReasons;

    const outputIndexes = [];
    for (const segment of execution.slides) {
      outputIndexes.push(plannedSlides.length + 1);
      plannedSlides.push({
        sourceSlideIndex: sourceSlide.index,
        strategy: slidePlan.strategy,
        templateId: segment.templateId,
        background: segment.background || null,
        elements: sortElementsByLayer(segment.elements),
      });
    }

    slideReport.outputSlideIndexes = outputIndexes;
    slideReport.outputSlideCount = outputIndexes.length;

    const confidenceScore = scoreSlideQuality({
      sourceSlide,
      actions: slideReport.actions,
      dropped: execution.dropped,
      outputSlideCount: outputIndexes.length,
      plannerConfidence: slideReport.plannerConfidence,
      plannerWarnings: slideReport.plannerValidationWarnings,
      fallbackReason,
      preserveAssessment,
      visualRiskReasons,
      timeoutFallback,
    });

    const review = buildReviewDecision({
      sourceSlide,
      actions: slideReport.actions,
      dropped: execution.dropped,
      outputSlideIndexes: outputIndexes,
      confidenceScore,
      policy,
      chosenStrategy: slidePlan.strategy,
      fallbackReason,
      plannerValidationWarnings: slideReport.plannerValidationWarnings,
      visualRiskReasons,
    });

    slideReport.confidenceScore = confidenceScore;
    slideReport.needsManualReview = review.needsManualReview;
    slideReport.reviewReasons = review.reasons;
    sourceReports.push(slideReport);

    if (review.needsManualReview) {
      manualReviewQueue.push({
        sourceSlideIndex: sourceSlide.index,
        outputSlideIndexes: outputIndexes,
        confidenceScore,
        strategy: slidePlan.strategy,
        reasons: review.reasons,
      });
    }
  }

  const maxSlides = Math.ceil(model.slides.length * (1 + policy.maxSlidesGrowthPct / 100));
  const maxGrowthExceeded = plannedSlides.length > maxSlides;

  return {
    sourceLayout: model.sourceLayout,
    targetLayout,
    planner: {
      type: planner.type,
      model: planner.model || null,
      layoutGoal:
        rawPolicy.layoutGoal ||
        "Preserve source content while maximizing readability for the chosen target size.",
    },
    policy,
    slides: plannedSlides,
    report: {
      sourceSlides: model.slides.length,
      outputSlides: plannedSlides.length,
      maxSlides,
      maxGrowthExceeded,
      reviewThreshold: policy.reviewThreshold,
      strictReview: policy.strictReview,
      manualReviewCount: manualReviewQueue.length,
      manualReviewQueue,
      slideReports: sourceReports,
    },
  };
}

module.exports = {
  buildConversionPlan,
  buildDefaultPolicy,
  __test: {
    assessPreserveExecution,
    executePreservePlan,
    executeTemplatePlan,
    renderAssignmentToElement,
    splitByHeight,
  },
};
