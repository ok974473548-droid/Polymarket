<script lang="ts">
  // Число «набегает» к новому значению (0,6 с, плавное замедление); при «меньше движения» — сразу.
  let { value, format }: { value: number; format: (v: number) => string } = $props();
  let shown = $state(0);
  let from = 0;
  let raf = 0;

  $effect(() => {
    const to = Number(value) || 0;
    const reduce = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    cancelAnimationFrame(raf);
    if (reduce || from === to) {
      shown = from = to;
      return;
    }
    const start = from,
      t0 = performance.now(),
      dur = 600;
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / dur);
      shown = start + (to - start) * (1 - Math.pow(1 - k, 3));
      if (k < 1) raf = requestAnimationFrame(step);
      else from = to;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  });
</script>

<span aria-label={format(value)}><span aria-hidden="true">{format(shown)}</span></span>
