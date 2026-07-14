//! Shared types between host and guest.
//!
//! These types are serialized with bincode on the host (via the runner's
//! input file convention) and deserialized inside the SP1 zkVM by the guest.

use serde::{Deserialize, Serialize};

/// Input passed to the Fibonacci guest.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FibInput {
    /// Which Fibonacci number to compute.
    pub n: u32,
}

/// Public values committed by the Fibonacci guest.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FibOutput {
    /// The input n (re-committed for traceability).
    pub n: u32,
    /// Fibonacci(n).
    pub a: u32,
    /// Fibonacci(n+1).
    pub b: u32,
}
