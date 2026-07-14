//! Fibonacci guest program for SP1.
//!
//! Reads `FibInput`, computes Fibonacci(n) and Fibonacci(n+1),
//! and commits the result as `FibOutput`.

#![no_main]
sp1_zkvm::entrypoint!(main);

use fib_lib::{FibInput, FibOutput};

pub fn main() {
    // Read the input (bincode-deserialized from the single SP1Stdin buffer).
    let input: FibInput = sp1_zkvm::io::read();

    // Compute Fibonacci(n) and Fibonacci(n+1) iteratively (wrapping_add to
    // avoid overflow panics on large n).
    let mut a: u32 = 0;
    let mut b: u32 = 1;
    for _ in 0..input.n {
        let next = a.wrapping_add(b);
        a = b;
        b = next;
    }

    // Commit the public output.
    let output = FibOutput { n: input.n, a, b };
    sp1_zkvm::io::commit(&output);
}