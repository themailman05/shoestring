# Dataset notes — annotated rows and provenance events

Failure rows with operator-provided context. A documented failure with a
named cause is worth more to the framework than a clean row; a documented
failure with an *honestly unresolved* cause is worth almost as much.

## 2026-07-30T12:07:39Z · akash1sevd2… (provider.jjozzietech.com.au) · never_reachable

- On-chain lease: dseq `1785413103258`, created 2026-07-30T12:05:03Z,
  tenant `akash1dtf429gnxhdgtfklm50l0tws8ukkl7m7ejk9e9`.
- The provider's only failure in 48 pilot leases (98% reachable, 30s
  median time-to-ready otherwise).
- **Operator investigation** ([comment](https://github.com/orgs/akash-network/discussions/1508)):
  a GPU node on this provider was silently network-partitioned
  ~16 Jul – 1 Aug (libceph ENETUNREACH → Calico down → no default
  routes), while its kubelet kept the node `Ready` — a **silent
  partition**: the scheduler places workloads on a node that cannot run
  them; the ingress URI exists; nothing ever answers.
- **Causation status — final, per the operator's node lookup:** failure
  occurred; provider-side records aged out; root cause unconfirmed. A
  documented silent-partition incident on one node (kubelet log flood
  \~140 lines/min vs 1–5 baseline, continuous through the window)
  overlapped and could account for it if the workload was scheduled
  there; placement is no longer determinable. Post-incident detection has
  since been added by the operator for this failure class (with a
  disclosed gap: the Ceph-OSD cross-check misses OSD-less nodes;
  per-node outbound heartbeat is planned but not built).
- **Lease-duration cross-check (operator):** on-chain the lease lived 23
  blocks (\~138s) and closed `lease_closed_owner`. Consistent with the
  probe design: the lease exists only from bid-acceptance to close —
  \~10s endpoint discovery + a full 120s reachability poll; the longer
  "\~3.5 minute" figure includes pre-lease bid wait. The poll ran its
  full window.
- Operator remediations shipped: Ceph OSD status cross-check in daily
  health script; per-node outbound heartbeat cron.

### Framework lessons adopted from this case

1. **Failure needs a cause class, not a boolean.** `never_reachable`
   bundles broken providers with transiently-faulted nodes; they should
   score differently.
2. **Retry discriminates cheaply.** A follow-up lease landing on a
   healthy node distinguishes node-scoped from provider-scoped failure.
3. **Recency-weight the scorecard.** One failure inside a two-week
   incident window ≠ a continuous 1-in-48 failure rate.
4. **Attribution data must be captured at write time.** Post-hoc
   attribution proved unreliable *even for the operator, with full log
   access, within a month*. The canary now records the lease dseq and
   the ingress endpoint per row for this reason (same principle that
   forced the gpu_model fix).

## gpu_model provenance events

- 2026-08-30: 99 historical rows backfilled by unambiguous market-inventory
  inference; 123 left NULL (see `README.md`).
- 2026-09-02: all 48 `akash1sevd2…` rows set to `rtx3090` —
  **operator-confirmed** on the record in the proposal thread.

## Dataset completeness audit (chain reconciliation, 2026-09-03)

`scripts/reconcile_chain.py` audits the dataset against the chain — the
ground-truth ledger of every lease the canary wallet created. Result:
**one lost row in the dataset's lifetime** (2026-08-29T16:05Z, dseq
`1788019503640`, provider `akash1al463…`): the run completed its lease
lifecycle but died before recording — resource contention on the thin
cluster hosting the legacy canary is the leading suspect. All other
unmatched chain leases were identified as workloads or tests and are
published with purposes in `nonprobe_leases.csv` — including the ML
training campaign lease (`1785460994984`) this project grew out of.
Recording durability is a reason the probe series is migrating to public
CI (and, planned, to Akash itself).

## 2026-07-18 · akash1sevd2… · two manifest_timeout closes (operator-contributed)

Volunteered by the operator from chain records — failures the dataset
never saw because they predate it (my manual testing era, GPU-tier bids
at 170.105767 uact/blk vs the pilot's flat 153.916): dseqs
`1784390016357` and `1784390613845`, created 9m54s apart, both closed
`lease_closed_reason_manifest_timeout` at exactly 52 blocks. One
~10-minute degraded window, cause not reconstructable 63 days later, no
recurrence since. Classified honestly: one incident, unknown cause,
operator-disclosed.

## Clock calibration (operator-contributed)

Console-API dseqs are client-generated ms timestamps and land 3–4 blocks
(~18–21s) before on-chain `created_at` — broadcast-to-commit lag. Rows
in this dataset quote client-side times; chain-side timestamps run ~20s
later. Matching windows account for this.
