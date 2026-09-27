"""Generate a synthetic parameter sweep that looks like real simulation output.

Model: a two-level (Schottky) excitation with gap D(U, J, n) on top of a
Fermi-liquid T^2 term:

    E(T) = E0 + D / (1 + exp(D / T)) + gamma * T^2 / 2

so the specific heat C = dE/dT has a peak near T ~ 0.42 D.

Sources, each with its own quirks:
  data/dmft/U_*/J_*/n_*.dat   DMFT: 40-80 log-spaced T points, a different grid in every file
  data/ed/U_*/J_*/n_*.dat     ED on a small cluster: 12-24 linear T points, a finite-size
                              error at low T, no runs for U=4 n=0.8, and one file with a
                              repeated T row from a restarted job
  data/gap/U_*/J_*/n_*.dat    one number per run: the gap D
  data/qmc.db                 QMC in SQLite: noisy energies with error bars, stored by beta = 1/T

Run `python make_data.py --rerun-ed` to simulate rerunning two ED jobs on a
larger cluster, or `--rerun-dmft` to rerun one DMFT job. Either one rewrites a
few files, which makes the saved datasets that use them stale.
"""

import argparse
import os
import sqlite3

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
U_VALUES = [1.0, 2.0, 3.0, 4.0]
J_VALUES = [0.0, 0.1, 0.2, 0.3]
N_VALUES = [0.8, 0.9, 1.0]


def gap(U, J, n):
    return 0.12 * U * (1 + 1.5 * J) * (1 - 0.5 * abs(n - 1.0) / 0.2)


def energy(T, U, J, n):
    D = gap(U, J, n)
    E0 = -0.8 * n * (2 - n) / (1 + 0.15 * U) + U * n * n / 8 * (1 - J)
    gamma = 0.4 / (1 + U)
    with np.errstate(over="ignore"):
        return E0 + D / (1 + np.exp(D / T)) + 0.5 * gamma * T**2


def ed_error(T, U, cluster=8):
    return 0.015 * U * (8 / cluster) ** 2 * np.exp(-T / 0.08)


def fname(root, U, J, n):
    d = os.path.join(DATA, root, f"U_{U}", f"J_{J}")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"n_{n}.dat")


def write(path, T, E, header):
    with open(path, "w") as f:
        f.write(header)
        for t, e in zip(T, E):
            f.write(f"{t:14.8f} {e:16.10f}\n")


def make(seed=7):
    rng = np.random.default_rng(seed)
    for U in U_VALUES:
        for J in J_VALUES:
            for n in N_VALUES:
                # DMFT: log-spaced, different range and count per run
                N = int(rng.integers(40, 81))
                tmin, tmax = rng.uniform(0.008, 0.02), rng.uniform(1.0, 1.5)
                T = np.sort(np.geomspace(tmin, tmax, N) * (1 + rng.normal(0, 0.01, N)))
                E = energy(T, U, J, n) + rng.normal(0, 2e-6, N)
                write(fname("dmft", U, J, n), T, E,
                      f"# DMFT, U={U} J={J} n={n}, CTQMC impurity solver\n# T E\n")

                # ED: linear grid, fewer points, finite-size error
                if not (U == 4.0 and n == 0.8):
                    N = int(rng.integers(12, 25))
                    T = np.linspace(rng.uniform(0.02, 0.04), rng.uniform(0.9, 1.1), N)
                    E = energy(T, U, J, n) + ed_error(T, U)
                    if (U, J, n) == (2.0, 0.1, 0.9):
                        T = np.insert(T, 5, T[5])
                        E = np.insert(E, 5, E[5] + 3e-4)
                    write(fname("ed", U, J, n), T, E, f"# ED 8-site cluster, U={U} J={J} n={n}\n# T E\n")

                with open(fname("gap", U, J, n), "w") as f:
                    f.write(f"# gap\n{gap(U, J, n):.10f}\n")

    db = os.path.join(DATA, "qmc.db")
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE runs (u REAL, j REAL, n REAL, beta REAL, energy REAL, energy_err REAL, sweeps INTEGER)")
    rows = []
    for U in U_VALUES:
        for J in (0.1,):
            for n in (1.0,):
                for beta in [2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 25, 30, 40]:
                    T = 1.0 / beta
                    err = 0.0015 * (1 + U / 4) * np.sqrt(beta / 10)
                    E = float(energy(np.array([T]), U, J, n)[0] + rng.normal(0, err))
                    rows.append((U, J, n, float(beta), E, err, int(rng.integers(1, 5)) * 10**6))
    con.executemany("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def rerun_ed():
    """Rerun two ED jobs on a 12-site cluster: smaller finite-size error."""
    for U, J, n in [(2.0, 0.1, 1.0), (3.0, 0.1, 1.0)]:
        T = np.linspace(0.03, 1.0, 20)
        E = energy(T, U, J, n) + ed_error(T, U, cluster=12)
        write(fname("ed", U, J, n), T, E, f"# ED 12-site cluster (rerun), U={U} J={J} n={n}\n# T E\n")
        print(f"rewrote {os.path.relpath(fname('ed', U, J, n), HERE)}")


def rerun_dmft():
    """Rerun one DMFT job with more temperatures."""
    U, J, n = 3.0, 0.2, 1.0
    T = np.geomspace(0.01, 1.2, 120)
    write(fname("dmft", U, J, n), T, energy(T, U, J, n),
          f"# DMFT, U={U} J={J} n={n}, CTQMC impurity solver (rerun, 120 points)\n# T E\n")
    print(f"rewrote {os.path.relpath(fname('dmft', U, J, n), HERE)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerun-ed", action="store_true", help="rewrite two ED files")
    ap.add_argument("--rerun-dmft", action="store_true", help="rewrite one DMFT file")
    args = ap.parse_args()
    if args.rerun_ed or args.rerun_dmft:
        if args.rerun_ed:
            rerun_ed()
        if args.rerun_dmft:
            rerun_dmft()
    else:
        make()
        print(f"wrote data under {os.path.relpath(DATA, os.getcwd())}")
