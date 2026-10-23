import { useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../lib/hooks";

// A newer release is out: one line under the top bar with the version, the release notes and the update steps.
// Notify only, nothing is downloaded. Dismissing hides it until the next version (remembered per browser, guarded).
const DISMISSED_KEY = "gexlab.updateDismissed";

function readDismissed(): string | null {
  try {
    return window.localStorage.getItem(DISMISSED_KEY);
  } catch {
    return null;
  }
}

export default function UpdateBanner({ hidden }: { hidden: boolean }) {
  const info = useAsync(() => api.update(), []);
  const [dismissed, setDismissed] = useState<string | null>(readDismissed);
  const u = info.data;
  if (hidden || !u || !u.available || !u.latest || dismissed === u.latest) return null;
  const latest = u.latest;
  return (
    <div className="update-banner" role="status">
      <span>
        <b>Edge GEX Lab {latest} is out</b> (you have v{u.current}).{" "}
        <a href={u.url} target="_blank" rel="noreferrer">
          Release notes
        </a>
        . To update: <code>git pull</code> then <code>setup.bat</code>, or download the new zip, copy your <code>.env</code> across
        and run <code>setup.bat</code>.
      </span>
      <button
        type="button"
        className="btn ghost"
        onClick={() => {
          try {
            window.localStorage.setItem(DISMISSED_KEY, latest);
          } catch {
            /* storage blocked: hidden for this page view only */
          }
          setDismissed(latest);
        }}
      >
        Dismiss
      </button>
    </div>
  );
}
