// Capture eligibility gate per the contract's "Capture protocol v1.3".
// Gate 1.3 (current, from 2026-10-20): gate 1.2 plus open interest from the previous session, unestimable |GEX|
// charged at a conservative bound, and closing quotes judged on the valuation clock. Same number and threshold.
// Gate 1.2 (2026-10-19 11:15 to the close): valid contracts hold >= 98% of the estimated absolute GEX within 5% of spot.
// Gate 1.1 (2026-10-19, from the 10:15 snapshot until 1.2): valid contracts hold >= 95% of the open interest within 5% of spot.
// Gate 1.0 (rows before that, gate_version "1.0" or absent): valid OI and IV on >= 95% of contracts within 10%.
// A capture keeps the status its own gate gave it, so each row is read against the number and threshold that judged it.

export const CURRENT_GATE = "1.3";

const GATE_MIN: Record<string, number> = { "1.0": 95, "1.1": 95, "1.2": 98, "1.3": 98 };

interface GateFields {
  gate_version?: string | null;
  coverage_pct: number | null;
  oi_coverage_pct?: number | null;
  gex_coverage_pct?: number | null;
}

export function gateOf(r: GateFields): string {
  return r.gate_version ?? "1.0";
}

export function isCountGate(g: string): boolean {
  return g === "1.0";
}

export function isOiGate(g: string): boolean {
  return g === "1.1";
}

/** Threshold of a gate version, in percent. Unknown (newer) versions read as the current gate. */
export function gateMin(g: string): number {
  return GATE_MIN[g] ?? GATE_MIN[CURRENT_GATE];
}

/** The number the row's own gate read: |GEX| share for 1.2 and 1.3, OI share for 1.1, the contract count for 1.0. */
export function gatePct(r: GateFields): number | null {
  const g = gateOf(r);
  if (isCountGate(g)) return r.coverage_pct;
  if (isOiGate(g)) return r.oi_coverage_pct ?? null;
  return r.gex_coverage_pct ?? null;
}

/** Short name of what the gate measures. */
export function gateMetric(g: string): string {
  return isCountGate(g) ? "contracts within 10%" : isOiGate(g) ? "OI within 5%" : "|GEX| within 5%";
}

/** Short label used on calendar cells. */
export function gateTag(g: string): string {
  return isCountGate(g) ? "ct" : isOiGate(g) ? "OI" : "GEX";
}

/** What the gate number is a share of, as a sentence fragment after the percent. */
export function gateMeaning(g: string): string {
  return isCountGate(g)
    ? "of contracts within 10% valid"
    : isOiGate(g)
      ? "of the OI within 5% on valid contracts"
      : "of the estimated |GEX| within 5% on valid contracts";
}
