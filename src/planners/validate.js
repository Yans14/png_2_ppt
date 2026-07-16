const { getTemplateDefinition } = require("../templates");

const SLOT_KIND_COMPATIBILITY = {
  title: new Set(["text", "table-text", "placeholder"]),
  body: new Set(["text", "table-text", "placeholder"]),
  body_secondary: new Set(["text", "table-text", "placeholder"]),
  media_primary: new Set(["image", "shape", "placeholder"]),
  accent: new Set(["shape", "image", "placeholder", "text", "table-text"]),
};

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function getPlannableElements(sourceSlide) {
  return asArray(sourceSlide.elements).filter((element) => element.role === "content");
}

function validateSlidePlan(slidePlan, sourceSlide, options = {}) {
  const maxOutputSlidesPerSource = Number(options.maxOutputSlidesPerSource) || 3;
  const allowElementDeletion = options.allowElementDeletion === true;
  const warnings = [];
  const errors = [];
  const repeatedElementIds = new Set();

  if (!slidePlan || typeof slidePlan !== "object") {
    return {
      valid: false,
      warnings,
      errors: ["Planner returned an empty slide plan."],
      droppedElementIds: getPlannableElements(sourceSlide).map((element) => element.sourceElementId),
      repeatedElementIds: [],
    };
  }

  const strategy = slidePlan.strategy;
  if (!["preserve", "reflow", "split"].includes(strategy)) {
    errors.push(`Unknown strategy "${strategy}".`);
  }

  const outputSlides = asArray(slidePlan.outputSlides);
  if (outputSlides.length < 1) {
    errors.push("Slide plan must contain at least one output slide.");
  }
  if (outputSlides.length > maxOutputSlidesPerSource) {
    errors.push(
      `Slide plan exceeds max output slides per source (${outputSlides.length}/${maxOutputSlidesPerSource}).`
    );
  }
  if (strategy === "reflow" && outputSlides.length !== 1) {
    errors.push("Reflow strategy must produce exactly one output slide.");
  }
  if (strategy === "split" && outputSlides.length < 2) {
    errors.push("Split strategy must produce at least two output slides.");
  }
  if (strategy === "preserve" && outputSlides.length !== 1) {
    errors.push("Preserve strategy must produce exactly one output slide.");
  }

  const elementById = new Map();
  const plannableElements = getPlannableElements(sourceSlide);
  for (const element of plannableElements) {
    elementById.set(element.sourceElementId, element);
  }

  const assignedElements = new Set();
  const fullyAssignedElements = new Set();
  const assignedBlockIds = new Set();
  const usageCounts = new Map();

  for (const outputSlide of outputSlides) {
    const templateId = outputSlide?.templateId;
    if (strategy !== "preserve") {
      const template = getTemplateDefinition(templateId);
      if (!template) {
        errors.push(`Unknown template "${templateId}".`);
        continue;
      }

      const slotById = new Map(template.slots.map((slot) => [slot.id, slot]));
      const assignments = asArray(outputSlide.assignments);
      if (assignments.length === 0) {
        errors.push(`Template "${templateId}" is missing assignments.`);
      }

      for (const assignment of assignments) {
        const sourceElement = elementById.get(assignment?.sourceElementId);
        if (!sourceElement) {
          errors.push(`Unknown source element "${assignment?.sourceElementId}".`);
          continue;
        }

        const slot = slotById.get(assignment.slotId);
        if (!slot) {
          errors.push(`Unknown slot "${assignment.slotId}" for template "${templateId}".`);
          continue;
        }

        if (!SLOT_KIND_COMPATIBILITY[slot.kind]?.has(sourceElement.type)) {
          errors.push(
            `Element "${sourceElement.sourceElementId}" of type "${sourceElement.type}" cannot be assigned to slot "${slot.id}" (${slot.kind}).`
          );
          continue;
        }

        const uses = usageCounts.get(sourceElement.sourceElementId) || 0;
        usageCounts.set(sourceElement.sourceElementId, uses + 1);
        if (uses > 0) {
          if (assignment.repeat !== true) {
            errors.push(`Element "${sourceElement.sourceElementId}" is reused without repeat=true.`);
          } else {
            repeatedElementIds.add(sourceElement.sourceElementId);
          }
        }
        assignedElements.add(sourceElement.sourceElementId);

        const requestedBlockIds = asArray(assignment.blockIds);
        if (requestedBlockIds.length > 0) {
          const sourceBlocks = new Map(asArray(sourceElement.blocks).map((block) => [block.blockId, block]));
          if (sourceBlocks.size === 0) {
            errors.push(`Element "${sourceElement.sourceElementId}" does not expose content blocks.`);
            continue;
          }

          for (const blockId of requestedBlockIds) {
            if (!sourceBlocks.has(blockId)) {
              errors.push(`Unknown block "${blockId}" for element "${sourceElement.sourceElementId}".`);
              continue;
            }

            if (assignedBlockIds.has(blockId) && assignment.repeat !== true) {
              errors.push(`Block "${blockId}" is reused without repeat=true.`);
            }
            assignedBlockIds.add(blockId);
          }
        } else {
          fullyAssignedElements.add(sourceElement.sourceElementId);
        }
      }
    }
  }

  if (strategy !== "preserve") {
    for (const element of plannableElements) {
      const blocks = asArray(element.blocks);
      if (blocks.length > 0) {
        for (const block of blocks) {
          if (
            !assignedBlockIds.has(block.blockId) &&
            !fullyAssignedElements.has(element.sourceElementId)
          ) {
            errors.push(
              `Text-bearing element "${element.sourceElementId}" is missing block "${block.blockId}" in the plan.`
            );
          }
        }
      }
    }
  }

  const droppedElementIds = sourceSlide.elements
    .filter((element) => strategy !== "preserve" && element.role === "content" && !assignedElements.has(element.sourceElementId))
    .map((element) => element.sourceElementId);

  if (droppedElementIds.length > 0) {
    warnings.push(`Planner omitted ${droppedElementIds.length} source element(s).`);
    if (!allowElementDeletion) {
      warnings.push("Element deletion is disabled; omitted elements require manual review.");
    }
  }

  return {
    valid: errors.length === 0,
    warnings,
    errors,
    droppedElementIds,
    repeatedElementIds: Array.from(repeatedElementIds),
  };
}

module.exports = {
  SLOT_KIND_COMPATIBILITY,
  validateSlidePlan,
};
