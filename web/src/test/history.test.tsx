import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import type { HistoryPageDto } from "../types";
import type { EventSourceFactory, EventSourceLike } from "../useBootstrap";
import { activeFixture } from "./fixture";

const page = (records: HistoryPageDto["records"]): HistoryPageDto => ({
  records, offset: 0, limit: 25, next_offset: null, has_more: false,
});
const record = { event_id: "persistent-1", body: "MOON", icao: "ABC123",
  callsign: "HISTORY1", predicted_event_utc: "2026-09-03T20:15:00Z",
  outcome: "PASSED", final_separation_deg: 0.41, transit_distance_km: 34.4,
  separation_class: "GREEN" };
const quietStream: EventSourceFactory = () => ({ close() {}, addEventListener() {},
  onopen: null, onerror: null });

describe("persistent HISTORY", () => {
  it("loads the existing history source and renders public fields", async () => {
    const historyClient = vi.fn().mockResolvedValue(page([record]));
    render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    expect(await screen.findByText("HISTORY1")).toBeInTheDocument();
    expect(screen.getAllByText("MOON")).toHaveLength(2);
    expect(screen.getByText("PASSED")).toBeInTheDocument();
    expect(screen.getByText("SEP 0.41°")).toHaveClass("sep-green");
    expect(screen.getByText("34.4 km")).toBeInTheDocument();
    expect(historyClient).toHaveBeenCalledWith({ date: undefined, fromDate: undefined,
      toDate: undefined, callsign: undefined,
      body: "ALL", maxSepDeg: undefined, offset: 0, limit: 25 });
  });

  it("applies date and trimmed callsign filters", async () => {
    const historyClient = vi.fn().mockResolvedValue(page([]));
    render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    await waitFor(() => expect(historyClient).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("UTC date"), { target: { value: "2026-09-03" } });
    await waitFor(() => expect(historyClient).toHaveBeenLastCalledWith(expect.objectContaining({
      date: "2026-09-03" })));
    fireEvent.change(screen.getByLabelText("Callsign filter"), { target: { value: "  HIS  " } });
    await waitFor(() => expect(historyClient).toHaveBeenLastCalledWith(expect.objectContaining({
      date: "2026-09-03", callsign: "HIS" })));
  });

  it("applies From and To dates to History requests", async () => {
    const historyClient = vi.fn().mockResolvedValue(page([]));
    render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    fireEvent.change(screen.getByLabelText("From date"), {
      target: { value: "2026-08-01" },
    });
    fireEvent.change(screen.getByLabelText("To date"), {
      target: { value: "2026-08-31" },
    });
    await waitFor(() => expect(historyClient).toHaveBeenLastCalledWith(expect.objectContaining({
      fromDate: "2026-08-01", toDate: "2026-08-31", offset: 0,
    })));
  });

  it("sends body and SEP filters, removes blank SEP, and preserves filters for load more", async () => {
    const historyClient = vi.fn()
      .mockResolvedValueOnce({ ...page([record]), has_more: true, next_offset: 25 })
      .mockResolvedValue(page([]));
    render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    await screen.findByText("HISTORY1");
    fireEvent.change(screen.getByLabelText("Celestial body"), { target: { value: "MOON" } });
    fireEvent.change(screen.getByLabelText("Maximum final separation"), { target: { value: "3.0" } });
    await waitFor(() => expect(historyClient).toHaveBeenLastCalledWith(expect.objectContaining({
      body: "MOON", maxSepDeg: "3.0", offset: 0 })));

    historyClient.mockResolvedValueOnce({ ...page([record]), has_more: true, next_offset: 25 });
    fireEvent.change(screen.getByLabelText("Callsign filter"), { target: { value: "HISTORY" } });
    await screen.findByText("HISTORY1");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(historyClient).toHaveBeenLastCalledWith({ date: undefined,
      fromDate: undefined, toDate: undefined,
      callsign: "HISTORY", body: "MOON", maxSepDeg: "3.0", offset: 25, limit: 25 }));

    fireEvent.change(screen.getByLabelText("Maximum final separation"), { target: { value: "" } });
    await waitFor(() => expect(historyClient).toHaveBeenLastCalledWith(expect.objectContaining({
      body: "MOON", maxSepDeg: undefined, offset: 0 })));
  });

  it("builds CSV export from all active filters independently of pagination", async () => {
    const historyClient = vi.fn().mockResolvedValue(page([]));
    render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    fireEvent.change(screen.getByLabelText("UTC date"), {
      target: { value: "2026-09-03" },
    });
    fireEvent.change(screen.getByLabelText("From date"), {
      target: { value: "2026-09-01" },
    });
    fireEvent.change(screen.getByLabelText("To date"), {
      target: { value: "2026-09-30" },
    });
    fireEvent.change(screen.getByLabelText("Callsign filter"), {
      target: { value: "  FIN99  " },
    });
    fireEvent.change(screen.getByLabelText("Celestial body"), {
      target: { value: "MOON" },
    });
    fireEvent.change(screen.getByLabelText("Maximum final separation"), {
      target: { value: "0,8" },
    });
    const button = screen.getByRole("button", { name: "Export CSV" });
    const form = button.closest("form") as HTMLFormElement;
    expect(new URL(form.action).pathname).toBe("/api/history/export.csv");
    expect(Object.fromEntries(new FormData(form).entries())).toEqual({
      date: "2026-09-03", from_date: "2026-09-01", to_date: "2026-09-30",
      callsign: "FIN99", body: "MOON", max_sep_deg: "0.8",
    });
  });

  it("renders ICAO beside callsign and tolerates missing ICAO", async () => {
    const historyClient = vi.fn().mockResolvedValue(page([
      { ...record, icao_hex: "abc123" },
      { ...record, event_id: "missing-icao", callsign: "NOHEX", icao: undefined },
    ]));
    render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    const callsign = await screen.findByText("HISTORY1");
    expect(callsign.closest(".event-identity")).toHaveTextContent("HISTORY1ABC123");
    expect(screen.getByText("ABC123")).toHaveClass("event-icao");
    expect(screen.getByText("NOHEX")).toBeInTheDocument();
  });

  it("builds the exact filtered URL, replaces results, and normalizes comma decimals", async () => {
    const fetchMock = vi.fn(async (request: string) => ({ ok: true,
      json: async () => page(request.includes("max_sep_deg=") ? [] : [record]) }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      render(<App client={async () => activeFixture} eventSourceFactory={quietStream} />);
      fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
      expect(await screen.findByText("HISTORY1")).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("Maximum final separation"), {
        target: { value: "0.8" },
      });
      await waitFor(() => expect(fetchMock.mock.calls.some(([url]) =>
        String(url).includes("max_sep_deg=0.8"))).toBe(true));
      expect(await screen.findByText("No matching events")).toBeInTheDocument();
      expect(screen.queryByText("HISTORY1")).not.toBeInTheDocument();

      fireEvent.change(screen.getByLabelText("Maximum final separation"), {
        target: { value: "0,8" },
      });
      await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("max_sep_deg=0.8"))).toHaveLength(2));
      expect(fetchMock.mock.calls.some(([url]) => String(url).includes("0%2C8"))).toBe(false);

      fireEvent.change(screen.getByLabelText("Maximum final separation"), {
        target: { value: "" },
      });
      await waitFor(() => expect(String(fetchMock.mock.calls.at(-1)?.[0]))
        .not.toContain("max_sep_deg"));
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("replaces an unfiltered 5.3 degree result and ignores its stale response", async () => {
    const highSepRecord = { ...record, event_id: "high-sep", callsign: "HIGH53",
      final_separation_deg: 5.3 };
    let finishUnfiltered!: (value: unknown) => void;
    const unfilteredResponse = new Promise((resolve) => { finishUnfiltered = resolve; });
    const requestedUrls: string[] = [];
    const responseRecords: number[][] = [];
    const fetchMock = vi.fn(async (request: string) => {
      if (String(request) === "/api/mobile-gps") {
        return { ok: true, json: async () => ({ available: true, status: "OFF" }) };
      }
      if (String(request).startsWith("/api/history?")) requestedUrls.push(String(request));
      const filtered = String(request).includes("max_sep_deg=0.8");
      const response = filtered
        ? { ok: true, json: async () => {
            responseRecords.push([]);
            return page([]);
          } }
        : await unfilteredResponse;
      return response as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      render(<App client={async () => activeFixture} eventSourceFactory={quietStream} />);
      fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
      await waitFor(() => expect(requestedUrls).toHaveLength(1));

      fireEvent.change(screen.getByLabelText("Maximum final separation"), {
        target: { value: "0.8" },
      });
      await waitFor(() => expect(requestedUrls).toContain(
        "/api/history?offset=0&limit=25&body=ALL&max_sep_deg=0.8"));
      expect(await screen.findByText("No matching events")).toBeInTheDocument();
      expect(responseRecords).toEqual([[]]);

      finishUnfiltered({ ok: true, json: async () => {
        responseRecords.push([5.3]);
        return page([highSepRecord]);
      } });
      await waitFor(() => expect(responseRecords).toEqual([[], [5.3]]));
      expect(screen.queryByText("HIGH53")).not.toBeInTheDocument();
      expect(screen.getByText("No matching events")).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it.each([["CALL123", "CALL123"], [null, "ABC123"], ["", "ABC123"]])(
    "uses callsign %j with ICAO fallback", async (callsign, expected) => {
      const historyClient = vi.fn().mockResolvedValue(page([{ ...record, callsign }]));
      render(<App client={async () => activeFixture} historyClient={historyClient}
        eventSourceFactory={quietStream} />);
      fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
      expect(await screen.findByText(expected)).toBeInTheDocument();
    });

  it("distinguishes loading, empty, and error states", async () => {
    let finish!: (value: HistoryPageDto) => void;
    const historyClient = vi.fn(() => new Promise<HistoryPageDto>((resolve) => { finish = resolve; }));
    const rendered = render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    expect(screen.getByText("Loading history…")).toBeInTheDocument();
    finish(page([]));
    expect(await screen.findByText("No matching events")).toBeInTheDocument();
    rendered.unmount();

    render(<App client={async () => activeFixture}
      historyClient={async () => { throw new Error("offline"); }}
      eventSourceFactory={quietStream} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("History request failed");
  });

  it("settings SSE does not wipe persistent history results or filters", async () => {
    class Source implements EventSourceLike {
      onopen = null; onerror = null;
      listeners = new Map<string, (event: MessageEvent<string>) => void>();
      close() {};
      addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
        this.listeners.set(type, listener);
      }
    }
    const source = new Source();
    const historyClient = vi.fn().mockResolvedValue(page([record]));
    render(<App client={async () => activeFixture} historyClient={historyClient}
      eventSourceFactory={() => source} />);
    fireEvent.click(await screen.findByRole("button", { name: "HISTORY" }));
    fireEvent.change(screen.getByLabelText("Callsign filter"), { target: { value: "HISTORY" } });
    expect(await screen.findByText("HISTORY1")).toBeInTheDocument();
    act(() => source.listeners.get("settings")?.({ data: JSON.stringify({ schema_version: 1,
      event: "settings", settings_revision: 4, payload: { schema_version: 1, revision: 4,
        values: activeFixture.settings, capabilities: activeFixture.capabilities,
        persistence: "RUNTIME_ONLY_RESET_TO_CONFIG_ON_RESTART" } }) } as MessageEvent<string>));
    expect(screen.getByLabelText("Callsign filter")).toHaveValue("HISTORY");
    expect(screen.getByText("HISTORY1")).toBeInTheDocument();
  });
});
