// The coin price of a crypto "Up or Down" round, drawn on a canvas as on polymarket.com.
//
// A round being played shows the last 30 seconds. The window slides left every frame and the newest price grows in
// from the right with a soft S-shaped step, because prices come about once a second. The price axis follows what is
// in view. The round's target is a dashed line; when it is out of view, its tag is pinned to the edge with arrows.
// Buys of the round float up in the lower left corner ("+ $5": green for Up, red for Down). A finished round is
// shown whole. Candles show the round (as much of it as is known) in candles of a fitting length.
//
//   const c = roundChart(host, {points: [[t, v]], target, start, end, finished}, {height})
//   c.add(t, v)  c.setTarget(v)  c.trade("up" | "down", dollars)  c.mode("line" | "candles")  c.finish()  c.remove()

const WINDOW = 30;                       // seconds in view while a round is played
const AHEAD = 0.9;                       // seconds of room right of the newest price
const COLOR = "#f7931a", UP = "#34ca5b", DOWN = "#ff5c56";
const TEXT = "#8696b0", GRID = "rgba(148,163,184,.10)", TARGET = "rgba(226,232,240,.55)";
const FONT = "11px Inter, system-ui, -apple-system, sans-serif";
const TOP = 14, BOTTOM = 26;             // px above the plot and for the time axis below it
const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400];
const CANDLES = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
const TRADES_SHOWN = 5, TRADE_LIFE = 3800;

const reduce = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const smooth = (u) => u * u * (3 - 2 * u);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const two = (n) => String(n).padStart(2, "0");
const clock = (s, secs) => {
  const d = new Date(s * 1000);
  return `${two(d.getHours())}:${two(d.getMinutes())}${secs ? ":" + two(d.getSeconds()) : ""}`;
};

/** A round step for a price axis: 1, 2, 2.5 or 5 times a power of ten. */
export function priceStep(span, count) {
  const raw = span / Math.max(1, count);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw * 0.999) || 10 * p;
}
/** Digits after the comma a step needs (2.5 → 1, 0.05 → 2, 10 → 0). */
export function stepDecimals(step) {
  for (let d = 0; d < 9; d++) if (Math.abs(Math.round(step * 10 ** d) - step * 10 ** d) < 1e-6) return d;
  return 8;
}
export function money(v, decimals) {
  return "$" + v.toLocaleString("ru-RU", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
/** Digits a coin's price is written with: $60 214,35, $1 680,61, $2,3412, $0,18234. */
export function coinDecimals(v) {
  const a = Math.abs(v);
  return a >= 100 ? 2 : a >= 1 ? 4 : 5;
}
/** Value at time t between known points: an S-shaped step from one price to the next (live), or a straight line. */
export function valueAt(pts, t, eased = true) {
  const n = pts.length;
  if (!n || t < pts[0][0]) return null;
  if (t >= pts[n - 1][0]) return pts[n - 1][1];
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (pts[mid][0] <= t) lo = mid; else hi = mid;
  }
  const [t0, v0] = pts[lo], [t1, v1] = pts[hi];
  const u = t1 > t0 ? (t - t0) / (t1 - t0) : 1;
  return v0 + (v1 - v0) * (eased ? smooth(u) : u);
}
/** Candles of `size` seconds over [from, to]: {t, o, h, l, c}. */
export function candles(pts, from, to, size) {
  const out = [];
  let cur = null;
  for (const [t, v] of pts) {
    if (t < from || t > to) continue;
    const b = from + Math.floor((t - from) / size) * size;
    if (!cur || cur.t !== b) {
      const open = cur ? cur.c : v;           // a candle opens where the one before closed
      cur = { t: b, o: open, h: Math.max(open, v), l: Math.min(open, v), c: v };
      out.push(cur);
    }
    cur.h = Math.max(cur.h, v);
    cur.l = Math.min(cur.l, v);
    cur.c = v;
  }
  return out;
}

export function roundChart(host, r, opts = {}) {
  host.classList.add("rc");
  host.innerHTML = '<canvas class="rc-canvas"></canvas><div class="rc-trades" aria-live="off"></div><div class="rc-ohlc" hidden></div>';
  const canvas = host.querySelector("canvas"), ctx = canvas.getContext("2d");
  const tradesBox = host.querySelector(".rc-trades"), ohlcBox = host.querySelector(".rc-ohlc");
  const pts = (r.points || []).map(([t, v]) => [Number(t), Number(v)]).filter(([t, v]) => isFinite(t) && isFinite(v)).sort((a, b) => a[0] - b[0]);
  let target = r.target || null;
  let live = !r.finished;
  let view = "line";
  let W = 0, H = 0, dpr = 1;
  let lo = null, hi = null, lastFrame = 0, raf = 0, gone = false;
  let lag = 1.6, lagSeen = null;          // how late prices arrive: the newest second is drawn once it is known
  let hover = null;                       // css px x of the pointer over the plot
  let axisW = 64;
  const born = performance.now();

  // ---- size
  const resize = () => {
    const rect = host.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = Math.max(200, Math.round(rect.width));
    H = Math.round(opts.height || rect.height || 300);
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    wake();
  };
  const ro = window.ResizeObserver ? new ResizeObserver(resize) : null;
  ro?.observe(host);

  // ---- what is in view
  const nowS = () => Date.now() / 1000;
  const tip = () => (live ? nowS() - lag : Math.min(r.end, pts.length ? pts[pts.length - 1][0] : r.end));
  function xDomain() {
    if (view === "line" && live) {
      const tv = tip();
      return [tv - WINDOW, tv + AHEAD];
    }
    const first = pts.length ? pts[0][0] : r.start;
    const from = Math.max(r.start, Math.min(first, r.end));
    const to = live ? Math.max(from + 60, nowS() + 1) : r.end;
    return [from, to];
  }
  function candleSize(from, to) {
    return CANDLES.find((s) => (to - from) / s <= 48) || 3600;
  }

  // ---- drawing
  function frame(ms) {
    raf = 0;
    if (gone) return;
    if (!host.isConnected) return remove();
    const dt = lastFrame ? Math.min(250, ms - lastFrame) : 1000;
    lastFrame = ms;
    if (!W) resize();
    draw(dt);
    if (live || hover != null || settling) wake(reduce() && live && hover == null ? 1000 : 0);
  }
  let settling = false, timer = 0;
  function wake(delay = 0) {
    if (gone || raf || timer) return;
    if (delay) timer = setTimeout(() => { timer = 0; wake(); }, delay);
    else raf = requestAnimationFrame(frame);
  }

  function draw(dt) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.font = FONT;
    const [x0, x1] = xDomain();
    const right = W - axisW, bottom = H - BOTTOM;
    const X = (t) => ((t - x0) / (x1 - x0)) * right;
    const T = (x) => x0 + (x / right) * (x1 - x0);

    // the line or the candles in view, and the price range they need
    let line = null, bars = null, size = 0, vlo = Infinity, vhi = -Infinity;
    const tv = tip();
    if (view === "line") {
      line = [];
      if (live) {
        for (let x = 0; x <= X(tv) + 0.01; x += 2) {
          const v = valueAt(pts, Math.min(T(x), tv), true);
          if (v != null) line.push([x, v]);
        }
        const last = valueAt(pts, tv, true);
        if (last != null) line.push([X(tv), last]);
      } else {
        for (const [t, v] of pts) if (t >= x0 - 30 && t <= x1) line.push([X(t), v]);
      }
      for (const [x, v] of line) if (x >= 0) { vlo = Math.min(vlo, v); vhi = Math.max(vhi, v); }
    } else {
      size = candleSize(x0, x1);
      bars = candles(pts, x0, x1, size);
      for (const b of bars) { vlo = Math.min(vlo, b.l); vhi = Math.max(vhi, b.h); }
    }
    if (!isFinite(vlo)) {
      if (!pts.length) return empty(right, bottom);
      vlo = vhi = pts[pts.length - 1][1];
    }
    const ref = Math.abs(vhi) || 1;
    const span = Math.max(vhi - vlo, ref * 0.00012);
    const mid = (vhi + vlo) / 2;
    const tlo = mid - span * 0.68, thi = mid + span * 0.68;
    if (lo == null || !isFinite(lo) || dt >= 1000) { lo = tlo; hi = thi; }
    else {
      const k = 1 - Math.exp(-dt / 260);
      lo += (tlo - lo) * k;
      hi += (thi - hi) * k;
    }
    settling = Math.abs(tlo - lo) > span * 0.002 || Math.abs(thi - hi) > span * 0.002;
    const Y = (v) => TOP + ((hi - v) / (hi - lo)) * (bottom - TOP);
    const above = target && target > hi, below = target && target < lo;
    const targetY = target ? (above ? TOP - 6 : below ? bottom + 2 : Y(target)) : null;
    const tagY = targetY == null ? null : clamp(targetY, 9, bottom - 2);

    // price grid and axis (the steps come from the range being reached, so labels stay round while it moves)
    const step = priceStep(thi - tlo, clamp(Math.floor((bottom - TOP) / 46), 3, 6));
    const dec = stepDecimals(step);
    // room on the right for the axis and for the price under the pointer
    let widest = ctx.measureText(money(mid, coinDecimals(mid))).width;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
      const y = Y(v);
      if (y < TOP - 2 || y > bottom + 2) continue;
      ctx.strokeStyle = GRID;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, Math.round(y) + 0.5);
      ctx.lineTo(right, Math.round(y) + 0.5);
      ctx.stroke();
      const label = money(v, dec);
      widest = Math.max(widest, ctx.measureText(label).width);
      ctx.fillStyle = TEXT;
      if (tagY == null || Math.abs(y - tagY) > 12) ctx.fillText(label, right + 8, y);   // not under the target's tag
    }
    const want = Math.ceil((widest + 16) / 8) * 8;
    if (widest && Math.abs(want - axisW) >= 8) axisW = clamp(want, 48, 110);

    // time axis
    const tstep = TIME_STEPS.find((s) => (s / (x1 - x0)) * right >= 74) || 14400;
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = TEXT;
    for (let t = Math.ceil(x0 / tstep) * tstep; t <= x1; t += tstep) {
      const x = X(t), label = clock(t, tstep < 60);
      const w = ctx.measureText(label).width;
      if (x - w / 2 < 2 || x + w / 2 > right - 2) continue;
      ctx.fillText(label, x, H - 8);
    }

    // the round's target: a dashed line, or a tag pinned to the edge it is beyond
    if (target) {
      const y = targetY;
      ctx.save();
      ctx.setLineDash([5, 5]);
      ctx.strokeStyle = TARGET;
      ctx.globalAlpha = above || below ? 0.5 : 1;
      ctx.beginPath();
      ctx.moveTo(0, Math.round(y) + 0.5);
      ctx.lineTo(right, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.restore();
      tag(right + 2, tagY, "Цель", above ? "up" : below ? "down" : "", "rgba(100,116,139,.9)", "#f1f5f9");
    }

    // the price: area, line, the newest price as a dashed level and a pulsing dot
    let tipXY = null;
    if (line && line.length > 1) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, right, H);
      ctx.clip();
      const grad = ctx.createLinearGradient(0, TOP, 0, bottom);
      grad.addColorStop(0, "rgba(247,147,26,.16)");
      grad.addColorStop(1, "rgba(247,147,26,0)");
      ctx.beginPath();
      line.forEach(([x, v], i) => (i ? ctx.lineTo(x, Y(v)) : ctx.moveTo(x, Y(v))));
      ctx.lineTo(line[line.length - 1][0], bottom);
      ctx.lineTo(line[0][0], bottom);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.beginPath();
      line.forEach(([x, v], i) => (i ? ctx.lineTo(x, Y(v)) : ctx.moveTo(x, Y(v))));
      ctx.strokeStyle = COLOR;
      ctx.lineWidth = 2.2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
      ctx.restore();
      const [lx, lv] = line[line.length - 1];
      tipXY = [lx, Y(lv)];
    } else if (bars && bars.length) {
      const bw = Math.max(1, ((size / (x1 - x0)) * right) * 0.68);
      for (const b of bars) {
        const x = X(b.t + size / 2), up = b.c >= b.o;
        ctx.strokeStyle = ctx.fillStyle = up ? UP : DOWN;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, Y(b.h));
        ctx.lineTo(Math.round(x) + 0.5, Y(b.l));
        ctx.stroke();
        const y0 = Y(Math.max(b.o, b.c)), y1 = Y(Math.min(b.o, b.c));
        ctx.fillRect(x - bw / 2, y0, bw, Math.max(1, y1 - y0));
      }
      const last = bars[bars.length - 1];
      tipXY = [X(last.t + size / 2), Y(last.c)];
    }
    if (tipXY) {
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = "rgba(247,147,26,.55)";
      ctx.beginPath();
      ctx.moveTo(0, Math.round(tipXY[1]) + 0.5);
      ctx.lineTo(right, Math.round(tipXY[1]) + 0.5);
      ctx.stroke();
      ctx.restore();
      if (view === "line") {
        if (live && !reduce()) {
          const k = ((performance.now() - born) % 1800) / 1800;
          ctx.beginPath();
          ctx.arc(tipXY[0], tipXY[1], 4.5 + 9 * k, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(247,147,26,${0.35 * (1 - k)})`;
          ctx.fill();
        }
        ctx.beginPath();
        ctx.arc(tipXY[0], tipXY[1], 4.5, 0, Math.PI * 2);
        ctx.fillStyle = COLOR;
        ctx.fill();
      }
    }

    // the pointer: a vertical line, the price and the time at it
    ohlcBox.hidden = true;
    if (hover != null && hover >= 0 && hover <= right) {
      let t = T(hover), v = null;
      if (view === "line") {
        t = Math.min(t, live ? tv : x1);
        v = valueAt(pts, t, live);
      } else if (bars?.length) {
        const b = bars.find((c) => t >= c.t && t < c.t + size) || null;
        if (b) {
          v = b.c;
          t = b.t;
          ohlcBox.hidden = false;
          const d = coinDecimals(b.c);
          ohlcBox.innerHTML = `<span>${clock(b.t, size < 60)}–${clock(b.t + size, size < 60)}</span>` +
            [["Откр.", b.o], ["Макс.", b.h], ["Мин.", b.l], ["Закр.", b.c]].map(([k, x]) => `<span>${k} <b>${money(x, d)}</b></span>`).join("");
        }
      }
      if (v != null) {
        const x = view === "line" ? X(t) : X(t + size / 2), y = Y(v);
        ctx.strokeStyle = "rgba(148,163,184,.45)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, TOP - 6);
        ctx.lineTo(Math.round(x) + 0.5, bottom);
        ctx.stroke();
        if (view === "line") {
          ctx.beginPath();
          ctx.arc(x, y, 4, 0, Math.PI * 2);
          ctx.fillStyle = COLOR;
          ctx.strokeStyle = "#0b1015";
          ctx.lineWidth = 2;
          ctx.fill();
          ctx.stroke();
        }
        tag(right + 2, y, money(v, coinDecimals(v)), "", COLOR, "#0b1015", true);
        const label = clock(t, (x1 - x0) < 4 * 3600);
        ctx.font = FONT;
        const w = ctx.measureText(label).width + 14;
        const lx = clamp(x - w / 2, 0, right - w);
        roundRect(lx, H - 21, w, 18, 6, "rgba(30,41,59,.96)");
        ctx.fillStyle = "#e8eaed";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(label, lx + w / 2, H - 12);
      }
    }
  }

  function empty(right, bottom) {
    ctx.fillStyle = TEXT;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("Цена монеты появится через несколько секунд", right / 2, (TOP + bottom) / 2);
  }
  function roundRect(x, y, w, h, rad, fill) {
    ctx.beginPath();
    ctx.moveTo(x + rad, y);
    ctx.arcTo(x + w, y, x + w, y + h, rad);
    ctx.arcTo(x + w, y + h, x, y + h, rad);
    ctx.arcTo(x, y + h, x, y, rad);
    ctx.arcTo(x, y, x + w, y, rad);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
  }
  // a tag on the price axis; "up" / "down" adds double arrows (the level is beyond that edge)
  function tag(x, y, text, arrows, bg, fg, bold = false) {
    ctx.font = (bold ? "600 " : "") + FONT;
    const w = Math.min(W - x - 2, ctx.measureText(text).width + (arrows ? 26 : 14));
    roundRect(x, y - 9, w, 18, 6, bg);
    ctx.fillStyle = fg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, x + 7, y + 0.5);
    if (arrows) {
      const cx = x + w - 10, dir = arrows === "up" ? -1 : 1;
      ctx.strokeStyle = fg;
      ctx.lineWidth = 1.4;
      ctx.lineJoin = "round";
      for (const dy of [-2.2, 2.2]) {
        const cy = y + dy;
        ctx.beginPath();
        ctx.moveTo(cx - 3.5, cy - 1.8 * dir);
        ctx.lineTo(cx, cy + 1.8 * dir);
        ctx.lineTo(cx + 3.5, cy - 1.8 * dir);
        ctx.stroke();
      }
    }
    ctx.font = FONT;
  }

  // ---- pointer
  const move = (e) => {
    const rect = canvas.getBoundingClientRect();
    hover = e.clientX - rect.left;
    wake();
  };
  const leave = () => {
    hover = null;
    wake();
  };
  canvas.addEventListener("pointermove", move);
  canvas.addEventListener("pointerdown", move);
  canvas.addEventListener("pointerleave", leave);
  canvas.addEventListener("pointercancel", leave);
  canvas.addEventListener("pointerup", (e) => { if (e.pointerType !== "mouse") leave(); });

  // ---- floating buys
  function trade(side, usd) {
    if (!live || !usd) return;
    const el = document.createElement("span");
    el.className = `rc-trade ${side === "up" ? "up" : "down"}${usd >= 100 ? " big" : ""}`;
    el.textContent = "+ $" + (usd >= 1000 ? (usd / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " тыс." : Math.max(1, Math.round(usd)));
    el.title = `Покупка «${side === "up" ? "Вверх" : "Вниз"}» на $${usd.toLocaleString("ru-RU")}`;
    // the ones shown move up to make room, smoothly (their old places are measured first)
    const before = new Map([...tradesBox.children].map((c) => [c, c.getBoundingClientRect().top]));
    tradesBox.prepend(el);
    while (tradesBox.children.length > TRADES_SHOWN) tradesBox.lastElementChild.remove();
    if (!reduce()) {
      for (const [c, top] of before) {
        if (!c.isConnected) continue;
        const d = top - c.getBoundingClientRect().top;
        if (!d) continue;
        c.style.transition = "none";
        c.style.transform = `translateY(${d}px)`;
        requestAnimationFrame(() => {
          c.style.transition = "";
          c.style.transform = "";
        });
      }
    }
    setTimeout(() => el.classList.add("out"), TRADE_LIFE);
    setTimeout(() => el.remove(), TRADE_LIFE + 700);
  }

  function remove() {
    gone = true;
    cancelAnimationFrame(raf);
    clearTimeout(timer);
    ro?.disconnect();
  }

  resize();
  wake();
  return {
    add(t, v) {
      t = Number(t);
      v = Number(v);
      if (!isFinite(t) || !isFinite(v)) return;
      const n = pts.length;
      if (!n || t > pts[n - 1][0]) pts.push([t, v]);
      else if (t === pts[n - 1][0]) pts[n - 1][1] = v;
      else {
        const i = pts.findIndex((p) => p[0] >= t);
        if (pts[i][0] === t) pts[i][1] = v;
        else pts.splice(i, 0, [t, v]);
      }
      if (pts.length > 20000) pts.splice(0, pts.length - 20000);
      if (live) {
        // the newest second is shown once it is known: lag follows how late prices come (seen, smoothed)
        const late = nowS() - t;
        if (late >= 0 && late < 15) {
          lagSeen = lagSeen == null ? late : lagSeen * 0.85 + late * 0.15;
          lag = clamp(lagSeen + 1.1, 1.2, 6);
        }
      }
      wake();
    },
    setTarget(v) {
      target = v || null;
      wake();
    },
    trade,
    mode(m) {
      view = m === "candles" ? "candles" : "line";
      lo = hi = null;
      wake();
    },
    finish() {
      live = false;
      lo = hi = null;
      tradesBox.replaceChildren();
      wake();
    },
    remove,
  };
}
