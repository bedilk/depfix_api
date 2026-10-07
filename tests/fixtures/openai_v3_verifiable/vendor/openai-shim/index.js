"use strict";

// Minimal, local stand-in for the "openai" SDK -- packaged as a `file:`
// dependency (see ../../package.json) rather than a bare relative import,
// so this fixture is imported the same way a real repo imports the real
// "openai" package (`require("openai")`) and depfix's call-site scanner
// can actually bind it -- a relative `require("./openai-shim")` is
// invisible to the scanner's import-binding tracker, which only
// recognizes bindings whose import spec matches a provider's declared
// `sdk_packages` name. No real npm package, no network access: `npm
// install` resolves this via a local `file:` path.
//
// Unlike a real SDK upgrade (which drops the old method), this shim
// exposes both the v3-shaped `createModeration` and the v4-shaped
// `moderations.create` on the same client instance, so this one fixture
// can exercise both outcomes Verifier settles edits to -- "fix migrates
// the call site correctly" (KEPT) and "fix is wrong" (REVERTED) -- by
// varying what src/chat.js calls, without needing two fixture trees.

class Moderations {
  async create({ input } = {}) {
    if (typeof input !== "string" || input.length === 0) {
      throw new TypeError("moderations.create: `input` must be a non-empty string");
    }
    return { results: [{ flagged: false, input }] };
  }
}

class OpenAI {
  constructor(opts = {}) {
    this.apiKey = opts.apiKey;
    this.moderations = new Moderations();
  }

  async createModeration({ input } = {}) {
    if (typeof input !== "string" || input.length === 0) {
      throw new TypeError("createModeration: `input` must be a non-empty string");
    }
    return { data: { results: [{ flagged: false, input }] } };
  }
}

module.exports = { OpenAI };
