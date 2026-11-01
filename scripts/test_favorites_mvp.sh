#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export CARGO_BUILD_JOBS=1
cargo test -j1 -p fuse_ntfs --lib favorites -- --test-threads=1
cargo test -j1 -p fuse_ntfs --test integration_favorites_mvp -- --test-threads=1
python3 -m unittest \
  tests.test_live_favorites_template \
  tests.test_live_favorites_bridge \
  tests.test_live_favorites_export \
  tests.test_live_favorites_pcr \
  tests.test_favorites_source_manager -v
