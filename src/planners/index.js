const { createHeuristicPlanner } = require("./heuristic");
const { createOpenAIPlanner } = require("./openai");

function createPlanner(options = {}) {
  const plannerType = String(options.planner || "heuristic").toLowerCase();

  if (plannerType === "heuristic") {
    return createHeuristicPlanner(options);
  }

  if (plannerType === "openai") {
    return createOpenAIPlanner(options);
  }

  throw new Error(`Unknown planner "${options.planner}". Valid planners: heuristic, openai`);
}

module.exports = {
  createPlanner,
};
