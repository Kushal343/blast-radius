# Blast Radius

This entry selects and schedules SQLGlot regression tests for the RWX Blast Radius challenge. It builds a dependency map by running the pinned SQLGlot revision with line tracing, fixture-read tracing, and dialect tracking. The map uses pytest node IDs so each run can match the test numbers in its own catalog.

The selector applies each diff to the pinned source in memory. Changes inside functions use line coverage and enclosing control flow. Changes to fixtures use recorded file reads. Initialization changes use observed dialect families and references to the affected names, with full-suite fallbacks for changes whose effect cannot be localized. For supported predicate edits, the selector compares the new predicate against baseline operands. For literal dictionary value edits with auditable data flow, it follows the observed readers and lookup keys.

When the potential savings justify replay, an isolated Python process reruns a small set of compatible `unittest` cases with the patched library modules in memory. For eligible executor tests, replay compares regenerated execution plans and reference SQL for the affected queries on unchanged fixture inputs. Any changed plan, affected executor code, or incomplete result keeps the test selected. Replay has per-patch and batch time limits. The scheduler compares worker counts from one to eight using integer milliseconds and includes each worker’s two-second overhead.

The build also measures which tests enter individual conditional operands, invoke callbacks stored in dispatch dictionaries, and read inherited scalar class settings. Each closure created by an eligible factory gets its own profiling identity. The selector checks the complete syntax-tree difference before using those measurements. On Linux, replay workers inherit preloaded third-party dependencies and load the patched SQLGlot library in a fresh child process.

## Local benchmark

The default Docker build uses the committed Linux profile. It was generated from the pinned revision in the scorer’s Python image. The collector remains in this repository so the profile can be regenerated. The selector reads only the patch and test catalog inputs during scoring.

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
