# Fibonacci

Minimal SP1 guest project to validate the `sp1-infra` pipeline end-to-end.

## Layout

- `lib/`     — shared input/output types (used by both host and guest)
- `program/` — guest code, compiled to RISC-V via `cargo prove build`
- `tools/`   — host-side utilities to generate inputs and read outputs

## Build

### Build the guest ELF

```sh
cd program
cargo prove build
# Output: target/elf-compilation/riscv64im-succinct-zkvm-elf/release/fib-program
```

### Build the host tools

```sh
cargo build --release -p fib-tools
# Outputs in target/release/{make-input,read-output}
```

## Generate an input

```sh
./target/release/make-input --n 20 --output fib_input.bin
# Wrote 4 bytes to fib_input.bin
```

## Run via sp1-infra

In practice you don't build or ship anything by hand — the `./run` dispatcher (in `infra/sp1-infra/`)
drives this guest through its `guest.sh`, which calls the `make-input` / `read-output` tools above
under the hood. No manual `scp`, no `/workspace` paths:

```sh
# from infra/sp1-infra/
./run build-elf GUEST=fibonacci                 # -> ../../guests/fibonacci/fibonacci.elf
./run gen-input GUEST=fibonacci N=20            # -> ../../guests/fibonacci/inputs/n20.bin
./run execute   ELF=../../guests/fibonacci/fibonacci.elf \
                INPUT=../../guests/fibonacci/inputs/n20.bin      # local CPU, no proof (validate first)
./run prove     ELF=../../guests/fibonacci/fibonacci.elf \
                INPUT=../../guests/fibonacci/inputs/n20.bin REMOTE=user@host PORT=p
```

`prove` ships the ELF + input to the box, proves, and pulls back a run record under
`results/fibonacci/n20/<mode>-<ts>/`, printing the exact `verify` / `decode-pv` commands.

## Read the output back

```sh
./run decode-pv GUEST=fibonacci PV=results/fibonacci/n20/<run>/pv.bin
# Public values:
#   n = 20
#   fib(n)   = 6765
#   fib(n+1) = 10946
```

## Why this layout

This project is a canonical template for any `sp1-infra` guest:

- `lib/` defines the shared types — they live once, are used by both sides.
- `program/` is isolated as its own workspace so `cargo prove build` can use
  the RISC-V toolchain without interference.
- `tools/` are normal host Rust binaries; add new ones (e.g. data fetchers,
  output analyzers) as the project grows.

Reuse this layout for your other guests.