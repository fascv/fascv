(function () {
  const tokenInput = document.getElementById('token');
  const reportDateInput = document.getElementById('reportDate');
  const reportSymbolInput = document.getElementById('reportSymbol');
  const loadReportBtn = document.getElementById('loadReportBtn');
  const loadRotationBtn = document.getElementById('loadRotationBtn');
  const loadLiveBtn = document.getElementById('loadLiveBtn');
  const statusEl = document.getElementById('status');

  const periodEl = document.getElementById('period');
  const bundlesEl = document.getElementById('kpiBundles');
  const symbolsEl = document.getElementById('kpiSymbols');
  const buyEl = document.getElementById('kpiBuy');
  const sellEl = document.getElementById('kpiSell');
  const proceedsEl = document.getElementById('kpiProceeds');

  const symbolRows = document.getElementById('symbolRows');
  const bundleRows = document.getElementById('bundleRows');
  const tradeRowsEl = document.getElementById('tradeRows');
  const tradeTotalPnlEl = document.getElementById('tradeTotalPnl');
  const rotationRows = document.getElementById('rotationRows');
  const rotationMetaEl = document.getElementById('rotationMeta');
  const rotationStatusEl = document.getElementById('rotationStatus');
  const liveRows = document.getElementById('liveRows');
  const liveCandidateRows = document.getElementById('liveCandidateRows');
  const liveCandidateMetaEl = document.getElementById('liveCandidateMeta');
  const liveCandidateBlockEl = document.getElementById('liveCandidateBlock');
  const liveMetaEl = document.getElementById('liveMeta');
  const liveStatusEl = document.getElementById('liveStatus');
  const strategyRows = document.getElementById('strategyRows');
  const strategyMetaEl = document.getElementById('strategyMeta');
  const inTradeRows = document.getElementById('inTradeRows');
  const inTradeMetaEl = document.getElementById('inTradeMeta');

  const TOKEN_KEY = 'relay_token';
  const REPORT_SYMBOL_KEY = 'relay_report_symbol';
  const MIN_REPORT_LOAD_INTERVAL_MS = 20000;
  const MIN_ROTATION_LOAD_INTERVAL_MS = 8000;
  const MIN_LIVE_LOAD_INTERVAL_MS = 5000;
  const LIVE_REFRESH_INTERVAL_MS = 10000;
  let lastReportLoadAtMs = 0;
  let lastRotationLoadAtMs = 0;
  let lastLiveLoadAtMs = 0;
  let liveLoadInFlight = false;

  function todayStr() {
    const now = new Date();
    const yyyy = now.getFullYear();
    const mm = String(now.getMonth() + 1).padStart(2, '0');
    const dd = String(now.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }

  function fmtNum(v) {
    const n = Number(v || 0);
    return n.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtSignedNum(v) {
    const n = Number(v || 0);
    const abs = Math.abs(n);
    const text = abs.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (n > 0) return `+${text}`;
    if (n < 0) return `-${text}`;
    return text;
  }

  function fmtQty(v) {
    const n = Number(v || 0);
    return n.toLocaleString('de-DE', { minimumFractionDigits: 0, maximumFractionDigits: 8 });
  }

  function fmtPrice(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n <= 0) return '-';
    let digits = 8;
    if (n >= 1000) digits = 2;
    else if (n >= 10) digits = 4;
    else if (n >= 1) digits = 5;
    else if (n >= 0.1) digits = 6;
    return n.toLocaleString('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtAgeSec(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n < 0) return '-';
    if (n >= 10) return `${Math.round(n)}s`;
    return `${n.toFixed(1)}s`;
  }

  function fmtDateTime(iso) {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso || '-';
    return dt.toLocaleString('de-DE', { hour12: false });
  }

  function fmtTime(iso) {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return '-';
    return dt.toLocaleTimeString('de-DE', {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
  }

  function fmtPct(v, digits = 0) {
    const n = Number(v || 0);
    if (!Number.isFinite(n)) return '-';
    const pct = n <= 1 ? n * 100 : n;
    return `${pct.toLocaleString('de-DE', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    })}%`;
  }

  function fmtPctValue(v, digits = 1) {
    const n = Number(v || 0);
    if (!Number.isFinite(n)) return '-';
    return `${n.toLocaleString('de-DE', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    })}%`;
  }

  function fmtHoldSec(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n <= 0) return '-';
    if (n < 60) return `${Math.round(n)}s`;
    if (n < 3600) return `${Math.round(n / 60)}m`;
    return `${(n / 3600).toFixed(1).replace('.', ',')}h`;
  }

  function holdSecondsFromIso(iso) {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return NaN;
    const sec = (Date.now() - dt.getTime()) / 1000;
    if (!Number.isFinite(sec) || sec < 0) return NaN;
    return sec;
  }

  function escapeHtml(value) {
    return String(value || '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function cleanSymbol(value) {
    return String(value || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  }

  function splitPair(symbolOrMarket) {
    const clean = cleanSymbol(symbolOrMarket);
    if (!clean) return { base: '', quote: '' };

    const quoteSuffixes = ['USDC', 'USDT', 'BUSD', 'FDUSD', 'TUSD', 'BTC', 'ETH', 'BNB', 'TRY', 'EUR'];
    for (const suffix of quoteSuffixes) {
      if (clean.endsWith(suffix) && clean.length > suffix.length) {
        return { base: clean.slice(0, -suffix.length), quote: suffix };
      }
    }
    return { base: clean, quote: 'USDC' };
  }

  function symbolLink(symbol, market) {
    const label = String(symbol || '').trim();
    if (!label) return '';
    const fromMarket = splitPair(market);
    const fromSymbol = splitPair(label);

    const base = fromMarket.base || fromSymbol.base;
    const quote = fromMarket.quote || fromSymbol.quote || 'USDC';
    if (!base || base.length < 2) return escapeHtml(label);

    const href = `https://www.binance.com/en/trade/${encodeURIComponent(base)}_${encodeURIComponent(quote)}?type=spot`;
    return `<a href="${href}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  }

  function setStatus(msg, type) {
    statusEl.textContent = msg || '';
    statusEl.classList.remove('error', 'ok');
    if (type) statusEl.classList.add(type);
  }

  function setRotationStatus(msg, type) {
    rotationStatusEl.textContent = msg || '';
    rotationStatusEl.classList.remove('error', 'ok');
    if (type) rotationStatusEl.classList.add(type);
  }

  function setLiveStatus(msg, type) {
    liveStatusEl.textContent = msg || '';
    liveStatusEl.classList.remove('error', 'ok');
    if (type) liveStatusEl.classList.add(type);
  }

  function boolBadge(value) {
    return `<span class="badge ${value ? 'ok' : 'no'}">${value ? 'OK' : 'Nein'}</span>`;
  }

  function laneBadge(item) {
    if (item.selected) return '<span class="badge ok">Aktiv</span>';
    if (item.eligible) return '<span class="badge warn">Kandidat</span>';
    return '<span class="badge no">Aus</span>';
  }

  function postDumpBadge(value) {
    if (value) return '<span class="badge warn">Pending</span>';
    return '<span class="badge ok">Frei</span>';
  }

  function liveStateBadges(item, options = {}) {
    const showReady = options.showReady !== false;
    const showManualEntry = options.showManualEntry !== false;
    const badges = [];
    if (!item.statusOk) {
      badges.push('<span class="badge no">Down</span>');
    } else if (item.currentlyTrading) {
      badges.push('<span class="badge ok">Im Trade</span>');
    } else if (item.selected && !item.manualEntryExitOnly) {
      badges.push('<span class="badge warn">Aktiv-4</span>');
    } else if (item.running) {
      badges.push('<span class="badge warn">Nur Watch</span>');
    } else {
      badges.push('<span class="badge no">Watch</span>');
    }

    if (showReady && item.tradeReady && !item.currentlyTrading) badges.push('<span class="badge ok">Ready</span>');
    if (showManualEntry && item.manualEntryExitOnly && !item.currentlyTrading) {
      badges.push('<span class="badge warn">Manuell Entry</span>');
    }
    if (item.stale) badges.push('<span class="badge warn">Stale</span>');
    if (Number(item.openOrdersCount || 0) > 0) {
      badges.push(`<span class="badge warn">${Number(item.openOrdersCount || 0)} Order</span>`);
    }
    return badges.join(' ');
  }

  function strategyLabel(strategy) {
    const key = String(strategy || '').trim().toLowerCase();
    if (key === 'pullback_continuation') return 'Pullback-Cont.';
    if (key === 'breakout_retest') return 'Breakout-Retest';
    if (key === 'relative_strength') return 'Relative Strength';
    if (key === 'breakout') return 'Breakout';
    if (key === 'staircase') return 'Staircase';
    if (key === 'continuation') return 'Continuation';
    if (key === 'rebound') return 'Rebound';
    if (key === 'unknown') return 'Unbekannt';
    return key || '-';
  }

  function strategyActionBadge(action) {
    const mode = String(action?.mode || '').trim().toLowerCase();
    const slotTarget = Number(action?.slotTarget || 0);
    if (!mode) return '<span class="badge no">-</span>';
    if (mode === 'primary') return `<span class="badge ok">Primary ${slotTarget || ''}</span>`;
    if (mode === 'secondary') return `<span class="badge warn">Secondary ${slotTarget || ''}</span>`;
    if (mode === 'watch') return `<span class="badge warn">Watch ${slotTarget || ''}</span>`;
    if (mode === 'pause') return '<span class="badge no">Pause</span>';
    return `<span class="badge no">${escapeHtml(mode)}</span>`;
  }

  function strategyRowClass(item) {
    const mode = String(item?.action?.mode || '').trim().toLowerCase();
    if (mode === 'pause') return 'row-strategy-pause';
    if (mode === 'primary') return 'row-strategy-primary';
    if (mode === 'secondary') return 'row-strategy-secondary';
    return '';
  }

  function compactList(items) {
    if (!Array.isArray(items) || !items.length) return '-';
    return items.filter(Boolean).map((value) => escapeHtml(String(value))).join(', ');
  }

  function compactReasons(reasons) {
    if (!reasons || typeof reasons !== 'object') return '-';
    const entries = Object.entries(reasons)
      .filter(([, value]) => Number(value || 0) > 0)
      .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
      .slice(0, 3);
    if (!entries.length) return '-';
    return entries
      .map(([key, value]) => `${escapeHtml(String(key))} ${Number(value || 0)}`)
      .join(' | ');
  }

  function laneRank(item) {
    if (item && item.selected) return 2;
    if (item && item.eligible) return 1;
    return 0;
  }

  function okCount(item) {
    return (
      Number(Boolean(item && item.point1Ok)) +
      Number(Boolean(item && item.point2Ok)) +
      Number(Boolean(item && item.point3Ok))
    );
  }

  function tupleCompare(a, b) {
    const len = Math.min(a.length, b.length);
    for (let i = 0; i < len; i += 1) {
      if (a[i] < b[i]) return -1;
      if (a[i] > b[i]) return 1;
    }
    return 0;
  }

  function rotationTuple(item) {
    return [
      3 - okCount(item),
      item?.point1Ok ? 0 : 1,
      item?.point2Ok ? 0 : 1,
      item?.point3Ok ? 0 : 1,
      -laneRank(item),
      -Number(item?.score || 0),
      String(item?.symbol || ''),
    ];
  }

  function liveTuple(item) {
    const inTrade = Boolean(
      item?.currentlyTrading ||
      item?.positionOpen ||
      Number(item?.openOrdersCount || 0) > 0
    );
    const symbol = String(item?.symbol || '');
    return [
      inTrade ? 0 : 1,
      inTrade ? symbol : '',
      item?.selected ? 0 : 1,
      item?.running ? 0 : 1,
      item?.statusOk ? 0 : 1,
      item?.stale ? 1 : 0,
      -Number(item?.openOrdersCount || 0),
      symbol,
    ];
  }

  function sortedRotationRows(items) {
    return [...items].sort((a, b) => tupleCompare(rotationTuple(a), rotationTuple(b)));
  }

  function sortedLiveRows(items) {
    return [...items].sort((a, b) => tupleCompare(liveTuple(a), liveTuple(b)));
  }

  function isPrimaryLiveRow(item) {
    if (!item || typeof item !== 'object') return false;
    if (item.currentlyTrading) return true;
    if (item.selected) return true;
    if (item.positionOpen) return true;
    return Number(item.openOrdersCount || 0) > 0;
  }

  function isWatchCandidateRow(item) {
    if (!item || typeof item !== 'object') return false;
    if (isPrimaryLiveRow(item)) return false;
    return Boolean(item.running);
  }

  function candidateTuple(item) {
    return [
      item?.tradeReady ? 0 : 1,
      item?.statusOk ? 0 : 1,
      item?.stale ? 1 : 0,
      -Number(item?.score || 0),
      String(item?.gateReason || ''),
      String(item?.symbol || ''),
    ];
  }

  function sortedCandidateRows(items) {
    return [...items].sort((a, b) => tupleCompare(candidateTuple(a), candidateTuple(b)));
  }

  function liveBlockReasonText(item) {
    const gate = String(item?.gateReason || '').trim();
    if (!item?.statusOk) return item?.statusError || 'Lane nicht erreichbar';
    if (!gate) {
      if (item?.manualEntryExitOnly) return 'Nur manuelles Entry erlaubt';
      if (!item?.tradingEnabled) return 'Trading deaktiviert';
      return 'Aktuell kein Kauf-Trigger';
    }
    if (gate === 'rule_7d_crash_event') {
      return 'In den letzten 7 Tagen gab es einen starken Kurssturz';
    }
    if (gate === 'rule_not_in_lower_quarter') {
      const pos7d = Number(item?.selectorPos7dPct);
      if (Number.isFinite(pos7d)) {
        return `Noch zu hoch in der 7d-Range (${fmtPctValue(pos7d, 1)})`;
      }
      const pos48h = Number(item?.selectorPos48hPct);
      if (Number.isFinite(pos48h)) {
        return `Noch zu hoch in der 7d-Range (Fallback 48h: ${fmtPctValue(pos48h, 1)})`;
      }
      const pos36h = Number(item?.selectorPos36hPct);
      if (Number.isFinite(pos36h)) {
        return `Noch zu hoch in der 7d-Range (Fallback 36h: ${fmtPctValue(pos36h, 1)})`;
      }
      const pos = Number(item?.selectorPosPct);
      if (Number.isFinite(pos)) {
        return `Noch zu hoch in der 7d-Range (Fallback kurz: ${fmtPctValue(pos, 1)})`;
      }
      return 'Noch zu hoch in der 7d-Range (Wert wird aktualisiert)';
    }
    if (gate === 'data_pending') return 'Daten werden noch aktualisiert';
    if (gate === 'rule_3day_cycle_miss') return '3-Tage-Zyklus noch nicht passend';
    if (gate === 'rule_micro_valley_context_miss') return 'Micro-Valley-Bedingung fehlt';
    if (gate === 'rule_not_rising_yet') return 'Bewegung steigt noch nicht';
    return gate;
  }

  function clearRotation() {
    rotationRows.innerHTML = '';
    rotationMetaEl.innerHTML = 'Stand: -';
    setRotationStatus('');
  }

  function clearLive() {
    liveRows.innerHTML = '';
    if (liveCandidateRows) liveCandidateRows.innerHTML = '';
    if (liveCandidateMetaEl) liveCandidateMetaEl.textContent = '';
    if (liveCandidateBlockEl) liveCandidateBlockEl.style.display = 'none';
    liveMetaEl.innerHTML = 'Stand: -';
    strategyRows.innerHTML = '';
    strategyMetaEl.innerHTML = 'Meta-Stand: -';
    if (inTradeRows) inTradeRows.innerHTML = '';
    if (inTradeMetaEl) inTradeMetaEl.innerHTML = 'Im Trade: -';
    setLiveStatus('');
  }

  function clearReport() {
    symbolRows.innerHTML = '';
    bundleRows.innerHTML = '';
    if (tradeRowsEl) tradeRowsEl.innerHTML = '';
    if (tradeTotalPnlEl) tradeTotalPnlEl.textContent = '0,00 USDC';
    setSummary({});
  }

  function setSummary(report) {
    const day = report.daySummary || {};
    periodEl.innerHTML = `Zeitraum: <strong>${report.fromIso || '-'} bis ${report.toIso || '-'}</strong>`;
    bundlesEl.innerHTML = `Buendel: <strong>${day.bundleCount || 0}</strong>`;
    symbolsEl.innerHTML = `Symbole: <strong>${day.symbolCount || 0}</strong>`;
    buyEl.innerHTML = `Einkauf brutto: <strong>${fmtNum(day.buyGrossUsdc)} USDC</strong>`;
    sellEl.innerHTML = `Verkauf brutto: <strong>${fmtNum(day.sellGrossUsdc)} USDC</strong>`;
    proceedsEl.innerHTML = `Netto-PnL (geschlossen): <strong>${fmtNum(day.proceedsUsdc)} USDC</strong>`;
  }

  function setSymbolRows(items) {
    if (!items || !items.length) {
      symbolRows.innerHTML = '<tr><td colspan="5">Keine Daten</td></tr>';
      return;
    }

    symbolRows.innerHTML = items.map((it) => `
      <tr>
        <td>${symbolLink(it.symbol)}</td>
        <td>${it.bundleCount || 0}</td>
        <td>${fmtNum(it.buyGrossUsdc)} USDC</td>
        <td>${fmtNum(it.sellGrossUsdc)} USDC</td>
        <td>${fmtNum(it.proceedsUsdc)} USDC</td>
      </tr>
    `).join('');
  }

  function setBundleRows(items) {
    if (!items || !items.length) {
      bundleRows.innerHTML = '<tr><td colspan="7">Keine Daten</td></tr>';
      return;
    }

    bundleRows.innerHTML = items.map((it) => `
      <tr>
        <td>${symbolLink(it.symbol)}</td>
        <td>${fmtQty(it.quantity)}</td>
        <td>${fmtDateTime(it.buyTime)}</td>
        <td>${fmtDateTime(it.sellTime)}</td>
        <td>${fmtNum(it.buyGrossUsdc)} USDC</td>
        <td>${fmtNum(it.sellGrossUsdc)} USDC</td>
        <td>${fmtNum(it.proceedsUsdc)} USDC</td>
      </tr>
    `).join('');
  }

  function aggregateTradeRowsForDisplay(items) {
    if (!Array.isArray(items) || !items.length) return [];
    const merged = new Map();
    const passthrough = [];

    items.forEach((raw, idx) => {
      const it = raw && typeof raw === 'object' ? raw : {};
      const closed = Boolean(it.closed);
      const symbol = String(it.symbol || '').trim().toUpperCase();
      const sellTime = String(it.sellTime || '').trim();
      const sellPrice = Number(it.sellPrice || 0);
      const buyPrice = Number(it.buyPrice || 0);
      const qty = Number(it.quantity || 0);
      const buyGross = Number(it.buyGrossUsdc || 0);
      const sellGross = Number(it.sellGrossUsdc || 0);
      const proceeds = Number(it.proceedsUsdc);
      const buyNotional = (
        Number.isFinite(buyPrice) && buyPrice > 0 && Number.isFinite(qty) && qty > 0
          ? buyPrice * qty
          : (Number.isFinite(buyGross) ? buyGross : 0)
      );
      const sellNotional = (
        Number.isFinite(sellPrice) && sellPrice > 0 && Number.isFinite(qty) && qty > 0
          ? sellPrice * qty
          : (Number.isFinite(sellGross) ? sellGross : 0)
      );

      if (closed && symbol && sellTime) {
        const key = `${symbol}|${sellTime}|${Number.isFinite(sellPrice) ? sellPrice.toFixed(12) : '-'}`;
        const prev = merged.get(key);
        if (!prev) {
          merged.set(key, {
            symbol,
            quantity: Number.isFinite(qty) ? qty : 0,
            buyTime: String(it.buyTime || ''),
            sellTime,
            buyGrossUsdc: Number.isFinite(buyGross) ? buyGross : 0,
            sellGrossUsdc: Number.isFinite(sellGross) ? sellGross : 0,
            proceedsUsdc: Number.isFinite(proceeds) ? proceeds : 0,
            buyNotionalUsdc: Number.isFinite(buyNotional) ? buyNotional : 0,
            sellNotionalUsdc: Number.isFinite(sellNotional) ? sellNotional : 0,
            closed: true
          });
          return;
        }
        prev.quantity += Number.isFinite(qty) ? qty : 0;
        prev.buyGrossUsdc += Number.isFinite(buyGross) ? buyGross : 0;
        prev.sellGrossUsdc += Number.isFinite(sellGross) ? sellGross : 0;
        prev.proceedsUsdc += Number.isFinite(proceeds) ? proceeds : 0;
        prev.buyNotionalUsdc += Number.isFinite(buyNotional) ? buyNotional : 0;
        prev.sellNotionalUsdc += Number.isFinite(sellNotional) ? sellNotional : 0;
        const prevBuyTime = String(prev.buyTime || '').trim();
        const curBuyTime = String(it.buyTime || '').trim();
        if (curBuyTime && (!prevBuyTime || curBuyTime < prevBuyTime)) prev.buyTime = curBuyTime;
        return;
      }

      passthrough.push({
        symbol: String(it.symbol || ''),
        quantity: Number.isFinite(qty) ? qty : 0,
        buyTime: String(it.buyTime || ''),
        sellTime: String(it.sellTime || ''),
        buyGrossUsdc: Number.isFinite(buyGross) ? buyGross : 0,
        sellGrossUsdc: Number.isFinite(sellGross) ? sellGross : 0,
        proceedsUsdc: Number.isFinite(proceeds) ? proceeds : null,
        buyPrice: Number(it.buyPrice || 0),
        sellPrice: Number(it.sellPrice || 0),
        closed,
        __idx: idx
      });
    });

    const grouped = [...merged.values()].map((it) => {
      const qty = Number(it.quantity || 0);
      const buyGross = Number(it.buyGrossUsdc || 0);
      const sellGross = Number(it.sellGrossUsdc || 0);
      const buyNotional = Number(it.buyNotionalUsdc || 0);
      const sellNotional = Number(it.sellNotionalUsdc || 0);
      return {
        ...it,
        buyPrice: qty > 0 ? ((buyNotional > 0 ? buyNotional : buyGross) / qty) : 0,
        sellPrice: qty > 0 ? ((sellNotional > 0 ? sellNotional : sellGross) / qty) : 0
      };
    });

    const out = [...grouped, ...passthrough];
    out.sort((a, b) => {
      const ta = String(a.sellTime || a.buyTime || '');
      const tb = String(b.sellTime || b.buyTime || '');
      if (ta !== tb) return ta.localeCompare(tb);
      const sa = String(a.symbol || '');
      const sb = String(b.symbol || '');
      if (sa !== sb) return sa.localeCompare(sb);
      return Number(a.__idx || 0) - Number(b.__idx || 0);
    });
    return out;
  }

  function setTradeRows(items, daySummary) {
    const displayItems = aggregateTradeRowsForDisplay(items);
    if (!tradeRowsEl) return;
    if (!displayItems.length) {
      tradeRowsEl.innerHTML = '<tr><td colspan="4">Keine Daten</td></tr>';
      if (tradeTotalPnlEl) tradeTotalPnlEl.textContent = `${fmtNum(0)} USDC`;
      return;
    }

    tradeRowsEl.innerHTML = displayItems.map((it) => {
      const qty = Number(it.quantity || 0);
      const buyGross = Number(it.buyGrossUsdc || 0);
      const sellGross = Number(it.sellGrossUsdc || 0);
      const buyPrice = Number(it.buyPrice || (qty > 0 ? (buyGross / qty) : 0));
      const sellPrice = Number(it.sellPrice || (qty > 0 ? (sellGross / qty) : 0));
      const buyCell = `${fmtPrice(buyPrice)} USDC × ${fmtQty(qty)}`;
      const sellCell = (it.closed && sellPrice > 0)
        ? `${fmtPrice(sellPrice)} USDC × ${fmtQty(qty)}<div class="small">Verkauf: ${fmtTime(it.sellTime)}</div>`
        : '-';
      const proceeds = Number(it.proceedsUsdc);
      const hasPnl = Number.isFinite(proceeds);
      const pnlText = hasPnl ? `${fmtSignedNum(proceeds)} USDC` : '-';
      const pnlCls = hasPnl ? pnlClass(proceeds) : 'pnl-flat';

      return `
      <tr>
        <td>${symbolLink(it.symbol)}</td>
        <td>${buyCell}</td>
        <td>${sellCell}</td>
        <td class="${pnlCls}">${pnlText}</td>
      </tr>
    `;
    }).join('');

    const totalPnl = Number(daySummary?.proceedsUsdc || 0);
    if (tradeTotalPnlEl) {
      tradeTotalPnlEl.textContent = `${fmtSignedNum(totalPnl)} USDC`;
      tradeTotalPnlEl.className = pnlClass(totalPnl);
    }
  }

  function setRotationSummary(payload) {
    const summary = payload.summary || {};
    const generated = payload.generatedAt ? fmtDateTime(payload.generatedAt) : '-';
    rotationMetaEl.innerHTML =
      `Stand: <strong>${generated}</strong> | ` +
      `Coins: <strong>${summary.total || 0}</strong> | ` +
      `Aktiv: <strong>${summary.selected || 0}</strong> | ` +
      `1 Sweet OK: <strong>${summary.point1Ok || 0}</strong> | ` +
      `2 Makro OK: <strong>${summary.point2Ok || 0}</strong> | ` +
      `3 Co/Vol OK: <strong>${summary.point3Ok || 0}</strong>`;
  }

  function setRotationRows(items) {
    if (!items || !items.length) {
      rotationRows.innerHTML = '<tr><td colspan="7">Keine Rotationsdaten</td></tr>';
      return;
    }

    const sorted = sortedRotationRows(items);

    rotationRows.innerHTML = sorted.map((it) => `
      <tr>
        <td>${symbolLink(it.symbol, it.market)}</td>
        <td>${laneBadge(it)}</td>
        <td>${boolBadge(it.point1Ok)}</td>
        <td>${boolBadge(it.point2Ok)}</td>
        <td>${boolBadge(it.point3Ok)}</td>
        <td>${postDumpBadge(it.postDumpRecoveryPending)}</td>
        <td>${it.gateReason || '-'}</td>
      </tr>
    `).join('');
  }

  function setLiveSummary(payload) {
    const summary = payload.summary || {};
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    const watchOnlyRunning = rows.filter((item) =>
      item?.running &&
      !item?.selected &&
      !item?.currentlyTrading &&
      !item?.positionOpen &&
      Number(item?.openOrdersCount || 0) <= 0
    ).length;
    const generated = payload.generatedAt ? fmtDateTime(payload.generatedAt) : '-';
    liveMetaEl.innerHTML =
      `Stand: <strong>${generated}</strong> | ` +
      `Coins: <strong>${summary.total || 0}</strong> | ` +
      `Live-Lanes: <strong>${summary.running || 0}</strong> | ` +
      `Nur-Watch: <strong>${watchOnlyRunning}</strong> | ` +
      `Im Trade: <strong>${summary.open || 0}</strong> | ` +
      `Aktiv-4: <strong>${summary.selected || 0}</strong> | ` +
      `Ready: <strong>${summary.tradeReady || 0}</strong> | ` +
      `Stale: <strong>${summary.stale || 0}</strong> | ` +
      `Down: <strong>${summary.down || 0}</strong>`;
  }

  function setLiveStrategies(summary) {
    const generated = summary?.generatedAt ? fmtDateTime(summary.generatedAt) : '-';
    const profile = summary?.currentProfile || '-';
    const riskMode = summary?.riskMode || '-';
    const metaMode = summary?.metaMode || '-';
    const confidence = fmtPct(summary?.confidence || 0, 0);
    const lookbackHours = Number(summary?.lookbackHours || 0);
    const lookback = lookbackHours > 0 ? `${fmtNum(lookbackHours, Number.isInteger(lookbackHours) ? 0 : 1)}h` : '-';
    const notes = String(summary?.notes || '').trim();
    const notesHtml = notes
      ? `<br><span class="small">Hinweis: ${escapeHtml(notes.split(';').join(' | '))}</span>`
      : '';
    strategyMetaEl.innerHTML =
      `Meta-Stand: <strong>${generated}</strong> | ` +
      `Lookback: <strong>${escapeHtml(lookback)}</strong> | ` +
      `Modus: <strong>${escapeHtml(metaMode)}</strong> | ` +
      `Profil: <strong>${escapeHtml(profile)}</strong> | ` +
      `Risiko: <strong>${escapeHtml(riskMode)}</strong> | ` +
      `Vertrauen: <strong>${confidence}</strong>` +
      notesHtml;

    const rows = Array.isArray(summary?.rows) ? summary.rows : [];
    if (!rows.length) {
      strategyRows.innerHTML = '<tr><td colspan="6">Keine Strategie-Daten</td></tr>';
      return;
    }

    strategyRows.innerHTML = rows.map((item) => `
      <tr class="${strategyRowClass(item)}">
        <td>
          <div>${escapeHtml(strategyLabel(item.strategy))}</div>
          <div class="small">${escapeHtml(item.lastExitAt ? fmtDateTime(item.lastExitAt) : 'kein letzter Exit')}</div>
        </td>
        <td>
          <div>${fmtPct(item.weight || 0, 0)} ${strategyActionBadge(item.action)}</div>
          <div class="small">Slots ${Number(item.action?.slotTarget || 0)} | Buy-ready ${Number(item.buyReadyCount || 0)} | ML+ ${Number(item.mlPositiveCount || 0)}</div>
        </td>
        <td>
          <div>${Number(item.tradeCount || 0)}</div>
          <div class="small">Hold ${fmtHoldSec(item.avgHoldSec)} | Watch ${Number(item.watchCandidateCount || 0)}</div>
        </td>
        <td>
          <div class="${pnlClass(item.netPnlUsdc)}">${fmtSignedNum(item.netPnlUsdc)} USDC</div>
          <div class="small">Winrate ${fmtPct(item.winRate || 0, 0)} | avg+ ${fmtSignedNum(item.avgWinUsdc)} | avg- ${fmtSignedNum(item.avgLossUsdc)}</div>
        </td>
        <td>
          <div>Failed Start ${Number(item.failedStartExitCount || 0)}</div>
          <div class="small">${compactReasons(item.exitReasons)}</div>
        </td>
        <td>
          <div class="small">Live ${compactList(item.action?.topSymbols)}</div>
          <div class="small">Trades ${compactList(item.topSymbols)}</div>
          <div class="small">Watch ${compactList(item.watchTopSymbols)}</div>
          <div class="small">Universum ${compactList(item.universeTopSymbols)}</div>
        </td>
      </tr>
    `).join('');
  }

  function pnlClass(value) {
    const n = Number(value || 0);
    if (n > 0) return 'pnl-pos';
    if (n < 0) return 'pnl-neg';
    return 'pnl-flat';
  }

  function liveRowClass(item) {
    if (!item.statusOk) return 'row-live-down';
    if (item.currentlyTrading) return 'row-live-open';
    if (item.selected) return 'row-live-selected';
    return '';
  }

  function liveGateText(item) {
    if (!item.statusOk) return item.statusError || 'Down';
    if (item.gateReason) return item.gateReason;
    if (item.manualEntryExitOnly && !item.currentlyTrading) return 'manuell_entry_only';
    return item.tradingEnabled ? 'frei' : 'trading_disabled';
  }

  function liveExitNeedText(item) {
    const need = Number(item?.exitNeedUsdc);
    if (!Number.isFinite(need)) return '-';
    const bonus = Number(item?.exitBonusUsdc || 0);
    const rollArmed = Boolean(item?.exitRollArmed);
    if (rollArmed || bonus > 0) {
      return `${fmtNum(need)} + ${fmtNum(Math.max(0, bonus))}`;
    }
    return fmtNum(need);
  }

  function liveExitNeedSubText(item) {
    const mode = String(item?.exitMode || '').trim().toLowerCase();
    if (mode === 'profit_roll_abs') return 'bis Gewinnsockel';
    if (mode === 'profit_target') return 'bis Gewinnziel';
    if (mode === 'stage_roll') return 'nächster regulärer Exit';
    return 'nächster Exit';
  }

  function liveExitMetaHtml(item) {
    const mode = String(item?.exitMode || '').trim().toLowerCase();
    const rollArmed = Boolean(item?.exitRollArmed);
    const stateHtml = rollArmed
      ? '<span class="live-active-label">roll aktiv</span>'
      : null;
    if (mode === 'profit_roll_abs') {
      const armUsdc = Number(item?.exitArmUsdc);
      const retraceUsdc = Number(item?.exitRetraceUsdc);
      const state = stateHtml || 'gewinnziel';
      const armText = Number.isFinite(armUsdc) ? `${fmtNum(armUsdc)} USDC` : '-';
      const retraceText = Number.isFinite(retraceUsdc) ? `${fmtNum(retraceUsdc)} USDC` : '-';
      return `Sockel ${armText} | Rückfall ${retraceText} (${state})`;
    }
    if (mode === 'profit_target') {
      const entryStage = Number(item?.exitEntryStagePct);
      const targetPct = Number(item?.exitTargetPct);
      if (!Number.isFinite(targetPct)) return '-';
      const targetText = `${targetPct.toLocaleString('de-DE', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      })}%`;
      const state = stateHtml || 'gewinnziel';
      if (Number.isFinite(entryStage)) {
        return `${fmtPctValue(entryStage, 0)} -> +${targetText} (${state})`;
      }
      return `+${targetText} (${state})`;
    }
    const entryStage = Number(item?.exitEntryStagePct);
    const targetPct = Number(item?.exitTargetPct);
    if (!Number.isFinite(entryStage) || !Number.isFinite(targetPct)) return '-';
    const state = stateHtml || 'stufe';
    return `${fmtPctValue(entryStage, 0)} -> ${fmtPctValue(targetPct, 0)} (${state})`;
  }

  function liveExitTargetHtml(item) {
    const rollArmed = Boolean(item?.exitRollArmed);
    return rollArmed
      ? '<span class="live-active-label">rückfall aktiv</span>'
      : 'rückfall noch nicht aktiv';
  }

  function setLiveRows(items) {
    const hasCandidateUi = Boolean(liveCandidateRows && liveCandidateMetaEl && liveCandidateBlockEl);
    if (!items || !items.length) {
      liveRows.innerHTML = '<tr><td colspan="8">Keine Live-Daten</td></tr>';
      if (hasCandidateUi) {
        liveCandidateRows.innerHTML = '';
        liveCandidateMetaEl.textContent = '';
        liveCandidateBlockEl.style.display = 'none';
      }
      return;
    }

    const primaryRows = items.filter((item) => isPrimaryLiveRow(item));
    const candidateRows = items.filter((item) => isWatchCandidateRow(item));
    const sorted = sortedLiveRows(primaryRows);
    const sortedCandidates = sortedCandidateRows(candidateRows);

    const primaryHtml = sorted.map((it) => `
      <tr class="${liveRowClass(it)}">
        <td>${symbolLink(it.symbol, it.market)}</td>
        <td>
          <div>${liveStateBadges(it)}</div>
          <div class="small">${escapeHtml(strategyLabel(it.strategy || ''))}</div>
        </td>
        <td>
          <div>${fmtPrice(it.entryPrice)}</div>
          <div class="small">${fmtNum(it.entryValueUsdc)} USDC | ${fmtQty(it.positionQty)}</div>
        </td>
        <td>
          <div>${fmtPrice(it.exitPrice)}</div>
          <div class="small">${fmtNum(it.exitValueUsdc)} USDC</div>
        </td>
        <td>
          <div class="${pnlClass(it.totalPnlUsdc)}">${fmtSignedNum(it.totalPnlUsdc)} USDC</div>
          <div class="small">real ${fmtSignedNum(it.realizedPnlUsdc)} | offen ${fmtSignedNum(it.unrealizedPnlUsdc)}</div>
        </td>
        <td>
          <div>${liveExitNeedText(it)}</div>
          <div class="small">${escapeHtml(liveExitNeedSubText(it))}</div>
        </td>
        <td>
          <div>${liveExitMetaHtml(it)}</div>
          <div class="live-roll-sub">${liveExitTargetHtml(it)}</div>
        </td>
        <td>
          <div>${fmtAgeSec(it.freshnessSec)}</div>
          <div class="small">${fmtDateTime(it.updatedAt)}</div>
        </td>
      </tr>
    `).join('');

    if (!primaryHtml) {
      liveRows.innerHTML = '<tr><td colspan="8">Keine aktive Live-Lane.</td></tr>';
    } else {
      liveRows.innerHTML = primaryHtml;
    }

    if (!hasCandidateUi) return;
    if (!sortedCandidates.length) {
      liveCandidateRows.innerHTML = '';
      liveCandidateMetaEl.textContent = '';
      liveCandidateBlockEl.style.display = 'none';
      return;
    }

    liveCandidateMetaEl.textContent = `Kandidaten (noch kein Auto-Kauf): ${sortedCandidates.length}`;
    liveCandidateRows.innerHTML = sortedCandidates.map((it) => `
      <tr class="row-live-candidate">
        <td>${symbolLink(it.symbol, it.market)}</td>
        <td>
          <div>${liveStateBadges(it, { showReady: false, showManualEntry: false })}</div>
          <div class="small">${escapeHtml(strategyLabel(it.strategy || ''))}</div>
        </td>
        <td>${escapeHtml(liveBlockReasonText(it))}</td>
        <td>
          <div>${fmtAgeSec(it.freshnessSec)}</div>
          <div class="small">${fmtDateTime(it.updatedAt)}</div>
        </td>
      </tr>
    `).join('');
    liveCandidateBlockEl.style.display = '';
  }

  function rangeDirectionClass(dir) {
    const value = String(dir || '').trim().toLowerCase();
    if (value === 'up') return 'dir-up';
    if (value === 'down') return 'dir-down';
    return 'dir-flat';
  }

  function rangeDirectionArrow(dir) {
    const value = String(dir || '').trim().toLowerCase();
    if (value === 'up') return '↑';
    if (value === 'down') return '↓';
    return '→';
  }

  function fmtSignedPct(v, digits = 2) {
    const n = Number(v || 0);
    if (!Number.isFinite(n)) return '-';
    const abs = Math.abs(n).toLocaleString('de-DE', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
    if (n > 0) return `+${abs}%`;
    if (n < 0) return `-${abs}%`;
    return `${abs}%`;
  }

  function trendDirectionFromValue(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n)) return 'flat';
    if (n > 0.001) return 'up';
    if (n < -0.001) return 'down';
    return 'flat';
  }

  function deriveDisplayRangePct(item) {
    const low = Number(item?.corridorLowPrice);
    const high = Number(item?.corridorHighPrice);
    const rangePrice = Number(item?.rangePrice);
    const currentPrice = Number(item?.currentPrice);
    const price = Number.isFinite(rangePrice) && rangePrice > 0 ? rangePrice : currentPrice;
    if (
      Number.isFinite(low)
      && Number.isFinite(high)
      && Number.isFinite(price)
      && high > low
      && price > 0
    ) {
      return ((price - low) / (high - low)) * 100.0;
    }
    const liveRange = Number(item?.rangePositionPct);
    if (Number.isFinite(liveRange)) return liveRange;
    const selectorRange = Number(item?.selectorPosPct);
    if (Number.isFinite(selectorRange)) return selectorRange;
    return NaN;
  }

  function renderInTradeTrendSparkline(rawSeries, holdSec, windowMin) {
    const width = 1320;
    const height = 80;
    const padX = 4;
    const padY = 6;
    const midY = height / 2;
    const series = Array.isArray(rawSeries)
      ? rawSeries
        .map((v) => Number(v))
        .filter((v) => Number.isFinite(v))
      : [];

    if (!series.length) {
      return `
        <div class="intrade-chart-wrap">
          <svg class="trend-spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
            <line class="trend-midline" x1="${padX}" y1="${midY}" x2="${width - padX}" y2="${midY}"></line>
          </svg>
        </div>
      `;
    }

    const absMax = Math.max(0.05, ...series.map((value) => Math.abs(value)));
    const innerWidth = width - (padX * 2);
    const amplitude = (height / 2) - padY;
    const den = Math.max(1, series.length - 1);
    const effectiveWindowMin = Number.isFinite(Number(windowMin)) && Number(windowMin) > 0
      ? Number(windowMin)
      : 600;
    const holdMinRaw = Number.isFinite(Number(holdSec)) && Number(holdSec) > 0
      ? (Number(holdSec) / 60)
      : effectiveWindowMin;
    const holdFraction = Math.max(0.0, Math.min(1.0, holdMinRaw / effectiveWindowMin));
    const visiblePoints = Math.max(2, Math.round((den * holdFraction)) + 1);
    const startIdx = Math.max(0, series.length - visiblePoints);
    const visibleSeries = series.slice(startIdx);

    const path = visibleSeries.map((value, idx) => {
      const absIdx = startIdx + idx;
      const x = padX + ((absIdx / den) * innerWidth);
      const projected = midY - ((value / absMax) * amplitude);
      const y = Math.max(padY, Math.min(height - padY, projected));
      return `${idx === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`;
    }).join(' ');

    const delta = visibleSeries[visibleSeries.length - 1] - visibleSeries[0];
    const dir = trendDirectionFromValue(delta);
    const cls = dir === 'up' ? 'trend-line-up' : (dir === 'down' ? 'trend-line-down' : 'trend-line-flat');

    return `
      <div class="intrade-chart-wrap">
        <svg class="trend-spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
          <line class="trend-midline" x1="${padX}" y1="${midY}" x2="${width - padX}" y2="${midY}"></line>
          <path class="${cls}" d="${path}"></path>
        </svg>
      </div>
    `;
  }

  function setInTradeRows(items) {
    if (!inTradeRows || !inTradeMetaEl) return;
    const inTrade = Array.isArray(items)
      ? items.filter((item) => item && item.currentlyTrading)
      : [];

    inTradeMetaEl.innerHTML = `Im Trade: <strong>${inTrade.length}</strong>`;
    if (!inTrade.length) {
      inTradeRows.innerHTML = '<tr><td colspan="5">Aktuell keine offenen Trades.</td></tr>';
      return;
    }

    const sorted = [...inTrade].sort((a, b) => String(a?.symbol || '').localeCompare(String(b?.symbol || '')));
    inTradeRows.innerHTML = sorted.map((it) => {
      const displayRange = deriveDisplayRangePct(it);
      const hasDisplayRange = Number.isFinite(displayRange);
      const rangeText = hasDisplayRange ? fmtPctValue(displayRange, 1) : '-';
      const entryPrice = Number(it.entryPrice || 0);
      const hasEntryPrice = Number.isFinite(entryPrice) && entryPrice > 0;
      const trend10mPct = hasEntryPrice && Number.isFinite(Number(it.entryTrendPct10m))
        ? Number(it.entryTrendPct10m)
        : NaN;
      const trend10mDir = trendDirectionFromValue(trend10mPct);
      const trend10mClass = rangeDirectionClass(trend10mDir);
      const trend10mArrow = rangeDirectionArrow(trend10mDir);
      const trend10mLine = Number.isFinite(trend10mPct) ? `${fmtSignedPct(trend10mPct, 2)} ${trend10mArrow}` : '-';
      const holdSec = holdSecondsFromIso(it.entryOpenedAt || '');
      const holdText = fmtHoldSec(holdSec);
      const mobileMetaHtml = `Range ${escapeHtml(rangeText)} | 10m Δ <span class="${trend10mClass}">${escapeHtml(trend10mLine)}</span> | Halt ${escapeHtml(holdText)}`;
      const chartHtml = renderInTradeTrendSparkline(
        it.entryTrendSeries10h,
        holdSec,
        Number(it.entryTrendWindow10hMin || 600)
      );

      return `
      <tr>
        <td data-label="Symbol">
          ${symbolLink(it.symbol, it.market)}
          <div class="intrade-mobile-meta">${mobileMetaHtml}</div>
        </td>
        <td data-label="Coin-Range">${rangeText}</td>
        <td data-label="10m Δ"><span class="${trend10mClass}">${trend10mLine}</span></td>
        <td data-label="" class="intrade-chart-cell">${chartHtml}</td>
        <td data-label="Haltezeit">${holdText}</td>
      </tr>
    `;
    }).join('');
  }

  async function loadRotationPoints(token) {
    setRotationStatus('Lade Rotation ...');
    try {
      const res = await fetch('/rotation/points', {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${token}`
        }
      });

      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg = body.detail || `HTTP ${res.status}`;
        throw new Error(msg);
      }

      setRotationSummary(body);
      setRotationRows(body.rows || []);
      setRotationStatus('Rotation geladen.', 'ok');
    } catch (err) {
      setRotationSummary({});
      setRotationRows([]);
      setRotationStatus(`Rotation Fehler: ${err.message || err}`, 'error');
    }
  }

  async function loadLiveData(token, options = {}) {
    const silent = Boolean(options.silent);
    const force = Boolean(options.force);
    const nowMs = Date.now();

    if (liveLoadInFlight) return;
    if (!force && lastLiveLoadAtMs && (nowMs - lastLiveLoadAtMs) < MIN_LIVE_LOAD_INTERVAL_MS) return;
    if (!token) {
      if (!silent) setLiveStatus('Relay Token fehlt.', 'error');
      return;
    }

    liveLoadInFlight = true;
    lastLiveLoadAtMs = nowMs;
    if (!silent) setLiveStatus('Lade Live-Daten ...');

    try {
      const res = await fetch('/rotation/live', {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${token}`
        }
      });

      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg = body.detail || `HTTP ${res.status}`;
        throw new Error(msg);
      }

      setLiveSummary(body);
      setLiveStrategies(body.strategySummary || {});
      setLiveRows(body.rows || []);
      setInTradeRows(body.rows || []);
      if (!silent) setLiveStatus('Live-Daten geladen.', 'ok');
    } catch (err) {
      setLiveSummary({});
      setLiveStrategies({});
      setLiveRows([]);
      setInTradeRows([]);
      setLiveStatus(`Live Fehler: ${err.message || err}`, 'error');
    } finally {
      liveLoadInFlight = false;
    }
  }

  async function loadReport() {
    const nowMs = Date.now();
    if (lastReportLoadAtMs && (nowMs - lastReportLoadAtMs) < MIN_REPORT_LOAD_INTERVAL_MS) {
      const waitSec = Math.ceil((MIN_REPORT_LOAD_INTERVAL_MS - (nowMs - lastReportLoadAtMs)) / 1000);
      setStatus(`Bitte ${waitSec}s warten, um API-Spitzen zu vermeiden.`, 'error');
      return;
    }
    const token = tokenInput.value.trim();
    const reportDate = reportDateInput.value;
    const symbolInput = (reportSymbolInput?.value || '').trim();
    const normalized = cleanSymbol(symbolInput);
    const reportSymbol = normalized
      ? (normalized.endsWith('USDC') ? normalized : `${normalized}USDC`)
      : '';

    if (!token) {
      setStatus('Relay Token fehlt.', 'error');
      return;
    }
    if (!reportDate) {
      setStatus('Tag fehlt.', 'error');
      return;
    }

    localStorage.setItem(TOKEN_KEY, token);
    if (reportSymbolInput) localStorage.setItem(REPORT_SYMBOL_KEY, symbolInput);

    const payload = {
      dayUtc: reportDate,
      source: 'auto'
    };
    if (reportSymbol) payload.symbol = reportSymbol;

    loadReportBtn.disabled = true;
    lastReportLoadAtMs = Date.now();
    setStatus('Lade Auswertung ...');

    try {
      const res = await fetch('/trades/report', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify(payload)
      });

      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg = body.detail || `HTTP ${res.status}`;
        throw new Error(msg);
      }

      setSummary(body);
      setTradeRows(body.tradeRows || [], body.daySummary || {});
      setSymbolRows(body.symbolSummaries || []);
      setBundleRows(body.bundles || []);
      const meta = [];
      if (body.source) meta.push(`Quelle: ${body.source}`);
      if (body.sourceRequested && body.sourceRequested !== body.source) meta.push(`Angefragt: ${body.sourceRequested}`);
      if (Number.isFinite(Number(body.scannedJournalFiles))) meta.push(`Dateien: ${body.scannedJournalFiles}`);
      if (Number.isFinite(Number(body.scannedMirrorFiles))) meta.push(`Mirror-Dateien: ${body.scannedMirrorFiles}`);
      setStatus(
        meta.length
          ? `Auswertung erfolgreich geladen. ${meta.join(' | ')}`
          : 'Auswertung erfolgreich geladen.',
        'ok'
      );
    } catch (err) {
      clearReport();
      setStatus(`Fehler: ${err.message || err}`, 'error');
    } finally {
      loadReportBtn.disabled = false;
    }
  }

  async function loadRotation() {
    const nowMs = Date.now();
    if (lastRotationLoadAtMs && (nowMs - lastRotationLoadAtMs) < MIN_ROTATION_LOAD_INTERVAL_MS) {
      const waitSec = Math.ceil((MIN_ROTATION_LOAD_INTERVAL_MS - (nowMs - lastRotationLoadAtMs)) / 1000);
      setRotationStatus(`Bitte ${waitSec}s warten.`, 'error');
      return;
    }
    const token = tokenInput.value.trim();
    if (!token) {
      setRotationStatus('Relay Token fehlt.', 'error');
      return;
    }

    localStorage.setItem(TOKEN_KEY, token);

    loadRotationBtn.disabled = true;
    lastRotationLoadAtMs = Date.now();
    try {
      await loadRotationPoints(token);
    } finally {
      loadRotationBtn.disabled = false;
    }
  }

  async function loadLive() {
    const token = tokenInput.value.trim();
    if (!token) {
      setLiveStatus('Relay Token fehlt.', 'error');
      return;
    }
    localStorage.setItem(TOKEN_KEY, token);
    loadLiveBtn.disabled = true;
    try {
      await loadLiveData(token, { force: true });
    } finally {
      loadLiveBtn.disabled = false;
    }
  }

  const savedToken = localStorage.getItem(TOKEN_KEY) || '';
  const savedReportSymbol = localStorage.getItem(REPORT_SYMBOL_KEY) || '';
  tokenInput.value = savedToken;
  if (reportSymbolInput) reportSymbolInput.value = savedReportSymbol;
  reportDateInput.value = todayStr();

  loadReportBtn.addEventListener('click', loadReport);
  loadRotationBtn.addEventListener('click', loadRotation);
  loadLiveBtn.addEventListener('click', loadLive);
  tokenInput.addEventListener('change', () => {
    const token = tokenInput.value.trim();
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
      loadLiveData(token, { force: true });
    }
  });

  if (savedToken) {
    loadRotationPoints(savedToken);
    loadLiveData(savedToken, { force: true });
  }

  window.setInterval(() => {
    const token = tokenInput.value.trim();
    if (!token) return;
    loadLiveData(token, { silent: true });
  }, LIVE_REFRESH_INTERVAL_MS);
})();
