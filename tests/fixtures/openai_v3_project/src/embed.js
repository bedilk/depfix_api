const { openai } = require("./client");

async function embedText(text) {
  const response = await openai.createEmbedding({
    model: "text-embedding-ada-002",
    input: text,
  });
  return response.data.data[0].embedding;
}

module.exports = { embedText };
