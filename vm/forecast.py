#!/usr/bin/env python3
"""
A demand forecaster, written from scratch.

    python3 forecast.py train      train on this shop's own history
    python3 forecast.py check      score it against the baselines
    python3 forecast.py ahead ID   what this product will need next month

── Why from scratch ────────────────────────────────────────────────────────

Not to prove a point about frameworks. Because of what the project claims.

Running somebody else's model on your own machine is now ordinary — Ollama
does it, LM Studio does it. What is not ordinary is TRAINING one there. A
shop's demand pattern is its private business intelligence: what sells, when
it sells, what dies on the shelf. Sending that to a vendor to train on is
precisely the thing this whole project argues against, so we do not.

The forward pass, the backward pass, the optimiser and the initialisation are
all written out below. numpy does array arithmetic and nothing else — there is
no autograd here, and no layer somebody else wrote. That means the weights are
inspectable, the whole thing is about two hundred lines, and it trains on a
laptop in seconds rather than needing a machine nobody in a small shop owns.

── What it is honestly worth ───────────────────────────────────────────────

A neural network implemented by hand is a course exercise, and on its own it
impresses nobody. The claim here is narrower and checkable: this is trained on
17,011 real products of purchase history, it is measured against the baselines
that matter, and `check` prints the comparison whether or not it flatters us.
A forecaster that cannot beat "next month looks like last month" is worse than
useless, because it is wrong with confidence. So that is the first thing it is
made to answer.
"""

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
HISTORY_NAME = "Hyd_20Products_20Purchase_20Data_2020-08-2026.xlsx"


def _shared():
    """Find the shared folder, by looking for something that should be in it.

    Inside the sandbox it is ~/shared. On the Mac, ~/shared also EXISTS — it is
    a different, empty directory — so testing whether the folder is there
    silently picks the wrong one and everything downstream reads nothing. That
    exact trap already cost this project a dashboard reporting a shop with no
    stock problems. Test for a file that only the real one has.
    """
    for base in (Path.home() / "shared", HERE.parent / "shared"):
        if (base / "training" / HISTORY_NAME).is_file():
            return base
    # Any shop's history, not one file name: the first shared folder whose
    # training material holds a monthly sheet (see _history).
    for base in (Path.home() / "shared", HERE.parent / "shared"):
        if _history(base) is not None:
            return base
    return HERE.parent / "shared"             # nothing found; fail with a real path


def _history(base):
    """The purchase history in this shared folder: the original file if it is
    there, otherwise the first spreadsheet whose header has at least LAGS + 2
    month columns — one row per product, one column per month."""
    import datetime as dt
    folder = base / "training"
    if (folder / HISTORY_NAME).is_file():
        return folder / HISTORY_NAME
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.xlsx")):
        try:
            import openpyxl
            ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
            header = next(ws.iter_rows(max_row=1, values_only=True))
        except Exception:
            continue
        if sum(isinstance(h, dt.datetime) for h in header) >= LAGS + 2:
            return path
    return None


LAGS = 6          # months of history the model sees
SHARED = _shared()
HISTORY = _history(SHARED) or SHARED / "training" / HISTORY_NAME
WEIGHTS = SHARED / "models" / "forecast.npz"
REPORT = SHARED / "models" / "forecast.json"

HIDDEN = 24       # hidden units — small on purpose; see fit()
SEED = 7


# ─── the data ───────────────────────────────────────────────────────────────

def load(path=HISTORY):
    """Monthly purchase quantities, one row per product.

    The sheet stores months as columns — 'wide' — which is how a person builds
    a spreadsheet and never how a model wants it. The first four columns are
    identifiers; everything after is a month.
    """
    import datetime as dt

    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header = list(next(rows))

    # Find the month columns by asking which headers are dates, rather than
    # assuming everything after the identifiers is one. In this sheet the
    # months are columns 4-20 and are followed by 'Manufacturer Name',
    # 'Closing Quantity', ten supplier columns and ten price columns. Taking
    # the whole tail produced a 51-month "history" whose last column was a
    # purchase price, and a model that trained to NaN on it.
    cols = [i for i, h in enumerate(header) if isinstance(h, dt.datetime)]
    if not cols:
        raise ValueError("no date columns found — is this the right sheet?")
    months = [header[i].strftime("%Y-%m") for i in cols]

    ids, names, series = [], [], []
    for r in rows:
        if not r or r[1] is None:
            continue
        # Negatives are real — a credit note or a return — and log1p is
        # undefined below -1, which is where the NaNs came from. Clipped at
        # zero: for "how much will be needed next month", a return is not
        # negative demand, it is no demand.
        vals = [max(0.0, float(r[i])) if i < len(r) and isinstance(r[i], (int, float))
                else 0.0 for i in cols]
        # A product that never moved teaches nothing except how to predict
        # zero, and there are enough of those to drown everything else.
        if sum(vals) <= 0:
            continue
        ids.append(str(r[1]))
        names.append(str(r[2] or ""))
        series.append(vals)
    wb.close()
    return ids, names, np.array(series, dtype=np.float64), months


def windows(series, lags=LAGS):
    """Slide a window over each product's history: `lags` months in, next out.

    Every position in every product's history is a training example, so 14,248
    products across 51 months give far more examples than products.
    """
    X, Y, who = [], [], []
    n_months = series.shape[1]
    for i in range(series.shape[0]):
        row = series[i]
        for t in range(lags, n_months):
            X.append(row[t - lags:t])
            Y.append(row[t])
            who.append(i)
    return np.array(X), np.array(Y), np.array(who)


def scale(X, Y):
    """Per-window normalisation, and log first.

    Two problems solved in one place. Purchase counts are heavily skewed — a
    few products move in thousands, most in single digits — so log1p pulls
    that into a range a small network can learn. And each window is divided by
    its own level, so the model learns SHAPE (rising, falling, seasonal)
    rather than memorising that product 4,411 is a big one. The level is
    handed back so the prediction can be returned to real units.
    """
    lx, ly = np.log1p(X), np.log1p(Y)
    level = lx.mean(axis=1, keepdims=True)
    return lx - level, ly - level.ravel(), level.ravel()


def unscale(pred, level):
    return np.expm1(pred + level)


# ─── the network ────────────────────────────────────────────────────────────
# One hidden layer with a tanh, and a linear output. Written out rather than
# assembled: forward, backward, and the update are all visible below.

def init(n_in, n_hidden, rng):
    """He-style scaling. Initialising to zeros gives every hidden unit the
    same gradient forever; initialising too large saturates the tanh and the
    gradient vanishes before the first epoch finishes."""
    return {
        "W1": rng.normal(0, np.sqrt(2.0 / n_in), (n_in, n_hidden)),
        "b1": np.zeros(n_hidden),
        "W2": rng.normal(0, np.sqrt(2.0 / n_hidden), (n_hidden, 1)),
        "b2": np.zeros(1),
    }


def forward(p, X):
    z1 = X @ p["W1"] + p["b1"]
    a1 = np.tanh(z1)
    return (a1 @ p["W2"] + p["b2"]).ravel(), a1


def backward(p, X, a1, pred, y):
    """The chain rule, by hand.

    Loss is mean squared error, so dL/dpred is 2(pred - y)/n. tanh' is
    (1 - tanh^2), and a1 already holds tanh(z1), so the derivative costs one
    multiply rather than a second forward pass.
    """
    n = len(y)
    d_pred = (2.0 / n) * (pred - y)
    gW2 = a1.T @ d_pred[:, None]
    gb2 = np.array([d_pred.sum()])
    d_a1 = d_pred[:, None] @ p["W2"].T
    d_z1 = d_a1 * (1.0 - a1 ** 2)
    return {"W1": X.T @ d_z1, "b1": d_z1.sum(axis=0), "W2": gW2, "b2": gb2}


def fit(X, y, epochs=60, batch=512, lr=0.05, hidden=HIDDEN, quiet=False):
    """Plain SGD with momentum. No framework, no autograd.

    The network is deliberately small. There are tens of thousands of training
    windows and six inputs; a larger one would memorise the training months and
    do worse on the month it has not seen, which is the only month anybody
    cares about.
    """
    rng = np.random.default_rng(SEED)
    p = init(X.shape[1], hidden, rng)
    vel = {k: np.zeros_like(v) for k, v in p.items()}
    n = len(y)
    for epoch in range(epochs):
        order = rng.permutation(n)
        total = 0.0
        for s in range(0, n, batch):
            idx = order[s:s + batch]
            xb, yb = X[idx], y[idx]
            pred, a1 = forward(p, xb)
            grads = backward(p, xb, a1, pred, yb)
            for k in p:
                vel[k] = 0.9 * vel[k] - lr * grads[k]
                p[k] += vel[k]
            total += float(((pred - yb) ** 2).sum())
        if not quiet and (epoch + 1) % 15 == 0:
            print(f"    epoch {epoch+1:3d}  train mse {total/n:.4f}")
    return p


# ─── measuring it honestly ──────────────────────────────────────────────────

def evaluate(series, lags=LAGS, quiet=False):
    """Train on everything but the last month; predict the last month.

    The split is by TIME, not at random. Shuffling months into the training
    set would let the model see the future of a product it is then tested on,
    which is the classic way to produce a forecaster that scores beautifully
    and is worthless.
    """
    train_part, test_part = series[:, :-1], series
    Xtr, ytr, _ = windows(train_part, lags)
    Xtr_s, ytr_s, _ = scale(Xtr, ytr)

    if not quiet:
        print(f"    {len(ytr):,} training windows from {series.shape[0]:,} products")
    p = fit(Xtr_s, ytr_s, quiet=quiet)

    # The held-out month: the last `lags` months in, the true last month out.
    Xte = test_part[:, -lags - 1:-1]
    yte = test_part[:, -1]
    Xte_s, _, level = scale(Xte, yte)
    pred = np.clip(unscale(forward(p, Xte_s)[0], level), 0, None)

    naive = Xte[:, -1]                       # next month looks like last month
    mean_k = Xte.mean(axis=1)                # or like the recent average

    def mae(a):
        return float(np.abs(a - yte).mean())

    return p, {
        "products": int(series.shape[0]),
        "months": int(series.shape[1]),
        "train_windows": int(len(ytr)),
        "mae_model": round(mae(pred), 4),
        "mae_naive_last": round(mae(naive), 4),
        "mae_naive_mean": round(mae(mean_k), 4),
    }


def verdict(m):
    best_baseline = min(m["mae_naive_last"], m["mae_naive_mean"])
    gain = (best_baseline - m["mae_model"]) / best_baseline * 100
    m["beats_baseline"] = bool(m["mae_model"] < best_baseline)
    m["improvement_pct"] = round(gain, 1)
    return m


# ─── commands ───────────────────────────────────────────────────────────────

def train():
    print("  reading this shop's own purchase history…")
    ids, names, series, months = load(HISTORY)
    print(f"  {series.shape[0]:,} products x {series.shape[1]} months\n")
    p, m = evaluate(series)
    m = verdict(m)

    WEIGHTS.parent.mkdir(parents=True, exist_ok=True)
    np.savez(WEIGHTS, **p, ids=np.array(ids), lags=LAGS)
    REPORT.write_text(json.dumps(m, indent=2), encoding="utf-8")

    print(f"\n  held-out month: {months[-1]}")
    print(f"    this model          MAE {m['mae_model']:8.3f}")
    print(f"    same as last month  MAE {m['mae_naive_last']:8.3f}")
    print(f"    recent average      MAE {m['mae_naive_mean']:8.3f}")
    print()
    if m["beats_baseline"]:
        print(f"  Beats the best baseline by {m['improvement_pct']}%.")
    else:
        print("  Does NOT beat the baselines. On this data the honest answer "
              "is to use the baseline and say so.")
    print(f"  weights → {WEIGHTS}")
    return m


def ahead(product_id):
    if not WEIGHTS.exists():
        sys.exit("no model yet — run: python3 forecast.py train")
    d = np.load(WEIGHTS, allow_pickle=True)
    p = {k: d[k] for k in ("W1", "b1", "W2", "b2")}
    ids, names, series, months = load(HISTORY)
    try:
        i = ids.index(product_id)
    except ValueError:
        sys.exit(f"no product {product_id!r} in the history")
    window = series[i][-LAGS:][None, :]
    Xs, _, level = scale(window, np.array([0.0]))
    pred = float(np.clip(unscale(forward(p, Xs)[0], level), 0, None)[0])
    print(f"  {product_id}  {names[i][:40]}")
    print(f"  last {LAGS} months: {[int(v) for v in series[i][-LAGS:]]}")
    print(f"  next month, forecast: {pred:.1f}")
    return pred


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"
    if cmd == "train":
        if len(sys.argv) > 2:                # a history file named outright
            HISTORY = Path(sys.argv[2])
        train()
    elif cmd == "check":
        print(json.dumps(json.loads(REPORT.read_text(encoding="utf-8")), indent=2)
              if REPORT.exists() else "not trained yet")
    elif cmd == "ahead":
        ahead(sys.argv[2] if len(sys.argv) > 2 else sys.exit("usage: ahead PRODUCT_ID"))
    else:
        print(__doc__.strip())
