//! Generic SP1 runner — execute or prove arbitrary guest ELFs.
//!
//! A single binary for both local (CPU) and remote (CUDA) use. The GPU path
//! is gated behind the `cuda` cargo feature; build with `--features cuda` for
//! the Docker/GPU image, or without it for a plain CPU build.
//!
//! The prover backend defaults to CUDA (`SP1_PROVER=cuda`) unless the
//! environment already sets it. Running a CPU-only build with the CUDA default
//! will fail — export `SP1_PROVER=cpu` for local runs without a GPU.
//!
//! The runner is agnostic of the guest's input/output types: it streams the
//! input file as a single opaque buffer into SP1Stdin and saves public values
//! as raw bytes. Type-aware (de)serialization is left to guest-specific tools.

use std::fs;
use std::path::PathBuf;
use std::time::Instant;

use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use sp1_sdk::blocking::{ProveRequest, Prover, ProverClient};
use sp1_sdk::{Elf, HashableKey, ProvingKey, SP1ProofMode, SP1ProofWithPublicValues, SP1Stdin};

#[derive(Parser, Debug)]
#[command(author, version, about = "Generic SP1 runner — execute or prove arbitrary ELFs", long_about = None)]
struct Args {
    /// Path to the guest program ELF.
    /// Required for execute/prove/verify; not needed for --emit-stdin.
    #[arg(long)]
    elf: Option<PathBuf>,

    /// Path to the input file (raw bytes, passed via SP1Stdin).
    /// Required for execute/prove modes and --emit-stdin; ignored in verify mode.
    #[arg(long)]
    input: Option<PathBuf>,

    /// Wrap --input into a bincode-serialized SP1Stdin written to this path,
    /// then exit. This is the format sp1-cluster's CLI expects as its stdin_file
    /// (it does `bincode::deserialize::<SP1Stdin>`). No ELF / prover needed.
    #[arg(long)]
    emit_stdin: Option<PathBuf>,

    /// Execution mode
    #[arg(long, value_enum, default_value_t = Mode::Execute)]
    mode: Mode,

    /// Path to an existing proof file to verify (verify mode only)
    #[arg(long)]
    proof: Option<PathBuf>,

    /// Output path for proof file (prove modes only)
    #[arg(long)]
    output: Option<PathBuf>,

    /// Public values (raw bytes). In execute/prove modes: output path where the
    /// proof's public values are written. In verify mode: the EXPECTED public
    /// values the proof must commit to (input, required).
    #[arg(long)]
    public_values: Option<PathBuf>,

    /// Output path for verifying key (32 bytes, hex)
    #[arg(long)]
    vkey: Option<PathBuf>,

    /// Output path for JSON report (cycles, timings, vkey hash)
    #[arg(long)]
    report: Option<PathBuf>,

    /// Skip proof verification after generation (default: verify)
    #[arg(long)]
    skip_verify: bool,

    /// Execute mode only: skip gas calculation. This drops SP1's extra
    /// gas-estimation pass (it re-processes the whole trace to build gas +
    /// opcode/syscall counts), so execution is faster — but the report's gas
    /// and per-opcode breakdown are then empty AND the reported cycle count
    /// comes out as 0. For the deterministic cycle count, use a gas-on run;
    /// take the timing from a --no-gas run (see guests/monad/README.md).
    #[arg(long)]
    no_gas: bool,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
enum Mode {
    Execute,
    ProveCore,
    ProveCompressed,
    ProveGroth16,
    /// Verify an existing proof (requires --proof and --elf; no guest input needed).
    /// --public-values is OPTIONAL: given → also bind the proof to those expected PV;
    /// omitted → cryptographic verify only.
    Verify,
}

/// Create the parent directory of `path` if it doesn't exist, so writing an
/// output file never fails just because its folder is missing.
fn ensure_parent(path: &std::path::Path) -> Result<()> {
    if let Some(dir) = path.parent() {
        if !dir.as_os_str().is_empty() {
            fs::create_dir_all(dir)
                .with_context(|| format!("creating directory {}", dir.display()))?;
        }
    }
    Ok(())
}

fn main() -> Result<()> {
    // Default to the CUDA prover unless the caller already chose a backend.
    // CPU-only builds/hosts must export SP1_PROVER=cpu explicitly.
    if std::env::var_os("SP1_PROVER").is_none() {
        // Safe: called at the very start of main, before any threads spawn.
        unsafe { std::env::set_var("SP1_PROVER", "cuda") };
    }
    sp1_sdk::utils::setup_logger();
    let args = Args::parse();

    // ──────── EMIT-STDIN (wrap a raw witness into bincode(SP1Stdin)) ────────
    // No ELF/prover needed: build the same single-buffer SP1Stdin the prover
    // would (write_slice == RSP host's write_vec), serialize it, and exit.
    if let Some(out) = &args.emit_stdin {
        let input_path = args
            .input
            .as_ref()
            .context("--input is required with --emit-stdin")?;
        let input_bytes = fs::read(input_path)
            .with_context(|| format!("reading input {}", input_path.display()))?;
        let mut stdin = SP1Stdin::new();
        stdin.write_slice(&input_bytes);
        let bytes = bincode::serialize(&stdin).context("serializing SP1Stdin")?;
        ensure_parent(out)?;
        fs::write(out, &bytes).with_context(|| format!("writing SP1Stdin to {}", out.display()))?;
        println!(
            "SP1Stdin written: {} ({} bytes) from {} ({} input bytes)",
            out.display(),
            bytes.len(),
            input_path.display(),
            input_bytes.len()
        );
        return Ok(());
    }

    println!("== sp1-runner ==");
    let elf_path = args
        .elf
        .as_ref()
        .context("--elf is required for execute/prove/verify modes")?;
    println!("ELF      : {}", elf_path.display());
    println!("Mode     : {:?}", args.mode);

    let elf_bytes =
        fs::read(elf_path).with_context(|| format!("reading ELF {}", elf_path.display()))?;
    println!("ELF size : {} bytes", elf_bytes.len());
    let elf = Elf::from(elf_bytes);

    // Execute/prove modes consume an input; verify reloads a finished proof.
    let stdin = if args.mode == Mode::Verify {
        None
    } else {
        let input_path = args
            .input
            .as_ref()
            .context("--input is required for execute/prove modes")?;
        let input_bytes = fs::read(input_path)
            .with_context(|| format!("reading input {}", input_path.display()))?;
        println!("Input    : {} ({} bytes)", input_path.display(), input_bytes.len());
        let mut s = SP1Stdin::new();
        s.write_slice(&input_bytes);
        Some(s)
    };

    // Prover backend.
    //
    // NETWORK build (the 16-GPU sp1-cluster client): use a *hosted* network prover. hosted() puts
    // the client in NetworkMode::Reserved + FulfillmentStrategy::Hosted — the mode the self-hosted
    // gateway expects (it skips the auction's get_proof_request_params call). It reads
    // NETWORK_RPC_URL / NETWORK_PRIVATE_KEY from the env like the old from_env() network path.
    //
    // ⚠️ hosted() does NOT, by itself, skip the pre-submit LOCAL execution on the BLOCKING API.
    // In sp1-sdk 6.2.4 the ASYNC prover honors `hosted` (skip_simulation=true + u64::MAX cycle/gas
    // limits), but the BLOCKING prove() ignores that flag and hardcodes skip_simulation=false /
    // limits=None. So despite hosted(), the whole guest was re-executed on THIS box's CPU (tens of
    // seconds on large blocks) just to derive an auction cycle/gas limit — pure serial latency
    // before any GPU starts, and pointless on a reserved cluster we own. We disable it explicitly
    // on the prove request below (skip_simulation + u64::MAX limits). See the prove call.
    #[cfg(feature = "network")]
    let prover = ProverClient::builder().hosted().build();
    // LOCAL build (CPU / CUDA / mock): pick the backend from SP1_PROVER, as before.
    #[cfg(not(feature = "network"))]
    let prover = ProverClient::from_env();
    println!("Prover initialized");
    let t_start = Instant::now();

    let report_json = match args.mode {
        // ──────── EXECUTE ────────
        Mode::Execute => {
            let mut exec_req = prover.execute(elf, stdin.expect("input present in execute mode"));
            if args.no_gas {
                exec_req = exec_req.calculate_gas(false);
            }
            let (public_values, exec_report) = exec_req
                .run()
                .map_err(|e| anyhow::anyhow!("execute failed: {e}"))?;

            let dt = t_start.elapsed();
            let cycles = exec_report.total_instruction_count();
            let pv_bytes = public_values.as_slice();

            println!("Cycles        : {cycles}");
            println!("Time          : {:.2}s", dt.as_secs_f64());
            println!("Public values : {} bytes", pv_bytes.len());

            // Sauvegarder public values
            if let Some(pv_path) = &args.public_values {
                ensure_parent(pv_path)?;
                fs::write(pv_path, pv_bytes)
                    .with_context(|| format!("writing public values to {}", pv_path.display()))?;
                println!("PV saved      : {}", pv_path.display());
            }

            // Human-readable breakdown — same format SP1/RSP print (gas + sorted
            // opcode counts + syscall counts), via ExecutionReport's Display.
            println!("--- Execution report ---");
            print!("{exec_report}");

            // Full execution report as JSON: opcode & syscall counts, cycle
            // tracker, touched memory, gas. Stable only within this SP1 version.
            let exec_report_json =
                serde_json::to_value(&exec_report).unwrap_or(serde_json::Value::Null);

            serde_json::json!({
                "mode": "execute",
                "cycles": cycles,
                "elapsed_secs": dt.as_secs_f64(),
                "public_values_bytes": pv_bytes.len(),
                "total_syscalls": exec_report.total_syscall_count(),
                "touched_memory_addresses": exec_report.touched_memory_addresses,
                "exit_code": exec_report.exit_code,
                "gas": exec_report.gas(),
                "execution_report": exec_report_json,
            })
        }
        // ──────── PROVE ────────
        Mode::ProveCore | Mode::ProveCompressed | Mode::ProveGroth16 => {
            let t_setup = Instant::now();
            let pk = prover
                .setup(elf)
                .map_err(|e| anyhow::anyhow!("setup failed: {e}"))?;
            let setup_dt = t_setup.elapsed();
            println!("Setup         : {:.2}s", setup_dt.as_secs_f64());

            let vk = pk.verifying_key();
            let vkey_hex = vk.bytes32();
            println!("Vkey hash     : {vkey_hex}");

            // Sauvegarder vkey
            if let Some(vkey_path) = &args.vkey {
                ensure_parent(vkey_path)?;
                fs::write(vkey_path, &vkey_hex)
                    .with_context(|| format!("writing vkey to {}", vkey_path.display()))?;
                println!("Vkey saved    : {}", vkey_path.display());
            }

            let proof_mode = match args.mode {
                Mode::ProveCore => SP1ProofMode::Core,
                Mode::ProveCompressed => SP1ProofMode::Compressed,
                Mode::ProveGroth16 => SP1ProofMode::Groth16,
                _ => unreachable!(),
            };

            let t_prove = Instant::now();
            let req = prover
                .prove(&pk, stdin.expect("input present in prove mode"))
                .mode(proof_mode);
            // NETWORK build only: kill the pre-submit LOCAL execution (the blocking prove() would
            // otherwise re-run the guest here — see the prover comment above). skip_simulation(true)
            // drops that pass; the explicit u64::MAX limits stop the fallback to the Reserved-mode
            // default (100M cycles), which would make large blocks unprovable. These builder methods
            // exist only on the network request, so the cfg gate keeps the local CPU/CUDA build (a
            // different builder type without them) compiling.
            #[cfg(feature = "network")]
            let req = req
                .skip_simulation(true)
                .cycle_limit(u64::MAX)
                .gas_limit(u64::MAX);
            let proof = req
                .run()
                .map_err(|e| anyhow::anyhow!("prove failed: {e}"))?;
            let prove_dt = t_prove.elapsed();
            println!("Prove         : {:.2}s", prove_dt.as_secs_f64());

            let pv_bytes = proof.public_values.as_slice().to_vec();
            println!("Public values : {} bytes", pv_bytes.len());

            // Vérification (sauf si --skip-verify)
            let verify_dt = if !args.skip_verify {
                let t_verify = Instant::now();
                prover
                    .verify(&proof, &vk, None)
                    .map_err(|e| anyhow::anyhow!("verification failed: {e}"))?;
                let dt = t_verify.elapsed();
                println!("Verify OK     : {:.2}s", dt.as_secs_f64());
                Some(dt.as_secs_f64())
            } else {
                println!("Verify        : SKIPPED");
                None
            };

            // Save proof
            let mut proof_bytes: Option<u64> = None;
            if let Some(out) = &args.output {
                ensure_parent(out)?;
                proof
                    .save(out)
                    .map_err(|e| anyhow::anyhow!("save proof failed: {e}"))?;
                proof_bytes = fs::metadata(out).ok().map(|m| m.len());
                if let Some(n) = proof_bytes {
                    println!("Proof saved   : {} ({n} bytes)", out.display());
                } else {
                    println!("Proof saved   : {}", out.display());
                }
            }

            // Save public values
            if let Some(pv_path) = &args.public_values {
                ensure_parent(pv_path)?;
                fs::write(pv_path, &pv_bytes)
                    .with_context(|| format!("writing public values to {}", pv_path.display()))?;
                println!("PV saved      : {}", pv_path.display());
            }

            let total_dt = t_start.elapsed();
            println!("Total         : {:.2}s", total_dt.as_secs_f64());

            // kebab-case (matches the CLI spelling + the shared report-schema; zisk/openvm
            // emit kebab too). `{:?}` would give PascalCase and break a `mode`-keyed consumer.
            let mode_name = args.mode.to_possible_value().expect("Mode is a ValueEnum").get_name().to_owned();

            // report-schema.md: `verified` is bool|null where null = "not run inline".
            // If we reached here without --skip-verify, verification ran and passed (a
            // failure would have bailed above), so emit `true`; else `null`, NOT `false`.
            let verified = if args.skip_verify {
                serde_json::Value::Null
            } else {
                serde_json::Value::Bool(true)
            };

            serde_json::json!({
                "mode": mode_name,
                "setup_secs": setup_dt.as_secs_f64(),
                "prove_secs": prove_dt.as_secs_f64(),
                "verify_secs": verify_dt,
                "total_secs": total_dt.as_secs_f64(),
                "vkey_hash": vkey_hex,
                "public_values_bytes": pv_bytes.len(),
                "proof_bytes": proof_bytes,
                "verified": verified,
            })
        }
        // ──────── VERIFY ────────
        Mode::Verify => {
            let proof_path = args
                .proof
                .as_ref()
                .context("--proof is required in verify mode")?;

            // Re-derive the verifying key from the ELF (vk depends only on the ELF).
            let t_setup = Instant::now();
            let pk = prover
                .setup(elf)
                .map_err(|e| anyhow::anyhow!("setup failed: {e}"))?;
            let vk = pk.verifying_key();
            let vkey_hex = vk.bytes32();
            println!("Setup         : {:.2}s", t_setup.elapsed().as_secs_f64());
            println!("Vkey hash     : {vkey_hex}");

            let proof = SP1ProofWithPublicValues::load(proof_path)
                .with_context(|| format!("loading proof {}", proof_path.display()))?;
            println!("Proof loaded  : {}", proof_path.display());

            // OPTIONAL: expected public values to BIND the proof to. Given → cross-check (a valid proof
            // for *different* PV would otherwise pass). Omitted → cryptographic verify only, which is
            // enough for cli/ethproofs-mock's --verify-cmd (it has the proof, not the block's expected PV).
            let expected_pv = args
                .public_values
                .as_ref()
                .map(|p| fs::read(p).with_context(|| format!("reading expected public values {}", p.display())))
                .transpose()?;

            let t_verify = Instant::now();
            prover
                .verify(&proof, &vk, None)
                .map_err(|e| anyhow::anyhow!("verification failed: {e}"))?;
            let verify_dt = t_verify.elapsed();
            println!("Verify OK     : {:.2}s", verify_dt.as_secs_f64());

            // Bind the proof to the expected public values, when provided.
            let proof_pv = proof.public_values.as_slice();
            let pv_match = match &expected_pv {
                Some(exp) => {
                    if proof_pv != exp.as_slice() {
                        anyhow::bail!(
                            "public values mismatch: proof commits {} bytes that differ from the expected {} bytes",
                            proof_pv.len(),
                            exp.len()
                        );
                    }
                    println!("PV match OK   : {} bytes", proof_pv.len());
                    true
                }
                None => {
                    println!("PV check      : skipped (no --public-values — cryptographic verify only)");
                    false
                }
            };

            serde_json::json!({
                "mode": "verify",
                "verify_secs": verify_dt.as_secs_f64(),
                "vkey_hash": vkey_hex,
                "public_values_bytes": proof_pv.len(),
                "public_values_match": pv_match,
                "verified": true,
            })
        }
    };

    if let Some(report_path) = args.report {
        ensure_parent(&report_path)?;
        fs::write(&report_path, serde_json::to_string_pretty(&report_json)?)?;
        println!("Report        : {}", report_path.display());
    }

    Ok(())
}
