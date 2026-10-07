import { openaiClient } from "./client";

export async function moderateInput(input: string): Promise<boolean> {
  const result = await openaiClient.moderations.create({ input });
  return result.results[0].flagged;
}
