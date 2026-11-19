<script lang="ts">
  // Горизонтальные полосы (вместо кругов: длину глаз сравнивает точнее — NN/g). Полоса растёт при появлении.
  export interface Bar {
    key: string;
    label: string;
    value: number;
    text: string; // подпись значения справа
    color?: string;
    sub?: string;
    dim?: boolean;
  }
  let { items, max = 0 }: { items: Bar[]; max?: number } = $props();
  const top = $derived(max || Math.max(1, ...items.map((i) => Math.abs(i.value))));
</script>

<div class="bars">
  {#each items as b (b.key)}
    <div class="bar-row" class:dim={b.dim}>
      <div class="bar-l"><span>{b.label}</span>{#if b.sub}<span class="sub">{b.sub}</span>{/if}</div>
      <div class="bar-track"><span style="width:{Math.max(2, (Math.abs(b.value) / top) * 100)}%;background:{b.color || 'var(--grad)'}"></span></div>
      <b class="bar-v">{b.text}</b>
    </div>
  {/each}
</div>

<style>
  .bars {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .bar-row {
    display: grid;
    grid-template-columns: minmax(110px, 0.9fr) 2fr auto;
    gap: 12px;
    align-items: center;
  }
  .bar-row.dim {
    opacity: 0.55;
  }
  .bar-l {
    display: flex;
    flex-direction: column;
    min-width: 0;
    font-weight: 600;
    font-size: 13.5px;
  }
  .bar-l .sub {
    font-weight: 400;
    font-size: 12px;
  }
  .bar-track {
    height: 10px;
    border-radius: 99px;
    background: rgba(226, 232, 240, 0.06);
    overflow: hidden;
  }
  .bar-track span {
    display: block;
    height: 100%;
    border-radius: 99px;
    box-shadow: 0 0 12px -2px rgba(139, 124, 255, 0.55);
  }
  .bar-v {
    font: 600 13px var(--mono);
    letter-spacing: -0.02em;
    min-width: 54px;
    text-align: right;
  }
  @media (prefers-reduced-motion: no-preference) {
    .bar-track span {
      animation: grow 0.8s cubic-bezier(0.3, 0.7, 0.2, 1) both;
      transform-origin: left;
    }
    @keyframes grow {
      from {
        transform: scaleX(0);
      }
    }
  }
</style>
