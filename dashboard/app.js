const _isLocal = window.location.protocol === "file:";
const API_BASE = _isLocal ? "http://192.168.1.153:8080/api/v1" : "/api/v1";
const STATIC_BASE = _isLocal ? "http://192.168.1.153:8080" : "";
const REFRESH_INTERVAL_MS = 30000;

function createEmptySeries() {
  return Array.from({ length: 12 }, () => 0);
}

function createEmptyDashboardData() {
  return {
    system: {
      cpu: null,
      memory: null,
      diskRemainingGb: null,
      tempC: null,
      uptime: "—",
    },
    ingest: {
      success60m: 0,
      failure60m: 0,
      avgLatencyMs: 0,
      series: createEmptySeries(),
    },
    queue: {
      depth: 0,
      running: 0,
      failed: 0,
      dead: 0,
      maxVisual: 40,
    },
    database: {
      connected: true,
      version: "SQLite",
      captures: 0,
      events: 0,
      jobs: 0,
      devices: 0,
      ingestAudit: 0,
      dbSizeMb: 0,
      tables: [],
    },
    devices: [],
    events: [],
    alerts: [],
    capturesDaily: [],
    stand: null,
    recentCaptures: [],
    dairyCamCaptures: [],
    dairyInventoryEvents: [],
  };
}

let dashboardData = createEmptyDashboardData();
let refreshTimer = null;

const el = (selector) => document.querySelector(selector);

function formatValue(value, unit = "") {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  return `${value}${unit}`;
}

function renderStat(containerId, entries) {
  const container = el(containerId);
  container.innerHTML = entries
    .map(
      ([label, value]) => `
      <div class="stat">
        <div class="label">${label}</div>
        <div class="value">${value}</div>
      </div>`
    )
    .join("");
}

function renderChart(series) {
  const chart = el("#ingest-chart");
  const safeSeries = series.length ? series : createEmptySeries();
  const max = Math.max(...safeSeries, 1);
  chart.innerHTML = safeSeries
    .map((value) => `<div class="bar" style="height:${Math.max(10, Math.round((value / max) * 100))}%"></div>`)
    .join("");
}

function renderDevices(devices) {
  const table = el("#device-table");
  if (!devices.length) {
    table.innerHTML = `
      <tr>
        <td colspan="5" class="empty-state">No devices have checked in yet.</td>
      </tr>`;
    return;
  }

  table.innerHTML = devices
    .map(
      (device) => `
      <tr>
        <td>${device.device_id}</td>
        <td>${formatLocalTime(device.last_seen)}</td>
        <td>${device.rssi ?? "—"}</td>
        <td>${device.battery_mv ? (device.battery_mv / 1000).toFixed(2) + " V" : "—"}</td>
        <td>${device.fw_version ?? "—"}</td>
      </tr>`
    )
    .join("");
}

function renderQueueMeter(queue) {
  const pct = Math.min(100, Math.round((queue.depth / queue.maxVisual) * 100));
  el("#queue-bar").style.width = `${pct}%`;
}

function renderDatabase(database) {
  renderStat("#db-health", [
    ["Connected", database.connected ? "Yes" : "No"],
    ["Version", database.version],
    ["Capture rows", database.captures],
    ["Event rows", database.events],
  ]);

  renderStat("#db-storage", [
    ["Jobs", database.jobs],
    ["Devices", database.devices],
    ["Ingest audit", database.ingestAudit],
    ["DB size", `${database.dbSizeMb.toFixed(1)} MB`],
  ]);

  const tableRows = database.tables
    .map(
      (table) => `
      <tr>
        <td>${table.name}</td>
        <td>${table.rows}</td>
        <td>${table.lastWrite}</td>
        <td>${table.size}</td>
      </tr>`
    )
    .join("");

  el("#db-table").innerHTML = tableRows || `<tr><td colspan="4" class="empty-state">No table stats available yet.</td></tr>`;
}

function renderEventGallery(events) {
  console.log(`Rendering ${events.length} events in gallery`);
  if (!events.length) {
    el("#event-gallery").innerHTML = `<p class="empty-state">No events recorded yet.</p>`;
    return;
  }
  
  // Show event count in UI
  const eventCountElement = el("#event-count");
  if (eventCountElement) {
    eventCountElement.textContent = `${events.length} events`;
  }

  el("#event-gallery").innerHTML = events
    .map(
      (event) => {
        const hasImage = event.storage_uri && event.storage_uri !== "null";
        const imagePreview = hasImage 
          ? `<img src="${API_BASE}/static${event.storage_uri.replace(/^\/data/, '')}" alt="Event preview" class="event-image">`
          : `<span class="no-preview">No preview available</span>`;
        
        return `
      <article class="event-card">
        <div class="event-preview">
          ${imagePreview}
        </div>
        <div class="event-body">
          <div><strong>${formatLocalTime(event.event_ts)}</strong> • ${event.event_type}</div>
          <p>${event.note ?? "No additional details."}</p>
          <div class="event-meta">
            ${hasImage ? `<span class="meta-item">📷 ${event.storage_uri.split('/').pop()}</span>` : ''}
            ${event.resolution ? `<span class="meta-item">📐 ${event.resolution}</span>` : ''}
            ${event.age_minutes > 0 ? `<span class="meta-item">⏱️ ${event.age_minutes} min ago</span>` : ''}
            ${event.confidence !== undefined ? `<span class="meta-item">🎯 ${(event.confidence * 100).toFixed(0)}% confident</span>` : ''}
          </div>
        </div>
      </article>`;
    })
    .join("");
}

function renderCaptureStats(database, ingest, daily) {
  const total = database.captures ?? 0;
  const lastHour = ingest.success60m ?? 0;
  const events = database.events ?? 0;
  const rate = lastHour > 0 ? (lastHour / 60).toFixed(1) : "0.0";

  renderStat("#capture-stats", [
    ["Total photos", total.toLocaleString()],
    ["Last 60 min", lastHour],
    ["Per minute", rate],
    ["Events logged", events.toLocaleString()],
  ]);

  renderCaptureTrend(daily);
  renderCaptureDailyBreakdown(daily);
}

function renderCaptureTrend(daily) {
  const indicator = el("#capture-trend");
  if (!daily || daily.length < 2) {
    indicator.textContent = "";
    return;
  }
  const today = daily[daily.length - 1].count;
  const yesterday = daily[daily.length - 2].count;
  if (yesterday === 0) {
    indicator.textContent = "";
    return;
  }
  const pct = Math.round(((today - yesterday) / yesterday) * 100);
  if (pct > 0) {
    indicator.textContent = `▲ ${pct}% vs yesterday`;
    indicator.className = "trend-indicator trend-up";
  } else if (pct < 0) {
    indicator.textContent = `▼ ${Math.abs(pct)}% vs yesterday`;
    indicator.className = "trend-indicator trend-down";
  } else {
    indicator.textContent = "— same as yesterday";
    indicator.className = "trend-indicator";
  }
}

function renderCaptureDailyBreakdown(daily) {
  const container = el("#capture-daily");
  if (!daily || !daily.length) {
    container.innerHTML = `<p class="empty-state">No daily data yet.</p>`;
    return;
  }
  const max = Math.max(...daily.map((d) => d.count), 1);
  container.innerHTML = daily
    .map((d) => {
      const pct = Math.max(4, Math.round((d.count / max) * 100));
      const label = new Date(d.day + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      return `
        <div class="daily-row">
          <span class="daily-label">${label}</span>
          <div class="daily-bar-wrap">
            <div class="daily-bar" style="width:${pct}%"></div>
          </div>
          <span class="daily-count">${d.count}</span>
        </div>`;
    })
    .join("");
}

function renderStandOverview(stand) {
  if (!stand) {
    el("#stand-status-pill").textContent = "No data";
    el("#stand-status-meta").textContent = "Backend not reachable.";
    return;
  }

  // Status pill — derived from last stock change note or interaction recency
  const pill = el("#stand-status-pill");
  const meta = el("#stand-status-meta");
  const lastStock = stand.last_stock_change;
  const lastInteraction = stand.last_interaction;

  if (lastStock) {
    const note = (lastStock.note ?? "").toLowerCase();
    if (note.includes("stock: full") || note.includes("restock") || note.includes("added") || note.includes("filled")) {
      pill.textContent = "Well stocked";
      pill.className = "stand-status-pill status-good";
    } else if (note.includes("stock: half")) {
      pill.textContent = "Half full";
      pill.className = "stand-status-pill status-good";
    } else if (note.includes("stock: low")) {
      pill.textContent = "Running low";
      pill.className = "stand-status-pill status-warn";
    } else if (note.includes("stock: empty") || note.includes("empty") || note.includes("out")) {
      pill.textContent = "Empty";
      pill.className = "stand-status-pill status-bad";
    } else {
      pill.textContent = "Stock checked";
      pill.className = "stand-status-pill status-neutral";
    }

    // Show food items if available
    const itemsMatch = lastStock.note && lastStock.note.match(/items: ([^)]+)\)/);
    const foodItems = itemsMatch ? itemsMatch[1] : null;
    meta.innerHTML = `Last checked: ${formatLocalTime(lastStock.event_ts)}${foodItems ? `<br><span class="food-items">🥫 ${foodItems}</span>` : ""}`;
  } else if (lastInteraction) {
    pill.textContent = "Active";
    pill.className = "stand-status-pill status-neutral";
    meta.textContent = `Last interaction: ${formatLocalTime(lastInteraction)}`;
  } else {
    pill.textContent = "No activity yet";
    pill.className = "stand-status-pill status-neutral";
    meta.textContent = "No events recorded.";
  }

  // Activity stats
  renderStat("#stand-activity", [
    ["Today", stand.interactions_today ?? 0],
    ["This week", stand.interactions_week ?? 0],
    ["All time", (stand.interactions_total ?? 0).toLocaleString()],
  ]);

  // Weekly interactions bar chart
  renderDailyChart("#stand-weekly-chart", stand.daily_interactions ?? []);

  // Hourly chart
  renderHourlyChart(stand.hourly_today ?? []);

  // Recent activity feed from events
  renderActivityFeed(dashboardData.events);
}

function renderDailyChart(selector, daily) {
  const container = el(selector);
  if (!daily.length) {
    container.innerHTML = `<p class="empty-state">No interactions this week.</p>`;
    return;
  }
  const max = Math.max(...daily.map((d) => d.count), 1);
  const legend = `
    <div class="daily-legend">
      <span class="legend-item"><span class="legend-dot night"></span>Midnight–6AM</span>
      <span class="legend-item"><span class="legend-dot morning"></span>6AM–Noon</span>
      <span class="legend-item"><span class="legend-dot afternoon"></span>Noon–6PM</span>
      <span class="legend-item"><span class="legend-dot evening"></span>6PM–Midnight</span>
    </div>`;
  const rows = daily.map((d) => {
    const total = d.count || 0;
    const nightPct  = total ? Math.round((d.night / total) * 100) : 0;
    const morningPct = total ? Math.round((d.morning / total) * 100) : 0;
    const afternoonPct = total ? Math.round((d.afternoon / total) * 100) : 0;
    const eveningPct = 100 - nightPct - morningPct - afternoonPct;
    const barWidth = Math.max(4, Math.round((total / max) * 100));
    const label = new Date(d.day + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    return `
      <div class="daily-row">
        <span class="daily-label">${label}</span>
        <div class="daily-bar-wrap">
          <div class="daily-bar-stacked" style="width:${barWidth}%">
            ${nightPct > 0 ? `<div class="seg night" style="width:${nightPct}%"></div>` : ""}
            ${morningPct > 0 ? `<div class="seg morning" style="width:${morningPct}%"></div>` : ""}
            ${afternoonPct > 0 ? `<div class="seg afternoon" style="width:${afternoonPct}%"></div>` : ""}
            ${eveningPct > 0 ? `<div class="seg evening" style="width:${eveningPct}%"></div>` : ""}
          </div>
        </div>
        <span class="daily-count">${total}</span>
      </div>`;
  }).join("");
  container.innerHTML = legend + rows;
}

function renderHourlyChart(hourly) {
  const container = el("#stand-hourly-chart");
  const max = Math.max(...hourly.map((h) => h.count), 1);
  container.innerHTML = hourly.map((h) => {
    const pct = Math.max(2, Math.round((h.count / max) * 100));
    const label = h.hour === 0 ? "12a" : h.hour < 12 ? `${h.hour}a` : h.hour === 12 ? "12p" : `${h.hour - 12}p`;
    return `
      <div class="hourly-col">
        <div class="hourly-bar-wrap">
          <div class="hourly-bar" style="height:${pct}%" title="${h.count} interactions"></div>
        </div>
        <span class="hourly-label">${label}</span>
      </div>`;
  }).join("");
}

function renderRecentPhotos(captures) {
  if (!captures.length) return;
  const latest = captures[0];
  const container = el("#latest-photo");
  if (!container) return;
  container.innerHTML = `
    <img src="${STATIC_BASE}${latest.static_url}" alt="Latest capture" loading="lazy">
    <span class="photo-time">${formatLocalTime(latest.capture_ts)}</span>`;
}

function renderFreestandInventory(events) {
  const inventory = el("#freestand-inventory");
  if (!inventory) return;
  const stockEvents = (events ?? []).filter(e => e.event_type === "stock_changed" && e.device_id === "freestand-cam").slice(0, 5);
  if (!stockEvents.length) {
    inventory.innerHTML = `<p class="empty-state">No inventory notes recorded yet.</p>`;
  } else {
    inventory.innerHTML = `<ul class="activity-feed">${stockEvents.map(e => `
      <li class="activity-item">
        <span class="activity-dot dot-stock"></span>
        <span class="activity-label">${e.note ?? "Stock updated"}</span>
        <span class="activity-time">${formatLocalTime(e.event_ts)}</span>
      </li>`).join("")}</ul>`;
  }
}

function renderDairyCam(captures, devices, events) {
  const latest = el("#dairycam-latest");
  const status = el("#dairycam-status");
  const inventory = el("#dairycam-inventory");

  const device = (devices ?? []).find(d => d.device_id === "dairy-cam");
  if (status) {
    renderStat("#dairycam-status", [
      ["Last Seen", device ? formatLocalTime(device.last_seen) : "Never"],
      ["RSSI", device?.rssi != null ? `${device.rssi} dBm` : "—"],
      ["Firmware", device?.fw_version ?? "—"],
    ]);
  }

  if (latest) {
    if (!captures.length) {
      latest.innerHTML = `<p style="color:#888;padding:1rem 0">No captures yet.</p>`;
    } else {
      const c = captures[0];
      latest.innerHTML = `
        <img src="${STATIC_BASE}${c.static_url}" alt="Latest dairy cam capture" loading="lazy" style="width:100%;border-radius:6px">
        <span class="photo-time">${formatLocalTime(c.capture_ts)}</span>`;
    }
  }

  if (inventory) {
    const stockEvents = (events ?? []).filter(e => e.event_type === "stock_changed" && e.device_id === "dairy-cam").slice(0, 5);
    if (!stockEvents.length) {
      inventory.innerHTML = `<p class="empty-state">No inventory notes recorded yet.</p>`;
    } else {
      inventory.innerHTML = `<ul class="activity-feed">${stockEvents.map(e => `
        <li class="activity-item">
          <span class="activity-dot dot-stock"></span>
          <span class="activity-label">${e.note ?? "Stock updated"}</span>
          <span class="activity-time">${formatLocalTime(e.event_ts)}</span>
        </li>`).join("")}</ul>`;
    }
  }
}

function renderWeeklySummary(stand) {
  const container = el("#weekly-summary");
  if (!container || !stand) return;

  const thisWeek = stand.interactions_week ?? 0;
  const lastWeek = stand.interactions_last_week ?? 0;
  const daily = stand.daily_interactions ?? [];

  let trend = "—";
  let trendClass = "";
  if (lastWeek > 0) {
    const pct = Math.round(((thisWeek - lastWeek) / lastWeek) * 100);
    trend = pct > 0 ? `▲ ${pct}% vs last week` : pct < 0 ? `▼ ${Math.abs(pct)}% vs last week` : "Same as last week";
    trendClass = pct > 0 ? "trend-up" : pct < 0 ? "trend-down" : "";
  }

  const peakDay = daily.length ? daily.reduce((a, b) => a.count > b.count ? a : b) : null;
  const peakLabel = peakDay ? new Date(peakDay.day + "T00:00:00").toLocaleDateString(undefined, { weekday: "long" }) : "—";

  const avgDaily = daily.length ? Math.round(thisWeek / daily.length) : 0;

  container.innerHTML = `
    <div class="weekly-summary-grid">
      <div class="summary-stat">
        <div class="label">This week</div>
        <div class="value">${thisWeek}</div>
      </div>
      <div class="summary-stat">
        <div class="label">Last week</div>
        <div class="value">${lastWeek}</div>
      </div>
      <div class="summary-stat">
        <div class="label">Daily avg</div>
        <div class="value">${avgDaily}</div>
      </div>
      <div class="summary-stat">
        <div class="label">Busiest day</div>
        <div class="value">${peakLabel}</div>
      </div>
      <div class="summary-trend ${trendClass}">${trend}</div>
    </div>`;
}

function renderActivityFeed(events) {
  const feed = el("#stand-activity-feed");
  const recent = events.slice(0, 8);
  if (!recent.length) {
    feed.innerHTML = `<li class="empty-state">No recent activity.</li>`;
    return;
  }
  feed.innerHTML = recent.map((e) => {
    const typeLabel = e.event_type === "interaction_detected" ? "Someone visited" : e.event_type === "stock_changed" ? "Stock changed" : e.event_type;
    return `
      <li class="activity-item">
        <span class="activity-dot ${e.event_type === 'stock_changed' ? 'dot-stock' : 'dot-interaction'}"></span>
        <span class="activity-label">${typeLabel}</span>
        <span class="activity-time">${formatLocalTime(e.event_ts)}</span>
      </li>`;
  }).join("");
}

function renderAlerts(alerts) {
  if (!alerts.length) {
    el("#alerts-list").innerHTML = `<li class="empty-state">No alerts at this time.</li>`;
    return;
  }

  el("#alerts-list").innerHTML = alerts
    .map(
      (item) => `
      <li>
        <span class="alert-severity ${item.severity}">${item.severity}</span>
        ${item.text}
      </li>`
    )
    .join("");
}

function setOverallStatus(data) {
  const status = el("#overall-status");
  status.classList.remove("warn", "bad");

  const temp = data.system.tempC;
  const disk = data.system.diskRemainingGb;
  const failureCount = data.ingest.failure60m ?? 0;
  const deadJobs = data.queue.dead ?? 0;

  if ((temp !== null && temp >= 70) || (disk !== null && disk <= 10) || deadJobs > 0) {
    status.textContent = "Needs attention";
    status.classList.add("bad");
    return;
  }

  if ((temp !== null && temp >= 60) || (disk !== null && disk <= 20) || failureCount >= 8 || data.queue.depth > 30) {
    status.textContent = "Watchlist";
    status.classList.add("warn");
    return;
  }

  status.textContent = "System nominal";
}

function formatLocalTime(value) {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('en-US', { timeZone: 'America/New_York' });
}

function buildAlerts(data) {
  const alerts = [];
  const disk = data.system.diskRemainingGb;
  if (disk !== null && disk < 10) {
    alerts.push({ severity: disk < 5 ? "critical" : "warn", text: `Disk running low (${disk.toFixed(1)} GB remaining).` });
  }

  const failures = data.ingest.failure60m ?? 0;
  if (failures >= 10) {
    alerts.push({ severity: "critical", text: `Ingest fail rate high (${failures} failures in the last hour).` });
  } else if (failures >= 5) {
    alerts.push({ severity: "warn", text: `Ingest failures increasing (${failures} in the last hour).` });
  }

  if (data.queue.depth >= 30) {
    alerts.push({ severity: "warn", text: `Worker queue depth is ${data.queue.depth}.` });
  }

  return alerts;
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Request failed: ${path}`);
  }
  return response.json();
}

async function fetchJsonWithFallback(path, fallback) {
  try {
    return await fetchJson(path);
  } catch (error) {
    console.warn(`Fallback used for ${path}:`, error);
    return fallback;
  }
}

async function refreshData() {
  try {
    // Show loading state
    el("#last-updated").textContent = "Loading data..."
    
    // Add timestamp to bypass caching
    const timestamp = Date.now();
    
    const [systemResp, ingestResp, queueResp, databaseResp, eventsResp, devicesResp, capturesDailyResp, standResp, capturesResp, dairyCamResp, dairyInventoryResp, freestandInventoryResp] = await Promise.all([
      fetchJsonWithFallback(`${API_BASE}/admin/metrics/system?t=${timestamp}`, {cpu: null, memory: null, diskRemainingGb: null, tempC: null, uptime: "—"}),
      fetchJsonWithFallback(`${API_BASE}/admin/metrics/ingest?t=${timestamp}`, {success_60m: 0, failure_60m: 0, avg_latency_ms: 0, series: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/metrics/queue?t=${timestamp}`, {depth: 0, queue: {}}),
      fetchJsonWithFallback(`${API_BASE}/admin/metrics/database?t=${timestamp}`, {connected: true, version: "SQLite", captures: 0, events: 0, jobs: 0, devices: 0, ingestAudit: 0, dbSizeMb: 0, tables: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/events?limit=50&t=${timestamp}`, {events: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/devices?t=${timestamp}`, {devices: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/metrics/captures_daily?t=${timestamp}`, {days: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/metrics/stand?t=${timestamp}`, {}),
      fetchJsonWithFallback(`${API_BASE}/admin/captures?limit=5&device_id=freestand-cam&t=${timestamp}`, {captures: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/captures?limit=20&device_id=dairy-cam&t=${timestamp}`, {captures: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/events?limit=10&device_id=dairy-cam&event_type=stock_changed&t=${timestamp}`, {events: []}),
      fetchJsonWithFallback(`${API_BASE}/admin/events?limit=10&device_id=freestand-cam&event_type=stock_changed&t=${timestamp}`, {events: []}),
    ]);
    
    console.log(`Refreshed data: ${eventsResp.events.length} events, ${databaseResp.captures} captures`);

    dashboardData.system = {
      cpu: systemResp.cpu,
      memory: systemResp.memory,
      diskRemainingGb: systemResp.diskRemainingGb,
      tempC: systemResp.tempC,
      uptime: systemResp.uptime,
    };

    dashboardData.ingest = {
      success60m: ingestResp.success_60m ?? 0,
      failure60m: ingestResp.failure_60m ?? 0,
      avgLatencyMs: ingestResp.avg_latency_ms ?? 0,
      series: ingestResp.series ?? createEmptySeries(),
    };

    const queueMetrics = queueResp.queue ?? {};
    dashboardData.queue = {
      depth: queueResp.depth ?? 0,
      running: queueMetrics.running ?? 0,
      failed: queueMetrics.failed ?? 0,
      dead: queueMetrics.dead ?? 0,
      maxVisual: 40,
    };

    dashboardData.database = {
      connected: databaseResp.connected,
      version: databaseResp.version,
      captures: databaseResp.captures,
      events: databaseResp.events,
      jobs: databaseResp.jobs,
      devices: databaseResp.devices,
      ingestAudit: databaseResp.ingestAudit,
      dbSizeMb: databaseResp.dbSizeMb ?? 0,
      tables: databaseResp.tables ?? [],
    };

    dashboardData.events = eventsResp.events ?? [];
    dashboardData.devices = devicesResp.devices ?? [];
    dashboardData.capturesDaily = capturesDailyResp.days ?? [];
    dashboardData.stand = standResp.ok ? standResp : null;
    dashboardData.recentCaptures = capturesResp.captures ?? [];
    dashboardData.dairyCamCaptures = dairyCamResp.captures ?? [];
    dashboardData.dairyInventoryEvents = dairyInventoryResp.events ?? [];
    dashboardData.freestandInventoryEvents = freestandInventoryResp.events ?? [];
    dashboardData.alerts = buildAlerts(dashboardData);
    el("#last-updated").textContent = new Date().toLocaleTimeString();
  } catch (error) {
    console.error("Failed to refresh dashboard data", error);
  } finally {
    render();
  }
}

function render() {
  try {
    renderStat("#system-health", [
      ["CPU", formatValue(dashboardData.system.cpu, "%")],
      ["Memory", formatValue(dashboardData.system.memory, "%")],
      ["Disk Free", dashboardData.system.diskRemainingGb !== null ? `${dashboardData.system.diskRemainingGb.toFixed(1)} GB` : "—"],
      ["Temp", formatValue(dashboardData.system.tempC, "°C")],
      ["Uptime", dashboardData.system.uptime],
    ]);

  renderStat("#ingest-metrics", [
    ["Success (60m)", dashboardData.ingest.success60m],
    ["Failures (60m)", dashboardData.ingest.failure60m],
    ["Avg Latency", `${dashboardData.ingest.avgLatencyMs} ms`],
  ]);

  renderStat("#queue-metrics", [
    ["Depth", dashboardData.queue.depth],
    ["Running", dashboardData.queue.running],
    ["Failed", dashboardData.queue.failed],
    ["Dead", dashboardData.queue.dead],
  ]);

  renderStandOverview(dashboardData.stand);
  renderRecentPhotos(dashboardData.recentCaptures);
  renderWeeklySummary(dashboardData.stand);
  renderChart(dashboardData.ingest.series);
  renderQueueMeter(dashboardData.queue);
  renderCaptureStats(dashboardData.database, dashboardData.ingest, dashboardData.capturesDaily);
  renderDatabase(dashboardData.database);
  renderDevices(dashboardData.devices);
  renderEventGallery(dashboardData.events);
  renderAlerts(dashboardData.alerts);
  renderDairyCam(dashboardData.dairyCamCaptures, dashboardData.devices, dashboardData.dairyInventoryEvents);
  renderFreestandInventory(dashboardData.freestandInventoryEvents);
  setOverallStatus(dashboardData);
  } catch (error) {
    console.error("Render error:", error);
    el("#last-updated").textContent = "Error rendering data - " + new Date().toLocaleTimeString();
  }
}

function setupTabs() {
  const tabs = document.querySelectorAll(".tab");
  const panels = document.querySelectorAll(".tab-panel");

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((other) => {
        other.classList.remove("active");
        other.setAttribute("aria-selected", "false");
      });

      panels.forEach((panel) => panel.classList.remove("active"));

      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
      el(`#tab-${tab.dataset.tab}`).classList.add("active");
    });
  });
}

function setupRefreshControls() {
  el("#refresh-btn").addEventListener("click", refreshData);
  
  // Add specific event refresh button if it exists
  const eventRefreshBtn = el("#refresh-events-btn");
  if (eventRefreshBtn) {
    eventRefreshBtn.addEventListener("click", () => {
      fetchJsonWithFallback(`${API_BASE}/admin/events?limit=50&t=${Date.now()}`, {events: []})
        .then(data => {
          dashboardData.events = data.events ?? [];
          renderEventGallery(dashboardData.events);
          console.log(`Manual refresh: ${data.events.length} events loaded`);
        });
    });
  }
  
  const autoRefresh = el("#auto-refresh");

  function startAutoRefresh() {
    if (refreshTimer) {
      clearInterval(refreshTimer);
    }
    refreshTimer = setInterval(refreshData, REFRESH_INTERVAL_MS);
  }

  function stopAutoRefresh() {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  }

  autoRefresh.addEventListener("change", () => {
    if (autoRefresh.checked) {
      startAutoRefresh();
    } else {
      stopAutoRefresh();
    }
  });

  if (autoRefresh.checked) {
    startAutoRefresh();
  }
}

function init() {
  setupTabs();
  setupRefreshControls();
  refreshData();

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshData();
    }
  });
}

init();
