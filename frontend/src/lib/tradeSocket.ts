// The one websocket to the server's trade gateway (/ws/trade). Orders/cancels go out here (no HTTP setup
// per click); account events (fills, order updates, balances, positions) come back here.

type Listener = (msg: any) => void;

class TradeSocket {
  ws: WebSocket | null = null;
  status = "connecting";
  rtt: number | null = null; // browser <-> server round trip (ms)
  private pending = new Map<string, { resolve: (v: any) => void; t0: number }>();
  private listeners = new Set<Listener>();
  private seq = 0;
  private started = false;

  start() {
    if (this.started) return;
    this.started = true;
    this.open();
    setInterval(() => this.send({ op: "ping", t: performance.now() }), 5000);
  }

  private open() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/trade`);
    this.ws = ws;
    this.setStatus("connecting");
    ws.onopen = () => this.setStatus("live");
    ws.onclose = () => {
      this.setStatus("reconnecting");
      for (const p of this.pending.values()) p.resolve({ ok: false, error: "connection lost" });
      this.pending.clear();
      setTimeout(() => this.open(), 800);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.type === "pong") {
        this.rtt = performance.now() - m.t;
      } else if (m.type === "ack") {
        const p = this.pending.get(m.cid);
        if (p) {
          this.pending.delete(m.cid);
          p.resolve({ ...m, client_ms: performance.now() - p.t0 });
        }
      }
      this.listeners.forEach((l) => l(m));
    };
  }

  private setStatus(s: string) {
    this.status = s;
    this.listeners.forEach((l) => l({ type: "socket", status: s }));
  }

  private send(m: any) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(m));
      return true;
    }
    return false;
  }

  /** Send an op and resolve with the server's ack ({ok, result|error, client_ms}). */
  request(op: string, body: Record<string, unknown> = {}): Promise<any> {
    const cid = `c${++this.seq}`;
    return new Promise((resolve) => {
      this.pending.set(cid, { resolve, t0: performance.now() });
      if (!this.send({ op, cid, ...body })) {
        this.pending.delete(cid);
        resolve({ ok: false, error: "not connected to the server" });
      }
    });
  }

  on(l: Listener) {
    this.listeners.add(l);
    return () => { this.listeners.delete(l); };
  }
}

export const tradeSocket = new TradeSocket();
