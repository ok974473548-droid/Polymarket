// The live broadcast of an event, as on polymarket.com (1.17.0): an esports match being played names its stream as the
// resolution source. Kick and YouTube play inline (muted, can be hidden). Twitch runs a pre-roll ad in its embed, so
// it is not played inline — only a link out (1.17.1). There is no honest way to strip Twitch's ads from the embed.
//
//   streamOf("https://kick.com/cct_cs2") -> {site: "Kick", channel: "cct_cs2", src, url, embed: true} | null
//   streamCard(e)                        -> markup, "" when the event has no live stream
//   bindStream(host)                     -> the hide / show button
import { esc, icon } from "./core.js";

const HIDDEN = "kontur:stream-hidden";

/** The player of a stream link, or null for anything that is not a Kick, YouTube or Twitch broadcast.
 * `embed` is false for Twitch: its embed forces an ad break, so we only link out to it. */
export function streamOf(link, host = location.hostname) {
  let u;
  try {
    u = new URL(String(link || "").trim());
  } catch {
    return null;
  }
  const site = u.hostname.replace(/^(www|m)\./, "");
  const parts = u.pathname.split("/").filter(Boolean);
  if (site === "kick.com" && parts[0] && /^[\w-]+$/.test(parts[0])) {
    return { site: "Kick", channel: parts[0], url: u.href, embed: true, src: `https://player.kick.com/${encodeURIComponent(parts[0])}?muted=true&autoplay=true` };
  }
  if (site === "youtube.com" || site === "youtu.be") {
    const id = site === "youtu.be" ? parts[0] : u.searchParams.get("v") || (["live", "embed", "shorts"].includes(parts[0]) ? parts[1] : null);
    if (id && /^[\w-]{6,20}$/.test(id)) {
      // youtube-nocookie: приватный встроенный плеер, реже показывает «подтвердите, что вы не бот» (1.17.3)
      return { site: "YouTube", channel: id, url: u.href, embed: true, src: `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&mute=1&playsinline=1` };
    }
  }
  if (site === "twitch.tv" && parts[0] && !["directory", "videos", "p", "settings"].includes(parts[0].toLowerCase())) {
    // Twitch's own embed shows a pre-roll ad; we don't play it inline, just link out. parent is for the player page.
    return { site: "Twitch", channel: parts[0], url: u.href, embed: false,
      src: `https://player.twitch.tv/?channel=${encodeURIComponent(parts[0])}&parent=${encodeURIComponent(host)}&muted=true&autoplay=true` };
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
  const open = `<a class="btn btn-sm" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${icon("ext", 13)} Открыть на ${esc(s.site)}</a>`;
  if (!s.embed) {
    // Twitch: a link only, with a note why (its embed opens with an ad)
    return `<section class="card ev-stream link-only">
      <div class="evs-head"><span class="lv-live">В эфире</span><b>Трансляция · ${esc(s.site)}</b><small>${esc(s.channel)}</small>${open}</div>
      <p class="evs-note">${icon("ext", 13)} Трансляция идёт на ${esc(s.site)}. Встроенный плеер ${esc(s.site)} начинается с рекламы, поэтому открывается там.</p></section>`;
  }
  const off = hidden();
  // the page itself sends no referrer; the player needs to know which site it is on, so the frame sends our origin
  return `<section class="card ev-stream ${off ? "off" : ""}" data-src="${esc(s.src)}" data-site="${esc(s.site)}">
    <div class="evs-head"><span class="lv-live">В эфире</span><b>Трансляция · ${esc(s.site)}</b><small>${esc(s.channel)}</small>${open}
      <button class="btn btn-sm evs-toggle" type="button">${off ? "Показать" : "Скрыть"}</button></div>
    <div class="evs-frame">${off ? "" : frame(s.src, s.site)}</div></section>`;
}

function frame(src, site) {
  return `<iframe src="${esc(src)}" title="Трансляция ${esc(site)}" allow="autoplay; fullscreen; picture-in-picture; encrypted-media; keyboard-map"
    allowfullscreen referrerpolicy="strict-origin-when-cross-origin" loading="lazy"></iframe>`;
}

export function bindStream(host) {
  const card = host.querySelector(".ev-stream:not(.link-only)");
  if (!card) return;
  card.querySelector(".evs-toggle").onclick = (ev) => {
    const off = !card.classList.contains("off");
    card.classList.toggle("off", off);
    ev.currentTarget.textContent = off ? "Показать" : "Скрыть";
    const box = card.querySelector(".evs-frame");
    box.innerHTML = off ? "" : frame(card.dataset.src, card.dataset.site);
    try {
      localStorage.setItem(HIDDEN, off ? "1" : "0");
    } catch { /* private mode: only this page remembers */ }
  };
}
