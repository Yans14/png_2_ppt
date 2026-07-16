function createHeuristicPlanner() {
  return {
    type: "heuristic",
    async planSlide() {
      return {
        strategy: "preserve",
        confidence: 0.95,
        reason: "Geometry-preserving conversion is sufficient for the default planner.",
        outputSlides: [
          {
            templateId: "preserve",
            assignments: [],
          },
        ],
      };
    },
  };
}

module.exports = {
  createHeuristicPlanner,
};
