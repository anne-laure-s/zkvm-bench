# Call-graph investigation — why hotspots are flat, and what a real call tree would take

**TL;DR.** The flat profile is **not** a frame-pointer problem (an earlier version of this note wrongly blamed frame pointers).
ziskemu reconstructs call stacks with an **`ra`-based heuristic**, and it (1) hard-disables the
whole feature at the first mismatch, and (2) even with that disable removed, **does not nest**
for optimized guests like reth/Monad — you get a depth-1 (flat) tree. A genuine deep call graph
is a real piece of profiler work, not a one-line patch.

## Where the logic is (commit 8447ee0)
- `emulator/src/stats/stats.rs`
  - `check_roi()` — per-instruction call/return tracking over ROIs (functions from ELF symbols).
  - `update()` ~L665-700 — sets `is_call = instruction.store_pc` (jal/jalr that saves PC) and
    `is_return` = jalr to `ra`/x1 (or the top frame's `return_reg`).
  - `call_stack_error()` ~L295 — on any mismatch sets `disable_call_stack = true` → the profile
    goes flat/empty. Mismatch sites: "RETURN CALL unexpected" (L401), "STACK MISMATCH" (L415),
    cost errors (L466/L484), "COST OVERFLOW" (L491), "Unexpected RETURN" (L500).
- `emulator/src/stats/profiler.rs` — `CallPathProfiler`: `push_call_path`/`pop_call_path` drive a
  `prefix_stack` + `stack_table`, serialized as a **Firefox Profiler** profile (`--profiler-output`).

## What the patch here does (`ziskemu-callstack.patch`)
- `call_stack_error()` → no longer disables; treats a mismatch as a cost-accounting hiccup and
  keeps profiling (the caller already popped one frame from BOTH stacks in sync).
- adds `CallPathProfiler::reset_call_path()` (unused in the final variant; kept for the resync
  approach).

**Effect (measured, reth block 1-24647140):** the profiler now runs to completion and emits a
valid Firefox profile with a real `shared.stackTable` — **378,214 samples** instead of 1. BUT:
`stackTable.length = 156`, and **155/156 nodes are roots** (prefix = None), depth distribution
= 100% at depth 1. So it's a **valid but flat** call tree: functions are recorded, nesting is not.

## Why it stays flat (root cause)
The `ra` heuristic emits a *return* between essentially every *call* for this code, so the
profiler's `prefix_stack` is empty at almost every push (push→pop→push→pop at depth ≤1). Likely
causes on optimized EVM/trie code: computed/indirect jumps and dispatch loops that don't follow
the `jal ra … / jalr x0, ra` convention the detector assumes; tail calls; precompile ecall
trampolines. The detector can't tell a real nested call from a sibling call, so it never builds
depth. This is a heuristic limitation, independent of frame pointers.

## What a real call graph would take (options, in effort order)
1. **A smarter call/return classifier.** A working deep tree needs more than "un-disable" — likely
   a better call/return classifier or a resync (pop-until-match) that also replays the cost
   accounting (`add_delta_costs`/`update_caller` in `check_roi` L453-493).
2. **Frame-pointer unwinder (new feature).** Build the guest with frame pointers
   (`RUSTFLAGS="-C force-frame-pointers=yes"` / C++ `-fno-omit-frame-pointer`) AND teach ziskemu to
   walk the FP chain instead of the `ra` heuristic. The guest rebuild alone does nothing — the
   emulator has no FP-based unwinder today.
3. **Manual regions.** `--profile-tags` / `start_profile_tag`/`end_profile_tag` give accurate
   hierarchical timing for regions the *guest* explicitly brackets — precise but requires
   instrumenting the guest.

## Build a patched ziskemu (to experiment)
```sh
git clone https://github.com/0xPolygonHermez/zisk && cd zisk
git apply /path/to/profiling/ziskemu-callstack.patch      # or your own
cargo build --release -p ziskemu --bin ziskemu            # ~50s cold, ~7s incremental (CPU-only)
./target/release/ziskemu -e guest.elf -i block.bin -X -S --profiler-output prof.json.gz
# load prof.json.gz at https://profiler.firefox.com
```

**Bottom line:** the accurate, useful artifact today is the **flat hotspot icicle** (module →
function by cost). A trustworthy deep call graph needs one of the above, not a quick patch.
