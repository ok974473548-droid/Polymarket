// Звук при новом сигнале (по желанию; выбор хранится в браузере).
export function soundOn(): boolean {
  try {
    return localStorage.getItem("wf-sound") === "1";
  } catch {
    return false;
  }
}

export function setSound(on: boolean): void {
  try {
    localStorage.setItem("wf-sound", on ? "1" : "0");
  } catch {
    /* без хранилища — до перезагрузки */
  }
}

let ctx: AudioContext | null = null;

export function chime(force = false): void {
  if (!force && !soundOn()) return;
  try {
    const W = window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext };
    const Ctor = W.AudioContext || W.webkitAudioContext;
    if (!Ctor) return;
    ctx = ctx || new Ctor();
    const c = ctx;
    for (const [f, t] of [
      [880, 0],
      [1320, 0.12],
    ]) {
      const o = c.createOscillator(),
        g = c.createGain();
      o.type = "sine";
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, c.currentTime + t);
      g.gain.exponentialRampToValueAtTime(0.12, c.currentTime + t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + t + 0.35);
      o.connect(g).connect(c.destination);
      o.start(c.currentTime + t);
      o.stop(c.currentTime + t + 0.4);
    }
  } catch {
    /* звук недоступен */
  }
}
