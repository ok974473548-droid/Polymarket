"""Сверка с эталоном (tests/golden, снят на pandas-версии 0.15.0): переход на polars не меняет результаты.

Позиции, связи кошельков, оценки китов (в том числе бутстреп и тест на удачу — тот же генератор случайных
чисел в том же порядке), живой список, бэктест и walk-forward сравниваются до 1e-9 относительной погрешности.
"""

import gzip
import json
import math
from pathlib import Path

import pytest

from golden.make_golden import WORLDS, snapshot

HERE = Path(__file__).resolve().parent / "golden"
REL = 1e-9


def _diff(a, b, path="", out=None, limit=20):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: {'нет в эталоне' if k not in a else 'нет в новом'}")
            else:
                _diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: длина {len(a)} → {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out, limit)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if math.isnan(float(a)) and math.isnan(float(b)):
            return out
        if not math.isclose(float(a), float(b), rel_tol=REL, abs_tol=1e-12):
            out.append(f"{path}: {a!r} → {b!r}")
    elif isinstance(a, str) and isinstance(b, str) and a.startswith("{") and b.startswith("{"):
        try:
            _diff(json.loads(a), json.loads(b), path + "<json>", out, limit)
        except ValueError:
            if a != b:
                out.append(f"{path}: {a!r} → {b!r}")
    elif a != b:
        out.append(f"{path}: {a!r} → {b!r}")
    return out


@pytest.mark.parametrize("name", list(WORLDS))
def test_results_match_golden(name):
    with gzip.open(HERE / f"{name}.json.gz", "rt", encoding="utf-8") as fh:
        gold = json.load(fh)
    new = json.loads(json.dumps(snapshot(name, WORLDS[name]), sort_keys=True, default=str))
    problems = _diff(gold, new)
    assert not problems, "Результаты разошлись с эталоном:\n" + "\n".join(problems)
