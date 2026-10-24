import { describe, expect, it } from "vite-plus/test";
import { ApiError } from "@/lib/api/errors";
import { failedBrokerStatus, toBrokerStatus } from "../auth";

describe("toBrokerStatus", () => {
  it("reads a connected body with its user", () => {
    expect(toBrokerStatus({ connected: true, user_id: "XA1234", active: false })).toEqual({
      kind: "connected",
      userId: "XA1234",
      active: false,
    });
  });

  it("keeps an unreadable token store apart from a plain disconnect", () => {
    const error = "Could not read Fyers login status";
    expect(toBrokerStatus({ connected: false, error, active: true })).toEqual({
      kind: "unreadable",
      error,
      active: true,
    });
    expect(toBrokerStatus({ connected: false, active: true })).toEqual({
      kind: "disconnected",
      active: true,
    });
  });
});

describe("failedBrokerStatus", () => {
  it("is offline for a network error or a 5xx", () => {
    const network = new ApiError({ code: "NETWORK_ERROR", message: "Failed to fetch" });
    const server = new ApiError({ code: "HTTP_ERROR", message: "HTTP 502", status: 502 });
    expect(failedBrokerStatus(network)).toEqual({ kind: "offline" });
    expect(failedBrokerStatus(server)).toEqual({ kind: "offline" });
  });

  it("is unreadable with the message for a 4xx", () => {
    const missing = new ApiError({ code: "HTTP_ERROR", message: "Not Found", status: 404 });
    expect(failedBrokerStatus(missing)).toEqual({
      kind: "unreadable",
      error: "Not Found",
      active: false,
    });
  });
});
