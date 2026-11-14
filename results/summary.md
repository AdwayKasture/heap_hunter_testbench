# heap_hunter vs Eclipse MAT benchmark

## Environment

- host: archon (5.15.167.4-microsoft-standard-WSL2), 20 CPUs
- RAM: 15836 MB, swap: 4096 MB
- hunt: hunt version 0.1.0
- MAT: 1.17.0.20260601 (openjdk version "21.0.13.0.3" 2026-10-24)
- memory limits: tool-level caps (hunt --sorting/--dominator-budget-mb, MAT -Xmx); hard cgroup caps unavailable in this WSL2 hybrid-cgroup setup

## Index phase (per dump x cap)

### mixed-2g

| tool | cap MB | status | wall s | peak RSS MB | index MB |
|---|---|---|---|---|---|
| hunt | 256 | fail | 106.74 | 3225.2 | 0.0 |
| hunt | 512 | fail | 75.55 | 3686.0 | 0.0 |
| hunt | 1024 | ok | 140.65 | 5968.2 | 1650.3 |
| hunt | 2048 | ok | 151.55 | 5836.2 | 1650.3 |
| mat | 1024 | ok | 100.71 | 1353.6 | 1318.7 |
| mat | 2048 | ok | 86.49 | 2352.8 | 1318.7 |
| mat | 4096 | ok | 92.63 | 4211.4 | 1318.7 |

### mixed-512m

| tool | cap MB | status | wall s | peak RSS MB | index MB |
|---|---|---|---|---|---|
| hunt | 256 | ok | 14.78 | 1506.8 | 416.1 |
| hunt | 512 | ok | 15.76 | 1495.3 | 416.1 |
| hunt | 1024 | ok | 14.48 | 1825.0 | 416.1 |
| mat | 512 | ok | 27.34 | 739.7 | 322.3 |
| mat | 1024 | ok | 30.02 | 1278.7 | 322.3 |
| mat | 2048 | ok | 28.85 | 1319.8 | 322.3 |

### mixed-6g

| tool | cap MB | status | wall s | peak RSS MB | index MB |
|---|---|---|---|---|---|
| hunt | 512 | fail | 355.73 | 7727.9 | 0.0 |
| hunt | 1024 | fail | 302.89 | 8000.8 | 0.0 |
| hunt | 2048 | fail | 104.94 | 8852.8 | 0.0 |
| hunt | 4096 | ok | 580.39 | 11243.6 | 4932.9 |
| mat | 2048 | ok | 207.26 | 2503.7 | 4032.9 |
| mat | 4096 | ok | 201.38 | 4399.0 | 4032.9 |
| mat | 8192 | ok | 183.83 | 8607.5 | 4032.9 |

## Query phases (successful index reused)

### mixed-2g

| tool | phase | status | wall s | peak RSS MB |
|---|---|---|---|---|
| hunt | histogram | ok | 0.21 | 99.5 |
| hunt | dominator | ok | 0.2 | 99.5 |
| hunt | roots | ok | 0.21 | 99.5 |
| mat | reports | ok | 200.6 | 4453.0 |

### mixed-512m

| tool | phase | status | wall s | peak RSS MB |
|---|---|---|---|---|
| hunt | histogram | ok | 0.21 | 14.8 |
| hunt | dominator | ok | 0.21 | 18.3 |
| hunt | roots | ok | 0.68 | 79.7 |
| mat | reports | ok | 56.72 | 1891.9 |

### mixed-6g

| tool | phase | status | wall s | peak RSS MB |
|---|---|---|---|---|
| hunt | histogram | ok | 0.2 | 99.5 |
| hunt | dominator | ok | 0.2 | 99.5 |
| hunt | roots | ok | 0.43 | 99.5 |
| mat | reports | ok | 641.15 | 8497.6 |

## Correctness cross-check (hunt vs MAT histograms)

| dump | objects hunt | objects MAT | obj delta % | shallow delta % | classes w/ diffs |
|---|---|---|---|---|---|
| mixed-512m | 5,476,299 | 5,478,112 | -0.033 | -6.623 | 666/737 |
| mixed-2g | 21,752,029 | 21,763,655 | -0.053 | -6.811 | 666/737 |
| mixed-6g | 65,025,906 | 65,067,739 | -0.064 | -6.642 | 666/737 |
