//! Generate an input.bin file for the Fibonacci guest.

use clap::Parser;
use fib_lib::FibInput;

#[derive(Parser)]
struct Args {
    /// Which Fibonacci number to compute
    #[arg(long, default_value_t = 20)]
    n: u32,

    /// Output path
    #[arg(long, default_value = "fib_input.bin")]
    output: std::path::PathBuf,
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let input = FibInput { n: args.n };
    let bytes = bincode::serialize(&input)?;
    std::fs::write(&args.output, &bytes)?;
    println!("Wrote {} bytes to {}", bytes.len(), args.output.display());
    println!("Input: n = {}", args.n);
    Ok(())
}
