const { openai } = require("./client");
const { chatReply, moderateMessage } = require("./chat");
const { embedText } = require("./embed");

async function legacyComplete(prompt) {
  const response = await openai.createCompletion({
    model: "text-davinci-003",
    prompt,
    max_tokens: 64,
  });
  return response.data.choices[0].text;
}

async function generateImage(prompt) {
  const response = await openai.createImage({
    prompt,
    n: 1,
    size: "512x512",
  });
  return response.data.data[0].url;
}

module.exports = { chatReply, moderateMessage, embedText, legacyComplete, generateImage };
