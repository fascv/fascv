const WINDOWS = [
  { id: "1h", query: "1h" },
  { id: "24h", query: "24h" },
];
let refreshTimer = null;

function qs(id) {
  return document.getElementById(id);
}

function classifyArrow(arrow) {
  if (!arrow) return "neutral";
  if (arrow.includes("▲")) return "up";
  if (arrow.includes("▼")) return "down";
  return "neutral";
}

function signalLabel(arrow) {
  const cls = classifyArrow(arrow);
  if (cls === "up") return "Bullish";
  if (cls === "down") return "Bearish";
  return "Neutral";
}

function signalStateLabel(value) {
  const normalized = String(value || "").toLowerCase();
  if (normalized === "no_signal") return "No Signal";
  if (normalized === "risk_on") return "Risk-On";
  if (normalized === "risk_off") return "Risk-Off";
  if (normalized === "neutral") return "Neutral";
  return normalized || "unbekannt";
}

function signalGlyphMarkup(arrow) {
  const cls = classifyArrow(arrow);
  let count = 1;
  if (cls === "up") count = (arrow.match(/▲/g) || []).length || 1;
  if (cls === "down") count = (arrow.match(/▼/g) || []).length || 1;
  if (cls === "neutral") {
    return `<span class="signal-glyph neutral" aria-hidden="true"></span>`;
  }
  const pieces = [];
  for (let i = 0; i < count; i += 1) {
    pieces.push(`<span class="signal-glyph ${cls}" aria-hidden="true"></span>`);
  }
  return pieces.join("");
}

function fmtScore(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return value >= 0 ? `+${value.toFixed(3)}` : value.toFixed(3);
}

async function fetchArrow(windowValue) {
  const res = await fetch(`/arrow?window=${encodeURIComponent(windowValue)}&include_reasons=true&reason_limit=3`);
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`Arrow ${windowValue}: ${err}`);
  }
  return res.json();
}

async function fetchReportsSummary() {
  const res = await fetch("/reports/summary");
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`Reports summary: ${err}`);
  }
  return res.json();
}

async function fetchAlertsReport() {
  const res = await fetch("/reports/alerts");
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`Alerts report: ${err}`);
  }
  return res.json();
}

function fmtContribution(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return value >= 0 ? `+${value.toFixed(3)}` : value.toFixed(3);
}

function fmtReasonTime(value) {
  return fmtGermanTs(value, "Zeit unbekannt");
}

function fmtReportTime(value) {
  return fmtGermanTs(value, "-");
}

function fmtGermanForensicUtc(value) {
  return fmtGermanTs(value, "-");
}

function fmtGermanTs(value, fallback) {
  if (!value) return fallback || "-";
  let s = String(value).trim();
  // trim fractional seconds to milliseconds to keep Date parsing robust
  s = s.replace(/\.(\d{3})\d+(Z|[+-]\d\d:\d\d)$/, ".$1$2");
  if (s.endsWith("Z")) s = s.slice(0, -1) + "+00:00";
  const parsed = new Date(s);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const dd = String(parsed.getUTCDate()).padStart(2, "0");
  const mm = String(parsed.getUTCMonth() + 1).padStart(2, "0");
  const yy = String(parsed.getUTCFullYear()).slice(-2);
  const hh = String(parsed.getUTCHours()).padStart(2, "0");
  const mi = String(parsed.getUTCMinutes()).padStart(2, "0");
  const ss = String(parsed.getUTCSeconds()).padStart(2, "0");
  return `${dd}-${mm}-${yy} T ${hh}:${mi}:${ss}`;
}

function confidenceLabel(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return "unbekannt";
  if (value >= 0.75) return "hoch";
  if (value >= 0.45) return "mittel";
  return "niedrig";
}

function strengthLabel(score) {
  const abs = Math.abs(Number(score) || 0);
  if (abs >= 0.67) return "stark";
  if (abs >= 0.34) return "mittel";
  if (abs > 0) return "leicht";
  return "keine";
}

function modeLabel(mode) {
  const normalized = String(mode || "").toLowerCase();
  if (normalized === "llm") return "LLM aktiv";
  if (normalized === "llm_hybrid") return "Hybrid (Rule+LLM+Learn)";
  if (normalized === "llm_no_new_items") return "LLM Cache (keine neuen Meldungen)";
  if (normalized === "llm_no_new_items_hybrid") return "Hybrid (LLM Cache + Rule+Learn)";
  if (normalized === "llm_stale_cache") return "LLM Cache (letzte gueltige Antwort)";
  if (normalized === "llm_stale_cache_hybrid") return "Hybrid (stale LLM + Rule+Learn)";
  if (normalized === "rule_fallback") return "Regel-Fallback";
  if (normalized === "rule_fallback_hybrid") return "Hybrid (Rule+Learn Fallback)";
  if (normalized === "rule_short_window") return "Regelmodus (kurzes Fenster)";
  if (normalized === "rule_short_window_hybrid") return "Hybrid (kurzes Fenster: Rule+Learn)";
  if (normalized === "rule") return "Regelmodus";
  if (normalized === "rule_hybrid") return "Hybrid (Rule+Learn)";
  return normalized || "unbekannt";
}

function regimeLabel(value) {
  const normalized = String(value || "").toLowerCase();
  if (normalized === "risk_on") return "Risk-On";
  if (normalized === "risk_off") return "Risk-Off";
  if (normalized === "no_signal") return "No Signal";
  if (normalized === "neutral") return "Neutral";
  return normalized || "unbekannt";
}

function windowLabel(id) {
  if (id === "1h") return "in der letzten Stunde";
  if (id === "24h") return "in den letzten 24 Stunden";
  return "im gewaehlten Fenster";
}

function plainSummaryText(result, windowId) {
  const signalState = String(result.signal_state || "").toLowerCase();
  if (signalState === "no_signal") {
    return `Noch kein belastbares Signal ${windowLabel(windowId)} (zu wenig relevante Daten).`;
  }
  const trend = classifyArrow(result.arrow);
  const strength = strengthLabel(result.score);
  const timePart = windowLabel(windowId);
  if (trend === "up") return `Positiver News-Impuls ${timePart} (${strength}).`;
  if (trend === "down") return `Negativer News-Impuls ${timePart} (${strength}).`;
  return `Aktuell kein klarer News-Impuls ${timePart}.`;
}

function renderPlain(windowId, result) {
  const textEl = qs(`plain-${windowId}`);
  const metaEl = qs(`plain-meta-${windowId}`);
  if (!textEl || !metaEl) return;

  textEl.textContent = plainSummaryText(result, windowId);
  const confidence = confidenceLabel(result.confidence);
  const mode = modeLabel(result.mode_effective);
  const items = Number(result.contributing_items) || 0;
  const signalState = signalStateLabel(result.signal_state);
  const relevance = Number(result.coverage_relevance_sum);
  const relevancePart = Number.isFinite(relevance) ? relevance.toFixed(2) : "-";
  const regime = regimeLabel(result.regime);
  const regimeStrength = Number(result.regime_strength);
  const regimePart =
    Number.isFinite(regimeStrength) ? `${regime} (${regimeStrength.toFixed(2)})` : regime;
  const note = typeof result.notes === "string" && result.notes.trim() ? ` | Hinweis: ${result.notes.trim()}` : "";
  const warning = typeof result.warning === "string" && result.warning.trim() ? ` | Warnung: ${result.warning.trim()}` : "";
  metaEl.textContent = `Signal: ${signalState} | Regime: ${regimePart} | Vertrauen: ${confidence} | Datenbasis: ${items} Meldungen (Relevanz ${relevancePart}) | System: ${mode}${note}${warning}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => {
    if (ch === "&") return "&amp;";
    if (ch === "<") return "&lt;";
    if (ch === ">") return "&gt;";
    if (ch === '"') return "&quot;";
    return "&#39;";
  });
}

function setReasonsVisibility(visible) {
  const lists = document.querySelectorAll(".reasons");
  for (const list of lists) {
    list.classList.toggle("hidden", !visible);
  }
}

function renderReasons(windowId, reasons) {
  const list = qs(`reasons-${windowId}`);
  if (!list) return;

  if (!Array.isArray(reasons) || !reasons.length) {
    list.innerHTML = "<li class='reason-item'><p class='reason-title'>Keine dominanten Gruende.</p></li>";
    return;
  }

  const html = reasons
    .map((reason) => {
      const title = reason.url
        ? `<a href="${encodeURI(reason.url)}" target="_blank" rel="noopener">${escapeHtml(reason.title)}</a>`
        : escapeHtml(reason.title);
      const timeText = fmtReasonTime(reason.timestamp_utc);
      return `<li class="reason-item">
        <p class="reason-title">${title}</p>
        <p class="reason-meta">${escapeHtml(timeText)} | ${escapeHtml(reason.source)} | ${escapeHtml(reason.category)} | ${fmtContribution(reason.contribution)}</p>
      </li>`;
    })
    .join("");
  list.innerHTML = html;
}

function reportStatusLine(entry) {
  if (!entry || !entry.available) return "missing";
  const ok = entry.ok;
  const trend = String(entry.trend_status || "").trim();
  if (ok === true && trend) return `ok | ${trend}`;
  if (ok === true) return "ok";
  if (ok === false && trend) return `not_ok | ${trend}`;
  if (ok === false) return "not_ok";
  return trend || "available";
}

function renderReportCard(key, entry) {
  const statusEl = qs(`report-${key}-status`);
  const metaEl = qs(`report-${key}-meta`);
  if (!statusEl || !metaEl) return;
  if (!entry || !entry.available) {
    statusEl.textContent = "missing";
    metaEl.textContent = "Datei fehlt oder ist unlesbar.";
    return;
  }
  const when = fmtReportTime(entry.generated_at_utc);
  const alertsTotal =
    Number.isFinite(Number(entry.alerts_total)) ? ` | alerts ${Number(entry.alerts_total)}` : "";
  statusEl.textContent = reportStatusLine(entry);
  metaEl.textContent = `${when}${alertsTotal}`;
}

function renderAlertsMiniList(alerts) {
  const list = qs("report-alert-list");
  if (!list) return;
  if (!Array.isArray(alerts) || !alerts.length) {
    list.innerHTML = "<li class='reason-item'><p class='reason-title'>Keine aktiven Alerts.</p></li>";
    return;
  }
  const html = alerts
    .slice(0, 4)
    .map((alert) => {
      const code = escapeHtml(alert.code || "unknown");
      const severity = escapeHtml(alert.severity || "unknown");
      const message = escapeHtml(alert.message || "");
      return `<li class="reason-item"><p class="reason-title">[${severity}] ${code}</p><p class="reason-meta">${message}</p></li>`;
    })
    .join("");
  list.innerHTML = html;
}

async function refreshReports() {
  const updateEl = qs("reports-update");
  try {
    const [summary, alertsPayload] = await Promise.all([fetchReportsSummary(), fetchAlertsReport()]);
    renderReportCard("hybrid", summary.hybrid);
    renderReportCard("source", summary.source_quality);
    renderReportCard("alerts", summary.alerts);
    const latestAlerts = alertsPayload.latest && Array.isArray(alertsPayload.latest.alerts) ? alertsPayload.latest.alerts : [];
    renderAlertsMiniList(latestAlerts);
    updateEl.textContent = new Date().toLocaleString();
  } catch (err) {
    updateEl.textContent = `Fehler: ${err.message}`;
    renderReportCard("hybrid", null);
    renderReportCard("source", null);
    renderReportCard("alerts", null);
    renderAlertsMiniList([]);
  }
}

async function refreshArrows() {
  const panel = qs("arrow-cards");
  panel.classList.add("loading");

  try {
    const settled = await Promise.allSettled(
      WINDOWS.map((spec) => fetchArrow(spec.query).then((result) => ({ spec, result })))
    );
    const successful = [];
    const failures = [];
    for (const entry of settled) {
      if (entry.status === "fulfilled") {
        successful.push(entry.value);
      } else {
        failures.push(entry.reason);
      }
    }

    for (const { spec, result } of successful) {
      const arrowEl = qs(`arrow-${spec.id}`);
      const trendEl = qs(`trend-${spec.id}`);
      const scoreEl = qs(`score-${spec.id}`);
      const card = document.querySelector(`.arrow-card[data-window='${spec.id}']`);
      if (!arrowEl || !trendEl || !scoreEl || !card) continue;

      arrowEl.innerHTML = signalGlyphMarkup(result.arrow);
      trendEl.textContent = signalLabel(result.arrow);
      const modeNote = result.mode_effective ? ` | ${result.mode_effective}` : "";
      const rulePart = `rule ${fmtScore(result.rule_score)}`;
      const llmPart = Number.isFinite(Number(result.llm_score)) ? ` | llm ${fmtScore(result.llm_score)}` : "";
      const learnPart = Number.isFinite(Number(result.learn_score)) ? ` | learn ${fmtScore(result.learn_score)}` : "";
      let weightsPart = "";
      if (result.score_weights_used && typeof result.score_weights_used === "object") {
        const rw = Number(result.score_weights_used.rule);
        const lw = Number(result.score_weights_used.llm);
        const ew = Number(result.score_weights_used.learn);
        const chunks = [];
        if (Number.isFinite(rw)) chunks.push(`r=${rw.toFixed(2)}`);
        if (Number.isFinite(lw)) chunks.push(`l=${lw.toFixed(2)}`);
        if (Number.isFinite(ew)) chunks.push(`k=${ew.toFixed(2)}`);
        if (chunks.length) weightsPart = ` | w(${chunks.join(",")})`;
      }
      scoreEl.textContent = `final ${fmtScore(result.final_score)} | ${rulePart}${llmPart}${learnPart}${weightsPart} | items ${result.contributing_items}${modeNote}`;
      renderReasons(spec.id, result.reasons || []);
      renderPlain(spec.id, result);

      card.classList.remove("up", "down", "neutral");
      card.classList.add(classifyArrow(result.arrow));
    }

    for (const spec of WINDOWS) {
      const hasData = successful.some((ok) => ok.spec.id === spec.id);
      if (hasData) continue;
      const trendEl = qs(`trend-${spec.id}`);
      const scoreEl = qs(`score-${spec.id}`);
      const card = document.querySelector(`.arrow-card[data-window='${spec.id}']`);
      const plainEl = qs(`plain-${spec.id}`);
      const plainMetaEl = qs(`plain-meta-${spec.id}`);
      if (trendEl) trendEl.textContent = "Fehler";
      if (scoreEl) scoreEl.textContent = "score - | items -";
      if (plainEl) plainEl.textContent = "Abruf fehlgeschlagen. Neues Update folgt automatisch.";
      if (plainMetaEl) plainMetaEl.textContent = "Bitte Logs/Netzwerk pruefen.";
      if (card) {
        card.classList.remove("up", "down");
        card.classList.add("neutral");
      }
    }

    const firstWarning = successful
      .map((entry) => entry.result.warning)
      .find((w) => typeof w === "string" && w.trim().length > 0);
    const firstFailure = failures.find((err) => err && err.message);
    const failureSuffix = firstFailure ? ` | Fehler: ${firstFailure.message}` : "";
    const suffix = firstWarning ? ` | Hinweis: ${firstWarning}` : failureSuffix;
    qs("last-update").textContent = `${new Date().toLocaleString()}${suffix}`;
  } catch (err) {
    qs("last-update").textContent = `Fehler: ${err.message}`;
  } finally {
    panel.classList.remove("loading");
  }
}

function renderForensicRows(items) {
  const body = qs("forensic-body");
  if (!items.length) {
    body.innerHTML = "<tr><td colspan='6' class='placeholder'>Keine Meldungen in diesem Fenster.</td></tr>";
    return;
  }

  const sortedItems = [...items].sort((a, b) => {
    const taRaw = String(a.timestamp_utc || "");
    const tbRaw = String(b.timestamp_utc || "");
    const lexical = tbRaw.localeCompare(taRaw);
    if (lexical !== 0) return lexical;
    const ta = Date.parse(taRaw);
    const tb = Date.parse(tbRaw);
    return (Number.isFinite(tb) ? tb : 0) - (Number.isFinite(ta) ? ta : 0);
  });

  const rows = sortedItems
    .map((item) => {
      const impact = fmtScore(item.impact);
      const contribution = item.is_future_vs_ts
        ? "n/a (future)"
        : fmtScore(item.contribution_at_ts);
      const title = item.url
        ? `<a href="${encodeURI(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.title)}</a>`
        : escapeHtml(item.title);
      return `<tr>
        <td class="forensic-time">${escapeHtml(fmtGermanForensicUtc(item.timestamp_utc))}</td>
        <td>${escapeHtml(item.source)}</td>
        <td>${escapeHtml(item.category)}</td>
        <td>${impact}</td>
        <td>${contribution}</td>
        <td>${title}</td>
      </tr>`;
    })
    .join("");

  body.innerHTML = rows;
}

async function loadForensic(event) {
  event.preventDefault();
  const ts = qs("ts-input").value.trim();
  const window = qs("window-input").value.trim();
  await loadForensicData(ts, window);
}

async function loadForensicData(ts, window) {
  if (!ts || !window) return;
  const meta = qs("forensic-meta");
  meta.textContent = "Loading...";

  try {
    const res = await fetch(
      `/forensic?ts=${encodeURIComponent(ts)}&window=${encodeURIComponent(window)}`
    );
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(txt);
    }

    const payload = await res.json();
    meta.textContent = `Items: ${payload.count} | ${fmtGermanForensicUtc(payload.window_start)} .. ${fmtGermanForensicUtc(payload.window_end)} | Rule@TS: ${fmtScore(payload.rule_score_at_ts)} (${payload.rule_regime_at_ts}, ${payload.rule_arrow_at_ts})`;
    renderForensicRows(payload.items);
  } catch (err) {
    meta.textContent = `Forensic error: ${err.message}`;
    renderForensicRows([]);
  }
}

function setDefaultTimestamp() {
  const nowIso = new Date().toISOString();
  qs("ts-input").value = nowIso;
}

function refreshIntervalMs() {
  return 60 * 1000;
}

function scheduleAutoRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
  }
  refreshTimer = setInterval(() => {
    refreshArrows();
    refreshReports();
  }, refreshIntervalMs());
}

function setup() {
  setDefaultTimestamp();
  setReasonsVisibility(true);
  qs("forensic-form").addEventListener("submit", loadForensic);
  qs("toggle-reasons").addEventListener("change", (event) => {
    setReasonsVisibility(event.target.checked);
  });

  refreshArrows();
  refreshReports();
  loadForensicData(qs("ts-input").value.trim(), qs("window-input").value.trim());
  scheduleAutoRefresh();
}

setup();
