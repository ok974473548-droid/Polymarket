// Окно подтверждения: confirmDialog({...}) → Promise<boolean>. Рисует компонент ConfirmDialog.svelte.
export interface ConfirmOpts {
  title: string;
  text: string;
  ok?: string;
  danger?: boolean;
}

export const confirmState = $state({
  open: false,
  title: "",
  text: "",
  ok: "Подтвердить",
  danger: false,
  resolve: null as null | ((v: boolean) => void),
});

export function confirmDialog({ title, text, ok = "Подтвердить", danger = false }: ConfirmOpts): Promise<boolean> {
  return new Promise((resolve) => {
    Object.assign(confirmState, { open: true, title, text, ok, danger, resolve });
  });
}

export function closeConfirm(result: boolean): void {
  const r = confirmState.resolve;
  Object.assign(confirmState, { open: false, resolve: null });
  r?.(result);
}
