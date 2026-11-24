import React, { Suspense, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import Layout from "./components/Layout";
import { api, getUserFromToken } from "./lib/api";
import Login from "./pages/Login";
import Home from "./pages/Home";

// Every application is split into its own route chunk and loads only when opened.
const CoverageList = React.lazy(() => import("./pages/CoverageList"));
const EarningsCalendar = React.lazy(() => import("./pages/EarningsCalendar"));
const Pulse = React.lazy(() => import("./pages/Pulse"));
const SectorScreener = React.lazy(() => import("./pages/SectorScreener"));
const SectorScreenerLanding = React.lazy(() => import("./pages/SectorScreener").then((module) => ({ default: module.SectorScreenerLanding })));
const SectorAutomotive = React.lazy(() => import("./pages/sectorAutomotive"));
const SectorSolar = React.lazy(() => import("./pages/sectorSolar"));
const SectorTradeStat = React.lazy(() => import("./pages/sectorTradestat"));
const SectorLpgCng = React.lazy(() => import("./pages/sectorLpgCng"));
const NewsAlert = React.lazy(() => import("./pages/NewsAlert"));
const Regulatory = React.lazy(() => import("./pages/Regulatory"));
const CrispNotes = React.lazy(() => import("./pages/CrispNotes"));

const DebtTracker = React.lazy(() => import("./pages/DebtTracker"));
const PortfolioComparison = React.lazy(() => import("./pages/PortfolioComparison"));
const YouTubeIntelligence = React.lazy(() => import("./pages/YouTubeIntelligence"));
const OpsTracker = React.lazy(() => import("./pages/OpsTracker"));

// Settings is the account page for every user. Admins get the full workspace
// (User Settings, App Access, Usage & Sessions, Manage Users) inside it.
const Settings = React.lazy(() => import("./pages/Settings"));

const TRACKED_APP_ROUTES = [
  ["/sector-screener", "Sector Screener"],
  ["/news-alert", "NewsAlert"],
  ["/regulatory-signals", "Regulatory Signal"],
  ["/earnings-calendar", "Earnings Calendar"],
  ["/coverage-list", "Coverage List"],
  ["/crisp-notes", "CRISP Notes"],
  ["/debt-tracker", "Debt Tracker"],
  ["/portfolio-comparison", "Portfolio Comparison"],
  ["/settings", "Settings"],
  ["/dashboard", "Dashboard"],
  ["/pulse", "Pulse"],
  ["/youtube-intelligence", "YouTube Intelligence"],
  ["/ops-tracker", "Ops Tracker"],
];

function trackedAppForPath(pathname) {
  const path = String(pathname || "/").replace(/\/+$/, "") || "/";
  return TRACKED_APP_ROUTES.find(([prefix]) => path === prefix || path.startsWith(`${prefix}/`))?.[1] || "Workspace";
}

function Private({ user, children }) {
  return user ? children : <Navigate to="/login" replace />;
}

function PageLoader() {
  return (
    <div className="min-h-[280px] px-5 py-8">
      <div className="h-8 w-56 animate-pulse rounded-lg bg-slate-100" />
      <div className="mt-3 h-4 w-80 max-w-full animate-pulse rounded bg-slate-100" />
      <div className="mt-6 h-40 animate-pulse rounded-2xl border border-slate-100 bg-slate-50" />
    </div>
  );
}

function Page({ children }) {
  return <Suspense fallback={<PageLoader />}>{children}</Suspense>;
}

export default function App() {
  // A JWT issued by our backend already contains sub/username/role. Decode it
  // only as an optimistic UI bootstrap; every protected backend endpoint still
  // validates the signed token server-side. This removes an otherwise visible
  // /auth/me round-trip from the first authenticated paint.
  const [user, setUser] = useState(() => getUserFromToken());
  const navigate = useNavigate();
  const location = useLocation();
  const trackedApp = trackedAppForPath(location.pathname);

  // Record activity for every authenticated module, not just the Settings page.
  useEffect(() => {
    if (!user?.id) return undefined;
    let mounted = true;
    const pathname = location.pathname;
    const app = trackedApp;
    const send = (action, visible = document.visibilityState === "visible") => {
      if (!mounted) return;
      void api.recordActivity({ app, action, route_path: pathname, visible })
        .then((result) => {
          if (result?.tracked === false) {
            console.warn("App usage heartbeat was not persisted", result.reason || result.status || "unknown");
          }
        })
        .catch((error) => console.warn("App usage heartbeat request failed", error?.message || error));
    };

    send("view");
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") send("heartbeat", true);
    }, 30_000);
    const onVisibilityChange = () => {
      const visible = document.visibilityState === "visible";
      send(visible ? "view" : "visibilitychange", visible);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      mounted = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [user?.id, location.pathname, trackedApp]);

  return (
    <Routes>
      <Route
        path="/login"
        element={
          user ? (
            <Navigate to="/dashboard" replace />
          ) : (
            <Login
              onLogin={(nextUser) => {
                setUser(nextUser);
                navigate("/dashboard");
              }}
            />
          )
        }
      />

      <Route path="/" element={user ? <Navigate to="/dashboard" replace /> : <Home user={null} />} />

      <Route
        path="*"
        element={
          <Private user={user}>
            <Layout user={user} setUser={setUser}>
              <Routes>
                <Route path="/dashboard" element={<Page><Pulse user={user} /></Page>} />
                <Route path="/pulse" element={<Navigate to="/dashboard" replace />} />
                <Route path="/youtube-intelligence" element={<Page><YouTubeIntelligence user={user} /></Page>} />
                <Route path="/stock-screener" element={<Navigate to="/dashboard" replace />} />
                <Route path="/stock-screener/:symbol" element={<Navigate to="/dashboard" replace />} />
                <Route path="/sector-screener" element={<Page><SectorScreenerLanding /></Page>} />
                <Route path="/sector-screener/automotive" element={<Page><SectorAutomotive user={user} /></Page>} />
                <Route path="/sector-screener/automotive/:reportKey" element={<Navigate to="/sector-screener/automotive" replace />} />
                <Route path="/sector-screener/solar" element={<Page><SectorSolar user={user} /></Page>} />
                <Route path="/sector-screener/solar/:reportKey" element={<Page><SectorSolar user={user} /></Page>} />
                <Route path="/sector-screener/tradestat" element={<Page><SectorTradeStat user={user} /></Page>} />
                <Route path="/sector-screener/lpg" element={<Page><SectorLpgCng user={user} /></Page>} />
                <Route path="/sector-screener/:sectorId" element={<Page><SectorScreener user={user} /></Page>} />
                <Route path="/sector-screener/:sectorId/:reportKey" element={<Page><SectorScreener user={user} /></Page>} />
                <Route path="/coverage-list" element={<Page><CoverageList user={user} /></Page>} />
                <Route path="/earnings-calendar" element={<Page><EarningsCalendar user={user} /></Page>} />
                <Route path="/lt-ht" element={<Navigate to="/coverage-list" replace />} />
                <Route path="/coverage" element={<Navigate to="/coverage-list" replace />} />

                <Route path="/news-alert" element={<Page><NewsAlert user={user} /></Page>} />
                <Route path="/regulatory-signals" element={<Page><Regulatory user={user} /></Page>} />
                <Route path="/pe-warrant" element={<Navigate to="/regulatory-signals" replace />} />
                <Route path="/crisp-notes" element={<Page><CrispNotes user={user} /></Page>} />
                <Route path="/debt-tracker" element={<Page><DebtTracker user={user} /></Page>} />
                <Route path="/portfolio-comparison" element={<Page><PortfolioComparison user={user} /></Page>} />
                <Route path="/ops-tracker" element={<Page><OpsTracker user={user} /></Page>} />

                {/* Unified admin/auth workspace. The old admin URLs are retained only as redirects. */}
                <Route path="/settings" element={<Page><Settings user={user} /></Page>} />
                <Route path="/admin/access-panel" element={<Navigate to="/settings" replace />} />
                <Route path="/admin/usage" element={<Navigate to="/settings" replace />} />

                <Route path="/market-intelligence/*" element={<Navigate to="/sector-screener" replace />} />

                <Route path="*" element={<Navigate to="/dashboard" replace />} />
              </Routes>
            </Layout>
          </Private>
        }
      />
    </Routes>
  );
}