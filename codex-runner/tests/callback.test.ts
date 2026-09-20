import assert from "node:assert/strict";
import test from "node:test";

import { postCallback } from "../src/callback.js";

test("callback retries 5xx and 429 responses", async () => {
  const statuses = [500, 429, 200];
  let requests = 0;
  await postCallback({
    url: "https://gateway.example.test/internal/event",
    token: "runner-token",
    body: { status: "PASSED" },
    attempts: 4,
    initialDelayMs: 1,
    fetchImplementation: async () => {
      const status = statuses[requests++];
      if (status === undefined) throw new Error("test status sequence exhausted");
      return new Response(null, { status });
    },
  });
  assert.equal(requests, 3);
});

test("callback does not retry a non-429 4xx response", async () => {
  let requests = 0;
  await assert.rejects(
    postCallback({
      url: "https://gateway.example.test/internal/event",
      token: "runner-token",
      body: {},
      attempts: 4,
      initialDelayMs: 1,
      fetchImplementation: async () => {
        requests += 1;
        return new Response(null, { status: 400 });
      },
    }),
    /rejected with status 400/u,
  );
  assert.equal(requests, 1);
});
