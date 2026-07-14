//! Deserialize and pretty-print the public values produced by the guest.
//!
//! Usage:
//!   read-output --input pv.bin

use clap::Parser;
use fib_lib::FibOutput;

#[derive(Parser)]
struct Args {
    /// Path to public values file (saved by sp1-runner --public-values)
    #[arg(long)]
    input: std::path::PathBuf,
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let bytes = std::fs::read(&args.input)?;
    let output: FibOutput = bincode::deserialize(&bytes)?;

    println!("Public values from {}:", args.input.display());
    println!("  n = {}", output.n);
    println!("  fib(n)   = {}", output.a);
    println!("  fib(n+1) = {}", output.b);

    Ok(())
}
