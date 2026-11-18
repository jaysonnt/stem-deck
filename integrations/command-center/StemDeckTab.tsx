"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";
import { toast } from "@/lib/notify";
import { useVisiblePolling } from "@/lib/tabVisibility";

/**
 * Stem Deck — the pianokb stem player on the 9950x5080, driven by the BOYI
 * keyboard. Shows what is playing and mirrors every control the board has:
 * hold-to-reverse with a latch, the dial's tape speed, scrub, song and stem
 * gates. Commands go /api/stemdeck → pianokb ui/server.py → the engine.
 */

interface StemNow { name: string; on: boolean; key?: string }
interface DeckNow { name: string; playing: boolean; position: number; duration: number; stems: StemNow[] }
interface Now {
  active: boolean;
  preset?: string;
  song?: string;
  playing?: boolean;
  position?: number;
  duration?: number;
  reverse?: boolean;
  reverse_held?: boolean;
  reverse_latched?: boolean;
  speed?: number;
  last?: number;
  decks?: DeckNow[];
  updated?: number;
}
interface SongRow { name: string; stems: string[] }
interface Board {
  rows: string[];
  songs: SongRow[];
  benched: SongRow[];
  byKey: Record<string, { song: string; stem: string }>;
  capacity: { keys: number; stems: number; hidden: number };
  engine: { active: boolean; text: string };
  splitter: { ready: boolean; detail: string };
}
interface Job { id: string; state: string; stage?: string; progress?: number; log?: string[]; error?: string }

const HUES = [12, 200, 140, 280, 45, 330, 170, 95, 230];
const songHue = (i: number) => HUES[i % HUES.length];
const KEY_LABEL: Record<string, string> = { "⇥": "Tab", "⏎": "Enter", "⌫": "Bksp", "`": "`" };

function clock(sec?: number) {
  if (sec == null || !isFinite(sec)) return "–:––";
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

async function send(body: Record<string, unknown>) {
  try {
    await apiPost("/api/stemdeck/control", body);
  } catch (e) {
    toast.error("stem player", e instanceof Error ? e.message : String(e));
  }
}

/** Rotary knob that mirrors the BOYI dial: drag up/down or scroll to turn. */
function SpeedDial({ speed, onChange }: { speed: number; onChange: (v: number) => void }) {
  const drag = useRef<{ y: number; v: number } | null>(null);
  const min = 0.5, max = 2.0;
  const frac = (speed - min) / (max - min);
  const angle = -135 + frac * 270;
  const clamp = (v: number) => Math.round(Math.min(max, Math.max(min, v)) * 20) / 20;
  const arc = (a0: number, a1: number) => {
    const r = 42, cx = 50, cy = 50;
    const p = (a: number) => [cx + r * Math.sin((a * Math.PI) / 180), cy - r * Math.cos((a * Math.PI) / 180)];
    const [x0, y0] = p(a0), [x1, y1] = p(a1);
    return `M ${x0} ${y0} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1} ${y1}`;
  };
  const oneAngle = -135 + ((1 - min) / (max - min)) * 270;
  return (
    <div className="flex flex-col items-center gap-1 select-none">
      <svg
        viewBox="0 0 100 100"
        className="w-28 h-28 cursor-ns-resize touch-none"
        onPointerDown={(e) => { (e.target as Element).setPointerCapture?.(e.pointerId); drag.current = { y: e.clientY, v: speed }; }}
        onPointerMove={(e) => {
          if (!drag.current) return;
          const v = clamp(drag.current.v + (drag.current.y - e.clientY) / 160);
          if (v !== speed) onChange(v);
        }}
        onPointerUp={() => { drag.current = null; }}
        onWheel={(e) => onChange(clamp(speed + (e.deltaY < 0 ? 0.05 : -0.05)))}
        onDoubleClick={() => onChange(1)}
      >
        <path d={arc(-135, 135)} fill="none" stroke="rgb(var(--border2))" strokeWidth="7" strokeLinecap="round" />
        <path d={arc(Math.min(oneAngle, angle), Math.max(oneAngle, angle) + 0.01)} fill="none"
          stroke={speed === 1 ? "rgb(var(--dim))" : "rgb(var(--accent))"} strokeWidth="7" strokeLinecap="round" />
        <circle cx="50" cy="50" r="30" fill="rgb(var(--card2))" stroke="rgb(var(--border))" />
        <line x1="50" y1="50" x2="50" y2="25" stroke="rgb(var(--fg))" strokeWidth="3" strokeLinecap="round"
          transform={`rotate(${angle} 50 50)`} />
        <text x="50" y="72" textAnchor="middle" fontSize="11" fill="rgb(var(--fg))" fontFamily="monospace">
          {speed.toFixed(2)}x
        </text>
      </svg>
      <div className="flex gap-1">
        <button className="px-2 py-0.5 rounded bg-card2 border border-border" onClick={() => onChange(clamp(speed - 0.05))}>−</button>
        <button className="px-2 py-0.5 rounded bg-card2 border border-border text-dim" onClick={() => onChange(1)}>1.00x</button>
        <button className="px-2 py-0.5 rounded bg-card2 border border-border" onClick={() => onChange(clamp(speed + 0.05))}>+</button>
      </div>
      <div className="text-[10.5px] text-dim">dial = tape speed · press = 1.00x</div>
    </div>
  );
}

export function StemDeckTab() {
  const [now, setNow] = useState<Now | null>(null);
  const [board, setBoard] = useState<Board | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [speed, setSpeed] = useState(1);
  const speedHold = useRef(0);          // ignore polled speed briefly after a local change
  const [link, setLink] = useState({ song: "", url: "" });
  const [job, setJob] = useState<Job | null>(null);
  const [holding, setHolding] = useState(false);

  const loadNow = useCallback(async () => {
    try {
      const n = await apiGet<Now>("/api/stemdeck/now", { fresh: true });
      setNow(n); setErr(null);
      if (Date.now() > speedHold.current && typeof n.speed === "number") setSpeed(n.speed);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  }, []);
  const loadBoard = useCallback(async () => {
    try { setBoard(await apiGet<Board>("/api/stemdeck/state", { fresh: true })); } catch { /* now-poll shows the error */ }
  }, []);
  useVisiblePolling(loadNow, 300);
  useVisiblePolling(loadBoard, 20000);

  useEffect(() => {
    if (!job || job.state === "done" || job.state === "error") return;
    const t = setInterval(async () => {
      try {
        const j = await apiGet<Job>(`/api/stemdeck/jobs/${job.id}`, { fresh: true });
        setJob(j);
        if (j.state === "done") { toast.success("song split", "it is on the board"); loadBoard(); }
        if (j.state === "error") toast.error("split failed", j.error || "");
      } catch { /* keep trying */ }
    }, 1500);
    return () => clearInterval(t);
  }, [job, loadBoard]);

  const changeSpeed = (v: number) => {
    setSpeed(v);
    speedHold.current = Date.now() + 1200;
    void send({ cmd: "speed", value: v });
  };

  const decks = now?.decks ?? [];
  const lastIdx = Math.min(now?.last ?? 0, Math.max(0, decks.length - 1));
  const cur = decks[lastIdx];
  const reversed = !!now?.reverse;
  const latched = !!now?.reverse_latched;
  const songIndex = (name: string) => decks.findIndex((d) => d.name === name);

  const toggleStem = (song: string, stem: string, on: boolean) => send({ cmd: "stem", song, stem, on: !on });
  const toggleSong = (song: string) => send({ cmd: "song", song });

  const bench = async (song: string, back: boolean) => {
    try {
      await apiPost(back ? "/api/stemdeck/unbench" : "/api/stemdeck/bench", { song });
      toast.success(back ? "added to board" : "benched", `${song} — engine restarts`);
      setTimeout(loadBoard, 2500);
    } catch (e) { toast.error("bench", e instanceof Error ? e.message : String(e)); }
  };

  const splitLink = async () => {
    try {
      const r = await apiPost<{ ok: boolean; job: string }>("/api/stemdeck/split-url", { ...link, replace: false });
      setJob({ id: r.job, state: "queued" });
      setLink({ song: "", url: "" });
    } catch (e) { toast.error("add song", e instanceof Error ? e.message : String(e)); }
  };

  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!cur?.duration) return;
    const r = e.currentTarget.getBoundingClientRect();
    void send({ cmd: "seek", seconds: ((e.clientX - r.left) / r.width) * cur.duration });
  };

  const holdDown = () => { setHolding(true); void send({ cmd: "latch", on: true }); };
  const holdUp = () => { if (!holding) return; setHolding(false); void send({ cmd: "latch", on: false }); };

  const pct = cur?.duration ? ((cur.position ?? 0) / cur.duration) * 100 : 0;
  const hue = songHue(lastIdx);

  return (
    <div className="h-full overflow-y-auto bg-bg p-3 font-mono text-[12px] space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-semibold text-fg">Stem Deck</span>
        <span className="text-dim">BOYI keyboard · 9950x5080</span>
        <span className={`px-1.5 py-0.5 rounded text-[10.5px] ${board?.engine.active ? "bg-up/15 text-up" : "bg-down/15 text-down"}`}>
          engine {board?.engine.text ?? "…"}
        </span>
        {now?.preset && <span className="text-dim">preset: {now.preset}</span>}
        <button className="ml-auto text-dim hover:text-accent underline" onClick={async () => {
          try { await apiPost("/api/stemdeck/restart"); toast.success("engine restarted", ""); } catch (e) { toast.error("restart", String(e)); }
        }}>restart engine</button>
      </div>
      {err && <div className="text-down">{err}</div>}

      {/* ── transport ───────────────────────────────────────────── */}
      <div className="rounded-xl border border-border bg-card p-3 grid gap-4 md:grid-cols-[1fr_auto]">
        <div className="min-w-0 space-y-3">
          <div className="flex items-baseline gap-3 min-w-0">
            <span className={`text-[18px] leading-none ${reversed ? "text-warn" : "text-up"}`}>{reversed ? "◀◀" : "▶"}</span>
            <span className="text-[15px] text-fg truncate" style={{ color: cur ? `hsl(${hue} 70% 68%)` : undefined }}>
              {cur?.name ?? "nothing loaded"}
            </span>
            <span className="ml-auto text-dim shrink-0">{clock(cur?.position)} / {clock(cur?.duration)}</span>
          </div>
          <div className="h-3 rounded bg-card2 border border-border cursor-pointer relative overflow-hidden" onClick={seek} title="click to jump">
            <div className="absolute inset-y-0 left-0" style={{ width: `${pct}%`, background: `hsl(${hue} 60% 50% / .55)` }} />
          </div>
          <div className="flex flex-wrap gap-1.5">
            {cur?.stems.map((s) => (
              <button key={s.name} onClick={() => toggleStem(cur.name, s.name, s.on)}
                className={`px-2.5 py-1 rounded border ${s.on ? "text-bg" : "text-dim bg-card2 border-border"}`}
                style={s.on ? { background: `hsl(${hue} 65% 60%)`, borderColor: `hsl(${hue} 65% 60%)` } : undefined}>
                {s.name}{s.key ? <span className="opacity-60"> · {(KEY_LABEL[s.key] ?? s.key).toUpperCase()}</span> : null}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onPointerDown={holdDown} onPointerUp={holdUp} onPointerLeave={holdUp}
              className={`px-3 py-2 rounded border select-none touch-none ${holding || now?.reverse_held ? "bg-warn/25 border-warn text-warn" : "bg-card2 border-border text-fg"}`}>
              ◀◀ hold reverse <span className="text-dim">( &apos; )</span>
            </button>
            <button onClick={() => send({ cmd: "latch", on: !latched })}
              className={`px-3 py-2 rounded border ${latched ? "bg-warn/25 border-warn text-warn" : "bg-card2 border-border text-fg"}`}>
              {latched ? "reverse latched — unlatch" : "latch reverse"} <span className="text-dim">( &apos; + space )</span>
            </button>
            <button onClick={() => send({ cmd: "scrub", seconds: -5 })} className="px-3 py-2 rounded bg-card2 border border-border">−5s</button>
            <button onClick={() => send({ cmd: "scrub", seconds: 5 })} className="px-3 py-2 rounded bg-card2 border border-border">+5s</button>
            {cur && (
              <button onClick={() => toggleSong(cur.name)} className="px-3 py-2 rounded bg-accent/15 border border-accent/40 text-accent">
                {cur.stems.some((s) => s.on) ? "mute song" : "play whole song"} <span className="text-dim">( space )</span>
              </button>
            )}
          </div>
        </div>
        <SpeedDial speed={speed} onChange={changeSpeed} />
      </div>

      {/* ── keyboard ────────────────────────────────────────────── */}
      {board && (
        <div className="rounded-xl border border-border bg-card p-3 overflow-x-auto">
          <div className="text-dim mb-2">the board — lit keys are sounding · click a key to gate it</div>
          <div className="space-y-1 min-w-[560px]">
            {board.rows.map((row, ri) => (
              <div key={ri} className="flex gap-1" style={{ paddingLeft: ri < 4 ? ri * 14 : 0 }}>
                {[...row].map((ch) => {
                  const hit = board.byKey[ch];
                  const di = hit ? songIndex(hit.song) : -1;
                  const deck = di >= 0 ? decks[di] : undefined;
                  const on = !!deck?.stems.find((s) => s.name === hit?.stem)?.on;
                  const h = songHue(di >= 0 ? di : 0);
                  return (
                    <button key={ch} disabled={!hit}
                      onClick={() => hit && toggleStem(hit.song, hit.stem, on)}
                      title={hit ? `${hit.song} · ${hit.stem}` : "free key"}
                      className="w-[52px] h-[46px] rounded-md border text-left px-1 py-0.5 leading-tight transition-colors"
                      style={hit ? {
                        borderColor: `hsl(${h} 55% ${on ? 60 : 35}%)`,
                        background: on ? `hsl(${h} 65% 55%)` : `hsl(${h} 40% 16%)`,
                        color: on ? "rgb(var(--bg))" : `hsl(${h} 60% 72%)`,
                        boxShadow: on ? `0 0 14px -2px hsl(${h} 70% 55%)` : undefined,
                      } : { borderColor: "rgb(var(--border))", color: "rgb(var(--dim))" }}>
                      <div className="text-[11px] font-semibold">{(KEY_LABEL[ch] ?? ch).toUpperCase()}</div>
                      <div className="text-[9.5px] truncate">{hit?.stem ?? ""}</div>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <div className="mt-2 text-[10.5px] text-dim">
            &apos; hold = reverse · space while reversed = stay reversed · space = whole song · dial = speed, press = 1.00x / whole song · - = scrub · [ ] preset
            {board.capacity.hidden > 0 && <span className="text-warn"> · {board.capacity.hidden} stems have no key — bench a song</span>}
          </div>
        </div>
      )}

      {/* ── songs ───────────────────────────────────────────────── */}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {decks.map((d, i) => {
          const h = songHue(i);
          const p = d.duration ? (d.position / d.duration) * 100 : 0;
          return (
            <div key={d.name} className={`rounded-xl border bg-card p-2.5 space-y-2 ${i === lastIdx ? "border-accent/50" : "border-border"}`}>
              <div className="flex items-center gap-2 min-w-0">
                <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: `hsl(${h} 65% 58%)` }} />
                <span className="truncate text-fg" title={d.name}>{d.name}</span>
                <span className="ml-auto text-dim shrink-0">{clock(d.position)} / {clock(d.duration)}</span>
              </div>
              <div className="h-1 rounded bg-card2 overflow-hidden">
                <div className="h-full" style={{ width: `${p}%`, background: `hsl(${h} 60% 50%)` }} />
              </div>
              <div className="flex flex-wrap gap-1">
                {d.stems.map((s) => (
                  <button key={s.name} onClick={() => toggleStem(d.name, s.name, s.on)}
                    className={`px-1.5 py-0.5 rounded border text-[11px] ${s.on ? "text-bg" : "text-dim border-border"}`}
                    style={s.on ? { background: `hsl(${h} 65% 60%)`, borderColor: `hsl(${h} 65% 60%)` } : undefined}>
                    {s.name}
                  </button>
                ))}
              </div>
              <div className="flex gap-2 text-[11px]">
                <button className="text-accent" onClick={() => toggleSong(d.name)}>{d.stems.some((s) => s.on) ? "mute" : "play all"}</button>
                <button className="ml-auto text-dim hover:text-down" onClick={() => bench(d.name, false)}>bench</button>
              </div>
            </div>
          );
        })}
      </div>

      {/* ── library ─────────────────────────────────────────────── */}
      <div className="grid gap-3 md:grid-cols-2">
        <div className="rounded-xl border border-border bg-card p-3 space-y-2">
          <div className="text-fg">Add a song from a link</div>
          <div className="text-[10.5px] text-dim">yt-dlp downloads it, Demucs splits six stems on the 5080 · {board?.splitter.detail ?? "…"}</div>
          <input className="w-full bg-card2 border border-border rounded px-2 py-1 text-fg" placeholder="song name"
            value={link.song} onChange={(e) => setLink((l) => ({ ...l, song: e.target.value }))} />
          <input className="w-full bg-card2 border border-border rounded px-2 py-1 text-fg" placeholder="https://youtube.com/…"
            value={link.url} onChange={(e) => setLink((l) => ({ ...l, url: e.target.value }))} />
          <button disabled={!link.song || !link.url || !board?.splitter.ready}
            className="px-3 py-1.5 rounded bg-accent/20 text-accent disabled:opacity-40" onClick={splitLink}>split into stems</button>
          {job && (
            <div className="space-y-1">
              <div className="text-dim">{job.stage || job.state}{job.progress != null ? ` · ${Math.round(job.progress * 100)}%` : ""}</div>
              {job.progress != null && <div className="h-1 bg-card2 rounded"><div className="h-full bg-accent rounded" style={{ width: `${job.progress * 100}%` }} /></div>}
              {job.log && <pre className="text-[10px] text-dim max-h-28 overflow-y-auto whitespace-pre-wrap">{job.log.slice(-8).join("\n")}</pre>}
            </div>
          )}
        </div>
        <div className="rounded-xl border border-border bg-card p-3 space-y-2">
          <div className="text-fg">Not on the board</div>
          {!board?.benched.length && <div className="text-dim">nothing benched</div>}
          {board?.benched.map((s) => (
            <div key={s.name} className="flex items-center gap-2">
              <span className="truncate">{s.name}</span>
              <span className="text-dim text-[10.5px]">{s.stems.length} stems</span>
              <button className="ml-auto text-accent" onClick={() => bench(s.name, true)}>add to board</button>
            </div>
          ))}
          {board && <div className="text-[10.5px] text-dim pt-1">{board.capacity.stems} stems on {board.capacity.keys} keys</div>}
        </div>
      </div>
    </div>
  );
}
