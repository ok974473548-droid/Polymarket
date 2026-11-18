// Отметка на графике цены: вход кита, наша покупка (цена — доля 0..1)
export interface Mark {
  ts: number;
  price: number | null;
  label: string;
  color: string;
}
