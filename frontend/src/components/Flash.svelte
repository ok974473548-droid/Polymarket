<script lang="ts">
  // Число, которое подсвечивается при изменении (живые цены)
  let { text, cls = "" }: { text: string; cls?: string } = $props();
  let seq = $state(0);
  let prev = "";
  $effect(() => {
    if (prev && text !== prev) seq++;
    prev = text;
  });
</script>

{#key seq}<span class="{cls}{seq ? ' tick' : ''}">{text}</span>{/key}
