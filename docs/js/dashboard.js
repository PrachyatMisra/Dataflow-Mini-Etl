/* ==========================================================================
   DataFlow Mini ETL - dashboard renderer
   Reads the pipeline artifact `data/latest.json` and renders KPIs, charts,
   movers, the market table and pipeline observability panels.
   No build step, no framework - plain ES2020 + Chart.js.
   ========================================================================== */

"use strict";

/* --------------------------------------------------------------------------
 * Formatting helpers
 * ------------------------------------------------------------------------ */
const fmtUsdCompact = (value) => {
  if (value === null || value === undefined) return "–";
  const abs = Math.abs(value);
  if (abs >= 1e12) return `$${(value / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  return `$${value.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
};

const fmtPrice = (value) => {
  if (value === null || value === undefined) return "–";
  const digits = Math.abs(value) < 1 ? 4 : 2;
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
};

const fmtPct = (value, digits = 2) => {
  if (value === null || value === undefined) return "–";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
};

const pctClass = (value) => {
  if (value === null || value === undefined) return "neutral";
  if (value > 0.05) return "pos";
  if (value < -0.05) return "neg";
  return "neutral";
};

const fmtUtc = (iso) => {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return iso;
  return dt.toUTCString().replace(" GMT", " UTC");
};

const fngClass = (value) => {
  if (value <= 24) return "fng-extreme-fear";
  if (value <= 44) return "fng-fear";
  if (value <= 55) return "fng-neutral";
  if (value <= 74) return "fng-greed";
  return "fng-extreme-greed";
};

/* --------------------------------------------------------------------------
 * Chart palette
 * ------------------------------------------------------------------------ */
const PALETTE = ["#4f8cff", "#a78bfa", "#34d399", "#fbbf24", "#f87171", "#22d3ee", "#f472b6", "#a3e635", "#fb923c", "#818cf8"];
const GRID_COLOR = "rgba(139, 155, 180, 0.14)";
const TICK_COLOR = "#8b9bb4";

function baseChartOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: TICK_COLOR, boxWidth: 12, font: { size: 11 } } },
      tooltip: { backgroundColor: "#111827", borderColor: "#223049", borderWidth: 1 },
    },
    scales: {
      x: { ticks: { color: TICK_COLOR, font: { size: 10 } }, grid: { color: GRID_COLOR } },
      y: { ticks: { color: TICK_COLOR, font: { size: 10 } }, grid: { color: GRID_COLOR } },
    },
  };
}

/* --------------------------------------------------------------------------
 * Renderers
 * ------------------------------------------------------------------------ */
function renderHeader(payload) {
  const badge = document.getElementById("run-badge");
  const source = payload.meta.source === "live-api" ? "live data" : "fixture replay";
  badge.textContent = `${source} · ${payload.meta.coins_tracked} coins`;
  badge.className = `badge ${payload.quality.summary.failures ? "badge-err" : "badge-ok"}`;
  document.getElementById("fetched-at").textContent = `fetched ${fmtUtc(payload.meta.fetched_at)} · ${payload.meta.run_id}`;
}

function renderKpis(payload) {
  const { kpis } = payload;
  document.getElementById("kpi-mcap").textContent = fmtUsdCompact(kpis.total_market_cap);
  document.getElementById("kpi-mcap-sub").textContent = `top ${payload.meta.coins_tracked} coins · ${payload.meta.vs_currency}`;
  document.getElementById("kpi-volume").textContent = fmtUsdCompact(kpis.total_volume_24h);

  const avg = document.getElementById("kpi-avg-change");
  avg.textContent = `avg 24h change ${fmtPct(kpis.avg_change_24h)}`;
  avg.className = `kpi-sub ${pctClass(kpis.avg_change_24h)}`;

  const btc = kpis.bitcoin || {};
  document.getElementById("kpi-btc").textContent = fmtPrice(btc.price);
  const btcSub = document.getElementById("kpi-btc-sub");
  btcSub.textContent = `${fmtPct(btc.change_24h)} 24h · ${btc.dominance_pct ?? "–"}% of tracked cap`;
  btcSub.className = `kpi-sub ${pctClass(btc.change_24h)}`;

  const fng = kpis.fear_greed;
  const fngEl = document.getElementById("kpi-fng");
  if (fng) {
    fngEl.textContent = fng.value;
    fngEl.className = `kpi-value ${fngClass(fng.value)}`;
    document.getElementById("kpi-fng-sub").textContent = `${fng.classification} · ${fng.date}`;
  }

  const q = payload.quality.summary;
  const qEl = document.getElementById("kpi-quality");
  qEl.textContent = q.failures ? `${q.failures} failed` : `${q.passed}/${q.total} passed`;
  qEl.className = `kpi-value ${q.failures ? "neg" : q.warnings ? "fng-neutral" : "pos"}`;
  document.getElementById("kpi-quality-sub").textContent =
    `${q.passed} passed · ${q.warnings} warnings · ${q.failures} failures`;
}

function renderMcapChart(payload) {
  const top10 = payload.coins.slice(0, 10);
  new Chart(document.getElementById("chart-mcap"), {
    type: "bar",
    data: {
      labels: top10.map((c) => c.symbol),
      datasets: [{
        label: "Market cap (USD)",
        data: top10.map((c) => c.market_cap),
        backgroundColor: PALETTE.map((c) => `${c}cc`),
        borderColor: PALETTE,
        borderWidth: 1,
        borderRadius: 6,
      }],
    },
    options: {
      ...baseChartOptions(),
      indexAxis: "y",
      plugins: {
        ...baseChartOptions().plugins,
        legend: { display: false },
        tooltip: {
          ...baseChartOptions().plugins.tooltip,
          callbacks: { label: (ctx) => fmtUsdCompact(ctx.parsed.x) },
        },
      },
      scales: {
        x: { ticks: { color: TICK_COLOR, callback: (v) => fmtUsdCompact(v) }, grid: { color: GRID_COLOR } },
        y: { ticks: { color: TICK_COLOR }, grid: { display: false } },
      },
    },
  });
}

function renderDominanceChart(payload) {
  new Chart(document.getElementById("chart-dominance"), {
    type: "doughnut",
    data: {
      labels: payload.dominance.map((d) => d.label),
      datasets: [{
        data: payload.dominance.map((d) => d.value),
        backgroundColor: PALETTE,
        borderColor: "#131b2b",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      plugins: {
        legend: { position: "right", labels: { color: TICK_COLOR, boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          backgroundColor: "#111827", borderColor: "#223049", borderWidth: 1,
          callbacks: { label: (ctx) => ` ${ctx.label}: ${fmtUsdCompact(ctx.parsed)}` },
        },
      },
    },
  });
}

function renderTrendChart(payload) {
  const { trends } = payload;
  const labels = trends.dates.map((iso) => {
    const d = new Date(iso);
    return `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
  });
  new Chart(document.getElementById("chart-trend"), {
    type: "line",
    data: {
      labels,
      datasets: trends.series.map((s, i) => ({
        label: s.symbol,
        data: s.index,
        borderColor: PALETTE[i % PALETTE.length],
        backgroundColor: `${PALETTE[i % PALETTE.length]}22`,
        tension: 0.35,
        spanGaps: true,
        pointRadius: 2.5,
        borderWidth: 2,
      })),
    },
    options: {
      ...baseChartOptions(),
      plugins: {
        ...baseChartOptions().plugins,
        tooltip: {
          ...baseChartOptions().plugins.tooltip,
          callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(1) ?? "–"}` },
        },
      },
      scales: {
        x: { ticks: { color: TICK_COLOR, font: { size: 10 } }, grid: { display: false } },
        y: {
          ticks: { color: TICK_COLOR, font: { size: 10 }, callback: (v) => v },
          grid: { color: GRID_COLOR },
          title: { display: true, text: "index (start = 100)", color: TICK_COLOR, font: { size: 10 } },
        },
      },
    },
  });
}

function renderFearGreedChart(payload) {
  const history = [...payload.fear_greed.history].reverse(); // oldest first
  const bands = [
    { to: 25, color: "rgba(248,113,113,0.10)", label: "Extreme fear" },
    { to: 45, color: "rgba(251,146,60,0.08)", label: "Fear" },
    { to: 56, color: "rgba(251,191,36,0.07)", label: "Neutral" },
    { to: 75, color: "rgba(163,230,53,0.07)", label: "Greed" },
    { to: 100, color: "rgba(52,211,153,0.08)", label: "Extreme greed" },
  ];
  let prev = 0;
  const zones = bands.map((b) => {
    const zone = { from: prev, to: b.to, color: b.color };
    prev = b.to;
    return zone;
  });

  new Chart(document.getElementById("chart-fng"), {
    type: "line",
    data: {
      labels: history.map((h) => h.date.slice(5)),
      datasets: [{
        label: "Fear & Greed",
        data: history.map((h) => h.value),
        borderColor: "#fbbf24",
        backgroundColor: "rgba(251,191,36,0.12)",
        fill: true,
        tension: 0.35,
        pointRadius: 2.5,
        borderWidth: 2,
      }],
    },
    options: {
      ...baseChartOptions(),
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#111827", borderColor: "#223049", borderWidth: 1,
          callbacks: {
            label: (ctx) => {
              const item = history[ctx.dataIndex];
              return ` ${item.value} · ${item.classification}`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: TICK_COLOR, font: { size: 10 }, maxTicksLimit: 10 }, grid: { display: false } },
        y: { min: 0, max: 100, ticks: { color: TICK_COLOR, stepSize: 25 }, grid: { color: GRID_COLOR } },
      },
    },
    plugins: [{
      id: "fngZones",
      beforeDraw(chart) {
        const { ctx, chartArea, scales } = chart;
        if (!chartArea) return;
        zones.forEach((z) => {
          const yTop = scales.y.getPixelForValue(z.to);
          const yBottom = scales.y.getPixelForValue(z.from);
          ctx.save();
          ctx.fillStyle = z.color;
          ctx.fillRect(chartArea.left, yTop, chartArea.right - chartArea.left, yBottom - yTop);
          ctx.restore();
        });
      },
    }],
  });
}

function renderMovers(payload) {
  const container = document.getElementById("movers");
  const buildRows = (coins) => coins.map((c) => `
    <div class="mover-row">
      <span class="mover-name">
        ${c.image ? `<img src="${c.image}" alt="" loading="lazy" onerror="this.style.display='none'">` : ""}
        <span>${c.name} <span class="coin-symbol">${c.symbol}</span></span>
      </span>
      <span class="mover-pct ${pctClass(c.change_24h)}">${fmtPct(c.change_24h)}</span>
    </div>`).join("");

  container.innerHTML = `
    <div><h3>▲ Top gainers</h3>${buildRows(payload.movers.gainers)}</div>
    <div><h3>▼ Top losers</h3>${buildRows(payload.movers.losers)}</div>`;
}

function renderTable(payload) {
  const tbody = document.querySelector("#coins-table tbody");
  const momentumPill = (m) => {
    if (!m || m === "n/a") return `<span class="pill pill-flat">n/a</span>`;
    const cls = m.includes("gain") ? "pill-gain" : m.includes("loss") ? "pill-loss" : "pill-flat";
    return `<span class="pill ${cls}">${m}</span>`;
  };

  tbody.innerHTML = payload.coins.map((c) => `
    <tr>
      <td class="muted">${c.rank ?? "–"}</td>
      <td>
        <span class="coin-cell">
          ${c.image ? `<img src="${c.image}" alt="" loading="lazy" onerror="this.style.display='none'">` : ""}
          <span>${c.name} <span class="coin-symbol">${c.symbol}${c.stablecoin ? " · stable" : ""}</span></span>
        </span>
      </td>
      <td class="num">${fmtPrice(c.price)}</td>
      <td class="num ${pctClass(c.change_24h)}">${fmtPct(c.change_24h)}</td>
      <td class="num ${pctClass(c.change_7d)}">${fmtPct(c.change_7d)}</td>
      <td class="num">${fmtUsdCompact(c.market_cap)}</td>
      <td class="num">${fmtUsdCompact(c.volume_24h)}</td>
      <td class="num muted">${c.vol_mcap.toFixed(3)}</td>
      <td>${momentumPill(c.momentum)}</td>
    </tr>`).join("");

  document.getElementById("table-note").textContent =
    `${payload.coins.length} assets · sorted by market cap`;
}

function renderStages(payload) {
  const container = document.getElementById("stages");
  const maxMs = Math.max(...payload.stages.map((s) => s.duration_s), 0.001);
  container.innerHTML = payload.stages.map((s) => {
    const width = Math.max((s.duration_s / maxMs) * 100, 2);
    const ms = s.duration_s >= 1 ? `${s.duration_s.toFixed(2)}s` : `${Math.round(s.duration_s * 1000)}ms`;
    return `
      <div class="stage-row">
        <span class="stage-name">${s.name}</span>
        <span class="stage-bar-track"><span class="stage-bar" style="width:${width}%"></span></span>
        <span class="stage-ms">${s.status === "ok" ? ms : "failed"}</span>
      </div>`;
  }).join("");
}

function renderQuality(payload) {
  const { quality } = payload;
  document.getElementById("quality-summary").textContent =
    `${quality.summary.passed}/${quality.summary.total} passed`;
  const icons = { pass: "✓", warn: "!", fail: "✕" };
  document.getElementById("quality-checks").innerHTML = quality.checks.map((c) => `
    <div class="check-row check-${c.status}">
      <span class="check-icon">${icons[c.status] || "?"}</span>
      <span class="check-name">${c.name}</span>
      <span class="check-msg">${c.message}</span>
    </div>`).join("");
}

/* --------------------------------------------------------------------------
 * Bootstrap
 * ------------------------------------------------------------------------ */
function showError(message) {
  const banner = document.getElementById("load-error");
  banner.hidden = false;
  document.getElementById("load-error-detail").textContent = message;
  const badge = document.getElementById("run-badge");
  badge.textContent = "data unavailable";
  badge.className = "badge badge-err";
}

async function init() {
  let payload;
  try {
    const response = await fetch("data/latest.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    payload = await response.json();
  } catch (err) {
    showError(`data/latest.json could not be fetched (${err.message}). Run the pipeline to generate it.`);
    return;
  }

  renderHeader(payload);
  renderKpis(payload);
  renderMovers(payload);
  renderTable(payload);
  renderStages(payload);
  renderQuality(payload);

  if (typeof Chart === "undefined") {
    showError("Chart.js failed to load from the CDN - charts are disabled, tables still work.");
    return;
  }
  renderMcapChart(payload);
  renderDominanceChart(payload);
  renderTrendChart(payload);
  renderFearGreedChart(payload);
}

document.addEventListener("DOMContentLoaded", init);
