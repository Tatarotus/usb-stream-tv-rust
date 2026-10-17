#![forbid(unsafe_code)]

use std::env;
use std::io::{self, BufReader};
use std::path::Path;
use std::process::ExitCode;

use sparse_unpack::{unpack_stream, UnpackError, IO_BUF_SIZE};

fn run() -> Result<(), UnpackError> {
    let mut args = env::args_os();
    let _bin = args.next();
    let out_arg = match args.next() {
        Some(arg) => arg,
        None => return Err(UnpackError::Usage),
    };

    if args.next().is_some() {
        return Err(UnpackError::Usage);
    }

    let out_str = out_arg.to_string_lossy();
    if out_str == "-h" || out_str == "--help" {
        eprintln!("Usage: sparse_unpack <output_file>");
        return Ok(());
    }

    if out_arg.is_empty() {
        return Err(UnpackError::EmptyOutputPath);
    }
    let out_path = Path::new(&out_arg);

    let stdin = io::stdin();
    let reader = BufReader::with_capacity(IO_BUF_SIZE, stdin.lock());

    let stats = unpack_stream(reader, out_path)?;
    eprintln!(
        "[✓] Unpacked {} sparse chunks ({} bytes) into {} (virtual size {} bytes)",
        stats.chunks_count,
        stats.bytes_count,
        out_path.display(),
        stats.total_size
    );
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(UnpackError::Usage) => {
            eprintln!("Usage: sparse_unpack <output_file>");
            ExitCode::from(64)
        }
        Err(err) => {
            eprintln!("Error: {err}");
            ExitCode::FAILURE
        }
    }
}
