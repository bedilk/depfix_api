#!/usr/bin/env bash
# Populate the controlled multi-SDK fixture after creating the empty GitHub repo.
set -euo pipefail

repo="bedilk/seed-multi-sdk"
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

echo "==> Cloning $repo into $workdir"
git clone "https://github.com/$repo.git" "$workdir/repo" 2>/dev/null || {
  echo "ERROR: $repo must exist as an empty repository before bootstrapping."
  exit 1
}

cd "$workdir/repo"

mkdir -p src test

cat > package.json <<'PACKAGE_EOF'
{
  "name": "seed-multi-sdk",
  "version": "1.0.0",
  "private": true,
  "description": "Seed repo exercising multi-SDK migrations (Stripe + OpenAI)",
  "scripts": { "test": "node --test test/" },
  "dependencies": { "stripe": "^12.0.0", "openai": "^3.3.0" }
}
PACKAGE_EOF

cat > src/payments.js <<'SOURCE_EOF'
"use strict";

const stripe = require("stripe")("sk_test_fake");

async function chargeCustomer(customerId, amount) {
  const charge = await stripe.charges.create({
    amount,
    currency: "usd",
    customer: customerId,
    description: "Seed multi-SDK charge",
  });
  return charge;
}

async function chargeWithSource(token, amount) {
  const charge = await stripe.charges.create({ amount, currency: "usd", source: token });
  return charge.id;
}

module.exports = { chargeCustomer, chargeWithSource };
SOURCE_EOF

cat > src/chat.js <<'SOURCE_EOF'
"use strict";

const { Configuration, OpenAIApi } = require("openai");

const openai = new OpenAIApi(new Configuration({
  apiKey: process.env.OPENAI_API_KEY || "sk-fake",
}));

async function askQuestion(question) {
  const response = await openai.createChatCompletion({
    model: "gpt-3.5-turbo",
    messages: [{ role: "user", content: question }],
  });
  return response.data.choices[0].message.content;
}

async function moderate(text) {
  const response = await openai.createModeration({ input: text });
  return response.data.results[0];
}

module.exports = { askQuestion, moderate };
SOURCE_EOF

cat > src/index.js <<'SOURCE_EOF'
"use strict";

const { chargeCustomer } = require("./payments");
const { askQuestion, moderate } = require("./chat");

async function processOrder(customerId, amount, question) {
  const charge = await chargeCustomer(customerId, amount);
  const answer = await askQuestion(question);
  const moderation = await moderate(answer);
  return { chargeId: charge.id, answer, flagged: moderation.flagged };
}

module.exports = { processOrder };
SOURCE_EOF

cat > test/payments.test.js <<'TEST_EOF'
const test = require("node:test");
const assert = require("node:assert");

test("payment functions are exported", () => {
  const { chargeCustomer, chargeWithSource } = require("../src/payments");
  assert.strictEqual(typeof chargeCustomer, "function");
  assert.strictEqual(typeof chargeWithSource, "function");
});
TEST_EOF

cat > test/chat.test.js <<'TEST_EOF'
const test = require("node:test");
const assert = require("node:assert");

test("chat functions are exported", () => {
  const { askQuestion, moderate } = require("../src/chat");
  assert.strictEqual(typeof askQuestion, "function");
  assert.strictEqual(typeof moderate, "function");
});
TEST_EOF

cat > .depfix.yml <<'CONFIG_EOF'
version: 1
enabled: true
verify: true
open_pr: true
CONFIG_EOF

git add package.json src test .depfix.yml
git commit -m "seed: multi-SDK fixture with Stripe charges and OpenAI v3 calls"
git push -u origin main

echo "==> Done. Run depfix watch, classify, then plan against $repo."
