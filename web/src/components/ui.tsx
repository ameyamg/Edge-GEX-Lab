import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ApiError } from "../api/client";
import { vocab } from "../lib/format";

// ---------- app-wide modes ----------
/** True when every number on screen is synthetic (API DEMO mode or the VITE_MOCK build). */
export const DemoContext = createContext(false);
/** Presentation layout for filming: larger type, key values first, secondary panels collapsed. */
export const PresentContext = createContext(false);
export const usePresent = () => useContext(PresentContext);

export const DEMO_LABEL = "DEMO: synthetic data, not market observations";

export function DemoTag() {
  const demo = useContext(DemoContext);
  if (!demo) return null;
  return (
    <span className="demo-tag" role="note">
      <Icon name="info" size={13} />
      {DEMO_LABEL}
    </span>
  );
}

// ---------- top bar meta slot: each screen publishes its own right-side status ----------
export interface TopMeta {
  node: ReactNode;
  progress?: number | null; // 0..1 refresh cycle, drawn as a hairline under the bar
}
export const MetaContext = createContext<(m: TopMeta) => void>(() => {});
export function useTopMeta(meta: TopMeta, deps: unknown[]) {
  const set = useContext(MetaContext);
  useEffect(() => {
    set(meta);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => () => set({ node: null, progress: null }), [set]);
}

// ---------- lead sentence: numbers set in mono, the first one in green ----------
const NUM = /(?<!(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Study) )(?<![\d:.,])([+-]?\$?\d[\d,]*(?:\.\d+)?%?[BMK]?)(?![\d:]|-minute)/g;

export function Lead({ text, loading }: { text: string | null | undefined; loading?: boolean }) {
  let body: ReactNode;
  if (!text) {
    body = loading ? <div className="skel" style={{ height: 30, width: "56%" }} /> : <p className="lead">&nbsp;</p>;
  } else {
    const t = vocab(text);
    const parts = t.split(NUM);
    let first = true;
    body = (
      <p className={`lead ${t.length > 170 ? "long" : ""}`} aria-live="polite">
        {parts.map((p, i) => {
          if (i % 2 === 1) {
            const hi = first;
            first = false;
            return (
              <span key={i} className={hi ? "n hi" : "n"}>
                {p}
              </span>
            );
          }
          return <span key={i}>{p}</span>;
        })}
      </p>
    );
  }
  return (
    <div className="lead-block">
      <DemoTag />
      {body}
    </div>
  );
}

export function Panel({
  title,
  right,
  children,
  className,
  style,
}: {
  title?: ReactNode;
  right?: ReactNode;
  children?: ReactNode;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <section className={`panel ${className ?? ""}`} style={style}>
      {(title || right) && (
        <div className="panel-h">
          <span>{title}</span>
          {right && <span className="r">{right}</span>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Skeleton({ h = 14, w = "100%", style }: { h?: number; w?: number | string; style?: React.CSSProperties }) {
  return <div className="skel" style={{ height: h, width: w, ...style }} />;
}

export function ErrorState({ error, onRetry, what }: { error: Error; onRetry?: () => void; what: string }) {
  const offline = error instanceof ApiError && error.status === 0;
  const notFound = error instanceof ApiError && error.status === 404;
  return (
    <div className="state" role="alert">
      <span className="kicker">{offline ? "API offline" : notFound ? "No data" : "Request failed"}</span>
      <h2>{offline ? `Can't load ${what}. The API isn't running.` : notFound ? `No ${what} for this selection.` : `Can't load ${what}.`}</h2>
      <p>{error.message}</p>
      {offline && (
        <p>
          Start the API with <code>launch.bat</code>, or run the UI against fixtures with <code>npm run dev:mock</code>.
        </p>
      )}
      {onRetry && (
        <button className="btn" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Seg<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: ReactNode; disabled?: boolean; title?: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          disabled={o.disabled}
          title={o.title}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Modal side drawer. Rendered into <body> so the whole app root can be made inert while it is open:
 * background controls cannot be clicked, focused or read by a screen reader. Tab and Shift+Tab cycle
 * inside the drawer, Escape closes it, and focus returns to whatever opened it.
 */
export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const root = document.getElementById("root");
    root?.setAttribute("inert", "");
    root?.setAttribute("aria-hidden", "true");
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.getClientRects().length > 0);
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (!active || !panel.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      root?.removeAttribute("inert");
      root?.removeAttribute("aria-hidden");
      prev?.focus?.();
    };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <>
      <div className="scrim" onClick={onClose} aria-hidden />
      <aside ref={panelRef} className="drawer" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}>
        <header>
          <h2>{title}</h2>
          <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </header>
        <div className="body">{children}</div>
      </aside>
    </>,
    document.body,
  );
}

export function Icon({ name, size = 14 }: { name: "search" | "close" | "pause" | "play" | "left" | "right" | "lock" | "refresh" | "info"; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.6, "aria-hidden": true } as const;
  switch (name) {
    case "search":
      return (
        <svg {...common} width={12} height={12}>
          <circle cx="7" cy="7" r="4.5" />
          <path d="M10.5 10.5 14 14" />
        </svg>
      );
    case "close":
      return (
        <svg {...common}>
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      );
    case "pause":
      return (
        <svg {...common}>
          <path d="M5.5 3.5v9M10.5 3.5v9" />
        </svg>
      );
    case "play":
      return (
        <svg {...common}>
          <path d="M5 3.5v9l7-4.5z" fill="currentColor" stroke="none" />
        </svg>
      );
    case "left":
      return (
        <svg {...common}>
          <path d="M10 3.5 5.5 8l4.5 4.5" />
        </svg>
      );
    case "right":
      return (
        <svg {...common}>
          <path d="M6 3.5 10.5 8 6 12.5" />
        </svg>
      );
    case "lock":
      return (
        <svg {...common} width={16} height={16}>
          <rect x="3.5" y="7" width="9" height="6.5" rx="1" />
          <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
        </svg>
      );
    case "refresh":
      return (
        <svg {...common}>
          <path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v2.5h-2.5" />
        </svg>
      );
    case "info":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" />
          <path d="M8 7.2v4M8 5v.2" />
        </svg>
      );
  }
}

export function Meter({ value, max, tone = "green", gate }: { value: number; max: number; tone?: "green" | "purple" | "gray"; gate?: number }) {
  const w = Math.max(0, Math.min(1, max > 0 ? value / max : 0)) * 100;
  return (
    <div className={`meter ${tone === "green" ? "" : tone}`} role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}>
      <i style={{ width: `${w}%` }} />
      {gate !== undefined && <span className="gate" style={{ left: `${(gate / max) * 100}%` }} />}
    </div>
  );
}
