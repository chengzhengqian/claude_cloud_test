"""Compute the spin-1/2 XXZ chain dataset used by TUTORIAL.md.

    H = J sum_i [ Sx_i Sx_{i+1} + Sy_i Sy_{i+1} + Delta Sz_i Sz_{i+1} ] - h sum_i Sz_i,  J = 1, periodic

Everything here is real numerics, not made-up curves:

- data/ed/L_{L}/Delta_{D}/h_{h}.dat  full exact diagonalization, one text file per run, each run
  with its own temperature grid. Columns T E C M chi, per site. Like real output, one run was
  restarted (some rows appear twice) and one run is missing.
- data/ftlm.db  finite-temperature Lanczos for longer chains, h = 0. Another code with other
  conventions: it calls Delta "jz", stores beta = 1/T, and writes energies for the whole chain.
  Random starting vectors give real statistical error bars (jackknife).
- data/gap.h5  spin gap and ground-state energy per site, one HDF5 group per (L, Delta).
- data/exact/xx_h{h}.csv  the exact infinite chain at Delta = 0 (free fermions), as CSV.

Run:  python make_data.py            (about a minute)
      python make_data.py --rerun    (recompute two ED runs with a finer grid, for the update demo)
"""

import argparse
import itertools
import os
import sqlite3
import time

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import eigsh

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

ED_L = [8, 10, 12, 14]
DELTAS = [0.0, 0.5, 1.0, 1.5]
FIELDS = [0.0, 0.5]
FTLM_L = [16, 18]
FTLM_DELTAS = [0.5, 1.0, 1.5]
MISSING = {(14, 1.5, 0.5)}          # this job never finished
RESTARTED = (12, 1.0, 0.5)          # this job was restarted and wrote some rows twice


# ------------------------------------------------------------------ Hamiltonian in one Sz sector


def sector_states(L, n_up):
    states = [sum(1 << i for i in c) for c in itertools.combinations(range(L), n_up)]
    return np.array(sorted(states), dtype=np.int64)


def sector_hamiltonian(L, n_up, delta, sparse=False):
    s = sector_states(L, n_up)
    dim = len(s)
    diag = np.zeros(dim)
    rows, cols = [], []
    for i in range(L):
        j = (i + 1) % L
        bi = (s >> i) & 1
        bj = (s >> j) & 1
        same = bi == bj
        diag += np.where(same, 0.25, -0.25) * delta
        flip = np.nonzero(~same)[0]
        new = s[flip] ^ ((1 << i) | (1 << j))
        rows.append(flip)
        cols.append(np.searchsorted(s, new))
    rows, cols = np.concatenate(rows), np.concatenate(cols)
    if sparse:
        off = sp.csr_matrix((np.full(len(rows), 0.5), (rows, cols)), shape=(dim, dim))
        return off + sp.diags(diag)
    H = np.zeros((dim, dim))
    np.add.at(H, (rows, cols), 0.5)
    H[np.diag_indices(dim)] += diag
    return H


def spectrum(L, delta):
    """All eigenvalues with their Sz, from full diagonalization of every sector."""
    energies, sz = [], []
    for n_up in range(L + 1):
        w = np.linalg.eigvalsh(sector_hamiltonian(L, n_up, delta))
        energies.append(w)
        sz.append(np.full(len(w), n_up - L / 2))
    return np.concatenate(energies), np.concatenate(sz)


def thermo(E, Sz, L, h, T):
    """Energy, specific heat, magnetization, and susceptibility per site at temperatures T."""
    Eh = E - h * Sz
    E0 = Eh.min()
    b = 1.0 / T[:, None]
    w = np.exp(-b * (Eh - E0)[None, :])
    Z = w.sum(1)
    e = (w * Eh).sum(1) / Z
    e2 = (w * Eh**2).sum(1) / Z
    m = (w * Sz).sum(1) / Z
    m2 = (w * Sz**2).sum(1) / Z
    return e / L, (e2 - e**2) / T**2 / L, m / L, (m2 - m**2) / T / L


# ------------------------------------------------------------------ finite-temperature Lanczos


def lanczos(H, v, m):
    alpha, beta = [], []
    v = v / np.linalg.norm(v)
    v_prev = np.zeros_like(v)
    b = 0.0
    V = [v]
    for k in range(m):
        w = H @ v - b * v_prev
        a = v @ w
        w -= a * v
        for u in V:                      # full reorthogonalization, to keep the Krylov basis clean
            w -= (u @ w) * u
        alpha.append(a)
        b = np.linalg.norm(w)
        if b < 1e-12 or k == m - 1:
            break
        beta.append(b)
        v_prev, v = v, w / b
        V.append(v)
    Tm = np.diag(alpha) + np.diag(beta[: len(alpha) - 1], 1) + np.diag(beta[: len(alpha) - 1], -1)
    eps, vec = np.linalg.eigh(Tm)
    return eps, vec[0] ** 2


def ftlm(L, delta, betas, R=6, M=60, seed=0):
    """Total energy and specific heat of the whole chain with jackknife errors over R random vectors."""
    rng = np.random.default_rng(seed + 1000 * L + int(100 * delta))
    parts = []                           # per random vector: list of (eps, weights * sector dim)
    for r in range(R):
        parts.append([])
    e0 = np.inf
    for n_up in range(L + 1):
        H = sector_hamiltonian(L, n_up, delta, sparse=True)
        dim = H.shape[0]
        for r in range(R):
            if dim <= M:
                eps, vec = np.linalg.eigh(H.toarray())
                wts = np.full(len(eps), 1.0 / R)          # exact small sectors: same in every sample
                parts[r].append((eps, wts * R))
            else:
                eps, wts = lanczos(H, rng.standard_normal(dim), M)
                parts[r].append((eps, wts * dim))
            e0 = min(e0, parts[r][-1][0].min())
    b = betas[:, None]

    def moments(sample_ids):
        Z = np.zeros(len(betas)); E1 = np.zeros(len(betas)); E2 = np.zeros(len(betas))
        for r in sample_ids:
            for eps, wts in parts[r]:
                x = np.exp(-b * (eps - e0)[None, :]) * wts[None, :]
                Z += x.sum(1); E1 += (x * eps).sum(1); E2 += (x * eps**2).sum(1)
        e = E1 / Z
        return e, (E2 / Z - e**2) * betas**2

    e_all, c_all = moments(range(R))
    jk = [moments([q for q in range(R) if q != r]) for r in range(R)]
    ej = np.array([j[0] for j in jk]); cj = np.array([j[1] for j in jk])
    e_err = np.sqrt((R - 1) / R * ((ej - ej.mean(0)) ** 2).sum(0))
    c_err = np.sqrt((R - 1) / R * ((cj - cj.mean(0)) ** 2).sum(0))
    return e_all, e_err, c_all, c_err


def ground_gap(L, delta):
    """Spin gap E0(Sz=1) - E0(Sz=0) and ground-state energy per site, from Lanczos (eigsh)."""
    e = {}
    for n_up in (L // 2, L // 2 + 1):
        H = sector_hamiltonian(L, n_up, delta, sparse=True)
        e[n_up] = eigsh(H, k=1, which="SA")[0][0] if H.shape[0] > 50 else np.linalg.eigvalsh(H.toarray())[0]
    return e[L // 2 + 1] - e[L // 2], e[L // 2] / L


# ------------------------------------------------------------------ exact XX chain (Delta = 0)


def xx_exact(h, T):
    k = np.linspace(-np.pi, np.pi, 4001)[:-1]
    xi = np.cos(k) - h
    t = T[:, None]
    f = 1.0 / (np.exp(np.clip(xi[None, :] / t, -700, 700)) + 1.0)
    E = (xi * f).mean(1) + h / 2
    C = (xi**2 * f * (1 - f)).mean(1) / T**2
    M = f.mean(1) - 0.5
    chi = (f * (1 - f)).mean(1) / T
    return E, C, M, chi


# ------------------------------------------------------------------ writing


def t_grid(rng, fine=False):
    tmin = rng.uniform(0.02, 0.06)
    tmax = rng.uniform(3.0, 6.0)
    n = int(rng.integers(40, 91)) * (3 if fine else 1)
    return np.round(np.geomspace(tmin, tmax, n), 6)


def write_ed(L, delta, h, T, cols, restarted=False):
    d = os.path.join(DATA, "ed", f"L_{L}", f"Delta_{delta}")
    os.makedirs(d, exist_ok=True)
    rows = np.column_stack([T] + list(cols))
    if restarted:                        # rows 25-31 written again after the restart
        rows = np.vstack([rows[:32], rows[25:]])
    with open(os.path.join(d, f"h_{h}.dat"), "w") as f:
        f.write(f"# ed-xxz 2.3  L={L} Delta={delta} h={h} periodic\n")
        f.write("# T E C M chi\n")
        for r in rows:
            f.write(" ".join(f"{x:.10g}" for x in r) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerun", action="store_true", help="recompute two ED runs with a finer T grid")
    args = ap.parse_args()
    t0 = time.time()
    if args.rerun:
        for L, delta, h in [(12, 1.5, 0.0), (10, 0.5, 0.5)]:
            E, Sz = spectrum(L, delta)
            T = t_grid(np.random.default_rng(99 + L), fine=True)
            write_ed(L, delta, h, T, thermo(E, Sz, L, h, T))
            print(f"rewrote data/ed/L_{L}/Delta_{delta}/h_{h}.dat with {len(T)} temperatures")
        return

    rng = np.random.default_rng(7)
    gaps = {}
    for L in ED_L:
        for delta in DELTAS:
            E, Sz = spectrum(L, delta)
            for h in FIELDS:
                T = t_grid(rng)
                if (L, delta, h) in MISSING:
                    continue
                write_ed(L, delta, h, T, thermo(E, Sz, L, h, T), restarted=(L, delta, h) == RESTARTED)
            gaps[(L, delta)] = ground_gap(L, delta)
        print(f"ED L={L} done ({time.time() - t0:.0f} s)")

    path = os.path.join(DATA, "ftlm.db")
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ftlm (L INTEGER, jz REAL, beta REAL, e_tot REAL, e_err REAL, "
                "cv_tot REAL, cv_err REAL, nvec INTEGER, lanczos_steps INTEGER)")
    betas = np.round(np.geomspace(0.2, 40.0, 60), 6)
    for L in FTLM_L:
        for delta in FTLM_DELTAS:
            e, de, c, dc = ftlm(L, delta, betas)
            con.executemany("INSERT INTO ftlm VALUES (?,?,?,?,?,?,?,?,?)",
                            [(L, delta, b, x, y, z, w, 6, 60) for b, x, y, z, w in zip(betas, e, de, c, dc)])
            gaps[(L, delta)] = ground_gap(L, delta)
        for delta in DELTAS:
            if (L, delta) not in gaps:
                gaps[(L, delta)] = ground_gap(L, delta)
        print(f"FTLM L={L} done ({time.time() - t0:.0f} s)")
    con.commit()
    con.close()

    import h5py

    with h5py.File(os.path.join(DATA, "gap.h5"), "w") as f:
        f.attrs["description"] = "spin gap E0(Sz=1) - E0(Sz=0) and ground-state energy per site"
        for (L, delta), (gap, e0) in sorted(gaps.items()):
            g = f.require_group(f"L_{L}/Delta_{delta}")
            g.create_dataset("data", data=np.array([[gap, e0]]))

    os.makedirs(os.path.join(DATA, "exact"), exist_ok=True)
    T = np.round(np.geomspace(0.01, 10.0, 300), 6)
    for h in FIELDS:
        E, C, M, chi = xx_exact(h, T)
        with open(os.path.join(DATA, "exact", f"xx_h{h}.csv"), "w") as f:
            f.write("temperature,energy,heat_capacity,magnetization,susceptibility\n")
            for r in zip(T, E, C, M, chi):
                f.write(",".join(f"{x:.10g}" for x in r) + "\n")
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
