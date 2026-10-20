import { describe, expect, it } from "vite-plus/test";
import { createApiClient, parseResponse, unwrap } from "../client";
import { ApiError } from "../errors";
import { z } from "zod";

function clientReturning(respond: () => Response | Promise<Response>) {
  return createApiClient({ baseUrl: "http://api.test", fetch: () => Promise.resolve(respond()) });
}

async function failureOf(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (thrown) {
    if (thrown instanceof ApiError) return thrown;
    throw thrown;
  }
  throw new Error("expected the request to fail");
}

describe("unwrap", () => {
  it("returns the typed body on success", async () => {
    const api = clientReturning(() => Response.json({ indices: [] }));
    await expect(unwrap(api.GET("/api/screener/indices"))).resolves.toEqual({ indices: [] });
  });

  it("normalises the backend error envelope", async () => {
    const api = clientReturning(() =>
      Response.json(
        { error: { code: "DATA_NOT_FOUND", message: "No such run" } },
        { status: 404, statusText: "Not Found" },
      ),
    );
    const error = await failureOf(unwrap(api.GET("/api/screener/indices")));
    expect(error).toMatchObject({
      name: "ApiError",
      code: "DATA_NOT_FOUND",
      message: "No such run",
      status: 404,
    });
  });

  it("normalises FastAPI string and validation details", async () => {
    const stringDetail = clientReturning(() =>
      Response.json({ detail: "Job not found" }, { status: 404 }),
    );
    expect(await failureOf(unwrap(stringDetail.GET("/api/screener/indices")))).toMatchObject({
      code: "HTTP_ERROR",
      message: "Job not found",
      status: 404,
    });

    const listDetail = clientReturning(() =>
      Response.json(
        { detail: [{ msg: "Field required", loc: ["query", "symbols"] }] },
        { status: 422 },
      ),
    );
    expect(await failureOf(unwrap(listDetail.GET("/api/screener/indices")))).toMatchObject({
      code: "VALIDATION_ERROR",
      message: "Field required",
      status: 422,
    });
  });

  it("falls back to the status line for non-JSON error bodies", async () => {
    const api = clientReturning(() => new Response("", { status: 502, statusText: "Bad Gateway" }));
    expect(await failureOf(unwrap(api.GET("/api/screener/indices")))).toMatchObject({
      code: "HTTP_ERROR",
      message: "HTTP 502: Bad Gateway",
      status: 502,
    });
  });

  it("turns a network failure into NETWORK_ERROR", async () => {
    const api = clientReturning(() => {
      throw new TypeError("Failed to fetch");
    });
    expect(await failureOf(unwrap(api.GET("/api/screener/indices")))).toMatchObject({
      code: "NETWORK_ERROR",
      message: "Failed to fetch",
      status: null,
    });
  });
});

describe("parseResponse", () => {
  const schema = z.object({ total: z.number() });

  it("returns parsed data", () => {
    expect(parseResponse(schema, { total: 3 })).toEqual({ total: 3 });
  });

  it("throws RESPONSE_SHAPE_ERROR on a mismatch", () => {
    expect(() => parseResponse(schema, { total: "3" })).toThrowError(
      expect.objectContaining({ code: "RESPONSE_SHAPE_ERROR" }),
    );
  });
});
