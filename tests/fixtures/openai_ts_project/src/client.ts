import type { ClientOptions } from "openai";
import OpenAI from "openai";

const options: ClientOptions = {
  apiKey: process.env.OPENAI_API_KEY,
};

export const openaiClient = new OpenAI(options);
