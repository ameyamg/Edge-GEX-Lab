import type {
  CaptureRow,
  Expirations,
  FuturesView,
  GexMap,
  Health,
  Recap,
  Replay,
  ReplayDates,
  Scorecard,
  SearchHit,
  Tickers,
  UpdateInfo,
} from "./types";

// VITE_MOCK=1 (npm run dev:mock) answers every call from src/mocks with synthetic data.
export const MOCK = import.meta.env.VITE_MOCK === "1";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// Where the API listens: the page's own address when the API serves the built UI, the proxy target in dev.
export const API_HOST = import.meta.env.DEV ? `127.0.0.1:${__API_PORT__}` : window.location.host;
const OFFLINE = `Nothing answered on ${API_HOST}.`;

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  if (MOCK) {
    const { mockFetch } = await import("../mocks");
    return (await mockFetch(path, init)) as T;
  }
  let res: Response;
  try {
    res = await fetch(path, { headers: { Accept: "application/json", "Content-Type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, OFFLINE);
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    let json = false;
    try {
      const body = await res.json();
      json = true;
      if (body && typeof body.detail === "string") msg = body.detail;
      else if (body && typeof body.error === "string") msg = body.error;
    } catch {
      /* body was not JSON; keep the status line */
    }
    // The Vite proxy answers 5xx with a non-JSON body when nothing listens on the API port.
    if (!json && res.status >= 500) throw new ApiError(0, OFFLINE);
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

const q = (params: Record<string, string | number | boolean | undefined>) =>
  Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");

export const api = {
  health: () => http<Health>("/api/health"),
  update: () => http<UpdateInfo>("/api/update"),
  tickers: () => http<Tickers>("/api/tickers"),
  search: (term: string) => http<SearchHit[]>(`/api/search?${q({ q: term })}`),
  expirations: (ticker: string) => http<Expirations>(`/api/expirations?${q({ ticker })}`),
  map: (ticker: string, expiries: string[], signed: boolean) =>
    http<GexMap>(`/api/map?${q({ ticker, expiries: expiries.join(","), signed })}`),
  captures: (ticker: string, days: number) => http<CaptureRow[]>(`/api/captures?${q({ ticker, days })}`),
  replay: (ticker: string, date: string) => http<Replay>(`/api/replay?${q({ ticker, date })}`),
  recap: (ticker: string, date: string) => http<Recap>(`/api/recap?${q({ ticker, date })}`),
  generateRecap: (ticker: string, date: string, signal?: AbortSignal) =>
    http<Recap>("/api/recap/generate", { method: "POST", body: JSON.stringify({ ticker, date }), signal }),
  scorecard: (variant?: "passed") => http<Scorecard>(`/api/scorecard${variant ? `?variant=${variant}` : ""}`),
  futures: (ticker: string, o: { overridePrice?: number; overrideBasis?: number } = {}) =>
    http<FuturesView>(`/api/futures?${q({ ticker, override_price: o.overridePrice, override_basis: o.overrideBasis })}`),
  replayDates: (ticker: string) => http<ReplayDates>(`/api/replay/dates?${q({ ticker })}`),
};
