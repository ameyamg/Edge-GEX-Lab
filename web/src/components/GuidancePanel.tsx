import { useEffect, useRef, useState } from "react";
import { MOCK } from "../api/client";
import { DEMO_LABEL, Icon, Panel, Skeleton, usePresent } from "./ui";
import { isIndex } from "../lib/format";
import "./GuidancePanel.css";

// Trade guidance: model-based ideas computed by the API from this map (gexlab/guidance).
// Every price comes from the map or a stated computation of map numbers. The narrative is a
// deterministic template unless "Explain this map" returns an AI text that passed the validator.

type Bias = "long" | "short" | "flat";
interface GLevel {
  key: string;
  label: string;
  price: number;
  distance: number;
  distance_pct: number | null;
  side: "above" | "below" | "at";
  frozen?: boolean;
  sign?: "positive" | "negative";
  net_gex_billions?: number;
}
interface Scenario {
  id: string;
  name: string;
  trigger: string;
  bias: Bias;
  entry: { low: number; high: number; basis: string } | null;
  stop: { price: number; basis: string } | null;
  targets: { price: number; label: string }[];
  why: string;
  rr: number | null;
  rr_basis: string | null;
}
interface Narrative {
  text: string;
  source: "ai" | "template";
  model: string;
  validated: boolean;
  rejected: string[];
  cached?: boolean;
}
interface Guidance {
  ticker: string;
  as_of: string | null;
  valuation_ts: string | null;
  spot: number;
  synthetic: boolean;
  feed: { label: string | null; stale: boolean; market_open: boolean | null };
  status: { tested: boolean; chip: string; scorecard_passed?: boolean; scorecard_lead: string | null; footnote: string };
  regime: { regime: string; summary: string; size_text: string };
  environment: { kind: "trend" | "mean_reversion" | "mixed"; label: string; why: string };
  atr: { value: number; buffer: number; buffer_formula: string } | null;
  levels: GLevel[];
  scenarios: Scenario[];
  setup_notes: string[];
  regime_change: string[];
  confidence: string[];
  computations: { name: string; formula: string; value: number }[];
  narrative: Narrative;
}

const OPEN_KEY = "gexlab.guidanceOpen";
function readOpen(): boolean {
  try {
    return localStorage.getItem(OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}
function writeOpen(v: boolean) {
  try {
    localStorage.setItem(OPEN_KEY, v ? "1" : "0");
  } catch {
    /* private window: the panel still works, it just forgets */
  }
}

// whole=true: a PRICE (or a distance in points) on SPX/NDX, shown in whole index points (Simon, Oct 18).
// Percentages and GEX billions always keep up to 2 decimals.
function fx(v: number | null | undefined, whole = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  return v.toLocaleString("en-US", { maximumFractionDigits: whole ? 0 : 2 });
}
function dist(l: GLevel, whole: boolean): string {
  if (l.side === "at") return "at spot";
  return `${l.distance > 0 ? "+" : "-"}${fx(Math.abs(l.distance), whole)}${l.distance_pct !== null ? ` (${fx(Math.abs(l.distance_pct))}%)` : ""}`;
}
function levelClass(l: GLevel): string {
  if (l.frozen) return "g-muted";
  if (l.key === "call_wall") return "g-green";
  if (l.key === "put_wall") return "g-purple";
  if (l.key === "cluster") return l.sign === "positive" ? "g-green-dim" : "g-purple-dim";
  return "g-text";
}
function entryText(s: Scenario, whole: boolean): string {
  if (!s.entry) return "none";
  return s.entry.low === s.entry.high ? fx(s.entry.low, whole) : `${fx(s.entry.low, whole)} to ${fx(s.entry.high, whole)}`;
}

async function getJson<T>(url: string, init?: RequestInit): Promise<{ status: number; body: T | null }> {
  const res = await fetch(url, { headers: { Accept: "application/json", "Content-Type": "application/json" }, ...init });
  if (!res.ok) return { status: res.status, body: null };
  return { status: res.status, body: (await res.json()) as T };
}

export default function GuidancePanel({
  ticker,
  expiries,
  signed,
  asOf,
  range,
}: {
  ticker: string;
  expiries: string;
  signed: boolean;
  asOf: string | null;
  range: number;
}) {
  const present = usePresent();
  const [open, setOpen] = useState(readOpen);
  const [g, setG] = useState<Guidance | null>(null);
  const [hidden, setHidden] = useState(MOCK); // the mock build has no guidance endpoint
  const [err, setErr] = useState<string | null>(null);
  const [ai, setAi] = useState<Narrative | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const aiKey = useRef("");

  const key = `${ticker}|${expiries}|${signed}|${range}`;

  useEffect(() => {
    if (MOCK || !asOf) return;
    let live = true;
    const qs = new URLSearchParams({ ticker, signed: String(signed), range: String(range) });
    if (expiries) qs.set("expiries", expiries);
    getJson<Guidance>(`/api/guidance?${qs.toString()}`)
      .then(({ status, body }) => {
        if (!live) return;
        if (status === 404 && !body) {
          setHidden(true);
          return;
        }
        if (!body) {
          setErr(`Guidance unavailable (HTTP ${status}).`);
          return;
        }
        setHidden(false);
        setErr(null);
        setG(body);
      })
      .catch(() => live && setErr("Guidance unavailable: the API did not answer."));
    return () => {
      live = false;
    };
  }, [ticker, expiries, signed, range, asOf]);

  // an AI narrative belongs to one map: drop it when the map or the selection changes
  useEffect(() => {
    if (g && aiKey.current !== `${key}|${g.valuation_ts}`) setAi(null);
  }, [g, key]);

  if (hidden) return null;

  const explain = async () => {
    if (!g) return;
    setAiBusy(true);
    const k = `${key}|${g.valuation_ts}`;
    try {
      const { body, status } = await getJson<Narrative>("/api/guidance/narrate", {
        method: "POST",
        body: JSON.stringify({ ticker, expiries: expiries || null, signed, range }),
      });
      aiKey.current = k;
      setAi(body ?? { text: g.narrative.text, source: "template", model: "template", validated: true, rejected: [`HTTP ${status}`] });
    } catch {
      aiKey.current = k;
      setAi({ ...g.narrative, rejected: ["the API did not answer"] });
    } finally {
      setAiBusy(false);
    }
  };

  const toggle = () => {
    setOpen((v) => {
      writeOpen(!v);
      return !v;
    });
  };

  const chip = g ? (
    <span className={`g-chip ${g.status.tested ? "measured" : ""}`} title={g.status.scorecard_lead ? `${g.status.scorecard_lead} The Scorecard measures levels, not these setups.` : "No study tests these entry, stop and target rules: no measured hit rate."}>
      {g.status.chip}
    </span>
  ) : null;

  const title = (
    <span className="g-title">
      Trade guidance {chip}
    </span>
  );
  const right = (
    <button type="button" className="g-toggle" onClick={toggle} aria-expanded={open}>
      {open ? "Hide" : "Show"}
    </button>
  );

  if (!g) {
    return (
      <Panel title={title} right={right} className={`guidance-panel ${present ? "present" : ""}`}>
        {err ? <p className="g-small g-muted">{err}</p> : <Skeleton h={60} />}
      </Panel>
    );
  }

  const narrative = ai ?? g.narrative;
  const whole = isIndex(g.ticker);
  const env = g.environment;
  const ladder: (GLevel | { spot: number })[] = [];
  let spotPlaced = false;
  for (const l of g.levels) {
    if (!spotPlaced && l.price < g.spot) {
      ladder.push({ spot: g.spot });
      spotPlaced = true;
    }
    ladder.push(l);
  }
  if (!spotPlaced) ladder.push({ spot: g.spot });

  return (
    <Panel title={title} right={right} className={`guidance-panel ${present ? "present" : ""}`}>
      {g.synthetic && (
        <span className="demo-tag" role="note">
          <Icon name="info" size={13} />
          {DEMO_LABEL}
        </span>
      )}
      {g.feed.stale && <p className="g-small g-amber">Feed is stale: {g.feed.label}. These levels may be out of date.</p>}
      {err && <p className="g-small g-amber">{err} Showing the last guidance.</p>}

      <div className={`g-env ${env.kind}`}>
        <span className="g-env-label">{env.label}</span>
        <span className="g-env-sum">{g.regime.summary}</span>
      </div>

      {open && (
        <>
          <p className="g-small g-muted">{env.why}</p>
          <p className="g-small g-muted">{g.regime.size_text}</p>

          <div className="g-sec">Levels to watch</div>
          <table className="g-levels">
            <thead>
              <tr>
                <th>Level</th>
                <th>Type</th>
                <th className="r">From spot</th>
              </tr>
            </thead>
            <tbody>
              {ladder.map((l, i) =>
                "spot" in l ? (
                  <tr key={`spot-${i}`} className="g-spot">
                    <td className="mono">{fx(l.spot, whole)}</td>
                    <td>spot</td>
                    <td className="r mono">0</td>
                  </tr>
                ) : (
                  <tr key={`${l.key}-${l.price}`}>
                    <td className={`mono ${levelClass(l)}`}>{fx(l.price, whole)}</td>
                    <td className={levelClass(l)}>
                      {l.label}
                      {l.net_gex_billions !== undefined ? <span className="g-muted"> {fx(l.net_gex_billions)}B</span> : null}
                    </td>
                    <td className="r mono">{dist(l, whole)}</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
          {g.atr && <p className="g-small g-muted">Buffer: {g.atr.buffer_formula}.</p>}

          <div className="g-sec">Scenarios</div>
          {(g.setup_notes ?? []).length > 0 && (
            <ul className="g-list g-notes">
              {g.setup_notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
          <div className="g-cards">
            {g.scenarios.map((s) => (
              <article key={s.id} className={`g-card ${s.bias}`}>
                <header>
                  <span className="g-name">{s.name}</span>
                  <span className="g-tags">
                    {s.rr !== null && s.rr !== undefined && (
                      <span className="g-rr" title={s.rr_basis ?? undefined}>
                        R:R {fx(s.rr)}
                      </span>
                    )}
                    <span className={`g-bias ${s.bias}`}>{s.bias}</span>
                  </span>
                </header>
                <p className="g-trigger">{s.trigger}.</p>
                {s.bias !== "flat" && (
                  <dl className="g-plan">
                    <dt>Entry</dt>
                    <dd className="mono">{entryText(s, whole)}</dd>
                    <dt>Stop</dt>
                    <dd className="mono" title={s.stop?.basis}>
                      {s.stop ? fx(s.stop.price, whole) : "none"}
                    </dd>
                    <dt>Targets</dt>
                    <dd className="mono">{s.targets.map((t) => fx(t.price, whole)).join(" / ") || "none"}</dd>
                  </dl>
                )}
                <p className="g-why">{s.why}</p>
              </article>
            ))}
          </div>

          <div className="g-sec">What changes this</div>
          <ul className="g-list">
            {g.regime_change.map((c, i) => (
              <li key={i} className={/feed is stale now/i.test(c) ? "g-amber" : ""}>
                {c}
              </li>
            ))}
          </ul>

          <div className="g-sec g-sec-row">
            <span>Narrative</span>
            <span className={`g-src ${narrative.source}`}>
              {narrative.source === "ai" ? (narrative.cached ? "AI, validated, cached" : "AI, validated") : ai ? "Template (AI not used)" : "Template"}
            </span>
          </div>
          <p className="g-narr">{narrative.text}</p>
          {ai && ai.rejected.length > 0 && (
            <details className="g-small g-muted">
              <summary>Why the template is shown</summary>
              <ul className="g-list">
                {ai.rejected.slice(0, 6).map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </details>
          )}
          <button type="button" className="chip g-explain" onClick={explain} disabled={aiBusy}>
            {aiBusy ? "Explaining..." : "Explain this map"}
          </button>

          <details className="g-small g-muted">
            <summary>Confidence notes</summary>
            <ul className="g-list">
              {g.confidence.map((c, i) => (
                <li key={i} className={/stale/i.test(c) ? "g-amber" : ""}>
                  {c}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}

      <p className="g-foot">{g.status.footnote}</p>
    </Panel>
  );
}
