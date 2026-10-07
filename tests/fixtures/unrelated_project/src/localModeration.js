// A homegrown moderation stub -- deliberately named to look like the real
// openai-node client, but never imports the "openai" package at all.
class LocalModerationClient {
  createModeration(input) {
    return { flagged: false, input };
  }
}

const openai = new LocalModerationClient();

module.exports = { openai };
