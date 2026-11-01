import { lazy, Suspense, useEffect } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import DataSources from "./pages/DataSources";
import Overview from "./pages/Overview";
import Portfolio from "./pages/Portfolio";
import Ranking from "./pages/Ranking";
import Research from "./pages/Research";
import Signals from "./pages/Signals";
import { DATA_UPDATED, STATIC, type SnapshotInfo } from "./api";
import { Loading } from "./components/ui";
import { useApi } from "./components/useApi";
import { date } from "./format";

function SnapshotBanner() {
  const { data } = useApi<SnapshotInfo>("/api/snapshot");
  useEffect(() => {  // live site: re-read the (small) data files every 2 minutes
    if (!data?.live) return;
    const id = setInterval(() => window.dispatchEvent(new Event(DATA_UPDATED)), 120_000);
    return () => clearInterval(id);
  }, [data?.live]);
  if (!data) return null;
  return <div className="snapshot">Данные блокчейна до блока {data.last_block.toLocaleString("ru-RU")}{" "}
    ({date(data.last_block_ts)}) · обновлено {date(data.exported_at)} ·{" "}
    {data.live ? `обновляется автоматически каждые ${data.every_minutes ?? 30} минут` : "снимок, обновляется вручную"}.</div>;
}

// Plotly (~4 MB) is only needed on the strategy page: load it on demand
const StrategyDetails = lazy(() => import("./pages/StrategyDetails"));

const NAV = [
  ["/", "Главная"], ["/ranking", "Рейтинг стратегий"], ["/research", "AI-исследования"],
  ["/signals", "Центр сигналов"], ["/portfolio", "Виртуальный портфель"], ["/data", "Источники данных"],
] as const;

export default function App() {
  return (
    <div className="layout">
      <aside className="side">
        <div className="brand"><span className="brand-mark">◆</span> DeFi Liquidity Lab</div>
        <nav>{NAV.map(([to, t]) => <NavLink key={to} to={to} end={to === "/"}>{t}</NavLink>)}</nav>
        <div className="legend">
          <div><span className="badge badge-exact">ТОЧНО</span> из блокчейна</div>
          <div><span className="badge badge-modeled">МОДЕЛЬ</span> бэктест</div>
          <div><span className="badge badge-scenario">ПРОГНОЗ</span> сценарий</div>
        </div>
        <div className="side-foot">только чтение · ничего не исполняется</div>
      </aside>
      <main className="main">
        {STATIC && <SnapshotBanner />}
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/ranking" element={<Ranking />} />
          <Route path="/strategy/:hash" element={<Suspense fallback={<Loading />}><StrategyDetails /></Suspense>} />
          <Route path="/research" element={<Research />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/data" element={<DataSources />} />
        </Routes>
      </main>
    </div>
  );
}
