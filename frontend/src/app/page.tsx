"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import {
  FiActivity,
  FiAlertTriangle,
  FiFileText,
  FiGitBranch,
  FiGrid,
  FiHeadphones,
  FiLock,
  FiMail,
  FiRadio,
  FiRefreshCw,
  FiShare2,
  FiShield,
  FiTrendingUp,
  FiUsers
} from "react-icons/fi";

import {
  getEmployeeInvestigation,
  getCorrelation,
  getAutoDetectedTradesDebug,
  importTradesCsv,
  listAlerts,
  listDocuments,
  documentViewUrl,
  documentDownloadUrl,
  logDocumentAccess,
  listTrades,
  resetAll,
  seedDemo,
  uploadDocument,
  type AlertOut,
  type CorrelationEdge,
  type DocumentOut,
  type EmployeeInvestigationOut,
  type InvestigationTradeOut
} from "@/lib/api";

function scoreBadge(score: number) {
  if (score >= 75) return { cls: "badge badgeBad", label: `High (${score})` };
  if (score >= 50) return { cls: "badge badgeWarn", label: `Medium (${score})` };
  return { cls: "badge badgeGood", label: `Low (${score})` };
}

/** API returns naive datetimes (UTC). Parse as UTC so access/trade times match the dataset. */
function fmt(ts: string) {
  const s = (ts || "").trim();
  if (!s) return ts;
  const isoNaive = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/;
  const d = new Date(isoNaive.test(s) ? `${s}Z` : s);
  return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, { timeZone: "UTC" });
}

function fmtDiffDays(days: number) {
  const totalSeconds = Math.max(0, Math.round((days || 0) * 86400));
  const d = Math.floor(totalSeconds / 86400);
  const h = Math.floor((totalSeconds % 86400) / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return `${d}d ${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
}

const EMPLOYEE_IDS = Array.from({ length: 20 }, (_, i) => `E${String(101 + i)}`);

type ToastAlarm = {
  id: string;
  alertType: string;
  severity: number;
  headline: string;
  symbolOrCompany?: string;
  employeeId?: string | null;
};

export default function HomePage() {
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [trades, setTrades] = useState<InvestigationTradeOut[]>([]);
  const [alerts, setAlerts] = useState<AlertOut[]>([]);
  const [edges, setEdges] = useState<CorrelationEdge[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tradeDebug, setTradeDebug] = useState<string | null>(null);
  const [investigation, setInvestigation] = useState<EmployeeInvestigationOut | null>(null);
  /** All DB trades for selected employee (fallback when investigation API errors). */
  const [employeeTradesInDbCount, setEmployeeTradesInDbCount] = useState(0);
  /** Investigation + CSV align are scoped to this MNPI document (newest upload auto-selected). */
  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(null);
  /** If true, CSV import overwrites traded_at to after access (demo). Default: keep dataset times. */
  const [alignTradesToAccess, setAlignTradesToAccess] = useState(false);
  const selectedDocumentIdRef = useRef<number | null>(null);
  useEffect(() => {
    selectedDocumentIdRef.current = selectedDocumentId;
  }, [selectedDocumentId]);

  // Default matches demo / CSV sample employees (E101–E120).
  const [employeeId, setEmployeeId] = useState("E101");
  const [selectedEmployees, setSelectedEmployees] = useState<string[]>(["E101"]);
  const [liveMonitoring, setLiveMonitoring] = useState(true);
  const employeeIdRef = useRef(employeeId);
  useEffect(() => {
    employeeIdRef.current = employeeId;
  }, [employeeId]);

  const [desktopAlertsEnabled, setDesktopAlertsEnabled] = useState(false);
  const [alarmToasts, setAlarmToasts] = useState<ToastAlarm[]>([]);
  const [showFlaggedModal, setShowFlaggedModal] = useState(false);
  const [dismissingToastId, setDismissingToastId] = useState<string | null>(null);

  const lastDocsCountRef = useRef(0);
  useEffect(() => {
    if (docs.length === 0) {
      setSelectedDocumentId(null);
      lastDocsCountRef.current = 0;
      return;
    }
    const newestId = docs[0].id;
    const grew = docs.length > lastDocsCountRef.current;
    lastDocsCountRef.current = docs.length;
    setSelectedDocumentId((prev) => {
      if (prev == null || !docs.some((d) => d.id === prev)) return newestId;
      if (grew) return newestId;
      return prev;
    });
  }, [docs]);

  const graphRef = useRef<HTMLDivElement | null>(null);
  const restrictedCount = useMemo(() => docs.filter((d) => d.restricted).length, [docs]);
  const highAlertCount = useMemo(() => alerts.filter((a) => a.severity >= 75).length, [alerts]);
  const openHighAlertCount = useMemo(
    () => alerts.filter((a) => a.severity >= 75 && !a.resolved).length,
    [alerts]
  );

  const flaggedEmployees = useMemo(() => {
    const counts = new Map<string, number>();
    for (const a of alerts) {
      if (!a.employee_id) continue;
      if (a.resolved) continue;
      if (a.severity < 75) continue;
      counts.set(a.employee_id, (counts.get(a.employee_id) || 0) + 1);
    }
    return Array.from(counts.entries())
      .map(([employeeId, count]) => ({ employeeId, count }))
      .sort((x, y) => y.count - x.count || x.employeeId.localeCompare(y.employeeId));
  }, [alerts]);

  const flaggedEmployeeIdSet = useMemo(() => new Set(flaggedEmployees.map((f) => f.employeeId)), [flaggedEmployees]);
  const investigationStatus = useMemo(() => {
    if (busy) return "Active";
    if (docs.length === 0) return "No Data";
    return "Ready";
  }, [busy, docs.length]);

  const seenHighAlertIdsRef = useRef<Set<number>>(new Set());
  const firstHighAlertLoadRef = useRef(true);

  type RefreshScope = { employeeId?: string; documentId?: number | null };

  const refreshAll = useCallback(async (scope?: RefreshScope) => {
    // IMPORTANT: correlation endpoint can generate correlation alerts as a side-effect.
    const emp =
      scope?.employeeId !== undefined ? scope.employeeId.trim() : (employeeIdRef.current || "").trim();
    const docScope =
      scope && Object.hasOwn(scope, "documentId")
        ? scope.documentId
        : selectedDocumentIdRef.current;

    try {
      const [d, tradeRows, c] = await Promise.all([
        listDocuments(),
        listTrades(),
        getCorrelation().catch(() => ({ edges: [] as CorrelationEdge[] }))
      ]);
      setDocs(d);
      let a: AlertOut[];
      try {
        a = await listAlerts();
      } catch {
        a = [];
      }
      setAlerts(a);

      const empDbCount = emp ? tradeRows.filter((t) => t.employee_id === emp).length : 0;

      // Investigation: access_time always from DocumentAccessLog; trades use CSV traded_at; only trades > access_time.
      if (emp) {
        try {
          const inv =
            typeof docScope === "number"
              ? await getEmployeeInvestigation(emp, docScope)
              : await getEmployeeInvestigation(emp);
          setInvestigation(inv);
          setTrades(inv.trades_after_access);
          setEmployeeTradesInDbCount(inv.employee_total_trades_in_db);
          setTradeDebug(inv.note ?? null);
        } catch (e: any) {
          setInvestigation(null);
          setTrades([]);
          setEmployeeTradesInDbCount(empDbCount);
          const dbgDocId = typeof docScope === "number" ? docScope : d[0]?.id;
          const dbg = await getAutoDetectedTradesDebug(dbgDocId);
          if (dbg?.ok) {
            const symbols = (dbg.available_trade_symbols || []).join(", ");
            setTradeDebug(
              `${emp}: investigation unavailable (${String(e?.message || e)}). Debug: extracted=${dbg.extracted_company || "-"} normalized=${dbg.normalized_company || "-"} symbols=${symbols || "-"}`
            );
          } else {
            setTradeDebug(String(e?.message || e));
          }
        }
      } else {
        setInvestigation(null);
        setTrades([]);
        setEmployeeTradesInDbCount(0);
        setTradeDebug("Enter an employee ID to investigate.");
      }
      setEdges(c.edges);

      if (scope?.employeeId !== undefined) {
        employeeIdRef.current = scope.employeeId.trim();
      }
      if (scope && Object.hasOwn(scope, "documentId")) {
        selectedDocumentIdRef.current = scope.documentId ?? null;
      }
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
  }, []);

  // Load data on mount. Do not call reset here — dev remount/HMR and repeat runs should keep the database unless the user clears it.
  useEffect(() => {
    void (async () => {
      try {
        await refreshAll();
      } catch (e: any) {
        setErr(String(e?.message || e));
      }
    })();
  }, [refreshAll]);

  // Re-run investigation when employee changes (without wiping the database).
  const employeeRefreshBoot = useRef(true);
  useEffect(() => {
    if (employeeRefreshBoot.current) {
      employeeRefreshBoot.current = false;
      return;
    }
    void refreshAll();
  }, [employeeId, refreshAll]);

  useEffect(() => {
    if (selectedDocumentId == null) return;
    void refreshAll();
  }, [selectedDocumentId, refreshAll]);

  // Popup alarm: show toast when new high-severity (>=75) alerts appear.
  useEffect(() => {
    const highOpenAlerts = alerts.filter((a) => a.severity >= 75 && !a.resolved);
    const seen = seenHighAlertIdsRef.current;

    if (firstHighAlertLoadRef.current) {
      firstHighAlertLoadRef.current = false;
      for (const a of highOpenAlerts) seen.add(a.id);
      return;
    }

    const newHighAlerts = highOpenAlerts.filter((a) => !seen.has(a.id));
    if (newHighAlerts.length === 0) return;

    for (const a of newHighAlerts) seen.add(a.id);

    setAlarmToasts((prev) => {
      const next = [
        ...newHighAlerts.map((a) => ({
          id: `alert-${a.id}`,
          alertType: a.alert_type.toUpperCase(),
          severity: a.severity,
          headline: a.title,
          symbolOrCompany:
            typeof a.details?.company === "string"
              ? String(a.details.company)
              : typeof a.details?.symbol === "string"
                ? String(a.details.symbol)
                : undefined,
          employeeId: a.employee_id,
        })),
        ...prev,
      ];
      return next.slice(0, 3);
    });

    if (desktopAlertsEnabled && typeof window !== "undefined" && "Notification" in window && Notification.permission === "granted") {
      for (const a of newHighAlerts) {
        try {
          new Notification(`MNPI Guard · ${a.alert_type.toUpperCase()} (${a.severity})`, {
            body: `${a.title}${a.employee_id ? ` · ${a.employee_id}` : ""}`,
          });
        } catch {
          // Ignore Notification failures.
        }
      }
    }
  }, [alerts, desktopAlertsEnabled]);


  const topEdges = useMemo(() => edges.slice().sort((a, b) => b.score - a.score).slice(0, 50), [edges]);

  useEffect(() => {
    if (!graphRef.current) return;
    const el = graphRef.current;
    el.innerHTML = "";

    const nodes: Array<{ data: { id: string; label: string; kind: string; flagged?: string } }> = [];
    const edgeEls: Array<{ data: { id: string; source: string; target: string; label: string; score: number } }> = [];

    for (const e of topEdges) {
      const empId = `emp:${e.employee_id}`;
      const docId = `doc:${e.document_id}`;
      const symId = `sym:${e.symbol}`;
      const trId = `tr:${e.trade_id}`;

      nodes.push({
        data: {
          id: empId,
          label: e.employee_id,
          kind: "employee",
          flagged: flaggedEmployeeIdSet.has(e.employee_id) ? "true" : "false",
        },
      });
      nodes.push({ data: { id: docId, label: `Doc ${e.document_id}`, kind: "document" } });
      nodes.push({ data: { id: symId, label: e.symbol, kind: "symbol" } });
      nodes.push({ data: { id: trId, label: `Trade ${e.trade_id}`, kind: "trade" } });

      edgeEls.push({
        data: { id: `e1:${empId}-${docId}`, source: empId, target: docId, label: "access", score: e.score }
      });
      edgeEls.push({
        data: { id: `e2:${docId}-${symId}`, source: docId, target: symId, label: "mentions", score: e.score }
      });
      edgeEls.push({
        data: { id: `e3:${empId}-${trId}`, source: empId, target: trId, label: "trade", score: e.score }
      });
      edgeEls.push({
        data: { id: `e4:${trId}-${symId}`, source: trId, target: symId, label: "in", score: e.score }
      });
    }

    const cy = cytoscape({
      container: el,
      elements: [...nodes, ...edgeEls],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#14b8a6",
            color: "#f8fafc",
            label: "data(label)",
            "font-size": "10px",
            "text-outline-color": "rgba(15,23,42,0.45)",
            "text-outline-width": 2,
            width: 20,
            height: 20
          }
        },
        {
          selector: 'node[kind="employee"][flagged="true"]',
          style: {
            "background-color": "#fb7185",
            "border-color": "#fb7185",
            "border-width": 3,
            width: 26,
            height: 26,
          },
        },
        {
          selector: 'node[kind="document"]',
          style: { "background-color": "#f59e0b" }
        },
        {
          selector: 'node[kind="trade"]',
          style: { "background-color": "#ef4444" }
        },
        {
          selector: 'node[kind="symbol"]',
          style: { "background-color": "#4ade80" }
        },
        {
          selector: "edge",
          style: {
            width: 2,
            label: "data(label)",
            "font-size": "9px",
            color: "#f8fafc",
            "text-outline-color": "rgba(15,23,42,0.85)",
            "text-outline-width": 2,
            "curve-style": "bezier",
            "line-color": "rgba(148,163,184,0.52)",
            "target-arrow-shape": "triangle",
            "target-arrow-color": "rgba(148,163,184,0.52)"
          }
        }
      ],
      layout: { name: "cose", animate: false }
    });

    return () => {
      cy.destroy();
    };
  }, [topEdges, flaggedEmployeeIdSet]);

  async function onSeed() {
    setErr(null);
    setBusy("Seeding demo data…");
    try {
      await seedDemo();
      await refreshAll();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function onReset() {
    setErr(null);
    setBusy("Clearing data…");
    try {
      await resetAll();
      await refreshAll();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function onUpload(file: File | null) {
    if (!file) return;
    setErr(null);
    setBusy("Uploading & scanning…");
    try {
      const uploaded = await uploadDocument(file);
      // Immediately scope the investigation/CSV alignment to this newly created document id.
      setSelectedDocumentId(uploaded.id);
      await refreshAll({ employeeId: employeeId.trim(), documentId: uploaded.id });
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function onImportTradesCsv(file: File | null) {
    if (!file) return;
    setErr(null);
    setBusy("Importing external trade dataset…");
    try {
      const res = await importTradesCsv(file, {
        alignToAccess: alignTradesToAccess,
        documentId: selectedDocumentId,
        replaceEmployeesInCsv: true
      });
      await refreshAll({ employeeId: employeeId.trim(), documentId: selectedDocumentId });
      setBusy(`Imported ${res.imported_trades} trades`);
      setTimeout(() => setBusy(null), 1500);
    } catch (e: any) {
      setErr(String(e?.message || e));
      setBusy(null);
    }
  }

  async function onInvestigate() {
    setErr(null);
    setBusy("Refreshing investigation…");
    try {
      await refreshAll({ employeeId: employeeId.trim(), documentId: selectedDocumentId });
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function onViewDocument(docId: number) {
    setErr(null);
    setBusy("Opening document…");
    try {
      await logDocumentAccess(docId, employeeId, "view");
      setSelectedDocumentId(docId);
      window.open(documentViewUrl(docId), "_blank", "noopener,noreferrer");
      await refreshAll({ employeeId: employeeId.trim(), documentId: docId });
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function onDownloadDocument(docId: number) {
    setErr(null);
    setBusy("Preparing download…");
    try {
      await logDocumentAccess(docId, employeeId, "download");
      setSelectedDocumentId(docId);
      window.open(documentDownloadUrl(docId), "_blank", "noopener,noreferrer");
      await refreshAll({ employeeId: employeeId.trim(), documentId: docId });
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(null);
    }
  }

  function onToggleEmployee(emp: string, checked: boolean) {
    setSelectedEmployees((prev) => {
      const nextSet = new Set(prev);
      if (checked) nextSet.add(emp);
      else nextSet.delete(emp);

      let next = Array.from(nextSet);
      if (next.length === 0) next = ["E101"];

      if (!next.includes(employeeId)) setEmployeeId(next[0]);
      return next;
    });
  }

  function onSelectAllEmployees() {
    setSelectedEmployees(EMPLOYEE_IDS);
  }

  function onResetEmployees() {
    setSelectedEmployees(["E101"]);
    setEmployeeId("E101");
  }

  async function onEnableDesktopAlerts() {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setErr("Desktop notifications are not supported in this browser.");
      return;
    }
    try {
      const permission = await Notification.requestPermission();
      if (permission === "granted") {
        setDesktopAlertsEnabled(true);
      } else {
        setDesktopAlertsEnabled(false);
      }
    } catch (e: any) {
      setDesktopAlertsEnabled(false);
      setErr(String(e?.message || e));
    }
  }

  function dismissToast(id: string) {
    setDismissingToastId(id);
    window.setTimeout(() => {
      setAlarmToasts((prev) => prev.filter((t) => t.id !== id));
      setDismissingToastId((cur) => (cur === id ? null : cur));
    }, 220);
  }

  function investigateFromToast(toast: ToastAlarm) {
    if (toast.employeeId) {
      setSelectedEmployees((prev) => (prev.includes(toast.employeeId as string) ? prev : [...prev, toast.employeeId as string]));
      setEmployeeId(toast.employeeId);
    }
    void onInvestigate();
    dismissToast(toast.id);
  }

  function onFocusFlaggedEmployee(emp: string) {
    // Make that employee the active one; keep their checkbox selected.
    setSelectedEmployees((prev) => (prev.includes(emp) ? prev : [...prev, emp]));
    setEmployeeId(emp);
    setShowFlaggedModal(false);
  }


  return (
    <div className="container">
      <div className="topbar">
        <div className="brand">
          <div className="brandTitle">MNPI Guard</div>
          <div className="brandSub">MNPI detection + trading anomalies + correlation dashboard</div>
        </div>
        <div className="nav">
          <a href="#hero" className="navLink">Command Center</a>
          <a href="#features" className="navLink">Explore</a>
          <a href="#about" className="navLink">About</a>
          <a href="#how-it-works" className="navLink">How It Works</a>
          <a href="#contact" className="navLink">Contact</a>
          <button className="btn btnSeedSecondary" onClick={onSeed} disabled={!!busy}>
            Seed demo data
          </button>
          <button className="btn btnDanger" onClick={onReset} disabled={!!busy}>
            Clear data
          </button>
        </div>
      </div>
      <section id="hero" className="hero sectionFade">
        <div>
          <div className="heroMetaRow">
            <span className="heroMetaIcon">◌</span>
            <span className="heroMetaText">INSIDER RISK DETECTION</span>
          </div>
          <h1 className="heroTitle">Scan sensitive documents, monitor employee trades, and detect risk fast.</h1>
          <p className="heroText">
            MNPI Guard helps teams connect document access, employee activity, and trade timing in one clear investigation workflow.
          </p>
          <div className="heroActions">
            <a href="#features" className="btn btnPrimary heroCta">Explore Dashboard</a>
            <a href="#contact" className="btn heroGhostBtn">Contact Team</a>
          </div>
        </div>
        <div className="heroStats">
          <div className="heroStat">
            <div className="heroStatHead"><FiActivity /> Live risk scoring</div>
            <div className="heroStatDesc">Real-time severity calculation for uploaded documents.</div>
          </div>
          <div className="heroStat">
            <div className="heroStatHead"><FiShare2 /> Correlation graph</div>
            <div className="heroStatDesc">Connect access events and trades with visual links.</div>
          </div>
          <div className="heroStat">
            <div className="heroStatHead"><FiUsers /> Employee tracking</div>
            <div className="heroStatDesc">Monitor activity by employee scope and access window.</div>
          </div>
          <div className="heroStat">
            <div className="heroStatHead"><FiShield /> Audit-ready alerts</div>
            <div className="heroStatDesc">Surface critical findings with compliance context.</div>
          </div>
        </div>
      </section>

      <section id="features" className="sectionFade">
      <div className="sectionDivider">
        <span>Insights</span>
      </div>
      <div className="kpiRow">
        <div className="kpiTile">
          <div className="kpiHeader">
            <div className="kpiLabel"><FiFileText /> Documents</div>
            <div className="kpiTrend kpiTrendUp">+4%</div>
          </div>
          <div className="kpiValue">{docs.length}</div>
          <div className="kpiMeta">Total scanned</div>
        </div>
        <div className="kpiTile">
          <div className="kpiHeader">
            <div className="kpiLabel"><FiLock /> Restricted</div>
            <div className="kpiTrend kpiTrendFlat">~0%</div>
          </div>
          <div className="kpiValue">{restrictedCount}</div>
          <div className="kpiMeta">Flagged sensitive</div>
        </div>
        <div className="kpiTile">
          <div className="kpiHeader">
            <div className="kpiLabel"><FiTrendingUp /> Trades for employee</div>
            <div className="kpiTrend kpiTrendUp">+2%</div>
          </div>
          <div className="kpiValue">{employeeTradesInDbCount}</div>
          <div className="kpiMeta">Post-access trades</div>
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            After access: {investigation?.total_trades_after_access ?? trades.length}
          </div>

        </div>
        <div className="kpiTile">
          <div className="kpiHeader">
            <div className="kpiLabel"><FiAlertTriangle /> High Alerts</div>
            <div className="kpiTrend kpiTrendDown">-1%</div>
          </div>
          <div className="kpiValue">{highAlertCount}</div>
          <div className="kpiMeta">Critical cases</div>
          <div style={{ marginTop: 10 }}>
            <div className="meterWrap">
              <div className="meterBar" style={{ width: `${Math.min(100, openHighAlertCount * 18)}%` }} />
            </div>
            <div className="muted meterMeta">{openHighAlertCount} open</div>
          </div>
        </div>
      </div>
      </section>

      {err ? (
        <div className="card cardError">
          <div className="cardTitle">Error</div>
          <div className="mono muted statusText">
            {err}
          </div>
        </div>
      ) : null}

      {busy ? (
        <div className="card statusCard">
          <div className="cardTitle">Working</div>
          <div className="muted statusText">
            {busy}
          </div>
        </div>
      ) : null}

      {alarmToasts.length > 0 ? (
        <div
          className="alertOverlay fixed inset-0 z-40 bg-black/30 backdrop-blur-sm pointer-events-none"
          aria-hidden="true"
        />
      ) : null}

      {alarmToasts.length > 0 ? (
        <div className="fixed top-6 right-6 z-50 pointer-events-auto" role="dialog" aria-modal="false">
          {(() => {
            const t = alarmToasts[0];
            if (!t) return null;
            const isDismissing = dismissingToastId === t.id;
            return (
              <div className={`toastAlarm toastAlarmModal ${isDismissing ? "toastAlarmLeaving" : ""}`}>
                <div className="toastAlarmAccent" />
                <div className="toastAlarmHeader">
                  <div className="toastAlarmTitle">
                    <FiAlertTriangle /> HIGH ALERT
                  </div>
                  <span className="toastAlarmScore">CORRELATION ({t.severity})</span>
                </div>
                <div className="toastAlarmDetail">{t.headline || "Potential insider trading detected"}</div>
                <div className="toastAlarmMeta">
                  {t.symbolOrCompany ? `${t.symbolOrCompany} • ` : ""}
                  {t.employeeId ? `Employee ${t.employeeId} • ` : ""}
                  Confidence: {t.severity}%
                </div>
                <div className="toastAlarmActions">
                  <button className="btn btnDanger toastAlarmInvestigate" onClick={() => investigateFromToast(t)} disabled={!!busy}>
                    Investigate
                  </button>
                  <button className="btn toastAlarmClose" onClick={() => dismissToast(t.id)} disabled={!!busy}>
                    Dismiss
                  </button>
                </div>
              </div>
            );
          })()}
        </div>
      ) : null}

      {/* ═══ ROW 1 — MNPI Detection + Trading Monitor ═══ */}
      <div className="grid gridSecond commandGrid">
        {/* LEFT — MNPI Detection (Prevention) */}
        <div className="card commandCard rounded-3xl bg-white/5 backdrop-blur-xl">
          <div className="commandLabel uppercase tracking-[0.18em] text-white/50">Prevention</div>
          <div className="cardHeader">
            <div>
              <div className="cardTitle">MNPI Detection</div>
              <div className="cardHint">Upload a sensitive document, select employees to monitor, then run an investigation to detect risk.</div>
            </div>
            <span className={`statusPill ${investigationStatus === "Ready" ? "statusPillReady" : investigationStatus === "Active" ? "statusPillLive" : "statusPillIdle"}`}>
              {investigationStatus}
            </span>
          </div>

          {/* Upload */}
          <div style={{ marginTop: 14 }}>
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>Upload MNPI Document</div>
            <input
              className="w-full text-xs text-white file:mr-2 file:py-1 file:px-3 file:rounded file:border-0 file:text-xs file:font-medium file:bg-blue-500/20 file:text-blue-300 hover:file:bg-blue-500/30 cursor-pointer bg-white/5 border border-white/10 rounded-lg p-2.5"
              type="file"
              onChange={(e) => onUpload(e.target.files?.[0] || null)}
              disabled={!!busy}
            />
          </div>

          {/* Documents table */}
          {docs.length > 0 ? (
            <table className="table" style={{ marginTop: 14 }}>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Filename</th>
                  <th>Restricted</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td className="mono">{d.id}</td>
                    <td>{d.filename}</td>
                    <td>{d.restricted ? <span className="badge badgeBad">Yes</span> : <span className="badge badgeGood">No</span>}</td>
                    <td>
                      <div className="row" style={{ gap: 6 }}>
                        <button className="btn" onClick={() => onViewDocument(d.id)} disabled={!!busy}>View</button>
                        <button className="btn" onClick={() => onDownloadDocument(d.id)} disabled={!!busy}>Download</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="muted" style={{ marginTop: 10, fontSize: 13 }}>No documents uploaded yet.</div>
          )}

          {/* Employee selection — redesigned compact box */}
          <div style={{ marginTop: 18 }}>
            <div className="muted" style={{ fontSize: 11, marginBottom: 8, letterSpacing: '0.08em', textTransform: 'uppercase' }}>Employees (E101–E120, matches trade CSV)</div>
            <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
              <button
                type="button"
                className="btn"
                style={{ fontSize: 12, padding: '5px 14px' }}
                onClick={onSelectAllEmployees}
                disabled={!!busy}
              >
                Select all
              </button>
              <button
                type="button"
                className="btn"
                style={{ fontSize: 12, padding: '5px 14px' }}
                onClick={onResetEmployees}
                disabled={!!busy}
              >
                Reset to E101
              </button>
            </div>
            <div className="employeeListBox">
              {EMPLOYEE_IDS.map((emp) => {
                const checked = selectedEmployees.includes(emp);
                const isActive = emp === employeeId;
                return (
                  <div
                    key={emp}
                    className={`employeeRow${isActive ? " employeeRowActive" : ""}`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => onToggleEmployee(emp, e.target.checked)}
                      disabled={!!busy}
                      style={{ width: 14, height: 14, accentColor: '#14b8a6', cursor: 'pointer' }}
                    />
                    <span style={{ fontSize: 13, color: '#f1f5f9', fontWeight: 500 }}>{emp}</span>
                    {isActive && (
                      <span className="activeBadge">ACTIVE</span>
                    )}
                    {checked && !isActive && (
                      <button
                        type="button"
                        className="setActiveBtn"
                        onClick={() => setEmployeeId(emp)}
                        disabled={!!busy}
                      >
                        Set active
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
            <div style={{ marginTop: 12, display: 'flex', justifyContent: 'flex-end' }}>
              <button className="btn btnPrimary commandInvestigationCta" type="button" onClick={() => void onInvestigate()} disabled={!!busy}>
                {busy ? "Running…" : "Run investigation"}
              </button>
            </div>
          </div>
        </div>

        {/* RIGHT — Trading Monitor */}
        <div className="card commandCard rounded-3xl bg-white/5 backdrop-blur-xl">
          <div className="commandLabel uppercase tracking-[0.18em] text-white/50">Employee Investigation</div>
          <div className="cardHeader">
            <div>
              <div className="cardTitle">Trading Monitor</div>
              <div className="cardHint">Scope investigation to a specific MNPI document, import external trade data, then review post-access trades.</div>
            </div>
          </div>

          {/* Scoped MNPI */}
          <div style={{ marginTop: 14 }}>
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>Scoped MNPI Document</div>
            <select
              className="w-full px-2 py-1.5 rounded-md bg-white/5 border border-white/10 text-white text-xs focus:border-blue-400/50 outline-none"
              value={selectedDocumentId ?? ""}
              onChange={(e) => {
                const v = e.target.value;
                setSelectedDocumentId(v ? Number(v) : null);
              }}
              disabled={!!busy || docs.length === 0}
            >
              {docs.length === 0 ? <option value="">No documents yet</option> : null}
              {docs.map((d) => (
                <option key={d.id} value={d.id}>#{d.id} {d.filename}</option>
              ))}
            </select>
          </div>

          {/* Trade CSV */}
          <div style={{ marginTop: 12 }}>
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>Trade CSV Upload</div>
            <input
              className="w-full text-xs text-white file:mr-2 file:py-1 file:px-3 file:rounded file:border-0 file:text-xs file:font-medium file:bg-blue-500/20 file:text-blue-300 hover:file:bg-blue-500/30 cursor-pointer bg-white/5 border border-white/10 rounded-lg p-2.5"
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => onImportTradesCsv(e.target.files?.[0] || null)}
              disabled={!!busy || docs.length === 0}
            />
          </div>

          {/* Checkbox */}
          <label className="flex items-center gap-2 cursor-pointer" style={{ marginTop: 10 }}>
            <input
              type="checkbox"
              checked={alignTradesToAccess}
              onChange={(e) => setAlignTradesToAccess(e.target.checked)}
              disabled={!!busy}
              className="rounded w-3 h-3"
            />
            <span className="text-xs text-white/60">Stamp trade times after access (ignore CSV times; demo only)</span>
          </label>

          {/* Error / hint */}
          {investigation?.hint && (
            <div className="calloutWarn" style={{ marginTop: 14 }}>
              {investigation.hint}
            </div>
          )}
          {!investigation && tradeDebug && employeeId.trim() && (
            <div className="calloutError" style={{ marginTop: 14 }}>
              {tradeDebug}
            </div>
          )}

          {/* Results table */}
          {trades.length > 0 ? (
            <table className="table" style={{ marginTop: 14 }}>
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Symbol</th>
                  <th>Risk</th>
                  <th>Lag</th>
                  <th>Trade time</th>
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 10).map((t, idx) => {
                  const b = scoreBadge(
                    t.risk_tag === "HIGH" ? 80 : 20
                  );
                  return (
                    <tr key={`${t.employee_id}-${t.symbol}-${idx}`}>
                      <td className="mono">{t.employee_id}</td>
                      <td className="mono">{t.symbol}</td>
                      <td><span className={b.cls}>{t.risk_tag}</span></td>
                      <td className="mono">{typeof t.time_difference_days === "number" ? fmtDiffDays(t.time_difference_days) : "—"}</td>
                      <td className="muted">{fmt(t.trade_time)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="muted" style={{ marginTop: 14, fontSize: 13 }}>
              {employeeId.trim() ? "No post-access trades found for this employee." : "Select an employee and run investigation to see trades."}
            </div>
          )}

          {/* Investigation meta — compact */}
          {investigation ? (
            <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
              Access: {fmt(investigation.access_time)} [{investigation.access_source}] · Doc #{investigation.document_id} ({investigation.document_company}) · {investigation.total_trades_after_access} after access · {investigation.matching_trades_count} ticker match · {investigation.employee_total_trades_in_db} total in DB for {investigation.employee_id}
            </div>
          ) : null}
        </div>
      </div>

      {/* ═══ ROW 2 — Risk Correlation + Alerts Feed ═══ */}
      <div className="grid gridSecond commandGrid">
        {/* LEFT — Risk Correlation Dashboard */}
        <div className="card commandCard rounded-3xl bg-white/5 backdrop-blur-xl graphEngineCard">
          <div className="commandLabel uppercase tracking-[0.18em] text-white/50">Correlation</div>
          <div className="cardHeader">
            <div>
              <div className="cardTitle">Risk Correlation Dashboard</div>
              <div className="cardHint">Links MNPI access and trade activity to surface risk paths.</div>
            </div>
            <button className="btn" type="button" onClick={() => void refreshAll()} disabled={!!busy}>
              <FiRefreshCw /> Refresh
            </button>
          </div>

          <div className="graphPattern" style={{ marginTop: 14 }}>
            <div ref={graphRef} className="graphPanel" />
            {topEdges.length === 0 ? (
              <div className="graphEmptyState">
                <FiGitBranch />
                <div className="graphEmptyTitle">No correlations detected</div>
                <div className="graphEmptyHint">Upload MNPI and trade data to activate correlation engine</div>
              </div>
            ) : null}
          </div>

          <div className="legendRow" style={{ marginTop: 12 }}>
            <span className="legendItem">
              <span className="legendSwatch legendSwatchHigh" /> High risk
            </span>
            <span className="legendItem">
              <span className="legendSwatch legendSwatchMedium" /> Medium
            </span>
            <span className="legendItem">
              <span className="legendSwatch legendSwatchNormal" /> Normal
            </span>
            <span className="legendItem">
              <span className="legendSwatch legendSwatchFlagged" /> Flagged employees ({flaggedEmployees.length})
            </span>
          </div>
        </div>

        {/* RIGHT — Alerts & Compliance Feed */}
        <div className="card commandCard rounded-3xl bg-white/5 backdrop-blur-xl">
          <div className="commandLabel uppercase tracking-[0.18em] text-white/50">Alert Feed</div>
          <div className="cardHeader">
            <div>
              <div className="cardTitle">Alerts & Compliance Feed</div>
              <div className="cardHint">Real-time stream of MNPI, trade, and correlation alerts.</div>
            </div>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn" onClick={() => setLiveMonitoring((v) => !v)}>
                <FiRadio /> Live Monitoring: {liveMonitoring ? "ON" : "OFF"}
              </button>
              <button className="btn" onClick={() => void onEnableDesktopAlerts()} disabled={desktopAlertsEnabled}>
                {desktopAlertsEnabled ? "Desktop alerts enabled" : "Enable desktop alerts"}
              </button>
              <button className="btn" onClick={() => setShowFlaggedModal(true)} disabled={flaggedEmployees.length === 0}>
                Flagged employees ({flaggedEmployees.length})
              </button>
            </div>
          </div>

          <table className="table alertTable">
            <thead>
              <tr>
                <th>Type</th>
                <th>Severity</th>
                <th>Title</th>
                <th>Links</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {alerts.slice(0, 20).map((a) => {
                const level = a.severity >= 75 ? "High" : a.severity >= 50 ? "Medium" : "Low";
                return (
                  <tr key={a.id}>
                    <td className="mono">
                      <span
                        className={`typeDot ${
                          a.severity >= 75 ? "typeDotBad" : a.severity >= 50 ? "typeDotWarn" : "typeDotGood"
                        }`}
                      />
                      {a.alert_type}
                    </td>
                    <td>
                      <span className={`severityTag severity${level}`}>{level}</span>
                    </td>
                    <td>
                      <div>{a.title}</div>
                      {a.employee_id && flaggedEmployeeIdSet.has(a.employee_id) ? (
                        <div style={{ marginTop: 4 }}>
                          <span className="badge badgeBad">FLAGGED</span>
                        </div>
                      ) : null}
                      {a.alert_type === "correlation" && typeof a.details?.lag_minutes === "number" ? (
                        <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                          Lag: <span className="mono">{a.details.lag_minutes}</span> min
                          {typeof a.details?.access_time === "string" ? (
                            <>
                              {" "}
                              • access <span className="mono">{fmt(a.details.access_time)}</span>
                            </>
                          ) : null}
                          {typeof a.details?.trade_time === "string" ? (
                            <>
                              {" "}
                              • trade <span className="mono">{fmt(a.details.trade_time)}</span>
                            </>
                          ) : null}
                        </div>
                      ) : null}
                    </td>
                    <td className="mono muted">
                      {a.employee_id ? `emp=${a.employee_id} ` : ""}
                      {a.document_id ? `doc=${a.document_id} ` : ""}
                      {a.trade_id ? `trade=${a.trade_id}` : ""}
                    </td>
                    <td className="muted">{fmt(a.created_at)}</td>
                  </tr>
                );
              })}
              {alerts.length === 0 ? (
                <tr>
                  <td colSpan={5} className="muted alertEmptyState">
                    <div>No alerts triggered</div>
                    <div>Real-time monitoring will surface risks here</div>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      {/* ═══ ABOUT SECTION ═══ */}
      <section id="about" className="infoSection sectionFade">
        <div className="card commandCard rounded-3xl bg-white/5 backdrop-blur-xl">
          <div className="commandLabel uppercase tracking-[0.18em] text-white/50">System Overview</div>
          <div className="cardHeader">
            <div className="cardTitle">About MNPI Guard</div>
          </div>
          <div className="overviewCard max-w-3xl">
            <div className="overviewIcon"><FiGrid /></div>
            <div className="cardHint">
              MNPI Guard Command Center is a real-time compliance intelligence system that detects insider trading risks by linking sensitive document access with employee trading behavior. It enables compliance teams to monitor activity, investigate anomalies, and act on high-risk signals through a unified operational interface.
            </div>
          </div>
        </div>
      </section>

      <section id="how-it-works" className="infoSection sectionFade">
        <div className="commandLabel uppercase tracking-[0.18em] text-white/50">Workflow</div>
        <div className="cardTitle" style={{ marginBottom: 16 }}>How MNPI Guard Works</div>
        <div className="stepsGrid">
          <div className="stepCard">
            <div className="stepNumber">1</div>
            <div className="stepTitle">Upload MNPI Document</div>
            <div className="stepDesc">Analyze sensitive documents and extract company signals.</div>
          </div>
          <div className="stepCard">
            <div className="stepNumber">2</div>
            <div className="stepTitle">Track Employee Access</div>
            <div className="stepDesc">Monitor which employees accessed sensitive information.</div>
          </div>
          <div className="stepCard">
            <div className="stepNumber">3</div>
            <div className="stepTitle">Import Trading Activity</div>
            <div className="stepDesc">Upload employee trade data for analysis.</div>
          </div>
          <div className="stepCard">
            <div className="stepNumber">4</div>
            <div className="stepTitle">Detect Risk Correlation</div>
            <div className="stepDesc">Identify suspicious patterns linking access and trades.</div>
          </div>
        </div>
      </section>

      <section id="contact" className="infoSection sectionFade">
        <div className="card commandCard rounded-3xl bg-white/5 backdrop-blur-xl">
          <div className="commandLabel uppercase tracking-[0.18em] text-white/50">Contact / Support</div>
          <div className="cardTitle">Support</div>
          <div className="cardHint" style={{ marginTop: 6 }}>
            Need help deploying or customizing the Command Center?
            <br />
            Contact our compliance solutions team.
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <a className="btn" href="mailto:support@mnpiguard.local">
              <FiMail /> support@mnpiguard.local
            </a>
            <button className="btn ghostBtn" type="button">
              <FiHeadphones /> Request Demo
            </button>
          </div>
        </div>
      </section>

      {showFlaggedModal ? (
        <div className="modalOverlay" onClick={() => setShowFlaggedModal(false)}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <div className="cardHeader">
              <div>
                <div className="cardTitle">Flagged employees</div>
                <div className="cardHint">Employees with open high-severity alerts.</div>
              </div>
              <button className="btn" onClick={() => setShowFlaggedModal(false)}>
                Close
              </button>
            </div>

            <table className="table">
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Open high alerts</th>
                  <th>Focus</th>
                </tr>
              </thead>
              <tbody>
                {flaggedEmployees.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="muted">
                      No flagged employees right now.
                    </td>
                  </tr>
                ) : (
                  flaggedEmployees.map((f) => (
                    <tr key={f.employeeId}>
                      <td className="mono">{f.employeeId}</td>
                      <td>
                        <span className="badge badgeBad">{f.count}</span>
                      </td>
                      <td>
                        <button
                          className="btn"
                          type="button"
                          onClick={() => onFocusFlaggedEmployee(f.employeeId)}
                          disabled={!!busy}
                        >
                          Focus
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

    </div>
  );
}

