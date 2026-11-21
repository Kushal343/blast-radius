# Blast Radius

This entry selects and schedules SQLGlot regression tests for the RWX Blast Radius challenge. It builds a dependency map by running the pinned SQLGlot revision with line tracing, fixture-read tracing, and dialect tracking. The map uses pytest node IDs so each run can match the test numbers in its own catalog.

The selector applies each diff to the pinned source in memory. Changes inside functions use line coverage and enclosing control flow. Changes to fixtures use recorded file reads. Initialization changes use observed dialect families and references to the affected names, with full-suite fallbacks for changes whose effect cannot be localized. For simple identity-to-equality edits, the selector can omit tests whose baseline operands produced the same result on every observed call with side-effect-free equality.

When the potential savings justify replay, an isolated Python process reruns a small set of compatible `unittest` cases with the patched library modules in memory. Tests stay selected if the rerun reports a failure, a failed subtest, a skip, or an incomplete result. Replay has per-patch and batch time limits. The scheduler compares worker counts from one to eight using integer milliseconds and includes each worker’s two-second overhead.

## Local benchmark

The official scorer reported 113.641 simulated seconds and zero misses on all 30 sample commits in three consecutive runs. The median rose to 119.5 seconds after adding measured Windows CPU time. The default Docker build regenerates the map from the pinned revision in the scorer’s Linux base image. The bundled map supports local development.

## Build and run the sample

Download the starter kit from the [official track page](https://www.joinbuilders.org/buildathon/tracks/blast-radius) and extract it alongside this repository as `sample-kit`. Run these commands from the repository root in a POSIX shell with Docker and Python 3.12 or newer.

```sh
docker build --platform linux/amd64 -t blast-radius .
sample="$(cd ../sample-kit/sample && pwd)"
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$sample/commits:/data/commits:ro" \
  --volume "$sample/tests.json:/data/tests.json:ro" \
  blast-radius --report-cpu > output.jsonl
python ../sample-kit/score.py "$sample" "$sample/answers.json" output.jsonl
```

Pass `--cpu-seconds` to the scorer with the reported `CPU_SECONDS` to include local CPU time. The official leaderboard measures CPU inside its Linux container and uses the median of three runs.

To develop without Docker, install the pinned packages from `requirements.txt` in a Python 3.12+ environment and obtain SQLGlot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. Generate the map with `python collect.py /path/to/sqlglot coverage.json.gz`, then run `python selector.py --data /path/to/sample --source /path/to/sqlglot --report-cpu`. Add `--coverage-only` to benchmark the coverage selection without pytest replay.
