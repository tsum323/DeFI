// Live pool price, read straight from the blockchain by the viewer's browser every 30 s.
// Read-only by construction: only eth_blockNumber and eth_call(slot0) on the pool are
// allowed (same rule as backend.core.safety.assert_read_only); no wallet, no keys.
import { useEffect, useState } from "react";
import { num } from "../format";

const POOL = "0x99ac8cA7087fA4A2A1FB6357269965A2014ABc35"; // WBTC/USDC 0.3%, Ethereum
const SLOT0 = "0x3850c7bd";
const RPCS = ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org"]; // free, no key
const ALLOWED = new Set(["eth_blockNumber", "eth_call"]);
const DEC0 = 8, DEC1 = 6; // WBTC, USDC

async function rpc(url: string, method: string, params: unknown[]): Promise<string> {
  if (!ALLOWED.has(method)) throw new Error(`blocked non-read method ${method}`);
  const r = await fetch(url, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const j = await r.json();
  if (!r.ok || j.error || typeof j.result !== "string") throw new Error(j.error?.message ?? `HTTP ${r.status}`);
  return j.result;
}

interface Live { price: number; block: number; at: number }

async function readLive(): Promise<Live> {
  let err: unknown;
  for (const url of RPCS) {
    try {
      const [b, s] = await Promise.all([
        rpc(url, "eth_blockNumber", []),
        rpc(url, "eth_call", [{ to: POOL, data: SLOT0 }, "latest"]),
      ]);
      const sqrt = Number(BigInt("0x" + s.slice(2, 66))) / 2 ** 96;
      return { price: sqrt * sqrt * 10 ** (DEC0 - DEC1), block: parseInt(b, 16), at: Date.now() };
    } catch (e) { err = e; }
  }
  throw err;
}

const msk = (ms: number) => new Date(ms).toLocaleTimeString("ru-RU", { timeZone: "Europe/Moscow" }) + " МСК";

export default function LivePrice() {
  const [live, setLive] = useState<Live | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let stop = false;
    const tick = () => readLive().then((l) => { if (!stop) { setLive(l); setFailed(false); } })
      .catch(() => { if (!stop) setFailed(true); });
    tick();
    const id = setInterval(tick, 30_000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  if (!live) return failed ? <div className="snapshot">Живая цена сейчас недоступна (публичный узел не ответил).</div> : null;
  return <div className="snapshot live-price">
    <span className="badge badge-exact">ТОЧНО</span> Цена WBTC сейчас: <b>{num(live.price, 0)} USDC</b> · блок{" "}
    {live.block.toLocaleString("ru-RU")} · {msk(live.at)} · обновляется каждые 30 секунд
    {failed && " · последнее обновление не удалось, показана прошлая цена"}
  </div>;
}
