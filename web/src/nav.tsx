// Hash routing, the global business date, and links between records.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { Job, scm } from "./scm";

export interface Route { path: string[]; query: URLSearchParams }

function parse(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [p, q = ""] = raw.split("?");
  return { path: p ? p.split("/").map(decodeURIComponent) : [], query: new URLSearchParams(q) };
}

export function useRoute(): Route {
  const [r, setR] = useState(parse);
  useEffect(() => {
    const on = () => {
      setR(parse());
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return r;
}

export const go = (to: string) => {
  window.location.hash = to.startsWith("/") ? to : `/${to}`;
};

// ---------------------------------------------------------------- business date
export const AsOfCtx = createContext<{ asOf: string; setAsOf: (d: string) => void; today: string }>({
  asOf: "2025-10-14", setAsOf: () => undefined, today: "2025-10-14",
});
export const useAsOf = () => useContext(AsOfCtx);

// ---------------------------------------------------------------- record links
const ROUTES: [RegExp, (id: string) => string][] = [
  [/^EVT\d+$/, (id) => `/disruptions/${id}`],
  [/^RPO\d+$/, (id) => `/pos/${id}`],
  [/^SUP\d+$/, (id) => `/suppliers/${id}`],
  [/^RM\d+$/, (id) => `/materials/${id}`],
  [/^DECL?\d+$/, (id) => `/decisions/${id}`],
  [/^PR\d+$/, (id) => `/production?q=${id}`],
  [/^(CMT|CMTL)\d+$/, (id) => `/commitments?q=${id}&status=all`],
  [/^PL\d+$/, (id) => `/production?plant=${id}`],
];

export const hrefFor = (id: string): string | null => {
  const hit = ROUTES.find(([re]) => re.test(id));
  return hit ? `#${hit[1](id)}` : null;
};

export function EntityLink({ id, children }: { id: string; children?: React.ReactNode }) {
  const href = hrefFor(id);
  return href ? <a className="elink" href={href}>{children ?? id}</a> : <span className="mono">{children ?? id}</span>;
}

/** Plain text with every record id turned into a link. */
export function Linkify({ text }: { text: string }) {
  const parts = text.split(/\b((?:EVT|RPO|SUP|RM|DECL|DEC|PR|CMTL|CMT|PL)\d{2,})\b/);
  return <>{parts.map((p, i) => (i % 2 ? <EntityLink key={i} id={p} /> : p))}</>;
}

// ---------------------------------------------------------------- data loading
export function useData<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    fn()
      .then((d) => live && setData(d))
      .catch((e) => live && setError(String((e as Error).message)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1), setData };
}

/** Start an agent job and poll it until it finishes. Re-attaches to a running job on re-render. */
export function useJob<T>() {
  const [job, setJob] = useState<Job<T> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const stop = () => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
  };
  useEffect(() => stop, []);

  const poll = useCallback((j: Job<T>) => {
    setJob(j);
    if (j.status === "queued" || j.status === "running") {
      timer.current = window.setTimeout(async () => {
        try {
          poll(await scm.job<T>(j.id));
        } catch (e) {
          setError(String((e as Error).message));
        }
      }, 1500);
    }
  }, []);

  const start = async (fn: () => Promise<Job<T>>) => {
    stop();
    setError(null);
    try {
      poll(await fn());
    } catch (e) {
      setError(String((e as Error).message));
    }
  };
  const running = job?.status === "queued" || job?.status === "running";
  return { job, error: error ?? (job?.status === "error" ? job.error : null), running, start, reset: () => { stop(); setJob(null); setError(null); } };
}
