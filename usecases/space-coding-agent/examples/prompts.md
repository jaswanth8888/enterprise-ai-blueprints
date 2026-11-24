# Demo prompts

The UI has chips for these. Each shows a different part of the Sandbox Board.

| Prompt | What to watch |
|---|---|
| **Monte Carlo π (8 microVMs).** Estimate pi with a Monte Carlo simulation in 8 parallel sandboxes, seeds 1 to 8, 4,000,000 samples each. Report each estimate, then the mean, the standard deviation and the error against math.pi, in a table. | `run_sweep`: 8 cards go *creating → ready → running → done → killed* together; the banner shows boot time and the run phase side by side vs in turn. |
| **Queue sweep (8 microVMs).** Simulate an M/M/1 queue in 8 parallel sandboxes, one per utilisation rho = 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, with service rate 1, 200,000 customers and seed 7. Compare the simulated mean wait in the queue with the theory Wq = rho / (1 - rho) in a table. | A parameter sweep; numeric `PARAM` values arrive as numbers. |
| **Sort benchmark (4 microVMs).** Benchmark four sorting algorithms in parallel, one microVM each: quicksort, mergesort, heapsort (with heapq) and Python's built-in sorted (timsort). Every task generates the same 1,000,000 random ints with seed 42, checks that its output is sorted, and reports the best of 3 runs in seconds. Compare them in a table. | Different code per microVM (`run_sweep` on the algorithm name, or `run_parallel`). |
| **Session persistence (1 microVM).** In your sandbox, write data.csv with 1,000 rows of synthetic sales data (date, region, amount) using seed 3. Then, in a separate step, read it back and report the total and average amount per region. | One *session* card that stays up; both calls reuse the same sandbox id. |
| **Isolation check.** Show what this sandbox is: run uname -a, nproc, free -m and df -h. Then try to reach http://litellm.litellm.svc.cluster.local:4000 from inside it and tell me what happens. | The guest kernel differs from the pod kernel (top bar chip); cluster names do not resolve inside the microVM. |

Try **Cancel** during a parallel run: every card ends *killed (cancelled)*, and
`/api/status` returns to 0 live microVMs.
