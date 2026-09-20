export interface CallbackOptions {
  url: string;
  token: string;
  body: object;
  attempts?: number;
  initialDelayMs?: number;
  fetchImplementation?: typeof fetch;
}

export async function postCallback(options: CallbackOptions): Promise<void> {
  const attempts = options.attempts ?? 4;
  const initialDelayMs = options.initialDelayMs ?? 250;
  const request = options.fetchImplementation ?? fetch;
  let lastError: Error | undefined;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await request(options.url, {
        method: "POST",
        headers: {
          authorization: `Bearer ${options.token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(options.body),
        signal: AbortSignal.timeout(10_000),
      });
      if (response.ok) return;
      if (response.status < 500 && response.status !== 429) {
        throw new PermanentCallbackError(`callback was rejected with status ${response.status}`);
      }
      lastError = new Error(`callback returned retryable status ${response.status}`);
    } catch (error) {
      if (error instanceof PermanentCallbackError) throw error;
      lastError = error instanceof Error ? error : new Error("callback request failed");
    }
    if (attempt < attempts) await delay(initialDelayMs * 2 ** (attempt - 1));
  }
  throw new Error(`callback failed after ${attempts} attempts: ${lastError?.message ?? "unknown error"}`);
}

class PermanentCallbackError extends Error {}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

