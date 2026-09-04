import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import { activeFixture } from "./fixture";

describe("LIVE screen", () => {
  it("renders ACTIVE live state, bodies, candidates, history and observer diagnostics", async () => {
    render(<App client={async () => activeFixture} pollIntervalMs={60_000} />);
    expect(await screen.findByRole("status")).toHaveTextContent("ACTIVE");
    expect(screen.getByText("TEST123")).toBeInTheDocument();
    expect(screen.getByText("RECENT1")).toBeInTheDocument();
    expect(screen.getByText(/ALT 31.2° · AZ 217.4°/)).toBeInTheDocument();
    expect(screen.getAllByText("MOBILE")).toHaveLength(2);
  });

  it("keeps backend STALE distinct from fetch failure", async () => {
    render(<App client={async () => ({ ...activeFixture, health: "STALE" })} pollIntervalMs={60_000} />);
    expect(await screen.findByRole("status")).toHaveTextContent("STALE");
    expect(screen.queryByText(/Connection failed/)).not.toBeInTheDocument();
  });

  it("preserves the last valid snapshot and marks it OFFLINE after failure", async () => {
    const client = vi.fn()
      .mockResolvedValueOnce(activeFixture)
      .mockRejectedValue(new Error("offline"));
    render(<App client={client} pollIntervalMs={5} />);
    expect(await screen.findByText("TEST123")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("OFFLINE"));
    expect(screen.getByText("TEST123")).toBeInTheDocument();
    expect(screen.getByText(/showing the last valid snapshot/i)).toBeInTheDocument();
  });

  it("renders only the privacy-safe observer diagnostics from bootstrap", async () => {
    const { container } = render(<App client={async () => activeFixture} pollIntervalMs={60_000} />);
    await screen.findByText("TEST123");
    expect(container).toHaveTextContent("GPS");
    expect(container.innerHTML).not.toMatch(/latitude|longitude|token|filesystem|forensic/i);
  });

  it("handles missing optional fields", async () => {
    const sparse = {
      ...activeFixture,
      observer: {},
      bodies: {
        sun: { current_position: null, candidates: [{}] },
        moon: { current_position: null, candidates: [] },
      },
      recent_events: [{}],
      capabilities: {},
    };
    render(<App client={async () => sparse} pollIntervalMs={60_000} />);
    expect(await screen.findAllByText("NOCALL")).toHaveLength(2);
    expect(screen.getByText("Runtime settings API: unavailable")).toBeInTheDocument();
  });
});
