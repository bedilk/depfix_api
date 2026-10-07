"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { moderateMessage } = require("./chat");

test("moderateMessage flags nothing for benign input", async () => {
  const result = await moderateMessage("hello there");
  assert.equal(result.flagged, false);
});
