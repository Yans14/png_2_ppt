function round(value, precision = 4) {
  const factor = 10 ** precision;
  return Math.round(value * factor) / factor;
}

function rect(x, y, w, h) {
  return {
    x: round(x),
    y: round(y),
    w: round(w),
    h: round(h),
  };
}

function contentBox(layout) {
  const margins = layout.safeMargins || { top: 0.4, right: 0.4, bottom: 0.4, left: 0.4 };
  return rect(
    margins.left,
    margins.top,
    layout.width - margins.left - margins.right,
    layout.height - margins.top - margins.bottom
  );
}

function buildSlot(id, kind, box) {
  return { id, kind, ...box };
}

function withGap(total, ratio, gap) {
  const first = total * ratio - gap / 2;
  const second = total - first - gap;
  return [first, second];
}

function resolveTitleBody(layout, titleRatio = 0.2, bodyTopGap = 0.12) {
  const box = contentBox(layout);
  const titleHeight = Math.max(0.75, box.h * titleRatio);
  const bodyY = box.y + titleHeight + bodyTopGap;
  const bodyHeight = Math.max(0.7, box.h - titleHeight - bodyTopGap);

  return [
    buildSlot("title", "title", rect(box.x, box.y, box.w, titleHeight)),
    buildSlot("body", "body", rect(box.x, bodyY, box.w, bodyHeight)),
  ];
}

function resolveTitleTwoColumn(layout) {
  const box = contentBox(layout);
  const titleHeight = Math.max(0.75, box.h * 0.18);
  const accentHeight = Math.max(0.08, Math.min(0.14, box.h * 0.025));
  const gap = Math.max(0.18, box.w * 0.025);
  const bodyY = box.y + titleHeight + 0.12;
  const bodyHeight = Math.max(0.7, box.h - titleHeight - accentHeight - 0.22);

  if (layout.sizeClass === "portrait") {
    const sectionGap = 0.16;
    const halfHeight = (bodyHeight - sectionGap) / 2;
    return [
      buildSlot("title", "title", rect(box.x, box.y, box.w, titleHeight)),
      buildSlot("accent", "accent", rect(box.x, bodyY - 0.07, box.w, accentHeight)),
      buildSlot("body", "body", rect(box.x, bodyY, box.w, halfHeight)),
      buildSlot(
        "body_secondary",
        "body_secondary",
        rect(box.x, bodyY + halfHeight + sectionGap, box.w, halfHeight)
      ),
    ];
  }

  const [leftWidth, rightWidth] = withGap(box.w, 0.5, gap);
  return [
    buildSlot("title", "title", rect(box.x, box.y, box.w, titleHeight)),
    buildSlot("accent", "accent", rect(box.x, bodyY - 0.07, box.w, accentHeight)),
    buildSlot("body", "body", rect(box.x, bodyY, leftWidth, bodyHeight)),
    buildSlot(
      "body_secondary",
      "body_secondary",
      rect(box.x + leftWidth + gap, bodyY, rightWidth, bodyHeight)
    ),
  ];
}

function resolveImageText(layout, mediaOnLeft) {
  const box = contentBox(layout);

  if (layout.sizeClass === "portrait") {
    const gap = 0.18;
    const titleHeight = Math.max(0.7, box.h * 0.14);
    const mediaHeight = Math.max(1.8, box.h * 0.42);
    const bodyY = box.y + titleHeight + mediaHeight + gap * 2;
    const bodyHeight = Math.max(0.7, box.h - titleHeight - mediaHeight - gap * 2);

    return [
      buildSlot("title", "title", rect(box.x, box.y, box.w, titleHeight)),
      buildSlot("media_primary", "media_primary", rect(box.x, box.y + titleHeight + gap, box.w, mediaHeight)),
      buildSlot("body", "body", rect(box.x, bodyY, box.w, bodyHeight)),
    ];
  }

  const gap = Math.max(0.18, box.w * 0.025);
  const [mediaWidth, textWidth] = withGap(box.w, 0.47, gap);
  const mediaX = mediaOnLeft ? box.x : box.x + textWidth + gap;
  const textX = mediaOnLeft ? box.x + mediaWidth + gap : box.x;
  const titleHeight = Math.max(0.7, box.h * 0.16);
  const bodyY = box.y + titleHeight + 0.12;
  const bodyHeight = Math.max(0.7, box.h - titleHeight - 0.12);

  return [
    buildSlot("media_primary", "media_primary", rect(mediaX, box.y, mediaWidth, box.h)),
    buildSlot("title", "title", rect(textX, box.y, textWidth, titleHeight)),
    buildSlot("body", "body", rect(textX, bodyY, textWidth, bodyHeight)),
  ];
}

function resolveTitleOnly(layout) {
  const box = contentBox(layout);
  const titleHeight = Math.max(1.1, Math.min(box.h * 0.45, box.h - 0.2));
  const titleY = box.y + Math.max(0, (box.h - titleHeight) / 2);
  return [buildSlot("title", "title", rect(box.x, titleY, box.w, titleHeight))];
}

function resolveComparison(layout) {
  return resolveTitleTwoColumn(layout);
}

function resolveContinuation(layout) {
  return resolveTitleBody(layout, 0.12, 0.1);
}

const TEMPLATE_DEFINITIONS = {
  title_only: {
    id: "title_only",
    supportedSizeClasses: ["landscape", "portrait", "square"],
    slots: [{ id: "title", kind: "title" }],
    resolve: resolveTitleOnly,
  },
  title_body: {
    id: "title_body",
    supportedSizeClasses: ["landscape", "portrait", "square"],
    slots: [
      { id: "title", kind: "title" },
      { id: "body", kind: "body" },
    ],
    resolve: (layout) => resolveTitleBody(layout, 0.2, 0.12),
  },
  title_two_column: {
    id: "title_two_column",
    supportedSizeClasses: ["landscape", "portrait", "square"],
    slots: [
      { id: "title", kind: "title" },
      { id: "accent", kind: "accent" },
      { id: "body", kind: "body" },
      { id: "body_secondary", kind: "body_secondary" },
    ],
    resolve: resolveTitleTwoColumn,
  },
  image_left_text_right: {
    id: "image_left_text_right",
    supportedSizeClasses: ["landscape", "portrait", "square"],
    slots: [
      { id: "media_primary", kind: "media_primary" },
      { id: "title", kind: "title" },
      { id: "body", kind: "body" },
    ],
    resolve: (layout) => resolveImageText(layout, true),
  },
  image_right_text_left: {
    id: "image_right_text_left",
    supportedSizeClasses: ["landscape", "portrait", "square"],
    slots: [
      { id: "media_primary", kind: "media_primary" },
      { id: "title", kind: "title" },
      { id: "body", kind: "body" },
    ],
    resolve: (layout) => resolveImageText(layout, false),
  },
  comparison_2up: {
    id: "comparison_2up",
    supportedSizeClasses: ["landscape", "portrait", "square"],
    slots: [
      { id: "title", kind: "title" },
      { id: "accent", kind: "accent" },
      { id: "body", kind: "body" },
      { id: "body_secondary", kind: "body_secondary" },
    ],
    resolve: resolveComparison,
  },
  continuation_list: {
    id: "continuation_list",
    supportedSizeClasses: ["landscape", "portrait", "square"],
    slots: [
      { id: "title", kind: "title" },
      { id: "body", kind: "body" },
    ],
    resolve: resolveContinuation,
  },
};

function getTemplateDefinition(templateId) {
  return TEMPLATE_DEFINITIONS[templateId] || null;
}

function getTemplateCatalog() {
  return Object.values(TEMPLATE_DEFINITIONS).map((template) => ({
    id: template.id,
    supportedSizeClasses: [...template.supportedSizeClasses],
    slots: template.slots.map((slot) => ({ ...slot })),
  }));
}

function resolveTemplate(templateId, layout) {
  const template = getTemplateDefinition(templateId);
  if (!template) {
    throw new Error(`Unknown template "${templateId}"`);
  }

  const resolvedSlots = template.resolve(layout).map((slot) => ({
    ...slot,
    x: round(slot.x),
    y: round(slot.y),
    w: round(slot.w),
    h: round(slot.h),
  }));

  return {
    id: template.id,
    supportedSizeClasses: [...template.supportedSizeClasses],
    slots: resolvedSlots,
  };
}

module.exports = {
  TEMPLATE_DEFINITIONS,
  getTemplateCatalog,
  getTemplateDefinition,
  resolveTemplate,
};
