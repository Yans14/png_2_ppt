const PAGE_SIZE_REGISTRY = {
  wide: { id: "wide", name: "WIDE_16_9", width: 13.333, height: 7.5 },
  standard: { id: "standard", name: "STANDARD_4_3", width: 10, height: 7.5 },
  a4: { id: "a4", name: "A4_LANDSCAPE", width: 11.693, height: 8.268 },
  "a4-portrait": { id: "a4-portrait", name: "A4_PORTRAIT", width: 8.268, height: 11.693 },
};

function round(value, precision = 4) {
  const factor = 10 ** precision;
  return Math.round(value * factor) / factor;
}

function inferSizeClass(width, height) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return "landscape";
  }

  const ratio = width / height;
  if (ratio >= 0.88 && ratio <= 1.12) {
    return "square";
  }

  return width >= height ? "landscape" : "portrait";
}

function getDefaultSafeMargins(sizeClass, width, height) {
  const shortSide = Math.min(width, height);
  const base = Math.max(0.3, Math.min(0.6, shortSide * 0.06));

  if (sizeClass === "portrait") {
    return {
      top: round(base + 0.08),
      right: round(base - 0.04),
      bottom: round(base + 0.08),
      left: round(base - 0.04),
    };
  }

  if (sizeClass === "square") {
    return {
      top: round(base),
      right: round(base),
      bottom: round(base),
      left: round(base),
    };
  }

  return {
    top: round(base),
    right: round(base + 0.05),
    bottom: round(base),
    left: round(base + 0.05),
  };
}

function normalizeLayout(layout = {}, overrides = {}) {
  const width = Number(overrides.width ?? layout.width);
  const height = Number(overrides.height ?? layout.height);

  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error("Invalid layout size. Width and height must be positive numbers.");
  }

  const id = overrides.id || layout.id || "custom";
  const name = overrides.name || layout.name || "CUSTOM";
  const sizeClass = overrides.sizeClass || inferSizeClass(width, height);
  const safeMargins =
    overrides.safeMargins ||
    layout.safeMargins ||
    getDefaultSafeMargins(sizeClass, width, height);

  return {
    id,
    name,
    width: round(width),
    height: round(height),
    sizeClass,
    safeMargins: {
      top: round(Number(safeMargins.top) || 0),
      right: round(Number(safeMargins.right) || 0),
      bottom: round(Number(safeMargins.bottom) || 0),
      left: round(Number(safeMargins.left) || 0),
    },
    isCustom: Boolean(overrides.isCustom ?? layout.isCustom ?? id === "custom"),
  };
}

function getPageSizeRegistry() {
  return Object.values(PAGE_SIZE_REGISTRY).map((layout) => normalizeLayout(layout));
}

function getTargetLayout(options = {}) {
  if (options.targetWidth && options.targetHeight) {
    return normalizeLayout(
      {
        id: "custom",
        name: "CUSTOM",
        width: Number(options.targetWidth),
        height: Number(options.targetHeight),
      },
      { isCustom: true }
    );
  }

  const preset = String(options.target || "wide").toLowerCase();
  const layout = PAGE_SIZE_REGISTRY[preset];
  if (!layout) {
    throw new Error(
      `Unknown target layout "${options.target}". Valid presets: ${Object.keys(PAGE_SIZE_REGISTRY).join(", ")}`
    );
  }

  return normalizeLayout(layout);
}

module.exports = {
  PAGE_SIZE_REGISTRY,
  getPageSizeRegistry,
  getTargetLayout,
  getDefaultSafeMargins,
  inferSizeClass,
  normalizeLayout,
};
