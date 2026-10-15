"""Run the production CDI importer and record phase/cgroup metrics.

Linux cgroup v2 is required for conversion CPU summaries. CPU peaks use 100ms
sampling and can straddle quota periods; average cores describe sustained usage.
"""

import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def cpu_stat():
    path = Path("/sys/fs/cgroup/cpu.stat")
    if path.exists():
        return {
            k: int(v)
            for k, v in (line.split() for line in path.read_text().splitlines())
        }
    result = {}
    path = Path("/sys/fs/cgroup/cpu/cpu.stat")
    if path.exists():
        result.update(
            {
                k: int(v)
                for k, v in (line.split() for line in path.read_text().splitlines())
            }
        )
    path = Path("/sys/fs/cgroup/cpuacct/cpuacct.usage")
    if path.exists():
        result["usage_nsec"] = int(path.read_text())
    return result


before = cpu_stat()
started = time.monotonic()
samples = []
stop_sampling = threading.Event()


def sample_cpu():
    previous_time = time.monotonic()
    previous = cpu_stat()
    while not stop_sampling.wait(0.1):
        now = time.monotonic()
        current = cpu_stat()
        if "usage_usec" in current and "usage_usec" in previous:
            samples.append(
                {
                    "elapsed_seconds": now - started,
                    "cores": (current["usage_usec"] - previous["usage_usec"])
                    / 1e6
                    / (now - previous_time),
                }
            )
        previous_time = now
        previous = current


sampler = threading.Thread(target=sample_cpu, daemon=True)
sampler.start()
metrics = {
    "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "phases": [],
    "cpu_stat_before": before,
}
process = subprocess.Popen(
    ["/usr/bin/virt-cdi-importer", "-alsologtostderr", "-v=1"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)
for line in process.stdout:
    print(line, end="", flush=True)
    if "phase:" in line or "Validating" in line or "TransferScratch" in line:
        metrics["phases"].append(
            {
                "elapsed_seconds": time.monotonic() - started,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "line": line.strip(),
                "cpu_stat": cpu_stat(),
            }
        )
code = process.wait()
stop_sampling.set()
sampler.join()
after = cpu_stat()
metrics.update(
    {
        "exit_code": code,
        "total_import_seconds": time.monotonic() - started,
        "cpu_stat_after": after,
        "cpu_stat_delta": {k: after[k] - before.get(k, 0) for k in after},
        "output_disk_bytes": Path("/data/disk.img").stat().st_size
        if Path("/data/disk.img").exists()
        else None,
    }
)
convert = next(
    (p for p in metrics["phases"] if "New phase: Convert" in p["line"]), None
)
resize = next((p for p in metrics["phases"] if "New phase: Resize" in p["line"]), None)
transfer = next(
    (p for p in metrics["phases"] if "New phase: TransferScratch" in p["line"]), None
)
if convert and resize:
    seconds = resize["elapsed_seconds"] - convert["elapsed_seconds"]
    cpu_seconds = (
        resize["cpu_stat"]["usage_usec"] - convert["cpu_stat"]["usage_usec"]
    ) / 1e6
    selected = [
        s["cores"]
        for s in samples
        if convert["elapsed_seconds"] + 0.1
        <= s["elapsed_seconds"]
        <= resize["elapsed_seconds"]
    ]
    metrics["conversion"] = {
        "wall_seconds": seconds,
        "cpu_seconds": cpu_seconds,
        "average_cpu_cores": cpu_seconds / seconds,
        "peak_cpu_cores_100ms": max(selected, default=0),
        "samples": len(selected),
        "throttled_seconds": (
            resize["cpu_stat"].get("throttled_usec", 0)
            - convert["cpu_stat"].get("throttled_usec", 0)
        )
        / 1e6,
        "throttled_periods": resize["cpu_stat"].get("nr_throttled", 0)
        - convert["cpu_stat"].get("nr_throttled", 0),
    }
if transfer and convert:
    seconds = convert["elapsed_seconds"] - transfer["elapsed_seconds"]
    metrics["transfer"] = {
        "wall_seconds": seconds,
        "mb_per_second": int(os.environ["BENCHMARK_SOURCE_BYTES"]) / 1e6 / seconds,
    }
print("BENCHMARK_METRICS " + json.dumps(metrics), flush=True)
sys.exit(code)
