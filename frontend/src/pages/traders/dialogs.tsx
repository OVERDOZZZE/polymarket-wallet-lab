// Trader dialogs: add, update data, settings (rename / market / delete), export.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Field, Modal, Seg, Switch, toast } from "../../components/ui";
import { api } from "../../lib/api";
import { coverageText, dateUTC, daysAgoUTC, todayUTC } from "../../lib/format";

const PRESETS: Record<string, number> = { "24 hours": 1, "7 days": 7, "30 days": 30, "90 days": 90 };

function RangePicker({ value, onChange, extra }: { value: { start: string; end: string; preset: string };
  onChange: (v: { start: string; end: string; preset: string }) => void; extra?: { label: string; start: string } }) {
  const opts = [...(extra ? [extra.label] : []), ...Object.keys(PRESETS), "Custom"];
  const pick = (p: string) => {
    if (extra && p === extra.label) onChange({ start: extra.start, end: todayUTC(), preset: p });
    else if (p in PRESETS) onChange({ start: daysAgoUTC(Math.max(PRESETS[p] - 1, 1)), end: todayUTC(), preset: p });
    else onChange({ ...value, preset: p });
  };
  return (
    <Field label="Period (UTC)">
      <Seg value={value.preset} options={opts} onChange={pick} />
      {value.preset === "Custom" && (
        <div className="row" style={{ marginTop: 8 }}>
          <input className="input" type="date" value={value.start} max={todayUTC()} onChange={(e) => onChange({ ...value, start: e.target.value })} />
          <span className="muted">to</span>
          <input className="input" type="date" value={value.end} max={todayUTC()} onChange={(e) => onChange({ ...value, end: e.target.value })} />
        </div>
      )}
    </Field>
  );
}

export function AddTrader({ onClose }: { onClose: () => void }) {
  const nav = useNavigate();
  const [ident, setIdent] = useState("");
  const [name, setName] = useState("");
  const [asset, setAsset] = useState("BTC");
  const [win, setWin] = useState(5);
  const [rng, setRng] = useState({ start: daysAgoUTC(6), end: todayUTC(), preset: "7 days" });
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function go() {
    setBusy(true);
    setErr("");
    try {
      const t = await api.post("/api/traders", { ident, name, asset, window_minutes: win, start: rng.start, end: rng.end, live });
      toast(`Downloading ${t.name}…`, "ok");
      onClose();
      nav(`/traders/${t.id}`);
    } catch (e: any) { setErr(e.message); }
    setBusy(false);
  }
  return (
    <Modal title="Add a trader" sub="Paste anything from Polymarket: the 0x wallet, the @username or the profile link. Data downloads in the background." onClose={onClose}
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !ident.trim()} onClick={go}>{busy ? "Checking…" : "Add and download"}</button></>}>
      <Field label="Wallet"><input className="input" autoFocus value={ident} onChange={(e) => setIdent(e.target.value)} placeholder="0x…  or  @username  or  polymarket.com/@username" /></Field>
      <Field label="Display name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Optional. Defaults to their Polymarket username" /></Field>
      <div className="row">
        <Field label="Asset"><select className="input" value={asset} onChange={(e) => setAsset(e.target.value)}>{["BTC", "ETH", "SOL", "XRP"].map((a) => <option key={a}>{a}</option>)}</select></Field>
        <Field label="Window"><select className="input" value={win} onChange={(e) => setWin(Number(e.target.value))}><option value={5}>5 minutes</option><option value={15}>15 minutes</option></select></Field>
      </div>
      <RangePicker value={rng} onChange={setRng} />
      <p className="hint">A busy bot makes about 5,000 trades a day; 100k trades take 1 to 2 minutes to download.</p>
      <label className="check"><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />Also start watching this wallet live</label>
      {err && <Alert kind="err">{err}</Alert>}
    </Modal>
  );
}

export function UpdateData({ t, onClose }: { t: any; onClose: () => void }) {
  const earliest = t.coverage.length ? dateUTC(t.coverage[0][0]) : daysAgoUTC(7);
  const [rng, setRng] = useState({ start: earliest, end: todayUTC(), preset: "Bring up to date" });
  const [prev, setPrev] = useState<any>(null);
  useEffect(() => {
    api.get(`/api/traders/${t.id}/download-preview?start=${rng.start}&end=${rng.end}`).then(setPrev).catch((e) => setPrev({ gaps: [], message: e.message }));
  }, [rng.start, rng.end, t.id]);
  async function go() {
    try { await api.post(`/api/traders/${t.id}/update`, { start: rng.start, end: rng.end }); toast("Download started", "ok"); onClose(); }
    catch (e: any) { toast(e.message, "err"); }
  }
  return (
    <Modal title="Update data" sub={`Stored so far: ${coverageText(t.coverage)} (UTC). Only missing time is downloaded; the analysis is refreshed afterwards.`} onClose={onClose}
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!prev?.gaps?.length} onClick={go}>Download</button></>}>
      <RangePicker value={rng} onChange={setRng} extra={{ label: "Bring up to date", start: earliest }} />
      {prev && <Alert kind={prev.gaps?.length ? "info" : "warn"}>{prev.message}</Alert>}
    </Modal>
  );
}

export function TraderSettings({ t, onClose }: { t: any; onClose: () => void }) {
  const nav = useNavigate();
  const [name, setName] = useState(t.name);
  const [asset, setAsset] = useState(t.asset);
  const [win, setWin] = useState(t.window_minutes);
  const [auto, setAuto] = useState(!!t.live_autostart);
  const [confirm, setConfirm] = useState("");
  async function save() {
    try {
      await api.patch(`/api/traders/${t.id}`, { name, asset, window_minutes: win, live_autostart: auto });
      toast("Saved" + ((asset !== t.asset || win !== t.window_minutes) ? ", re-analysing" : ""), "ok");
      onClose();
    } catch (e: any) { toast(e.message, "err"); }
  }
  async function del() {
    try { await api.del(`/api/traders/${t.id}`); toast(`${t.name} deleted`, "ok"); onClose(); nav("/traders"); }
    catch (e: any) { toast(e.message, "err"); }
  }
  return (
    <Modal title="Trader settings" onClose={onClose}
           footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!!t.job} onClick={save}>Save changes</button></>}>
      <Field label="Display name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
      <div className="row">
        <Field label="Asset"><select className="input" value={asset} onChange={(e) => setAsset(e.target.value)}>{["BTC", "ETH", "SOL", "XRP"].map((a) => <option key={a}>{a}</option>)}</select></Field>
        <Field label="Window"><select className="input" value={win} onChange={(e) => setWin(Number(e.target.value))}><option value={5}>5 minutes</option><option value={15}>15 minutes</option></select></Field>
      </div>
      <p className="hint">Changing asset or window re-analyses the stored activity; nothing is re-downloaded.</p>
      <div className="row"><Switch on={auto} onChange={setAuto} /><span>Watch live automatically whenever the app is running</span></div>
      <div className="sec">Folder</div>
      <div className="row"><code className="mono muted" style={{ overflowWrap: "anywhere" }}>data/traders/{t.id}</code><span className="spacer" /><button className="btn sm" onClick={() => api.post(`/api/traders/${t.id}/open-folder`)}>Open in Explorer</button></div>
      <div className="sec">Delete trader</div>
      <p className="hint">Removes the trader, its live events and its folder (activity, analysis, notes). Shared caches are kept.</p>
      <div className="row">
        <input className="input" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={`Type "${t.name}" to confirm`} />
        <button className="btn danger" disabled={confirm.trim() !== t.name || !!t.job} onClick={del}>Delete</button>
      </div>
    </Modal>
  );
}

export function ExportDialog({ ids, label, onClose }: { ids: string[]; label: string; onClose: () => void }) {
  const [prices, setPrices] = useState(false);
  const [mb, setMb] = useState<number | null>(null);
  useEffect(() => {
    Promise.all(ids.map((id) => api.get(`/api/traders/${id}/export-size?prices=${prices}`))).then((r) => setMb(r.reduce((a, x) => a + x.mb, 0))).catch(() => setMb(null));
  }, [ids.join(","), prices]);
  const url = ids.length === 1 ? `/api/traders/${ids[0]}/export?prices=${prices}` : `/api/export?ids=${ids.join(",")}&prices=${prices}`;
  return (
    <Modal title="Export data" sub={`A zip with everything stored for ${label}: settings and notes, raw activity, analysis outputs, live recordings, market resolutions and a README.`} onClose={onClose}
           footer={<><button className="btn ghost" onClick={onClose}>Close</button><a className="btn primary" href={url} onClick={() => setTimeout(onClose, 300)}>Build and download zip</a></>}>
      <label className="check"><input type="checkbox" checked={prices} onChange={(e) => setPrices(e.target.checked)} />Include 1-second reference prices (for re-running signal tests)</label>
      <p className="hint">About {mb === null ? "…" : mb.toFixed(0)} MB before compression. Large traders take a few seconds to build.</p>
    </Modal>
  );
}
