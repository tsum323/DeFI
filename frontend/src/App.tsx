import { useEffect } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { DATA_UPDATED, STATIC, type SnapshotInfo } from "./api";
import LivePrice from "./components/LivePrice";
import { useApi } from "./components/useApi";
import { date } from "./format";
import { Activity, Bot, Dashboard, Portfolio, Positions, Research, Signals } from "./lab/pages";

function SnapshotBanner() {
  const { data } = useApi<SnapshotInfo>("/api/snapshot");
  useEffect(() => {  // live site: re-read the (small) data files every 2 minutes
    if (!data?.live) return;
    const id = setInterval(() => window.dispatchEvent(new Event(DATA_UPDATED)), 120_000);
    return () => clearInterval(id);
  }, [data?.live]);
  if (!data) return null;
  return <div className="snapshot">Данные блокчейна до блока {data.last_block.toLocaleString("ru-RU")} ({date(data.last_block_ts)}) ·
    обновлено {date(data.exported_at)} · {data.live ? `автоматически каждые ${data.every_minutes ?? 15} минут` : "снимок"}.</div>;
}

const NAV = [["/", "Главная"], ["/signals", "Сигналы"], ["/positions", "Мои позиции"], ["/bot", "Мой бот"], ["/portfolio", "Портфель"],
  ["/research", "Исследования"], ["/activity", "Журнал"]] as const;

export default function App() {
  return (
    <div className="layout">
      <aside className="side">
        <div className="brand"><span className="brand-mark">◆</span> UNISWAP AI LAB</div>
        <nav>{NAV.map(([to, t]) => <NavLink key={to} to={to} end={to === "/"}>{t}</NavLink>)}</nav>
        <div className="legend">
          <div><span className="badge badge-exact">ТОЧНО</span> из блокчейна</div>
          <div><span className="badge badge-modeled">МОДЕЛЬ</span> точный расчёт пула</div>
          <div><span className="badge badge-estimated">ОЦЕНКА</span> приближённо</div>
          <div><span className="badge badge-scenario">ПРОГНОЗ</span> не гарантия</div>
        </div>
        <div className="side-foot">только чтение · ничего не исполняется</div>
      </aside>
      <main className="main">
        {STATIC && <SnapshotBanner />}
        <LivePrice />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/bot" element={<Bot />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/research" element={<Research />} />
          <Route path="/activity" element={<Activity />} />
        </Routes>
      </main>
    </div>
  );
}
