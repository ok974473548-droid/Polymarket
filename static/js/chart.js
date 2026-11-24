// Thin wrapper over TradingView Lightweight Charts (local copy in /static/vendor).
// Line chart with a floating tooltip, % or $ axis, toggleable series and end-of-line dots.

export const PALETTE = ["#34ca5b", "#157aff", "#e6ce1e", "#f69124", "#c13b83", "#0ba4ae", "#8b5cf6", "#ef4444",
  "#22d3ee", "#a3e635", "#f472b6", "#fb923c"];

const fmtPct = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const fmtUsd = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const fmtDate = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

export function axisMoney(v) {
  const a = Math.abs(v);
  const [d, s] = a >= 1e9 ? [1e9, "B"] : a >= 1e6 ? [1e6, "M"] : a >= 1e3 ? [1e3, "K"] : [1, ""];
  return (v < 0 ? "-$" : "$") + (s ? fmtPct.format(a / d) + s : fmtUsd.format(a));
}

/**
 * series: [{name, color, points: [[unixSeconds, value], ...], area?: bool, visible?: bool}]
 * options: {unit: "%" | "$", height}
 * Returns {setVisible(index, bool), destroy()}.
 */
/** Chart height for this screen: the base height on a laptop, up to 35% taller on wide monitors, never over 55% of the window. */
export function fitHeight(base) {
  const w = window.innerWidth, h = window.innerHeight;
  if (w < 1100) return base;
  const k = Math.min(1.35, 1 + Math.max(0, w - 1440) / 1400);
  return Math.round(Math.max(base, Math.min(base * k, h * 0.55)));
}

export function lineChart(host, series, options = {}) {
  const LC = window.LightweightCharts;
  host.innerHTML = "";
  host.classList.add("chart-host");
  const unit = options.unit === "$" ? "$" : "%";
  const base = options.height || 260;
  const height = fitHeight(base);
  const usable = series.filter((s) => s.points && s.points.length);
  if (!LC || !usable.length) {
    host.innerHTML = `<div class="chart-empty">${options.empty || "Нет истории цен за выбранный период"}</div>`;
    return { setVisible() {}, destroy() {} };
  }
  const chart = LC.createChart(host, {
    width: host.clientWidth || 600,
    height,
    layout: { background: { type: LC.ColorType.Solid, color: "transparent" }, textColor: "#8696b0", fontSize: 11,
      fontFamily: "Inter, system-ui, sans-serif", attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { color: "rgba(41,55,78,.55)" } },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.08, bottom: 0.06 } },
    timeScale: { borderVisible: false, timeVisible: options.intraday === true, secondsVisible: false, rightOffset: 2,
      fixLeftEdge: true, fixRightEdge: true },
    crosshair: { mode: LC.CrosshairMode.Magnet, vertLine: { color: "#415473", labelVisible: false },
      horzLine: { visible: false, labelVisible: false } },
    handleScroll: false,
    handleScale: false,
    localization: { locale: "ru-RU", priceFormatter: (v) => (unit === "%" ? fmtPct.format(v) + "%" : axisMoney(v)) },
  });
  const lines = series.map((s, i) => {
    if (!s.points || !s.points.length) return null;
    const color = s.color || PALETTE[i % PALETTE.length];
    const opts = { color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 4,
      visible: s.visible !== false,
      priceFormat: { type: "custom", minMove: 0.01, formatter: (v) => (unit === "%" ? fmtPct.format(v) + "%" : axisMoney(v)) } };
    const api = s.area
      ? chart.addSeries(LC.AreaSeries, { ...opts, lineColor: color, topColor: color + "40", bottomColor: color + "05" })
      : chart.addSeries(LC.LineSeries, opts);
    const seen = new Map();
    for (const [t, v] of s.points) if (isFinite(t) && isFinite(v)) seen.set(Math.floor(t), v);
    const data = [...seen].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
    api.setData(data);
    if (data.length && LC.createSeriesMarkers) {
      try {
        LC.createSeriesMarkers(api, [{ time: data[data.length - 1].time, position: "inBar", color, shape: "circle", size: 0.6 }]);
      } catch { /* markers are cosmetic */ }
    }
    return { api, name: s.name, color, data, values: new Map(data.map((d) => [d.time, d.value])) };
  });
  chart.timeScale().fitContent();

  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  host.append(tip);
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point || param.point.x < 0) {
      tip.hidden = true;
      return;
    }
    const rows = lines
      .filter((l) => l && l.api.options().visible)
      .map((l) => {
        const d = param.seriesData.get(l.api);
        return d ? { name: l.name, color: l.color, value: d.value } : null;
      })
      .filter(Boolean)
      .sort((a, b) => b.value - a.value)
      .slice(0, 8);
    if (!rows.length) {
      tip.hidden = true;
      return;
    }
    tip.innerHTML = `<time>${fmtDate.format(param.time * 1000)}</time>` + rows.map((r) =>
      `<div><i style="background:${r.color}"></i><span>${r.name.replace(/[<>&]/g, "")}</span><b>${unit === "%" ? fmtPct.format(r.value) + "%" : axisMoney(r.value)}</b></div>`).join("");
    tip.hidden = false;
    const w = tip.offsetWidth, x = param.point.x;
    tip.style.left = (x + 16 + w > host.clientWidth ? x - w - 16 : x + 16) + "px";
    tip.style.top = "8px";
  });
  const ro = new ResizeObserver(() => host.clientWidth && chart.applyOptions({ width: host.clientWidth, height: fitHeight(base) }));
  ro.observe(host);
  return {
    setVisible(i, on) {
      lines[i]?.api.applyOptions({ visible: on });
    },
    destroy() {
      ro.disconnect();
      chart.remove();
    },
  };
}
