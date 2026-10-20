import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { api } from "../api/client";
import type { Expiration, GexMap, Magnitude, SearchHit } from "../api/types";
import GexProfile, { type ProfileLevels } from "../components/GexProfile";
import GuidancePanel from "../components/GuidancePanel";
import { Drawer, ErrorState, Icon, Lead, Panel, Seg, Skeleton, usePresent, useTopMeta } from "../components/ui";
import { GEX_UNITS, dayFull, dayShort, distText, gex, int, nyTime, nyToday, pct, price, px, strike } from "../lib/format";
import { CURRENT_GATE, gateMin } from "../lib/gate";
import { useAsync, useNow } from "../lib/hooks";

const REFRESH_S = 60;
const RANGES = [2, 5, 10] as const;
const CURVE_NOTE = "Recalculates the selected chain at each price, holding IV, open interest and time fixed.";
const UNSIGNED_NOTE = "Direction is not modeled in this view.";
const SIDES_NOTE = "Left/right separates option types, not signs.";

function hasAM(e: Expiration) {
  return e.settlement === "AM" || (e.settlements ?? []).includes("AM");
}
function settleText(e: Expiration) {
  const set = new Set([e.settlement, ...(e.settlements ?? [])]);
  if (set.has("AM") && set.has("PM")) return "AM settled monthly root (09:30 ET) and PM settled weeklies (16:00 ET)";
  return set.has("AM") ? "AM settled, 09:30 ET" : "PM settled, 16:00 ET";
}
function expLabel(e: Expiration) {
  return e.dte === 0 ? "0DTE" : `+${e.dte}d`;
}

const NY_HM = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

/** True once the expiry's last settlement has passed. Uses settles_at (early closes included) when the API sends it,
 *  else the standard times: PM 16:00 ET, AM-only 09:30 ET, on the expiration date. */
function hasSettled(e: Expiration, nowMs: number, today: string): boolean {
  if (e.settles_at) return Date.parse(e.settles_at) <= nowMs;
  if (e.date !== today) return e.date < today;
  const set = new Set([e.settlement, ...(e.settlements ?? [])]);
  const [h, m] = NY_HM.format(new Date(nowMs)).split(":").map(Number);
  const mins = h * 60 + m;
  return set.has("PM") ? mins >= 16 * 60 : mins >= 9 * 60 + 30;
}

/** Default expiry when a ticker loads: the nearest one that has not settled (0DTE on SPY, QQQ, SPX and NDX weekdays;
 *  the closest listed expiry for stocks). After today's settlement it moves to the next one. Dashboard only: the
 *  recorder and the studies always use every expiry in the window. */
function nearestExpiry(list: Expiration[], nowMs: number, today: string): string | null {
  const sorted = [...list].sort((a, b) => a.date.localeCompare(b.date));
  const open = sorted.find((e) => !hasSettled(e, nowMs, today));
  return (open ?? sorted[0])?.date ?? null;
}

function useWindowHeight() {
  const [h, setH] = useState(() => window.innerHeight);
  useEffect(() => {
    const on = () => setH(window.innerHeight);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return h;
}

/** Headline for the unsigned view, built from the data. It never mentions a flip or a sign. */
function unsignedLead(m: GexMap): string {
  const lv = m.levels;
  const total = Math.abs(m.totals.call_gex) + Math.abs(m.totals.put_gex);
  const where =
    lv.call_wall !== null && lv.call_wall === lv.put_wall
      ? `, with the largest call and put exposure both at ${strike(lv.call_wall)}`
      : `${lv.call_wall !== null ? `, largest call exposure at ${strike(lv.call_wall)}` : ""}${lv.put_wall !== null ? `, largest put exposure at ${strike(lv.put_wall)}` : ""}`;
  return `Unsigned exposure: ${m.ticker} at ${px(m.spot.price, m.ticker)}${where}, total absolute GEX ${gex(total)}. ${UNSIGNED_NOTE}`;
}

/** Method drawer: which vendor's open interest the map uses and how its date was set (model 1.1). */
function oiSourceText(m: GexMap): string {
  const o = m.oi_source;
  if (!o || !o.label) return "Synthetic fixture.";
  const alp = o.alpaca_oi_date ? dayShort(o.alpaca_oi_date) : null;
  if (o.kind === "cboe" && m.feed.kind === "cboe_delayed")
    return `${o.label}. The whole chain comes from Cboe's delayed payload, which does not publish an OI date; it is shown as the session before the payload's day.`;
  if (o.kind === "cboe") {
    const head = `${o.label}. Quotes, spot and IV come from Alpaca; open interest comes from Cboe's delayed payload, joined by OCC symbol (${o.matched ?? "?"} of ${o.total ?? "?"} contracts).`;
    if (o.basis === "matches_alpaca") return `${head} Cboe's numbers match Alpaca's, so they carry Alpaca's date${alp ? ` (${alp})` : ""}.`;
    return `${head} Cboe publishes no OI date; its numbers differ from Alpaca's${alp ? ` (dated ${alp})` : ""}, so it is read as the newer file and dated the most recent completed session before the payload's day.`;
  }
  return `${o.label}. Open interest from Alpaca's contracts endpoint, with the date Alpaca reports.${o.fallback_reason ? ` Cboe open interest was not used: ${o.fallback_reason}.` : ""}`;
}

export default function LiveMap({ initialTicker, onTicker }: { initialTicker: string; onTicker: (t: string) => void }) {
  const present = usePresent();
  const [ticker, setTicker] = useState(initialTicker);
  const [selected, setSelected] = useState<string[] | null>(null);
  // ticker whose expiry selection the user changed by hand; switching tickers clears it so the default applies again
  const manualFor = useRef<string | null>(null);
  const [signedMode, setSignedMode] = useState(true);
  const [overlay, setOverlay] = useState(false);
  const [showCurve, setShowCurve] = useState(false); // off in the introductory map
  const [range, setRange] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);

  const tickers = useAsync(() => api.tickers(), []);
  const pinned = tickers.data?.pinned ?? ["SPY", "QQQ", "SPX", "NDX"];

  const exps = useAsync(() => api.expirations(ticker), [ticker]);
  useEffect(() => {
    if (!exps.data || exps.data.ticker !== ticker) return;
    const list = exps.data.expirations;
    const listed = new Set(list.map((e) => e.date));
    setSelected((cur) => {
      if (manualFor.current === ticker && cur) {
        const kept = cur.filter((d) => listed.has(d));
        if (kept.length) return kept;
      }
      const d = nearestExpiry(list, Date.now(), nyToday());
      return d ? [d] : [];
    });
  }, [exps.data, ticker]);

  const selKey = selected ? [...selected].sort().join(",") : "";
  const map = useAsync<GexMap>(() => api.map(ticker, selKey.split(",").filter(Boolean), signedMode), [ticker, selKey, signedMode], selected !== null && selected.length > 0);

  useEffect(() => {
    if (map.data) setFetchedAt(Date.now());
  }, [map.data]);
  useEffect(() => {
    if (map.error) setFetchedAt(Date.now());
  }, [map.error]);

  // auto refresh with countdown
  const now = useNow(true, 250);
  const elapsed = fetchedAt ? (now - fetchedAt) / 1000 : 0;
  const remaining = Math.max(0, Math.ceil(REFRESH_S - elapsed));
  const reloadRef = useRef(map.reload);
  reloadRef.current = map.reload;
  useEffect(() => {
    if (!paused && fetchedAt && !map.loading && elapsed >= REFRESH_S) {
      setFetchedAt(Date.now()); // restart the clock now, the data effect resets it on arrival
      reloadRef.current();
    }
  }, [elapsed, paused, fetchedAt, map.loading]);

  const m = map.data && map.data.ticker === ticker ? map.data : null;
  const expsFor = exps.data && exps.data.ticker === ticker ? exps.data : null;
  const noExpiries = expsFor !== null && expsFor.expirations.length === 0;
  const fatal: Error | null = !m ? (map.error ?? (exps.error && !expsFor ? exps.error : null)) : null;
  const retryAll = () => {
    exps.reload();
    map.reload();
  };

  // default range: the widest of 2/5/10% that keeps the ladder under ~80 strikes (~45 in Present
  // mode, so each bar stays thick enough to read on a phone)
  const autoRange = useMemo(() => {
    if (!m) return 5;
    const cap = present ? 45 : 80;
    for (const r of [10, 5, 2]) {
      const n = m.strikes.filter((s) => Math.abs(s.strike - m.spot.price) <= (m.spot.price * r) / 100).length;
      if (n <= cap) return r;
    }
    return 2;
  }, [m, present]);
  const rangePct = range ?? autoRange;
  useEffect(() => setRange(null), [ticker]);

  const frozen = m?.frozen_today ?? null;
  const showFrozen = overlay && frozen !== null;
  const levels: ProfileLevels | null = m
    ? showFrozen
      ? { call_wall: frozen!.call_wall, put_wall: frozen!.put_wall, nearest_zero_gamma: frozen!.nearest_zero_gamma, zero_gamma: frozen!.nearest_zero_gamma !== null ? [frozen!.nearest_zero_gamma] : [] }
      : { call_wall: m.levels.call_wall, put_wall: m.levels.put_wall, nearest_zero_gamma: m.levels.nearest_zero_gamma, zero_gamma: m.levels.zero_gamma }
    : null;
  const curveOn = showCurve && !!m?.signed;

  const winH = useWindowHeight();
  const captionRows = (m && !m.signed ? 1 : 0) + (curveOn ? 1 : 0) + (showFrozen ? 1 : 0);
  const magRow = present && !!m?.signed && !!m?.magnitude ? 96 : 0;
  const chartH = present ? Math.max(420, winH - 560 - magRow - captionRows * 26) : Math.max(500, winH - 330 - captionRows * 22);

  // top bar status
  const feedState = !m ? "none" : !m.feed.market_open ? "closed" : m.feed.stale ? "stale" : "live";
  useTopMeta(
    {
      node: m ? (
        <>
          <span className={`dot ${feedState === "live" ? "live" : feedState}`} aria-hidden />
          <span>
            {feedState === "stale" ? (
              <b style={{ color: "var(--amber)" }}>Stale: no refresh since {nyTime(m.feed.last_success, true)}</b>
            ) : feedState === "closed" ? (
              <>
                <b style={{ color: "var(--muted)" }}>Market closed</b>
                {m.valuation?.label ? <span className="hide-sm">. {m.valuation.label}</span> : null}
              </>
            ) : (
              <b>{m.feed.label}</b>
            )}
          </span>
          <span className="sep hide-sm" />
          <span className="hide-sm">
            last <b>{nyTime(m.feed.last_success ?? m.as_of, true)}</b> ET
          </span>
          <span className="sep" />
          <span>
            OI date <b>{dayShort(m.oi_date)}</b>
          </span>
          {m.oi_source?.label ? (
            <>
              <span className="sep hide-sm" />
              <span className="hide-sm" title={m.oi_source.fallback_reason ? `Cboe open interest not used: ${m.oi_source.fallback_reason}` : undefined}>
                <b style={m.oi_source.fallback_reason ? { color: "var(--amber)" } : undefined}>{m.oi_source.label}</b>
              </span>
            </>
          ) : null}
        </>
      ) : fatal || map.error ? (
        <span className="inline-error">Feed unavailable</span>
      ) : (
        <span>Loading feed</span>
      ),
      progress: paused || !fetchedAt ? null : Math.min(1, elapsed / REFRESH_S),
    },
    [m, feedState, map.error, fatal, paused, fetchedAt, Math.floor(elapsed * 4)],
  );

  const chooseTicker = useCallback(
    (t: string) => {
      if (t === ticker) return;
      manualFor.current = null;
      setTicker(t);
      setSelected(null);
      setOverlay(false);
      onTicker(t);
    },
    [ticker, onTicker],
  );

  function toggleExp(d: string) {
    manualFor.current = ticker;
    setSelected((cur) => {
      const s = new Set(cur ?? []);
      if (s.has(d)) {
        if (s.size === 1) return cur;
        s.delete(d);
      } else s.add(d);
      return [...s];
    });
  }

  const expList = exps.data && exps.data.ticker === ticker ? exps.data.expirations : [];
  const allOn = selected !== null && expList.length > 0 && selected.length === expList.length;
  const selExps = expList.filter((e) => selected?.includes(e.date));
  const expText = allOn && expList.length > 1 ? `all ${expList.length} expiries` : selExps.length > 0 && selExps.length <= 3 ? selExps.map(expLabel).join(", ") : null;
  const lead = m ? (m.signed || m.quotes?.usable === false ? m.lead : unsignedLead(m)) : null;

  const chartPanel = (
    <Panel
      className="chart-panel"
      title={
        <>
          {m && !m.signed ? "Unsigned exposure by strike" : "GEX by strike"} <span style={{ color: "var(--muted-2)" }}>·</span> <b className="units">{GEX_UNITS}</b>
        </>
      }
      right={
        <>
          <span className="legend">
            <span>
              <i style={{ background: "var(--green)" }} />
              Calls {signedMode ? "+" : ""}
            </span>
            <span>
              <i style={{ background: "var(--purple-1)" }} />
              Puts {signedMode ? "-" : "(size)"}
            </span>
          </span>
          <button
            type="button"
            className={`chip ${curveOn ? "on" : ""}`}
            aria-pressed={curveOn}
            disabled={!signedMode}
            onClick={() => setShowCurve((s) => !s)}
            style={{ height: 24 }}
            title={signedMode ? `Separate panel. ${CURVE_NOTE}` : "Signed view only. The unsigned view does not model direction."}
          >
            Net GEX at hypothetical prices
          </button>
          <Seg label="Strike range" value={String(rangePct)} onChange={(v) => setRange(Number(v))} options={RANGES.map((r) => ({ value: String(r), label: `±${r}%` }))} />
        </>
      }
    >
      {m && levels ? (
        <GexProfile map={m} levels={levels} rangePct={rangePct} showCurve={curveOn} frozen={showFrozen} height={chartH} present={present} />
      ) : (
        <ProfileSkeleton height={chartH} />
      )}
      {m && captionRows > 0 && (
        <div className="chart-notes">
          {!m.signed && (
            <p>
              <b>{SIDES_NOTE}</b> Both sides show size. {UNSIGNED_NOTE}
            </p>
          )}
          {curveOn && (
            <p>
              <b>Net GEX at hypothetical prices:</b> {CURVE_NOTE} Its vertical axis is that hypothetical price; the bars' vertical axis is strike.
            </p>
          )}
          {showFrozen && (
            <p>
              <b className="p">Wall and gamma flip lines are the 10:00 capture</b>
              <span className={`cap-status ${frozen!.status ?? "pending"}`}>capture {frozen!.status ?? "status unknown"}</span>
              {frozen!.status && frozen!.status !== "eligible" && frozen!.reason ? <span className="muted"> ({frozen!.reason})</span> : null}
              . Bars, spot and totals are the current market.
              {frozen!.status && frozen!.status !== "eligible" ? " This capture is excluded from the studies." : ""}
            </p>
          )}
        </div>
      )}
      {map.error && m && <span className="inline-error">Last refresh failed: {map.error.message}. Showing data from {nyTime(m.as_of, true)} ET.</span>}
    </Panel>
  );

  return (
    <div className="screen">
      {/* controls */}
      <div className="controls">
        <div className="group" role="group" aria-label="Ticker">
          {pinned.map((t) => (
            <button key={t} type="button" className="chip ticker" aria-pressed={ticker === t} onClick={() => chooseTicker(t)}>
              {t}
            </button>
          ))}
          <TickerSearch active={!pinned.includes(ticker) ? ticker : null} onPick={chooseTicker} />
        </div>
        <span className="vsep" />
        <div className="group" role="group" aria-label="Expiries">
          <span className="group-label" title="Opens on the nearest expiry that has not settled. Add expiries with the chips or All; your pick holds until you switch tickers.">
            Expiry
          </span>
          {exps.loading && !expList.length
            ? [0, 1, 2, 3].map((i) => <Skeleton key={i} h={28} w={52} />)
            : expList.map((e) => (
                <button
                  key={e.date}
                  type="button"
                  className="chip"
                  aria-pressed={selected?.includes(e.date) ?? false}
                  title={`${dayFull(e.date)} · ${settleText(e)} · ${e.kind}`}
                  onClick={() => toggleExp(e.date)}
                >
                  {expLabel(e)}
                  {hasAM(e) && <span className="sup">AM</span>}
                </button>
              ))}
          {expList.length > 1 && (
            <button
              type="button"
              className="chip"
              disabled={allOn}
              onClick={() => {
                manualFor.current = ticker;
                setSelected(expList.map((e) => e.date));
              }}
              title="Select every expiry in the 0 to 7 day window (the default is the nearest expiry only)"
            >
              All
            </button>
          )}
        </div>
        <span className="vsep" />
        <button
          type="button"
          className={`chip ${showFrozen ? "on" : ""}`}
          aria-pressed={showFrozen}
          disabled={!frozen}
          onClick={() => setOverlay((o) => !o)}
          title={frozen ? `Draw the walls and gamma flip captured at ${nyTime(frozen.captured_at, true)} ET over the current bars (capture ${frozen.status ?? "status unknown"})` : "No 10:00 capture for this ticker today"}
        >
          Overlay 10:00 levels
        </button>
        <Seg
          label="Sign assumption"
          value={signedMode ? "signed" : "unsigned"}
          onChange={(v) => setSignedMode(v === "signed")}
          options={[
            { value: "signed", label: "Calls + / Puts -", title: "Calls count as positive gamma, puts as negative. An assumption about dealer positioning." },
            { value: "unsigned", label: "Unsigned", title: "Every contract counts by size. Shows where gamma sits, not its direction." },
          ]}
        />
        <div style={{ marginLeft: "auto" }} className="refresh">
          <Countdown remaining={remaining} paused={paused} frac={fetchedAt ? Math.min(1, elapsed / REFRESH_S) : 0} loading={map.loading} />
          <button type="button" className="icon-btn" onClick={() => setPaused((p) => !p)} aria-label={paused ? "Resume auto refresh" : "Pause auto refresh"} title={paused ? "Resume" : "Pause"}>
            <Icon name={paused ? "play" : "pause"} size={13} />
          </button>
          <button type="button" className="icon-btn" onClick={() => map.reload()} aria-label="Refresh now" title="Refresh now" disabled={map.loading}>
            <Icon name="refresh" size={13} />
          </button>
        </div>
      </div>

      {/* lead */}
      <div className="lead-row">
        <Lead text={lead} loading={!m && !fatal && !noExpiries} />
        {m && (
          <div className="lead-aside">
            {showFrozen ? (
              <span className="p">Current market with 10:00 reference levels</span>
            ) : (
              <>Model v{m.model_version}</>
            )}
            <br />
            {expText ?? `${m.by_expiry.length} ${m.by_expiry.length === 1 ? "expiry" : "expiries"}`} · {int(m.coverage.eligible)} contracts
          </div>
        )}
      </div>

      {fatal ? (
        <ErrorState error={fatal} onRetry={retryAll} what={`the ${ticker} map`} />
      ) : noExpiries ? (
        <div className="state">
          <span className="kicker">No chain</span>
          <h2>{ticker} has no listed expirations in the next 7 days.</h2>
          <p>The live map covers expiries from 0 to 7 calendar days out. Pick another ticker, or check back after new weeklies list.</p>
        </div>
      ) : present ? (
        <div className="present-stack">
          <LevelsPanel m={m} levels={levels} frozen={showFrozen} layout="strip" onExplain={() => setDrawer(true)} />
          {chartPanel}
          <GuidancePanel ticker={ticker} expiries={selKey} signed={signedMode} asOf={m?.as_of ?? null} range={rangePct} />
        </div>
      ) : (
        <div className="grid-main">
          {chartPanel}
          <div className="side">
            <LevelsPanel m={m} levels={levels} frozen={showFrozen} layout="grid" onExplain={() => setDrawer(true)} />
            <ExpiryPanel m={m} exps={expList} />
            {m && m.warnings.length > 0 && (
              <Panel title="Notes">
                <ul className="warns">
                  {m.warnings.map((w, i) => (
                    <li key={i} className={/stale/i.test(w) ? "amber" : ""}>
                      {w}
                    </li>
                  ))}
                </ul>
              </Panel>
            )}
            <button type="button" className="drawer-trigger" onClick={() => setDrawer(true)}>
              <span>
                <b>How this is calculated</b> · formula, sign, OI date, feed
              </span>
              <span className="chip" style={{ height: 24 }}>
                Open
              </span>
            </button>
            <GuidancePanel ticker={ticker} expiries={selKey} signed={signedMode} asOf={m?.as_of ?? null} range={rangePct} />
          </div>
        </div>
      )}

      <Drawer open={drawer} onClose={() => setDrawer(false)} title="How this is calculated">
        <MethodBody m={m} ticker={ticker} />
      </Drawer>
    </div>
  );
}

function Countdown({ remaining, paused, frac, loading }: { remaining: number; paused: boolean; frac: number; loading: boolean }) {
  const r = 8;
  const c = 2 * Math.PI * r;
  return (
    <>
      <svg width={20} height={20} viewBox="0 0 20 20" aria-hidden>
        <circle cx={10} cy={10} r={r} fill="none" stroke="var(--line-2)" strokeWidth={2} />
        <circle
          cx={10}
          cy={10}
          r={r}
          fill="none"
          stroke={paused ? "var(--muted-2)" : "var(--green)"}
          strokeWidth={2}
          strokeDasharray={`${c} ${c}`}
          strokeDashoffset={c * frac}
          transform="rotate(-90 10 10)"
          style={{ transition: "stroke-dashoffset .25s linear" }}
        />
      </svg>
      <span aria-live="off">
        {loading ? (
          "Refreshing"
        ) : paused ? (
          "Paused"
        ) : (
          <>
            Next refresh <b>{remaining}s</b>
          </>
        )}
      </span>
    </>
  );
}

function TickerSearch({ active, onPick }: { active: string | null; onPick: (t: string) => void }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [idx, setIdx] = useState(0);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!q.trim()) {
      setHits([]);
      return;
    }
    let live = true;
    const t = window.setTimeout(() => {
      api
        .search(q.trim())
        .then((h) => {
          if (!live) return;
          setHits(h);
          setIdx(0);
          setErr(null);
        })
        .catch((e: Error) => live && setErr(e.message));
    }, 180);
    return () => {
      live = false;
      window.clearTimeout(t);
    };
  }, [q]);

  function pick(s: string) {
    onPick(s);
    setQ("");
    setOpen(false);
    (document.activeElement as HTMLElement | null)?.blur();
  }

  return (
    <div className={`search ${active && !open ? "on" : ""}`}>
      <Icon name="search" />
      <input
        value={open ? q : active ?? q}
        placeholder="Any optionable ticker"
        aria-label="Search optionable tickers"
        role="combobox"
        aria-expanded={open && q.length > 0}
        aria-controls="ticker-typeahead"
        onFocus={() => {
          setOpen(true);
          setQ("");
        }}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        onChange={(e) => setQ(e.target.value.toUpperCase())}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setIdx((i) => Math.min(hits.length - 1, i + 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setIdx((i) => Math.max(0, i - 1));
          } else if (e.key === "Enter") {
            if (hits[idx]) pick(hits[idx].symbol);
          } else if (e.key === "Escape") {
            setOpen(false);
            (e.target as HTMLInputElement).blur();
          }
        }}
      />
      {open && q.length > 0 && (
        <ul className="typeahead" id="ticker-typeahead" role="listbox">
          {err ? (
            <li className="empty">{err}</li>
          ) : hits.length === 0 ? (
            <li className="empty">No optionable match for {q}</li>
          ) : (
            hits.map((h, i) => (
              <li key={h.symbol} role="option" aria-selected={i === idx} onMouseDown={() => pick(h.symbol)} onMouseEnter={() => setIdx(i)}>
                <b>{h.symbol}</b>
                <span>{h.name}</span>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}

interface Kpi {
  key: string;
  label: ReactNode;
  value: ReactNode;
  valueClass?: string;
  valueStyle?: CSSProperties;
  sub?: ReactNode;
  def?: ReactNode;
  wide?: boolean;
}

function Ref10() {
  return (
    <span className="ref10" title="Captured at 10:00 ET, not the current chain">
      10:00
    </span>
  );
}

function LevelsPanel({
  m,
  levels,
  frozen,
  layout,
  onExplain,
}: {
  m: GexMap | null;
  levels: ProfileLevels | null;
  frozen: boolean;
  layout: "grid" | "strip";
  onExplain?: () => void;
}) {
  if (!m || !levels) {
    return (
      <Panel title="Levels now">
        <div className={`kpis ${layout}`}>
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div className="kpi" key={i}>
              <Skeleton h={10} w={60} />
              <Skeleton h={22} w={90} />
              <Skeleton h={10} w={80} />
            </div>
          ))}
        </div>
      </Panel>
    );
  }
  const S = m.spot.price;
  const t = m.ticker;
  const reg = m.levels.regime;
  const ft = m.frozen_today;
  const shared = levels.call_wall !== null && levels.call_wall === levels.put_wall;
  const nowNote = (cur: number | null, fmt: (v: number | null) => string) => (frozen ? <>Now {fmt(cur)}.</> : null);

  const tiles: Kpi[] = [
    {
      key: "spot",
      label: "Spot",
      value: px(S, m.ticker),
      sub:
        m.spot.basis === "regular_close"
          ? m.spot.source === "cboe"
            ? "Regular close (Cboe)"
            : "Regular close"
          : `${m.spot.source.replace("alpaca_", "").toUpperCase()} at ${nyTime(m.spot.ts, true)}`,
    },
  ];
  if (shared) {
    tiles.push({
      key: "cpw",
      label: (
        <>
          <i style={{ color: "var(--green)" }} />
          <i style={{ color: "var(--purple-1)", marginLeft: -4 }} />
          Call + put walls
          {frozen && <Ref10 />}
        </>
      ),
      value: (
        <>
          <span className="g">{strike(levels.call_wall)}</span>
        </>
      ),
      sub: distText(levels.call_wall, S, t),
      def: (
        <>
          Both exposure peaks occur at this strike. {nowNote(m.levels.call_wall, strike)}
        </>
      ),
    });
  } else {
    tiles.push(
      {
        key: "cw",
        label: (
          <>
            <i style={{ color: "var(--green)" }} />
            Call wall
            {frozen && <Ref10 />}
          </>
        ),
        value: strike(levels.call_wall),
        valueClass: "g",
        sub: distText(levels.call_wall, S, t),
        def: frozen ? nowNote(m.levels.call_wall, strike) : m.signed ? undefined : "Largest call exposure.",
      },
      {
        key: "pw",
        label: (
          <>
            <i style={{ color: "var(--purple-1)" }} />
            Put wall
            {frozen && <Ref10 />}
          </>
        ),
        value: strike(levels.put_wall),
        valueStyle: { color: "var(--purple-1)" },
        sub: distText(levels.put_wall, S, t),
        def: frozen ? nowNote(m.levels.put_wall, strike) : m.signed ? undefined : "Largest put exposure.",
      },
    );
  }
  const callAbs = Math.abs(m.totals.call_gex);
  const putAbs = Math.abs(m.totals.put_gex);
  if (m.signed) {
    tiles.push(
      {
        key: "zg",
        label: (
          <>
            <i style={{ color: "var(--purple-2)" }} />
            Gamma flip
            {frozen && <Ref10 />}
            {!frozen && m.levels.zero_gamma.length > 1 && <span style={{ color: "var(--muted-2)" }}>nearest of {m.levels.zero_gamma.length}</span>}
          </>
        ),
        value: levels.nearest_zero_gamma === null ? "None" : px(levels.nearest_zero_gamma, m.ticker),
        valueClass: "p",
        sub: levels.nearest_zero_gamma === null ? "no crossing within 10%" : distText(levels.nearest_zero_gamma, S, t),
        def: (
          <>
            Where modeled net GEX crosses zero. {nowNote(m.levels.nearest_zero_gamma, (v) => (v === null ? "none" : px(v, m.ticker)))}
          </>
        ),
      },
      {
        key: "sign",
        label: "Modeled gamma sign",
        value: reg === "positive" ? "Positive" : reg === "negative" ? "Negative" : "Neutral",
        valueClass: "word",
        valueStyle: { color: reg === "positive" ? "var(--green)" : reg === "negative" ? "var(--purple-2)" : "var(--text)" },
        sub: "at current spot",
        def: "Positive does not mean bullish.",
      },
      {
        key: "net",
        label: "Net GEX",
        value: gex(m.totals.net_gex, { sign: true }),
        sub: (
          <>
            calls <span className="g">{gex(m.totals.call_gex)}</span> / puts <span className="p">{gex(m.totals.put_gex, { sign: true })}</span>
          </>
        ),
        def: GEX_UNITS,
      },
    );
  } else {
    tiles.push({
      key: "abs",
      label: "Total absolute GEX",
      value: gex(callAbs + putAbs),
      sub: (
        <>
          calls <span className="g">{gex(callAbs)}</span> + puts <span className="p">{gex(putAbs)}</span>
        </>
      ),
      def: GEX_UNITS,
    });
  }
  // No usable quotes: every current-chain value is unavailable, never a neutral sign or a $0 total. The 10:00
  // reference levels (frozen) come from the capture, so they stay.
  if (m.quotes?.usable === false) {
    const current = frozen ? ["sign", "net", "abs"] : ["cpw", "cw", "pw", "zg", "sign", "net", "abs"];
    for (const k of tiles) {
      if (!current.includes(k.key)) continue;
      Object.assign(k, { value: "Unavailable", valueClass: "word", valueStyle: { color: "var(--muted)" }, sub: "no usable quotes", def: undefined });
    }
  }
  if (layout === "grid" && tiles.length % 2 === 1) tiles[tiles.length - 1].wide = true;

  const title = frozen ? <span className="p">Current market with 10:00 reference levels</span> : m.signed ? "Levels now" : "Unsigned exposure";
  const right =
    !frozen && ft ? (
      <span>
        10:00 capture {ft.call_wall !== null && ft.call_wall === ft.put_wall ? `walls ${strike(ft.call_wall)}` : `${strike(ft.call_wall)} / ${strike(ft.put_wall)}`}
      </span>
    ) : frozen && ft ? (
      <span>
        captured {nyTime(ft.captured_at, true)} ET<span className={`cap-status ${ft.status ?? "pending"}`}>{ft.status ?? "status unknown"}</span>
      </span>
    ) : undefined;

  return (
    <Panel title={title} right={right} className="levels-panel">
      <div className={`kpis ${layout}`} style={layout === "strip" ? { gridTemplateColumns: `repeat(${tiles.length}, minmax(0, 1fr))` } : undefined}>
        {tiles.map((k) => (
          <div className={`kpi ${k.wide ? "wide" : ""}`} key={k.key}>
            <small>{k.label}</small>
            <span className={`v ${k.valueClass ?? ""}`} style={k.valueStyle}>
              {k.value}
            </span>
            {k.sub && <span className="sub">{k.sub}</span>}
            {k.def && <span className="def">{k.def}</span>}
          </div>
        ))}
      </div>
      {m.signed && m.magnitude && <MagnitudeRef mg={m.magnitude} layout={layout} onExplain={onExplain} />}
      {!m.signed && (
        <p className="kpi-note">
          <b>{UNSIGNED_NOTE}</b> {SIDES_NOTE}
        </p>
      )}
    </Panel>
  );
}

// ---------- net GEX size: share of average daily dollar volume, on a neutral log scale ----------
const MAG_LO = 0.1; // % of ADV at the left edge
const MAG_HI = 20; // % of ADV at the right edge
function magPos(p: number): number {
  const c = Math.min(MAG_HI, Math.max(MAG_LO, p));
  return ((Math.log10(c) - Math.log10(MAG_LO)) / (Math.log10(MAG_HI) - Math.log10(MAG_LO))) * 100;
}
function sharePct(p: number): string {
  if (p < 0.01) return "<0.01%";
  if (p < 0.1) return `${p.toFixed(2)}%`;
  if (p < 10) return `${p.toFixed(1)}%`;
  return `${p.toFixed(0)}%`;
}
function ordinal(n: number): string {
  const r = Math.round(n);
  const t = r % 100;
  const suf = t >= 11 && t <= 13 ? "th" : r % 10 === 1 ? "st" : r % 10 === 2 ? "nd" : r % 10 === 3 ? "rd" : "th";
  return `${r}${suf}`;
}
function percentileText(mg: Magnitude): string {
  return mg.percentile === null ? `building history (${mg.history_n} of ${mg.history_min})` : `${ordinal(mg.percentile)} (of ${mg.history_n} sessions)`;
}

// Per 1% or per typical day (1 ATR): remembered per browser. Storage can be blocked, so every access is guarded.
type MagMode = "pct" | "atr";
const MAG_MODE_KEY = "gexlab.magMode";
function readMagMode(): MagMode {
  try {
    return window.localStorage.getItem(MAG_MODE_KEY) === "atr" ? "atr" : "pct";
  } catch {
    return "pct";
  }
}
function writeMagMode(v: MagMode) {
  try {
    window.localStorage.setItem(MAG_MODE_KEY, v);
  } catch {
    /* storage blocked: the choice still holds for this page view */
  }
}
const PER_ATR_TIP =
  "Per typical day (1 ATR): the hedge per 1% times the 14-session average true range in percent of price. A linear approximation: it sizes an ordinary day for this ticker, so a quiet index and a volatile stock compare on more equal terms.";
const NET_GROSS_TIP =
  "Net to gross: net GEX as a share of calls plus puts counted without signs. A low ratio means calls and puts largely offset each other; check the Unsigned view.";
const INDEX_TIP =
  "Index products read larger: daily expiries, put heavy positioning so less nets out, lower IV, and a 1% move is a large day for an index but a small one for a volatile stock.";
function magTooltip(mg: Magnitude): string {
  return [mg.explainer, PER_ATR_TIP, NET_GROSS_TIP, mg.index_note ? INDEX_TIP : ""].filter(Boolean).join("\n\n");
}
function ratioPct(r: number): string {
  const v = r * 100;
  return v < 10 ? `${v.toFixed(1)}%` : `${v.toFixed(0)}%`;
}

function MagnitudeRef({ mg, layout, onExplain }: { mg: Magnitude; layout: "grid" | "strip"; onExplain?: () => void }) {
  const [mode, setModeState] = useState<MagMode>(readMagMode);
  const setMode = (v: MagMode) => {
    setModeState(v);
    writeMagMode(v);
  };
  const a = mg.per_atr ?? null;
  const atrPct = a?.atr_pct ?? null;
  const atr = mode === "atr" && atrPct !== null;
  const p = atr ? a?.pct_of_adv_per_atr ?? null : mg.pct_of_adv;
  const dollars = atr ? a?.hedge_per_atr_usd ?? null : mg.hedge_per_1pct_usd;
  const noRef = mg.adv_basis === "none";
  const unit = atr && atrPct !== null ? `per typical day (1 ATR ${"\u2248"} ${atrPct.toFixed(1)}%${a?.atr_basis === "etf" ? `, ${a.atr_label}` : ""})` : "per 1% move";
  const g = mg.gross ?? null;
  const gp = g ? (atr ? g.pct_of_adv_per_atr : g.pct_of_adv) : null;
  const gUsd = g ? (atr ? g.hedge_per_atr_usd : g.per_1pct_usd) : null;
  const gUnit = atr ? "per typical day" : "per 1% move";
  return (
    <div className={`mag ${layout}${noRef ? " no-ref" : ""}`}>
      <div className="mag-text">
        <div className="mag-head">
          <small>
            Net GEX size
            <button type="button" className="mag-info" title={magTooltip(mg)} aria-label="What the size of net GEX means" onClick={onExplain}>
              <Icon name="info" size={12} />
            </button>
          </small>
          <Seg<MagMode>
            label="Net GEX size unit"
            value={atr ? "atr" : "pct"}
            onChange={setMode}
            options={[
              { value: "pct", label: "Per 1%" },
              {
                value: "atr",
                label: "Per typical day (1 ATR)",
                disabled: atrPct === null,
                title: atrPct === null ? `ATR unavailable: ${a?.atr_reason ?? "no data"}` : PER_ATR_TIP,
              },
            ]}
          />
        </div>
        {noRef ? (
          <span className="mag-line">
            <b>{"\u2248"} {gex(dollars)}</b> hedge {unit}. <span className="muted">{mg.adv_label}.</span>
          </span>
        ) : p !== null ? (
          <span className="mag-line">
            <b>{"\u2248"} {sharePct(p)}</b> of {mg.adv_label} {unit}
          </span>
        ) : (
          <span className="mag-line muted">Daily dollar volume unavailable: {mg.adv_reason ?? "no data"}.</span>
        )}
        {g && (
          <span className="mag-gross">
            Gross (calls + puts, unsigned): {gp !== null ? <>{"\u2248"} {sharePct(gp)} of {mg.adv_label} {gUnit}</> : <>{"\u2248"} {gex(gUsd)} {gUnit}</>}
            {g.net_to_gross !== null && <> {"\u00b7"} net is {ratioPct(g.net_to_gross)} of gross</>}
          </span>
        )}
        <span className="mag-pctl">Percentile vs your recorded sessions: {percentileText(mg)}</span>
      </div>
      {!noRef && (
      <div className="mag-scale" role="img" aria-label={p === null ? "No volume reference" : `${sharePct(p)} of average daily dollar volume ${atr ? "per typical day" : "per 1% move"}, log scale from 0.1% to 20%`}>
        <div className="mag-track">
          {p !== null && <i className="mag-fill" style={{ width: `${magPos(p)}%` }} />}
          {mg.ticks_pct.map((t) => (
            <span key={t} className="mag-tick" style={{ left: `${magPos(t)}%` }} />
          ))}
          {p !== null && <span className="mag-mark" style={{ left: `${magPos(p)}%` }} />}
        </div>
        <div className="mag-ticks" aria-hidden>
          {mg.ticks_pct.map((t) => (
            <span key={t} style={{ left: `${magPos(t)}%` }}>
              {t}%
            </span>
          ))}
        </div>
      </div>
      )}
    </div>
  );
}

function ExpiryPanel({ m, exps }: { m: GexMap | null; exps: Expiration[] }) {
  const byDate = new Map(exps.map((e) => [e.date, e]));
  const totalContracts = m ? m.by_expiry.reduce((a, r) => a + r.contracts, 0) : 0;
  const totalGex = m ? m.by_expiry.reduce((a, r) => a + r.net_gex, 0) : 0;
  return (
    <Panel title="By expiry" right={m ? <span>{m.signed ? "signed" : "unsigned"}</span> : undefined}>
      {!m ? (
        <div style={{ display: "grid", gap: 10 }}>
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} h={14} />
          ))}
        </div>
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th>Expiry</th>
              <th>Contracts</th>
              <th>Valid</th>
              <th>{m.signed ? "Net GEX" : "Total abs GEX"}</th>
            </tr>
          </thead>
          <tbody>
            {m.by_expiry.map((r) => {
              const e = byDate.get(r.expiry);
              return (
                <tr key={r.expiry}>
                  <td>
                    <span style={{ display: "inline-block", minWidth: 46 }}>{e ? expLabel(e) : ""}</span>
                    <span className="muted">{dayShort(r.expiry)}</span>
                    {e && hasAM(e) && <span className="p" style={{ marginLeft: 6, fontSize: 10 }}>AM</span>}
                  </td>
                  <td>{int(r.contracts)}</td>
                  <td>{pct(r.valid_pct)}</td>
                  <td style={{ color: !m.signed ? "var(--text)" : r.net_gex >= 0 ? "var(--green)" : "var(--purple-2)" }}>{gex(r.net_gex, { sign: m.signed })}</td>
                </tr>
              );
            })}
          </tbody>
          {m.by_expiry.length > 1 && (
            <tfoot>
              <tr>
                <td className="lbl">All selected</td>
                <td>{int(totalContracts)}</td>
                <td>{pct(m.coverage.pct)}</td>
                <td>{gex(totalGex, { sign: m.signed })}</td>
              </tr>
            </tfoot>
          )}
        </table>
      )}
      {m && <Coverage m={m} />}
    </Panel>
  );
}

/** The gate a capture would be judged by: the |GEX| share within 5% under 1.2. Older payloads fall back to the OI share
 *  (1.1) or the contract count (1.0); the other numbers are shown as diagnostics. */
function Coverage({ m }: { m: GexMap }) {
  const c = m.coverage;
  const band = c.oi_band_pct ?? 5;
  const hasGex = c.gex_pct !== undefined;
  const hasOi = c.oi_pct !== undefined && c.oi_pct !== null;
  const gate = c.gate_version ?? (hasGex ? CURRENT_GATE : hasOi ? "1.1" : "1.0");
  const min = c.gate_pct ?? gateMin(gate);
  const v = hasGex ? (c.gex_pct ?? null) : hasOi ? (c.oi_pct as number) : c.pct;
  const ok = v !== null && v >= min;
  return (
    <div style={{ display: "grid", gap: 8, paddingTop: 4 }}>
      <div className="panel-h">
        <span>{hasGex ? `|GEX| coverage within ${band}%` : hasOi ? `OI coverage within ${band}%` : "Coverage"}</span>
        <span className="r" style={{ color: "var(--text-2)" }}>
          {v === null ? "n/a" : pct(v, hasGex || hasOi ? 2 : 1)}
          {!ok && " · below gate"}
        </span>
      </div>
      <div style={{ position: "relative" }}>
        <div className="meter" style={{ height: 8 }}>
          <i style={{ width: `${Math.min(100, v ?? 0)}%`, background: ok ? "var(--green)" : "var(--text-2)" }} />
        </div>
        <span style={{ position: "absolute", left: `${min}%`, top: -4, bottom: -4, borderLeft: "1px dashed var(--text)", opacity: 0.6 }} title={`${min}% capture gate`} />
      </div>
      <div className="legend" style={{ justifyContent: "space-between" }}>
        <span>
          {hasGex
            ? v === null
              ? `No usable or estimable contracts within ${band}% of spot: the share cannot be computed`
              : `Valid contracts hold this share of the estimated |GEX| within ${band}% of spot (selected expiries)`
            : hasOi
              ? `Valid contracts hold this share of the open interest within ${band}% of spot (missing OI counted)`
              : `${int(c.valid)} of ${int(c.eligible)} within 10% of spot have valid OI and IV`}
        </span>
        <span>
          gate {gate} {min}%
        </span>
      </div>
      {(hasGex || hasOi) && (
        <div className="legend">
          <span>
            Diagnostics: {hasGex && hasOi ? `OI share within ${band}% ${pct(c.oi_pct as number, 2)}, ` : ""}contract count {int(c.valid)} of {int(c.eligible)} within 10% of spot valid ({pct(c.pct)})
          </span>
        </div>
      )}
    </div>
  );
}

function ProfileSkeleton({ height }: { height: number }) {
  const rows = 34;
  return (
    <div style={{ height, display: "grid", alignContent: "space-evenly", padding: "8px 0 30px 62px" }} aria-busy="true" aria-label="Loading map">
      {Array.from({ length: rows }).map((_, i) => {
        const a = Math.abs(Math.sin(i * 1.7)) * 30 + 4;
        const b = Math.abs(Math.cos(i * 1.3)) * 30 + 4;
        return (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 0, height: 8, paddingRight: 150 }}>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <div className="skel" style={{ width: `${b}%`, height: 8 }} />
            </div>
            <div>
              <div className="skel" style={{ width: `${a}%`, height: 8 }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function MethodBody({ m, ticker }: { m: GexMap | null; ticker: string }) {
  return (
    <>
      <section>
        <h3>Formula</h3>
        <div className="formula">
          {"Gamma = N'(d1) / (S · IV · √T)\n"}
          {"GEX   = sign · Gamma · OI · 100 · S² · 0.01\n"}
          <span className="c">{`units = ${GEX_UNITS} (${ticker})\n`}</span>
          <span className="c">{"sign  = +1 calls, -1 puts (an assumption)"}</span>
        </div>
        <p>
          Black-Scholes gamma with r = q = 0, a stated approximation. T is calendar seconds to settlement divided by one year. Expired contracts are excluded. Model v{m?.model_version ?? "1.0"}.
        </p>
      </section>
      <section>
        <h3>The sign assumption</h3>
        <p>
          The signed view treats calls as positive gamma and puts as negative, as if dealers were long calls and short puts. Nobody observes dealer positions directly, so this is a model choice, not a fact. The unsigned view drops the sign and shows where gamma sits; direction is not modeled there, and left versus right only separates calls from puts.
        </p>
      </section>
      <section>
        <h3>Levels</h3>
        <ul>
          <li>
            <b>Call wall</b>: the strike with the largest summed call GEX.
          </li>
          <li>
            <b>Put wall</b>: the strike with the largest summed put GEX, by size. When both peaks land on one strike the map shows one shared annotation, "Call + put walls".
          </li>
          <li>
            <b>Gamma flip</b>: where modeled net GEX crosses zero. Total signed GEX is recalculated at hypothetical prices across ±10%, IV, OI and time held fixed. Every sign change is a crossing. The one nearest spot is highlighted, the rest are drawn faint. No crossing is a valid result.
          </li>
          <li>
            <b>Modeled gamma sign</b>: the sign of net GEX at the current spot under the sign assumption. Positive does not mean bullish.
          </li>
          <li>Ties go to the strike nearest spot, then the lower strike.</li>
        </ul>
      </section>
      <MagnitudeSection m={m} />
      <section>
        <h3>Net GEX at hypothetical prices</h3>
        <p>
          An optional panel beside the bars. {CURVE_NOTE} It shares the bars' vertical scale, but reads as underlying price, not strike, and has its own dollar axis. It crosses zero at the gamma flip.
        </p>
      </section>
      <section>
        <h3>This view</h3>
        {m ? (
          <dl className="kv">
            <dt>Feed</dt>
            <dd>{m.feed.label}</dd>
            <dt>Spot</dt>
            <dd>
              {m.spot.basis === "regular_close"
                ? m.spot.source === "cboe"
                  ? `${price(m.spot.price)}, the regular-session close from the Cboe delayed payload${m.feed.data_ts ? ` stamped ${nyWeekday(m.feed.data_ts)} ${nyTime(m.feed.data_ts, true)} ET` : ""}`
                  : `${price(m.spot.price)}, the regular-session close (${m.spot.source}); extended-hours prints are ignored`
                : `${price(m.spot.price)} from ${m.spot.source} at ${nyTime(m.spot.ts, true)} ET`}
            </dd>
            <dt>Valued at</dt>
            <dd>
              {m.valuation?.label ?? `${nyTime(m.valuation?.ts ?? m.as_of, true)} ET`}. Time to expiry runs from this clock, not the wall clock.
            </dd>
            <dt>OI date</dt>
            <dd>{dayFull(m.oi_date)}. Open interest is published once a day, one session behind, for every tool.</dd>
            <dt>OI source</dt>
            <dd>{oiSourceText(m)}</dd>
            <dt>Expiries</dt>
            <dd>
              {m.by_expiry.length} selected. The map opens on the nearest expiry that has not settled (0DTE on SPY, QQQ, SPX and NDX weekdays, the closest listed expiry for a stock;
              after the day's settlement, the next one). Add expiries with the chips or All; the pick holds until you switch tickers. The 10:00 capture and the studies always use every
              expiry in the 0 to 7 day window.
            </dd>
            <dt>Coverage</dt>
            <dd>
              {m.coverage.gex_pct != null &&
                `Valid contracts held ${pct(m.coverage.gex_pct, 2)} of the estimated |GEX| within ${m.coverage.oi_band_pct ?? 5}% of spot for the selected expiries, the number the ${m.coverage.gate_pct ?? gateMin(CURRENT_GATE)}% capture gate (${m.coverage.gate_version ?? CURRENT_GATE}) reads; the capture itself is judged on every expiry. `}
              {m.coverage.oi_pct != null && `Their share of the open interest in that band: ${pct(m.coverage.oi_pct, 2)}. `}
              {int(m.coverage.valid)} of {int(m.coverage.eligible)} contracts within 10% of spot ({pct(m.coverage.pct)}) had usable OI and IV
              {m.coverage.gex_pct != null || m.coverage.oi_pct != null ? ". Both are diagnostics only." : "."}
            </dd>
            <dt>As of</dt>
            <dd>{nyTime(m.as_of, true)} ET</dd>
          </dl>
        ) : (
          <p>Waiting for data.</p>
        )}
      </section>
      <section>
        <h3>Feed labels</h3>
        <ul>
          <li>
            <b>OPRA quotes, refreshed 60s</b>: consolidated option quotes. IV is solved from the bid and ask midpoint, falling back to indicative IV when the midpoint is unusable.
          </li>
          <li>
            <b>Indicative quotes, modified; trades delayed 15m</b>: the free Alpaca option feed. IV and greeks come from that feed.
          </li>
          <li>
            <b>Cboe delayed quotes (about 15 min)</b>: SPX and NDX. Spot is the delayed index level in the same file.
          </li>
          <li>When no refresh has succeeded for 3 minutes, or the market is closed, that state replaces the label.</li>
        </ul>
      </section>
      <section>
        <h3>Settlement</h3>
        <ul>
          <li>PM settled (SPY, QQQ, stocks, SPXW, NDXP): 16:00 ET on the expiration date, early closes respected.</li>
          <li>AM settled (SPX and NDX monthly roots): 09:30 ET on the expiration date. Marked AM on the expiry chips.</li>
        </ul>
      </section>
      <section>
        <h3>What this is not</h3>
        <p>Open interest is end of day, so this map cannot show intraday positioning. It describes where gamma sits under one assumption. It makes no forecast.</p>
      </section>
    </>
  );
}

const WEEKDAY = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short" });
function nyWeekday(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : WEEKDAY.format(d);
}

const MAG_EXPLAINER =
  "Net GEX estimates how many dollars of the underlying dealers would have to trade, under the sign assumption, for each 1% move. Compare it to how much normally trades: a few percent of daily volume is noticeable; a fraction of a percent is usually lost in normal flow. The size is a model estimate, not a measured flow.";

function MagnitudeSection({ m }: { m: GexMap | null }) {
  const mg = m?.magnitude ?? null;
  const a = mg?.per_atr ?? null;
  const g = mg?.gross ?? null;
  return (
    <section>
      <h3>Size of net GEX</h3>
      <p>{mg?.explainer ?? MAG_EXPLAINER}</p>
      <p>
        The reference is average daily dollar volume: volume times the bar VWAP from daily consolidated (SIP) bars, averaged over the last 20 completed sessions. The scale under the levels marks 1%, 2%, 5% and 10% of it on a log axis.
        It is descriptive: no size is labeled good or bad.
      </p>
      {m && mg && (
        <>
          <table className="tbl mag-table">
            <thead>
              <tr>
                <th>If price moves</th>
                <th>Hedge flow (model)</th>
                <th>Share of daily volume</th>
              </tr>
            </thead>
            <tbody>
              {mg.hedge_for_move.map((r) => (
                <tr key={r.move_pct}>
                  <td>{r.move_pct}%</td>
                  <td>{gex(r.usd)}</td>
                  <td>{mg.adv_usd ? sharePct((r.usd / mg.adv_usd) * 100) : mg.adv_basis === "none" ? "no reference" : "n/a"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small-note">{mg.hedge_for_move_method}</p>
          <dl className="kv">
            <dt>Net GEX</dt>
            <dd>{gex(mg.hedge_per_1pct_usd)} per 1% move: the absolute signed net of the selected expiries.</dd>
            <dt>Daily dollar volume</dt>
            <dd>
              {mg.adv_basis === "none" ? (
                <>{mg.adv_label}. {m.ticker} options have no share volume, and SPY or QQQ volume does not measure them.</>
              ) : mg.adv_usd ? (
                <>
                  {gex(mg.adv_usd)}
                  {mg.adv_window?.first && mg.adv_window?.last ? `, average of ${mg.adv_sessions ?? 20} sessions ${dayShort(mg.adv_window.first)} to ${dayShort(mg.adv_window.last)}` : ""}
                  {mg.adv_source === "demo" ? " (synthetic fixture)" : ""}.
                </>
              ) : (
                <>Unavailable: {mg.adv_reason ?? "no data"}.</>
              )}
            </dd>
            <dt>Percentile</dt>
            <dd>
              {percentileText(mg)}. {mg.percentile_basis} Shown once {mg.history_min} eligible captures exist.
            </dd>
          </dl>
        </>
      )}
      <h3 className="mag-sub">Per typical day (1 ATR)</h3>
      <p>
        A 1% move is a big day for an index and a small one for a volatile stock. The per typical day view scales the hedge by the ticker's own average true range: hedge per 1% times ATR in percent of price. ATR is the simple mean true range of the last 14
        completed sessions, from split adjusted daily consolidated bars, cached once per session. It is a linear approximation.
      </p>
      {a && (
        <dl className="kv">
          <dt>ATR</dt>
          <dd>
            {a.atr_pct !== null ? (
              <>
                {a.atr_pct.toFixed(2)}% of {a.atr_pct_basis === "spot" ? "spot" : `${a.atr_symbol}'s last close`}
                {a.atr_window?.first && a.atr_window?.last ? `, ${a.atr_sessions ?? 14} sessions ${dayShort(a.atr_window.first)} to ${dayShort(a.atr_window.last)}` : ""}
                {a.atr_source === "demo" ? " (synthetic fixture)" : ""}.{a.atr_basis === "etf" ? ` ${a.atr_label}.` : ""}
              </>
            ) : (
              <>Unavailable: {a.atr_reason ?? "no data"}.</>
            )}
          </dd>
          {a.atr_note && (
            <>
              <dt>Index</dt>
              <dd>{a.atr_note}</dd>
            </>
          )}
          <dt>Hedge, 1 ATR</dt>
          <dd>
            {a.hedge_per_atr_usd !== null ? gex(a.hedge_per_atr_usd) : "n/a"}
            {a.pct_of_adv_per_atr !== null ? `, ${sharePct(a.pct_of_adv_per_atr)} of daily dollar volume` : mg?.adv_basis === "none" ? ", dollars only (no volume reference)" : ""}.
          </dd>
        </dl>
      )}
      {a && <p className="small-note">{a.method}</p>}
      <h3 className="mag-sub">Gross and net to gross</h3>
      <p>{g?.note ?? "Gross adds call GEX and put GEX without signs, same expiries. A low net to gross ratio means calls and puts largely offset each other: check the Unsigned view."}</p>
      {g && (
        <dl className="kv">
          <dt>Gross</dt>
          <dd>
            {gex(g.per_1pct_usd)} per 1% move{g.pct_of_adv !== null ? `, ${sharePct(g.pct_of_adv)} of daily dollar volume` : ", dollars only"}.
          </dd>
          <dt>Net to gross</dt>
          <dd>{g.net_to_gross !== null ? `Net is ${ratioPct(g.net_to_gross)} of gross.` : "n/a"}</dd>
        </dl>
      )}
      <h3 className="mag-sub">Why indexes read larger</h3>
      <p>
        {mg?.index_note ??
          "Index products read larger than single stocks: daily expiries keep a lot of short dated gamma open, index positioning is put heavy so less of it nets out, implied volatility is lower, and a 1% move is a large day for an index but a small one for a volatile stock."}
      </p>
    </section>
  );
}
