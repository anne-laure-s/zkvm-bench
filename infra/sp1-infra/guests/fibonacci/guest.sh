# guests/fibonacci/guest.sh — guest contract for the Fibonacci example.
#
# Sourced by ./run. Available globals: ROOT, ELF, INPUT. Params via env: N.

GUEST_DIR="$ROOT/guests/fibonacci"

# Canonical id for the current params (drives file names).
guest_tag() { echo "n${N:?set N=<fibonacci index>}"; }

# Build the guest ELF and place it at $ELF.
guest_build_elf() {
  ( cd "$GUEST_DIR/program" && cargo prove build )
  local p
  p="$(find "$GUEST_DIR/program" -path '*/elf-compilation/*/release/fib-program' -type f 2>/dev/null | head -n1)"
  [[ -n "$p" ]] || { echo "ERROR: built fib-program ELF not found" >&2; return 1; }
  cp "$p" "$ELF"
}

# Generate the input (bincode FibInput) at $INPUT.
guest_gen_input() {
  ( cd "$GUEST_DIR" && cargo run --release --bin make-input -- --n "${N:?set N}" --output "$INPUT" )
}

# Pretty-print public values (bincode FibOutput) from $1.
guest_decode_pv() {
  ( cd "$GUEST_DIR" && cargo run --release --bin read-output -- --input "$1" )
}
