import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { after, beforeEach, test } from "node:test";
import { MAX_REQUEST_BODY_BYTES, readRequestBody, RequestBodyTooLargeError } from "../lib/request-body.ts";

const base = "https://rendafutura.test";
const encoder = new TextEncoder();
const fixture = {};
globalThis.__rfRequestBodyTest = fixture;

// Import the real route while keeping account storage and the financial service local.
const stubs = {
  "@/lib/auth": `
    const fixture = globalThis.__rfRequestBodyTest;
    export const CONSENT_VERSION = "test-consent";
    export const database = () => { fixture.databaseCalls++; return fixture.db; };
    export const currentUser = async () => fixture.user;
    export const sameOrigin = request => request.headers.get("Origin") === new URL(request.url).origin;
    export const createSession = async () => "rf-session=test; Path=/; HttpOnly";
    export const expiredCookie = () => "rf-session=; Max-Age=0; Path=/";
    export const digest = async () => "test-digest";
    export const passwordHash = async () => "test-hash";
    export const randomSecret = () => "test-salt";
    export const sessionToken = () => "test-token";
    export const throttle = async () => {};
    export const verifyPassword = async () => true;
  `,
  "@/lib/questionnaire": `
    export const validateAnswers = answers => answers;
    export const classify = () => ({status: "complete"});
  `,
  "@/lib/recommendation": `
    export const engineInput = (_answers, settings) => ({settings, input: settings});
  `,
  "@/lib/recommendation-service": `
    export const callEngine = async () => { globalThis.__rfRequestBodyTest.engineCalls++; return {status: "ready"}; };
  `,
};
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/lib/request-body") {
      return nextResolve(new URL("../lib/request-body.ts", import.meta.url).href, context);
    }
    if (specifier === "@/lib/recommendation-storage") {
      return nextResolve(new URL("../lib/recommendation-storage.ts", import.meta.url).href, context);
    }
    if (Object.hasOwn(stubs, specifier)) {
      return { url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}`, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },
});
let route;
try {
  route = await import("../app/api/[...path]/route.ts");
} finally {
  hooks.deregister();
}
after(() => { delete globalThis.__rfRequestBodyTest; });

beforeEach(() => {
  fixture.user = { id: "test-user", alias: "Test", email: "test@example.test" };
  fixture.profile = { answers: "{}", status: "complete", updated_at: "test-revision" };
  fixture.databaseCalls = 0;
  fixture.engineCalls = 0;
  fixture.db = {
    prepare(sql) {
      return {
        bind() { return this; },
        async first() {
          if (sql.includes("FROM users")) {
            return { ...fixture.user, password_hash: "test-hash", password_salt: "test-salt" };
          }
          if (sql.includes("FROM profiles")) return fixture.profile;
          return null;
        },
        async run() { return { meta: { changes: 1 } }; },
      };
    },
    async batch() { return []; },
  };
});

function streamedRequest(chunks, { path = "profile", method = "POST", headers = {}, cancel } = {}) {
  const state = { pulls: 0, cancelled: false };
  let index = 0;
  const stream = new ReadableStream({
    pull(controller) {
      state.pulls++;
      if (index === chunks.length) return controller.close();
      const chunk = chunks[index++];
      controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
    },
    cancel() { state.cancelled = true; return cancel?.(); },
  }, { highWaterMark: 0 });
  const request = new Request(`${base}/api/${path}`, {
    method, headers: { Origin: base, "Content-Type": "application/json", ...headers },
    body: stream, duplex: "half",
  });
  return { request, state, stream };
}

const headerCases = [
  ["missing length", {}],
  ["chunked", { "Transfer-Encoding": "chunked" }],
  ["zero length", { "Content-Length": "0" }],
  ["understated length", { "Content-Length": "1" }],
  ["boundary length", { "Content-Length": String(MAX_REQUEST_BODY_BYTES) }],
  ["invalid length", { "Content-Length": "invalid" }],
  ["negative length", { "Content-Length": "-1" }],
];
const jsonRoutes = [
  ["auth/register", "POST", { alias: "Test", email: "test@example.test", password: "fixture-password", consent: true }, 201],
  ["auth/login", "POST", { email: "test@example.test", password: "fixture-password" }, 200],
  ["profile", "POST", { answers: {}, consent: true, complete: true, step: 3 }, 200],
  ["recommendation", "POST", { consent: true, dataMode: "real", forecastPolicy: "xgboost_experimental" }, 200],
  ["account", "DELETE", { password: "fixture-password" }, 200],
];

test("reader preserves JSON and UTF-8 across byte boundaries", async () => {
  const value = { alias: "Jo\u00e3o \ud83d\ude00", enabled: true };
  const bytes = encoder.encode(JSON.stringify(value));
  const { request, stream } = streamedRequest(Array.from(bytes, byte => Uint8Array.of(byte)));
  assert.deepEqual(JSON.parse(await readRequestBody(request)), value);
  assert.equal(stream.locked, false);
});

test("reader accepts exactly 16 KiB and retains standard JSON/BOM handling", async () => {
  const exact = `"${"x".repeat(MAX_REQUEST_BODY_BYTES - 2)}"`;
  assert.equal(encoder.encode(exact).byteLength, MAX_REQUEST_BODY_BYTES);
  assert.equal(await readRequestBody(streamedRequest([exact]).request), exact);
  const withBom = Uint8Array.from([0xef, 0xbb, 0xbf, ...encoder.encode('{"ok":true}')]);
  const expected = await new Request(`${base}/api/profile`, { method: "POST", body: withBom }).json();
  assert.deepEqual(JSON.parse(await readRequestBody(streamedRequest([withBom]).request)), expected);
});

test("reader caps bytes rather than decoded characters", async () => {
  const text = JSON.stringify({ value: "\u00e9".repeat(9000) });
  assert.ok(text.length < MAX_REQUEST_BODY_BYTES);
  assert.ok(encoder.encode(text).byteLength > MAX_REQUEST_BODY_BYTES);
  await assert.rejects(readRequestBody(streamedRequest([text]).request), RequestBodyTooLargeError);
});

test("overflow stops reading and cancellation cannot delay rejection", { timeout: 1000 }, async () => {
  const { request, state, stream } = streamedRequest([
    new Uint8Array(MAX_REQUEST_BODY_BYTES), Uint8Array.of(0), "unread",
  ], { cancel: () => new Promise(() => {}) });
  await assert.rejects(readRequestBody(request), RequestBodyTooLargeError);
  assert.equal(state.pulls, 2);
  assert.equal(state.cancelled, true);
  assert.equal(stream.locked, false);
});

test("cancellation rejection does not replace the overflow error", async () => {
  const { request } = streamedRequest([new Uint8Array(MAX_REQUEST_BODY_BYTES + 1)], {
    cancel: () => Promise.reject(new Error("transport cancellation failed")),
  });
  await assert.rejects(readRequestBody(request), RequestBodyTooLargeError);
});

test("all JSON APIs reject oversized actual bytes regardless of length headers", async t => {
  for (const [path, method] of jsonRoutes) {
    for (const [label, headers] of headerCases) {
      await t.test(`${method} ${path}: ${label}`, async () => {
        const { request, state } = streamedRequest([
          new Uint8Array(MAX_REQUEST_BODY_BYTES), Uint8Array.of(0), "unread",
        ], { path, method, headers });
        const response = await route[method](request);
        assert.equal(response.status, 413);
        assert.deepEqual(await response.json(), { error: "Solicita\u00e7\u00e3o muito grande." });
        assert.equal(response.headers.get("Cache-Control"), "no-store");
        assert.equal(state.cancelled, true);
        assert.equal(state.pulls, 2);
        assert.equal(fixture.databaseCalls, 0);
      });
    }
  }
});

test("all JSON APIs accept valid payloads and retain cookies/status at the exact cap", async t => {
  for (const [path, method, input, status] of jsonRoutes) {
    await t.test(`${method} ${path}`, async () => {
      const json = JSON.stringify(input);
      const padded = json + " ".repeat(MAX_REQUEST_BODY_BYTES - encoder.encode(json).byteLength);
      const { request } = streamedRequest([padded], { path, method, headers: { "Content-Length": "1" } });
      const response = await route[method](request);
      assert.equal(response.status, status);
      assert.equal(response.headers.get("Cache-Control"), "no-store");
      const result = await response.json();
      if (path.startsWith("auth/")) {
        if (path === "auth/register") {
          assert.match(result.user.id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
          assert.equal(result.user.alias, input.alias);
          assert.equal(result.user.email, input.email);
        } else {
          assert.deepEqual(result.user, fixture.user);
        }
        assert.match(response.headers.get("Set-Cookie"), /rf-session=test/);
      } else if (path === "recommendation") {
        assert.deepEqual(result.recommendation.settings, input);
        assert.deepEqual(result.recommendation.result, { status: "ready" });
      } else {
        assert.equal(result.ok, true);
        if (path === "account") assert.match(response.headers.get("Set-Cookie"), /Max-Age=0/);
      }
    });
  }
});

test("malformed JSON still returns 400 for every JSON API", async t => {
  for (const [path, method] of jsonRoutes) {
    await t.test(`${method} ${path}`, async () => {
      const response = await route[method](streamedRequest(["{"], { path, method }).request);
      assert.equal(response.status, 400);
      assert.equal(typeof (await response.json()).error, "string");
    });
  }
});

test("origin and advertised-size rejection retain their early ordering", async () => {
  const forbidden = streamedRequest([new Uint8Array(MAX_REQUEST_BODY_BYTES + 1)], {
    headers: { Origin: "https://untrusted.test", "Content-Length": "99999" },
  });
  assert.equal((await route.POST(forbidden.request)).status, 403);
  assert.equal(forbidden.state.pulls, 0);
  const advertised = streamedRequest(["{}"], { headers: { "Content-Length": "99999" } });
  assert.equal((await route.POST(advertised.request)).status, 413);
  assert.equal(advertised.state.pulls, 0);
  assert.equal(fixture.databaseCalls, 0);
});

test("authentication and profile checks still precede JSON parsing", async () => {
  fixture.user = null;
  assert.equal((await route.POST(streamedRequest(["{"]).request)).status, 401);
  fixture.user = { id: "test-user" };
  fixture.profile = null;
  const { request } = streamedRequest(["{"], { path: "recommendation" });
  assert.equal((await route.POST(request)).status, 409);
});

test("bodyless GET and endpoints that ignore JSON retain their responses", async () => {
  assert.equal((await route.GET(new Request(`${base}/api/me`))).status, 200);
  for (const [path, method] of [["auth/logout", "POST"], ["profile", "DELETE"], ["recommendation", "DELETE"]]) {
    const response = await route[method](streamedRequest(["{"], { path, method }).request);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true });
  }
  assert.equal((await route.POST(streamedRequest(["{"], { path: "unknown" }).request)).status, 404);
});

test("the web rejects fictitious data and alternative forecasting before calling the engine", async () => {
  for(const settings of [
    {dataMode:"demo",forecastPolicy:"xgboost_experimental",consent:true},
    {dataMode:"real",forecastPolicy:"validation",consent:true},
  ]) {
    const response=await route.POST(streamedRequest([JSON.stringify(settings)],{path:"recommendation"}).request);
    assert.equal(response.status,400);
    assert.match((await response.json()).error,/somente dados reais e previsão XGBoost/);
    assert.equal(fixture.engineCalls,0);
  }
});
