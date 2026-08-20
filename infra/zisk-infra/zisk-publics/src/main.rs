//! zisk-publics — read the public values OUT of a ZisK proof, on stdout, as raw bytes.
//!
//! WHY: the ethproofs seam has to answer "is this proof about THIS block?", and the only party that can is
//! whoever holds the proof. The reference client (0xPolygonHermez/zisk-ethproofs) sends nothing but the
//! proof — no public values, no roots — precisely because the verifier is meant to establish that itself.
//!
//! WHAT IT DOES NOT DO: interpret the bytes. The layout of the guest's commitment (which 32-byte word is the
//! post-state root, which is the parent's, which is the block hash) is defined once, in cli/check-pv, and a
//! second definition here is how the two would drift. So this writes the bytes and stops.
//!
//! `verify()` is called before reading, and that is the whole point: it binds the values to the proof. Values
//! read without it would be a claim about a file, not about a proof.
use std::io::Write;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let path = match args.next() {
        Some(p) => p,
        None => {
            eprintln!("usage: zisk-publics <proof.bin> [n_bytes]   # raw public values -> stdout");
            std::process::exit(2);
        }
    };
    // 96 = three 32-byte words, which is what the Monad guest commits. Overridable rather than hardcoded:
    // another guest commits a different amount, and truncating it silently would be a wrong answer, not a
    // partial one.
    let n: usize = args.next().unwrap_or_else(|| "96".into()).parse()?;

    let proof = zisk_sdk::Proof::load(&path)?;
    proof.verify()?;

    let publics = proof.publics();
    let mut buf = vec![0u8; n];
    publics.read_slice(&mut buf);
    std::io::stdout().write_all(&buf)?;
    Ok(())
}
