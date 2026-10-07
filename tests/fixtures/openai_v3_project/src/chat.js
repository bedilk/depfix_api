const { openai } = require("./client");

async function chatReply(messages) {
  const response = await openai.createChatCompletion({
    model: "gpt-3.5-turbo",
    messages,
  });
  return response.data.choices[0].message.content;
}

async function moderateMessage(input) {
  const response = await openai.createModeration({
    input,
  });
  return response.data.results[0];
}

module.exports = { chatReply, moderateMessage };
