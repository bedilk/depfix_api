"use strict";

const { openai } = require("./client");

// v3 API -- depfix's fixer is expected to migrate this call site to
// `openai.moderations.create(...)`. Left unmigrated here, matching
// openai_v3_project's convention: fixtures start pre-fix.
async function moderateMessage(input) {
  const response = await openai.createModeration({ input });
  return response.data.results[0];
}

module.exports = { moderateMessage };
