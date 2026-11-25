// The live broadcast of an event, as on polymarket.com (1.17.0): an esports match being played names its stream as the
// resolution source (Twitch, Kick or YouTube); it is shown above the event, muted, and can be hidden.
//
//   streamOf("https://www.twitch.tv/PGL") -> {site: "Twitch", channel: "PGL", src, url} | null
//   streamCard(e)                         -> markup, "" when the event has no live stream
//   bindStream(host)                      -> the hide / show button
import { esc, icon } from "./core.js";

const HIDDEN = "kontur:stream-hidden";

/** The player of a stream link, or null for anything that is not a Twitch, Kick or YouTube broadcast. */
export function streamOf(link, host = location.hostname) {
  let u;
  try {
    u = new URL(String(link || "").trim());
  } catch {
    return null;
  }
  const site = u.hostname.replace(/^(www|m)\./, "");
  const parts = u.pathname.split("/").filter(Boolean);
  if (site === "twitch.tv" && parts[0] && !["directory", "videos", "p", "settings"].includes(parts[0].toLowerCase())) {
    const ch = parts[0];
    // Twitch shows its player only on the pages named in `parent`
    return { site: "Twitch", channel: ch, url: u.href,
      src: `https://player.twitch.tv/?channel=${encodeURIComponent(ch)}&parent=${encodeURIComponent(host)}&muted=true&autoplay=true` };
  }
  if (site === "kick.com" && parts[0] && /^[\w-]+$/.test(parts[0])) {
    return { site: "Kick", channel: parts[0], url: u.href, src: `https://player.kick.com/${encodeURIComponent(parts[0])}?muted=true&autoplay=true` };
  }
  if (site === "youtube.com" || site === "youtu.be") {
    const id = site === "youtu.be" ? parts[0] : u.searchParams.get("v") || (["live", "embed", "shorts"].includes(parts[0]) ? parts[1] : null);
    if (id && /^[\w-]{6,20}$/.test(id)) {
      return { site: "YouTube", channel: id, url: u.href, src: `https://www.youtube.com/embed/${id}?autoplay=1&mute=1&playsinline=1` };
    }
  }
  return null;
}

function hidden() {
  try {
    return localStorage.getItem(HIDDEN) === "1";
  } catch {
    return false;
  }
}

export function streamCard(e) {
  const s = e.live && !e.closed && !e.ended ? streamOf(e.resolution) : null;
  if (!s) return "";
  const off = hidden();
  // the page itself sends no referrer; YouTube's player needs to know which site it is on, so the frame sends our origin
  return `<section class="card ev-stream ${off ? "off" : ""}" data-src="${esc(s.src)}">
    <div class="evs-head"><span class="lv-live">В эфире</span><b>Трансляция · ${esc(s.site)}</b><small>${esc(s.channel)}</small>
      <a class="btn btn-sm" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${icon("ext", 13)} Открыть на ${esc(s.site)}</a>
      <button class="btn btn-sm evs-toggle" type="button">${off ? "Показать" : "Скрыть"}</button></div>
    <div class="evs-frame">${off ? "" : frame(s.src, s.site)}</div></section>`;
}

function frame(src, site) {
  return `<iframe src="${esc(src)}" title="Трансляция ${esc(site)}" allow="autoplay; fullscreen; picture-in-picture; encrypted-media; keyboard-map"
    allowfullscreen referrerpolicy="strict-origin-when-cross-origin" loading="lazy"></iframe>`;
}

export function bindStream(host) {
  const card = host.querySelector(".ev-stream");
  if (!card) return;
  card.querySelector(".evs-toggle").onclick = (ev) => {
    const off = !card.classList.contains("off");
    card.classList.toggle("off", off);
    ev.currentTarget.textContent = off ? "Показать" : "Скрыть";
    const box = card.querySelector(".evs-frame");
    box.innerHTML = off ? "" : frame(card.dataset.src, card.querySelector(".evs-head b").textContent.split("· ")[1]);
    try {
      localStorage.setItem(HIDDEN, off ? "1" : "0");
    } catch { /* private mode: only this page remembers */ }
  };
}
